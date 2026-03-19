# 架构可行性补充分析

本文档回答关于 secnano 基于 nanobot + subagent 重设计方案的三个补充可行性问题，并给出代码层面的引用依据。

---

## Q1：aiosqlite 处理异步时准确性可以保障吗？

### 结论：**可以保障，但需要正确配置连接模式**

#### aiosqlite 的工作原理

`aiosqlite` 是 Python 标准库 `sqlite3` 的异步封装，其内部使用 **专用后台线程（dedicated thread）** 运行所有 SQLite 操作，主协程通过 `asyncio.run_coroutine_threadsafe` 与该线程通信。

```
asyncio event loop ──await──▶ aiosqlite.Connection ──thread──▶ sqlite3 (C library)
```

这意味着：

- **ACID 保证完整继承**：所有原子性、一致性、隔离性、持久性由底层 SQLite C 库保证，aiosqlite 只是调度层，不破坏事务语义。
- **单连接天然串行**：pyclaw `db.py` 使用全局单连接 `_db`，所有协程对同一 `aiosqlite.Connection` 的操作在底层线程串行执行，**不存在并发写冲突**。

#### 参考实现

pyclaw 的 `refs/pyclaw/db.py` 已完整采用 aiosqlite：

```python
# refs/pyclaw/db.py（第 33–38 行）
_db: Optional[aiosqlite.Connection] = None

async def init_database() -> None:
    global _db
    _db = await aiosqlite.connect(_DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
```

所有读写操作均为 `await _get_db().execute(...)` + `await _get_db().commit()`，确保逻辑顺序与事务提交完全对应。

#### 多进程并发写的注意事项

当多个 subagent **进程**（`subprocess.Popen`，而非协程）同时访问同一个 SQLite 文件时，单连接串行化失效，需要额外措施：

| 场景 | 是否需要额外配置 | 建议 |
|---|---|---|
| 单进程内多协程 | ❌ 不需要 | aiosqlite 单连接天然串行 |
| 多进程读取 | ❌ 不需要 | SQLite 支持并发读 |
| 多进程并发写 | ✅ 需要 | 启用 WAL 模式（见下） |

**WAL 模式配置**（多进程写的标准解法）：

```python
async def init_database() -> None:
    _db = await aiosqlite.connect(_DB_PATH)
    await _db.execute("PRAGMA journal_mode=WAL")   # 启用 WAL
    await _db.execute("PRAGMA synchronous=NORMAL") # 平衡性能与安全
    await _db.commit()
```

WAL 模式下，SQLite 允许一个写者和多个读者同时运行，且写操作使用文件级锁而非页级锁，在 agent 任务场景（写操作不频繁）下实测性能无明显瓶颈。

#### 结论

> **aiosqlite 的准确性与标准 sqlite3 完全一致**，其底层 ACID 保证来自 SQLite C 库。对于 secnano 的单主进程使用场景，使用全局单连接即可满足准确性要求；若 subagent 以独立进程运行并直接写入 SQLite，加上 `PRAGMA journal_mode=WAL` 即可保证正确性。

---

## Q2：需求三改为 nanoclaw 风格的文件 IPC 是否可行？有什么差别？

### 结论：**完全可行，且已有 Python 完整参考实现**

#### nanoclaw 文件 IPC 的工作机制

nanoclaw 的文件 IPC 由 `refs/nanoclaw/src/ipc.ts` 和 `refs/nanoclaw/src/group-queue.ts` 共同实现，采用如下目录结构：

```
{DATA_DIR}/ipc/
  {group_folder}/
    messages/       # subagent → 主进程：发送消息给用户
      {ts}-{rand}.json
    tasks/          # subagent → 主进程：任务调度命令
      {ts}-{rand}.json
    input/          # 主进程 → subagent：追加消息或关闭信号
      {ts}-{rand}.json
      _close         # 关闭哨兵文件
```

**原子写入**（防止主进程读到半写文件）：

```typescript
// refs/nanoclaw/src/group-queue.ts（sendMessage 方法）
const tempPath = `${filepath}.tmp`;
fs.writeFileSync(tempPath, JSON.stringify({ type: 'message', text }));
fs.renameSync(tempPath, filepath);  // POSIX rename() 原子操作
```

