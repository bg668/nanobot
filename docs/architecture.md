# nanobot 二次开发架构设计文档

> **版本**：v1.0  
> **日期**：2026-03-19  
> **状态**：已确认方案 — aiosqlite + 文件 IPC + nanoclaw 风格 WorkerPool

---

## 目录

1. [总体架构](#1-总体架构)
2. [10 条核心需求逐条应答](#2-10-条核心需求逐条应答)
3. [模块设计与关键接口](#3-模块设计与关键接口)
4. [开发计划](#4-开发计划)
5. [风险与约束](#5-风险与约束)

---

## 1. 总体架构

### 1.1 架构概览图

```
┌────────────────────────────────────────────────────────────────────┐
│                      主 Agent 进程                                   │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  AgentLoop (nanobot 原生事件循环)                              │  │
│  │   - 接收用户消息 (MessageBus.inbound)                         │  │
│  │   - 调用 LLM provider                                        │  │
│  │   - 执行 Tool（含 spawn_task / memory 工具）                  │  │
│  │   - 写结果到 MessageBus.outbound → Channel                   │  │
│  └──────────────┬───────────────────────────────────────────────┘  │
│                 │ spawn_task(task_json)                              │
│                 ▼                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  TaskManager（主进程内，asyncio 协程）                         │  │
│  │   - 写任务到 SQLite tasks 表（status=pending）                │  │
│  │   - 写 task.json 到 IPC 投递目录                              │  │
│  │   - 轮询 SQLite 结果（asyncio.sleep 循环）                    │  │
│  └──────────────┬───────────────────────────────────────────────┘  │
│                 │ SQLite(aiosqlite)                                  │
│                 ▼                                                    │
│  ┌─────────────────────────┐                                        │
│  │  tasks.db (aiosqlite)   │                                        │
│  │   tasks 表（状态机）    │                                        │
│  │   task_run_logs 表      │                                        │
│  └─────────────────────────┘                                        │
└────────────────────────────────────┬───────────────────────────────┘
                                     │ 文件 IPC
                                     ▼
┌────────────────────────────────────────────────────────────────────┐
│                  IPC 目录结构 (.nanobot/ipc/)                        │
│                                                                     │
│  .nanobot/ipc/                                                      │
│  ├── tasks/          ← 主 agent 写入任务 JSON 文件                   │
│  │   └── {task_id}.json                                             │
│  ├── results/        ← subagent 写入结果 JSON 文件                   │
│  │   └── {task_id}.json                                             │
│  └── processing/     ← WorkerPool 移动中间态文件（防重复领取）       │
│      └── {task_id}.json                                             │
└────────────────────────────────────┬───────────────────────────────┘
                                     │ 文件监视
                                     ▼
┌────────────────────────────────────────────────────────────────────┐
│                    WorkerPool 常驻进程                               │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  IpcWatcher（asyncio 轮询 ipc/tasks/ 目录）                   │  │
│  │   - 扫描 {task_id}.json 文件                                  │  │
│  │   - 原子 rename → ipc/processing/{task_id}.json              │  │
│  │   - 提交到 WorkerPool.submit(task)                            │  │
│  └──────────────┬───────────────────────────────────────────────┘  │
│                 │                                                    │
│  ┌──────────────▼───────────────────────────────────────────────┐  │
│  │  WorkerPool（参考 nanoclaw group-queue.ts）                    │  │
│  │   - active_count / max_workers                               │  │
│  │   - active_count < max_workers → 立即启动 subprocess         │  │
│  │   - active_count >= max_workers → 入 pending_queue           │  │
│  │   - 子进程结束后 → _drain() 消费等待队列                      │  │
│  └──────────────┬───────────────────────────────────────────────┘  │
│                 │ subprocess.Popen                                   │
│     ┌───────────┼──────────────┐                                    │
│     ▼           ▼              ▼                                    │
│  ┌──────┐  ┌──────┐       ┌──────┐                                 │
│  │Sub 1 │  │Sub 2 │  ...  │Sub N │  (N <= MAX_WORKERS)             │
│  │进程  │  │进程  │       │进程  │                                 │
│  └──┬───┘  └──────┘       └──────┘                                 │
└─────┼──────────────────────────────────────────────────────────────┘
      │ 写结果
      ▼
 ipc/results/{task_id}.json
      │
      ▼ (主 agent 轮询 SQLite)
 tasks.db tasks 表 status=done / failed
```

### 1.2 数据流说明

| 步骤 | 动作 | 机制 |
|------|------|------|
| ① | 用户触发任务，主 agent 调用 `spawn_task` 工具 | AgentLoop → ToolRegistry |
| ② | TaskManager 写任务到 SQLite，状态 `pending` | aiosqlite，BEGIN IMMEDIATE |
| ③ | TaskManager 写 `{task_id}.json` 到 `ipc/tasks/` | 原子写：先写 `.tmp` 后 rename |
| ④ | IpcWatcher 扫描 `ipc/tasks/`，原子 rename 到 `processing/` | asyncio.sleep 轮询 |
| ⑤ | WorkerPool 检查 `active_count < max_workers` | nanoclaw GroupQueue 逻辑 |
| ⑥ | 有空位 → `subprocess.Popen` 启动 subagent；无空位 → `pending_queue` | Python asyncio |
| ⑦ | Subagent 执行任务，更新 SQLite 状态为 `running` | aiosqlite WAL |
| ⑧ | Subagent 完成，写结果到 `ipc/results/{task_id}.json` | 原子写 |
| ⑨ | Subagent 更新 SQLite 状态为 `done/failed`，写 `result_json` | aiosqlite 事务 |
| ⑩ | 主 agent 轮询 SQLite，检测状态变化，返回结果给用户 | asyncio.sleep 循环 |

### 1.3 SQLite 任务状态机

```
           ┌─────────┐
  创建任务  │ pending │ ←──────────────────┐
           └────┬────┘                    │ retry（可选）
                │ WorkerPool 领取          │
                ▼                         │
           ┌─────────┐                    │
           │ running │ ──── 超时/错误 ────►│
           └────┬────┘                    │
                │                         │
         ┌──────┴──────┐                  │
         ▼             ▼                  │
      ┌──────┐    ┌────────┐              │
      │ done │    │ failed │──────────────┘
      └──────┘    └────────┘
```

字段说明：

```sql
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    role        TEXT NOT NULL,           -- 执行角色/类型
    prompt      TEXT NOT NULL,           -- 任务描述
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    result_json TEXT,                    -- JSON 结果
    error       TEXT,                    -- 错误信息
    worker_pid  INTEGER,                 -- 执行子进程 PID
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_run_logs (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id),
    started_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT NOT NULL,
    error       TEXT,
    worker_pid  INTEGER
);
```

### 1.4 记忆工具调用关系

```
┌─────────────┐       调用       ┌──────────────────────┐
│  主 Agent   │ ───────────────► │    MemoryTool        │
│ (AgentLoop) │                  │  store(key, value)   │
└─────────────┘                  │  retrieve(query)     │
                                 │  promote(task_id)    │
┌─────────────┐       调用       │                      │
│  SubAgent   │ ───────────────► │  [共享 SQLite 文件]   │
│  (子进程)   │                  │  memory.db           │
└─────────────┘                  └──────────────────────┘
```

主 agent 与 subagent 通过共享同一个 SQLite 文件（memory.db）访问记忆，无需额外通信信道。SQLite WAL 模式支持一写多读并发。

---

## 2. 10 条核心需求逐条应答

### 需求 1：subagent 必须使用子进程完成

**落地方式**：`subprocess.Popen` 启动独立 Python 进程。

```python
# subagent_runner.py
import subprocess, sys

proc = subprocess.Popen(
    [sys.executable, "-m", "nanobot.subagent_entry", "--task-id", task_id],
    cwd=workspace,
    env={**os.environ, "NANOBOT_TASK_ID": task_id},
)
```

- Subagent 是完全独立进程，与主 agent 内存隔离
- 崩溃不影响主进程
- `proc.pid` 写入 tasks 表 `worker_pid` 字段，便于监控和调试

### 需求 2：任务持久化存储，参考 nanoclaw 管理机制，SQLite 优先

**落地方式**：`aiosqlite` 异步封装，完整复刻 nanoclaw `db.ts` 的表结构和接口。

```python
# db.py 核心接口（对应 nanoclaw db.ts）
async def create_task(task_id, role, prompt, priority=0) -> Task
async def get_task_by_id(task_id) -> Task | None
async def update_task_status(task_id, status, **fields) -> None
async def claim_task(task_id, worker_pid) -> bool  # BEGIN IMMEDIATE 原子操作
async def complete_task(task_id, result_json) -> None
async def fail_task(task_id, error) -> None
async def log_task_run(task_id, started_at, status, error=None) -> None
async def get_pending_tasks(limit=10) -> list[Task]
```

**一致性保障（参考 pyclaw 实现）**：

```python
async with db.execute("BEGIN IMMEDIATE"):
    row = await db.execute_fetchone(
        "SELECT task_id FROM tasks WHERE status='pending' ORDER BY priority DESC, created_at LIMIT 1"
    )
    if row:
        await db.execute(
            "UPDATE tasks SET status='running', started_at=?, worker_pid=? WHERE task_id=? AND status='pending'",
            (time.time(), os.getpid(), row["task_id"])
        )
```

**SQLite 初始化配置（每次连接后执行）**：

```python
await db.execute("PRAGMA journal_mode=WAL")
await db.execute("PRAGMA synchronous=NORMAL")
await db.execute("PRAGMA busy_timeout=5000")
await db.execute("PRAGMA foreign_keys=ON")
```

### 需求 3：消息传递使用文件 IPC（同 nanoclaw ipc.ts）

**落地方式**：复刻 nanoclaw `ipc.ts` 的文件 IPC 机制，IPC 目录结构如下：

```
{workspace}/.nanobot/ipc/
├── tasks/          ← 主 agent 投递任务（task JSON 文件）
├── processing/     ← WorkerPool 领取中（原子 rename，防重复领取）
└── results/        ← subagent 回写结果
```

**原子写入**（防止读到写了一半的文件）：

```python
# ipc.py
import os, json, tempfile
from pathlib import Path

async def write_task_file(ipc_dir: Path, task_id: str, payload: dict) -> None:
    target = ipc_dir / "tasks" / f"{task_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.rename(target)  # 原子替换

async def write_result_file(ipc_dir: Path, task_id: str, result: dict) -> None:
    target = ipc_dir / "results" / f"{task_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False))
    tmp.rename(target)
```

**与 nanoclaw ipc.ts 的对比**：

| 特性 | nanoclaw ipc.ts | nanobot 文件 IPC |
|------|-----------------|-----------------|
| 传输介质 | 文件系统 JSON | 文件系统 JSON（相同） |
| 原子性 | rename 原子替换 | rename 原子替换（相同） |
| 目录结构 | `ipc/{group}/tasks/` | `ipc/tasks/` |
| 轮询机制 | setTimeout 循环 | asyncio.sleep 循环 |
| 部署依赖 | 无（零外部依赖） | 无（零外部依赖） |
| 跨机器扩展 | 需共享文件系统 | 需共享文件系统 |

### 需求 4：子进程管理使用 worker 常驻 + 消息队列，且设置最大 worker 数量

**落地方式**：WorkerPool 常驻进程，参考 nanoclaw `group-queue.ts` 实现。

```python
# worker_pool.py
class WorkerPool:
    def __init__(self, max_workers: int):
        self.max_workers = max_workers
        self.active_count = 0               # 当前活跃 worker 数
        self.pending_queue: deque[Task] = deque()  # 等待队列

    async def submit(self, task: Task) -> None:
        """提交任务：有空位立即启动，否则入等待队列"""
        if self.active_count < self.max_workers:
            await self._start_worker(task)
        else:
            self.pending_queue.append(task)
            logger.info("Task {} queued (active={}/{})", task.task_id, self.active_count, self.max_workers)

    async def _start_worker(self, task: Task) -> None:
        self.active_count += 1
        asyncio.create_task(self._run_and_drain(task))

    async def _run_and_drain(self, task: Task) -> None:
        try:
            await self._execute(task)  # subprocess.Popen + wait
        finally:
            self.active_count -= 1
            self._drain()              # 消费等待队列

    def _drain(self) -> None:
        """尝试从等待队列取出下一个任务"""
        while self.pending_queue and self.active_count < self.max_workers:
            next_task = self.pending_queue.popleft()
            asyncio.create_task(self._start_worker(next_task))
```

### 需求 5：任务表和状态机完全照抄 nanoclaw，SQLite 优先

已在 [需求 2](#需求-2任务持久化存储参考-nanoclaw-管理机制sqlite-优先) 和 [1.3 节](#13-sqlite-任务状态机) 中详细说明。

状态迁移函数（对应 nanoclaw `updateTaskAfterRun`）：

```python
async def update_task_after_run(
    db: aiosqlite.Connection,
    task_id: str,
    status: Literal["done", "failed"],
    result_json: str | None = None,
    error: str | None = None,
) -> None:
    now = time.time()
    await db.execute(
        """UPDATE tasks
           SET status=?, finished_at=?, result_json=?, error=?
           WHERE task_id=?""",
        (status, now, result_json, error, task_id),
    )
    await db.execute(
        """INSERT INTO task_run_logs(task_id, started_at, finished_at, status, error, worker_pid)
           SELECT task_id, started_at, ?, ?, ?, worker_pid FROM tasks WHERE task_id=?""",
        (now, status, error, task_id),
    )
    await db.commit()
```

### 需求 6：主 agent 通过轮询获取结果

**落地方式**：主 agent 提交任务后，异步轮询 SQLite `tasks` 表。

```python
# task_manager.py
async def wait_for_result(
    self,
    task_id: str,
    timeout: float = 300.0,
    poll_interval: float = 1.0,
) -> TaskResult:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = await self.db.get_task_by_id(task_id)
        if task and task.status in ("done", "failed"):
            return TaskResult(
                task_id=task_id,
                status=task.status,
                result=task.result_json,
                error=task.error,
            )
        await asyncio.sleep(poll_interval)
    # 超时处理
    await self.db.update_task_status(task_id, "failed", error="timeout")
    raise TaskTimeoutError(f"Task {task_id} timed out after {timeout}s")
```

**参考 nanoclaw `ipc.ts` 的 `startIpcWatcher`** 轮询模式：`setTimeout(processIpcFiles, IPC_POLL_INTERVAL)` → Python 对应 `asyncio.sleep(poll_interval)` 无限循环。

### 需求 7 & 10：记忆模块是工具，主 agent 和 subagent 都可调用

**落地方式**：`MemoryTool` 作为独立工具，注册到 nanobot `ToolRegistry`，主 agent 和 subagent 均可调用，共享同一个 `memory.db` SQLite 文件。

```python
# memory_tool.py
class MemoryTool(Tool):
    """统一记忆工具：store / retrieve / promote"""

    async def store(self, key: str, value: str, role: str | None = None) -> bool: ...
    async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryEntry]: ...
    async def promote(self, task_id: str, role: str) -> bool: ...
```

主 agent 通过 nanobot 原生 `ToolRegistry` 注册；subagent 进程通过 `import` 同一模块直接调用（共享 `memory.db` 文件，SQLite WAL 支持多进程并发读写）。

### 需求 8：启动时检查是否有空余，有则启动，无则入队

**落地方式**：直接对应 nanoclaw `group-queue.ts` 的槽位检查逻辑：

```typescript
// nanoclaw 原始逻辑（group-queue.ts）
if (this.activeCount >= MAX_CONCURRENT_CONTAINERS) {
    state.pendingTasks.push(task)
    this.waitingGroups.push(groupJid)
} else {
    this.runForGroup(groupJid, fn)
}
```

Python 等价实现（见需求 4 中 `WorkerPool.submit`）：

```python
if self.active_count < self.max_workers:
    await self._start_worker(task)   # 有空位 → 立即启动
else:
    self.pending_queue.append(task)  # 无空位 → 入队等待
```

`max_workers` 从环境变量 `NANOBOT_MAX_WORKERS`（默认值 4）读取：

```python
MAX_WORKERS = int(os.environ.get("NANOBOT_MAX_WORKERS", "4"))
```

### 需求 9：轮询机制参考 nanoclaw

**两层轮询设计**：

| 层次 | 目的 | 实现 |
|------|------|------|
| IpcWatcher | 扫描 IPC 目录，发现新任务文件 | `asyncio.sleep(IPC_POLL_INTERVAL)` 循环，默认 0.5s |
| TaskManager.wait_for_result | 主 agent 等待任务完成 | `asyncio.sleep(RESULT_POLL_INTERVAL)` 循环，默认 1s |

IpcWatcher 实现（参考 nanoclaw `startIpcWatcher`）：

```python
# ipc_watcher.py
class IpcWatcher:
    def __init__(self, ipc_dir: Path, worker_pool: WorkerPool, poll_interval: float = 0.5):
        self.ipc_dir = ipc_dir
        self.worker_pool = worker_pool
        self.poll_interval = poll_interval

    async def run(self) -> None:
        tasks_dir = self.ipc_dir / "tasks"
        processing_dir = self.ipc_dir / "processing"
        while True:
            for task_file in sorted(tasks_dir.glob("*.json")):
                processing_file = processing_dir / task_file.name
                try:
                    task_file.rename(processing_file)  # 原子领取
                except FileNotFoundError:
                    continue  # 其他 watcher 已领取
                task = self._parse_task(processing_file)
                await self.worker_pool.submit(task)
            await asyncio.sleep(self.poll_interval)
```

---

## 3. 模块设计与关键接口

### 3.1 模块总览

```
nanobot/
├── subagent/
│   ├── db.py              # aiosqlite 封装，tasks/task_run_logs 表 CRUD
│   ├── task_manager.py    # 主 agent 侧：创建任务、轮询结果
│   ├── ipc.py             # 文件 IPC 读写工具函数
│   ├── ipc_watcher.py     # IPC 目录监视（asyncio 轮询）
│   ├── worker_pool.py     # nanoclaw GroupQueue 风格 worker 槽位管理
│   ├── subagent_runner.py # subprocess.Popen 封装，启动 subagent 子进程
│   └── memory_tool.py     # 记忆工具，主 agent / subagent 共用
└── subagent_entry.py      # subagent 子进程入口（__main__ 模式）
```

### 3.2 `db.py` — aiosqlite 数据层

```python
class TaskDB:
    """异步 SQLite 封装，对应 nanoclaw db.ts"""

    def __init__(self, db_path: Path): ...

    async def init(self) -> None:
        """初始化连接、PRAGMA、建表"""

    async def create_task(
        self, task_id: str, role: str, prompt: str, priority: int = 0
    ) -> Task: ...

    async def get_task_by_id(self, task_id: str) -> Task | None: ...

    async def claim_task(self, task_id: str, worker_pid: int) -> bool:
        """原子领取任务（BEGIN IMMEDIATE），返回是否成功"""

    async def complete_task(self, task_id: str, result_json: str) -> None: ...

    async def fail_task(self, task_id: str, error: str) -> None: ...

    async def get_pending_tasks(self, limit: int = 10) -> list[Task]: ...

    async def log_task_run(self, task_id: str, status: str, error: str | None = None) -> None: ...
```

### 3.3 `task_manager.py` — 主 agent 任务管理

```python
class TaskManager:
    """主 agent 侧：创建任务、写 IPC 文件、轮询结果"""

    def __init__(self, db: TaskDB, ipc_dir: Path): ...

    async def submit(
        self, role: str, prompt: str, priority: int = 0
    ) -> str:
        """创建任务，写 SQLite + IPC 文件，返回 task_id"""

    async def wait_for_result(
        self, task_id: str, timeout: float = 300.0, poll_interval: float = 1.0
    ) -> TaskResult:
        """轮询 SQLite 直到任务完成或超时"""

    async def cancel(self, task_id: str) -> bool:
        """取消 pending 任务（running 任务无法取消）"""

    async def get_status(self, task_id: str) -> Task | None: ...
```

### 3.4 `ipc_watcher.py` — IPC 目录监视

```python
class IpcWatcher:
    """参考 nanoclaw startIpcWatcher，asyncio 轮询 ipc/tasks/ 目录"""

    def __init__(
        self,
        ipc_dir: Path,
        worker_pool: "WorkerPool",
        poll_interval: float = 0.5,
    ): ...

    async def run(self) -> None:
        """主循环：扫描 → 原子 rename → 提交 WorkerPool"""

    async def start(self) -> asyncio.Task:
        """启动后台协程"""
```

### 3.5 `worker_pool.py` — nanoclaw GroupQueue 风格

```python
class WorkerPool:
    """
    参考 nanoclaw group-queue.ts GroupQueue 实现。
    max_workers 控制最大并发子进程数。
    """

    def __init__(self, max_workers: int, db: TaskDB, ipc_dir: Path): ...

    async def submit(self, task: Task) -> None:
        """有空位 → 立即启动；无空位 → 入 pending_queue"""

    async def _start_worker(self, task: Task) -> None: ...

    async def _run_and_drain(self, task: Task) -> None:
        """执行任务，完成后释放槽位并 _drain()"""

    def _drain(self) -> None:
        """从 pending_queue 拉取任务填满空位"""

    @property
    def stats(self) -> dict:
        return {
            "active": self.active_count,
            "max": self.max_workers,
            "pending": len(self.pending_queue),
        }
```

### 3.6 `subagent_runner.py` — 子进程执行器

```python
class SubagentRunner:
    """subprocess.Popen 封装，启动 subagent 独立进程"""

    async def run(self, task: Task, workspace: Path) -> SubagentResult:
        """
        启动子进程：python -m nanobot.subagent_entry --task-id {task_id}
        监控 stdout/stderr，等待退出，返回结果
        """
        proc = subprocess.Popen(
            [sys.executable, "-m", "nanobot.subagent_entry", "--task-id", task.task_id],
            cwd=workspace,
            env={**os.environ, "NANOBOT_TASK_ID": task.task_id, "NANOBOT_DB_PATH": str(db_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # 异步等待（asyncio.get_event_loop().run_in_executor）
        stdout, stderr = await asyncio.get_event_loop().run_in_executor(None, proc.communicate)
        return SubagentResult(returncode=proc.returncode, stdout=stdout, stderr=stderr)
```

### 3.7 `memory_tool.py` — 记忆工具

```python
class MemoryTool(Tool):
    """
    统一记忆工具，主 agent 和 subagent 均可调用。
    底层共享 memory.db（aiosqlite，WAL 模式）。
    """

    @property
    def name(self) -> str:
        return "memory"

    async def store(self, key: str, value: str, role: str | None = None) -> bool:
        """写入记忆条目"""

    async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """检索相关记忆（全文搜索 or 向量检索）"""

    async def promote(self, task_id: str, role: str) -> bool:
        """将任务结果提升为长期记忆"""

    async def execute(self, action: str, **kwargs) -> str:
        """Tool 统一入口，action in {store, retrieve, promote}"""
```

---

## 4. 开发计划

### 阶段划分

```
MVP（Milestone 5）→ 稳定性（Milestone 6）→ 扩展（Milestone 7+）
```

### 4.1 MVP — Milestone 5：核心骨架

**目标**：最小可运行的子进程任务链路（不含记忆模块）。

**交付物**：

| 模块 | 内容 |
|------|------|
| `subagent/db.py` | aiosqlite 封装，tasks + task_run_logs 表，WAL 配置 |
| `subagent/ipc.py` | 文件 IPC 读写工具函数（原子写、rename） |
| `subagent/worker_pool.py` | nanoclaw GroupQueue 风格，MAX_WORKERS 控制 |
| `subagent/ipc_watcher.py` | asyncio 轮询 ipc/tasks/ 目录 |
| `subagent/subagent_runner.py` | subprocess.Popen 启动子进程 |
| `subagent/task_manager.py` | submit() + wait_for_result() 轮询接口 |
| `subagent_entry.py` | subagent 子进程入口（读 task_id，执行，写结果） |

**验收命令**：

```bash
# 单元测试
pytest tests/subagent/ -v

# 集成冒烟测试
NANOBOT_MAX_WORKERS=2 python -m nanobot.subagent_entry --smoke-test

# 验收检查点
# 1. 主进程提交 3 个任务，MAX_WORKERS=2，验证第 3 个任务进入等待队列
# 2. 一个任务完成后，等待队列中的任务自动启动
# 3. tasks 表状态机流转正确（pending → running → done）
# 4. 崩溃 subagent（kill -9 PID），主进程检测到并将任务标记 failed
```

### 4.2 稳定性 — Milestone 6：可靠性与主 agent 集成

**目标**：与 nanobot 主 agent 完整集成，记忆模块工具化。

**交付物**：

| 模块 | 内容 |
|------|------|
| `subagent/memory_tool.py` | MemoryTool 统一接口，注册到 ToolRegistry |
| `agent/tools/spawn_task.py` | SpawnTask 工具，主 agent 通过此工具提交任务 |
| `subagent_entry.py` | 注册 MemoryTool，subagent 内可调用记忆 |
| 崩溃恢复 | 启动时扫描 processing/ 目录，恢复中断任务 |
| 幂等保护 | `claim_task` 原子操作防重复执行 |
| 超时检测 | 后台协程定期检查 running 任务超时 |

**验收命令**：

```bash
# 记忆模块集成测试
pytest tests/subagent/test_memory_tool.py -v

# 崩溃恢复测试
python tests/subagent/test_crash_recovery.py

# 主 agent 集成测试
nanobot gateway --config tests/fixtures/test_config.yaml
# 发送消息：/spawn_task "请帮我写一个 hello world 脚本"
# 验证：任务状态流转，结果正确返回

# 验收检查点
# 1. 主 agent 调用 spawn_task 工具，任务进入 SQLite pending 状态
# 2. subagent 成功执行并写入记忆
# 3. 主 agent 通过 wait_for_result 轮询到 done 状态
# 4. processing/ 目录中的孤儿文件在重启后自动恢复
```

### 4.3 扩展 — Milestone 7+：生产就绪

**目标**：生产环境可用，可观测性、性能调优、多实例支持。

**交付物**：

| 特性 | 内容 |
|------|------|
| 可观测性 | `loguru` 结构化日志，任务状态变化全程记录 |
| 健康检查 | `/health` 端点报告 WorkerPool 状态（active/max/pending） |
| 优先级队列 | `tasks.priority` 字段支持高优任务插队 |
| 任务取消 | cancel API，通过 `proc.terminate()` 停止子进程 |
| 压测验证 | 并发提交 100 个任务，验证 SQLite WAL 无死锁 |
| Docker 支持 | `docker-compose.yml` 增加 worker 服务定义 |

**验收命令**：

```bash
# 压测
python tests/subagent/bench_concurrent.py --tasks 100 --max-workers 8

# 端到端验证（含记忆持久化）
pytest tests/subagent/test_e2e.py -v --timeout=120

# 验收检查点
# 1. 100 并发任务无数据丢失（tasks 表 done+failed = 100）
# 2. SQLite WAL 无 "database is locked" 错误
# 3. 重启后 processing/ 目录自动恢复所有中断任务
# 4. MemoryTool 在主 agent 和 subagent 中均成功读写
```

---

## 5. 风险与约束

### 5.1 SQLite 并发写竞争

| 项目 | 详情 |
|------|------|
| **风险** | 多个 subagent 同时写 tasks 表（更新状态、写结果），出现 `database is locked` 错误 |
| **根因** | SQLite 写者互斥，默认锁超时时间短（500ms） |
| **缓解措施** | ① WAL 模式（多读一写，性能提升 3–5×）；② `busy_timeout=5000`（5s 重试）；③ `claim_task` 用 `BEGIN IMMEDIATE` 避免死锁；④ 集中写操作（subagent 只更新自己的 task_id，不扫描全表） |
| **监控** | 记录 `OperationalError: database is locked` 日志，触发告警 |

### 5.2 文件 IPC 可靠性

| 项目 | 详情 |
|------|------|
| **风险** | 文件写到一半进程崩溃，导致 `ipc/tasks/` 中出现不完整 JSON 文件 |
| **缓解措施** | ① 原子写：先写 `.tmp` 后 `rename`（POSIX rename 是原子操作）；② 文件名格式 `{task_id}.json`，不完整文件使用 `.tmp` 后缀，IpcWatcher 只扫描 `.json` |
| **孤儿文件** | 处理中进程崩溃，文件残留在 `processing/`：启动时扫描 `processing/`，将对应任务标记为 `failed`（或 `pending` 重试） |

### 5.3 崩溃恢复与幂等

| 项目 | 详情 |
|------|------|
| **风险** | Subagent 进程被 `kill -9` 杀死，任务状态永远停留在 `running` |
| **缓解措施** | ① WorkerPool 监控子进程退出码（`proc.wait()`），异常退出时调用 `db.fail_task()`；② 启动时扫描 `tasks` 表中 `status=running` 且 `worker_pid` 不存在的记录，标记为 `failed`；③ 可选：`max_retries` 字段支持自动重试 |
| **幂等保护** | `claim_task` 使用 `BEGIN IMMEDIATE` + `WHERE status='pending'`，确保同一任务不会被两个 worker 同时领取 |

### 5.4 文件 IPC 的限制（与 Redis Stream 对比）

| 特性 | 文件 IPC（当前方案） | Redis Stream（备选） |
|------|-------------------|-------------------|
| 部署依赖 | 无（零依赖） | 需要 Redis 服务 |
| 跨机器扩展 | 需共享文件系统（NFS） | 原生支持 |
| 可观测性 | 手动实现日志 | `XPENDING`、消费者组 |
| 失败重投 | 手动实现（扫描 processing/） | `XCLAIM` 原生支持 |
| 延迟 | 受轮询间隔影响（~0.5s） | `XREAD BLOCK` 毫秒级 |

**决策**：当前阶段优先使用文件 IPC（零依赖，易调试），架构层面预留 `QueueBackend` 抽象接口，后续可平滑切换为 Redis Stream，不影响 WorkerPool 和 TaskManager 逻辑。

### 5.5 权限隔离

| 项目 | 详情 |
|------|------|
| **风险** | Subagent 子进程访问主 agent 不应公开的文件或环境变量 |
| **缓解措施** | ① 子进程通过 `cwd=workspace` 限制工作目录；② 环境变量白名单传递（不传递敏感 API Key，subagent 从独立配置文件读取）；③ nanobot 原有 `restrict_to_workspace` 机制在 subagent 侧同样生效 |

### 5.6 Python 版本兼容性

| 项目 | 详情 |
|------|------|
| **当前设置** | `pyproject.toml` 中 `requires-python = ">=3.11"` |
| **aiosqlite 兼容性** | `aiosqlite` 支持 Python 3.8+，无需修改版本要求 |
| **注意** | 使用 `asyncio.TaskGroup`（Python 3.11+）等新语法时需确认版本下界 |

---

## 附录：关键配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `NANOBOT_MAX_WORKERS` | `4` | 最大并发子进程数 |
| `NANOBOT_DB_PATH` | `{workspace}/.nanobot/tasks.db` | SQLite 任务数据库路径 |
| `NANOBOT_IPC_DIR` | `{workspace}/.nanobot/ipc` | 文件 IPC 根目录 |
| `NANOBOT_IPC_POLL_INTERVAL` | `0.5` | IpcWatcher 轮询间隔（秒） |
| `NANOBOT_RESULT_POLL_INTERVAL` | `1.0` | 主 agent 结果轮询间隔（秒） |
| `NANOBOT_TASK_TIMEOUT` | `300` | 任务超时时间（秒） |
| `NANOBOT_MEMORY_DB_PATH` | `{workspace}/.nanobot/memory.db` | 记忆模块 SQLite 路径 |

---

## 附录：参考实现来源

| 参考 | 路径 | 对应模块 |
|------|------|---------|
| nanoclaw group-queue.ts | `refs/nanoclaw/src/group-queue.ts` | `worker_pool.py` |
| nanoclaw ipc.ts | `refs/nanoclaw/src/ipc.ts` | `ipc.py`、`ipc_watcher.py` |
| nanoclaw db.ts | `refs/nanoclaw/src/db.ts` | `db.py` |
| pyclaw db.py | `refs/pyclaw/db.py` | `db.py`（aiosqlite 实现） |
| pyclaw task_scheduler.py | `refs/pyclaw/task_scheduler.py` | `task_manager.py` |
| nanobot subagent.py | `nanobot/agent/subagent.py` | `subagent_runner.py`（基础） |
| nanobot spawn.py | `nanobot/agent/tools/spawn.py` | `agent/tools/spawn_task.py` |
