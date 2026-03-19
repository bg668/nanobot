"""GroupQueue: concurrency-limited task queue aligned with nanoclaw's GroupQueue.

Rules
-----
* At most MAX_WORKERS tasks run concurrently.
* Excess tasks are held in an in-memory *waiting* deque.
* When a running task finishes, ``drain_waiting()`` pulls from the front of the
  waiting deque and launches the next task immediately.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Awaitable, Callable

from loguru import logger


class GroupQueue:
    """Concurrency-limited async task queue."""

    def __init__(self, max_workers: int = 4):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.max_workers = max_workers
        self._active: dict[str, asyncio.Task[Any]] = {}   # task_id -> asyncio.Task
        self._waiting: deque[tuple[str, Callable[[], Awaitable[Any]]]] = deque()

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    def enqueue(self, task_id: str, fn: Callable[[], Awaitable[Any]]) -> bool:
        """Schedule *fn* for execution.

        If a worker slot is free the coroutine is launched immediately and
        ``True`` is returned.  Otherwise it is queued and ``False`` is returned.
        """
        if self.active_count < self.max_workers:
            self._spawn(task_id, fn)
            return True
        else:
            logger.debug("GroupQueue: queuing {} (active={}/{})", task_id, self.active_count, self.max_workers)
            self._waiting.append((task_id, fn))
            return False

    def _spawn(self, task_id: str, fn: Callable[[], Awaitable[Any]]) -> asyncio.Task[Any]:
        logger.debug("GroupQueue: spawning {} (active={}/{})", task_id, self.active_count, self.max_workers)
        t = asyncio.create_task(fn(), name=f"gq-{task_id}")
        self._active[task_id] = t

        def _done_cb(fut: asyncio.Task[Any]) -> None:
            self._active.pop(task_id, None)
            self._drain_one()

        t.add_done_callback(_done_cb)
        return t

    def _drain_one(self) -> None:
        """If there is capacity and a waiting item, launch it."""
        if self._waiting and self.active_count < self.max_workers:
            next_id, next_fn = self._waiting.popleft()
            self._spawn(next_id, next_fn)

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task. Returns True if found and cancelled."""
        if task := self._active.get(task_id):
            task.cancel()
            return True
        # Also remove from waiting
        before = len(self._waiting)
        self._waiting = deque((tid, fn) for tid, fn in self._waiting if tid != task_id)
        return len(self._waiting) < before

    async def wait_all(self) -> None:
        """Wait until all active tasks finish (does not drain waiting queue)."""
        while self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)

    def status(self) -> dict[str, Any]:
        return {
            "max_workers": self.max_workers,
            "active": self.active_count,
            "waiting": self.waiting_count,
            "active_ids": list(self._active.keys()),
        }
