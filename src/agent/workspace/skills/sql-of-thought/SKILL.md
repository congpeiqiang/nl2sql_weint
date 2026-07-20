---
name: sql-of-thought
description: "触发条件：用户提出需要从数据库中查询数据的问题；用户希望将自然语言转换为SQL；用户提到NL2SQL、Text-to-SQL或自然语言数据库查询；用户需要使用自然语言问题来分析、查询或提取数据库中的数据。跳过条件：用户直接编写SQL而不需要自然语言输入；用户要求执行数据库管理任务（备份、迁移等）；用户询问NoSQL或非SQL数据库操作。"
---

# SQL-of-Thought：智能体NL2SQL编排器

## 概述

本技能编排 **SQL-of-Thought** 智能体流水线，用于将自然语言问题转换为可执行的SQL查询。该框架将NL2SQL任务分解为专门化的智能体阶段，按照顺序流水线执行，并包含一个条件性的分类引导纠错循环。

基于研究论文 *"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"*（Chaturvedi, Chadha, Bindschaedler, 2025），在Spider基准测试上达到 **91.59%的执行准确率**。

## 使用场景

- 用户提出需要数据库查询的自然语言问题
- 用户希望将业务问题转换为SQL
- 用户需要用通俗语言从数据库中提取/分析数据
- 任何需要NL → SQL转换的场景

## 架构：Y = LLM(Q, S, C, P, T | θ)

其中：
- **Q** = 自然语言问题
- **S** = 关联的Schema（相关表/列）
- **C** = 子句级子问题（JSON键值对）
- **P** = 查询计划（程序化的、逐步的）
- **T** = 错误分类体系（用于CoT引导的错误纠正）
- **θ** = LLM参数

## 流水线工作流程

### 阶段零：WrenAI 语义层准备

**决策逻辑：** 执行以下检查，满足任一条件则加载 `load_skill("wrenai")`：

| 条件 | 检查方式 | 动作 |
|------|---------|------|
| 首次使用该数据库 | `wren profile list` 无匹配 profile | 执行完整 Phase 0：`generate-mdl` → `enrich-context` |
| Schema 已变更但 MDL 未更新 | `wren context validate` 报错 | 重新 `generate-mdl` |
| 用户明确要求 | 用户提到"语义层""MDL""业务上下文" | 按需加载对应 workflow |

> 如果 MDL 已存在且有效，跳过此阶段。Phase 0 和 Phase 1 独立并行——MDL 建立业务语义，llmwiki 建立 Schema 结构文档。

### 阶段一：Schema知识准备（流水线前置）

在主流水线执行之前，通过 **llm-wiki-compiler** MCP服务器确保数据库Schema知识可用：

1. **导入Schema文档** — 使用 `ingest_source` 加载数据库Schema文档、ER图、数据字典
2. **编译Wiki** — 使用 `compile_wiki` 提取表和关系的结构化实体/概念页面
3. **验证状态** — 使用 `wiki_status` 确认所有Schema实体已编译且没有遗漏

> 如果Schema Wiki已经编译完成，跳过此阶段。

### 阶段二：顺序流水线（主流程）

严格按照以下顺序执行，根据需要加载每个智能体技能：

```
步骤1 → load_skill("nl2sql-schema-linking")
         Schema关联智能体：自然语言问题 → 裁剪后的Schema
         使用 llmwiki MCP：search_pages、query_wiki 进行表/列发现

步骤2 → load_skill("nl2sql-subproblem")
         子问题智能体：问题 + Schema → JSON子问题
         （WHERE、GROUP BY、JOIN、DISTINCT、ORDER BY、HAVING、EXCEPT、LIMIT、UNION）

步骤3 → load_skill("nl2sql-query-plan")
         查询计划智能体：问题 + Schema + 子问题 → 程序化计划（不含SQL）
         需要思维链（Chain-of-Thought）推理

步骤4 → load_skill("nl2sql-sql-generation")
         SQL生成智能体：问题 + 查询计划 → 可执行SQL
         后处理：去除尾部多余分号、自然语言片段

步骤4.5 → load_skill("nl2sql-sql-validate")
         SQL校验智能体：语法(wren dry-plan) + 安全性 + 性能校验
         通过 → 继续；阻断 → 返回步骤4修正；警告 → 记录后继续

步骤5（可选）→ WrenAI 干运行验证

         决策逻辑：
         - 如果 MDL 项目存在（Phase 0 已完成）→ load_skill("wrenai")，执行 `wren dry-plan --sql '...'` 验证
         - 如果 MDL 不存在 → 跳过，直接进入步骤6

         验证通过 → 进入步骤6
         验证失败 → 根据 `dry-plan` 返回的语义错误修正 SQL，重新验证（最多2次），仍失败则标记后进入步骤6

步骤6 → 对数据库执行SQL
         如果成功 → 返回结果，流水线结束
         如果出错 → 进入阶段三
```

### 阶段三：引导式纠错循环（条件执行）

仅在SQL执行失败时调用。循环执行直到成功或达到最大尝试次数：

```
步骤7 → 如果 WrenAI MDL 可用，先执行 `wren memory recall --question "<原始NL问题>"`
         检索相似历史正确 SQL 作为参考（不替代纠错推理，仅辅助）

步骤8 → load_skill("nl2sql-correction")
         纠错计划智能体：失败的SQL + 错误信息 + 分类体系 + memory recall 结果 → CoT纠错计划
         纠错SQL智能体：纠错计划 → 重新生成的SQL

步骤9 → 重新执行纠正后的SQL
         如果成功 → 返回结果
         如果出错 → 重复步骤8（最多3次尝试）
```

