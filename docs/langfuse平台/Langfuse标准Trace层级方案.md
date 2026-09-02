# Langfuse 标准 Trace 层级方案

> 基于 Langfuse 官方文档（https://langfuse.com/docs/observability/data-model、best-practices）
> 创建：2026-08-25 ｜ 最后更新：2026-08-26（M-T3c 修复 3 + M-T3d + M-T5 任务级归属 + M-T6 主 agent skill span 嵌套）

---

## 一、Langfuse 数据模型

### 四层结构

```
Session（会话）── 1:N ── Trace（一次查询）── 1:N ── Observation（执行步骤，可嵌套成树）
                                                          └── Score（评分，挂在 trace 或 observation 上）
```

| 概念 | 定义 | 本项目对应 |
|---|---|---|
| **Session** | 将多个 trace 分组（典型：聊天线程中的多轮对话） | LangGraph `thread_id`（一个聊天窗口） |
| **Trace** | 一次完整的请求（从用户提问到返回回答） | 一次 `runs.create` / 一次用户查询 |
| **Observation** | trace 内的单个执行步骤，可嵌套成树 | LLM 调用（GENERATION）、工具调用（SPAN/TOOL）、agent 节点（AGENT） |
| **Score** | 挂在 trace 或 observation 上的评分 | 五维评分、用户反馈 |

### 与 LangGraph 概念映射

| LangGraph 概念 | Langfuse 对应 | 关系 |
|---|---|---|
| Thread | Session | 1:1 |
| Run（一次 graph 执行） | Trace | 1:1 |
| Node 执行 / LLM 调用 / Tool 调用 | Observation | 1:N（一个 run 产生多个 observation） |
| Sub-agent Run | Observation (type=AGENT) | 嵌套在父 trace 内 |

### Observation 嵌套规则

Observation 在 trace 内形成**树形结构**：

```
Trace
└── Observation (root: AGENT chat_agent)
    ├── Observation (GENERATION: llm-call)
    │   └── Observation (GENERATION: llm tokens)
    ├── Observation (AGENT: nl2sql_agent)       ← sub-agent 嵌套
    │   ├── Observation (GENERATION: llm-call)
    │   └── Observation (SPAN: skill:run_sql)   ← skill span 嵌套
    └── Observation (GENERATION: llm-call)
```

嵌套方式：

| 方式 | 适用场景 | 代码 |
|---|---|---|
| OTel context 自动继承 | 同一线程内 | `with client.start_as_current_observation()` |
| `trace_context` 手动指定 | 跨线程（deepagents 工作线程） | `client.start_observation(trace_context={"trace_id": ..., "parent_span_id": ...})` |
| 对象方法 | 显式父子关系 | `parent.start_observation(...)` |

### 命名规则

| 属性 | 放什么 | 不放什么 |
|---|---|---|
| `trace_name` | `"chat-turn"` / `"nl2sql-agent"`（低基数、稳定） | 用户问题文本、ID、时间戳 |
| `input` | 用户问题原文（root observation） | 原始 JSON blob |
| `output` | 最终回答 | 全部中间结果 |
| `metadata` | db_name、prompt_label、user_question | 敏感数据（PII） |
| `tags` | `["nl2sql"]`、功能标签 | 高基数动态值 |

> **官方原文**："Keep dynamic values out of names. A name should identify the operation, not a single execution of it."

---

## 二、最终层级关系（线上实测基线）

> 实测来源：2026-08-26 自托管 8.163.4.42 会话 `01a03c8f`，trace `ee55f397`（82 observations）。
> **一次用户查询（含异步子智能体 + auto-continue 续跑）= 单条 `chat-turn` trace。**

```
Session: {thread_id}                                   ← LangGraph thread，一个聊天窗口
│
├── Trace: "chat-turn"                                 ← 第 1 次查询（唯一 trace）
│   │   metadata: {user_question, db_name, workspace, skills, prompt, ...}
│   │
│   └── AGENT: chat_agent (aebfebd4)                   ← 原查询 root observation
│       ├── AGENT: {Memory/Skills/PatchToolCalls}Middleware.before_agent
│       ├── CHAIN: model ─ GENERATION: llm-call        ← 规划
│       ├── CHAIN: tools ─ ...                         ← 主 agent 工具（含发起异步子任务）
│       │
│       ├── AGENT: nl2sql_agent (a59607a6)             ← 异步子智能体（M-T3 注入归入本 trace）
│       │   ├── AGENT: ...Middleware.before_agent
│       │   ├── CHAIN: model / tools / HITL / TodoList... （完整执行子树）
│       │   ├── SPAN: skill:schema-linking:...         ← skill span 嵌子 agent 下（map 优先）
│       │   ├── SPAN: skill:sql-generation:...
│       │   └── SPAN: skill:sql-execution:...
│       │
│       ├── AGENT: chat_agent (7a8d8477)               ← auto-continue 续跑（M-T3d 归入本 trace）
│       │   ├── AGENT: ...Middleware.before_agent
│       │   ├── CHAIN: model / tools ...               ← 续跑链（汇总/最终回答）
│       │   └── ...
│       │
│       └── GENERATION: llm-call（最终回答）
│
└── Trace: "chat-turn"                                 ← 第 2 次查询（新 trace，正确）
    └── AGENT: chat_agent
        └── ...（同上结构）
```

对照修复前（扁平孤儿）：主/子 agent/skill 各开独立 trace，一次查询散成 5+ 条，子树缺失。根因：deepagents 异步工作线程丢失 OTel context（`get_current_span()` 返回 EMPTY），各 observation 创建源各自新开 trace。

> **连问场景注（M-T5）**：若问题 N 的异步子任务在问题 N+1 的 run 活跃期间完成，其 auto-continue 续跑链仍按 `task_id` 归属到问题 N 的 trace（任务级路由），不会误挂进问题 N+1 的 trace。

---

## 三、最终实现方案（总览）

四个环节协作达成「一次查询 = 单条 trace、层级完整」。过程演进见第四节存档。

### 3.1 环节总览

