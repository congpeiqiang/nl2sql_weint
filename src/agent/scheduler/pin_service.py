"""固化查询的编排层：取来源会话的结果 → 固化 → **验证执行** → 落库。

为什么必须验证执行一次：一是确认这条 SQL 今天还能跑通（源 run 可能已经是几天
前的），二是把 ``expect_columns`` 定成**真实执行**返回的列——来源会话里只有模型
写给人看的文本，列名大小写/顺序/子查询别名都可能与驱动返回的不一致；拿文本当
基线，第二次触发就会误报列漂移。列漂移的**比对点**在触发时（worker），不在固化时。

固化失败一律**不落库**——半截的 spec 比没有 spec 更糟：它会以 active 状态被
回放，直到某次列漂移才暴露。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from agent.scheduler.pin_exec import PinExecError, run_pinned_spec
from agent.scheduler.query_spec import SpecError, extract_spec
from agent.scheduler.store import ScheduledTaskStore, get_store
from agent.utils.git_repo import repo_info
from agent.utils.semantic_db import SemanticDbDetector

_logger = logging.getLogger(__name__)


class PinError(Exception):
    """固化失败（API 层折成 400）。``code`` 见计划 §C.2。

    ``extra`` 原样透到响应体（如 ``ambiguous_date`` 的 ``literal``/候选锚点），
    给前端渲染选择框用；``detail`` 只负责给人看的措辞。
    """

    def __init__(self, code: str, detail: str = "", extra: Optional[dict] = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.extra = extra or {}


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


def _source_run_time(
    store: ScheduledTaskStore, task_id: str, thread_id: str
) -> Optional[datetime]:
    """来源 run 的触发时刻（``task_runs.fired_at``）；查不到返回 None。

    日期锚点必须按**产生这条 SQL 那次运行**当天解析：字面量 ``2026-10-26`` 在
    10-27（周二）是 ``yesterday``，在 10-28（周三）却恰好是 ``week_start``。拿
    固化当天解析会静默固化成「上个周一」起的窗口——2026-10-28 生产实证：来源 run
    是 10-27 跑的、10-28 重新固化，回放查的还是 10-26，且窗口本周内逐日变宽、
    下周一（week_start > yesterday）起止倒挂恒空。

    显式传入的 thread 可能不是本任务的（如从交互会话固化）→ 查不到就回落到固化
    时刻，也就是改动前的行为。
    """
    for run in store.list_runs(task_id, limit=20):
        if str(run.get("thread_id") or "") != thread_id:
            continue
        raw = str(run.get("fired_at") or "")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


async def _thread_last_run_time(client: Any, thread_id: str) -> Optional[datetime]:
    """该 thread 最近一次 run 的时刻——本任务没有流水时的回落（交互会话固化）。

    取 ``created_at`` 的最大值：结果载荷总出自这个 thread 上最后一次真跑，
    续跑（continue）不产生新数据。拿不到就返回 None（调用方回落到固化时刻）。
    """
    try:
        runs = await client.runs.list(thread_id=thread_id, limit=10)
    except Exception:  # noqa: BLE001 —— 客户端桩/接口不可用都只是「拿不到时间」
        return None
    best: Optional[datetime] = None
    for run in runs or []:
        raw = str((run or {}).get("created_at") or "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if best is None or dt > best:
            best = dt
    return best


#: 连接器追加在语句**末尾**的那条 ``LIMIT n``（见 ``wren_plan.mirror_connector_limit``）。
_CONNECTOR_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)


def _workspace_root() -> str:
    """活跃工作区磁盘根（``dialect_sql_file`` 是 ``/workspace/…`` 形式的 VFS 指针）。

    包成模块级函数是为了可打桩——工作区根在 import 期就冻住了（见
    workspace_manager 的 data_root 坑），测试不能靠改 env 生效。
    """
    from agent.workspace_manager import get_workspace_manager

    return str(get_workspace_manager().active_workspace)


def _read_plan_sql(ptr: str) -> str:
    """读回 ``/workspace/…`` 指针指向的物理 SQL；读不到返回 ``""``（由调用方定性）。"""
    if not isinstance(ptr, str) or not ptr.startswith("/workspace/"):
        return ""
    try:
        disk = Path(_workspace_root()) / ptr[len("/workspace/"):]
        if disk.is_file():
            return disk.read_text(encoding="utf-8").strip()
        _logger.warning("[pin] 物理 SQL 文件缺失: %s", ptr)
    except Exception as e:  # noqa: BLE001  读盘失败按「读不到」处理
        _logger.warning("[pin] 读物理 SQL 文件失败 %s: %s", ptr, e)
    return ""


def _split_connector_limit(sql: str) -> tuple[str, Optional[int]]:
    """剥掉连接器**无条件**追加的尾部 ``LIMIT n``，返回 ``(SQL, n)``。

    只剥**末尾那一条**：模型自己写的 top-N（``… ORDER BY x LIMIT 10``）在它前一
    行，留下不动（``wren_plan`` 的 ``dup_limit`` 场景）。不剥的后果是每次回放都
    被静默截在 ``n`` 行——而 ``n`` 只是 wren 的探测上限+1，与业务口径无关。
    """
    s = (sql or "").rstrip()
    m = _CONNECTOR_LIMIT_RE.search(s)
    if not m:
        return s, None
    return s[: m.start()].rstrip(), int(m.group(1))


def _last_user_text(messages: list) -> str:
    """来源会话最后一条**真实**用户提问的文本。

    用途只有一个：日期字面量同时命中多个锚点时当消歧证据（``templatize``）。
    「[系统自动通知]」是子任务超时/完成后的续跑注入，不是用户提的问题，跳过。
    """
    from agent.tools.report_builder import _msg_content

    for m in reversed(messages or []):
        if isinstance(m, dict):
            role = m.get("type") or m.get("role")
        else:
            role = getattr(m, "type", "")
        if role not in ("human", "user"):
            continue
        text = (_msg_content(m) or "").strip()
        if not text or text.startswith("[系统自动通知]"):
            continue
        return text
    return ""


def _physical_sql_of(check: dict) -> tuple[str, Optional[int], bool]:
    """check 结果 → 真正下发物理库执行的 SQL。

    Returns: ``(sql, 连接器 LIMIT, 是否存在物理 SQL)``；``sql`` 为空串表示这次
    结果根本没有物理 SQL 可读。

    为什么不能直接用 ``check["sql"]``：语义层库的结果里它是**模型写的 wren 方言
    SQL**（``FROM v_workhour`` 之类是 MDL 虚拟视图，物理库里不存在），回放必炸。
    物理版本由 ``check_progress._attach_physical_plan`` 附上——超过
    ``PLAN_INLINE_MAX``(3000) 字符时**只落盘**、结果里只有指针（线上 2026-10-27
    那次就是 5692 字符、无内联）。所以这里按「内联 > 读盘」解析，两者都拿不到而
    又明确知道存在物理 SQL（``dialect_sql_chars``/``dialect_sql_file``）时报错，
    绝不悄悄退回语义层 SQL。
    """
    if check.get("sql_kind") == "cube":
        return "", None, False
    inline = check.get("dialect_sql")
    inline = inline.strip() if isinstance(inline, str) else ""
    if inline:
        sql, limit_n = _split_connector_limit(inline)
        return sql, limit_n, True
    ptr = check.get("dialect_sql_file") or ""
    from_file = _read_plan_sql(ptr)
    if from_file:
        sql, limit_n = _split_connector_limit(from_file)
        return sql, limit_n, True
    exists = bool(check.get("dialect_sql_chars")) or bool(ptr)
    return "", None, exists


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

    # 物理 SQL 的取回是 I/O ⇒ 留在编排层；`extract_spec` 只认 `dialect_sql`。
    sql, sql_limit, has_physical = _physical_sql_of(check)
    if sql:
        check = {**check, "dialect_sql": sql}
    elif has_physical:
        raise PinError(
            "physical_sql_unreadable",
            "这次结果带的是语义层 SQL（物理版本只落了文件、已读不到）；"
            "任务再跑成功一次就能重新固化",
        )

    version = await semantic_version_of(str(task.get("db_name") or ""))
    # 消歧证据：来源会话的最后一条真实提问；会话里没有（如早期 run）就用任务的
    # 原话——定时 run 的 user 消息本来就是 ``[定时任务] 名字：prompt …``。
    intent_text = _last_user_text(messages) or str(task.get("prompt") or "")
    tz = str(task.get("tz") or "Asia/Shanghai")
    # 锚点按来源 run 当天解析（见 ``_source_run_time``）；本任务没这条流水（交互
    # 会话固化）就看该 thread 的 run 时刻；都拿不到才用固化时刻。
    source_now = _source_run_time(st, task_id, source_thread)
    if source_now is None:
        source_now = await _thread_last_run_time(client, source_thread)
    try:
        spec, warns = extract_spec(
            check,
            db_name=str(task.get("db_name") or ""),
            tz=tz,
            semantic_version=version,
            source_thread_id=source_thread,
            now=source_now or now,
            captured_at=now,
            date_bindings=date_bindings,
            accept_constants=accept_constants,
            intent_text=intent_text,
        )
        if sql_limit:
            # 剥掉的连接器 LIMIT 折算成行上限：回放再补回同一条，行数与来源 run 一致。
            spec["row_limit"] = min(int(spec.get("row_limit") or sql_limit), sql_limit)
    except SpecError as e:
        raise PinError(e.code, e.detail, extra=e.extra) from e
    # 来源不是今天跑的就明说：锚点是按来源那天解析的，固化结果与「今天跑的」不同，
    # 运营看到这句才知道「我点了固化，但用的是昨天的查询」。``warns`` 与
    # ``spec["warnings"]`` 是同一个列表（``extract_spec`` 里同一对象），追加即双写。
    if source_now is not None:
        src_day = source_now.astimezone(ZoneInfo(tz)).date()
        cap_day = now.astimezone(ZoneInfo(tz)).date()
        if src_day != cap_day:
            warns.append(
                f"来源 run 是 {src_day} 跑的（今天 {cap_day}），日期锚点按 {src_day} 解析"
            )
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

    # 这里**不**比对列：``extract_spec`` 出来的 `expect_columns` 是空的（来源
    # 会话里只有模型写给人看的文本，没有驱动返回的列名），拿它比对必然通过——
    # 一度写成 `validate_columns(spec, result.columns)` 是个永远为真的死分支
    # （2026-10-23 评审 I3）。基线就是这次验证执行的列，漂移在**触发时**比。
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
