# trace_recorder.py — TraceRecorderMiddleware 代码解读

> 源文件：`src/agent/middlewares/trace_recorder.py`
> 类别：**可观测性**
> 挂载点：`wrap_model_call` + `wrap_tool_call`（最外层）

## 1. 解决的问题

本地自包含追踪：事件写 EventStore（SQLite），生产无 LangSmith/Langfuse 仍可回溯。与 Langfuse 互补——Langfuse 是云端遥测，TraceRecorder 是本地黑匣子。

## 2. 核心机制

### 2.1 wrap_model_call

```
LLM_CALL_START（模型名 / 消息数）
    → handler(request) 执行 LLM 调用
    → LLM_CALL_END（耗时 / usage）
异常 → ERROR 事件后上抛
```

### 2.2 wrap_tool_call

```
TOOL_CALL_START（工具名 / 参数摘要 ≤200 字符）
    → handler(request) 执行工具
    → TOOL_CALL_END（结果摘要）
```

**特殊检测**：`start_async_task` 工具——从返回的 `Command.update.async_tasks` 提取子 thread_id：

- 记 `SUBAGENT_SPAWN` 事件
- `upsert_lineage` 建立父子线程谱系

### 2.3 事件类型

| 事件 | 触发 | 记录内容 |
|---|---|---|
| `LLM_CALL_START` | 模型调用前 | 模型名、消息数、thread_id |
| `LLM_CALL_END` | 模型调用后 | 耗时(ms)、input/output tokens |
| `TOOL_CALL_START` | 工具调用前 | 工具名、参数摘要 |
| `TOOL_CALL_END` | 工具调用后 | 结果摘要、耗时 |
| `ERROR` | 任何异常 | 异常类型、消息 |
| `SUBAGENT_SPAWN` | 子 agent 创建 | 父 thread_id、子 thread_id |

### 2.4 thread_id 策略

- LLM 调用时缓存 `_cached_thread_id`（从 `configurable` 读取）
- 工具调用复用它（`wrap_tool_call` 拿不到 model request 的 configurable）
- `parent_thread_id` 读 `configurable.trace_parent_thread_id`（deepagents patch 注入）

### 2.5 参数摘要

工具调用参数超 200 字符时截断：

```python
summary = json.dumps(args, ensure_ascii=False)
if len(summary) > 200:
    summary = summary[:197] + "..."
```

## 3. 关键设计决策

### 3.1 不写 state.token_stats

避免与 TokenMeter 双写导致 reducer 累加重复（实测翻倍）。职责分明：

- TokenMeter → 写 state（前端展示）
- TraceRecorder → 写 SQLite（本地追踪）

### 3.2 为什么挂载在最外层

- 最外层 = 最先启动、最后结束
- 能记录所有内层中间件的耗时（包括 Langfuse span 的开销）
- 异常不会被内层中间件吞掉

### 3.3 为什么用 SQLite 而非日志文件

- SQLite 支持查询（按 thread_id 过滤、按时间范围）
- 结构化数据比文本日志更易分析
- 与 EventStore 共用同一个 SQLite 文件

## 4. 与 Langfuse 的关系

| 维度 | TraceRecorder | Langfuse |
|---|---|---|
| 存储 | 本地 SQLite | 云端 |
| 粒度 | 每次 LLM/tool 调用 | skill 级 span |
| 评分 | 无 | 五维评分 |
| 用途 | 本地调试、离线回溯 | 生产监控、评估 |

两者并行运行，互不依赖。

## 5. 错误处理

- EventStore 写入失败 → 记 warning，不影响主流程
- configurable 读取失败 → thread_id 记为 "unknown"
- handler 抛异常 → 记 ERROR 事件后原样上抛
