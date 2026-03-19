"""Subagent entry point executed as a subprocess.

The parent process writes a JSON object to this process's stdin with the shape::

    {
        "task_id": "...",
        "worker_id": "...",
        "runtime_dir": "/path/to/runtime",
        "payload": { ... }  # arbitrary task payload; "task" key is the instruction
    }

The process:
1. Reads and parses the JSON input from stdin.
2. Executes the task (currently: simple echo of payload for MVP; replace with
   actual nanobot agent loop as needed).
3. Writes a single JSON line to stdout::

       {"result": "<outcome text>"}

4. Exits 0 on success, non-zero on unhandled failure.

The WorkerPool reads stdout/stderr and writes the result to the SQLite DB.
"""

from __future__ import annotations

import json
import sys


def _run(task_input: dict) -> str:
    """Execute the task and return a result string.

    Override this function to integrate the full nanobot agent loop.
    Currently it returns a formatted representation of the payload so the
    end-to-end pipeline can be tested without an LLM.
    """
    payload = task_input.get("payload", {})
    task_text = payload.get("task", json.dumps(payload, ensure_ascii=False))
    task_id = task_input.get("task_id", "?")
    return f"[task={task_id}] Completed: {task_text}"


def main() -> None:
    raw = sys.stdin.buffer.read()
    try:
        task_input = json.loads(raw)
    except Exception as exc:
        sys.stderr.write(f"Failed to parse task input: {exc}\n")
        sys.exit(1)

    try:
        result = _run(task_input)
        sys.stdout.write(json.dumps({"result": result}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"Task execution error: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
