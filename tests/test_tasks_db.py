"""Tests for the task DB layer (nanobot.tasks.db)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


class TestInitDb:
    @pytest.mark.asyncio
    async def test_creates_tables(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import init_db

        conn = await init_db(runtime_dir)
        try:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
            assert "tasks" in tables
            assert "run_log" in tables
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_idempotent(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import init_db

        conn1 = await init_db(runtime_dir)
        await conn1.close()
        conn2 = await init_db(runtime_dir)
        await conn2.close()


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_creates_pending_task(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import create_task, get_task, init_db

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={"task": "hello"}, label="greet")
            row = await get_task(conn, tid)
            assert row is not None
            assert row["status"] == "pending"
            assert row["label"] == "greet"
            assert json.loads(row["payload"]) == {"task": "hello"}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_explicit_task_id(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import create_task, get_task, init_db

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={}, task_id="myid-001")
            assert tid == "myid-001"
            row = await get_task(conn, "myid-001")
            assert row is not None
        finally:
            await conn.close()


class TestClaimTask:
    @pytest.mark.asyncio
    async def test_claim_pending(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, get_task, init_db

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            ok = await claim_task(conn, tid, "worker-1")
            assert ok is True
            row = await get_task(conn, tid)
            assert row["status"] == "running"
            assert row["worker_id"] == "worker-1"
            assert row["attempts"] == 1
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_cannot_double_claim(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, init_db

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            await claim_task(conn, tid, "worker-1")
            ok2 = await claim_task(conn, tid, "worker-2")
            assert ok2 is False
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_cannot_claim_done_task(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, init_db, mark_done

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            await claim_task(conn, tid, "w1")
            await mark_done(conn, tid, "ok")
            ok = await claim_task(conn, tid, "w2")
            assert ok is False
        finally:
            await conn.close()


class TestMarkDoneAndFailed:
    @pytest.mark.asyncio
    async def test_mark_done(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, get_task, init_db, mark_done

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            await claim_task(conn, tid, "w1")
            await mark_done(conn, tid, "great result")
            row = await get_task(conn, tid)
            assert row["status"] == "done"
            assert row["result"] == "great result"
            assert row["finished_at"] is not None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_mark_failed(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, get_task, init_db, mark_failed

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            await claim_task(conn, tid, "w1")
            await mark_failed(conn, tid, "boom!")
            row = await get_task(conn, tid)
            assert row["status"] == "failed"
            assert row["error"] == "boom!"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_run_log_populated(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, init_db, mark_done

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            await claim_task(conn, tid, "w1")
            await mark_done(conn, tid, "ok")
            async with conn.execute(
                "SELECT * FROM run_log WHERE task_id=?", (tid,)
            ) as cur:
                log_rows = await cur.fetchall()
            assert len(log_rows) == 1
            assert log_rows[0]["status"] == "done"
        finally:
            await conn.close()


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_all(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import create_task, init_db, list_tasks

        conn = await init_db(runtime_dir)
        try:
            for i in range(3):
                await create_task(conn, payload={"i": i})
            rows = await list_tasks(conn)
            assert len(rows) == 3
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_filter_by_status(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, init_db, list_tasks, mark_done

        conn = await init_db(runtime_dir)
        try:
            t1 = await create_task(conn, payload={})
            t2 = await create_task(conn, payload={})
            await claim_task(conn, t1, "w1")
            await mark_done(conn, t1, "done")
            rows = await list_tasks(conn, status="pending")
            ids = [r["id"] for r in rows]
            assert t2 in ids
            assert t1 not in ids
        finally:
            await conn.close()


class TestPollTask:
    @pytest.mark.asyncio
    async def test_poll_returns_done(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import claim_task, create_task, init_db, mark_done, poll_task

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            await claim_task(conn, tid, "w1")
            await mark_done(conn, tid, "yay")

            row = await poll_task(conn, tid, timeout=5.0, interval=0.1)
            assert row is not None
            assert row["status"] == "done"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_poll_times_out(self, runtime_dir: Path) -> None:
        from nanobot.tasks.db import create_task, init_db, poll_task

        conn = await init_db(runtime_dir)
        try:
            tid = await create_task(conn, payload={})
            row = await poll_task(conn, tid, timeout=0.3, interval=0.1)
            assert row is None
        finally:
            await conn.close()
