---
version: 0.1.0
name: wren-sql-author
description: "触发：非指标路径或混合路径。基于取料包（结构+范例+规则+知识）手写项目方言 SQL；混合场景以 query_cube 编译结果为子查询/CTE 底座外层补维度。dry_run 失败即改，≤3 次。只生成 SELECT 只读查询。"
---

# SQL 手写 / 混合生成

## 执行步骤

1. **方言**：首查 `get_data_source()` 取项目方言（后续同一会话复用，不重复调）。
2. **范例优先**：`exemplars` 中存在**表/字段与当前 Schema 命中一致**的历史 SQL → 以其为骨架改写（官方 exemplar 轴的价值所在）；仅语义像但表字段对不上的范例**不采纳**。
3. **规则/陷阱注入**：`rules` 的口径过滤（如"剔除测试账号"）与 `caveats` 的已知陷阱逐条对照落地。
4. **混合底座**：输入含 metric-query 编译 SQL 时，将其作为 CTE/子查询，外层只补 Cube 未定义的维度/过滤/排名。
5. **dry_run 修正循环**：产物先 `dry_run(sql)`；失败读报错即改，≤3 次；仍败交编排器降级或转澄清。

## 硬约束

- **只读**：仅 `SELECT` / `WITH ... SELECT`。
- **正文不写 LIMIT**：top-N 用 `ORDER BY`，行数由编排器 `run_sql(sql, limit=N)` 控制（服务端默认 1000、硬上限 10000，自带 LIMIT 会双重语法错误）。
- **时间边界**：相对时间词必须解析成方言字面量（日期函数保持方言一致）。
- 输出**最终 SQL 原文**（单代码块），供性能优化与执行环节直接引用。

## 输出

- 回复末尾输出最终 SQL 代码块；>15KB 或多段子 SQL 时 write_file 到 `/workspace/nl2sql_process_data/{thread_id}/skill_sop/wren-sql-author/sql.sql`。