**主进程轮询循环**（`refs/nanoclaw/src/ipc.ts`，`startIpcWatcher`）：

```typescript
const processIpcFiles = async () => {
    // 扫描所有 group IPC 目录
    // 处理 messages/*.json 和 tasks/*.json
    setTimeout(processIpcFiles, IPC_POLL_INTERVAL);  // 递归 setTimeout 轮询
};
processIpcFiles();
```

#### Python 完整实现已存在

pyclaw 的 `refs/pyclaw/ipc.py` 和 `refs/pyclaw/group_queue.py` 是文件 IPC 的完整 Python 翻译：

```python
# refs/pyclaw/ipc.py：轮询循环
async def _loop():
    while True:
        await _process()
        await asyncio.sleep(IPC_POLL_INTERVAL)   # 对应 nanoclaw 的 setTimeout

asyncio.create_task(_loop())
```

```python
# refs/pyclaw/group_queue.py：原子写入
tmp_path = input_dir / f"{filename}.tmp"
tmp_path.write_text(json.dumps({"type": "message", "text": text}))
tmp_path.rename(final_path)   # os.rename()，POSIX 原子
```

#### 与 Redis Streams 的差别对比

| 维度 | 文件 IPC（nanoclaw 风格）| Redis Streams |
|---|---|---|
| **外部依赖** | ❌ 无，零额外服务 | ✅ 需要部署 Redis |
| **可调试性** | ✅ 文件可直接查看/编辑 | 需要 redis-cli |
| **消息延迟** | 受 `IPC_POLL_INTERVAL` 影响（通常 0.5–1s）| 支持 `XREAD BLOCK` 毫秒级延迟 |
| **消息历史** | ❌ 处理后删除，无历史 | ✅ 持久化，支持 `XRANGE` 回放 |
| **消费者组** | 不支持 | ✅ `XREADGROUP` 原生支持 |
| **并发安全** | 依赖 rename() 原子性（POSIX 保证）| Redis 单线程模型天然安全 |
| **吞吐量** | 低（文件系统 IO）| 高（内存操作） |
| **适用场景** | agent 任务（低频，< 10 QPS）| 高吞吐消息队列 |
| **部署复杂度** | ✅ 极低 | 需要 Redis 运维 |
| **跨机器通信** | ❌ 需要共享文件系统 | ✅ 天然支持分布式 |

#### 推荐判断

对于 secnano 的单机部署场景（主 agent + 若干 subagent 均在同一台机器），**文件 IPC 完全够用**，且有以下优势：
- 无新增运维负担
- pyclaw 已有完整 Python 参考，代码迁移成本极低
- agent 任务天然低频（每次任务耗时数十秒），轮询延迟无感知

若未来需要跨机器部署或高并发消息路由，再切换 Redis Streams 是增量式改造，不影响架构骨架。

---

## Q3：需求 8（启动时检查 worker 槽位）在 nanoclaw 和 nanobot 中有相关实现吗？

### 结论：**nanoclaw/pyclaw 有完整实现；nanobot 当前无槽位限制**

#### nanoclaw 的实现：`GroupQueue.enqueueTask`

`refs/nanoclaw/src/group-queue.ts` 在 `enqueueTask` 和 `enqueueMessageCheck` 中实现了槽位检查：

```typescript
// refs/nanoclaw/src/group-queue.ts（enqueueTask 方法，第 87–100 行）
if (this.activeCount >= MAX_CONCURRENT_CONTAINERS) {
    // 无空位：入队等待
    state.pendingTasks.push({ id: taskId, groupJid, fn });
    if (!this.waitingGroups.includes(groupJid)) {
        this.waitingGroups.push(groupJid);
    }
    logger.debug({ groupJid, taskId, activeCount: this.activeCount },
        'At concurrency limit, task queued');
    return;
}
// 有空位：立即启动
this.runTask(groupJid, { id: taskId, groupJid, fn }).catch(...);
```

**关键计数器**：`activeCount` 在任务启动时 `++`，在 `finally` 块中 `--`，保证准确性。完成后调用 `drainGroup()` → `drainWaiting()` 消费等待队列：

