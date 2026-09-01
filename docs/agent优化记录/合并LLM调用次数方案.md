# 合并 LLM 调用次数方案

> 基于 trace 01a022be/01a0228d 分析：生产环境 SQL 查询会话中，主 agent 在收到子 agent 结果后，执行 4-5 轮 LLM 调用（分析结果 → 推荐图表 → 展示结果 → 总结回复），其中"展示结果"和"总结回复"语义重叠，可合并。减少 1-2 轮 LLM 调用，节省 ~5-10s/会话。

---

## 问题定位

### 当前 LLM 调用序列（生产 SQL 查询）

以 trace 01a022be 为例：

```
轮次1: ChatDeepSeek 34.3s → 分析 check_async_task 返回的查询结果
轮次2: ChatDeepSeek 14.4s → 推荐图表类型
       generate_echarts 7.6s → 渲染 ECharts 图表
轮次3: ChatDeepSeek 5.8s  → 展示结果（含图表 HTML）
轮次4: ChatDeepSeek 5.2s  → 总结回复
```

**问题**：轮次 3（展示结果）和轮次 4（总结回复）是两个独立的 LLM 调用，但功能高度重叠——都是把查询结果和图表呈现给用户。轮次 4 的 "总结" 本质上就是轮次 3 末尾的几句话。

### 根因：prompt 中的"步骤思维"引导 LLM 分步执行

当前 `MAIN_AGENT_PROMPT.md` 中：

```
数据查询 → nl2sql 子智能体
编排规则：查询 → 推荐图表 → 渲染图表 → 生成报告（全自动串联）
```

以及 `write_todos` 的示例：

```json
write_todos([
    {"content": "委派 nl2sql 执行查询", "status": "in_progress"},
    {"content": "推荐并渲染图表", "status": "pending"},
    {"content": "生成分析报告", "status": "pending"}
])
```

这暗示 LLM 把"推荐图表"、"渲染图表"、"展示结果"、"总结"当作独立的步骤，每步都需要一次 LLM 调用。

---

## 方案设计

### 总思路：在 prompt 中明确指示"展示结果时一并总结"，减少独立步骤

不是改代码架构，而是**改 prompt 行为约束**。当前 prompt 没有明确告诉 LLM 哪些步骤可以合并，导致 LLM 默认按最小粒度拆分。

### 方案 A：prompt 中明确"展示即总结"（推荐）

#### 修改 `MAIN_AGENT_PROMPT.md` 的数据查询编排规则

**修改前：**
```
编排规则：查询 → 推荐图表 → 渲染图表 → 生成报告（全自动串联），其中图表和报告不强制生成
```

**修改后：**
```
编排规则（全自动串联，合并相邻步骤以减少轮次）：
1. 收到子 agent 结果后，一次性完成：分析结果 + 推荐图表 + 渲染图表 + 展示并总结
2. 展示结果时，直接在回复中附上总结分析，不要分成"展示"和"总结"两次回复
3. 图表推荐和渲染可在同一轮完成（推荐图表时直接调用 generate_echarts）
```

#### 修改 `write_todos` 示例

**修改前：**
```json
write_todos([
    {"content": "委派 nl2sql 执行查询", "status": "in_progress", "id": "1"},
    {"content": "推荐并渲染图表", "status": "pending", "id": "2"},
    {"content": "生成分析报告", "status": "pending", "id": "3"}
])
```

**修改后：**
```json
write_todos([
    {"content": "委派 nl2sql 执行查询", "status": "in_progress", "id": "1"},
    {"content": "分析结果并展示（含图表推荐/渲染）", "status": "pending", "id": "2"},
    {"content": "生成分析报告", "status": "pending", "id": "3"}
])
```

#### 增加合并规则

在"交互原则"中新增：

```
### 合并 LLM 调用
- 收到子 agent 结果后，**在同一轮回复中**完成：数据分析 + 图表推荐 + 结果展示 + 总结
- 不要分多次回复说"接下来分析结果""接下来推荐图表""接下来展示结果"
- 图表渲染（generate_echarts）可在同一轮 LLM 调用中直接触发，不需要额外一轮"推荐图表"的纯文本回复
```

#### 预期效果

| 轮次 | 优化前 | 优化后 |
|:---|:---|:---|
| 1 | 分析结果 (34.3s) | 分析结果 + 推荐图表 + 展示 + 总结 (合并为 1 轮) |
| 2 | 推荐图表 (14.4s) | 渲染图表（纯工具调用，token 小） |
| 3 | 展示结果 (5.8s) | 生成报告（可选） |
| 4 | 总结回复 (5.2s) | — |

