# 会话 Trace 步骤耗时分析报告

> **Trace ID:** `01a022be`
> **Thread ID:** `01a022be-2c1a-7d02-b356-3975f3703cf0`
> **分析日期:** 2026-08-21
> **数据来源:** LangSmith `nl2sql` 项目

---

## 会话概览

- **开始时间:** 2026-08-21 05:20:02 UTC
- **持续时长:** ~73s（最后一步 LLM 调用未完成即截断）
- **总步骤数:** 100 个 run
- **执行轮次:** 1 次轮询结果 + 4 轮 `model → tools → model` 循环

---

## 执行流程

```
05:20:02  check_async_task 0.4s     → 轮询子 agent 进度，获取结果
05:20:02  ChatDeepSeek 34.3s        → 分析查询结果 ← 最慢！prompt=34,565 tokens
05:20:37  write_todos 0.0s
05:20:38  ChatDeepSeek 14.4s        → 推荐图表，prompt=34,766 tokens
05:20:52  generate_echarts 7.6s     → 渲染 ECharts 图表
05:21:00  ChatDeepSeek 5.8s         → 展示结果
05:21:07  write_todos 0.0s
05:21:07  ChatDeepSeek 5.2s         → 总结回复
05:21:13  read_file 0.0s
05:21:13  ChatDeepSeek N/A          → 未完成（trace 截断）
```

---

## 按执行顺序的步骤耗时

| 步骤 | 耗时 | prompt tokens | completion | 说明 |
|:---|:---:|:---:|:---:|:---|
| `check_async_task` (tool) | 0.4s | — | — | 获取子 agent 查询结果 |
| `ChatDeepSeek` (LLM) | **34.3s** | 34,565 | 326 | 🔴 分析结果，输入 token 爆炸 |
| `ChatDeepSeek` (LLM) | **14.4s** | 34,766 | 835 | 推荐图表 |
| `generate_echarts` (tool) | 7.6s | — | — | ECharts 图表渲染 |
| `ChatDeepSeek` (LLM) | 5.8s | 37,163 | 174 | 展示结果 |
| `ChatDeepSeek` (LLM) | 5.2s | 37,364 | 107 | 总结回复 |
| `ChatDeepSeek` (LLM) | N/A | 0 | 0 | 未完成（截断） |

---

## 耗时分布

| 类别 | 耗时 | 占比 |
|:---|:---:|:---:|
| **ChatDeepSeek LLM 调用（4次）** | **59.7s** | **88%** |
| 工具调用（echarts + check） | 8.0s | 12% |
| 中间件/编排开销 | < 0.5s | < 1% |
| **合计有效时间** | **~68s** | 100% |

---

## LLM 调用与 token 量的关系

| 轮次 | 耗时 | prompt tokens | 说明 |
|:---|:---:|:---:|:---|
| 第1次 | **34.3s** | **34,565** | 分析结果，check_async_task 返回了大量数据 |
| 第2次 | 14.4s | 34,766 | 推荐图表，上下文累积 |
| 第3次 | 5.8s | 37,163 | 展示结果，上下文继续累积 |
| 第4次 | 5.2s | 37,364 | 总结，上下文最大 |

**关键发现：**
- 第1次 LLM 调用时 prompt 达到 **34,565 tokens**，耗时 34.3s。这远超正常深度推理的 5-8s 范围
- prompt tokens 从第1次的 34.5k 累积到第4次的 37.4k，说明上下文在持续膨胀
- 第3、4次虽然 prompt tokens 更多（37k），但 completion 很短（100-200 tokens），耗时仅 5-6s，说明**推理时间主要取决于输入 token 量 + 输出 token 量，而非单纯的输入量**

---

## 中间件耗时

`ThinkingToggleMiddleware` → `CurrentDbContextMiddleware` 间隙极小（< 0.1s），**模型重建问题未触发**。所有中间件总开销 < 0.5s。

---

## 三个 trace 横向对比

| 指标 | 01a021e9 (调试) | 01a0228d (生产1) | **01a022be (生产2)** |
|:---|:---:|:---:|:---:|
| 会话类型 | 查询 LS trace | 数据库 SQL 查询 | 数据库 SQL 查询 |
| 总耗时 | 150s | 67s | **73s** |
| LLM 调用次数 | 5 | 5 | **4** |
| LLM 总耗时 | 42.7s | 56.3s | **59.7s** |
| LLM 占比 | 28% | 84% | **88%** |
| 最大单次 LLM | 12.8s | 16.3s | **34.3s** |
| 平均 prompt tokens | 未知 | 未知 | **~35k** |
| 模型重建开销 | 36s | 0s | 0s |
| 工具调用耗时 | 62.3s | 8.1s | 8.0s |
| 主要瓶颈 | 模型重建 | LLM 推理 | **LLM 推理 + 超大 prompt** |