```typescript
// refs/nanoclaw/src/group-queue.ts（drainWaiting，第 262–280 行）
private drainWaiting(): void {
    while (this.waitingGroups.length > 0 &&
           this.activeCount < MAX_CONCURRENT_CONTAINERS) {
        const nextJid = this.waitingGroups.shift()!;
        // 有空位才启动下一个
        const task = state.pendingTasks.shift();
        this.runTask(nextJid, task).catch(...);
    }
}
```

`MAX_CONCURRENT_CONTAINERS` 来自 `refs/nanoclaw/src/config.ts` 的环境变量配置：

```typescript
// refs/nanoclaw/src/config.ts
export const MAX_CONCURRENT_CONTAINERS = Math.max(
  1,
  parseInt(process.env.MAX_CONCURRENT_CONTAINERS || '5', 10) || 5,
);
```

#### pyclaw 的 Python 实现：`GroupQueue._active_count`

`refs/pyclaw/group_queue.py` 完整翻译了上述逻辑：

```python
# refs/pyclaw/group_queue.py（enqueue_task，第 65–80 行）
if self._active_count >= MAX_CONCURRENT_CONTAINERS:
    state.pending_tasks.append(_QueuedTask(task_id, group_jid, fn))
    if group_jid not in self._waiting_groups:
        self._waiting_groups.append(group_jid)
    return   # 无空位，入队

# 有空位，立即启动
asyncio.create_task(self._run_task(group_jid, _QueuedTask(task_id, group_jid, fn)))
```

`_run_task` 的 `finally` 块负责计数和排水：

```python
# refs/pyclaw/group_queue.py（_run_task，第 155–165 行）
finally:
    state.active = False
    self._active_count -= 1
    self._drain_group(group_jid)   # 释放槽位后立即消费等待队列
```

#### nanobot 当前实现：`SubagentManager`

nanobot 的 `nanobot/agent/subagent.py` 使用 `asyncio.create_task()` 运行 subagent，**当前无并发槽位限制**：

```python
# nanobot/agent/subagent.py（spawn 方法，第 63–66 行）
bg_task = asyncio.create_task(
    self._run_subagent(task_id, task, display_label, origin)
)
self._running_tasks[task_id] = bg_task  # 只做记录，无数量上限
```

`get_running_count()` 方法仅返回当前运行数量，但 `spawn()` 不检查此值是否超限。

#### 三者对比总结

| 项目 | 实现文件 | 槽位检查 | 等待队列 | 排水机制 |
|---|---|---|---|---|
| **nanoclaw (TS)** | `refs/nanoclaw/src/group-queue.ts` | ✅ `activeCount >= MAX_CONCURRENT_CONTAINERS` | ✅ `waitingGroups[]` | ✅ `drainWaiting()` |
| **pyclaw (Python)** | `refs/pyclaw/group_queue.py` | ✅ `_active_count >= MAX_CONCURRENT_CONTAINERS` | ✅ `_waiting_groups[]` | ✅ `_drain_waiting()` |
| **nanobot (Python)** | `nanobot/agent/subagent.py` | ❌ 无限制 | ❌ 无 | ❌ 无 |

#### 在 secnano 中实现需求 8 的方案

直接将 pyclaw 的 `GroupQueue` 迁移到 secnano，或在 nanobot `SubagentManager.spawn()` 中添加检查：

```python
# 方案 A：在 SubagentManager.spawn() 中添加槽位检查
async def spawn(self, task, label=None, ...):
    if len(self._running_tasks) >= self.max_workers:
        # 入文件 IPC 或内存队列等待
        self._pending_queue.append((task, label, ...))
        return f"Task queued (workers full, position {len(self._pending_queue)})"
    # 有空位，立即启动
    ...

# 方案 B：直接复用 pyclaw GroupQueue（推荐，逻辑完整）
queue = GroupQueue()  # MAX_CONCURRENT_CONTAINERS 来自环境变量
queue.enqueue_task(session_key, task_id, lambda: run_subagent(task))
```

方案 B 直接复用已验证的逻辑，与 nanoclaw 行为完全一致，推荐优先采用。
