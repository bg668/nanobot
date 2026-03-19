"""Tests for GroupQueue concurrency control."""

from __future__ import annotations

import asyncio

import pytest


class TestGroupQueue:
    @pytest.mark.asyncio
    async def test_immediate_launch_when_slots_free(self) -> None:
        from nanobot.tasks.group_queue import GroupQueue

        q = GroupQueue(max_workers=2)
        started = []

        async def job(name: str) -> None:
            started.append(name)
            await asyncio.sleep(0)

        q.enqueue("a", lambda: job("a"))
        q.enqueue("b", lambda: job("b"))
        await asyncio.sleep(0.05)
        assert sorted(started) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_excess_tasks_queued(self) -> None:
        from nanobot.tasks.group_queue import GroupQueue

        q = GroupQueue(max_workers=1)
        launched = []
        done_event = asyncio.Event()

        async def slow(name: str) -> None:
            launched.append(name)
            await done_event.wait()

        q.enqueue("a", lambda: slow("a"))
        launched_immediately = q.enqueue("b", lambda: slow("b"))
        await asyncio.sleep(0)

        assert "a" in launched
        assert "b" not in launched
        assert launched_immediately is False
        assert q.waiting_count == 1

        done_event.set()
        await asyncio.sleep(0.05)
        assert "b" in launched

    @pytest.mark.asyncio
    async def test_drain_on_completion(self) -> None:
        from nanobot.tasks.group_queue import GroupQueue

        q = GroupQueue(max_workers=2)
        results: list[str] = []
        release = asyncio.Event()

        async def blocking(name: str) -> None:
            await release.wait()
            results.append(name)

        for name in ["a", "b", "c", "d"]:
            q.enqueue(name, lambda n=name: blocking(n))

        await asyncio.sleep(0)
        assert q.active_count == 2
        assert q.waiting_count == 2

        release.set()
        await asyncio.sleep(0.1)

        assert sorted(results) == ["a", "b", "c", "d"]
        assert q.active_count == 0
        assert q.waiting_count == 0

    @pytest.mark.asyncio
    async def test_status(self) -> None:
        from nanobot.tasks.group_queue import GroupQueue

        q = GroupQueue(max_workers=3)
        s = q.status()
        assert s["max_workers"] == 3
        assert s["active"] == 0
        assert s["waiting"] == 0

    @pytest.mark.asyncio
    async def test_cancel_active(self) -> None:
        from nanobot.tasks.group_queue import GroupQueue

        q = GroupQueue(max_workers=2)
        cancelled = asyncio.Event()

        async def long_job() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        q.enqueue("task-x", long_job)
        await asyncio.sleep(0)
        assert q.cancel("task-x") is True
        await asyncio.sleep(0.05)
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_cancel_waiting(self) -> None:
        from nanobot.tasks.group_queue import GroupQueue

        q = GroupQueue(max_workers=1)
        hold = asyncio.Event()

        async def blocking() -> None:
            await hold.wait()

        async def never() -> None:
            pass

        q.enqueue("blocker", blocking)
        q.enqueue("waiting", never)
        await asyncio.sleep(0)
        assert q.waiting_count == 1
        assert q.cancel("waiting") is True
        assert q.waiting_count == 0

    def test_invalid_max_workers(self) -> None:
        from nanobot.tasks.group_queue import GroupQueue

        with pytest.raises(ValueError):
            GroupQueue(max_workers=0)


class TestWorkerPool:
    @pytest.mark.asyncio
    async def test_submit_and_complete(self, tmp_path) -> None:
        from nanobot.tasks.db import create_task, get_task, init_db
        from nanobot.tasks.worker_pool import WorkerPool

        runtime_dir = tmp_path / "runtime"
        conn = await init_db(runtime_dir)
        try:
            pool = WorkerPool(conn, runtime_dir, max_workers=2, task_timeout=30.0)
            tid = await create_task(conn, payload={"task": "echo hello"})
            pool.submit(tid, {"task": "echo hello"})
            # Poll until task reaches a terminal state
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                row = await get_task(conn, tid)
                if row and row["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)
            else:
                row = await get_task(conn, tid)
            assert row is not None
            assert row["status"] in ("done", "failed"), f"Unexpected status: {row['status']}"
        finally:
            await pool.shutdown()
            await conn.close()

    @pytest.mark.asyncio
    async def test_concurrency_limit(self, tmp_path) -> None:
        from nanobot.tasks.db import create_task, init_db, list_tasks
        from nanobot.tasks.worker_pool import WorkerPool

        runtime_dir = tmp_path / "runtime"
        conn = await init_db(runtime_dir)
        try:
            pool = WorkerPool(conn, runtime_dir, max_workers=2, task_timeout=30.0)
            tids = []
            for i in range(4):
                tid = await create_task(conn, payload={"task": f"task-{i}"})
                tids.append(tid)
                pool.submit(tid, {"task": f"task-{i}"})

            assert pool.status["active"] <= 2
            # Poll until all tasks reach terminal states
            deadline = asyncio.get_event_loop().time() + 20.0
            while asyncio.get_event_loop().time() < deadline:
                rows = await list_tasks(conn)
                if all(r["status"] in ("done", "failed") for r in rows):
                    break
                await asyncio.sleep(0.1)

            rows = await list_tasks(conn)
            statuses = [r["status"] for r in rows]
            assert all(s in ("done", "failed") for s in statuses)
        finally:
            await pool.shutdown()
            await conn.close()
