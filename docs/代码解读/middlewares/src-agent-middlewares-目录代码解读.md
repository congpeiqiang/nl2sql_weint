# src/agent/middlewares 目录代码解读

> 目录路径：`src/agent/middlewares/`
> 解读日期：2026-10-14（全面重写）
> 共 **27 个** Python 文件（含 `__init__.py`）。本目录是 NL2SQL 系统的 **Agent 中间件层**：基于 LangChain 1.x `AgentMiddleware` 的 hook（`before_model` / `after_model` / `wrap_model_call` / `wrap_tool_call` / `after_agent`）与 Monkey-Patch，实现**配置注入、工具面控制、安全护栏、执行监督、上下文管理、上下文修复、错误翻译、可观测性**八大类能力。

## 总览：文件分类

| 类别 | 文件 | 挂载点 | 一句话职责 |
|---|---|---|---|
| **配置注入** | current_db_context.py | wrap_model_call | 当前库名注入最新用户消息，防切库后模型按旧库委派 |
| 配置注入 | query_keywords.py | wrap_model_call | 前端关键词动态替换系统提示词触发词 |
| 配置注入 | thinking_toggle.py | wrap_model_call | 思考开关 + 模型路由 + auto-continue 继承（带 mtime 缓存） |
| 配置注入 | model_required.py | wrap_model_call | 账号无模型时 fail-closed 返回友好 AIMessage |
| **工具面控制** | tool_filter.py | wrap_model_call | 按当前库过滤 wrenai/dbmcp 工具，语义层独占 |
| 工具面控制 | dynamic_mcp_tools.py | wrap_model_call | MCP 工具运行期热注册，增删库免重启 |
| **安全护栏** | execute_guard.py | wrap_tool_call | shell 命令护栏：禁破坏性命令与工作区外路径 |
| 安全护栏 | sql_approval.py | wrap_tool_call | SQL 只读硬拦截 + Wren LIMIT 归一 + Cube 窗口参数 |
| 安全护栏 | filesystem_thread_guard.py | wrap_tool_call | 线程级文件隔离：读/写/grep 三维度 + 路径归一化 |
| **执行监督** | query_gate.py | wrap_tool_call | 查询通道硬闸 + 顺序软提醒 |
| 执行监督 | cube_route_gate.py | wrap_tool_call | Cube 路由软门：手写聚合 SQL 重定向到 query_cube |
| 执行监督 | progress_boundary.py | after_model | 确定性推进 write_todos 进度（防任务卡冻结） |
| 执行监督 | write_todos.py | wrap_model_call | 追加 write_todos 分级使用协议到 system prompt |
| 执行监督 | caliber_gate.py | after_model | 口径治理：终稿无/不合规时 jump_to 打回重写 |
| 执行监督 | failure_circuit.py | wrap_tool_call | 同类失败熔断：(错误族,对象名) 签名计数 + 硬闸 |
| **上下文管理** | message_slimmer.py | wrap_tool_call | 超大 tool 结果截断落盘 + 完全重复去重 |
| 上下文管理 | query_result_offload.py | wrap_tool_call | 大查询结果表代码代写落盘 + 消息瘦身 |
| 上下文管理 | knowledge_trim.py | wrap_tool_call | get_instructions 结果按问题相关性裁剪 |
| 上下文管理 | vfs_path_resolver.py | wrap_model_call 后处理 | AIMessage 里 VFS 路径改写为真实磁盘路径 |
| 上下文管理 | chart_artifact_owner.py | wrap_tool_call | 图表 HTML 自动代写落盘 + 路径注入返回值 |
| **上下文修复** | dangling_tool_calls.py | before_model | 为孤儿 tool_call 补合成 ToolMessage，防 400 |
| **错误翻译** | model_timeout.py | wrap_model_call | LLM 超时 → 友好中文 AIMessage |
| 错误翻译 | quota_error.py | wrap_model_call | 额度耗尽 → 友好中文 AIMessage |
| **可观测性** | langfuse_span.py | wrap_tool_call + after_agent | skill 级 span、五维评分、中间产物落盘、评估单元 |
| 可观测性 | token_meter.py | wrap_model_call | 每次 LLM 调用 token/耗时采集，累积到 state |
| 可观测性 | trace_recorder.py | wrap_model/tool_call | 本地 SQLite 事件追踪 + 子 agent 谱系 |
| **Monkey-Patch** | deepagents_async_config_patch.py | 模块导入时 | 透传父 configurable 与 Langfuse 上下文到异步子 agent |
| （包声明） | __init__.py | - | 空模板文件 |

