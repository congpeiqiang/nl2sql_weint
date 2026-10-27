"""主 agent 聊天工具：manage_scheduled_task（单工具多 action，方案 §7）。

纪律：
- **本模块禁止 `from __future__ import annotations`** —— 字符串化注解会让
  ``ToolRuntime`` 注入静默失效（判据 ``'runtime' in tool._injected_args_keys``；
  2026-09-30 生产事故，详见 attribution_analyze.py:63-77 的机理注释）。
- **NO second job engine**：全部动作只调 ``agent.scheduler.service``（与 REST
  API 同一实现层），本文件只做身份解析、参数整形和机器可读的结果封装。
- 返回紧凑 JSON 字符串（任务列表不带 prompt 全文，20 任务上限下远低于
  MessageSlimmer 8000 字符截断线，无需豁免）。
"""
import json
import logging
from typing import Annotated, Any, Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

from agent.scheduler import service
from agent.scheduler.service import ScheduleError
from agent.scheduler.store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

TOOL_NAME = "manage_scheduled_task"

ACTIONS = ("create", "list", "pause", "resume", "remove", "run_now", "runs")


class ManageScheduledTaskSchema(BaseModel):
    action: str = Field(
        description=(
            "操作类型：create=创建定时任务 / list=列出我的任务 / pause=暂停 / "
            "resume=恢复 / remove=删除 / run_now=立即运行一次 / runs=查运行历史"
        ),
        json_schema_extra={"enum": list(ACTIONS)},
    )
    task_id: Optional[str] = Field(
        default=None,
        description="任务 id（pause/resume/remove/run_now/runs 必填；create 成功后返回）",
    )
    name: Optional[str] = Field(default=None, description="任务名（create 必填，≤100 字）")
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "自包含的问数内容（create 必填，≤4000 字）。定时执行时是全新会话、"
            "没有当前聊天上下文 —— 必须写完整（统计口径、时间范围、要不要图表/报告）"
        ),
    )
    db_name: Optional[str] = Field(
        default=None, description="目标数据库名（create 缺省用当前会话的数据库）"
    )
    sched_kind: Optional[str] = Field(
        default=None,
        description="调度类型：cron / interval（固定间隔分钟数）/ once（一次性）",
        json_schema_extra={"enum": ["cron", "interval", "once"]},
    )
    sched_expr: Optional[str] = Field(
        default=None,
        description=(
            "调度表达式：cron=5 字段表达式（如 `0 9 * * *`=每天09:00）；"
            "interval=分钟数（≥5）；once=ISO 时间点（如 2026-01-02T09:00:00，按北京时间解释）"
        ),
    )
    repeat_times: Optional[int] = Field(
        default=None, description="最多执行次数（缺省=不限次；到次自动完成）"
    )


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    """任务行的紧凑视图（列表/详情共用；不回传 prompt 全文防截断）。"""
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "db_name": row.get("db_name"),
        "sched_kind": row.get("sched_kind"),
        "sched_expr": row.get("sched_expr"),
        "sched_display": row.get("sched_display"),
        "state": row.get("state"),
        "enabled": row.get("enabled"),
        "next_run_at": row.get("next_run_at"),
        "last_run_at": row.get("last_run_at"),
        "last_status": row.get("last_status"),
        "last_error": (row.get("last_error") or "")[:200],
        "pin_status": row.get("pin_status") or "",
        "pin_invalid_reason": (row.get("pin_invalid_reason") or "")[:200],
        "running": bool(row.get("running_thread_id")),
        "repeat_times": row.get("repeat_times"),
        "repeat_completed": row.get("repeat_completed"),
    }


def _ok(**kw: Any) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False, default=str)


def _fail(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False, default=str)


def manage(
    action: str,
    user: Optional[dict[str, Any]],
    payload: dict[str, Any],
    thread_id: str = "",
    store: Optional[ScheduledTaskStore] = None,
) -> str:
    """纯分发层（可单测）：action → service 调用 → 机器可读 JSON 字符串。"""
    if not user or not user.get("user_id"):
        return _fail("缺少用户身份，无法管理定时任务")
    try:
        if action == "create":
            data = dict(payload or {})
            if thread_id:
                data["origin_thread_id"] = thread_id
            task = service.create_task(user, data, store=store)
            return _ok(
                task=_brief(task),
                hint="请向用户复述：任务名、周期（sched_display）与下次运行时间"
                "（next_run_at 是 UTC，回复用户时换算成北京时间）；结果将出现在"
                "「[定时] 任务名」新会话里",
            )
        if action == "list":
            tasks = service.list_tasks(user, store=store)
            return _ok(tasks=[_brief(t) for t in tasks])
        if action not in ACTIONS:
            return _fail(f"未知 action: {action!r}（可选 {'/'.join(ACTIONS)}）")
        task_id = str((payload or {}).get("task_id") or "")
        if not task_id:
            return _fail("缺少 task_id（可从 list 的结果里拿）")
        if action == "pause":
            task = service.pause_task(user, task_id, store=store)
        elif action == "resume":
            task = service.resume_task(user, task_id, store=store)
        elif action == "remove":
            removed = service.delete_task(user, task_id, store=store)
            return _ok(removed=task_id) if removed else _fail("任务不存在")
        elif action == "run_now":
            task = service.run_now(user, task_id, store=store)
        elif action == "runs":
            runs = service.list_runs(user, task_id, limit=20, store=store)
            if runs is None:
                return _fail("任务不存在")
            return _ok(runs=[
                {
                    "thread_id": r.get("thread_id"),
                    "fired_at": r.get("fired_at"),
                    "finished_at": r.get("finished_at"),
                    "status": r.get("status"),
                    "error": (r.get("error") or "")[:200],
                }
                for r in runs
            ])
        else:  # 防御分支（action 已在上面闸过，正常到不了）
            return _fail(f"未知 action: {action!r}（可选 {'/'.join(ACTIONS)}）")
        if task is None:
            return _fail("任务不存在")
        return _ok(task=_brief(task))
    except ScheduleError as e:
        return _fail(str(e))
    except Exception as e:  # noqa: BLE001  工具永不抛（attribution 同款纪律）
        _logger.exception("[sched-tool] action=%s 失败", action)
        return _fail(f"定时任务操作失败: {e}")


