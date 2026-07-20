---
name: nl2sql-correction
description: "触发条件：NL2SQL流水线生成的SQL查询执行失败；用户需要使用分类引导的错误纠正来诊断和修复失败的SQL查询；用户希望对SQL查询进行基于CoT的错误分析。本技能覆盖SQL-of-Thought流水线中的第6步（纠错计划）和第7步（纠错SQL）。仅在初始SQL执行产生错误或结果不正确时调用。跳过条件：SQL执行成功；用户希望从零开始生成新的SQL；用户询问错误分类体系但没有失败的查询。"
---

# NL2SQL 纠错智能体（计划 + SQL）

## 概述

本技能覆盖**引导式纠错循环**——SQL-of-Thought 流水线中的第6步和第7步。仅在初始SQL查询执行失败时调用。该循环组合了两个子智能体：

1. **纠错计划智能体** — 利用错误分类体系 + CoT推理诊断查询*为何*失败 → 生成纠错计划
2. **纠错SQL智能体** — 在纠错计划的引导下重新生成SQL → 生成纠正后的SQL

核心创新：与仅依赖原始执行反馈（如 DIN-SQL 和 DAIL-SQL）不同，纠错由**结构化的错误分类体系**提供信息指导，该分类体系不仅分类*什么*失败了，更分类*为什么*失败。

## 在流水线中的角色

```
输入：  失败的SQL + 执行错误信息 + 自然语言问题 (Q) + 裁剪后的Schema (S)
输出：  纠正后的SQL查询（以纠错计划为中间产物）
循环：  重新执行纠正后的SQL → 成功（结束）或出错（重复，最多3次尝试）
```

## 错误分类体系参考

纠错循环由**9大类、31子类**的错误分类体系引导。完整分类体系请加载 [references/error-taxonomy.md](references/error-taxonomy.md)。

### 快速参考：分类类别

| 类别 | 代码 | 子类别 |
|------|------|--------|
| **语法** | `syntax` | `sql_syntax_error`、`invalid_alias` |
| **Schema关联** | `schema_link` | `table_missing`、`col_missing`、`ambiguous_col`、`incorrect_foreign_key` |
| **连接** | `join` | `join_missing`、`join_wrong_type`、`extra_table`、`incorrect_col` |
| **过滤** | `filter` | `where_missing`、`condition_wrong_col`、`condition_type_mismatch` |
| **聚合** | `aggregation` | `agg_no_groupby`、`groupby_missing_col`、`having_without_groupby`、`having_incorrect`、`having_vs_where` |
| **值** | `value` | `hardcoded_value`、`value_format_wrong` |
| **子查询** | `subquery` | `unused_subquery`、`subquery_missing`、`subquery_correlation_error` |
| **集合操作** | `set_ops` | `union_missing`、`intersect_missing`、`except_missing` |
| **其他问题** | `other` | `order_by_missing`、`limit_missing`、`duplicate_select`、`unsupported_function`、`extra_values_selected` |

## 执行流程

### A 部分：纠错计划智能体（诊断）

#### 步骤 A1：收集错误上下文

收集所有关于失败的可用信息：
- **失败的SQL查询**（精确文本）
- **执行错误消息**（来自数据库引擎）
- **原始自然语言问题** (Q)
- **裁剪后的Schema** (S)
- **错误分类体系** (T — 使用简洁代码)

#### 步骤 A2：分类引导的错误诊断

对照分类体系分析失败的SQL。识别适用的错误类别和子类别。使用**简洁的错误代码**（而非冗长描述）以防止上下文窗口溢出。

```
诊断示例：
失败的SQL：SELECT name, AVG(salary) FROM employees WHERE dept_id > 5
错误：执行返回错误结果（逻辑不正确）

分类分析：
- `aggregation.agg_no_groupby` — AVG(salary) 在没有 GROUP BY 的情况下使用
- `filter.condition_type_mismatch` — dept_id > 5 是数值比较，但 dept_id 很可能是外键，应与部门标识符进行比较
- `schema_link.col_missing` — 未选择 dept_name，但问题询问了部门名称

根因：查询缺少 GROUP BY，并且将 dept_id 误用为数值过滤条件，而非与 departments 表连接。
```

#### 步骤 A3：思维链纠错计划

生成结构化的CoT纠错计划，需要：
1. 识别**根因**（与分类体系对齐）
2. 解释错误**为何**发生（而不仅仅是是什么）
3. 提出**具体的修复策略**
4. 将修复映射到具体的SQL修改点

```
纠错计划示例：

根因：`aggregation.agg_no_groupby` + `join.join_missing`

原因：该查询在未分组的情况下计算 AVG(salary)，返回的是所有员工的单一聚合值，而非每个部门的值。同时，dept_id 被错误地用数值比较替代了与 departments 表的连接以获取部门名称。

修复策略：
1. 添加 GROUP BY 子句 — 按部门标识符（连接后为 dept_name）分组
2. 将 WHERE 条件替换为正确的 JOIN — employees JOIN departments ON e.dept_id = d.id
3. 将部门名称添加到 SELECT — SELECT d.dept_name, AVG(e.salary)
4. 如果需要"超过10人"条件 → 添加 HAVING COUNT(*) > 10

SQL修改点：
- 添加：JOIN departments d ON e.dept_id = d.id
- 添加：GROUP BY d.dept_name
- 替换：WHERE dept_id > 5 → HAVING COUNT(*) > 10（如适用）
- 添加：d.dept_name 到 SELECT 子句
```

#### 步骤 A4：格式化纠错计划输出