| # | 环节 | 机制 | 组件 |
|---|---|---|---|
| ① | 主 run 元数据注入 | ASGI 中间件拦截 `POST /threads/{tid}/runs`，注入 `langfuse_session_id=tid` / `langfuse_trace_name="chat-turn"` / tags + `user_question`/`db_name`/`workspace`/`skills`/`prompt` | `src/api/langfuse_metadata.py` |
| ② | 子 agent 归入父 trace | `_wrap_runs_create` 在 `orig_create` **前**快照 `langfuse_parent_trace_id` + `langfuse_parent_obs_id` 注入子 run metadata；子 agent root chain `on_chain_start` 前注入 `NonRecordingSpan` 父 context → 整棵子树加入父 trace | `deepagents_async_config_patch.py` + `langfuse_client.py` |
| ③ | Skill span 嵌套 | `trace_context={trace_id, parent_span_id}`；parent **map 优先**（`_ROOT_OBS_MAP` → 当前 agent root obs）、metadata 兜底 → 嵌到执行它的子 agent 下 | `langfuse_span.py` |
| ④ | auto-continue 复用原 trace | `_THREAD_TRACE_MAP[thread_id → (trace_id, root_obs)]`（仅新查询写）+ `_is_auto_continue` 识别（末条消息 id 以 `auto-continue` 开头或 content 以 `[系统` 开头）→ 注入同样的父 context，续跑归入原 trace | `langfuse_client.py` |

### 3.2 核心数据结构（进程级，均在 `langfuse_client.py`）

| 结构 | 键 → 值 | 写入者 | 读取者 |
|---|---|---|---|
| `_ROOT_OBS_MAP` | `trace_id → 最新 root_obs_id` | 每个 root chain 的 `on_chain_start` 后（含子/续跑，覆盖即预期） | ② `_wrap_runs_create` 快照；③ `_start_span` parent |
| `_THREAD_TRACE_MAP` | `thread_id → (trace_id, root_obs_id)` | 仅「新查询」root chain（无父、非续跑）；子/续跑不写以免覆盖 | ④ auto-continue 查原 trace |
| `handler.last_trace_id` | 单例属性 | CallbackHandler `on_chain_start` | `_current_otel_trace_id()` 兜底（OTel context 跨线程丢失时） |

### 3.3 一次查询的时序

```
用户提问 → POST /threads/{tid}/runs
 ① HTTP 中间件注入 config.metadata（session_id=tid、trace_name=chat-turn、user_question…）
 主 agent root chain on_chain_start（无父、非续跑）
    → 新开 trace T；_ROOT_OBS_MAP[T]=main_obs；_THREAD_TRACE_MAP[tid]=(T, main_obs)
 主 agent 工具发起异步子任务 → client.runs.create
 ② _wrap_runs_create 先快照（parent_trace_id=T、parent_obs_id=main_obs）→ 子 run metadata
 子 agent root chain on_chain_start（metadata 有父信息）
    → 注入 NonRecordingSpan(T, main_obs, SAMPLED) → 子 root obs 挂 T/main_obs 下
    → _ROOT_OBS_MAP[T]=sub_obs（覆盖，预期行为）
 ③ 子 agent 执行 skill → _start_span 取 parent_tid=T、parent=_ROOT_OBS_MAP[T]=sub_obs
    → skill span 嵌子 agent 下
 子任务完成 → 前端 auto-continue → POST /threads/{tid}/runs（[系统自动通知] 消息）
 ④ 续跑 root chain on_chain_start → _is_auto_continue 命中
    → _THREAD_TRACE_MAP[tid]=(T, main_obs) → 注入 NonRecordingSpan(T, main_obs)
    → 续跑链挂 T/main_obs 下，最终回答与原查询同 trace
```

### 3.4 关键不变量（违反即丢数据，均实测验证）

1. **`NonRecordingSpan.span_id` 必须是真实存在的父 root observation id**——占位值（如 1）使子树 `parent_span_id` 悬空。
2. **`SpanContext.trace_flags` 必须 `TraceFlags(0x01)`（SAMPLED）**——默认 0 → `ParentBased(AlwaysOn)` 拒采 → 整棵子树 `NonRecordingSpan`、从不导出（进程内日志有 obs id、API 恒 404，极具迷惑性）。构造参数对齐 SDK `Langfuse._create_remote_parent_span`（`trace_flags=0x01`、`is_remote=False`）。
3. **`runs.create` 的父级快照必须在 `orig_create` 之前**——同步执行时子 `on_chain_start` 会覆盖 `_ROOT_OBS_MAP` 与 `last_trace_id`。
4. **ID 口径**：`obs.id` = OTel span_id（16 hex）= API `observation_id`；`obs.trace_id` = OTel trace_id（32 hex）= API `traceId`。
5. **兜底行为**：auto-continue 在 `_THREAD_TRACE_MAP` 无记录（跨进程/重启后首查）→ 照常新开 trace；skill span map 未命中 → 兜底 metadata 父 obs，不孤儿。

### 3.5 已知边界

- **单进程假设**：三个 map（`_THREAD_TRACE_MAP` / `_ROOT_OBS_MAP` / `_TASK_TRACE_MAP`）都是进程级内存；多 worker 部署需改外部存储（当前单进程部署，不受影响）。
- **高并发**：`last_trace_id` 兜底在并发下可能串（竞态窗口极小）；生产高并发改 per-run 存储。
- **重启丢注册表**（M-T5）：`_TASK_TRACE_MAP` 进程重启丢失 → auto-continue 回退线程级路由，行为与 M-T3d 相同。
- **cap 驱逐**（M-T5）：注册表 cap=2000 FIFO 驱逐；任务派发到其续跑之间超过 2000 次其他派发（实际不可能）才会被驱逐。
- **v4 trace 显示名**：event 模型下 trace `name` 取首个到达事件名，异步 flush 顺序偶发显示为 `skill:...` 而非 `chat-turn`（纯展示，不影响嵌套/分组）。

---

## 四、分阶段实施记录（过程存档，最终基线见第三节）

### M-T1（✅ 2026-08-25）：命名规范化 + metadata 增强

1. ✅ `trace_name` 改为低基数 `"chat-turn"`（主 agent）/ `"nl2sql-agent"`（子 agent）
2. ✅ `metadata.user_question` 注入用户查询原文（前 200 字符）
3. ✅ `metadata.db_name` / `workspace` / `skills` / `prompt` 增强

改动文件：`src/api/langfuse_metadata.py`、`src/agent/middlewares/deepagents_async_config_patch.py`

### M-T2（✅ 2026-08-26）：Skill Span trace_id 传递

**问题**：deepagents 异步工作线程丢失 OTel context（已验证 `get_current_span()` 在 `_wrap_runs_create` 中为 EMPTY）。

**方案 B（手动传递）**：

1. ✅ `_current_otel_trace_id()`：优先读 OTel context，兜底读 `CallbackHandler.last_trace_id`（单例，`on_chain_start` 时写入，跨线程可读）
2. ✅ `_wrap_runs_create`：注入 `metadata.langfuse_parent_trace_id` 到子 agent run 的 config
3. ✅ `_parent_trace_id()`：从 `get_config().metadata` 读取父 trace_id
4. ✅ `_start_span`：传 `trace_context={"trace_id": parent_tid}` 给 `client.start_observation()`

