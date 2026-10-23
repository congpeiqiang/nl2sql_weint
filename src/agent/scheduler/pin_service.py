"""固化查询的编排层：取来源会话的结果 → 固化 → **验证执行** → 落库。

为什么必须验证执行一次：``expect_columns`` 只能来自真实执行返回的列。来源会话
里那段文本是模型当时写给人看的，列名大小写/顺序/子查询别名都可能与本次驱动
返回的不一致；拿文本当基线，第二次触发就会误报列漂移。

固化失败一律**不落库**——半截的 spec 比没有 spec 更糟：它会以 active 状态被
回放，直到某次列漂移才暴露。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from agent.scheduler.pin_exec import PinExecError, run_pinned_spec
from agent.scheduler.query_spec import SpecError, extract_spec, validate_columns
from agent.scheduler.store import ScheduledTaskStore, get_store
from agent.utils.git_repo import repo_info
from agent.utils.semantic_db import SemanticDbDetector

_logger = logging.getLogger(__name__)


class PinError(Exception):
    """固化失败（API 层折成 400）。``code`` 见计划 §C.2。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def project_path_for(db_name: str) -> Optional[str]:
    """``db_name`` 的 Wren 项目路径；未建模返回 None。

    包成模块级函数是为了可打桩——``SemanticDbDetector`` 有缓存状态，
    测试不能直接 patch 它的方法。
    """
    return SemanticDbDetector().project_path_for(db_name)


async def semantic_version_of(db_name: str) -> str:
    """语义库版本 = 当前 HEAD 的 commit sha；直连库返回 ``""``。

    比 **commit**，不比 tag / branch：tag 可以在同一个 commit 上挪，branch 名
    更是天天指向新东西——任何"看起来变了"的判据都会让固化永远失效。
    """
    path = await asyncio.to_thread(project_path_for, db_name)
    if not path:
        return ""
    info = await asyncio.to_thread(repo_info, str(path)) or {}
    return str(info.get("commit") or "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _make_client():
    """HTTP 自调用（后台线程没有主进程 app 上下文，见 ``worker._make_client``）。"""
    from langgraph_sdk import get_client

    from api._common import _api_base_url

    return get_client(url=_api_base_url())


def _latest_success_thread(store: ScheduledTaskStore, task_id: str) -> str:
    """最近一条成功跑完、且落了 thread_id 的流水（新的在前）。"""
    for run in store.list_runs(task_id, limit=20):
        if run.get("status") == "success" and run.get("thread_id"):
            return str(run["thread_id"])
    return ""


async def capture_spec(
    task: dict[str, Any],
    *,
    thread_id: Optional[str] = None,
    now: datetime,
    date_bindings: Optional[dict[str, str]] = None,
    accept_constants: bool = False,
    client: Any = None,
    store: Optional[ScheduledTaskStore] = None,
) -> tuple[dict, list[str]]:
    """从一次**成功**的定时执行里抽出固化 spec。

    来源会话按「显式 thread_id > 流水里最近一条 success」取；``messages`` 里
    找最近一条成功的 ``check_async_task`` 结果——那是报告唯一的数据源，
    也是唯一带着真实下发 SQL / cube 定义的地方。
    """
    st = store if store is not None else get_store()
    task_id = str(task["id"])

    source_thread = thread_id or _latest_success_thread(st, task_id)
    if not source_thread:
        raise PinError(
            "no_source_run",
            "该任务还没有成功跑过一次，没有可固化的查询（先让它跑成功一次）",
        )

    client = client if client is not None else _make_client()
    state = await client.threads.get_state(thread_id=source_thread)
    values = (state or {}).get("values") or {}
    messages = values.get("messages") or []

    from agent.tools.report_builder import _find_last_check_result

    hit = _find_last_check_result(messages, "")
    if not hit:
        raise PinError(
            "no_check_result",
            "来源会话里没有成功的查询结果（check_async_task status=success）",
        )
    check, _text = hit

    version = await semantic_version_of(str(task.get("db_name") or ""))
    try:
        spec, warns = extract_spec(
            check,
            db_name=str(task.get("db_name") or ""),
            tz=str(task.get("tz") or "Asia/Shanghai"),
            semantic_version=version,
            source_thread_id=source_thread,
            now=now,
            date_bindings=date_bindings,
            accept_constants=accept_constants,
        )
    except SpecError as e:
        raise PinError(e.code, e.detail) from e
    return spec, warns


async def pin_task(
    task: dict[str, Any],
    *,
    thread_id: Optional[str] = None,
    date_bindings: Optional[dict[str, str]] = None,
    accept_constants: bool = False,
    now: Optional[datetime] = None,
    store: Optional[ScheduledTaskStore] = None,
    client: Any = None,
) -> dict[str, Any]:
    """固化一个定时任务：取结果 → 建 spec → 验证执行 → 落库。

    验证执行是**必须**的一步（见模块头）。任何一步失败都不写库。
    """
    st = store if store is not None else get_store()
    now = now or _now()
    task_id = str(task["id"])

    # 注意：这里不把 store 传进 capture_spec —— 它自己回落到 get_store()。
    # 生产路径两者是同一个单例；单测里 capture_spec 一律打桩。
    spec, warns = await capture_spec(
        task,
        thread_id=thread_id,
        now=now,
        date_bindings=date_bindings,
        accept_constants=accept_constants,
        client=client,
    )

    try:
        result = await run_pinned_spec(spec, now=now)
    except PinExecError as e:
        raise PinError(e.code, e.detail) from e

    ok, detail = validate_columns(spec, result.columns)
    if not ok:
        raise PinError("columns_changed", detail)

    spec["expect_columns"] = [str(c) for c in result.columns]
    st.set_pin(
        task_id,
        spec_json=json.dumps(spec, ensure_ascii=False),
        semantic_version=str(spec.get("semantic_version") or ""),
        source_thread_id=str(spec.get("source_thread_id") or ""),
        now_iso=_utc_iso(now),
    )
    row = st.get_task(task_id) or {}
    _logger.info(
        "[pin] 已固化任务 %s（%s 通道，%d 个锚点，来源 %s）",
        task_id, spec.get("channel"), len(spec.get("params") or []),
        spec.get("source_thread_id"),
    )
    return {
        "pin": {k: row.get(k, "") for k in (
            "pin_status", "pin_semantic_version", "pin_saved_at",
            "pin_invalid_reason", "pin_source_thread_id",
        )},
        "pin_status": row.get("pin_status", ""),
        "channel": spec.get("channel", ""),
        # 实际下发的那条（含引擎追加/收敛的 LIMIT）——前端「以后跑这条」直接展示它
        "preview_sql": result.sql,
        "params": result.params or {},
        "warnings": warns,
    }


async def invalidate(
    task_id: str,
    reason: str,
    now: Optional[datetime] = None,
    store: Optional[ScheduledTaskStore] = None,
) -> None:
    """标失效（保留 spec 供排查）。回放路径失败时调用，本 can run 之外不改任何东西。"""
    st = store if store is not None else get_store()
    st.invalidate_pin(task_id, reason, _utc_iso(now or _now()))
    _logger.warning("[pin] 任务 %s 固化失效：%s", task_id, reason)


async def clear(
    task_id: str,
    reason: str,
    now: Optional[datetime] = None,
    store: Optional[ScheduledTaskStore] = None,
) -> None:
    """解绑固化（spec 一并清空），任务回到正常 agent 执行。"""
    st = store if store is not None else get_store()
    st.clear_pin(task_id, reason, _utc_iso(now or _now()))
    _logger.info("[pin] 任务 %s 固化已解绑：%s", task_id, reason)
