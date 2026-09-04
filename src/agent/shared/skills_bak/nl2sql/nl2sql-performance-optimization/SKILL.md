---
version: 0.1.0
name: nl2sql-performance-optimization
description: "触发：SQL生成成功且dry_run验证通过后，在正式执行前进行性能优化检查。策略A中使用。基于性能规则集检测SQL中的性能隐患（SELECT *、未设行数上限、笛卡尔积、函数包裹列、NOT IN等），并给出优化建议。策略B简单查询可选。策略C不经过此步骤。"
---

# NL2SQL SQL 性能优化智能体

## 概述

SQL-of-Thought 流水线性能优化环节。在 SQL 生成成功、dry_run 验证通过后、正式执行前调用。

- **定位**：SQL **执行成功但可优化**时进行性能优化（区别于 `nl2sql-correction` 的"执行失败纠错"）
- **策略A**: 调用此 skill 进行性能检测与优化
- **策略B**: 简单查询可选（单表简单筛选通常无需优化）
- **策略C**: 不适用（不经过 NL2SQL 流水线）

## 数据存储说明
- **输入路径**: `/workspace/nl2sql_process_data/{thread_id}/nl2sql-sql-generation/sql.sql`
- **输出路径**: `/workspace/nl2sql_process_data/{thread_id}/nl2sql-performance-optimization/optimization.json`
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录

## 输入

- 优先从对话上下文中获取 sql-generation 输出的 SQL
- 若上下文中找不到，则 read_file `/workspace/nl2sql_process_data/{thread_id}/nl2sql-sql-generation/sql.sql` 作为 fallback
- 可选：从上下文或 read_file 获取 schema-linking 的 Schema（辅助判断索引/列）

## 输出

- 在回复末尾输出优化结果 JSON（````json` 代码块）
- 仅在数据 >15KB 时 write_file 到 `/workspace/nl2sql_process_data/{thread_id}/nl2sql-performance-optimization/optimization.json` 作为 fallback
- 格式：

```json
{
  "sql": "原始SQL",
  "optimized_sql": "优化后的SQL（如有）",
  "issues": [
    {
      "rule": "select_star",
      "severity": "high",
      "message": "SELECT * 应明确列出所需列",
      "suggestion": "SELECT id, name, age FROM users"
    }
  ],
  "summary": "共发现 2 个性能问题，1 个高风险"
}
```

## 执行步骤

```
读取 sql.sql（待优化SQL）
    │
    ▼
Step 1: 逐条对照性能规则集（references/performance-rules.md）检查SQL
    │
    ▼
Step 2: 对每个命中规则，评估严重程度（high/medium/low）
    │
    ▼
Step 3: 生成优化建议（必要时给出优化后的SQL）
    │
    ▼
Step 4: 写入 optimization.json
    │
    ▼
Step 5: 返回优化结果给编排器
```

## 关键规则

- **只读约束**：本技能只做性能分析，不执行 SQL、不修改数据库
- **保守优化**：优化建议必须保持 SQL 语义不变，不得改变查询结果
- **优先级**：高风险问题（SELECT *、笛卡尔积、未设行数上限）优先处理
- **不强制修改**：输出优化建议，由编排器决定是否采纳
- **规则集**：完整规则见 `references/performance-rules.md`，必须逐条对照

## 性能规则速查（详见 references/performance-rules.md）

| # | 规则 | 严重度 | 说明 |
|---|------|:---:|------|
| 1 | SELECT * | high | 应明确列出所需列 |
| 2 | 未设行数上限 | high | 可能返回大量数据（默认 run_sql limit=1000 兜底；top-N 须显式传 limit，SQL 不写 LIMIT） |
| 3 | JOIN 无 ON 条件 | high | 笛卡尔积风险 |
| 4 | 子查询 | medium | 可改写为 JOIN |
| 5 | 函数包裹列 | medium | 如 YEAR(created_at) 导致索引失效 |
| 6 | NOT IN | medium | 可改写为 NOT EXISTS |
| 7 | DISTINCT | low | 评估是否必要 |
| 8 | LIKE 前缀通配符 | medium | 避免索引失效 |
| 9 | OR 条件 | low | 考虑 UNION ALL |
| 10 | LIMIT 无 ORDER BY | medium | 结果不确定 |

## 错误处理

- 如果 SQL 无法解析，写入 `/workspace/nl2sql_process_data/{thread_id}/error.json`
- 错误信息: `{"error": "性能优化", "detail": "..."}`
- 若存在 `/workspace/nl2sql_process_data/{thread_id}/error.json`，则追加
