"""File IPC watcher: polls IPC directories and dispatches to DB / WorkerPool.

Directory layout watched::

    runtime/ipc/
        main/
            tasks/      ← task submission files (main namespace, trusted)
            results/    ← result files written by subprocesses
            messages/   ← optional generic messages
        <other>/
            tasks/      ← restricted namespace, limited operations

Authorization rules (aligned with nanoclaw namespace model):
- ``main`` namespace: full permissions (create tasks, read results)
- Any other namespace: tasks are accepted but *cannot* override task_id or
  escalate to admin operations.

Files are consumed (moved to ``errors/`` on failure, deleted on success) after
processing to avoid re-delivery.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from nanobot.tasks.db import create_task, get_task, mark_done
from nanobot.tasks.worker_pool import WorkerPool

_MAIN_NAMESPACE = "main"


class IPCWatcher:
    """Polls IPC directories and dispatches to the DB and WorkerPool."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        pool: WorkerPool,
        runtime_dir: Path,
        *,
        poll_interval: float = 1.0,
    ):
        self.conn = conn
        self.pool = pool
        self.runtime_dir = runtime_dir
        self.poll_interval = poll_interval
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Continuously poll IPC directories until ``stop()`` is called."""
        logger.info("IPC watcher started (interval={}s)", self.poll_interval)
        ipc_root = self.runtime_dir / "ipc"
        ipc_root.mkdir(parents=True, exist_ok=True)

        while not self._stop_event.is_set():
            try:
                await self._scan(ipc_root)
            except Exception:
                logger.exception("IPC watcher scan error")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=self.poll_interval,
                )
            except asyncio.TimeoutError:
                pass

        logger.info("IPC watcher stopped")

    async def _scan(self, ipc_root: Path) -> None:
        for ns_dir in sorted(ipc_root.iterdir()) if ipc_root.exists() else []:
            if not ns_dir.is_dir():
                continue
            namespace = ns_dir.name

            for kind_dir in sorted(ns_dir.iterdir()):
                if not kind_dir.is_dir():
                    continue
                kind = kind_dir.name

                for fpath in sorted(kind_dir.glob("*.json")):
                    await self._process_file(fpath, namespace, kind)

    async def _process_file(self, fpath: Path, namespace: str, kind: str) -> None:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("IPC: could not read {}: {}", fpath.name, exc)
            self._move_to_errors(fpath)
            return

        try:
            if kind == "tasks":
                await self._handle_task(data, namespace)
            elif kind == "results":
                await self._handle_result(data, namespace)
            elif kind == "messages":
                await self._handle_message(data, namespace)
            else:
                logger.debug("IPC: unknown kind '{}' for file {}", kind, fpath.name)

            fpath.unlink(missing_ok=True)
        except Exception as exc:
            logger.error("IPC: error processing {} ({}): {}", fpath.name, kind, exc)
            self._move_to_errors(fpath)

    async def _handle_task(self, data: dict[str, Any], namespace: str) -> None:
        """Create a task in the DB and submit to the worker pool."""
        raw_id = data.get("task_id")
        payload = data.get("payload", {})
        label = data.get("label")

        # Non-main namespaces may not prescribe task IDs.
        task_id_kwarg: dict[str, Any] = {}
        if namespace == _MAIN_NAMESPACE and raw_id:
            task_id_kwarg["task_id"] = raw_id

        # Idempotency: skip if task already exists.
        if raw_id and namespace == _MAIN_NAMESPACE:
            existing = await get_task(self.conn, raw_id)
            if existing:
                logger.debug("IPC: task {} already exists, skipping", raw_id)
                return

        task_id = await create_task(
            self.conn,
            payload=payload,
            label=label,
            **task_id_kwarg,
        )
        self.pool.submit(task_id, payload)
        logger.info("IPC: submitted task {} from namespace={}", task_id, namespace)

    async def _handle_result(self, data: dict[str, Any], namespace: str) -> None:
        """Update task result from a result IPC file (optional, DB is authoritative)."""
        task_id = data.get("task_id")
        result = data.get("result", "")
        status = data.get("status", "done")
        if not task_id:
            logger.warning("IPC: result file missing task_id, ignoring")
            return
        if status == "done":
            await mark_done(self.conn, task_id, result)
        logger.debug("IPC: processed result for task {}", task_id)

    async def _handle_message(self, data: dict[str, Any], namespace: str) -> None:
        """Log generic IPC messages (hook point for custom handlers)."""
        logger.info("IPC message from {}: {}", namespace, data)

    def _move_to_errors(self, fpath: Path) -> None:
        errors_dir = self.runtime_dir / "ipc" / "errors"
        errors_dir.mkdir(parents=True, exist_ok=True)
        dest = errors_dir / fpath.name
        try:
            shutil.move(str(fpath), dest)
        except Exception as exc:
            logger.warning("IPC: could not move {} to errors: {}", fpath.name, exc)