效果：skill spans 与主 agent 共享 trace_id（不再是独立孤儿 trace）。

⚠ **并发注意**：`last_trace_id` 是单例属性，高并发下可能被其他请求覆盖（竞态窗口极小），若生产并发高需改为 per-run 存储。

改动文件：`src/agent/middlewares/deepagents_async_config_patch.py`、`src/agent/middlewares/langfuse_span.py`

### M-T3（✅ 2026-08-26）：Sub-agent Trace 嵌套

**问题**：子 agent 的 CallbackHandler 在异步工作线程创建独立 root trace，与主 agent trace 平级。

**方案**：monkey-patch `CallbackHandler.on_chain_start`——当 root chain 启动且 `metadata.langfuse_parent_trace_id` 存在时，激活带父 trace_id 的 OTel `NonRecordingSpan` 上下文。CallbackHandler 内部的 `start_observation` 检测到有效 OTel context，自动将子 agent 的 root observation 嵌套到主 trace 下。

- 主 agent 场景：metadata 无 `langfuse_parent_trace_id` → 不注入 context → 照常创建新 trace（既有行为不变）
- 子 agent 场景：metadata 有 `langfuse_parent_trace_id` → 注入 context → observations 嵌套到主 trace

> ⚠ 手工构造 `NonRecordingSpan` 有两个必踩坑：`span_id` 不能用占位值（须真实 obs id）、`trace_flags` 必须置 SAMPLED，见 **M-T3c**（子 agent root observation 丢失/整棵子树未导出的根因与修复）。

改动文件：`src/agent/trace/langfuse_client.py`（`_patch_handler_for_trace_nesting()`）

### M-T3b（✅ 2026-08-26，🔧 竞态修复 2026-08-26）：Skill Span parent_span_id 嵌套

**问题**：M-T2 让 skill spans 共享了 trace_id，但它们是 trace 内的 root observation（与 chat_agent 平级），而非 chat_agent 的子 observation。

**方案**：

1. ✅ `_ROOT_OBS_MAP`（全局 dict）：`trace_id → root_observation_id` 映射
2. ✅ `_patch_handler_for_trace_nesting`：`on_chain_start` 完成后，从 `handler._runs[run_id]` 读取 root observation 的 ID，存入 `_ROOT_OBS_MAP[trace_id]`
3. ✅ `get_root_observation_id(trace_id)`：查询函数
4. ✅ `_start_span`：`trace_context` 补上 `parent_span_id`，让 skill span 成为 chat_agent 的子 observation

**竞态根因（2026-08-26 定位修复）**：

M-T3 让主/子 agent 共享 trace_id → `_ROOT_OBS_MAP` 的 key 相同 → 子 agent 的 `on_chain_start` **覆盖**主 agent 的 entry。同时 `runs.create` 同步执行时，子 agent 的 `on_chain_start` 在 `orig_create` 内触发，覆盖 `handler.last_trace_id`。

```
时序分析：
1. 主 agent on_chain_start → _ROOT_OBS_MAP[trace] = main_obs_id  ✅
2. _wrap_runs_create 读 last_trace_id → main_trace_id  ✅
3. orig_create() → 子 agent on_chain_start 同步触发：
   a. last_trace_id = sub_trace_id（同 main，因为 M-T3 共享）
   b. _ROOT_OBS_MAP[trace] = sub_obs_id  ← 覆盖！❌
4. 子 agent 的 skill span 读 _ROOT_OBS_MAP[trace] → sub_obs_id（非 main_obs_id）
```

**修复**：

- `_wrap_runs_create` 在调用 `orig_create` **之前**捕获 `parent_trace_id` + `parent_obs_id`（从 `_ROOT_OBS_MAP` 读取，此时尚未被覆盖）
- 将 `parent_obs_id` 注入 `metadata.langfuse_parent_obs_id`
- `_start_span` 读 root observation ID 作为 `parent_span_id`（读取优先级见 **M-T3c**，最终为 map 优先）

效果：

```
修复前（M-T2 only）：
Trace: 78f3916d...
├── AGENT: chat_agent      ← root observation（无 parent）
│   └── ...children...
└── SPAN: skill:run_sql    ← root observation（无 parent，平级）

修复后（M-T2 + M-T3b + M-T3c）：
Trace: 78f3916d...
└── AGENT: chat_agent                ← root observation
    ├── ...main agent children...
    └── AGENT: nl2sql_agent          ← sub-agent root observation（M-T3c 修复）
        ├── ...sub-agent model/tools...
        └── SPAN: skill:run_sql      ← 子 observation（parent = sub-agent.id）
```

改动文件：`src/agent/trace/langfuse_client.py`（`_ROOT_OBS_MAP` + `get_root_observation_id()`）、`src/agent/middlewares/deepagents_async_config_patch.py`（`_current_root_obs_id` + 提前捕获）、`src/agent/middlewares/langfuse_span.py`（`_parent_obs_id` + `_start_span`）

### M-T3c（✅ 2026-08-26，修复 1/2；🔧 修复 3 2026-08-26）：子 agent root observation 丢失修复

**症状**：真实会话复现——一次查询产生 **两个** trace（主 trace + auto-continue trace）。主 trace 里 4 个 skill span 都正确嵌在 `chat_agent` 下，但 **子 agent 的 root observation 查 API 返回 404**（修复 1/2 后重启实测仍 404：`60d701691d2b495b`），子 agent 自己的 `model`/`tools`/middleware 子树整体缺失。

**根因（两层，修复 3 才定位到致命层）**：

1. ~~`span_id=1` 占位悬空~~（修复 1 处理）：`NonRecordingSpan` 用 `span_id=1`，子 agent root observation 的 `parent_span_id` 指向不存在的 span。
2. **`trace_flags=0` 未采样（修复 3，真根因）**：构造 `SpanContext` 未传 `trace_flags`，默认 `TraceFlags(0)` = 未采样。OTel 默认采样器 `ParentBased(AlwaysOn)` 对**未采样父** → 子 span 全部 `NonRecordingSpan` → `end()` 不触发 `on_end` → **整棵子树从未导出到 Langfuse 服务端**。进程内 `_runs` 记录正常（日志能看到 obs id，极具迷惑性），API 恒 404。修复 1 换成真实父 obs id 后重启实测仍 404，即此原因。
   - 旁证：skill span 走 SDK `trace_context` 路径幸存——SDK 内部 `Langfuse._create_remote_parent_span` 构造同类 `NonRecordingSpan` 时显式 `trace_flags=TraceFlags(0x01)` + `is_remote=False`，故始终被采样导出（即使 parent 悬空也保留）。
   - 已用项目 venv 的 OTel SDK 复现验证：`flags=0 → 子 span NonRecordingSpan`；`flags=1 → 子 span 真实 _Span`。

