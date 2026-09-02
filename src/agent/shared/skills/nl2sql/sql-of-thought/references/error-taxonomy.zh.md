# SQL-of-Thought 错误分类法

## 概述

本分类法源自 Shen 等人的研究，并由 SQL-of-Thought 框架扩展。它提供了 **9 个类别**和 **31 个子类**的逻辑错误，供 LLM 识别和纠正。

**关键设计原则**：使用**简洁的错误编码**代替冗长的解释，以便于识别并防止溢出 LLM 上下文窗口。

## 类别 1：语法错误（`syntax`）

| 子类 | 编码 | 描述 |
|------|------|------|
| SQL 语法错误 | `syntax.sql_syntax_error` | 一般性 SQL 语法违规（格式错误的查询、缺少关键字） |
| 无效别名 | `syntax.invalid_alias` | 表/列别名使用不正确或冲突 |

**诊断提示**：检查是否缺少关键字、括号不匹配、逗号位置错误以及别名冲突。

## 类别 2：Schema 链接（`schema_link`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 缺少表 | `schema_link.table_missing` | 查询中未包含所需的表 |
| 缺少列 | `schema_link.col_missing` | 查询中未引用所需的列 |
| 列名歧义 | `schema_link.ambiguous_col` | 列名存在于多个表中且未加限定 |
| 外键错误 | `schema_link.incorrect_foreign_key` | JOIN 条件中使用了错误的外键列 |

**诊断提示**：对照链接后的 Schema 交叉检查每个引用的表/列。验证外键关系与数据库定义一致。

## 类别 3：JOIN 错误（`join`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 缺少 JOIN | `join.join_missing` | 遗漏了必需的 JOIN 子句（查询引用了多个表） |
| JOIN 类型错误 | `join.join_wrong_type` | 应使用 LEFT/RIGHT JOIN 却用了 INNER JOIN，或反之 |
| 多余的表 | `join.extra_table` | 查询中包含了不必要的表 |
| JOIN 列错误 | `join.incorrect_col` | JOIN ON 条件中使用了错误的列 |

**诊断提示**：验证所有多表查询都有正确的 JOIN。检查是否需要 LEFT/RIGHT JOIN 来处理可能为 NULL 的关系。移除对结果无贡献的表。

## 类别 4：过滤错误（`filter`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 缺少 WHERE | `filter.where_missing` | 遗漏了必需的 WHERE 子句（返回了所有行而非过滤后的子集） |
| 条件列错误 | `filter.condition_wrong_col` | 过滤条件应用到了错误的列上（例如，按 ID 过滤而非按名称过滤） |
| 条件类型不匹配 | `filter.condition_type_mismatch` | 比较中的类型不匹配（例如，字符串与整数比较，日期格式不匹配） |

**诊断提示**：验证 WHERE 条件与问题的过滤意图一致。检查比较中使用的列的数据类型。

## 类别 5：聚合错误（`aggregation`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 聚合无 GROUP BY | `aggregation.agg_no_groupby` | 使用了聚合函数但没有 GROUP BY（返回单行而非每组一行） |
| 缺少 GROUP BY 列 | `aggregation.groupby_missing_col` | GROUP BY 子句缺少必需的分组列 |
| HAVING 无 GROUP BY | `aggregation.having_without_groupby` | 存在 HAVING 子句但缺少 GROUP BY |
| HAVING 条件错误 | `aggregation.having_incorrect` | HAVING 条件逻辑错误（阈值错误、聚合函数错误） |
| HAVING 与 WHERE 混淆 | `aggregation.having_vs_where` | 聚合后过滤放在了 WHERE 而非 HAVING 中，或反之 |

**诊断提示**：每个聚合函数都需要 GROUP BY（除非计算单个全局聚合值）。WHERE 在分组前过滤行；HAVING 在分组后过滤组。SELECT 中的非聚合列必须出现在 GROUP BY 中。

## 类别 6：值错误（`value`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 硬编码值 | `value.hardcoded_value` | 在应使用列引用或参数的地方使用了字面值 |
| 值格式错误 | `value.value_format_wrong` | 日期、字符串或数值格式不正确（例如，'2024-01-01' 与 '01/01/2024'） |

**诊断提示**：检查字面值是否与预期的数据格式匹配。当问题暗示动态比较时，优先使用列引用而非硬编码值。

## 类别 7：子查询错误（`subquery`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 未使用的子查询 | `subquery.unused_subquery` | 子查询结果在外层查询中未被引用 |
| 缺少子查询 | `subquery.subquery_missing` | 未实现所需的嵌套查询（问题暗示需要与计算值进行比较） |
| 子查询关联错误 | `subquery.subquery_correlation_error` | 关联子查询引用了错误的外层列，或缺少正确的关联条件 |

**诊断提示**：类似"高于平均值"或"大于最大值"的问题通常需要子查询。验证关联子查询引用了正确的外层表列。

## 类别 8：集合操作（`set_ops`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 缺少 UNION | `set_ops.union_missing` | 未包含所需的 UNION 操作（问题暗示合并多个结果集） |
| 缺少 INTERSECT | `set_ops.intersect_missing` | 未包含所需的 INTERSECT 操作（问题暗示查找共同元素） |
| 缺少 EXCEPT | `set_ops.except_missing` | 未包含所需的 EXCEPT 操作（问题暗示排除某些结果） |

**诊断提示**：提到"两者"、"共同"、"也在"、"但不在"、"排除"的问题通常需要集合操作。验证操作数之间的列兼容性（列的数量和类型相同）。

## 类别 9：其他问题（`other`）

| 子类 | 编码 | 描述 |
|------|------|------|
| 缺少 ORDER BY | `other.order_by_missing` | 未应用所需的排序（问题暗示排序，如"top"、"highest"、"lowest"） |
| 缺少 LIMIT | `other.limit_missing` | 遗漏了所需的行数限制（问题暗示"前 N 个"） |
| 重复 SELECT | `other.duplicate_select` | 同一列被多次选择 |
| 不支持的函数 | `other.unsupported_function` | 函数在当前数据库中不可用 |
| 选择了多余的值 | `other.extra_values_selected` | SELECT 中的列数超过了问题所需 |

**诊断提示**："前 5 个"、"最高"、"最低"暗示需要 ORDER BY + LIMIT。检查是否有冗余的列选择。验证所有函数与目标数据库兼容。

## 在纠错循环中的使用

诊断失败的 SQL 查询时：

1. **按顺序扫描每个类别**：syntax → schema_link → join → filter → aggregation → value → subquery → set_ops → other
2. **分配简洁的错误编码**（例如 `aggregation.agg_no_groupby`）而非冗长的描述
3. **识别根本原因**——导致失败的主要错误
4. **记录次要错误**——可能加剧根本原因的其他问题
5. **使用识别出的错误编码作为结构化指导，构建 CoT 纠错计划**

## 诊断示例

```
失败的 SQL：SELECT name AVG(salary) FROM employees WHERE dept_id > 5

诊断：
- syntax.sql_syntax_error：SELECT 列之间缺少逗号
- aggregation.agg_no_groupby：AVG() 没有 GROUP BY
- schema_link.incorrect_foreign_key：dept_id 被用作数值过滤而非 JOIN 键
- filter.condition_type_mismatch：dept_id 与数字 5 比较（应引用部门表）

主要根本原因：aggregation.agg_no_groupby（影响最大）
次要错误：schema_link.incorrect_foreign_key, syntax.sql_syntax_error
```
