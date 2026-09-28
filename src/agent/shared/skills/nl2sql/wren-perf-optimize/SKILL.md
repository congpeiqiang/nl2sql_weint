---
version: 0.1.0
name: wren-perf-optimize
description: "触发：SQL 通过 dry_run 后、run_sql 前（官方 SOP 的本方案扩展步骤）。对照性能规则集检测执行成功但可优化的隐患（SELECT *、笛卡尔积、函数包列等），产出语义不变的优化 SQL；改过必重 dry_run。规则集见本技能自带 references/performance-rules.md。"
---

# SQL 性能优化

## 定位

**执行成功但可优化**时的优化环节（区别于纠错：语法/执行错误已在步骤4修完）。本步不阻塞主流程：无命中即原样放行。

## 性能规则集（逐条对照，完整规则见本技能自带 `references/performance-rules.md`）

| # | 规则 | 严重度 | 说明 |
|---|------|:---:|------|
| 1 | SELECT * | high | 明确列出所需列 |
| 2 | 未设行数上限 | high | top-N 须显式传 limit；SQL 正文不写 LIMIT（run_sql 兜底 1000） |
| 3 | JOIN 无 ON 条件 | high | 笛卡尔积风险 |
| 4 | 子查询 | medium | 可改写为 JOIN |
| 5 | 函数包裹列 | medium | 如 `YEAR(col)` 致索引失效 → 改范围条件 |
| 6 | NOT IN | medium | 改 NOT EXISTS |
| 7 | DISTINCT | low | 评估是否必要 |
| 8 | LIKE 前缀通配 | medium | `%xxx` 索引失效 |
| 9 | OR 条件 | low | 考虑 UNION ALL |
| 10 | LIMIT 无 ORDER BY | medium | 结果不确定 |

## 关键规则

- **语义不变铁律**：优化前后查询结果必须等价；拿不准等价性的改写（如子查询→JOIN 在去重语义下）**不改，只记建议**。
- **Cube 编译 SQL 谨慎**：输入若来自 `query_cube` 编译产物，其 GROUP BY/DATE_TRUNC 结构视为口径权威，**只在明确命中高风险规则时改写**，避免破坏 measure 口径。
- **改后必复验**：产出 `optimized_sql` ≠ 原 SQL 时，编排器必须对 `optimized_sql` **重新 dry_run** 后才进 run_sql。
- **不强制修改**：仅 high 命中默认采纳；medium/low 输出建议，由编排器权衡。
- 只读约束：本步只分析，不执行 SQL。

## 输出

回复末尾 JSON 块（>15KB 才 write_file `.../skill_sop/wren-perf-optimize/optimization.json`）：

```json
{
  "sql": "原始SQL",
  "optimized_sql": "优化后SQL（无改动则同原文）",
  "changed": true,
  "issues": [{"rule": "select_star", "severity": "high", "message": "", "suggestion": ""}],
  "summary": "共发现 N 个性能问题，M 个高风险已修"
}
```