## 核心设计原则

1. **分阶段推理是强制要求**：始终在生成SQL之前生成查询计划——不可跳过
2. **查询计划智能体不得生成SQL**：它只生成程序化计划
3. **温度 = 0**：所有LLM调用使用温度0以获得确定性、可靠的输出
4. **纠错尝试之间不共享历史**：每次纠错从零开始——不使用草稿本
5. **单一纠错流水线**：切勿为每种错误类型使用多个智能体（会导致冲突编辑）
6. **简洁的错误代码优于冗长描述**：使用分类代码（如 `join_missing`）而非长文本
7. **引导式纠错优于无引导纠错**：分类体系 + CoT > 仅凭原始执行反馈

## 失败的消融实验（避免以下模式）

- ❌ 自由格式分类体系 → 直接到SQL智能体（LLM不擅长无引导调试）
- ❌ 温度 > 0（降低计划忠实度，更多无效连接）
- ❌ JOIN/LIMIT的子句特定提示规则（膨胀上下文，分散模型注意力）
- ❌ 每种错误类型使用多个修复智能体 → 聚合（编辑冲突、SQL不连贯）
- ❌ 携带历史的共享草稿本（Schema偏移、重复、成本增加）

## 混合模型策略（成本优化）

| 智能体类型 | 推荐模型级别 | 原因 |
|------------|-------------|------|
| Schema关联 | 推理模型（如 Claude Opus、GPT-5） | 需要深度Schema理解 |
| 查询计划 | 推理模型 | 需要CoT推理生成计划 |
| 纠错计划 | 推理模型 | 需要分类引导的诊断推理 |
| 子问题分解 | 非推理模型（如 GPT-4o） | 结构化JSON分解，推理需求较低 |
| SQL生成 | 非推理模型 | 计划到SQL的合成，遵循明确指令 |
| 纠错SQL | 非推理模型 | 纠错计划已经是结构化指导 |

> 此混合方案可降低约30%成本，同时保持约85%的执行准确率。

## MCP集成：llm-wiki-compiler

llmwiki MCP服务器提供Schema知识检索：

| MCP工具 | 流水线阶段 | 用途 |
|---------|-----------|------|
| `ingest_source` | 阶段一 | 加载Schema文档、ER图、数据字典 |
| `compile_wiki` | 阶段一 | 从导入的Schema源构建结构化Wiki |
| `search_pages` | 阶段二（步骤1） | 为自然语言问题找到相关表/列 |
| `query_wiki` | 阶段二（步骤1-2） | 对Schema关系提出有依据的问题 |
| `wren dry-plan` | 步骤4.5 | SQL语法和Schema一致性校验 |
| `wiki_status` | 阶段一 | 检查编译状态和完整性 |
| `lint_wiki` | 阶段一 | 验证Schema文档质量 |

### WrenAI 工具（可选增强）

| 工具 | 流水线阶段 | 用途 | 触发条件 |
|------|-----------|------|---------|
| `wren generate-mdl` | 阶段零 | 从数据库生成MDL语义模型 | 首次使用或Schema变更 |
| `wren enrich-context` | 阶段零 | 添加业务上下文 | MDL生成后 |
| `wren dry-plan` | 步骤5 | 干运行验证SQL语义 | MDL已存在 |
| `wren memory recall` | 步骤7 | 检索相似历史正确查询 | 进入纠错循环且MDL可用 |

## 输出格式

最终输出应包含：
- **生成的SQL查询**（已后处理，无尾部多余分号）
- **执行结果**（如果数据库可用）
- **流水线追踪**（执行了哪些步骤，是否应用了纠错）
- **错误诊断**（如果需要纠错，识别了哪些分类类别）

## 快速入门示例

```
用户："Find the average salary of employees in departments with more than 10 people"
（查询人数超过10人的部门中员工的平均薪资）

→ 阶段零：检查MDL是否存在 → 不存在则 `wren generate-mdl`（约30秒）
→ 阶段一：确保HR数据库的Schema Wiki已编译
→ 步骤1：Schema关联 → 识别 `employees` 和 `departments` 表、`salary` 列、`dept_id` 外键
→ 步骤2：子问题分解 → {"GROUP BY": "department", "HAVING": "COUNT(*) > 10", "SELECT": "AVG(salary)"}
→ 步骤3：查询计划 → "1. 通过dept_id连接employees和departments表。2. 按部门分组。3. 筛选人数>10的组。4. 计算每个符合条件的组的平均薪资。"
→ 步骤4：SQL生成 → SELECT d.dept_name, AVG(e.salary) FROM employees e JOIN departments d ON e.dept_id = d.dept_id GROUP BY d.dept_name HAVING COUNT(*) > 10
→ 步骤5：执行 → 成功 ✓
```

## 参考资料

- [错误分类体系](references/error-taxonomy.md) — 完整的9大类、31子类分类体系
- [流水线流程](references/pipeline-flow.md) — 详细的流程图及决策逻辑
- [设计原则](references/design-principles.md) — 核心原则与失败的消融实验教训
- [混合模型策略](references/hybrid-model-strategy.md) — 成本-性能优化指南