**合并后：轮次 1 的 prompt tokens 会略增（因为一次性要输出更多内容），但省掉了轮次 3+4 的额外 LLM 调用。**

| 指标 | 优化前 | 优化后 | 节省 |
|:---|:---:|:---:|:---:|
| LLM 调用次数 | 4 次 | 2-3 次 | -1~2 次 |
| LLM 总耗时 | ~60s | ~45-50s | **~10-15s (17-25%)** |

---

### 方案 B：子 agent 结果通知时自动触发图表渲染（更激进）

不仅改 prompt，还改代码逻辑：当 `check_async_task` 返回 `success` 时，自动触发图表推荐/渲染，减少 LLM 决策轮次。

#### 思路

在 `check_progress.py` 的 `_enhanced_build_check_result` 中，当子 agent 返回成功时，在 result 中附带一个 `suggested_action` 字段：

```python
if run["status"] == "success":
    result["suggested_action"] = "evaluate_and_render_chart"
    result["hint"] = (
        "如果数据适合可视化，请在同一轮回复中直接调用 generate_echarts 渲染图表，"
        "并在展示结果时一并总结分析。不要分多轮。"
    )
```

#### 风险

- 代码层干预 LLM 决策，可能在某些场景下不适用（如用户只想知道一个数字）
- 增加了 `check_async_task` 返回内容的复杂度

**推荐作为方案 A 的补充，而非替代。**

---

### 方案 C：合并"委派 + 轮询"为一步（已有 HITL 时）

当前流程（trace 01a0228d）：
1. LLM 调用：委派子 agent → 16.3s
2. HITL interrupt → 用户审批
3. LLM 调用：check_async_task 轮询 → 5.7s
4. LLM 调用：分析结果 → 14.7s

轮次 1（委派）和轮次 3（轮询）之间的 HITL 中断是必须的（用户审批 SQL 执行），但轮次 3 的 `check_async_task` 调用本身可以优化：在 HITL 审批通过后，系统自动调用 `check_async_task` 而无需 LLM 决策。

#### 思路

在 `HumanInTheLoopMiddleware` 审批通过后，自动触发一次 `check_async_task`，把结果直接注入上下文，跳过 LLM 的"决定去轮询"这一步。

**不推荐**：改动涉及中间件协作，复杂度高，收益仅 ~5.7s。

---

## 推荐实施路径

**方案 A（prompt 优化）** 是零风险、零代码改动的最优解：

1. 修改 `MAIN_AGENT_PROMPT.md` 的编排规则和 `write_todos` 示例
2. 在"交互原则"中增加合并规则
3. 回归测试几个典型查询，确认 LLM 不再分"展示"和"总结"

**方案 B** 可作为后续迭代，在方案 A 效果不理想时补充。

---

## 涉及文件

| 文件 | 改动 | 说明 |
|:---|:---|:---|
| `src/agent/prompt/MAIN_AGENT_PROMPT.md` | 修改编排规则 + write_todos 示例 + 新增合并原则 | 核心改动 |
| （可选）`src/agent/subagents/check_progress.py` | `suggested_action` 字段 | 方案 B 补充 |

---

## 与系统提示词精简的协同

本方案与"系统提示词精简方案"可以同时实施，互不冲突：

- **精简方案**：减少 prompt 的静态 token 开销（~950 tokens/轮）
- **合并方案**：减少 LLM 调用轮次（-1~2 轮/会话）

两者叠加效果：
- 优化前：4 轮 × 35k tokens 平均 = 140k tokens 总输入，~60s LLM 耗时
- 优化后：2-3 轮 × 15k tokens 平均（含结果摘要化）= 30-45k tokens 总输入，~30-35s LLM 耗时
- **节省 ~50% tokens + ~42% 时间**

---

## 风险与注意事项

1. **一次性输出过长**：合并后单轮 LLM 输出可能包含数据分析 + 图表推荐 + 结果展示 + 总结，completion tokens 会增加。但 DeepSeek 的 max_tokens 默认 8,192，足够覆盖。

2. **图表推荐逻辑可能跳过**：如果 LLM 在"分析结果"时判断数据不适合可视化，会直接跳过图表渲染。这是正确的行为，不是 bug。

3. **不能过度合并**：不要尝试把"委派子 agent"也合并进来——因为子 agent 是异步的，委派后必须等结果回来才能继续。合并只适用于"收到结果后"的后续步骤。

4. **prompt 措辞要精确**：不能说"永远只回复一次"，因为有些场景确实需要多轮（如用户追问）。只需说"收到子 agent 结果后，一次性完成展示和总结"。在 prompt 中明确"展示结果时一并总结"即可。措辞要精确，避免 LLM 误解为"永远只回复一次"。