**修复**：

1. （修复 1）`NonRecordingSpan` 的 `span_id` 用 `metadata.langfuse_parent_obs_id`（`_wrap_runs_create` 已在 `orig_create` 前快照的主/父 agent root observation id，真实存在）。
2. （修复 2）`_start_span` 优先级反转：**先读 `_ROOT_OBS_MAP`**（此时是子 agent root observation）→ skill span 嵌到子 agent 下；**兜底读 metadata**（主 agent root obs）→ map 未命中仍嵌主 agent，不孤儿。
3. （修复 3）`SpanContext` 补 `trace_flags=TraceFlags(0x01)`（SAMPLED）+ `is_remote=False`，与 SDK `_create_remote_parent_span` 对齐 → 子树恢复采样导出。

> 注：M-T3b 曾让 skill span 优先读 metadata（主 agent obs）——那是因子 agent obs 是幻影（本根因）的 workaround。M-T3c 修复幻影后，map 里的子 agent obs 变真实，故反转为 map 优先，得到 `chat_agent → nl2sql_agent → skill` 标准层级。

改动文件：`src/agent/trace/langfuse_client.py`（`span_id` 用 `langfuse_parent_obs_id` + `trace_flags` 采样位）、`src/agent/middlewares/langfuse_span.py`（`_start_span` map 优先 + metadata 兜底）

### M-T3d（✅ 2026-08-26）：auto-continue 续跑复用原 trace（一次查询 = 单 trace）

**症状**：子智能体异步完成后，前端自动发起 auto-continue run（`POST /threads/{tid}/runs`，input 为系统通知消息）。该 run 是新的 root chain → CallbackHandler 新铸 trace_id → 同一查询产生**第二个** `chat-turn` trace（如 `a2a6e860...` / `49898c48...`），最终回答与原查询链路割裂。

**方案**（进程级，与 M-T3 同一注入机制）：

1. `_THREAD_TRACE_MAP: dict[thread_id, (trace_id, root_obs_id)]`：仅在「新查询」的 root chain（无父、非续跑）写入该 thread 当前查询的主 trace；子 agent / auto-continue 不写，避免覆盖原查询记录。
2. `_is_auto_continue(inputs)`：root chain 输入末条消息 `id` 以 `auto-continue` 开头，或 `content` 以 `[系统` 开头（`[系统自动通知]`/`[系统通知]`，与 `sync_subagent_todos._extract_user_query` 的排除约定一致）→ 判定为续跑。
3. `_patched_on_chain_start` 双场景检测：`parent_run_id is None` 时——A：metadata 带 `langfuse_parent_trace_id`（子 agent，M-T3）；B：`_is_auto_continue` 命中 → 用 `metadata.langfuse_session_id`（HTTP 中间件注入的 thread_id）查 `_THREAD_TRACE_MAP` 拿原 (trace, root_obs) → 注入同样的 `NonRecordingSpan`（含修复 3 的采样位）→ 续跑 observations 归入原 trace、挂在原查询 root observation 下。
4. 兜底：`_THREAD_TRACE_MAP` 未命中（跨进程/重启后首查不在此进程）→ 照常新开 trace（既有行为），打 INFO 日志。

**效果**：一次用户查询（含异步子智能体 + auto-continue 续跑）= **单条** `chat-turn` trace；trace 内层级 `chat_agent（原查询）→ … / chat_agent（续跑）→ 最终回答`。

**验证**（逻辑模拟 `D:/tmp/test_mt3d_sim.py`，16 项全过）：新查询记录映射；子 agent 加入原 trace 且不覆盖映射；auto-continue 加入原 trace 且父 = 原查询 root obs；block-list content 识别；同 thread 第二问覆盖映射；无记录兜底新开。

**线上终验（2026-08-26，自托管 8.163.4.42，会话 `01a03c8f`，M-T3c 修复 3 + M-T3d 部署后重启）**：单条查询 = **单条** `chat-turn` trace（`ee55f397`，82 observations）——root `chat_agent`（`aebfebd4`）；子 agent `nl2sql_agent`（`a59607a6`）`parent=aebfebd4` ✓（修复 3 生效，整棵 model/tools/middleware 子树不再 404）；4 个 skill span 全嵌子 agent 下（`parent=a59607a6`）✓；**auto-continue 续跑 `chat_agent`（`7a8d8477`）`parent=aebfebd4`，归入同一 trace，不再新开** ✓。同会话第二问（`2ddf00a9`）正确开独立新 trace 且子 agent/skill 同样嵌套正确。日志见 `[langfuse_m3] auto-continue root chain → 注入父 trace context`。

> 已知表象（非层级问题）：v4 event 模型下 trace 的 `name` 取**首个到达**的 observation 事件名，异步 flush 顺序偶发让第二条 trace 显示为 `skill:sql-execution:...` 而非 `chat-turn`（第一条正常）。不影响嵌套/分组，UI 上 session 仍按 `sessionId` 正确归组。

改动文件：`src/agent/trace/langfuse_client.py`（`_THREAD_TRACE_MAP` + `get_thread_trace_context()` + `_is_auto_continue()` + `_patched_on_chain_start` 双场景）

### M-T5（✅ 2026-08-26）：连问场景下的任务级 Trace 归属

**症状**：用户连续发问（间隔 ~20 秒，任务 N 还在执行时问题 N+1 的 run 已接管线程状态），问题 N 的异步子任务完成通知以 auto-continue run 形式投递到线程当前活跃的 run（问题 N+1）→ 续跑链虽层级正确（挂 root 下），但**归属错**：问题 4 任务的完成动作落在了问题 5 的 trace 树里。会话 `01a03c8f` 实测：第 5 问的 trace `d0c09b5a` 里 05:53:10 的续跑携带的是第 4 问的任务描述。

**根因**：M-T3d 的路由键是 `thread_id → 最新一次新查询的 trace`。串行提问时「最新」恰好正确；连问并发时「最新」指向了错误的那条 trace。**正确的键是 `task_id`**——续跑通知的消息 id 与正文里都带着它（`auto-continue-{完整uuid}-{ms}` / `check_async_task("完整uuid")` / `任务 X（`）。

**方案**（进程级，三层）：

