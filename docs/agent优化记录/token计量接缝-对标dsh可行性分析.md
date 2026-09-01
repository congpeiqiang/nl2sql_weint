# nl2sql Token 计量接缝：对标 dsh 可行性分析

> 2026-08-18 · 两项目源码级交叉核验
> 对标对象：`D:/code_work_space/llm/deepseek-harness`（dsh）
> 目标：评估 nl2sql 能否实现 dsh 的 token 计量 + 性能统计 UI

## 一、dsh 的完整数据流

### 1.1 数据采集层

dsh 的 token/timing 数据**全部来自会话事件日志**（事件溯源架构），不是从 LLM provider 返回的 metadata 读的：

```
DeepSeek API
    ↓ fetch + SSE
DeepSeekAdapter.stream()
    ↓ emits StreamChunk (text-delta, usage, finish)
Session Layer
    ↓ 记录事件（带 wall-clock 时间戳）：
    │   step/start → assistant/chunk (首个 isTokenDelta()=true 即 TTFT)
    │   → assistant/message → tool/call → tool/result → step/end → turn/end
    ↓
session-stats projection（服务器端 fold）
    │   turns, steps, llmMs, toolMs, ttftMs, ttftSteps, decodeMs, decodeTokens
    ↓
Client 接收 events → assistant-timing.ts（客户端 fold）
    ↓   AssistantTiming { stepStartTime, firstTokenTime, completedTime }
    ↓
UI 渲染
```

### 1.2 关键数据来源

| 数据 | 来源 | 如何得出 |
|------|------|---------|
| **TTFT**（首 token 时间） | 会话事件时间戳 | `step/start` 时间 → 第一个 `assistant/chunk`（`isTokenDelta()=true`）的时间差 |
| **tok/s**（解码速度） | provider usage + 事件时间戳 | `outputTokens / (decodeMs / 1000)` |
| **LLM 耗时** | 会话事件时间戳 | `step/start` → `assistant/message` |
| **工具耗时** | 会话事件时间戳 | `tool/call` → `tool/result` 按 callId 配对 |
| **输入/输出 token** | DeepSeek API `usage` 字段 | `mapUsage()` 转换为 disjoint 计数 |
| **缓存命中率** | DeepSeek API `usage.prompt_tokens_details.cached_tokens` | `cacheReadTokens / (uncachedInput + cacheRead + cacheWrite) * 100` |
| **轮数/步数** | 会话事件计数 | `turn/end`、`step/end` 事件 |

### 1.3 UI 渲染位置

**消息级**（`TurnTailNodeView` → `MessageIconActions`）：
```
14:03 · 用时 3秒 · 首 token 0.8秒 · 99 tok/s
```

**会话级**（`StatsLine`，composer 下方 dock）：
```
3 轮 · 7 步 | LLM 27.3s · 工具调用 17.4s | 首 token 平均 0.7s · 125 tok/s | 缓存命中 77% | 输入 136K tok · 输出 2.8K tok
```

**上下文占用**（`ContextMeter`，send 按钮旁）：
SVG 圆环显示上下文占用百分比，点击展开 system/tools/messages 三段分解。

---

## 二、nl2sql 现有可用数据

### 2.1 ✅ 已有：LangChain `usage_metadata`

LangChain 的 `AIMessage`（`langchain_core.messages.ai`）**已内置** `usage_metadata` 字段：

```python
# langchain_core/messages/ai.py:176
class AIMessage(BaseMessage):
    usage_metadata: UsageMetadata | None = None
```

