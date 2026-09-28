---
version: 0.1.1
name: wren-orchestrator
description: "触发：基于 WrenAI 语义库的自然语言数据问题（官方 SOP 通道）。按 6 步单循环编排：四路并行取料 → 用户澄清 → 具名指标判路由 → 生成（query_cube / 手写）→ dry_run 修正 → SQL 性能优化 → run_sql → 输出答案。store_query 记忆回写**不在循环内**（由 FeedbackStore 桥接异步执行）。不预设子问题分解，复杂时临时拆分。"
---

# WrenAI 官方 SOP 主编排

## 概述

WrenAI 官方 `wren_workflow`（`mcp_server.py:622-644`）的落地编排，加入两个本项目扩展步骤：**用户澄清**（步骤2）与 **SQL 性能优化**（步骤5）。哲学不变：**单 Agent、工具优先、循环式**；`query_cube` 是软偏好不是独占车道。官方第 6 步的 `store_query` 回写**移出会话循环**：对错判定交给 FeedbackStore 桥接体系（用户👍=L1、人工金标=L2）异步执行，杜绝 LLM 自评污染 `knowledge/sql/*.md`。

## 能力 skill 清单

```
skill_sop/
├── wren-orchestrator/    # 本文件：6 步总控 + 路由决策
├── wren-retrieve/        # (1) 四路并行取料
├── wren-clarify/         # (2) 用户澄清
├── wren-metric-query/    # (3) 具名指标 → query_cube
├── wren-sql-author/      # (3)(4) 手写/混合 SQL + dry_run 修正循环
├── wren-perf-optimize/   # (5) SQL 性能优化
├── wren-execution/       # (6) 查询执行（run_sql）+ 结果呈现契约
└── wren-writeback/       # 循环外：store_query 回写执行规范（FeedbackStore 桥接实施时引用，不由会话触发）
```

## 六步主循环

```
问句
 └─(1) wren-retrieve：一次并行取齐 结构/范例/规则/知识 四块料
 └─(2) wren-clarify：依取料包裁决清晰度；不清晰 → [需要澄清] 停止本轮
 └─(3) 判路由（本文件执行，零工具调用）：
        具名指标三条件全满足      → wren-metric-query（纯 Cube）
        ①②满足、③维度缺          → wren-metric-query 出主体 + wren-sql-author 包外层（混合）
        measure 匹配不上          → wren-sql-author（手写）
 └─(4) dry_run → 失败即改（回对应能力修，≤3 次）
 └─(5) wren-perf-optimize：规则集检测 + 语义不变优化；改过必重 dry_run
 └─(6) wren-execution：run_sql(sql, limit=N)；正文不写 LIMIT → 输出答案
```

## 禁止会话内回写（铁律）

- 循环内**任何情况下都不调用 `store_query`**。
- 理由：会话结束时用户尚未反馈，`rating` 真信号要下一轮 HTTP 请求才异步产生；LLM 自评"答对"无外部真值背书；错误范例一旦写入 `knowledge/sql/*.md`，会被后续 `recall_queries` 召回当样板，污染是复利式的。
- 回写唯一路径：FeedbackStore 桥接任务（L1 用户👍 / L2 人工金标）在循环外异步执行，参数与幂等约定见 `wren-writeback/SKILL.md`。

## 路由判据：具名指标三条件

1. 问题含**聚合意图**（总额/数量/平均/占比/去重计数/按度量 TopN）。
2. 能锁定**某 cube 的某 measure**——匹配看 measure 的 **`expression`**（聚合烧在 expression 里，**永不读 `type`**，实测 wren_core 编译彻底忽略 type）与 `metrics/`、`glossary/` 术语映射。
3. 过滤/分组维度都在该 cube 的 `dimensions`/`time_dimensions` 内。

判断链：`聚合词 →（glossary/metrics 映射标准指标名）→ 匹配 cube.measure（按 expression）`。判断质量强依赖第四轴知识面。

## 铁律

- **dry_run 门**：任何路径产物 SQL 未 dry_run 通过，禁止 run_sql；失败 ≤3 次，仍败降级（指标→手写）或转澄清。
- **最终复验**：SQL 在 dry_run / 性能优化后被改动过 → run_sql 前必须用**最终 SQL** 再 dry_run 一次，杜绝"干跑 A 执行 B"。
- **行数契约**：SQL 正文不写 LIMIT；top-N 用 ORDER BY，行数由 `run_sql(sql, limit=N)` 控制。
- **只读**：只允许 `SELECT` / `WITH ... SELECT`，严禁 DML/DDL。
- **澄清一轮上限**：本轮会话最多追问 1 次；带【补充信息】仍不清 → 记 assumption 继续。
- **检索唯一性**：取料四工具全程各至多一次，下游只读取料包，不重复检索。

## 复杂查询临时拆分（非必经阶段）

多业务问题（如"对比 A 和 B 各自的月度趋势"）→ 临时拆 2~3 个子问题，各自跑 (3)~(6)，最后合并作答。官方 SOP 无固定分解阶段，**不预设**。

## 错误处理

- 取料部分失败 → 用成功部分继续，缺失块标空（见 wren-retrieve）。
- run_sql 执行失败 → 见 `wren-execution/SKILL.md` 的「失败处理」：语法/口径类回 wren-sql-author 修正（≤3 次）再重跑；超时/权限/连接类**不重试执行**，据实返回后由本编排器判定降级或转澄清。