# ── ToolRuntime 侧：身份 / db / thread 解析（house style：get_config）──────


def _resolve_user() -> Optional[dict[str, Any]]:
    """resolve_caller 优先（auth 上下文，is_admin 可信）；退回 configurable.user_id。

    `internal` 不接受（不是真实用户，归属会烂掉）；`dev` 接受 —— 与 REST 的
    NL2SQL_AUTH_DISABLED 旁路口径一致（auth_middleware 给的就是 user_id=dev）。
    """
    try:
        from agent.auth.runtime import resolve_caller

        user = resolve_caller("manage_scheduled_task")
        if user and user.get("user_id"):
            return user
    except Exception:  # noqa: BLE001
        pass
    try:
        from langgraph.config import get_config

        cfg = get_config().get("configurable", {}) or {}
        uid = str(cfg.get("user_id") or cfg.get("langgraph_auth_user_id") or "")
        if uid and uid != "internal":
            return {"user_id": uid, "is_admin": False}
    except Exception:  # noqa: BLE001  不在 run 上下文里（脚本/单测直调）
        pass
    return None


def _resolve_thread_id() -> str:
    try:
        from langgraph.config import get_config

        cfg = get_config().get("configurable", {}) or {}
        return str(cfg.get("thread_id") or "")
    except Exception:  # noqa: BLE001
        return ""


def _resolve_db_name() -> str:
    """create 缺省 db：复用 report_builder 的三级解析（configurable → 会话台账）。

    主 agent 常跑在手-built 续跑 run 里、configurable 只有 user_id 没有 db_name
    （2026-09-26 生产事故）—— _current_db_name 已处理这个坑。
    """
    try:
        from agent.tools.report_builder import _current_db_name

        return _current_db_name() or ""
    except Exception:  # noqa: BLE001
        return ""


async def _manage_scheduled_task_coro(
    runtime: Annotated[ToolRuntime, InjectedToolArg()],
    action: str,
    task_id: Optional[str] = None,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    db_name: Optional[str] = None,
    sched_kind: Optional[str] = None,
    sched_expr: Optional[str] = None,
    repeat_times: Optional[int] = None,
) -> str:
    user = _resolve_user()
    thread_id = _resolve_thread_id()
    payload: dict[str, Any] = {}
    for key, value in (
        ("task_id", task_id), ("name", name), ("prompt", prompt),
        ("db_name", db_name), ("sched_kind", sched_kind),
        ("sched_expr", sched_expr), ("repeat_times", repeat_times),
    ):
        if value is not None and value != "":
            payload[key] = value
    if action == "create" and not payload.get("db_name"):
        payload["db_name"] = _resolve_db_name()
    return manage(action, user, payload, thread_id=thread_id)


_DESCRIPTION = (
    "管理当前用户的定时任务（到点自动以该用户身份发起一次问数，结果落在"
    "「[定时] 任务名」新会话里）。action：create（创建，需要 name/prompt/"
    "sched_kind/sched_expr，db_name 缺省用当前库）、list、pause、resume、"
    "remove、run_now（立即运行一次）、runs（运行历史）。"
    "调度表达式：cron 5 字段（`0 9 * * *`=每天09:00，北京时间）/ interval 分钟数"
    "（≥5）/ once ISO 时间点。prompt 必须自包含 —— 定时执行时没有当前聊天上下文。"
    "创建成功后必须向用户复述 sched_display 与下次运行时间（换算成北京时间）。"
    "返回 JSON：{\"ok\":true,...} 或 {\"ok\":false,\"error\":...}。"
)

manage_scheduled_task_tool = StructuredTool.from_function(
    coroutine=_manage_scheduled_task_coro,
    name=TOOL_NAME,
    description=_DESCRIPTION,
    args_schema=ManageScheduledTaskSchema,
)