`UsageMetadata` 结构（[ai.py:104-170](D:\code_work_space\llm\nl2sql\.venv\Lib\site-packages\langchain_core\messages\ai.py#L104-L170)）：
```python
class UsageMetadata(TypedDict):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_token_details: InputTokenDetails  # cache_read, cache_write
    output_token_details: OutputTokenDetails  # reasoning_tokens
```

**ChatDeepSeek/ChatOpenAI 每次调用完成后，`AIMessage.usage_metadata` 会自动填充。** 这是 LangChain 的标准行为，无需额外代码。

**当前状态**：nl2sql 代码中**完全没有读取这个字段**——`model.py`、所有 middleware、`check_progress.py` 都没有使用 `usage_metadata`。

### 2.2 ✅ 已有：`response_metadata` 中的时间戳

`check_progress.py` 的 `_extract_timing_from_messages()` 已从 `response_metadata` 读取时间戳估算耗时：

```python
# check_progress.py:436-448
meta = msg.get("response_metadata") or {}
ts = (
    meta.get("timestamp")
    or meta.get("created_at")
    or (meta.get("model_extra") or {}).get("created")
)
```

但这是**估算**，不是精确测量（用的是 provider 返回的时间戳，不是本地 wall-clock）。

### 2.3 ✅ 已有：`_format_duration()` 格式化

`check_progress.py` 已有 duration 格式化函数。

### 2.4 ✅ 已有：async_tasks 状态中的时间戳

每条 async task 有 `created_at`、`last_checked_at`、`last_updated_at`，可用于子 agent 总耗时。

### 2.5 ❌ 缺失：TTFT 测量

**没有任何代码测量首 token 时间。** 流式 SSE 在前端接收，但前端没有记录首个 chunk 到达时间。

### 2.6 ❌ 缺失：tok/s 计算

没有代码计算输出 token 除以解码时间。

### 2.7 ❌ 缺失：工具调用耗时

没有代码测量 `tool/call` → `tool/result` 的 wall-clock 时间。

### 2.8 ❌ 缺失：缓存命中率

DeepSeek API 返回的 `usage.prompt_tokens_details.cached_tokens` 进入了 `usage_metadata.input_token_details.cache_read`，但无人读取。

### 2.9 ❌ 缺失：前端全部 token/timing UI

grep 确认前端 `harness-deep-agents-ui` **没有任何** TTFT、tok/s、token 用量、缓存命中率相关的代码。

---

## 三、差距对比

| 能力 | dsh 实现方式 | nl2sql 能否实现 | 难度 |
|------|------------|----------------|------|
| **输入/输出 token 数** | 会话事件 → session-stats projection | ✅ 能：读 `AIMessage.usage_metadata` | **低** |
| **缓存命中率** | DeepSeek `usage.prompt_tokens_details.cached_tokens` | ✅ 能：`usage_metadata.input_token_details.cache_read` | **低** |
| **LLM 总耗时** | 会话事件 `step/start` → `assistant/message` 时间戳 | ✅ 能：需在 middleware 层打点 | **中** |
| **token/s（解码速度）** | `outputTokens / decodeMs` | ✅ 能：需 TTFT 后的解码时间 | **中**（依赖 TTFT） |
| **TTFT（首 token）** | 会话事件 `step/start` → 首个 `isTokenDelta()` chunk | ✅ 能：需在 SSE 流式层打点 | **中** |
| **工具调用耗时** | 会话事件 `tool/call` → `tool/result` | ✅ 能：需 hook 工具调用前后 | **中** |
| **轮数/步数** | 会话事件计数 | ✅ 能：LangGraph state 中已有步骤信息 | **低** |
| **上下文占用** | token-meter projection → contextPressure | ✅ 能：需 tokenizer 估算 | **高**（需要 tokenizer） |
| **推理 token 数** | `usage.completion_tokens_details.reasoning_tokens` | ✅ 能：`usage_metadata.output_token_details.reasoning_tokens` | **低** |

---

## 四、实现方案要点

### 4.1 核心差异：dsh 是事件溯源，nl2sql 是 checkpoint

dsh 的会话事件是**追加式日志**，每个事件自带 wall-clock 时间戳，所以 timing 数据天然可得。

nl2sql 是 LangGraph checkpoint，**没有记录每个事件的时间戳**。需要自己打点。

### 4.2 打点位置

```
nl2sql 调用链路：
用户输入 → LangGraph → middleware 链 → ChatDeepSeek SDK → start_async_task → 子 agent
                ↑              ↑              ↑                    ↑
             [打点1]        [打点2]       [打点3]              [打点4]
```

| 打点 | 时机 | 采集数据 |
|------|------|---------|
| 打点1 | 请求进入 middleware 链 | `step_start_time` |
| 打点2 | 首个 SSE token 到达 | `first_token_time`（→ TTFT） |
| 打点3 | LLM 调用完成 | `usage_metadata`（→ input/output tokens, cache, reasoning） |
| 打点4 | 子 agent 完成 | `sub_agent_duration` |

### 4.3 数据汇总方式

dsh 的模式不适合直接搬（事件溯源 vs checkpoint 架构差异太大）。推荐方案：

**方案 A：中间件层打点 + state 字段**

在 `ThinkingToggleMiddleware` 或新建 `TokenMeterMiddleware` 中：
1. `wrap_model_call` 前记录 `time.time()`
2. `wrap_model_call` 后读取 `AIMessage.usage_metadata`，计算 TTFT（从流式首个 chunk）、解码时间
3. 写入 state 的 `token_stats` 字段（新字段，累积型）
4. 前端从 state 读取渲染

**方案 B：前端侧打点**

在 `ChatInterface.tsx` 的 SSE 处理中：
1. 记录 `sendMessage` 时间
2. 记录首个 content chunk 到达时间（→ TTFT）
3. 记录 `finish` 事件时间（→ 总耗时）
4. 从最后的 `usage_metadata`（如果有传到前端）读 token 数

**推荐方案 A**：数据更完整（能拿到 provider 的 usage_metadata），且能跨 session 持久化。

### 4.4 Token 数据不需要 tokenizer

除非要做上下文占用环（ContextMeter），否则**不需要 tokenizer**。输入/输出 token 数直接从 `usage_metadata` 读，这是 provider 返回的真实计数，比任何 tokenizer 估算都准。

### 4.5 缓存命中率

`usage_metadata.input_token_details` 里有 `cache_read`（缓存命中）和 `cache_write`（缓存写入），可以直接算。但注意：DeepSeek 的 prompt caching 是**自动的**（不需要手动标记），所以 cache_read 取决于 prompt 前缀是否重复。

---

## 五、结论

**nl2sql 完全可以实现 dsh 级别的 token 计量和性能统计。** 数据源（`usage_metadata`）已经由 LangChain 自动采集，只是没人读。主要工作量在：

1. **打点**：新建 middleware 记录 TTFT、解码时间、工具耗时
2. **汇总**：把数据写入 state 或前端
3. **前端 UI**：消息级 tooltip + 会话级 StatsLine（纯前端，可独立做）

上下文占用环（ContextMeter）需要 tokenizer，其他全部不需要。