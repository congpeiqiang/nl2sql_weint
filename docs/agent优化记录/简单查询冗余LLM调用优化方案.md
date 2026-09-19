# 简单查询冗余 LLM 调用优化方案

**日期**：2026-08-19
**问题来源**：LangSmith trace 分析（`01a018d5-c13f-7de0-9f3f-b1dd92da3d81`）

---

## 一、问题描述

对于"2020 年之后（含 2020）共有多少部电影？" 这类**策略 B（快速通道）**的简单查询，子智能体在第一次 LLM 输出中已经识别出是简单问题，但仍被迫执行了 3 个不必要的操作：

| 不必要的操作 | 原因 | 实际耗时 |
|-------------|------|---------|
| `write_todos` 初始化 5 步进度 | 原则 9 强制要求"无论多简单" | +1 次 LLM 轮次 |
| `read_file(sql-of-thought/SKILL.md)` | 想"了解流程"，但系统提示词已包含完整流程 | +1 次 LLM 轮次 |
| `read_file(nl2sql-clarification/SKILL.md)` | 同上 | +1 次 LLM 轮次 |

这些冗余操作触发了额外的 LLM 轮次，在 qwen3.7-max 不稳定时直接导致 Knowledge Loader 阶段耗时 4 分 15 秒，最终以 `APIConnectionError` 失败。

## 二、根因分析

### 2.1 强制 write_todos 的链路

```
NL2SQL_SYSTEM_PROMPT.md 原则 9
  → "write_todos 是本流水线的硬性要求，无论问题看起来多简单都必须调用"
  ↓
WriteTodosProtocolMiddleware
  → 追加协议文本到 system prompt 末尾，压过 TodoListMiddleware 默认"可跳过"
  ↓
LLM 无论简单/复杂都必须先执行 write_todos
```

### 2.2 读取 SKILL.md 的根因

提示词中 `"使用 load_skill(name) 按需加载"` 的表述，暗示 LLM 需要先"了解有什么技能"，导致它去 `read_file(SKILL.md)`。实际上：
- 系统提示词已包含完整的三阶段工作流 + 所有技能名称
- `SkillsMiddleware` 已自动注入 `load_skill` 工具

### 2.3 前端进度条依赖

```
优先级1: 子线程 write_todos 结果（polledSubagentTodos）
   ↓ 为空时
优先级2: deriveStepsFromSubMessages（从工具调用推导）
   ↓ 合并增强
优先级3: keyed subagent_steps_map（sync 线程写入，含耗时）
```

`deriveStepsFromSubMessages` 已能从工具调用序列推导出人类可读步骤名，覆盖所有 NL2SQL 工具。**前端已有完整兜底，不会报错。**

## 三、优化方案

### 3.1 提示词：按策略分级的 write_todos 要求

**文件**：[NL2SQL_SYSTEM_PROMPT.md](../../src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md) 原则 9

改为：

```markdown
### 原则 9：进度追踪（按策略分级）

**策略 A（标准流水线）/ C（Cube 通道）**：步骤多（7-8 步），必须在开工前用 write_todos
创建进度列表，每步完成时更新。

**策略 B（快速通道，单表/简单筛选/计数）**：步骤少（≤3 步），**可跳过 write_todos**。
系统会自动从工具调用序列推导步骤，无需手动维护进度。

**禁止为"了解流程"而读取 SKILL.md**：系统提示词已包含完整的三阶段工作流、
所有技能名称和加载时机。直接开始执行 Step 0（澄清），不要先 read_file 读取 SKILL.md。
```

### 3.2 WriteTodosProtocolMiddleware：降低强硬程度

**文件**：[write_todos.py](../../src/agent/middlewares/write_todos.py)

不再压过默认指引，而是与之兼容：

```python
WRITE_TODOS_PROTOCOL = """\
## `write_todos` 使用指引

在 NL2SQL 流水线中，`write_todos` 的使用遵循以下规则：

1. **策略 A/C（复杂查询）**：开工前先初始化完整步骤列表，每步完成立即更新。
2. **策略 B（快速通道）**：可跳过——系统会自动从工具调用序列推导进度。
3. **禁止为"了解流程"而读取 SKILL.md**：系统提示词已包含完整流程，直接开始执行。
"""
```

### 3.3 提示词：移除对 SKILL.md 读取的引导

**文件**：[NL2SQL_SYSTEM_PROMPT.md](../../src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md) 第四节

改为：

```markdown
## 四、技能加载策略

系统已自动注入所有技能的 `load_skill` 工具。直接按流水线步骤执行，
每步只加载当前需要的技能，不要提前加载后续步骤。**禁止为"了解流程"而
先 read_file 读取 SKILL.md**——系统提示词已包含完整流程。
```

### 3.4 前端：无需修改

`deriveStepsFromSubMessages` 已有完整兜底，覆盖所有 NL2SQL 工具调用。

## 四、预期效果

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 简单查询 LLM 轮次 | 8-10 次 | 4-6 次 |
| 首 token 延迟 | 额外 +2 轮 tool call | 消除 |
| 前端进度条 | write_todos 驱动 | write_todos + 兜底推导 |
| 复杂查询（策略 A/C） | 不变 | 不变 |

## 五、风险

- **write_todos 不调用时**：前端进度条步骤名从工具调用推导，可能不如手写的 write_todos 语义精确，
  但功能不受影响，不会报错。
- **极简查询**（如 `SELECT COUNT(*) FROM titles` 只有 1 步）：`deriveStepsFromSubMessages` 仍能推导出 "执行 SQL 查询" 步骤。