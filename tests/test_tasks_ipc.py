"""Tests for IPC writer and watcher."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


class TestIPCWriter:
    def test_write_task_creates_file(self, tmp_path: Path) -> None:
        from nanobot.tasks.ipc_writer import write_task

        runtime_dir = tmp_path / "runtime"
        tid = write_task(runtime_dir, {"task": "do something"}, label="test-task")

        task_dir = runtime_dir / "ipc" / "main" / "tasks"
        assert task_dir.exists()
        files = list(task_dir.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["task_id"] == tid
        assert data["payload"]["task"] == "do something"
        assert data["label"] == "test-task"

    def test_write_task_explicit_id(self, tmp_path: Path) -> None:
        from nanobot.tasks.ipc_writer import write_task

        runtime_dir = tmp_path / "runtime"
        tid = write_task(runtime_dir, {}, task_id="fixed-id-123")
        assert tid == "fixed-id-123"
        f = runtime_dir / "ipc" / "main" / "tasks" / "fixed-id-123.json"
        assert f.exists()

    def test_write_result(self, tmp_path: Path) -> None:
        from nanobot.tasks.ipc_writer import write_result

        runtime_dir = tmp_path / "runtime"
        write_result(runtime_dir, "t1", "great result")
        f = runtime_dir / "ipc" / "main" / "results" / "t1.json"
        assert f.exists()
        data = json.loads(f.read_text())
        assert data["result"] == "great result"
        assert data["status"] == "done"

    def test_write_message(self, tmp_path: Path) -> None:
        from nanobot.tasks.ipc_writer import write_message

        runtime_dir = tmp_path / "runtime"
        mid = write_message(runtime_dir, {"hello": "world"})
        f = runtime_dir / "ipc" / "main" / "messages" / f"{mid}.json"
        assert f.exists()
        data = json.loads(f.read_text())
        assert data["hello"] == "world"

    def test_custom_namespace(self, tmp_path: Path) -> None:
        from nanobot.tasks.ipc_writer import write_task

        runtime_dir = tmp_path / "runtime"
        tid = write_task(runtime_dir, {}, namespace="worker-1")
        f = runtime_dir / "ipc" / "worker-1" / "tasks" / f"{tid}.json"
        assert f.exists()

    def test_no_tmp_file_leftover(self, tmp_path: Path) -> None:
        """Atomic write should not leave .tmp files behind."""
        from nanobot.tasks.ipc_writer import write_task

        runtime_dir = tmp_path / "runtime"
        write_task(runtime_dir, {})
        task_dir = runtime_dir / "ipc" / "main" / "tasks"
        tmp_files = list(task_dir.glob("*.tmp"))
        assert tmp_files == []


class TestIPCWatcher:
    @pytest.mark.asyncio
    async def test_task_file_triggers_db_creation(self, tmp_path: Path) -> None:
        from nanobot.tasks.db import get_task, init_db
        from nanobot.tasks.ipc_watcher import IPCWatcher
        from nanobot.tasks.ipc_writer import write_task
        from nanobot.tasks.worker_pool import WorkerPool

        runtime_dir = tmp_path / "runtime"
        conn = await init_db(runtime_dir)
        try:
            pool = WorkerPool(conn, runtime_dir, max_workers=1, task_timeout=10.0)
            watcher = IPCWatcher(conn, pool, runtime_dir, poll_interval=0.05)

            # Write a task IPC file before watcher starts
            write_task(runtime_dir, {"task": "hello"}, task_id="ipc-task-01")

            # Run watcher for a short time
            watcher_task = asyncio.create_task(watcher.run())
            await asyncio.sleep(0.3)
            watcher.stop()
            await watcher_task

            row = await get_task(conn, "ipc-task-01")
            assert row is not None, "Task should have been created by watcher"
        finally:
            await pool.shutdown()
            await conn.close()

    @pytest.mark.asyncio
    async def test_ipc_file_consumed_after_processing(self, tmp_path: Path) -> None:
        from nanobot.tasks.db import init_db
        from nanobot.tasks.ipc_watcher import IPCWatcher
        from nanobot.tasks.ipc_writer import write_task
        from nanobot.tasks.worker_pool import WorkerPool

        runtime_dir = tmp_path / "runtime"
        conn = await init_db(runtime_dir)
        try:
            pool = WorkerPool(conn, runtime_dir, max_workers=1, task_timeout=10.0)
            watcher = IPCWatcher(conn, pool, runtime_dir, poll_interval=0.05)

            write_task(runtime_dir, {"task": "test"}, task_id="ipc-task-02")
            task_file = runtime_dir / "ipc" / "main" / "tasks" / "ipc-task-02.json"
            assert task_file.exists()

            watcher_task = asyncio.create_task(watcher.run())
            await asyncio.sleep(0.3)
            watcher.stop()
            await watcher_task

            assert not task_file.exists(), "IPC file should be deleted after processing"
        finally:
            await pool.shutdown()
            await conn.close()

    @pytest.mark.asyncio
    async def test_idempotent_task_submission(self, tmp_path: Path) -> None:
        """Submitting the same task_id twice via IPC should not duplicate it."""
        from nanobot.tasks.db import init_db, list_tasks
        from nanobot.tasks.ipc_watcher import IPCWatcher
        from nanobot.tasks.ipc_writer import write_task
        from nanobot.tasks.worker_pool import WorkerPool

        runtime_dir = tmp_path / "runtime"
        conn = await init_db(runtime_dir)
        try:
            pool = WorkerPool(conn, runtime_dir, max_workers=1, task_timeout=10.0)
            watcher = IPCWatcher(conn, pool, runtime_dir, poll_interval=0.05)

            write_task(runtime_dir, {"task": "x"}, task_id="dup-task-01")
            write_task(runtime_dir, {"task": "x"}, task_id="dup-task-01")

            watcher_task = asyncio.create_task(watcher.run())
            await asyncio.sleep(0.4)
            watcher.stop()
            await watcher_task

            rows = await list_tasks(conn)
            dup_rows = [r for r in rows if r["id"] == "dup-task-01"]
            assert len(dup_rows) == 1, "Duplicate IPC files should not create duplicate tasks"
        finally:
            await pool.shutdown()
            await conn.close()

    @pytest.mark.asyncio
    async def test_bad_json_moved_to_errors(self, tmp_path: Path) -> None:
        from nanobot.tasks.db import init_db
        from nanobot.tasks.ipc_watcher import IPCWatcher
        from nanobot.tasks.worker_pool import WorkerPool

        runtime_dir = tmp_path / "runtime"
        conn = await init_db(runtime_dir)
        try:
            pool = WorkerPool(conn, runtime_dir, max_workers=1, task_timeout=10.0)
            watcher = IPCWatcher(conn, pool, runtime_dir, poll_interval=0.05)

            # Write a malformed JSON file
            tasks_dir = runtime_dir / "ipc" / "main" / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)
            bad_file = tasks_dir / "bad-file.json"
            bad_file.write_text("not valid json{{{{")

            watcher_task = asyncio.create_task(watcher.run())
            await asyncio.sleep(0.3)
            watcher.stop()
            await watcher_task

            errors_dir = runtime_dir / "ipc" / "errors"
            assert not bad_file.exists()
            assert (errors_dir / "bad-file.json").exists()
        finally:
            await pool.shutdown()
            await conn.close()