1. **派发注册（`deepagents_async_config_patch.py::_wrap_runs_create`）**：`task_id = kwargs["thread_id"]`（deepagents `start_async_task` 用独立 thread_id 作 task_id）；在 `orig_create` 之前、`parent_trace_id`/`parent_obs_id` 快照之后，调用 `register_task_trace_context(task_id, main_thread_id, trace_id, root_obs_id, user_question, description)`。`description` 取自 `kwargs["input"]["messages"][0]["content"]`。`update_async_task` 重派发带 `multitask_strategy="interrupt"`，显式跳过，保护原绑定。
2. **任务级注册表（`langfuse_client.py`）**：`_TASK_TRACE_MAP: dict[task_id, (main_thread_id, trace_id, root_obs_id, question, description)]`，cap=2000 FIFO 驱逐最旧；幂等（已存在不覆盖）。`get_task_trace_context(task_id)` 支持 8 位短 id 前缀匹配。
3. **路由（`_patched_on_chain_start` auto-continue 分支）**：`_extract_task_id_from_auto_continue(inputs)` 三级解析（消息 id → 正文 `check_async_task` → 正文 `任务 X（`）→ 查注册表命中则路由回发起任务的 trace；未命中回退 M-T3d 的线程级路由（`_fallback_thread_route`，跨进程/重启/旧格式兜底）。

**描述耐久性（`sync_subagent_todos.py` + `check_progress.py`）**：watcher 在 `query_headers` 首次成功写入后，把描述 merge 进 `async_tasks[task_id].description`（读-改-写，复用 `_SYNC_WRITE_LOCK`，仅缺 description 时一次性触发）；终态写入时再兜底一次（初始 task 字典无 description 字段）。让跨进程/重启后前端仍显示真实描述而非任务 ID。

> **⚠ 描述耐久性修正（2026-08-26 线上验证发现）**：deepagents 的 `_tasks_reducer` 对 `async_tasks` 是**整条替换**（`merged.update(update)`），且 `check_async_task`/`update_async_task`/`list_async_tasks` 重建 `AsyncTask` 时**不含 description** → watcher 已 merge 的描述会被后续写入抹掉（线上终态 state 复现为空）。已修 `check_progress.py::apply_patch`：`_enhanced_build_check_command` 保留 description；新增 `_reapply_task_descriptions(out, state)` 包装 `_build_update_tool` / `_build_list_tasks_tool`，返回前从旧 state 把描述补回（15/15 模拟过，`D:/tmp/test_mt5c_desc_durability.py`）。注意 `_build_update_tool` 签名是 `(agent_map, clients)` 双参（`_build_list_tasks_tool` 是单参 `(clients)`），包装签名必须对齐。**包装函数里的 `runtime` 必须带 `Annotated[ToolRuntime, InjectedToolArg()]` 注解**：langgraph `tool_node._get_all_injected_args` 从 `tool.func/coroutine` 的类型注解识别注入参数，裸 `runtime` 无注解 → 不注入 → 线上 `TypeError: _aw() missing 1 required positional argument: 'runtime'`（会话 01a03d27 复现）。

**效果**：连问场景下，问题 N 的子任务完成续跑链归属**始终**指向问题 N 的 trace（即使问题 N+1 的 run 活跃）。串行场景行为不变（M-T3d 路由作为 fallback 保留）。

**验证**（逻辑模拟 `D:/tmp/test_mt5_task_route_sim.py`，28 项全过）：派发注册；**核心：Q2 覆盖线程级映射后 taskA 续跑仍路由回 Q1 的 trace**；三种消息格式解析；block-list content；未知 task 回退线程级；前缀匹配；幂等；重派发保护；cap FIFO 驱逐。**线上连问终验（会话 `01a03d08`，4 连问）**：4/4 续跑归属正确——Q1 的任务 07:56 才完成（晚于 Q2/Q3/Q4 派发），其续跑仍经 `auto-continue-task:01a03d08-850` 路由回 Q1 的 trace `55e30afb`，而非最新问的 trace。

> 已知边界（与 M-T3d 同口径）：进程级注册表，多 worker 部署需外部化（Redis/SQLite），当前单进程不受影响；重启后注册表丢失 → 回退线程级映射（行为与今天相同）。

改动文件：`src/agent/trace/langfuse_client.py`（`_TASK_TRACE_MAP` + `register/get_task_trace_context` + `_extract_task_id_from_auto_continue` + `_fallback_thread_route` + 路由分支重构）、`src/agent/middlewares/deepagents_async_config_patch.py`（`_wrap_runs_create` 派发注册）、`src/agent/subagents/sync_subagent_todos.py`（description merge 进 `async_tasks`）、`src/agent/subagents/check_progress.py`（描述耐久性：check/update/list 三条重建路径保留 description）

### M-T6（✅ 2026-08-26）：主 agent 直接调工具的 skill span 显式嵌套（孤儿 span 收口）

**症状**：会话 `01a03d42` 里出现 5 条独立 Trace——`skill:artifact-read:read_file` ×4 + `skill:artifact-write:write_file` ×1，全部 `agent: chat_agent`、无 parent、单 obs。读的是 `/memory/AGENTS.md`、`/large_tool_results/...`、`/shared/skills/main/{chart-saver,report-export}/SKILL.md`，写的是 `/workspace/report/Chinook多维度数据分析报告*.md`——全是**主 agent 自己直接调的文件工具**（DB 查询已委派子 agent，主 agent 能碰到的 skill 类工具就这些文件操作）。

**根因**：`_start_span` 只有两条嵌套路径——
- **A**：`metadata.langfuse_parent_trace_id` 存在 → 显式 `trace_context`。但该值**只对 deepagents 子 run 注入**（`_wrap_runs_create`），主 agent 线程永远没有。
- **B**：OTel 上下文活跃 → `start_observation` 自动继承。主 agent 文件调用时 OTel 上下文为空（与 M-T2 记录的异步线程 OTel 丢失同类）。

两条都断 → `start_observation` 裸开新 root trace（session 靠 `session.id` OTel 属性兜底挂上，所以出现在会话里）。**决定性对比**：同一个 `read_file` 读同一个 `/large_tool_results/call_a3b2fd…`——子 agent 读（08:51）→ 嵌套正确（`agent=nl2sql_agent`，走路径 A）；主 agent 读（08:55）→ 孤儿（`agent=chat_agent`）。规律：子 agent 的 skill span 全嵌套，主 agent 直接调的文件工具 span 全孤儿。

