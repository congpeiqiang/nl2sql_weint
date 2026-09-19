# model_timeout.py 代码解读

> 文件路径：`src/agent/middlewares/model_timeout.py`（130 行）
> 解读日期：2026-09-18

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

## 边界

- 只翻译「模型调用超时」，**其他错误原样上抛**（保持原有失败语义）。
- 包在模型调用边界（`wrap_model_call`）——业务 SQL 报错不会以异常形式出现在这里，不会误伤。
- httpx import 失败不影响（openai 自带 httpx，且有类名/文本兜底）。

## 文案

`MODEL_TIMEOUT_MESSAGE`：说明常见原因（服务繁忙、网络波动、推理模型大上下文首字延迟高）+ 行动指引（稍后重试；反复超时可在「模型配置」换模型）。

## 关联文件

- `quota_error.py`：同构的错误翻译中间件（额度耗尽）
- 前端 useStream 集成（SDK 不渲染 stream.error 是本方案动因）
