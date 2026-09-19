# 架构评估：主智能体剥离 NL2SQL 工具和技能

## 方案描述

| 组件 | 当前 | 优化后 |
|------|------|--------|
| 主智能体 tools | MCP全部工具 + task | **仅 task** |
| 主智能体 skills | main/ + nl2sql/ | **仅 main/** |
| 子智能体 tools | 继承主智能体 | 独立注入 MCP全部工具 |
| 子智能体 skills | 继承主智能体 | 独立加载 nl2sql skills |

## 优势分析

### 1. token 大幅节省
主智能体每次 LLM 调用都要把所有工具描述塞进 prompt。
当前工具列表约 35 个（MCP 18+17），去掉后只剩 task 工具。
估算每次 LLM 调用节省 2000-4000 token。

### 2. 职责更清晰
```
主智能体: 意图识别 → 委派 → 汇总            (纯协调者)
子智能体: Schema发现 → SQL生成 → 执行 → 纠错 (领域专家)
```
tool > 主智能体不会"越权"直接调 run_sql，所有查询必经子智能体。

### 3. 安全隔离
主智能体无法直接访问数据库，无法执行 SQL，所有数据操作必须通过子智能体。

### 4. 技能不冲突
当前主智能体和子智能体加载相同的 nl2sql 技能，可能造成指令重复或冲突。

## 劣势分析

### 1. 子智能体构建复杂度
不能再用 deepagents 内置 subagents 机制（内置 task 会自动继承主智能体 tools/skills）。
必须自定义 task 工具，手动构建子智能体 graph，注入专门的 tools + skills。

### 2. 多一个问题分发环节
如果后续增加第二个子智能体（如"图表专家"），也需要独立构建。

### 3. 调试复杂度
两个独立的 agent graph，排查问题时需要分别追踪。

## 实现工作量

| 步骤 | 难度 | 说明 |
|------|:--:|------|
| main_agent.py 去工具 | 低 | tools=[task_tool]，subagents=[] |
| SkillsMiddleware 去 nl2sql | 低 | sources=["/skills/main/"] |
| task_with_trace.py 增强 | 中 | build_subagent_graphs 中给子智能体加 SkillsMiddleware + MCP tools |
| SYSTEM_PROMPT 调整 | 低 | 主智能体不提 WrenAI 工具名 |

## 结论

| 维度 | 评价 |
|------|------|
| 可行性 | 完全可行 |
| 优先级 | 中（不是阻塞问题）|
| 收益 | 每次对话节省 2000-4000 token |
| 风险 | 子智能体必须通过自定义 task 工具构建 |
| 建议 | 实施。当前 task_with_trace.py 已有子智能体独立构建能力，只是缺少 SkillsMiddleware 注入 |

## 当前项目适合度

当前 nl2sql 是唯一子智能体，去耦带来的复杂度不高。如果后续子智能体增多（图表、报告...），去耦收益更大。
