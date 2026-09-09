# -*- coding: utf-8 -*-
"""在线评估开关（读时求值，2026-09-09）。

三个环境变量（写在 `.env` / `.env.prod`；容器由 compose `env_file` 注入）：

- ``NL2SQL_EVAL_ENABLED``（总开关，默认 1）：0/false/no/off → 所有评估器停用——
  确定性分（sql_valid / exec_success / schema_match）不写、LLM-judge 不入队、
  评估单元 sidecar 不落盘。
- ``NL2SQL_EVAL_JUDGE_ENABLED``（LLM 维度开关，默认 1）：0 → 只停
  ``sql_biz_correct_score`` / ``report_table_score`` / ``analysis_report_score``
  三维（省 token），零成本确定性维度照常。总开关为 0 时恒 False。
- ``NL2SQL_EVAL_SUBJECT``（评估单元 sidecar，默认 1）：0 → 不落证据、不组装。

采样率仍由 ``evaluators.judge_sample_rate()``（``NL2SQL_EVAL_JUDGE_SAMPLE``，
默认 0.3）控制，仅在 judge 开关为 1 时生效。

**为什么是函数而不是模块级常量**：``run_experiment`` 等脚本在运行时改 env
（显式设 ``NL2SQL_EVAL_JUDGE_QUEUE=0`` / ``NL2SQL_EVAL_JUDGE_SAMPLE=1.0``），
且 `.env` 的加载时机可能晚于 import——模块级常量在 import 时求值会读不到。
语义与 ``langfuse_client.langfuse_enabled()`` 保持一致。

依赖方向：本模块只 import os。``evaluators`` / ``eval_queue`` / ``eval_subject``
均可安全 import（``eval_queue`` 刻意不 import ``evaluators`` 以防 import 环）。
"""
from __future__ import annotations

import os

_OFF = ("0", "false", "no", "off")


def _flag(name: str, default: str = "1") -> bool:
    """解析布尔型环境变量（0/false/no/off 为假，其余为真）。"""
    return (os.getenv(name, default) or default).strip().lower() not in _OFF


def eval_enabled() -> bool:
    """在线评估总开关（NL2SQL_EVAL_ENABLED，默认开）。"""
    return _flag("NL2SQL_EVAL_ENABLED")


def judge_enabled() -> bool:
    """LLM-judge 开关（NL2SQL_EVAL_JUDGE_ENABLED，默认开）；总开关为 0 时恒假。"""
    return eval_enabled() and _flag("NL2SQL_EVAL_JUDGE_ENABLED")


def subject_enabled() -> bool:
    """评估单元 sidecar 开关（NL2SQL_EVAL_SUBJECT，默认开）；总开关为 0 时恒假。"""
    return eval_enabled() and _flag("NL2SQL_EVAL_SUBJECT")
