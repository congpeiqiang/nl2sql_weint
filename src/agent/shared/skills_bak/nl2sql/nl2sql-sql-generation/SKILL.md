---
version: 0.1.0
name: nl2sql-sql-generation
description: "触发：根据查询计划生成SQL。策略A：输入业务知识+Schema+子问题详情+查询计划。策略B：输入业务知识+Schema→直接输出SQL。生成后必须 dry_run(sql) 验证。唯一生成SQL的skill。"
---

# NL2SQL SQL 生成智能体（WrenAI 增强版）

## 概述

SQL-of-Thought 流水线第5步。唯一生成 SQL 的 skill。

- **策略A**: 接收业务知识+Schema+子问题详情+查询计划 → dry_run 验证 → 修复
- **策略B**: 接收 业务知识+Schema → 直接生成 SQL → dry_run 验证 → 修复

## 数据存储说明
- **存储路径**: `/workspace/nl2sql_process_data/{thread_id}/nl2sql-sql-generation/sql.sql`
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录
- **依赖数据**: 根据策略读取不同的前置数据

## 输入

- 优先从对话上下文中获取前序 Skill 的输出（knowledge-loader / schema-linking / subproblem / query-plan）
- 若上下文中找不到，则 read_file 对应文件作为 fallback：
  - `/workspace/nl2sql_process_data/{thread_id}/knowledge-loader/knowledge.json`
  - `/workspace/nl2sql_process_data/{thread_id}/nl2sql-schema-linking/schema.json`
  - `/workspace/nl2sql_process_data/{thread_id}/nl2sql-subproblem/subproblem.json`
  - `/workspace/nl2sql_process_data/{thread_id}/nl2sql-query-plan/query_plan.txt`

## 输出

- 在回复中直接输出经过 dry_run 验证的可执行 SQL
- 仅在 SQL >15KB 时 write_file 到 `/workspace/nl2sql_process_data/{thread_id}/nl2sql-sql-generation/sql.sql` 作为 fallback

## 执行步骤

### 策略A（标准流水线）

```
自然语言问题 (Q) +业务知识+  Schema (S)+子问题详情+查询计划 (P) 
    │
    ▼
生成 SQL（基于计划逐步构建）
    │
    ▼
dry_run(sql)   ← WrenAI 验证
    │
    ├─ 成功 → 结束并返回（后续若改动 SQL，run_sql 前须用最终 SQL 复验 dry_run）
    └─ 失败 → 分析错误 → 修复SQL → dry_run → 循环生成 SQL
                 │
                 └─ 3次失败 → nl2sql-correction
```

### 策略B（快速通道）

```
get_context 片段 + recall_queries 模板 + 问题 (Q)
    │
    ▼
基于模板生成 SQL（一次生成）
    │
    ▼
dry_run(sql)               ← WrenAI 验证
    │
    ├─ 成功 → 用最终 SQL 复验 dry_run → Phase 6: 执行
    └─ 失败 → 简单修复 → dry_run → 成功/放弃
```

## 生成规则

- **dry_run**: 生成后立即 dry_run，失败最多修复3次
- **纠错**: 3次 dry_run 失败 → 调用 nl2sql-correction
- **最终 SQL 复验（铁律）**: 执行 run_sql 前，**必须用最终 SQL 再 dry_run 一次**。若 SQL 在初版 dry_run 后被修改/精化（增删字段、改排序/别名、加过滤条件等），**禁止直接执行**——先 `dry_run(最终SQL)` 验证通过，再交给执行步骤 run_sql。确保 dry_run 记录与最终执行的 SQL 完全一致，避免出现「干跑的是版本A、实际执行版本B」的中间产物错位（process_data 里会留两个版本，且报告 SQL 与 dry_run 对不上）。
- 不要执行run_sql
- **run_sql 行数契约**：SQL 正文**不写 `LIMIT` 子句**；top-N / 防超量用 `ORDER BY ...` 排好序，交由执行阶段 `run_sql(sql=..., limit=N)` 传参（默认 1000）。服务端会自动追加上限，SQL 自带 LIMIT 会双重 LIMIT 语法报错
- **只读铁律**：只生成 `SELECT`（含 `WITH ... SELECT`）只读查询；**严禁**生成 INSERT/UPDATE/DELETE/REPLACE/MERGE 等 DML，或 DROP/ALTER/CREATE/TRUNCATE/RENAME/GRANT/REVOKE 等 DDL 及 SET/USE/LOAD/COPY/CALL 等非查询语句

## 错误处理

- 如果 wren_ask 返回错误，写入 `/workspace/nl2sql_process_data/{thread_id}/error.json`
- 错误信息: `{"error": "执行SQL", "detail": "..."}`
- 若存在`/workspace/nl2sql_process_data/{thread_id}/error.json`,则追加