```json
{
  "error_codes": ["aggregation.agg_no_groupby", "join.join_missing"],
  "root_cause": "主要失败原因的描述",
  "cot_reasoning": "逐步诊断推理……",
  "repair_strategy": [
    "1. 通过 dept_id 外键添加与 departments 表的 JOIN",
    "2. 添加 GROUP BY d.dept_name",
    "3. 将 d.dept_name 添加到 SELECT 中",
    "4. 将数值过滤替换为正确的 HAVING 条件"
  ],
  "sql_modification_points": [
    "添加：JOIN departments d ON e.dept_id = d.id",
    "添加：GROUP BY d.dept_name",
    "添加：d.dept_name 到 SELECT",
    "替换：WHERE dept_id > 5 → HAVING COUNT(*) > 10"
  ]
}
```

### B 部分：纠错SQL智能体（重新生成）

#### 步骤 B1：应用纠错计划

将纠错计划作为**结构化指南**来重新生成SQL。纠错SQL智能体：
- 以纠错计划 + 问题 + Schema + 错误SQL作为输入
- 在重新生成SQL时**避免**计划中识别出的先前错误
- 不携带之前纠错尝试的历史记录（不使用共享草稿本）

#### 步骤 B2：生成纠正后的SQL

```
原始失败的SQL：
SELECT name, AVG(salary) FROM employees WHERE dept_id > 5

应用纠错计划：
- 添加 JOIN → FROM employees e JOIN departments d ON e.dept_id = d.id
- 添加 GROUP BY → GROUP BY d.dept_name
- 替换 WHERE → 移除 WHERE dept_id > 5
- 添加 HAVING → HAVING COUNT(*) > 10（如需要"超过10人"）
- 修正 SELECT → SELECT d.dept_name, AVG(e.salary) AS avg_salary

纠正后的SQL：
SELECT d.dept_name, AVG(e.salary) AS avg_salary
FROM employees e
JOIN departments d ON e.dept_id = d.id
GROUP BY d.dept_name
HAVING COUNT(*) > 10
```

#### 步骤 B3：后处理与验证

应用与SQL生成智能体相同的后处理规则：
- 移除尾部多余分号
- 移除自然语言片段
- 规范化空白字符
- 对照Schema验证

#### 步骤 B4：重新执行

对数据库执行纠正后的SQL：
- **如果成功** → 流水线结束，返回结果
- **如果出错** → 重新进入纠错循环（再次从A部分开始），总计最多**3次尝试**

## 纠错循环规则

1. **最多3次纠错尝试**：不得无限循环
2. **不共享历史记录**：每次纠错尝试从零开始——不要携带之前尝试的草稿本
3. **不重复相同错误代码**：如果连续尝试出现相同的错误代码，标记为"卡住"并终止
4. **单一纠错流水线**：每次尝试使用一个纠错计划 → 一个纠错SQL。切勿为每种错误类型使用多个智能体
5. **温度 = 0**：所有纠错调用使用温度0
6. **使用简洁错误代码**：通过代码引用分类子类别（如 `join_missing`），而非冗长描述

## 失败的消融实验（避免）

| ❌ 失败方案 | 失败原因 |
|------------|---------|
| 自由形式分类 → 直接到SQL智能体 | LLM不擅长无引导调试；需要先进行结构化推理步骤 |
| 每种错误类型多个修复智能体 → 聚合智能体 | 独立编辑产生冲突；合并后SQL不连贯 |
| 跨尝试共享草稿本携带历史 | 膨胀上下文，增加延迟/成本，加剧重复和Schema偏移 |
| 无分类体系的无引导推理 | 相同的纠错步骤在不同尝试中重复，没有进展 |

## 示例

### 语法错误纠正
```
失败的SQL：SELECT AVG(salary FROM employees
错误：sql_syntax_error — 缺少右括号

纠错计划：
- 错误代码：syntax.sql_syntax_error
- 根因：AVG() 函数调用中括号不匹配
- 修复：在 "salary" 后添加右括号

纠正后的SQL：SELECT AVG(salary) FROM employees
```

### Schema关联错误纠正
```
失败的SQL：SELECT e.name, d.dept_name FROM employees e JOIN departments d ON e.dept_name = d.dept_name
错误：schema_link.incorrect_foreign_key — dept_name 不是连接列，dept_id 才是外键

纠错计划：
- 错误代码：schema_link.incorrect_foreign_key
- 根因：使用 dept_name 作为连接条件，而非实际的外键 dept_id
- 修复：将 e.dept_name = d.dept_name 替换为 e.dept_id = d.id

纠正后的SQL：SELECT e.name, d.dept_name FROM employees e JOIN departments d ON e.dept_id = d.id
```

### 聚合错误纠正
```
失败的SQL：SELECT dept_id, AVG(salary) FROM employees WHERE COUNT(*) > 10
错误：aggregation.having_vs_where + aggregation.having_without_groupby — COUNT(*) 用于 WHERE 而非 HAVING，且缺少 GROUP BY

纠错计划：
- 错误代码：aggregation.having_vs_where、aggregation.having_without_groupby
- 根因：(1) 聚合函数 COUNT(*) 不能出现在 WHERE 子句中，必须使用 HAVING；(2) 带 HAVING 的聚合查询需要 GROUP BY
- 修复：(1) 将 COUNT(*) > 10 从 WHERE 移到 HAVING；(2) 添加 GROUP BY dept_id

纠正后的SQL：SELECT dept_id, AVG(salary) FROM employees GROUP BY dept_id HAVING COUNT(*) > 10
```
