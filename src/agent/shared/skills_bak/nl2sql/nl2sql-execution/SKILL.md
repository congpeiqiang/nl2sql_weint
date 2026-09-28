---
version: 0.1.0
name: nl2sql-execution
description: "触发：SQL 已通过 dry_run 验证（策略 A 性能优化完成）后、真正执行前——策略 A Step 6 / 策略 B 末步。覆盖：run_sql 执行契约（通道选择、只读、limit 参数、db_name 传参、结果落盘与汇总交接、失败转纠错）。跳过：策略 C（Cube 通道，用 query_cube）；SQL 尚在生成/dry_run 验证阶段（那是 nl2sql-sql-generation）；执行失败后的纠错（nl2sql-correction）。"
---

# NL2SQL 查询执行 Skill

## 概述

SQL-of-Thought 流水线的**查询执行步**（策略 A Step 6 / 策略 B 末步）。SQL 已 dry_run 通过、性能已优化后，**read 本 skill 再执行**。本步唯一职责：把最终 SQL 交给**正确通道**执行，并把结果交接给结果汇总。

**触发前提**（不满足不要执行）：
- SQL 已生成且 `dry_run` 通过（策略 B）/ 通过性能优化（策略 A）；
- 已按 nl2sql-understand 完成理解建模（当前库 schema/口径已取，未理解建模就被拦是规则问题，勿反复强试）。

## 执行通道（按当前库选，勿混用）

- **已建模库**（dynamic_prompt 注入「已在 Wren 语义层建模」；有 `wrenai_<库名>_*` 工具）→ 执行用 **`wrenai_<库名>_run_sql(sql, limit?)`**；schema/知识只准用语义工具（`describe_model`/`get_context` 等）。**禁止 `dbmcp_*` 直连**——不经过语义层，丢业务口径（系统会拦）。
- **未建模库**（注入「未在语义层建模」，无对应 wrenai server）→ 用 **`dbmcp_run_sql(sql=..., db_name='<当前库>')`** 直连，这是它唯一查询通道；不要用别的库的 wrenai_*_run_sql（会 `not found`/`INVALID_SQL`）。

## 执行契约

1. **只读**：仅 `SELECT`（含 `WITH ... SELECT`、CTE 只读查询）。写/DDL 由 SqlReadOnly 硬拦，生成 SQL 时不要产生。
2. **SQL 正文不写 `LIMIT`**：需要行数上限时用 `ORDER BY` 排好序，上限走 `run_sql` 的 `limit` 参数（默认 1000、最大 10000）——服务端会自动追加上限，SQL 自带 LIMIT 会双重 LIMIT 语法报错。
3. **db_name 传当前库**：按 dynamic_prompt 注入的【当前数据库】传参。
4. **结果落盘勿重查**：大结果（>50 行）已自动落盘 `nl2sql_process_data/{thread_id}/query_result/*.md` 并把消息瘦身为 `{row_count, rows: 前20样例, full_result_file}`——直接读精简结果或 `full_result_file`，**不要为了拿全量重跑同一条 SQL**。
5. **顺序**：dry_run 通过 →（A）优化 → 才 run_sql；未验证不执行。

## 收尾

- **成功**：把关键数字/结论整理成简短结果，交回汇总（`write_todos` 把「查询执行」标 completed、「结果汇总」标 in_progress），向用户给结论；不要重复执行同一条 SQL 去"确认"。
- **失败**：进入 Step 7 纠错（`nl2sql-correction`，最多 3 轮）。若语义层工具报 `not found`：仅在**未建模库**才考虑切 `dbmcp_run_sql`；**已建模库**的 not found 是语义项目/schema 问题，不要切直连绕口径，走纠错或返回结果说明。