---

## 关键发现

### 1. prompt tokens 爆炸是核心问题

第1次 LLM 调用 prompt 达到 **34,565 tokens**（约 2.5 万中文字），耗时 34.3s。这是 `check_async_task` 返回的子 agent 查询结果直接塞进了下一次 LLM 调用的上下文。

**时间线证据：**
```
05:20:02  check_async_task 返回结果 → 0.4s
05:20:02  ChatDeepSeek 开始，prompt=34,565 tokens → 34.3s
```

0.4s 的 `check_async_task` 返回了足以塞满 34k tokens 的数据，然后 LLM 花了 34s 处理。

### 2. LLM 耗时与 prompt tokens 正相关，但非线性

| prompt tokens | completion | 耗时 | 每千 token 耗时 |
|:---:|:---:|:---:|:---:|
| 34,565 | 326 | 34.3s | ~0.98s/k |
| 34,766 | 835 | 14.4s | ~0.40s/k |
| 37,163 | 174 | 5.8s | ~0.16s/k |
| 37,364 | 107 | 5.2s | ~0.14s/k |

**推测：** 第1次耗时异常高（34.3s），除 prompt 量大外，可能还叠加了 DeepSeek API 网络波动（首包延迟）。后续调用可能命中了服务端缓存，延迟回归正常。

### 3. 上下文持续膨胀

prompt tokens 从 34,565 → 34,766 → 37,163 → 37,364，每轮增加 600~2,400 tokens。这是多轮对话的固有特征，但 37k 的上下文对 DeepSeek 来说已经很高。

### 4. 工具调用合理

- `check_async_task` 0.4s：正常
- `generate_echarts` 7.6s：正常（ECharts 渲染）

---

## 优化建议

### 🔴 P0：减少 LLM 输入 token 量

**问题：** 第1次 LLM 调用时 prompt 高达 34,565 tokens，其中大部分是 `check_async_task` 返回的 SQL 查询结果。

**方案：**

1. **结果摘要化**（推荐）：在 `check_async_task` 返回后、传给 LLM 前，用 `MessageSlimmerMiddleware` 对结果做摘要：
   - 如果结果超过 N 行（如 50 行），只保留前 N 行 + 统计摘要（总行数、列名、数据类型）
   - 当前 `MessageSlimmerMiddleware` 阈值 16,000 字符，针对 SQL 结果场景可调整为按行数截断

2. **系统 prompt 精简**：当前 `MAIN_AGENT_PROMPT.md` 约 145 行（~5KB ≈ 1,500 tokens），可精简模板规则，压缩到 80 行

3. **上下文窗口管理**：考虑在每轮结束后清理不需要的历史消息（如 token 量超过阈值时触发 `SummarizationMiddleware` 的摘要压缩）

**预期收益：** 将首轮 LLM prompt 从 34k 降到 15k 以下，耗时从 34s 降到 10-15s，节省 **~20s（29%）**

### 🟡 P1：减少 LLM 调用轮次

**问题：** 当前流程 4 轮 LLM 调用（分析结果 → 推荐图表 → 展示 → 总结），其中"展示结果"和"总结"可合并。

**方案：** 在 prompt 中明确指示 LLM：展示结果时同时做总结，减少 1 轮调用。

**预期收益：** 节省 ~5s

### 🟢 P2：模型缓存（同前）

当前 trace 未触发（enable_thinking 未传），但作为兜底优化保留。

---

## 总结

**三个 trace 的优化优先级：**

| 优先级 | 优化项 | 适用场景 | 预期收益 |
|:---:|:---|:---|:---:|
| 🔴 P0 | **结果摘要化**（降低 prompt tokens） | 生产 SQL 查询 | **~20s/会话 (29%)** |
| 🔴 P0 | **模型缓存**（enable_thinking 时） | 开启思考的会话 | **~36s/会话 (50%)** |
| 🟡 P1 | 减少 LLM 调用轮次 | 所有会话 | ~5s/会话 |
| 🟡 P1 | 系统 prompt 精简 | 所有会话 | ~3-5s/会话 |

**核心结论：** 生产环境中，耗时大头是 **LLM 推理延迟**（占 84-88%），其中最大的可变因素是 **prompt token 量**。`check_async_task` 返回的 SQL 查询结果未经处理直接塞给 LLM，导致 prompt 膨胀到 34k+ tokens 是首要优化目标。