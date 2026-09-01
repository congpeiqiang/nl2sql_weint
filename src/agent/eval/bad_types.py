# -*- coding: utf-8 -*-
"""BadCase 错误类型枚举（6 大类，标注页下拉 + 统计 + 打标签共用）。

设计来源：docs/langfuse平台/NL2SQL反馈闭环优化设计方案.md §4.7。
⚠ 原需求在「多表关联错」处截断，join_error / filter_condition_error 为补全提案，
实施前与需求方确认。

每项：(枚举值, 中文名, 判定口径)。
"""
from __future__ import annotations

BAD_TYPES: list[tuple[str, str, str]] = [
    (
        "table_hallucination",
        "表幻觉",
        "使用不存在的表 / 表名写错",
    ),
    (
        "column_hallucination",
        "字段幻觉",
        "使用不存在的字段 / 字段归属错表",
    ),
    (
        "time_condition_error",
        "时间条件错误",
        "时间范围/粒度/时区/日期比较错（如「近3天」写成反向 BETWEEN）",
    ),
    (
        "agg_logic_error",
        "聚合与统计错误",
        "GROUP BY 缺漏 / 聚合函数错 / 去重错 / 比率口径错",
    ),
    (
        "join_error",
        "多表关联错误",
        "JOIN 条件错 / 漏 ON / 笛卡尔积 / 表关系方向错",
    ),
    (
        "filter_condition_error",
        "过滤条件错误",
        "WHERE 条件缺失 / 运算符错 / 取值错",
    ),
]

BAD_TYPE_KEYS = [k for k, _, _ in BAD_TYPES]
BAD_TYPE_LABELS: dict[str, str] = {k: label for k, label, _ in BAD_TYPES}


def is_valid_bad_type(value: str) -> bool:
    return value in BAD_TYPE_KEYS
