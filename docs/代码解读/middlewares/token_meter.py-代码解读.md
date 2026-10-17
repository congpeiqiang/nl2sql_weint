# token_meter.py — TokenMeterMiddleware 代码解读

> 源文件：`src/agent/middlewares/token_meter.py`
> 类别：**可观测性**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

对标 deepseek-harness 的 session-stats projection，需要在 LangGraph middleware 层采集每次 LLM 调用的 token 用量和耗时，累积到 state 供前端展示。

## 2. 核心机制

### 2.1 采集流程

```
t0 = time.time()
response = handler(request)      # 执行 LLM 调用
elapsed_ms = (time.time() - t0) * 1000

usage = _extract_usage(response)  # 从 AIMessage.usage_metadata 提取
step_stat = _build_step_stat(elapsed_ms, usage, round_index)

return ExtendedModelResponse(
    model_response=response,
    command=Command(update={"token_stats": step_stat}),
)
```

### 2.2 state 结构

`token_stats` 字段结构：

```json
{
    "total_llm_ms": 5000,
    "total_input_tokens": 10000,
    "total_output_tokens": 800,
    "total_cache_read_tokens": 3000,
    "total_reasoning_tokens": 0,
    "step_count": 3,
    "steps": [
        {"total_llm_ms": 1200, "total_input_tokens": 3000, "round_index": 1},
        {"total_llm_ms": 2000, "total_input_tokens": 4000, "round_index": 1},
        {"total_llm_ms": 1800, "total_input_tokens": 3000, "round_index": 2}
    ]
}
```

### 2.3 累积机制

`_accumulate_token_stats` 是 LangGraph state reducer：

- 数值字段（`total_*`）累加
- `step_count` 递增
- `steps` 列表追加，**只保留最近 50 步**防 state 膨胀

```python
def _accumulate_token_stats(existing, update):
    merged = dict(existing or {})
    for key in ("total_llm_ms", "total_input_tokens", ...):
        merged[key] = merged.get(key, 0) + update.get(key, 0)
    merged["step_count"] = merged.get("step_count", 0) + 1
    steps = list(merged.get("steps", []))
    steps.append(update)
    if len(steps) > 50:
        steps = steps[-50:]
    merged["steps"] = steps
    return merged
```

### 2.4 轮号计算

`_current_round` 从 `request.state["messages"]` 数 HumanMessage 的数量得到当前轮号（1-based）：

```python
def _current_round(request):
    state = getattr(request, "state", None) or {}
    messages = state.get("messages") or []
    human_count = sum(1 for m in messages if getattr(m, "type", "") == "human")
    return human_count or 0
```

## 3. usage_metadata 提取

`_extract_usage` 从 `ModelResponse.result`（AIMessage 列表）中提取 `usage_metadata`：

```python
def _extract_usage(response):
    if not isinstance(response, ModelResponse):
        return None
    for msg in getattr(response, "result", []) or []:
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            return usage
    return None
```

提取不到（如流式无 usage）→ 返回 None → 不写 state（原样返回 response）。

## 4. 关键设计决策

### 4.1 与 TraceRecorder 的分工

- **TokenMeter**：只写 `state.token_stats`（前端展示用）
- **TraceRecorder**：只记事件到 SQLite（本地追踪用），**不写 token_stats**

注释明确记录：实测双写导致统计翻倍（reducer 累加了两次）。

### 4.2 为什么用 ExtendedModelResponse

普通 `ModelResponse` 无法携带 state 更新。`ExtendedModelResponse` 的 `command` 字段可以附带 `Command(update={...})`，让 reducer 自动累加到 state。

### 4.3 为什么 steps 只保留 50 步

- 长会话可能有 100+ 次 LLM 调用
- steps 全量保留 → state 膨胀 → checkpoint 写入变慢
- 50 步足够前端渲染趋势图

## 5. 错误处理

- handler 抛异常 → 不吞异常，计时失败不影响 agent 循环
- usage_metadata 为 None → 不写 state，原样返回 response
- `_current_round` 异常 → 返回 0（前端按"无轮号"处理）
