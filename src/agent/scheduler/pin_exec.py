"""定时任务固化查询的**只读执行器**。

渲染后的 spec（物理 SQL 或 cube 定义）在这里真正跑一次，产出报告要用的
columns / rows。两条通道：

- sql：直接丢给 dbmcp 引擎（与标注页「执行并验证 SQL」同一条路）；
- cube：``plan_cube_sql_checked`` 编译成 ``dialect_sql`` 再走 dbmcp——
  cube 结构上没有 SQL，直接执行只能靠引擎编译。

三条铁律：

1. **只读**：``classify_sql`` 判 write 直接拒（不是审批，是硬拒）。
2. **不占事件循环**：10 个 engine 的 ``run_sql`` 是「async 外壳 + 同步实心」，
   必须 ``asyncio.to_thread`` / 独立线程跑（与 ``api/feedback_annotation._run_preview``
   同款，那里的注释是这条的唯一权威解释）。
3. **回填实际下发的 SQL**：``_apply_default_limit`` 可能给语句补 LIMIT，报告
   「执行 SQL」节必须与真正发给物理库的那条逐字一致。
"""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.scheduler.query_spec import SpecError, render_spec
from agent.utils.wren_call_extract import resolve_wren_ctx_by_db
from agent.utils.wren_plan import CubePlanError, plan_cube_sql_checked

_ROW_LIMIT_ENV = "NL2SQL_SCHED_PIN_ROW_LIMIT"
_DEFAULT_ROW_LIMIT = 5000

#: 只把前 N 行塞进回放载荷——报告是给人看的，再多的行也只是把上下文撑爆。
_MAX_PAYLOAD_ROWS = 200


