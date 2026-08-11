---
name: nl2sql-query-plan
description: "触发：策略A流水线第4步。基于业务知识+Schema+子问题详情，生成程序化逐步查询计划。禁止生成SQL。策略B/策略C跳过。"
---

# NL2SQL 查询计划skill

## 概述

SQL-of-Thought 流水线第4步。**仅在策略A（复杂查询）中执行。**

基于业务知识+Schema+子问题详情详情，通过 CoT 推理生成逐步执行计划。

## 数据存储说明
- **存储路径**: `/workspace/nl2sql_process_data/{thread_id}/nl2sql-query-plan/query_plan.txt`
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录
- **依赖数据**: 读取前序 Skill 的 `knowledge.json`、`schema.json` 和 `subproblem.json`

## 输入数据读取

- 必须调用 read_file 写读取`/workspace/nl2sql_process_data/{thread_id}/knowledge-loader/knowledge.json`
- 必须调用 read_file 写读取`/workspace/nl2sql_process_data/{thread_id}/nl2sql-schema-linking/schema.json`
- 必须调用 read_file 写读取`/workspace/nl2sql_process_data/{thread_id}/nl2sql-subproblem/subproblem.json`

## 输出

- 必须调用 write_file 写入`/workspace/nl2sql_process_data/{thread_id}/nl2sql-query-plan/query_plan.txt` 程序化查询计划（文本格式），例如：

```
1. 从 titles_t 筛选 title_type='movie' 且 num_votes>10000 的行
2. 通过 principals_t.title_id 连接到 principals_t
3. 通过 principals_t.name_id 连接 names_t
4. 按 average_rating DESC 排序
5. 取前10行
```

## 关键规则

- **禁止在此阶段生成SQL** — 输出纯文本计划
- 对每个 JOIN 说明连接列

## 策略跳过规则

| 策略     | 是否执行 | 原因                |
| -------- | :------: | ------------------- |
| A (标准) |    是    | 复杂查询需要分解    |
| B (快速) |    否    | 简单查询直接生成SQL |
| C (Cube) |    否    | 不经过NL2SQL流水线  |

## 错误处理

- 如果 wren_ask 返回错误，写入 `/workspace/nl2sql_process_data/{thread_id}/error.json`
- 错误信息: `{"error": "查询计划", "detail": "..."}`
- 若存在`/workspace/nl2sql_process_data/{thread_id}/error.json`,则追加