**方案（落点 A，改动最小）**：`_start_span` 在路径 A 失效后新增**路径 C**——主 agent 场景用进程内「会话线程 → 最近一次新查询的 `(trace_id, root_obs_id)`」映射显式构造 `trace_context` 嵌套，与子 agent 走同一机制：
1. 优先 `get_thread_trace_context(thread_id)`（`_THREAD_TRACE_MAP`，主/auto-continue run 的 `on_chain_start` 写入/保留，按会话线程 key，**无跨会话串号**；auto-continue 不覆盖它 → 归原查询 trace，天然正确）；
2. 兜底 `handler.last_trace_id` + `get_root_observation_id`（CallbackHandler 单例，跨会话竞态风险，仅线程 map 未命中时）；
3. 双空 → 保持原孤儿行为（裸 `start_observation` + session 属性）。

**验证**（`D:/tmp/test_mt6_pathc_nesting.py`，18/18 PASS）：路径 A 回归（注入 trace_context 不变）、路径 C 线程 map 命中/无 obs、map 未命中 → last_trace_id 兜底、map 抛异常兜底仍工作、双空保持孤儿 + session 属性、thread_id 空跳过路径 C、总开关关闭不埋点。M-T5 模拟回归 28/28 不受影响（未改 langfuse_client）。

> 后续跟进（不在本次范围）：若想更彻底，可在 `langfuse_client._patched_on_chain_start` 把当前 trace_id + root obs 写回主 run metadata（落点 B，主/子完全对称），让路径 A 原样生效、路径 C 退化为兜底。

### M-T6b（✅ 2026-08-26）：path-C span 的 v4 root 误判 → trace 名污染 + 会话页多 root 行

**症状**：M-T6 上线后会话 `01a03d7d` 的 trace 详情树正确（skill span 挂在 chat_agent root 下、无孤儿），但 **trace 名被污染**——TRACE-2/TRACE-3 显示 `skill:artifact-write:write_file` 而非 `chat-turn`；Session 页把它们列为额外 root 行。

**根因（v4 特有，三环链）**：
1. `start_observation(trace_context=...)` 内部恒置 `otel_span.set_attribute("langfuse.internal.as_root", True)`，并让 span 在 `_create_remote_parent_span`（NonRecordingSpan）下创建。
2. 该 span 的父 observation（chat_agent root）**早已先批 flush**——父不在同一 export batch → v4 在 ingest 时对每个事件独立算 `is_root_observation`：**parent 不在同一批 = root=True**（`parent_observation_id` 仍正确存储、读侧能解析，所以树是对的）。
3. v4 没有独立 trace 资源，**trace 名 = 最新一条 root observation 的 trace_name** → 被 path-C span 名覆盖。

同一机制下 path-A（子 agent）span 为什么是 root=False：它与子 agent root 在**同一棵/同一批**导出，父在同批 → 判非 root。**决定性对比**：path-A span `ed28b565…` parent=子 root 且 is_root=False；path-C span `af204dd2…` parent=主 root 且 is_root=True。

**修复（两个互补，`src/agent/middlewares/langfuse_span.py` `_start_span` 路径 C）**：
- **修复 B（嵌套，治本）**：先探 OTel 上下文——`get_current_span().is_recording()` 为真（主 agent 链路未断）→ **不传 trace_context**，让 span 自然挂到当前父 span 下（root=False、与父同批导出）；只有 OTel 已丢失时才显式 trace_context。
- **修复 A（trace 名）**：path-C span 创建后补 `span._otel_span.set_attribute("langfuse.trace.name", _trace_name())`——`_trace_name()` 读 config.metadata 的 `langfuse_trace_name`（主 agent 即 `"chat-turn"`，langfuse_metadata.py 注入）。`langfuse.trace.name` 是 trace 级传播属性键（`LangfuseOtelSpanAttributes.TRACE_NAME`，与已生效的 `session.id`/`langfuse.trace.tags` 同机制），v4 读取该 span 属性设为该事件 trace_name → 即使被判 root，trace 名也解析回 `chat-turn`。

**验证**（`D:/tmp/test_mt6_pathc_nesting.py`，26/26 PASS，原 18 + 新 8）：修复 B 的 OTel 活跃跳过 trace_context / OTel 丢失仍显式嵌套；修复 A 的 path-C 设 `langfuse.trace.name=chat-turn`、trace_name 空不设、路径 A 不设、thread_id 空不设。

> 已知边界：修复 B 依赖主 agent 工具调用时 OTel 上下文是否真的活跃（深 agents 异步派发的线程仍丢失，走 trace_context+修复 A 兜底）；两路都对，只是 root 判定不同。

### M-T6b 跟进（✅ 2026-08-26）：run 取消不再标 ERROR + path-A 补主 trace 名

2026-08-26 线上复查（会话 `01a03d94`）发现两条衍生问题并一并修复：

**① 取消 run 被误标 ERROR**（`langfuse_client.py`）
- 症状：前端点「停止」/ `runs.cancel` / 审批超时打断的 run，其正在执行的 `model` CHAIN 与 `chat_agent` AGENT 被标红 ERROR；`model` 的 status_message 实测坏成 `<object object at 0x...>`（异常对象序列化失败）。
- 根因：langfuse `CallbackHandler.on_chain_error` 只把 `CONTROL_FLOW_EXCEPTION_TYPES`（默认仅 `GraphBubbleUp`）判为控制流非错误；`asyncio.CancelledError` 不在列 → 取消被当成真错误。链路：`runs.cancel` → gRPC 控制流 `interrupt` 信号 → `langgraph_api/grpc/ops/runs.py` `done.set(UserInterrupt())` → `wait_if_not_done` 以 `CancelledError(UserInterrupt(...))` 取消 in-flight 图任务。
- 修复：`get_langfuse_handler()` 首次构造 handler 时把 `asyncio.CancelledError` 加入 `CONTROL_FLOW_EXCEPTION_TYPES` → 取消 → `level=DEFAULT`（不标红）+ status_message 可读（`"User interrupted the run"`）；真实错误（如 `ValueError`）仍标 ERROR。

**② path-A span 补主 trace 名**（`langfuse_span.py`，修复 A 扩展）
- 症状：path-A（子 agent skill span）若被 v4 误判 root，其 trace_name=None → trace 名回退成 `skill:*`。实测 path-A 目前恒 root=False（父=子 agent root 同批/后批导出，机制上不会触发 v4 的"父在先批"误判），但属防御性补齐。
- 修复：`_start_span` 增加 `_path_a` 标志；修复 A 块扩展为 `if _path_c or _path_a:`。**path-A 用 `_MAIN_TRACE_NAME="chat-turn"` 常量而非 `_trace_name()`**——子 agent 上下文里 `metadata.langfuse_trace_name` 是 `"nl2sql-agent"`（子 agent 自己的名），path-A span 嵌套在主 trace 下，若误判 root 会把主 trace 名污染成子 agent 名。path-C 仍用 `_trace_name()`（主 agent 上下文即 chat-turn）。
- 验证：`D:/tmp/test_mt6_pathc_nesting.py` 26 → **30/30 PASS**（新加：path-A 设 chat-turn 且不用 nl2sql-agent、无 root obs 仍设、path-C 用 `_trace_name()` vs path-A 用常量区分）；M-T3d/M-T5 回归全 PASS。

