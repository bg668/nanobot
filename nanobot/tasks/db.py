"""aiosqlite-based task persistence layer.

Schema
------
tasks
    id          TEXT PRIMARY KEY
    label       TEXT
    payload     TEXT   (JSON)
    status      TEXT   (pending|queued|running|done|failed)
    result      TEXT
    error       TEXT
    created_at  TEXT   (ISO-8601)
    updated_at  TEXT
    started_at  TEXT
    finished_at TEXT
    worker_id   TEXT
    attempts    INTEGER DEFAULT 0

run_log
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    task_id     TEXT REFERENCES tasks(id)
    worker_id   TEXT
    status      TEXT
    started_at  TEXT
    finished_at TEXT
    duration_s  REAL
    error       TEXT
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    label       TEXT,
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    worker_id   TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    worker_id   TEXT,
    status      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    duration_s  REAL,
    error       TEXT
);
"""

VALID_STATUSES = frozenset({"pending", "queued", "running", "done", "failed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(runtime_dir: Path) -> Path:
    db_dir = runtime_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "tasks.sqlite3"


async def init_db(runtime_dir: Path) -> aiosqlite.Connection:
    """Open (or create) the tasks database and apply schema migrations."""
    path = _db_path(runtime_dir)
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await conn.commit()
    logger.debug("Task DB ready at {}", path)
    return conn


async def create_task(
    conn: aiosqlite.Connection,
    *,
    payload: dict[str, Any],
    label: str | None = None,
    task_id: str | None = None,
) -> str:
    """Insert a new task and return its ID."""
    tid = task_id or str(uuid.uuid4())
    now = _now()
    await conn.execute(
        """
        INSERT INTO tasks (id, label, payload, status, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (tid, label or tid, json.dumps(payload, ensure_ascii=False), now, now),
    )
    await conn.commit()
    logger.debug("Created task {}: {}", tid, label or tid)
    return tid


async def get_task(conn: aiosqlite.Connection, task_id: str) -> dict[str, Any] | None:
    """Return task row as dict, or None if not found."""
    async with conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_tasks(
    conn: aiosqlite.Connection,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return tasks optionally filtered by status, ordered by creation time."""
    if status:
        async with conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at LIMIT ?",
            (status, limit),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def claim_task(
    conn: aiosqlite.Connection,
    task_id: str,
    worker_id: str,
) -> bool:
    """Atomically claim a pending/queued task for execution.

    Returns True only if the task was successfully claimed (rowcount == 1).
    """
    now = _now()
    async with conn.execute(
        """
        UPDATE tasks
        SET status='running', worker_id=?, started_at=?, updated_at=?, attempts=attempts+1
        WHERE id=? AND status IN ('pending','queued')
        """,
        (worker_id, now, now, task_id),
    ) as cur:
        changed = cur.rowcount
    await conn.commit()
    if changed:
        logger.debug("Worker {} claimed task {}", worker_id, task_id)
    return bool(changed)


async def mark_done(
    conn: aiosqlite.Connection,
    task_id: str,
    result: str,
    worker_id: str | None = None,
) -> None:
    """Mark a running task as done and log the run."""
    now = _now()
    row = await get_task(conn, task_id)
    started_at = row["started_at"] if row else None
    duration = _calc_duration(started_at, now)

    await conn.execute(
        """
        UPDATE tasks
        SET status='done', result=?, finished_at=?, updated_at=?, worker_id=COALESCE(?, worker_id)
        WHERE id=?
        """,
        (result, now, now, worker_id, task_id),
    )
    await _append_run_log(conn, task_id, worker_id, "done", started_at, now, duration, None)
    await conn.commit()
    logger.debug("Task {} done", task_id)


async def mark_failed(
    conn: aiosqlite.Connection,
    task_id: str,
    error: str,
    worker_id: str | None = None,
) -> None:
    """Mark a running task as failed and log the run."""
    now = _now()
    row = await get_task(conn, task_id)
    started_at = row["started_at"] if row else None
    duration = _calc_duration(started_at, now)

    await conn.execute(
        """
        UPDATE tasks
        SET status='failed', error=?, finished_at=?, updated_at=?, worker_id=COALESCE(?, worker_id)
        WHERE id=?
        """,
        (error, now, now, worker_id, task_id),
    )
    await _append_run_log(conn, task_id, worker_id, "failed", started_at, now, duration, error)
    await conn.commit()
    logger.debug("Task {} failed: {}", task_id, error[:80])


async def poll_task(
    conn: aiosqlite.Connection,
    task_id: str,
    *,
    timeout: float = 300.0,
    interval: float = 1.0,
) -> dict[str, Any] | None:
    """Poll until a task reaches a terminal state (done/failed) or timeout.

    Returns the final task dict, or None on timeout.
    """
    import asyncio

    elapsed = 0.0
    while elapsed < timeout:
        row = await get_task(conn, task_id)
        if row and row["status"] in ("done", "failed"):
            return row
        await asyncio.sleep(interval)
        elapsed += interval
    return None


async def _append_run_log(
    conn: aiosqlite.Connection,
    task_id: str,
    worker_id: str | None,
    status: str,
    started_at: str | None,
    finished_at: str,
    duration_s: float | None,
    error: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO run_log (task_id, worker_id, status, started_at, finished_at, duration_s, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, worker_id, status, started_at, finished_at, duration_s, error),
    )


def _calc_duration(started_at: str | None, finished_at: str) -> float | None:
    if not started_at:
        return None
    try:
        s = datetime.fromisoformat(started_at)
        f = datetime.fromisoformat(finished_at)
        return (f - s).total_seconds()
    except Exception:
        return None


def open_db_sync(runtime_dir: Path) -> sqlite3.Connection:
    """Open the task DB synchronously (for subprocess use)."""
    path = _db_path(runtime_dir)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