class PinExecError(Exception):
    """固化执行失败。``code`` 见计划 §C.2，``detail`` 直接进 ``pin_invalid_reason``。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class PinResult:
    channel: str
    sql: str = ""
    cube_query: str = ""
    cube_args: dict = field(default_factory=dict)
    cube_tool: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    params: dict = field(default_factory=dict)


def _env_row_limit() -> int:
    raw = (os.getenv(_ROW_LIMIT_ENV, "") or "").strip()
    if not raw:
        return _DEFAULT_ROW_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_ROW_LIMIT


def _json_safe(v: Any) -> Any:
    """runner 返回（pandas/numpy/Decimal/datetime）→ JSON 安全值。"""
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, (bool, int, str)) or v is None:
        return v
    try:
        import numpy as np

        if isinstance(v, np.generic):
            return v.item()
    except Exception:  # noqa: BLE001
        pass
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:  # noqa: BLE001
            pass
    return str(v)


def _run_engine_blocking(runner, args, context):
    """在工作线程里把 engine 的 ``run_sql`` 跑完（引擎是同步实心）。"""
    return asyncio.run(runner.run_sql(args, context))


async def run_readonly_sql(db_name: str, sql: str, limit: int = 1000) -> dict:
    """复用 dbmcp 引擎执行只读 SQL。

    返回 ``{"columns", "rows", "truncated", "executed_sql"}``。
    写/DDL 抛 ``ValueError``，执行异常原样上抛（调用方统一转 ``PinExecError``）。
    """
    from agent.middlewares.sql_approval import classify_sql
    from mcp_server.db_mcp_server.db.config import McpSqlConfig
    from mcp_server.db_mcp_server.db.db_server import (
        _apply_default_limit,
        _build_tool_context,
        _load_runner_class,
    )
    from mcp_server.db_mcp_server.db.multi_sql import (
        combine_multi_results,
        split_sql_statements,
    )
    from mcp_server.db_mcp_server.db.sql_runner import RunSqlToolArgs

    verdict, detail = classify_sql(sql)
    if verdict == "write":
        raise ValueError(f"仅允许只读查询（检测到 {detail or '写/DDL'} 操作，已拒绝执行）")

    cfg = McpSqlConfig.from_env(db_name)
    runner = await asyncio.to_thread(lambda: _load_runner_class(cfg.db_type)(**cfg.config))
    context = _build_tool_context()

    statements = split_sql_statements(sql)
    all_results: list = []
    executed: list[str] = []
    for stmt in statements:
        # clamp_existing=True：固化回放是**无人值守**通道，SQL 里自带的
        # `LIMIT 999999` 必须收敛到 row_limit，否则一次定时就把全表拉进内存。
        # （只往下压，不会压小本来就小于上限的 LIMIT。）
        # clamp_existing=True：固化回放是**无人值守**通道，SQL 里自带的
        # `LIMIT 999999` 必须收敛到 row_limit，否则一次定时就把全表拉进内存。
        # （只往下压，不会压小本来就小于上限的 LIMIT。）
        stmt = _apply_default_limit(stmt, limit, clamp_existing=True)
        executed.append(stmt)
        df = await asyncio.to_thread(
            _run_engine_blocking, runner, RunSqlToolArgs(sql=stmt), context
        )
        all_results.append((stmt, df))

    result = combine_multi_results(all_results)
    rows = [_json_safe(r) for r in result.get("rows", [])]
    return {
        "columns": [str(c) for c in result.get("columns", [])],
        "rows": rows,
        "truncated": bool(result.get("truncated", False)) or len(rows) >= limit,
        "executed_sql": ";\n".join(executed),
    }


async def run_pinned_spec(
    spec: dict,
    *,
    now: datetime,
    limit: int | None = None,
    timeout_s: int = 120,
) -> PinResult:
    """渲染 + 只读执行。所有失败都转成 ``PinExecError``（带 §C.2 的失效码）。"""
    try:
        rendered = render_spec(spec, now)
    except SpecError as e:
        raise PinExecError(e.code, e.detail) from e

    row_limit = min(
        int(spec.get("row_limit") or _DEFAULT_ROW_LIMIT),
        _env_row_limit(),
    )
    if limit is not None:
        row_limit = min(row_limit, int(limit))

    if rendered["channel"] == "sql":
        sql = rendered["sql"] or ""
        cube_args: dict = {}
        cube_query = ""
        cube_tool = ""
    else:
        cube_args = rendered["cube"] or {}
        if not cube_args.get("cube") or not cube_args.get("measures"):
            raise PinExecError("render_failed", "cube 定义缺少 cube 或 measures")
        project, conn = resolve_wren_ctx_by_db(spec.get("db_name") or "")
        if not project or not conn:
            raise PinExecError(
                "not_semantic_db", f"库 {spec.get('db_name') or '(未指定)'} 已没有语义库项目"
            )
        try:
            plan = await asyncio.to_thread(plan_cube_sql_checked, project, conn, cube_args)
        except CubePlanError as e:
            raise PinExecError("cube_compile_failed", str(e)[:200]) from e
        except Exception as e:  # noqa: BLE001
            raise PinExecError(
                "cube_compile_failed", f"{type(e).__name__}: {e}"[:200]
            ) from e
        sql = str(plan.get("dialect_sql") or "")
        if not sql:
            raise PinExecError("cube_compile_failed", "引擎没有产出物理 SQL")
        # 审计用：cube_sql 是语义层中间 SQL，报告节展示的是 dialect_sql。
        cube_query = str(plan.get("cube_sql") or "")
        cube_tool = "query_cube"

    try:
        # 注意：`run_readonly_sql` 内部是 `asyncio.to_thread`，线程不可取消——
        # 超时只放弃等待，那条 SQL 会在线程里跑完。定时任务宁可多占一会儿连接，
        # 也不能让看门狗被一条慢查询卡死。
        raw = await asyncio.wait_for(
            run_readonly_sql(spec.get("db_name") or "", sql, row_limit),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as e:
        raise PinExecError("exec_timeout", f"超过 {timeout_s}s") from e
    except ValueError as e:
        raise PinExecError("sql_failed", str(e)[:200]) from e
    except PinExecError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PinExecError("sql_failed", f"{type(e).__name__}: {e}"[:200]) from e

    columns = list(raw.get("columns") or [])
    rows = list(raw.get("rows") or [])
    return PinResult(
        channel=rendered["channel"],
        sql=str(raw.get("executed_sql") or sql),
        cube_query=cube_query,
        cube_args=cube_args,
        cube_tool=cube_tool,
        columns=columns,
        rows=rows[:_MAX_PAYLOAD_ROWS],
        row_count=len(rows),
        truncated=bool(raw.get("truncated")),
        params=rendered["params"],
    )
