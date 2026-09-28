---
version: 0.1.1
name: wren-writeback
description: "触发：**FeedbackStore 桥接任务异步执行**（用户👍=L1 / 人工金标=L2）或人工手动——不由会话循环内 agent 主动触发。本文件是回写的执行规范：store_query 参数规范 / 幂等 / tags / 撤销约定。『该不该写』的对错判定归 FeedbackStore 体系，本 skill 只定义『怎么写』。"
---

# store_query 回写执行规范（桥接专属，非会话步骤）

## 概述

`store_query` 是官方设计的复利内核：答对即沉淀，`recall_queries` 池随使用自增长。实测本环境 fast-path 已实现"写 markdown 真源、跳过 LanceDB 索引"（`path_resolver.py:988`），索引 best-effort。

**本 skill 不是主循环的第 7 步**：它定义桥接任务落盘时必须遵守的参数与幂等约定，供"反馈→记忆桥"实施时引用；会话内 agent 不应加载执行本流程。

## 触发（会话内唯一路径：无；回写在循环外）

- 会话内 agent **禁止**主动调 `store_query`——LLM 自评无外部真值，误报写进 `knowledge/sql/*.md` 会被后续 `recall_queries` 当范例召回，污染是复利式的，且无人知情、无人撤回。
- 唯一触发源 = **FeedbackStore 桥接任务**（增量扫 `rating=positive` 且 SQL 非空且非闲聊 = L1；`confirm_good`/validated→good 金标 = L2），异步补写/覆盖，与 `withdraw_auto_good` 形成对称撤销链。
- 官方 SOP 第 6 步（会话内 store_query）在此方案中的落位即被此桥接取代。

## 参数规范（桥接调用时）

```
store_query(
  nl_query = 用户原始问题（不改写，取 FeedbackStore.question 快照），
  sql_query = 最终执行 SQL（FeedbackStore.sql 快照，性能优化后的版本），
  datasource = 当前库名,
  tags = "sop,<来源标记>&thread=<tid>&msg=<mid>"
         # 来源标记：user（L1👍）/ gold（L2 金标）
         # thread/msg 指纹 = 撤销定位键（用户撤销点赞时按此删对应 md）
)
```

## 幂等

- 写前用 `recall_queries(nl_query, limit=1)` 自查：**同文 nl_query 已存在则跳过**（不重复 append）；
- L2 金标条目可**覆盖**同 nl_query 的 L1 条目（gold 优先于 user）；
- 内容相同但 nl 措辞近似的不强制去重（近重复天然构成检索语料多样性）。

## 撤销（对称路径）

- 用户撤销点赞 → 桥接按 `tags` 中 thread/msg 指纹定位 md 文件删除/标记失效（Wren 原生无删改接口，由桥接侧补）；
- L2 金标条目**不随** L1 撤销消失（人的判断依据不止那个 👍，与 `withdraw_auto_good` 只收 `auto_good=1` 同理）。

## 边界

- 只回写**只读 SELECT** 的成功查询（DML/DDL 一律不落，实测上也过不了）。
- `store_query` 失败 → 一次即止，记 error 日志，由桥接下轮游标重试；不影响任何已产出的会话回答。