**③ 工具失败 skill span 的 status_message 显示真实错误**（`langfuse_span.py`，M-T6b-3）
- 症状：`_invoke`/`_invoke_async` 里工具抛异常时 `_end_span(..., status_message="tool call failed")` 硬编码 → skill span 只显示静态文案，真实错误只进了 score（error 维度），UI 上看不到具体报错。
- 修复：新增 `_err_message(e)`（`str(e)` 去空白 + 超 500 截断 + `__str__` 抛异常时回退类名），两处 `_end_span` 改为 `status_message=_err_message(e)`。
- 实测：真实异常经 `CallbackHandler.on_chain_error` → `level=ERROR` + `statusMessage="模型返回非法 JSON: expected object, got 'foo'"` 可从 API 读回（`D:/tmp/verify_error_msg_live.py`），UI 详情面板即显示该文本。

**④ skill span output 结构化（SQL 结果可折叠 JSON 树）**（`langfuse_span.py`，M-T6b-4）
- 症状：Session 页中间每个 skill span 一张卡，sql-execution 的 output 显示 `[{'type':'text','text':'<结果JSON>','id':'lc_...'}]`——DB 工具返回 AIMessage，`_result_text` 的 `str(content)` 是 Python repr 字符串（单引号），不是合法 JSON，UI「Formatted JSON」无法折叠成树，整段平铺占空间。
- 修复：新增 `_result_payload(result)` 仅用于 span output——取 content-block 第一个 `type=="text"` 块的 text，`json.loads` 成功且为 dict/list 则存结构化对象（UI 渲染可折叠树）；解析失败/非 JSON 退回原字符串。`_result_text` 保留给打分用（`looks_like_exec_error` 等需纯文本）不变。
- 大结果截断的 **vfs 占位指向具体文件**：占位文本与 `metadata.vfs_path` 从 `vfs://large_tool_results/`（目录）改为 `vfs://large_tool_results/<tool_call_id>`（`_tool_call_id` 读 `request.tool_call.id`，即 MessageSlimmer 落盘文件名 `<tool_call_id>`，经 `_sanitize_tool_call_id` 对齐 deepagents 的字符安全化）；无 tool_call_id 时回退目录。
- **截断阈值从 8000 对齐到 16000**（`_LARGE_RESULT_LIMIT`，与 MessageSlimmer `_DEFAULT_MAX_CHARS_BEFORE_TRUNCATE` 同值）——此前 8000~16000 之间的结果会显示占位符但 MessageSlimmer 从未落盘，占位指向的文件不存在；对齐后 ≤16000 直接进 output（结构化树），>16000 才截断且文件真实存在。`_SMALL_OUTPUT_LIMIT` 删除。
- 验证：`D:/tmp/test_result_payload.py` 15/15 PASS（content-block JSON→dict / 非 JSON→字符串 / 多 block 取第一个 text / 顶层标量退回 / `_result_text` 语义不变 / 大结果截断）；`_tool_call_id`/`_sanitize_tool_call_id`/截断路径/阈值对齐断言全过；M-T6b 回归 30/30 PASS。

### M-T4（远期）：完整 Agent Graph 支持

1. 利用 Langfuse Agent Graph 功能可视化 agent 间调用关系
2. 每个 observation 的 `type` 严格遵循官方分类
3. root observation 的 `input`/`output` 设为用户问题/最终回答（供 evaluator 和 dataset experiment 使用）

---

## 五、改动文件清单

| 文件 | 里程碑 | 改动说明 |
|---|---|---|
| `src/api/langfuse_metadata.py` | M-T1 | `trace_name` → `"chat-turn"`；新增 `user_question` metadata |
| `src/agent/middlewares/deepagents_async_config_patch.py` | M-T1/T2/T3b/M-T5 | `trace_name` → `"nl2sql-agent"`；`_current_otel_trace_id()` 兜底读 `last_trace_id`；`_wrap_runs_create` 在 `orig_create` **前**捕获 `parent_trace_id` + `parent_obs_id` 注入 metadata；M-T5 派发时登记 `task_id → (thread, trace, obs, question, description)` |
| `src/agent/middlewares/langfuse_span.py` | M-T2/T3b/T3c/M-T6/M-T6b | `_parent_trace_id()` / `_parent_obs_id()` 从 config.metadata 读；`_start_span` **先读 `_ROOT_OBS_MAP`**（子 agent root obs），兜底 metadata（主 agent root obs）；M-T6 路径 A 失效后新增**路径 C**（`get_thread_trace_context(thread_id)` 优先 + `handler.last_trace_id` 兜底）显式嵌套主 agent 文件工具 span，不再孤儿化；M-T6b 路径 C 先探 OTel 上下文（活跃→自然嵌套 root=False，丢失→trace_context）+ path-C span 补 `langfuse.trace.name`（读 metadata `langfuse_trace_name`）修复 v4 root 误判导致的 trace 名污染；M-T6b-2 修复 A 扩展至 path-A（`_path_a` 标志 + `_MAIN_TRACE_NAME="chat-turn"` 常量，防子 agent 名污染主 trace）；M-T6b-3 `_err_message(e)` 替代硬编码 `"tool call failed"`（真实错误进 skill span status_message，截断 500）；M-T6b-4 `_result_payload(result)` 仅用于 span output——解析 content-block 内层 JSON 为 dict/list（UI 可折叠树），`_result_text` 保留给打分；截断阈值 8000→16000 对齐 MessageSlimmer，vfs 占位指向具体文件 `large_tool_results/<tool_call_id>`（`_tool_call_id` + `_sanitize_tool_call_id`） |
| `src/agent/trace/langfuse_client.py` | M-T3/T3b/T3c/T3d/M-T5/M-T6b-1 | `_patch_handler_for_trace_nesting()` monkey-patch `on_chain_start`（双场景：子 agent 走 metadata、auto-continue 走 `_THREAD_TRACE_MAP`）；`NonRecordingSpan` 的 `span_id` 用父 root obs id + **`trace_flags=TraceFlags(0x01)` 采样位**（M-T3c 修复 1/3）；`_ROOT_OBS_MAP` + `get_root_observation_id()`；`_THREAD_TRACE_MAP` + `get_thread_trace_context()` + `_is_auto_continue()`（M-T3d）；M-T5 `_TASK_TRACE_MAP` + `register/get_task_trace_context` + `_extract_task_id_from_auto_continue` + `_fallback_thread_route`；M-T6b-1 `get_langfuse_handler()` 把 `asyncio.CancelledError` 加入 `CONTROL_FLOW_EXCEPTION_TYPES`（run 取消不再标 ERROR） |
| `src/agent/subagents/sync_subagent_todos.py` | M-T5 | watcher `query_headers` 首次写成功后把描述 merge 进 `async_tasks[task_id].description`（读-改-写 + `_SYNC_WRITE_LOCK`，一次性）；终态写兜底保留 description |

