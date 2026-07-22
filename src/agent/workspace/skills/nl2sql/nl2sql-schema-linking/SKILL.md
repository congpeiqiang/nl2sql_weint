---
name: nl2sql-schema-linking
description: "触发条件：NL2SQL流水线需要为自然语言问题识别相关的数据库表、列、主键、外键及连接关系；用户希望为查询提取Schema上下文；用户询问给定问题需要哪些表/列。本技能是SQL-of-Thought流水线的第1步，应在子问题分解和查询计划之前使用。跳过条件：用户已提供完整的Schema上下文；用户提出的SQL问题不需要Schema发现。"
---

# NL2SQL Schema关联智能体

## 概述

Schema关联智能体是SQL-of-Thought流水线的**第一步**。它结合自然语言问题和数据库Schema，解析并识别回答查询所需的表、列、主键、外键及连接关系。

这一步将SQL生成限制在与Schema相关的实体范围内，减少幻觉并确保下游阶段的结构正确性。

## 在流水线中的角色

```
输入：  自然语言问题 (Q) + 数据库标识符 (db_id)
输出：  裁剪后的Schema (S) — 相关表、列、主键、外键、连接关系
下一步：→ nl2sql-subproblem（第2步）
```

## 执行流程

### 第1步：通过 wren context show MCP 检索Schema知识

使用 wren context show-compiler MCP 工具发现相关Schema信息：

1. **`wren context show`** — 搜索与问题相关的表/列
   
   ```
   wren context show({ question: "哪些表包含客户订单信息及其支付状态？" })
```
   
2. **`wren context show`** — 对Schema关系提出有据可查的问题，并附带引用
   
   ```
   wren context show({ question: "哪个外键连接了订单表和客户表？", save: true })
```
   
4. **如果 MDL 尚未构建**，先执行 `python skills/sql-of-thought/scripts/gen_models_mysql.py` + `wren context build`（参见编排器阶段一）

### 第2步：Schema关联推理（思维链）

执行结构化推理，将问题与Schema实体关联起来：

```
给定问题："查询人数超过10人的部门中员工的平均薪资"

Schema关联推理：
1. 问题提到"员工" → 可能映射到 `employee` 表
2. "薪资" → 可能映射到 `employee` 表中的 `salary` 列
3. "部门" → 可能映射到 `department` 表
4. "人数超过10人" → 隐含需要计算每个部门的员工数量 → 需要 `dept_id` 列
5. 连接关系：`employee.dept_id` → `department.dept_id`（外键）

裁剪后的Schema输出：
- 表：employee、department
- 列：employee.salary、employee.dept_id、department.dept_name、department.dept_id
- 主键：department.dept_id
- 外键：employee.dept_id → departments.dept_id
- 连接方式：employee JOIN departments ON employee.dept_id = department.dept_id
```

### 第3步：格式化输出

裁剪后的Schema输出必须包括：
- **表名** — 仅包含与问题相关的表
- **列名** — 仅包含需要的列（而非表中的所有列）
- **主键** — 相关表的主键
- **外键** — 用于连接关系的外键
- **连接路径** — 相关表之间的连接方式

## 最佳实践

- **保守筛选**：仅包含直接相关的表/列。多余的实体会分散下游智能体的注意力。
- **追踪连接路径**：如果问题涉及多个表，通过外键明确识别连接路径。
- **处理歧义**：当一个术语可能映射到多个列时（如 "name" → first_name 还是 last_name），将两者都列为候选并标注歧义。
- **使用 MDL 模型名引用**：为每个识别出的表/列引用具体的 MDL 模型名以提供来源追溯。

## 常见陷阱

| 陷阱 | 纠正方式 |
|------|---------|
| 遗漏了JOIN所需的表 | 通过wren context show检查间接关系；跟踪外键链 |
| 包含了所有列而非仅相关列 | 仅包含明确提及或结构上必需的列 |
| 忽略了复合外键 | 通过wren context show读取完整的表实体页面以获取多列外键 |
| 将自然语言术语映射到错误的列 | 使用 `wren context show` 验证列的语义含义 |

## 输出模板

```json
{
  "tables": ["table1", "table2"],
  "columns": {
    "table1": ["col_a", "col_b", "col_id"],
    "table2": ["col_c", "col_d", "table1_id"]
  },
  "primary_keys": {
    "table1": "col_id",
    "table2": "col_d"
  },
  "foreign_keys": {
    "table2.table1_id": "table1.col_id"
  },
  "join_paths": [
    "table2 JOIN table1 ON table2.table1_id = table1.col_id"
  ],
  "reasoning": "逐步的Schema关联逻辑……"
}
```
