# nanobot 架构与规划设计一致性评估（详细版）

> 评估时间：2026-03-18  
> 评估范围：`nanobot/` 主代码、`docs/` 现有文档、`README.md` 项目结构说明

## 1. 结论摘要

基于当前仓库实现，**nanobot 与给定的规划设计不完全相同**。总体上属于“已有可运行分层，但尚未显式制度化为你给出的官署模型”。

- **高度对应（有明确实现）**：
  - 中书省（主控编排）
  - 翰林院 / 史馆（会话历史 + 记忆整理）
  - 六部（工具/能力执行与注册）
  - 归档与状态（会话与记忆持久化）
  - 能力适配 / Provider / 配置 / 技能（外部能力隔离）
- **部分对应（有基础，但边界不够“正式模块化”）**：
  - 通政司（入口解析、校验与规范化）
  - 数据 Schema（存在多套类型契约，但非统一不可变契约层）
  - 角色与资产（资产加载存在，但校验与治理弱）
- **弱对应或缺失（需要新增显式层）**：
  - 门下省（统一回复审批/过滤/拒绝守卫层）

---

## 2. 评估方法

本次评估按“**职责边界是否清晰 + 是否有稳定契约 + 是否有独立层**”三项判断：

- **一致**：职责与规划高度一致，且有稳定边界
- **部分一致**：能力存在，但边界混合或缺少显式契约
- **不一致**：职责缺失或与规划明显不符

---

## 3. 核心正式模块逐项评估

### 3.1 通政司（入口解析、校验与规范化，仅产出 InboundEvent）

**评估：部分一致**

**现状映射**
- 入口接入与基础权限校验主要在 channel 层：
  - `nanobot/channels/base.py`
    - `_handle_message(...)` 负责接收参数、权限检查 `is_allowed(...)`、封装 `InboundMessage`、投递总线
- 统一入口事件类型定义在：
  - `nanobot/bus/events.py` (`InboundMessage` dataclass)

**与规划差异**
- 已做到“入口最终产出统一事件对象（InboundMessage）”。
- 但“解析/校验/规范化”并未抽成独立正式模块，仍散落在各 channel 与 `BaseChannel` 中。
- 类型名是 `InboundMessage`，非规划中的 `InboundEvent`（语义接近，但契约未统一命名）。

**建议**
- 增设独立 ingress normalizer（可命名通政司层），把 channel 特有输入统一规整为稳定事件契约，再送入总线。

---

### 3.2 中书省（主控策略、会话级路由、顶层推理流程）

**评估：一致（实现集中，但体量较大）**

**现状映射**
- 主控流程集中在：
  - `nanobot/agent/loop.py` (`AgentLoop`)
- 典型职责已覆盖：
  - 消费 inbound 消息、命令分流（`/stop`、`/restart`、`/new`、`/help`）
  - 会话读取与保存（`SessionManager`）
  - 构建 prompt/context（`ContextBuilder`）
  - 调用模型 + tool call 循环（`_run_agent_loop`）
  - 发布 outbound 响应（`MessageBus.publish_outbound`）

**与规划差异**
- 职责上高度吻合中书省。
- 但 `AgentLoop` 同时承担较多协调细节（命令控制、会话管理、记忆触发、进度消息路由），模块边界可以进一步细化。

---

### 3.3 翰林院 / 史馆（检索、记忆整理、历史上下文供给）

**评估：部分一致到一致（能力完整，显式门控不足）**

**现状映射**
- 记忆与归档：
  - `nanobot/agent/memory.py`
    - `MemoryStore`: `MEMORY.md` + `HISTORY.md`
    - `MemoryConsolidator`: consolidation 策略、分段与归档
- 会话历史：
  - `nanobot/session/manager.py`
    - JSONL 持久化、`last_consolidated` 偏移
- 历史注入上下文：
  - `nanobot/agent/context.py`
    - `build_messages(...)` 拼接 system + history + user

**与规划差异**
- “读取归档与角色记忆、供推理使用”已具备。
- “长期记忆写入必须经过显式规则门控”尚未形成单独规则引擎；当前更多依赖 consolidation 流程与工具调用约束，而非独立可配置门控层。

---

### 3.4 六部（backend、工具注册表、容器生命周期与工件处理）

**评估：部分一致（工具执行成熟；容器生命周期管理能力有限）**

**现状映射**
- 工具注册/执行：
  - `nanobot/agent/tools/registry.py` (`ToolRegistry`)
  - `nanobot/agent/loop.py` 中 `_register_default_tools()`
- 执行后端（host 执行）：
  - `nanobot/agent/tools/shell.py`（ExecTool）
  - `nanobot/agent/tools/spawn.py`（子代理任务）
- 能力扩展与插件：
  - `nanobot/skills/*`
  - `nanobot/channels/registry.py`（entry points 插件发现）

**与规划差异**
- 有“执行与能力注册层”雏形，且与主控有清晰调用边界。
- 但“容器生命周期与工件处理”并非显式一层统一管理；当前偏工具级能力，不是完整 execution runtime 编排平面。

---

### 3.5 门下省（回复校验与输出守卫；本身不生成内容）

**评估：不一致（缺少独立统一守卫层）**

**现状映射**
- 输出分发在：
  - `nanobot/channels/manager.py` `_dispatch_outbound(...)`
- 有少量控制：
  - progress/tool hint 开关过滤（`send_progress`、`send_tool_hints`）

**与规划差异**
- 当前没有“回复审批/策略过滤/拒绝”的独立输出守卫模块。
- 主回复内容基本从 `AgentLoop` 直接流向 outbound，再由 channel 发送。

