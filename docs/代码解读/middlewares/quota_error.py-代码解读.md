# quota_error.py 代码解读

> 文件路径：`src/agent/middlewares/quota_error.py`（151 行）
> 解读日期：2026-09-18

## 一句话概括

`QuotaErrorMiddleware`：把 LLM 额度耗尽错误（HTTP 402/403 等）翻译为一条带友好中文提示的 AIMessage（**不抛异常**），解决前端「卡住、无提示」；与 `model_timeout.py` 同构。

## 解决的问题

模型账号额度耗尽时（DeepSeek 等返回 402/403，如 "Free quota exhausted" / "insufficient_quota" / "Insufficient Balance"），后端原始异常被 LangGraph 序列化进 run stream 的 error 事件，但前端 SDK（useStream）只把 error 放进 `stream.error` 状态、**聊天界面不渲染它** → 表现为「卡住、无提示」。

## 机制

捕获模型调用异常 → `is_quota_error` 判定 → 是则返回 `_friendly_response`（友好 AIMessage 组成的 ModelResponse）：
- agent 正常结束、消息写入 checkpoint、前端按普通 AI 回复渲染；
- AIMessage 不带 tool_calls → 直接走 END，不触发工具节点、不再抛"缺工具结果"错；
- 其他错误**原样上抛**（保持原有失败语义）。

## 识别策略（防误伤是重点）

| 级别 | 关键词 | 判定条件 |
|---|---|---|
| 强关键词 | insufficient_quota / quota exhausted / free quota / out of quota / insufficient balance / 额度 / 余额不足 / 欠费 等 | 单独命中即判定 |
| 弱关键词 | quota / balance / 余额 | 必须**同时**出现 HTTP 状态码 402/403/429（文本中 ` 402`/`status: 402`/`status_code=402` 或异常 `status_code` 属性）才判定 |

- 弱关键词加状态码佐证的原因：避免把含 "balance"/"quota" 的**业务文本**（如 SQL 报错）误判为额度问题。
- 429 也在佐证状态码集合中（供应商常用 429 表达配额耗尽）。
- 401 鉴权失败、纯 SQL 错误等原样透传。

## 约束（P1-9 配置权威性）

本中间件**只翻译错误文案，不读取/回退 .env 的 LLM_***——模型配置唯一来源仍是前端 CRUD 的 `model_config.json`。

## `QuotaExhaustedError` 类

供**非 agent 场景**（auto_title / thread_compact 等直接调用模型、自带 try/except 降级的调用点）手动翻译用——那些调用点不走本中间件；`_translate_quota_error` 保留原始异常作 cause 便于追踪。

## 关联文件

- `model_timeout.py`：同构错误翻译器
- `thinking_toggle.py`：额度耗尽后的换模型路径（文案指路「模型配置」）
