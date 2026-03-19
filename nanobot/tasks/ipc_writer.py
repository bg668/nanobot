"""File IPC writer: atomically drop task/message files into the IPC directory.

Convention (aligned with nanoclaw):

    runtime/ipc/<namespace>/tasks/<task_id>.json
    runtime/ipc/<namespace>/results/<task_id>.json
    runtime/ipc/<namespace>/messages/<msg_id>.json

Writes are done via a tmp file + rename to guarantee atomic delivery even if
the writer crashes mid-write.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def _ipc_dir(runtime_dir: Path, namespace: str, kind: str) -> Path:
    d = runtime_dir / "ipc" / namespace / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* atomically via tmp+rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_task(
    runtime_dir: Path,
    payload: dict[str, Any],
    *,
    namespace: str = "main",
    task_id: str | None = None,
    label: str | None = None,
) -> str:
    """Write a task IPC file and return the task_id."""
    tid = task_id or str(uuid.uuid4())
    d = _ipc_dir(runtime_dir, namespace, "tasks")
    envelope = {
        "task_id": tid,
        "namespace": namespace,
        "label": label or tid,
        "payload": payload,
    }
    _atomic_write(d / f"{tid}.json", envelope)
    return tid


def write_result(
    runtime_dir: Path,
    task_id: str,
    result: str,
    *,
    namespace: str = "main",
    status: str = "done",
) -> None:
    """Write a result IPC file (subprocess → main)."""
    d = _ipc_dir(runtime_dir, namespace, "results")
    _atomic_write(
        d / f"{task_id}.json",
        {"task_id": task_id, "namespace": namespace, "status": status, "result": result},
    )


def write_message(
    runtime_dir: Path,
    content: dict[str, Any],
    *,
    namespace: str = "main",
    msg_id: str | None = None,
) -> str:
    """Write a generic message IPC file and return the message ID."""
    mid = msg_id or str(uuid.uuid4())
    d = _ipc_dir(runtime_dir, namespace, "messages")
    _atomic_write(d / f"{mid}.json", {"msg_id": mid, "namespace": namespace, **content})
    return mid