## 中间件链顺序约束

核心顺序规则：

```
请求流（first → last → tool）：
  MessageSlimmer → QueryResultOffload → FailureCircuit → LangfuseSpan → 工具

响应流（tool → last → first）：
  工具 → LangfuseSpan → FailureCircuit → QueryResultOffload → MessageSlimmer
```

具体约束：

1. **QueryResultOffload / MessageSlimmer** 必须注册在 **LangfuseSpan 之前（外层）**——保证 span 吃到原始全量 payload，只有进 state 的消息被瘦身
2. **FailureCircuit** 紧挨 LangfuseSpan **之前（外层）**——span 吃原始报错，改写后的文本进 Slimmer 做体积判据
3. **QueryGate** 位于 SqlReadOnly **之前**——被拦的 run_sql 仍在 span 里可见，写/DDL 由硬闸兜底
4. **CubeRouteGate** 位于 QueryGate **之前**——Cube 闸门优先级更高
5. **deepagents_async_config_patch** 必须在 `create_deep_agent` **之前**导入

## 目录级共性模式

### 1. 配置读取统一走 `langgraph.config.get_config()`

多处实证 `request.runtime.config` 恒为空 dict，这是本目录最重要的框架坑。所有需要读 `configurable` 的中间件都走这条路径。

### 2. deny 消息三件套

`status="error"` + `Error:` 前缀 + 中文可执行指引——让模型自我修正不死循环，且被评分层正确识别为失败。

### 3. fail-open vs fail-close 哲学

| 类型 | 代表 | 策略 |
|---|---|---|
| 观测/瘦身类 | message_slimmer, langfuse_span, token_meter | fail-open（异常不影响主流程） |
| 拦截类 | execute_guard, sql_approval | fail-close（宁可多拦） |
| 隔离类 | filesystem_thread_guard | fail-open（线程 id 取不到则放行） |
| 路由类 | query_gate, cube_route_gate | fail-open（库标记取不到则放行） |

### 4. 进程级 dict + 锁 + 容量上限清空

轻量缓存模式，防止内存泄漏：

| 中间件 | 缓存 | 上限 |
|---|---|---|
| progress_boundary | `_seen_bucket` | 2000 |
| query_gate | `_REMINDED` | 4000 |
| cube_route_gate | `_CUBE_GATE_REMINDED` | 独立命名空间 |
| thinking_toggle | `_model_cache` + `_last_thread_key` | 8 / 200 |
| langfuse_span | `_THREAD_ACTIVE_SKILL` / `_THREAD_TRACE_MAP` / `_ROOT_OBS_MAP` | 2000/500/500 |
| trace_recorder | `_cached_thread_id` | 无上限（per-request） |

### 5. 阈值同源

`LARGE_RESULT_TRUNCATE_CHARS` 被三处共读：message_slimmer / query_result_offload / langfuse_span。

### 6. 目录口径同源

`nl2sql_process_data/{thread}/` 落盘（langfuse_span、query_result_offload）与线程护栏（filesystem_thread_guard）共用 `_thread_id` 解析链。

### 7. 错误翻译为 AIMessage 的统一模式

model_timeout / quota_error / model_required 三者共用：不抛异常 → 返回友好中文 AIMessage → agent 正常结束 → 消息进 checkpoint。

### 8. hook 参数名必须叫 `runtime`

langchain 按参数名注入，改叫 `r` 会运行期报错。caliber_gate 实测确认。
