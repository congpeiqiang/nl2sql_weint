# -*- coding: utf-8 -*-
"""在线评估开关（读时求值，2026-09-09）。

四个键（默认值见 ``_DEFAULTS``；可写在 `.env` / `.env.prod`，也可在前端
「设置 → 评估」里改——后者落盘到 ``eval_flags_store`` 的覆盖层）：

- ``NL2SQL_EVAL_ENABLED``（总开关，默认 1）：0/false/no/off → 所有评估器停用——
  确定性分（sql_valid / exec_success / schema_match）不写、LLM-judge 不入队、
  评估单元 sidecar 不落盘。
- ``NL2SQL_EVAL_JUDGE_ENABLED``（LLM 维度开关，默认 1）：0 → 只停
  ``sql_biz_correct_score`` / ``report_table_score`` / ``analysis_report_score``
  三维（省 token），零成本确定性维度照常。总开关为 0 时恒 False。
- ``NL2SQL_EVAL_SUBJECT``（评估单元 sidecar，默认 1）：0 → 不落证据、不组装。
- ``NL2SQL_EVAL_JUDGE_SAMPLE``（LLM-judge 采样率，默认 0.3）：0~1，仅在 judge
  开关为 1 时生效。

**取值优先级：覆盖层（前端） > os.environ（.env / compose 注入） > 代码默认。**
覆盖层只存被显式改过的键，其余键继续跟随 .env——即「前端恢复默认」= 回到 .env。
``raw()`` 同时返回来源（override / env / default），供 ``/api/eval-flags`` 展示。

**为什么是函数而不是模块级常量**：``run_experiment`` 等脚本在运行时改 env
（显式设 ``NL2SQL_EVAL_JUDGE_QUEUE=0`` / ``NL2SQL_EVAL_JUDGE_SAMPLE=1.0``），
且 `.env` 的加载时机可能晚于 import——模块级常量在 import 时求值会读不到；
前端覆盖层同样要求改完即刻生效。语义与 ``langfuse_client.langfuse_enabled()`` 一致。

依赖方向：本模块只 import ``os`` + ``eval_flags_store``（后者不反向 import，无环）。
``evaluators`` / ``eval_queue`` / ``eval_subject`` 均可安全 import
（``eval_queue`` 刻意不 import ``evaluators`` 以防 import 环）。
"""
from __future__ import annotations

import os

from agent.eval.eval_flags_store import KEYS, overrides

_OFF = ("0", "false", "no", "off")

_DEFAULTS: dict[str, str] = {
    "NL2SQL_EVAL_ENABLED": "1",
    "NL2SQL_EVAL_JUDGE_ENABLED": "1",
    "NL2SQL_EVAL_SUBJECT": "1",
    "NL2SQL_EVAL_JUDGE_SAMPLE": "0.3",
}


def raw(name: str) -> tuple[str, str]:
    """返回 ``(取值, 来源)``；来源 ∈ ``override`` / ``env`` / ``default``。"""
    ov = overrides()
    if name in ov:
        return ov[name], "override"
    env = os.environ.get(name)
    if env is not None and env.strip():
        return env.strip(), "env"
    return _DEFAULTS.get(name, ""), "default"


def _flag(name: str) -> bool:
    """解析布尔型开关（0/false/no/off 为假，其余为真）。"""
    return raw(name)[0].strip().lower() not in _OFF


def eval_enabled() -> bool:
    """在线评估总开关（NL2SQL_EVAL_ENABLED，默认开）。"""
    return _flag("NL2SQL_EVAL_ENABLED")


def judge_enabled() -> bool:
    """LLM-judge 开关（NL2SQL_EVAL_JUDGE_ENABLED，默认开）；总开关为 0 时恒假。"""
    return eval_enabled() and _flag("NL2SQL_EVAL_JUDGE_ENABLED")


def subject_enabled() -> bool:
    """评估单元 sidecar 开关（NL2SQL_EVAL_SUBJECT，默认开）；总开关为 0 时恒假。"""
    return eval_enabled() and _flag("NL2SQL_EVAL_SUBJECT")


def sample_rate() -> float:
    """LLM-judge 采样率（0~1，默认 0.3）；解析失败回退默认。"""
    val, _ = raw("NL2SQL_EVAL_JUDGE_SAMPLE")
    try:
        rate = float(val)
    except ValueError:
        return 0.3
    return max(0.0, min(1.0, rate))


def snapshot() -> dict[str, dict[str, str]]:
    """所有开关的当前取值 + 来源 + 代码默认（``/api/eval-flags`` 用）。"""
    return {
        name: {"value": raw(name)[0], "source": raw(name)[1], "default": _DEFAULTS[name]}
        for name in KEYS
    }
