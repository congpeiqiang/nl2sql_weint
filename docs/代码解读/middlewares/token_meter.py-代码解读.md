# token_meter.py 代码解读

> 文件路径：`src/agent/middlewares/token_meter.py`（184 行）
> 解读日期：2026-09-18

## 一句话概括

`TokenMeterMiddleware`：在每次 LLM 调用边界采集 **token 用量与 wall-clock 耗时**，经 `ExtendedModelResponse + Command` 写入 state，由 reducer 累积成 `token_stats`（对标 deepseek-harness 的 session-stats projection）。

## state 字段结构

```text
token_stats = {
  "total_llm_ms": 5000,            # LLM 调用总耗时
  "total_input_tokens": 10000,
  "total_output_tokens": 800,
  "total_cache_read_tokens": 3000,
  "total_reasoning_tokens": 0,
  "step_count": 3,                 # LLM 调用次数
  "steps": [...]                   # 每步详情（含 round_index）
}
```

## 核心实现

### `wrap_model_call` / `awrap_model_call`
1. 调用前 `_current_round(request)` 取轮号、起计时；
2. 执行 handler——**异常不吞，直接上抛**（计时失败不影响 agent 循环）；
3. `_extract_usage`：遍历 `ModelResponse.result` 的 AIMessage 取 `usage_metadata`（取不到 → 原样返回不写 state，如某些流式响应无 usage）；
4. 返回 `ExtendedModelResponse(model_response=response, command=Command(update={"token_stats": step_stat}))`——这是 wrap_model_call 场景下**既透传响应又写 state** 的标准形态。

### `_accumulate_token_stats`（LangGraph state reducer）
- 5 个数值字段按 key 累加，`step_count` 递增，steps 追加；
- **steps 只留最近 50 条**（防 state 膨胀——checkpoint 会序列化整个 token_stats）。

### `_current_round`
数 `request.state["messages"]` 中 human 消息数 = 当前轮号（1-based）。ModelRequest.state 由 factory 构造时传入完整 state（不含 system）；取不到返回 0（前端按「无轮号」处理，不计入任何轮）。

### `_build_step_stat`
从 `usage_metadata` 的嵌套 details 提取：`input_token_details.cache_read`（缓存命中）、`output_token_details.reasoning_tokens`（思考 token）。

## 关键分工约定

**本中间件是 token 写 state 的唯一出口**：`trace_recorder.py` 只记事件（token 用量进事件 data 供回溯）**不写 state.token_stats**——注释明确实测过两中间件同时 update 同一字段导致 **reducer 累加重复（token 统计翻倍）**。

## 关联文件

- `trace_recorder.py`：事件层采集（互补，勿双写）
- `query_keywords.py` 等：`request.state` 可读性的框架事实
- 前端：按轮聚合展示 token_stats
