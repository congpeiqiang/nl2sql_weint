# -*- coding: utf-8 -*-
"""「哪些工具会真的跑查询、产出数据表」的单一权威清单。

三个消费点，必须共用同一份 —— 任何两处不一致都会长出半死状态：
- `QueryResultOffloadMiddleware`：决定大结果是否落盘 + 消息瘦身（不认 → 结果永不落盘）；
- `check_progress._collect_full_result_files`：决定是否把落盘指针汇总进 check 结果
  （不认 → 落了盘也没人收 → 报告缺「完整数据表」节）；
- `path_resolver._tool_timeout_for`：决定是否给「真实长查询」的 300s 超时
  （不认 → 同一类查询在 Cube 通道只有 120s，更易被工具超时打断）。

放在 `agent.utils`（纯 stdlib、零依赖）而不是上面任一处：三个模块的导入图刻意
不同（中间件引 langchain，path_resolver 刻意不引），谁引谁都会带进多余依赖。
"""
from __future__ import annotations

# 数据表型工具名后缀：
# - `_run_sql`：标准语义管道（wrenai_<库名>_run_sql）
# - `_query_cube`：Cube 快速通道（wrenai_<库名>_query_cube）
# 两者返回结构同构（`{columns, rows, row_count}`），都是「真的查了一次库」。
# `dry_run` / `dry_plan` 只有执行计划、没有行数据，故意不列（真进来了也会在
# 「rows 非 list」处被挡掉）；元数据类（list_cubes / describe_cube）同理不列
# ——它们不跑查询，不该拿到长查询的容忍度。
DATA_TOOL_SUFFIXES = ("_run_sql", "_query_cube")


def is_data_tool(name) -> bool:
    """工具名是否为数据表型（真跑查询、返回行数据）。"""
    return isinstance(name, str) and bool(name) and name.endswith(DATA_TOOL_SUFFIXES)
