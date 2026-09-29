# model_timeout.py 代码解读

> 文件路径：`src/agent/middlewares/model_timeout.py`
> 解读日期：2026-09-18（2026-09-28 补「失败戳」一节）

## 一句话概括

`ModelTimeoutMiddleware`：把 LLM 调用超时异常翻译为一条带友好中文文案的 AIMessage（**不抛异常**），解决前端「卡住、无提示」问题。与 `quota_error.py` 同构。

## 解决的问题

模型 API 在限定时间内无响应（httpx ReadTimeout / openai APITimeoutError，消息常为 "Request timed out."）。后端原始异常会被 LangGraph 序列化进 run stream 的 `error` 事件，而**前端 SDK（useStream）不渲染 stream.error** → 表现为「卡住、无提示」（trace ef83792 实证：model 节点 4×60s 重试后超时，界面空白）。

## 关键机制决策：为什么不抛异常

抛异常 → 进 stream.error → 前端不渲染。改为捕获后**返回正常 ModelResponse**：
- agent 正常结束、消息写入 checkpoint、前端按普通 AI 回复渲染 → 用户直接看到「模型调用超时…」；
- AIMessage **不带 tool_calls** → 模型节点后直接走 END，不触发工具节点，不会因「缺工具结果」再抛错；
- 消息经 add_messages reducer 写入 checkpoint → **刷新页面后仍可见**。

## 超时识别 `is_timeout_error`（判定链，任一命中即判定）

1. `httpx.TimeoutException`（ReadTimeout/ConnectTimeout/WriteTimeout/PoolTimeout 等，`__str__` 都是 "Request timed out."）；
2. 内置 `TimeoutError`（部分 SDK / asyncio.timeout 直接抛）；
3. 类名含 `Timeout`（openai.APITimeoutError、requests.ConnectTimeout 等）;
4. 正则 `(request|read|connect|write|pool)?\s*(timed\s?out|time\s*out)` 匹配消息文本；
5. **递归遍历异常链** `__cause__` / `__context__`（带 `seen` id 集合防循环引用）。

## 失败戳：`_friendly_response` 里同一条消息带 `nl2sql_failure`（2026-09-28 新增）

上面「不抛异常」的方案有个**副作用**：这条友好消息在链路上跟一条真实答复**完全无法区分**。图照常 END ⇒ SDK 报 `success`，于是三个读取方各自猜错（生产 trace `3dcc9a66…` 实证）：

| 读取方 | 没有戳时的错误行为 |
|---|---|
| 子图 watcher（`sync_subagent_todos._async_sync_loop`） | `run.status=="success"` ⇒ 发「已完成查询任务」续跑 |
| `check_async_task`（`_enhanced_build_check_result`） | 同一个判据 ⇒ 主 agent 拿到 `status:"success"` + 超时文案，只能自己推理「marked as success, but the result is actually a timeout error」 |
| `CaliberGateMiddleware._after` | 把超时文案当「终稿缺 `## 业务口径` 块」⇒ `jump_to="model"` **打回**，再烧一次 240s（820s 事故里的 234s 空窗） |
| 主图 `/api/threads/{tid}/run-status` | 末条是终稿文本 ⇒ 一律 `turn_incomplete=false`，用户看到一段"正常回答" |

修法：`_friendly_response` 返回前用 `mark_failed(msg, KIND_MODEL_TIMEOUT, MODEL_TIMEOUT_MESSAGE)`（[utils/failure_signal.py](../../../src/agent/utils/failure_signal.py)）在 `additional_kwargs["nl2sql_failure"]` 写 `{"kind","detail","at"}`。**文案一字符未改**，逻辑分支（`_classify_failure` 的 `("friendly", 0.0)`、两个调用点）也一字未动——只是多带一个袋子。

- **为什么不是文本匹配**：文案是可调项；主 agent 向用户解释失败时正文里也会出现「模型调用超时」几个字，会误伤。本仓已有两次「纯 prompt 契约被证伪」的先例（见 `caliber_gate.py` docstring）。
- **为什么放 `additional_kwargs`**：全链透传、能过 checkpoint 序列化往返、一路到前端；本仓已在当平台语义用（`additional_kwargs.tool_calls`、`lc_source`、`reasoning_content`）。`response_metadata` 是 provider 侧重建的袋子，不能用。
- **值只许 JSON 原生标量**：`mark_failed` 内部统一 `str()`、detail 截 500 字符——序列化器遇未知类型会抛异常，那比现状更糟。
- **`kind`→run 状态是单点映射** `run_status_for`：`model_timeout → "timeout"`，其余 → `"error"`。硬约束：只许产出 `error`/`timeout`（都在 `_RUN_DONE_STATUSES` 里），写错的串会让前端卡片永远不结束。
- **无戳 = 今天的旧行为**，逐字不变；历史 checkpoint 不受影响。负对照在 `scripts/verify_failure_signal.py`（④「无戳 → 仍 success 且带 result」、③「同样文案但无戳 → 仍打回」）。

## 超时为什么不重试（2026-09-28 复核，维持原决定）

SDK 侧已是 `timeout=60, max_retries=3` 的四连击 ≈240s（[model.py:417-418](../../../src/agent/llms/model.py#L417-L418)）；[llm_gate.py:215-218](../../../src/agent/utils/llm_gate.py#L215-L218) 已记着「再加次数会把单次模型调用推向工具超时 300s」。本层再加一轮会直接撞工具超时。超时的正确出路是**快速失败 + 明确失败态 + 用户可见的重试按钮**（前端 `ChatInterface.tsx` 的「上一轮执行失败（未完成）」+ 重试，由 `run-status` 的 `turn_failed` 驱动）。

## 边界

- 只翻译「模型调用超时」，**其他错误原样上抛**（保持原有失败语义）。
- 包在模型调用边界（`wrap_model_call`）——业务 SQL 报错不会以异常形式出现在这里，不会误伤。
- httpx import 失败不影响（openai 自带 httpx，且有类名/文本兜底）。

## 文案

`MODEL_TIMEOUT_MESSAGE`：说明常见原因（服务繁忙、网络波动、推理模型大上下文首字延迟高）+ 行动指引（稍后重试；反复超时可在「模型配置」换模型）。

## 关联文件

- `quota_error.py` / `model_required.py`：同构的错误翻译中间件（额度耗尽 / 未配模型），**同样打戳**
- `agent/utils/failure_signal.py`：戳的契约与生产者/消费者清单
- `scripts/verify_failure_signal.py`：七段验收（每段带负对照）
- 前端 useStream 集成（SDK 不渲染 stream.error 是本方案动因）