---

## 六、一次查询的完整 Trace 结构（实测样例）

实测会话 `01a03c8f`，用户提问「查询每张发票的 ID、发票日期、客户全名和发票总金额」（db_name=Chinook_Aliyun）：

```
Session: 01a03c8f-4456-7d23-8188-bad83b386f29          ← thread_id
│
└── Trace: ee55f39744ced4aaf6eec37a9ecdc1e2 "chat-turn"（82 observations）
    │   metadata: {user_question, db_name: "Chinook_Aliyun", workspace, skills, prompt, ...}
    │
    └── AGENT: chat_agent (aebfebd4ad171373)           ← 原查询 root observation
        ├── AGENT: Memory/Skills/PatchToolCalls Middleware.before_agent
        ├── CHAIN: model ─ GENERATION: llm-call        ← 规划
        ├── CHAIN: tools                                ← 发起异步子任务等
        │
        ├── AGENT: nl2sql_agent (a59607a682e6b3c5)     ← 异步子智能体归入本 trace
        │   ├── AGENT: Skills/PatchToolCalls Middleware.before_agent
        │   ├── CHAIN: model / tools / HumanInTheLoop / TodoList（完整子树）
        │   ├── SPAN: skill:schema-linking:wrenai_Chinook_Aliyun_list_models
        │   ├── SPAN: skill:sql-generation:wrenai_Chinook_Aliyun_dry_run
        │   └── SPAN: skill:sql-execution:wrenai_Chinook_Aliyun_run_sql   ← 均 parent=子 agent
        │
        └── AGENT: chat_agent (7a8d84777f43f724)       ← auto-continue 续跑归入本 trace
            ├── AGENT: ...Middleware.before_agent
            ├── CHAIN: model / tools                    ← 续跑链（汇总）
            └── ...                                     ← 最终回答
```

**Langfuse UI 体验**：
- **Session 页**：该会话所有查询按时间排列（每条 trace 用 `metadata.user_question` 区分）
- **Trace 详情页**：可折叠的 observation 树，一目了然看到规划→工具→子 agent→续跑→回答的完整流程
- **每个 observation**：点击可看 input/output/metadata/耗时

---

## 七、关键技术决策记录

### 为何不用 OTel context 自动传播？

**验证结果（2026-08-26）**：`get_current_span()` 在 deepagents 异步工作线程中返回 EMPTY。根因是 deepagents 用 `run_in_executor` 或类似机制将子 agent 创建/工具执行派发到独立线程，OTel contextvar 不跨线程传播。

→ M-T2 采用手动传递 `trace_id` 经 LangGraph config.metadata

### 为何用 `CallbackHandler.last_trace_id` 兜底？

直接在 `_wrap_runs_create`（主 agent 工具调用线程）读 OTel context 也为 EMPTY——该线程也是 deepagents 异步派发的。但 `CallbackHandler` 是进程内单例，`on_chain_start` 写入的 `last_trace_id` 跨线程可读（Python 对象属性，GIL 保护）。

⚠ 高并发场景下 `last_trace_id` 可能被并发请求覆盖。当前单用户/低并发场景足够，生产化需改为 per-run 存储（如 `run_id → trace_id` dict）。

### 为何 M-T3 用 `NonRecordingSpan` 而非直接传 `trace_context`？

CallbackHandler 的 `on_chain_start` 在创建 root observation 前检查 `trace.get_current_span().get_span_context()`——若有效（`_take_root_trace_context` 主动让位给活动 context），observations 自动嵌套。我们无法直接修改 CallbackHandler 的构造参数（它是单例），但可以在 `on_chain_start` 执行前临时 attach 一个带父 trace_id 的 OTel context（`NonRecordingSpan`），执行完 detach 恢复。

### 手工构造 `SpanContext` 的两个必踩坑（M-T3c 教训）

1. **`span_id` 必须是真实存在的 observation id**：占位值（如 1）会让子 observation 的 `parent_span_id` 悬空。
2. **`trace_flags` 必须置 `TraceFlags(0x01)`（SAMPLED）**：默认 `TraceFlags(0)` → `ParentBased(AlwaysOn)` 采样器拒采 → 子树全部 `NonRecordingSpan`，进程内记录正常但**从未导出**（API 404，日志有 obs id，极具迷惑性）。构造方式直接对齐 SDK 内部 `Langfuse._create_remote_parent_span`（`trace_flags=0x01`、`is_remote=False`）。

### `_ROOT_OBS_MAP` 的竞态与修复

**原始设计**：`_ROOT_OBS_MAP` 是进程级 dict（`trace_id → root_observation_id`），写入在 `on_chain_start`，读取在 `_start_span`。

**发现的竞态（2026-08-26）**：M-T3 让主/子 agent 共享 trace_id → `_ROOT_OBS_MAP` 的 key 相同。当 `runs.create` 同步执行子 agent 时，子 agent 的 `on_chain_start` 在 `orig_create` 内触发，覆盖主 agent 的 entry。同时 `handler.last_trace_id` 也被覆盖。

**修复方案**：`_wrap_runs_create` 在调用 `orig_create` **之前**快照 `parent_trace_id` + `parent_obs_id`，注入 `metadata.langfuse_parent_obs_id`。读取优先级随 M-T3c 修复 3 反转为 **map 优先**：`_start_span` 先读 `_ROOT_OBS_MAP`（子树正常导出后，其中为子 agent root obs，真实存在）→ skill span 嵌到子 agent 下；兜底读 metadata（主 agent root obs）→ map 未命中仍嵌主 agent，不孤儿。

### OTel trace_id 格式

**验证结果（2026-08-26）**：Langfuse SDK v4 的 trace_id 统一为 128-bit OTel 格式（32 hex chars）。`obs.trace_id`、`handler.last_trace_id`、API `traceId` 三者一致。`obs.id` = OTel span_id（16 hex）= API `observation_id`。
