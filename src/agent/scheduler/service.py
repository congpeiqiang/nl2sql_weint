"""定时任务 service 层：schedule 数学 + 校验 + CRUD。

纪律（hermes 同款）：**API 与聊天工具都只调这一层，"there is NO second job engine"**。
校验规则集中在此，触发路径（worker tick / 手动 run_now）共用同一个 CAS 认领。

时区口径（hermes #51021 的教训）：naive 时间点一律按 ``NL2SQL_SCHEDULER_TZ``
（默认 Asia/Shanghai）解释——按服务器 UTC 解释会错位数小时；**持久化统一 UTC ISO**
（``timespec="seconds"``，与 store 的字符串比较口径一致）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from agent.auth.grants import can_access_db
from agent.scheduler.store import ScheduledTaskStore, get_store

_logger = logging.getLogger(__name__)

DEFAULT_TZ = "Asia/Shanghai"

MAX_TASKS_PER_USER = 20
"""每用户任务数上限（方案 §8 配额）。"""

MAX_PROMPT_CHARS = 4000
MAX_NAME_CHARS = 100

MIN_INTERVAL_MINUTES = 5
"""周期下限（分钟）。E2E 可用 ``NL2SQL_SCHED_MIN_INTERVAL_MIN`` 放宽（dev 1 分钟任务）。"""

KINDS = ("cron", "interval", "once")


class ScheduleError(ValueError):
    """校验/状态错误 —— message 直接面向用户（API 层折成 ``.error``）。"""


def scheduler_tz_name() -> str:
    return os.environ.get("NL2SQL_SCHEDULER_TZ") or DEFAULT_TZ


def min_interval_minutes() -> int:
    try:
        return max(1, int(os.environ.get("NL2SQL_SCHED_MIN_INTERVAL_MIN") or MIN_INTERVAL_MINUTES))
    except ValueError:
        return MIN_INTERVAL_MINUTES


def _tz(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or scheduler_tz_name())
    except Exception as exc:  # 配置错了要能启动（fail 到默认时区）
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:
            raise ScheduleError(f"无效时区: {name!r}") from exc


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _as_local(expr_dt: datetime, tzinfo: ZoneInfo) -> datetime:
    """naive → 按配置时区解释；aware → 原样。"""
    if expr_dt.tzinfo is None:
        return expr_dt.replace(tzinfo=tzinfo)
    return expr_dt


def _parse_once(expr: str, tzinfo: ZoneInfo) -> datetime:
    try:
        raw = datetime.fromisoformat(expr.strip())
    except ValueError as exc:
        raise ScheduleError(f"无效时间点（要 ISO 格式，如 2026-01-02T09:00:00）: {expr!r}") from exc
    return _as_local(raw, tzinfo)


def next_run_after(
    kind: str, expr: str, tz_name: Optional[str] = None, after: Optional[datetime] = None
) -> str:
    """下一次触发时间（UTC ISO）。``once`` 已过期 → ScheduleError。"""
    tzinfo = _tz(tz_name)
    base = (after or _now_utc()).astimezone(tzinfo)
    if kind == "interval":
        minutes = _interval_minutes(expr)
        return _utc_iso(base + timedelta(minutes=minutes))
    if kind == "cron":
        _check_cron(expr)
        nxt = croniter(expr, base).get_next(datetime)
        return _utc_iso(nxt if nxt.tzinfo else _as_local(nxt, tzinfo))
    if kind == "once":
        when = _parse_once(expr, tzinfo)
        if when <= base:
            raise ScheduleError("一次性任务的执行时间必须晚于当前时间")
        return _utc_iso(when)
    raise ScheduleError(f"未知调度类型: {kind!r}（可选 {'/'.join(KINDS)}）")


def _interval_minutes(expr: str) -> int:
    try:
        minutes = int(str(expr).strip())
    except ValueError as exc:
        raise ScheduleError(f"间隔必须是整数分钟: {expr!r}") from exc
    if minutes < min_interval_minutes():
        raise ScheduleError(f"间隔至少 {min_interval_minutes()} 分钟")
    return minutes


def _check_cron(expr: str) -> None:
    expr = str(expr).strip()
    if not expr or not croniter.is_valid(expr):
        raise ScheduleError(f"无效的 cron 表达式: {expr!r}")


def estimate_period_seconds(kind: str, expr: str, tz_name: Optional[str] = None) -> Optional[float]:
    """调度周期估算（cron 取未来两次触发的差值）；once → None。"""
    if kind == "interval":
        return float(_interval_minutes(expr) * 60)
    if kind == "once":
        return None
    _check_cron(expr)
    tzinfo = _tz(tz_name)
    base = _now_utc().astimezone(tzinfo)
    it = croniter(expr, base)
    first = it.get_next(datetime)
    second = it.get_next(datetime)
    if first.tzinfo is None:
        first = _as_local(first, tzinfo)
        second = _as_local(second, tzinfo)
    return (second - first).total_seconds()


def describe_schedule(kind: str, expr: str, tz_name: Optional[str] = None) -> str:
    """人类可读描述（前端只读展示；美化版由前端阶段 2 自己再算）。"""
    tzname = tz_name or scheduler_tz_name()
    if kind == "interval":
        return f"每 {_interval_minutes(expr)} 分钟"
    if kind == "once":
        when = _parse_once(expr, _tz(tzname))
        return f"单次 {when:%Y-%m-%d %H:%M}（{tzname}）"
    if kind == "cron":
        _check_cron(expr)
        m = _humanize_cron(expr)
        return m or f"cron: {expr}"
    raise ScheduleError(f"未知调度类型: {kind!r}")


def _humanize_cron(expr: str) -> Optional[str]:
    """只认最常见三种形态，其余回落到 ``cron: <expr>``（不猜）。"""
    parts = expr.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if not minute.isdigit():
        return None
    if dom != "*" or month != "*":
        return None
    if hour.isdigit() and dow == "*":
        return f"每天 {int(hour):02d}:{int(minute):02d}"
    if hour == "*" and dow == "*":
        return f"每小时第 {int(minute)} 分"
    if hour.isdigit() and dow.isdigit():
        names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        try:
            day = names[int(dow) % 7]
        except ValueError:
            return None
        return f"每{day} {int(hour):02d}:{int(minute):02d}"
    return None


def preview_runs(
    kind: str, expr: str, tz_name: Optional[str] = None, count: int = 3,
    after: Optional[datetime] = None,
) -> list[str]:
    """未来 count 次触发时间（UTC ISO 列表；创建表单实时预览用）。once 只回 1 条。"""
    out: list[str] = []
    cur = after or _now_utc()
    for _ in range(max(1, min(count, 10))):
        if kind == "once":
            out.append(next_run_after(kind, expr, tz_name, after=cur))
            break
        nxt = next_run_after(kind, expr, tz_name, after=cur)
        out.append(nxt)
        cur = datetime.fromisoformat(nxt)
    return out


# ── CRUD（全部带 owner 强制；非 admin 碰别人的任务 → None，API 层折 404）──


def _store(store: Optional[ScheduledTaskStore]) -> ScheduledTaskStore:
    return store if store is not None else get_store()


def _is_admin(user: dict[str, Any]) -> bool:
    return bool(user.get("is_admin"))


def _can_touch(user: dict[str, Any], row: dict[str, Any]) -> bool:
    return _is_admin(user) or row.get("owner") == user.get("user_id")


def build_row(user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """校验 + 计算派生字段，返回可直接 insert 的行（不落库）。"""
    name = str(payload.get("name") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    db_name = str(payload.get("db_name") or "").strip()
    kind = str(payload.get("sched_kind") or "").strip()
    expr = str(payload.get("sched_expr") or "").strip()
    tz_name = str(payload.get("tz") or "").strip() or scheduler_tz_name()

    if not name or len(name) > MAX_NAME_CHARS:
        raise ScheduleError(f"任务名不能为空且不超过 {MAX_NAME_CHARS} 字")
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise ScheduleError(f"问数内容不能为空且不超过 {MAX_PROMPT_CHARS} 字")
    if not db_name:
        raise ScheduleError("必须指定目标数据库")
    if kind not in KINDS:
        raise ScheduleError(f"未知调度类型: {kind!r}（可选 {'/'.join(KINDS)}）")
    # 库权限：创建期按用户 grants 校验（触发前 worker 还会再验一次，fail-closed）
    if not can_access_db(user, db_name):
        raise ScheduleError(f"无权访问数据库: {db_name}")

    if kind == "interval":
        _interval_minutes(expr)
    elif kind == "cron":
        _check_cron(expr)
        period = estimate_period_seconds(kind, expr, tz_name)
        floor = min_interval_minutes() * 60
        if period is not None and period < floor:
            raise ScheduleError(f"周期至少 {min_interval_minutes()} 分钟（cron 触发过密）")
    next_run = next_run_after(kind, expr, tz_name)  # once 过期在这抛

    repeat_times = payload.get("repeat_times")
    if repeat_times is not None:
        try:
            repeat_times = int(repeat_times)
        except (TypeError, ValueError) as exc:
            raise ScheduleError(f"repeat_times 必须是整数: {repeat_times!r}") from exc
        if repeat_times < 1:
            raise ScheduleError("repeat_times 至少为 1")

    return {
        "owner": str(user.get("user_id") or ""),
        "name": name,
        "prompt": prompt,
        "db_name": db_name,
        "llm_route": str(payload.get("llm_route") or "").strip(),
        "llm_model": str(payload.get("llm_model") or "").strip(),
        "sched_kind": kind,
        "sched_expr": expr,
        "sched_display": describe_schedule(kind, expr, tz_name),
        "tz": tz_name,
        "next_run_at": next_run,
        "repeat_times": repeat_times,
        "origin_thread_id": str(payload.get("origin_thread_id") or "").strip(),
    }


def create_task(
    user: dict[str, Any], payload: dict[str, Any], store: Optional[ScheduledTaskStore] = None
) -> dict[str, Any]:
    st = _store(store)
    owner = str(user.get("user_id") or "")
    if not owner:
        raise ScheduleError("缺少用户身份")
    if st.count_by_owner(owner) >= MAX_TASKS_PER_USER:
        raise ScheduleError(f"每人最多 {MAX_TASKS_PER_USER} 个定时任务")
    row = build_row(user, payload)
    tid = st.insert_task(row)
    return st.get_task(tid) or {}


def get_task_for_user(
    user: dict[str, Any], task_id: str, store: Optional[ScheduledTaskStore] = None
) -> Optional[dict[str, Any]]:
    row = _store(store).get_task(task_id)
    if row is None or not _can_touch(user, row):
        return None
    return row


def list_tasks(
    user: dict[str, Any], show_all: bool = False, store: Optional[ScheduledTaskStore] = None
) -> list[dict[str, Any]]:
    st = _store(store)
    if show_all and _is_admin(user):
        return st.list_tasks(owner=None)
    return st.list_tasks(owner=str(user.get("user_id") or ""))


_EDITABLE = ("name", "prompt", "db_name", "llm_route", "llm_model", "repeat_times")
_RESCHEDULING = ("sched_kind", "sched_expr", "tz")


def update_task(
    user: dict[str, Any], task_id: str, payload: dict[str, Any],
    store: Optional[ScheduledTaskStore] = None,
) -> Optional[dict[str, Any]]:
    """改名/改 prompt/改库/改周期。改周期字段 → 重校验 + 重算 next_run_at（不补历史）。"""
    st = _store(store)
    row = st.get_task(task_id)
    if row is None or not _can_touch(user, row):
        return None
    fields: dict[str, Any] = {}
    for key in _EDITABLE:
        if key in payload:
            value = payload[key]
            if key in ("name", "prompt"):
                value = str(value or "").strip()
                limit = MAX_NAME_CHARS if key == "name" else MAX_PROMPT_CHARS
                if not value or len(value) > limit:
                    raise ScheduleError(f"{key} 不能为空且不超过 {limit} 字")
            if key == "db_name":
                value = str(value or "").strip()
                if not can_access_db(user, value):
                    raise ScheduleError(f"无权访问数据库: {value}")
            if key == "repeat_times" and value is not None:
                value = int(value)
                if value < 1:
                    raise ScheduleError("repeat_times 至少为 1")
            fields[key] = value

    if any(k in payload for k in _RESCHEDULING):
        kind = str(payload.get("sched_kind") or row["sched_kind"]).strip()
        expr = str(payload.get("sched_expr") if "sched_expr" in payload else row["sched_expr"]).strip()
        tz_name = str(payload.get("tz") or row["tz"]).strip() or scheduler_tz_name()
        if kind not in KINDS:
            raise ScheduleError(f"未知调度类型: {kind!r}")
        if kind == "interval":
            _interval_minutes(expr)
        elif kind == "cron":
            _check_cron(expr)
        merged = {**row, **fields, "sched_kind": kind, "sched_expr": expr, "tz": tz_name}
        # 复用 build_row 的全套校验（把已有行伪装成 payload）
        rebuilt = build_row(user, merged)
        fields.update({
            "sched_kind": kind,
            "sched_expr": expr,
            "tz": tz_name,
            "sched_display": rebuilt["sched_display"],
        })
        st.update_task(task_id, fields)
        # 改周期只重排 next_run_at，**不动 state / enabled**（review I-3）：paused
        # 任务被编辑不该隐式恢复 —— 翻成 scheduled 而 enabled 仍是 0 会造出 due
        # 选取看不见、resume 又拒（state≠paused）的死态。next_run_at 先写着供展示，
        # resume 会从「现在」重算并重新 enable。
        st.reschedule(task_id, rebuilt["next_run_at"])
    elif fields:
        st.update_task(task_id, fields)

    # 换库必须解绑固化：回放执行的是 spec 里记的**旧库**，而触发前的权限复验
    # 验的是任务行的**新库** ⇒ 既可能拿旧库数据冒充新库报表，也等于绕开授权。
    old_db = str(row.get("db_name") or "")
    new_db = str(fields.get("db_name") or old_db)
    if "db_name" in fields and new_db != old_db:
        st.clear_pin(task_id, f"db_changed: {old_db} -> {new_db}", _utc_iso(_now_utc()))
    return st.get_task(task_id)


def pause_task(
    user: dict[str, Any], task_id: str, store: Optional[ScheduledTaskStore] = None
) -> Optional[dict[str, Any]]:
    st = _store(store)
    row = st.get_task(task_id)
    if row is None or not _can_touch(user, row):
        return None
    if row["state"] not in ("scheduled", "paused"):
        raise ScheduleError(f"当前状态（{row['state']}）不能暂停")
    st.update_task(task_id, {"enabled": 0, "state": "paused"})
    return st.get_task(task_id)


def resume_task(
    user: dict[str, Any], task_id: str, store: Optional[ScheduledTaskStore] = None
) -> Optional[dict[str, Any]]:
    """恢复：next_run_at 从「现在」重算 —— 不补历史（hermes resume 同款取舍）。"""
    st = _store(store)
    row = st.get_task(task_id)
    if row is None or not _can_touch(user, row):
        return None
    if row["state"] != "paused":
        raise ScheduleError(f"当前状态（{row['state']}）不能恢复")
    if not can_access_db(user, row["db_name"]) and not _is_admin(user):
        raise ScheduleError(f"无权访问数据库: {row['db_name']}（授权可能已回收）")
    if row["sched_kind"] == "once":
        # once 过期则拒绝恢复（不静默改时间）
        when = _parse_once(row["sched_expr"], _tz(row["tz"]))
        if when <= _now_utc():
            raise ScheduleError("一次性任务的执行时间已过，无法恢复（请改建新任务）")
        next_run = _utc_iso(when)
    else:
        next_run = next_run_after(row["sched_kind"], row["sched_expr"], row["tz"])
    st.update_task(task_id, {"enabled": 1})
    st.reschedule(task_id, next_run, state="scheduled")
    return st.get_task(task_id)


def delete_task(
    user: dict[str, Any], task_id: str, store: Optional[ScheduledTaskStore] = None
) -> bool:
    st = _store(store)
    row = st.get_task(task_id)
    if row is None or not _can_touch(user, row):
        return False
    # 在飞 run 不打断（方案 §5）：reconcile 收尾时按 task_id 找不到行自然作废
    return st.delete_task(task_id)


def run_now(
    user: dict[str, Any], task_id: str, store: Optional[ScheduledTaskStore] = None
) -> dict[str, Any]:
    """立即运行一次 —— 与 tick 走**同一个 CAS 认领**，双路触发天然互斥。"""
    st = _store(store)
    row = st.get_task(task_id)
    if row is None or not _can_touch(user, row):
        raise ScheduleError("任务不存在")
    if row["running_thread_id"]:
        raise ScheduleError("任务正在执行中，请等本次运行结束")
    if row["state"] != "scheduled":
        raise ScheduleError(f"当前状态（{row['state']}）不能立即运行")
    now = _now_utc()
    expected = row["next_run_at"]
    if not expected:
        raise ScheduleError("任务没有下一次触发时间（可能已完成）")
    new_next = "" if row["sched_kind"] == "once" else next_run_after(
        row["sched_kind"], row["sched_expr"], row["tz"], after=now
    )
    if not st.claim(task_id, expected, new_next, _utc_iso(now)):
        raise ScheduleError("任务刚被另一路触发认领（tick/手动竞争），请稍后再试")
    submit_fire(task_id)
    return st.get_task(task_id) or {}


def submit_fire(task_id: str) -> None:
    """转发到 worker 的执行队列（间接层：单测 monkeypatch 此函数；懒 import 防循环依赖）。"""
    from agent.scheduler import worker

    worker.submit_fire(task_id)


def recheck_db_access(owner: str, db_name: str) -> bool:
    """触发前复验库权限（授权可能已被回收）。

    身份还原（review I-2 修正：此前硬编码 is_admin=False —— admin 通常没有显式
    grants 行，硬编码会让 admin 名下的任务每次触发都白记「无权访问」，dev-34 的
    dev 旁路用户同理全灭）：

    - 非真实用户（``dev`` 旁路 / ``internal``）→ **不启用**按用户判权，直接放行
      （与 ``auth/runtime.resolve_caller`` 的 fail-open 边界同款：开发旁路与容器
      内部调用本来就没有用户维度）；
    - 真实用户 → 查 ``users.json`` 还原 is_admin 再进 grants；**账号已删** →
      is_admin=False + grants 空集 = 拒（fail-closed：删除是结论，不是故障）；
    - 读 users.json / grants **抛异常** → 放行 + warning（``caller_can_access_db``
      同款取舍：基础设施故障不该打死所有定时任务；「读到了、就是没授权」才拒绝）。
    """
    from agent.auth.ownership import is_real_owner

    if not is_real_owner(owner):
        return True
    try:
        from agent.auth.users import find_user

        rec = find_user(owner)
        return can_access_db(
            {"user_id": owner, "is_admin": bool((rec or {}).get("is_admin"))}, db_name
        )
    except Exception:  # noqa: BLE001  读故障 fail-open（口径见 docstring）
        _logger.warning(
            "[sched] recheck_db_access 读取身份/授权失败（owner=%s db=%s），本次放行",
            owner, db_name, exc_info=True,
        )
        return True


def list_runs(
    user: dict[str, Any], task_id: str, limit: int = 30,
    store: Optional[ScheduledTaskStore] = None,
) -> Optional[list[dict[str, Any]]]:
    st = _store(store)
    row = st.get_task(task_id)
    if row is None or not _can_touch(user, row):
        return None
    return st.list_runs(task_id, limit=limit)
