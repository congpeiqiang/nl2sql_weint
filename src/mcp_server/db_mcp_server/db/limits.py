# -*- coding: utf-8 -*-
"""目标库访问的两道硬闸：statement timeout + 结果行数上限（P2-8）。

## 为什么集中在一个模块里

这两件事各有 4 个落点（postgres / mysql / sqlite / clickhouse runner）+ 1 个汇总点
（``db_server.run_sql``）。散着写必然长出「4 处实现互不相同」——这正是改造前的状态：
行数上限只有 ``_apply_default_limit`` 一处、且判据是**全文文本嗅探**。

## 两道闸各自防什么

- **statement timeout**：防「一条 SQL 在目标库上跑飞」。改造前全仓**没有任何一处**
  传递 statement timeout ⇒ 一条慢查询会一直占着目标库的资源，直到本仓的**工具超时**
  （300s，见 ``agent/utils/path_resolver.py::_TOOL_TIMEOUTS``）把客户端放弃；
  客户端走了，**服务端那条查询还在跑**。默认值必须**小于**工具超时，好让数据库先中止
  并回一个明确错误，而不是我们这边先超时、留下一个还在烧资源的查询。
- **结果行数上限**：防「大表结果全量拉进 1200m 的容器」。改造前靠给 SELECT 追加
  ``LIMIT``，但判据是 ``"LIMIT" not in sql.upper()`` ⇒ 模型自己写 ``LIMIT 999999999``、
  或子查询/注释里出现过 LIMIT，注入就被跳过，**结果全量进内存**。所以这里除了追加，
  还要在**取数之后按行数硬截断**——文本判据可以被绕过，行数是绕不过去的事实。

## 口径（与仓内其它开关一致）

``0`` = 关闭该闸；**非数字 → 退回默认**（静默关掉保护比报错危险得多）；负值按关闭算。
sqlite 没有 statement timeout 机制（只有 progress handler 轮询，对读主导的场景收益低），
**刻意不做**，见 ``statement_timeout_sql`` 的返回值。
"""
from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)

# 与语义层（wren）的 DEFAULT_ROW_LIMIT / MAX_ROW_LIMIT 同值，保证两条通道契约一致。
# 改这里要同步 ``agent/utils/wren_plan.py:56-57`` 那两个复刻值。
DEFAULT_ROW_LIMIT = 1000
MAX_ROW_LIMIT = 10000

# 工具超时是 300s（``path_resolver._TOOL_TIMEOUTS['run_sql']``）⇒ 默认值取 240s：
# 留 60s 余量让「数据库中止 → 错误回传 → agent 看到」先发生。
DEFAULT_STATEMENT_TIMEOUT_SECS = 240.0


def _env_float(name: str, default: float) -> float:
    """读 float 型 env：空/解析不了 → 默认（并记 warning，不静默）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        _logger.warning("[db-limits] %s=%r 不是数字，退回默认 %s", name, raw, default)
        return default


def statement_timeout_secs() -> float:
    """statement timeout 秒数（``<=0`` = 关闭）。env ``NL2SQL_DB_STATEMENT_TIMEOUT_SECS``。"""
    v = _env_float("NL2SQL_DB_STATEMENT_TIMEOUT_SECS", DEFAULT_STATEMENT_TIMEOUT_SECS)
    return v if v > 0 else 0.0


def max_row_limit() -> int:
    """硬上限（任何 ``limit`` 参数都越不过它）。env ``NL2SQL_DB_MAX_ROW_LIMIT``。"""
    v = _env_float("NL2SQL_DB_MAX_ROW_LIMIT", float(MAX_ROW_LIMIT))
    if v <= 0:
        return MAX_ROW_LIMIT
    return int(v)


def effective_row_cap(limit: object) -> int:
    """把工具入参 ``limit`` 收敛成一个可信的上限。

    - 缺失 / 非数字 / ``<=0`` → ``DEFAULT_ROW_LIMIT``
    - 超过硬上限 → 硬上限

    ⚠️ 入参来自模型，**必须当成不可信输入**：``limit=10**9`` 是能写出来的。
    """
    try:
        n = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = DEFAULT_ROW_LIMIT
    cap = max_row_limit()
    return min(n, cap)


def statement_timeout_sql(db_type: str) -> str | None:
    """该引擎用来设 statement timeout 的 SQL；没有机制/已关闭则返回 ``None``。

    - postgres：会话级 ``statement_timeout``（毫秒）
    - clickhouse：走 client settings，不在这里（见 runner）
    - sqlite：**刻意不做**（无此机制）
    - mysql：单位/变量名与版本有关，见 ``mysql_timeout_sqls()``
    """
    secs = statement_timeout_secs()
    if secs <= 0:
        return None
    ms = int(secs * 1000)
    if db_type == "postgres":
        return f"SET statement_timeout = {ms}"
    return None


def mysql_timeout_sqls() -> list[str]:
    """MySQL 系按序尝试的候选语句（第一条成功即止）。

    差异是真实存在的：MySQL 5.7.8+ 是 ``MAX_EXECUTION_TIME``（**毫秒**、只作用于
    只读 SELECT），MariaDB 是 ``max_statement_time``（**秒**）。写死一个必然在另一边
    报「Unknown system variable」——所以按序试，全失败只记日志、**不让查询失败**
    （超时是保护，不是正确性前提）。
    """
    secs = statement_timeout_secs()
    if secs <= 0:
        return []
    return [
        f"SET SESSION MAX_EXECUTION_TIME = {int(secs * 1000)}",
        f"SET SESSION max_statement_time = {secs:g}",
    ]


def clickhouse_settings() -> dict:
    """ClickHouse 的 statement timeout 走 client ``settings``（键 ``max_execution_time``，秒）。"""
    secs = statement_timeout_secs()
    if secs <= 0:
        return {}
    return {"max_execution_time": secs}
