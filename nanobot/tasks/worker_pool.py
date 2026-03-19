"""WorkerPool: launches subagent subprocesses with concurrency control.

Each task is run in its own subprocess (``subagent_entry.py``).  The pool
enforces MAX_WORKERS via GroupQueue, handles timeouts, and writes results back
to the SQLite task DB.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from nanobot.tasks.db import claim_task, mark_done, mark_failed
from nanobot.tasks.group_queue import GroupQueue

_MAX_STDERR_OUTPUT = 500
_ENTRY_MODULE = "nanobot.tasks.subagent_entry"


class WorkerPool:
    """Manages subagent subprocess concurrency."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        runtime_dir: Path,
        *,
        max_workers: int = 4,
        task_timeout: float = 300.0,
    ):
        self.conn = conn
        self.runtime_dir = runtime_dir
        self.task_timeout = task_timeout
        self._queue = GroupQueue(max_workers=max_workers)
        self._worker_counter = 0

    @property
    def status(self) -> dict[str, Any]:
        return self._queue.status()

    def submit(self, task_id: str, task_payload: dict[str, Any]) -> bool:
        """Schedule task_id for execution.

        Returns True if started immediately, False if queued.
        """
        return self._queue.enqueue(task_id, lambda: self._run_task(task_id, task_payload))

    async def _run_task(self, task_id: str, task_payload: dict[str, Any]) -> None:
        self._worker_counter += 1
        worker_id = f"worker-{self._worker_counter}"

        claimed = await claim_task(self.conn, task_id, worker_id)
        if not claimed:
            logger.warning("WorkerPool: task {} already claimed by another worker, skipping", task_id)
            return

        logger.info("WorkerPool: starting subprocess for task {} (worker={})", task_id, worker_id)
        try:
            result = await asyncio.wait_for(
                self._spawn_subprocess(task_id, task_payload, worker_id),
                timeout=self.task_timeout,
            )
            await mark_done(self.conn, task_id, result, worker_id)
        except asyncio.TimeoutError:
            error = f"Task timed out after {self.task_timeout}s"
            logger.error("WorkerPool: {} timed out", task_id)
            await mark_failed(self.conn, task_id, error, worker_id)
        except Exception as exc:
            error = f"Worker error: {exc}"
            logger.exception("WorkerPool: error running task {}", task_id)
            await mark_failed(self.conn, task_id, error, worker_id)

    async def _spawn_subprocess(
        self,
        task_id: str,
        task_payload: dict[str, Any],
        worker_id: str,
    ) -> str:
        """Launch a Python subprocess running subagent_entry and return its output."""
        stdin_data = json.dumps(
            {
                "task_id": task_id,
                "worker_id": worker_id,
                "runtime_dir": str(self.runtime_dir),
                "payload": task_payload,
            },
            ensure_ascii=False,
        ).encode()

        python = sys.executable
        proc = await asyncio.create_subprocess_exec(
            python, "-m", _ENTRY_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )

        stdout, stderr = await proc.communicate(input=stdin_data)

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"Subprocess exited {proc.returncode}: {err_text[:_MAX_STDERR_OUTPUT]}")

        output = stdout.decode(errors="replace").strip()
        # Subprocess should emit a JSON line with {"result": "..."}
        try:
            data = json.loads(output)
            return data.get("result", output)
        except json.JSONDecodeError:
            return output or "(no output)"

    async def shutdown(self) -> None:
        """Wait for all running tasks to finish."""
        await self._queue.wait_all()