**建议**
- 在 `AgentLoop -> publish_outbound` 之间引入 ReplyGuard（规则校验、敏感输出拦截、策略拒绝）。

---

## 4. 支撑正式模块逐项评估

### 4.1 数据 Schema（共享不可变契约层）

**评估：部分一致**

**现状映射**
- 事件对象：`nanobot/bus/events.py`（`InboundMessage`、`OutboundMessage`）
- 配置契约：`nanobot/config/schema.py`（Pydantic）
- Provider 返回契约：`nanobot/providers/base.py`（`LLMResponse`、`ToolCallRequest`）

**差异点**
- 存在多处“契约类型”，但不是统一单一 Schema 层。
- “不可变”未严格执行（例如 dataclass 非 frozen）。
- 规划中的 `ExecutionRequest/ExecutionResult/Reply` 作为统一核心类型尚未完整显式化。

---

### 4.2 角色与资产（SOUL/ROLE/MEMORY/POLICY 资产加载与校验）

**评估：部分一致**

**现状映射**
- 角色/提示资产模板：
  - `nanobot/templates/SOUL.md`
  - `nanobot/templates/USER.md`
  - `nanobot/templates/TOOLS.md`
  - `nanobot/templates/AGENTS.md`
- 启动时模板同步：
  - `nanobot/utils/helpers.py` `sync_workspace_templates(...)`
- 运行时加载：
  - `nanobot/agent/context.py` `_load_bootstrap_files()`

**差异点**
- 已有 SOUL/USER/TOOLS/AGENTS 资产加载机制。
- 但缺少严格 schema 校验与版本化治理，`ROLE/POLICY` 没有作为独立强约束资产体系出现。

---

### 4.3 归档与状态（任务记录、会话状态、执行输出）

**评估：一致（在当前体量下实现较完整）**

**现状映射**
- 会话状态：`nanobot/session/manager.py`（JSONL）
- 长期记忆与历史归档：`nanobot/agent/memory.py`（`MEMORY.md`、`HISTORY.md`）
- 会话裁剪与归档偏移：`last_consolidated` 机制

**差异点**
- 具备结构化可追溯能力（JSONL + markdown 归档），但偏文件化，不是独立归档服务。

---

### 4.4 能力适配 / Provider / 配置 / 技能（显式契约隔离外部集成）

**评估：一致**

**现状映射**
- Provider 抽象层：`nanobot/providers/base.py`（`LLMProvider`）
- Provider 注册与匹配：`nanobot/providers/registry.py` + `nanobot/config/schema.py`
- Channel 插件隔离：`nanobot/channels/registry.py`
- 工具与技能扩展：`nanobot/agent/tools/registry.py` + `nanobot/skills/*`

**结论**
- 外部能力适配已通过接口/注册表进行隔离，替换 provider 或 channel 的侵入性较低，符合规划意图。

---

## 5. 一致性总表

| 规划模块 | 当前对应实现 | 一致性结论 | 主要证据 |
|---|---|---|---|
| 通政司 | Channel + BaseChannel + Bus Event | 部分一致 | `nanobot/channels/base.py`, `nanobot/bus/events.py` |
| 中书省 | AgentLoop 主控编排 | 一致 | `nanobot/agent/loop.py` |
| 翰林院 / 史馆 | Session + Memory + Context | 部分一致~一致 | `nanobot/agent/memory.py`, `nanobot/session/manager.py`, `nanobot/agent/context.py` |
| 六部 | ToolRegistry + Exec/Spawn + Skills | 部分一致 | `nanobot/agent/tools/registry.py`, `nanobot/agent/loop.py`, `nanobot/skills/*` |
| 门下省 | Outbound dispatch（无独立守卫） | 不一致 | `nanobot/channels/manager.py` |
| 数据 Schema | events + config + provider types | 部分一致 | `nanobot/bus/events.py`, `nanobot/config/schema.py`, `nanobot/providers/base.py` |
| 角色与资产 | templates + context loader | 部分一致 | `nanobot/templates/*`, `nanobot/agent/context.py`, `nanobot/utils/helpers.py` |
| 归档与状态 | Session JSONL + MEMORY/HISTORY | 一致 | `nanobot/session/manager.py`, `nanobot/agent/memory.py` |
| 能力适配/Provider/配置/技能 | provider/channel/tool registry | 一致 | `nanobot/providers/*`, `nanobot/channels/registry.py`, `nanobot/agent/tools/registry.py` |

---

## 6. 与目标架构的关键差距（按优先级）

1. **缺少门下省（统一输出守卫）**：目前没有独立回复审批层。  
2. **通政司未显式独立**：入口标准化逻辑分散在 channel 与 base 层。  
3. **Schema 未统一为“不可变核心契约层”**：多套类型并存、缺少统一核心事件/执行/回复模型。  
4. **角色资产治理较弱**：存在模板加载，但缺少严格校验、版本化和策略资产（POLICY）治理。  
5. **六部缺少统一 runtime 管理平面**：工具执行成熟，但“容器生命周期/工件治理”能力仍偏分散。

---

## 7. 总体判断（回答“是否相同”）

**结论：当前 nanobot 架构与该规划设计“方向相近，但并不相同”。**

- 已有可运行的主控、记忆、执行、适配分层；
- 但尚未将入口治理（通政司）与输出守卫（门下省）正式独立；
- 统一不可变 Schema 契约和角色资产治理也未完全达到规划要求。

如果要“完全对齐”该规划，建议优先补齐：**门下省（输出守卫）** 与 **通政司（统一入口规范化）**，再推进统一 Schema 与资产治理。
