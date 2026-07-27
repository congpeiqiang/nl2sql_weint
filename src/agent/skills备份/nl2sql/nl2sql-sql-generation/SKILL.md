---
name: nl2sql-sql-generation
description: "触发条件：NL2SQL流水线需要根据自然语言问题和程序化查询计划生成可执行的SQL查询；用户希望从结构化计划中合成最终的SQL查询。本技能是SQL-of-Thought流水线的第4步，在查询计划生成之后使用。它是流水线中唯一生成SQL的智能体。跳过条件：用户仅需要查询计划而不需要SQL；用户希望对已有SQL进行纠错；用户直接提供了SQL。"
---

# NL2SQL SQL生成智能体

## 概述

SQL生成智能体是SQL-of-Thought流水线的**第4步**。它接收自然语言问题和**程序化查询计划**，合成可执行的SQL查询。

这是**整个流水线中唯一生成SQL的智能体**。后处理会移除多余的产物以确保语法有效性。

## 在流水线中的角色

```
输入：  自然语言问题 (Q) + 程序化查询计划 (P) + 裁剪后的Schema (S)
输出：  可执行的SQL查询（经后处理）
下一步：→ 对数据库执行SQL → 成功（结束）或出错（→ nl2sql-correction）
```

## 执行流程

### 第1步：审阅查询计划

仔细阅读查询计划智能体生成的程序化计划。理解每个编号步骤及其背后的推理逻辑。计划是SQL合成的主要指南——而非仅凭原始问题。

### 第2步：将计划步骤映射为SQL结构

将每个程序化步骤转换为其对应的SQL结构：

| 计划模式 | SQL结构 |
|----------|---------|
| "从X表开始" | `FROM X` |
| "按条件与Y表连接" | `JOIN Y ON 条件` |
| "过滤行数据，条件为……" | `WHERE 条件` |
| "按……分组" | `GROUP BY 列名` |
| "过滤分组，条件为……" | `HAVING 条件` |
| "计算平均值/计数/求和……" | `SELECT` 中的 `AVG()/COUNT()/SUM()` |
| "按……降序排列" | `ORDER BY 列名 DESC` |
| "限制为前N条" | `LIMIT N` |
| "仅取不重复的……" | `DISTINCT` |
| "排除/移除……" | `NOT IN (子查询)` 或 `EXCEPT` |

### 第3步：按计划顺序组装SQL

按照计划编号顺序逐步构建SQL查询：

```
计划：
1. 通过 e.dept_id = d.id 连接 employees 与 departments
2. 按 d.dept_name 分组
3. 计算 AVG(e.salary) 和 COUNT(*)
4. 筛选 COUNT(*) > 10 的分组
5. 返回 dept_name 和 avg_salary

→ SQL构建过程：
SELECT d.dept_name, AVG(e.salary) AS avg_salary   -- 步骤5：SELECT列
FROM employees e                                     -- 步骤1a：FROM
JOIN departments d ON e.dept_id = d.id               -- 步骤1b：JOIN
GROUP BY d.dept_name                                 -- 步骤2：GROUP BY
HAVING COUNT(*) > 10                                 -- 步骤4：HAVING
```

### 第4步：后处理

应用以下后处理规则以确保SQL干净、有效：

| 产物类型 | 处理方式 |
|----------|---------|
| 尾部多余分号（`;`） | **移除** — 执行用SQL不应以分号结尾 |
| 输出中的自然语言片段 | **移除** — 剥离任何泄漏到SQL中的散句 |
| 行内注释（`-- ...`） | **移除** — 清理注释以获得可执行的纯净SQL |
| 多余空白/换行 | **规范化** — 折叠为单空格以提高可读性 |
| 小写表名/列名 | **保持原样** — 尊重Schema的命名规范 |

### 第5步：对照Schema验证

快速结构检查：
- 所有引用的表在裁剪后的Schema中均存在
- 所有引用的列在其所属表中均存在
- JOIN条件使用了有效的外键关系
- 聚合列要么出现在GROUP BY中，要么自身是聚合函数

## 输出模板

```json
{
  "sql_query": "SELECT d.dept_name, AVG(e.salary) AS avg_salary FROM employees e JOIN departments d ON e.dept_id = d.id GROUP BY d.dept_name HAVING COUNT(*) > 10",
  "plan_steps_covered": [1, 2, 3, 4, 5],
  "post_processing_applied": ["removed_trailing_semicolon", "normalized_whitespace"],
  "schema_validation": {
    "tables_valid": true,
    "columns_valid": true,
    "joins_valid": true
  }
}
```

## 生成规则

1. **温度 = 0**：始终使用确定性生成来产生SQL
2. **忠实遵循计划**：不要超出计划范围自行发挥
3. **使用SQLite兼容语法**：目标数据库为SQLite（依Spider基准测试），因此：
   - 不使用 `LIMIT n` 以外的 `LIMIT/OFFSET` 变体
   - 使用 `EXCEPT` 而非 `MINUS`
   - 字符串比较默认区分大小写
4. **别名保持一致**：使用简洁、一致的表别名（如 `e` 代表 `employees`，`d` 代表 `departments`）
5. **显式列引用**：涉及多表时始终用表别名限定列名（`e.salary`，而非仅 `salary`）
6. **不使用子句级硬编码规则**：不要为单个子句（JOIN、LIMIT等）添加特殊提示规则——这会膨胀上下文并分散模型注意力

## 常见SQL模式

### 简单筛选
```sql
SELECT T1.name FROM employees AS T1
```

### 连接 + 聚合 + HAVING
```sql
SELECT T1.dept_name, AVG(T2.salary) AS avg_salary
FROM departments AS T1
JOIN employees AS T2 ON T1.dept_id = T2.dept_id
GROUP BY T1.dept_name
HAVING COUNT(*) > 10
```

### 嵌套子查询
```sql
SELECT T1.name, T1.salary
FROM employees AS T1
WHERE T1.salary > (
    SELECT AVG(T2.salary)
    FROM employees AS T2
    WHERE T2.dept_id = T1.dept_id
)
```

### 集合操作
```sql
SELECT T1.product_name FROM products AS T1
WHERE T1.category = 'Electronics'
EXCEPT
SELECT T2.product_name FROM discontinued_products AS T2
```

## 常见陷阱

| 陷阱 | 纠正方式 |
|------|---------|
| 生成的SQL未遵循计划 | 计划是主要指南——始终将计划步骤映射为SQL |
| 添加尾部多余分号 | 在后处理中移除 |
| 多表查询中列名未加限定 | 始终使用表别名（T1.col, T2.col） |
| 使用聚合函数时遗漏GROUP BY | SELECT中的每个非聚合列必须出现在GROUP BY中 |
| 混淆HAVING和WHERE | WHERE = 行级过滤（分组前）；HAVING = 分组级过滤（分组后） |
| 使用非SQLite语法 | 坚持使用SQLite兼容语法 |
| 添加子句级硬编码规则 | 已证实会降低准确率——保持提示词通用 |
