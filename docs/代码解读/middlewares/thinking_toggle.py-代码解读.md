# thinking_toggle.py 代码解读

> 文件路径：`src/agent/middlewares/thinking_toggle.py`（219 行）
> 解读日期：2026-09-18

## 一句话概括

`ThinkingToggleMiddleware`：前端单开关 `enable_thinking` 控制模型**真实**思考（开→reasoning_content 流出前端显示；关→无思考），并支持 P1-9 模型路由（`llm_route`/`llm_model` 每次调用重建模型，**切模型免重启后端**），带进程级模型实例缓存与 P1-10 auto-continue 模型继承。

## 机制

- 前端把 `enable_thinking`（"true"/"false"）随 run 传入 `configurable.enable_thinking`（与 query_keywords / db_name 同一通道，已实证可到达；读取同样必须走 `langgraph.config.get_config()`）。
- `wrap_model_call` 时用 `create_model(enable_thinking=..., route=..., model_name=...)` 重建模型实例并 `request.override(model=...)`。
- **时序依据**：langchain 在替换**之后**才执行 `request.model.bind_tools(...)`（factory `_execute_model_sync` 从 request.model 派生最终调用模型）→ 工具绑定/响应格式不受影响。
- configurable 三项全缺 → 返回 None 不替换，走 import 时默认模型（两个模型默认思考开）。
- enable 解析容错：`str(val).lower() in ("true","1","yes","on")`。

## 进程级模型缓存（性能关键）

`ChatDeepSeek` 构造实测 **~8.2s**——每次模型调用都重建会毁掉延迟：
- 缓存键：`(enable_thinking, route, model_name)` 三元组 → 模型实例，命中 ~0s；
- **失效机制**：比对 `model_config.json` 的 **mtime**（前端 CRUD 改配置即自动清缓存）；
- 上限 8 条，超限 FIFO 淘汰（防内存泄漏）。

## P1-10 auto-continue 模型继承

**问题**：同一 thread 的后续 auto-continue run（异步子智能体完成后前端自动续跑）**不携带** llm_route/llm_model/enable_thinking（续跑只发系统通知消息）→ 会回退到模块级默认模型（可能已欠费/不可用）。

**方案**：`_last_thread_key: dict[thread_id → cache_key]` 记录每个 thread 最后一次**显式配置**；缺配置的 run 自动复用该 thread 上次的模型实例 → **整会话使用同一模型**。上限 200 线程，FIFO 淘汰。

## 设计细节

- 缓存命中与新建两条路径都会 `_record_thread_key`（显式配置即更新继承锚点）。
- 日志分级：新建模型 info（含缓存条目数），缓存命中/继承 debug。

## 关联文件

- `agent/llms/model.py`：`create_model` 工厂
- `quota_error.py` / `model_timeout.py`：默认模型欠费/超时场景（继承机制避免续跑踩坑）
- `workspace_manager.py`：model_config_path mtime 来源
