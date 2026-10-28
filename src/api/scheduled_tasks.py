"""定时任务管理 API（方案 §5）。

契约：
- 成功 ``{"ok": true, "task"/"tasks"/"runs": ...}``；
- 失败 ``{"ok": false, "error": <用户可读消息>}`` —— **403/400/404 必须「返回」
  JSONResponse 不能「抛」**（langgraph 会把异常折成 ``{"detail":…}``，前端只认
  ``.error``；admin_guard 同款教训）。
- owner 隔离在 service 层强制：非 admin 碰别人的任务一律按 404 处理（不泄露存在性）。
- 校验/状态错误（ScheduleError）→ 400。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from agent.scheduler import pin_service, service
from agent.scheduler.service import ScheduleError
from api._common import get_user, json_response, parse_body

_logger = logging.getLogger(__name__)

#: 固化失效码 → 用户可读提示。没列到的码回落成 ``code: detail``（机读优先，
#: 前端弹窗原样展示也说得过去）。这些 message 会直接出现在前端对话框里。
_PIN_HINT = {
    # 这些文案要落在**用户界面上真能做的动作**上：date_bindings 是 API 参数、
    # 界面上没有对应字段，让用户「去指定 date_bindings」等于让他改一个他看不到
    # 的东西（线上 2026-10-27 的原提示就是这么写的）。歧义的正解是弹选择框，
    # 候选锚点走 `extra.candidates`。
    "ambiguous_date": "日期歧义：SQL 里的同一个日期同时对应多个口径锚点，请选择这条报表要按哪个口径（选定后按该口径重新固化）",
    "unmatched_date": "SQL 里有对不上任何日期锚点的字面量。若它确实是业务常量，点「保留常量重试」；否则请先在会话里改成相对日期再固化",
    "physical_sql_unreadable": "这次结果里只有语义层 SQL（可执行的物理版本没能读回来）。让任务再成功跑一次后重新固化即可",
    "no_source_run": "该任务还没有成功执行过一次，没有可固化的查询",
    "no_check_result": "来源会话里没有成功的查询结果（check_async_task status=success）",
    "columns_changed": "本次执行返回的列与固化时不一致（口径或表结构变了）",
    "not_semantic_db": "该库已不是语义库（或语义项目已被移除），cube 通道无法回放",
    "cube_compile_failed": "cube 定义编译失败（语义库改了？）",
    "exec_timeout": "验证执行超时",
    "sql_failed": "验证执行失败",
    "spec_version_unknown": "固化格式版本不认识（服务已升级，请重新固化）",
    "render_failed": "固化模板渲染失败（spec 被改坏了？）",
    "cube_key_unsupported": "cube 查询定义里有无法固化的键（limit/offset/granularity 等）",
    "no_query": "来源结果里既没有物理 SQL 也没有 cube 定义",
    "bad_binding": "所选日期口径与当天日期对不上（锚点取值已变），请重新固化",
}


def _pin_error_text(e: Exception) -> str:
    code = getattr(e, "code", "")
    detail = getattr(e, "detail", "")
    hint = _PIN_HINT.get(code)
    if hint and detail:
        return f"{hint}（{detail}）"
    return hint or (f"{code}: {detail}" if detail else str(e))


def _err(
    msg: str, status: int = 400, code: str = "", extra: dict | None = None
) -> JSONResponse:
    """``code``/``extra`` 只在有值时出现（机读失效码与补充数据），``error`` 契约不变。"""
    body: dict[str, Any] = {"ok": False, "error": msg}
    if code:
        body["code"] = code
    if extra:
        body["extra"] = extra
    return json_response(body, status=status)


def _user(request: Request) -> dict[str, Any] | JSONResponse:
    user = get_user(request)
    if not user:
        return _err("未登录", 401)
    return user


async def list_tasks_api(request: Request) -> JSONResponse:
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    show_all = request.query_params.get("all") == "1"
    try:
        tasks = service.list_tasks(user, show_all=show_all)
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 列表失败")
        return _err(f"读取任务列表失败: {e}", 500)
    return json_response({"ok": True, "tasks": tasks})


async def create_task_api(request: Request) -> JSONResponse:
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    body = await parse_body(request)
    try:
        task = service.create_task(user, body)
    except ScheduleError as e:
        return _err(str(e), 400)
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 创建失败")
        return _err(f"创建任务失败: {e}", 500)
    return json_response({"ok": True, "task": task})


async def patch_task_api(request: Request) -> JSONResponse:
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    task_id = request.path_params["task_id"]
    body = await parse_body(request)
    action = str(body.pop("action", "") or "")
    try:
        if action == "pause":
            task = service.pause_task(user, task_id)
        elif action == "resume":
            task = service.resume_task(user, task_id)
        elif action:
            return _err(f"未知 action: {action}（可选 pause/resume，或直接传要改的字段）")
        else:
            task = service.update_task(user, task_id, body)
    except ScheduleError as e:
        return _err(str(e), 400)
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 修改失败 id=%s", task_id)
        return _err(f"修改任务失败: {e}", 500)
    if task is None:
        return _err("任务不存在", 404)
    return json_response({"ok": True, "task": task})


async def delete_task_api(request: Request) -> JSONResponse:
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    task_id = request.path_params["task_id"]
    try:
        ok = service.delete_task(user, task_id)
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 删除失败 id=%s", task_id)
        return _err(f"删除任务失败: {e}", 500)
    if not ok:
        return _err("任务不存在", 404)
    return json_response({"ok": True})


async def run_task_api(request: Request) -> JSONResponse:
    """立即运行一次 —— 与 tick 走同一个 CAS 认领，双路触发天然互斥。"""
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    task_id = request.path_params["task_id"]
    try:
        task = service.run_now(user, task_id)
    except ScheduleError as e:
        msg = str(e)
        return _err(msg, 404 if msg == "任务不存在" else 400)
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 立即运行失败 id=%s", task_id)
        return _err(f"立即运行失败: {e}", 500)
    return json_response({"ok": True, "task": task})


async def list_runs_api(request: Request) -> JSONResponse:
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    task_id = request.path_params["task_id"]
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", 30))))
    except ValueError:
        limit = 30
    try:
        runs = service.list_runs(user, task_id, limit=limit)
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 运行历史失败 id=%s", task_id)
        return _err(f"读取运行历史失败: {e}", 500)
    if runs is None:
        return _err("任务不存在", 404)
    return json_response({"ok": True, "runs": runs})


async def preview_task_api(request: Request) -> JSONResponse:
    """创建/编辑表单的实时预览（方案 §6 高级模式）：算未来几次触发时间 + 人类可读
    描述，不落库不建任务。cron 数学只在后端做（croniter + tz），前端不装第二套。"""
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    body = await parse_body(request)
    try:
        kind = str(body.get("sched_kind") or "").strip()
        expr = str(body.get("sched_expr") or "").strip()
        tz_name = str(body.get("tz") or "").strip() or None
        try:
            count = max(1, min(10, int(body.get("count", 3))))
        except (TypeError, ValueError):
            count = 3
        runs = service.preview_runs(kind, expr, tz_name, count=count)
        display = service.describe_schedule(kind, expr, tz_name)
    except ScheduleError as e:
        return _err(str(e), 400)
    except Exception as e:  # noqa: BLE001  含 body 非 dict（.get AttributeError）—— 不逃逸 .error 契约
        _logger.exception("[sched-api] 预览失败")
        return _err(f"预览失败: {e}", 500)
    return json_response({"ok": True, "runs": runs, "sched_display": display})


async def pin_task_api(request: Request) -> JSONResponse:
    """固化：把**跑对过的那次**查询存成模板（POST body 可带 thread_id/date_bindings）。"""
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    task_id = request.path_params["task_id"]
    task = service.get_task_for_user(user, task_id)
    if task is None:
        return _err("任务不存在", 404)
    # 固化会**真跑一次查询**：授权可能已被回收，与 fire 前同样要复验（fail-closed）。
    if not service.recheck_db_access(task["owner"], task["db_name"]):
        return _err(f"无权访问数据库: {task['db_name']}（授权已回收）", 403)

    body = await parse_body(request)
    source_thread = str(body.get("thread_id") or "").strip()
    if source_thread:
        # `pin_task` 走内部自调用读该 thread 的状态（不带 cookie），handler 是唯一
        # 能判归属的地方。不校验 ⇒ 知道别人 thread_id 就能把别人的查询固化进自己
        # 的任务，`preview_sql` 还会把别人的 SQL 原文回给人（跨用户 IDOR）。
        from agent.auth.grants import owned_thread

        if not owned_thread(user, source_thread):
            return _err("无权访问该会话", 403)
    try:
        res = await pin_service.pin_task(
            task,
            thread_id=(source_thread or None),
            date_bindings=body.get("date_bindings") or None,
            accept_constants=bool(body.get("accept_constants")),
        )
    except pin_service.PinError as e:
        return _err(
            _pin_error_text(e),
            400,
            code=str(getattr(e, "code", "") or ""),
            extra=getattr(e, "extra", None),
        )
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 固化失败 id=%s", task_id)
        return _err(f"固化失败: {e}", 500)

    fresh = service.get_task_for_user(user, task_id) or task
    pin = dict(res.get("pin") or {})
    pin.setdefault("channel", res.get("channel", ""))
    pin.setdefault("preview_sql", res.get("preview_sql", ""))
    pin.setdefault("params", res.get("params") or {})
    pin.setdefault("warnings", res.get("warnings") or [])
    return json_response({"ok": True, "task": fresh, "pin": pin})


async def unpin_task_api(request: Request) -> JSONResponse:
    """解绑固化：任务回到正常 agent 执行（下次触发重新让模型写 SQL）。"""
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    task_id = request.path_params["task_id"]
    task = service.get_task_for_user(user, task_id)
    if task is None:
        return _err("任务不存在", 404)
    try:
        await pin_service.clear(task_id, "user_unpin: 用户手动解绑固化")
    except Exception as e:  # noqa: BLE001
        _logger.exception("[sched-api] 解绑固化失败 id=%s", task_id)
        return _err(f"解绑固化失败: {e}", 500)
    fresh = service.get_task_for_user(user, task_id) or task
    return json_response({"ok": True, "task": fresh})


async def get_pin_api(request: Request) -> JSONResponse:
    """当前固化状态 + spec（无固化时 ``pin: null``）。"""
    user = _user(request)
    if isinstance(user, JSONResponse):
        return user
    task_id = request.path_params["task_id"]
    task = service.get_task_for_user(user, task_id)
    if task is None:
        return _err("任务不存在", 404)

    status = str(task.get("pin_status") or "")
    spec_text = str(task.get("pin_spec") or "")
    if not status or not spec_text:
        return json_response({"ok": True, "pin": None})
    try:
        spec = json.loads(spec_text)
    except (json.JSONDecodeError, ValueError):
        spec = {}
    return json_response(
        {
            "ok": True,
            "pin": {
                "status": status,
                "semantic_version": task.get("pin_semantic_version", ""),
                "saved_at": task.get("pin_saved_at", ""),
                "invalid_reason": task.get("pin_invalid_reason", ""),
                "source_thread_id": task.get("pin_source_thread_id", ""),
                "spec": spec,
            },
        }
    )


routes: list[BaseRoute] = [
    Route("/api/scheduled-tasks", list_tasks_api, methods=["GET"]),
    Route("/api/scheduled-tasks", create_task_api, methods=["POST"]),
    Route("/api/scheduled-tasks/preview", preview_task_api, methods=["POST"]),
    Route("/api/scheduled-tasks/{task_id}", patch_task_api, methods=["PATCH"]),
    Route("/api/scheduled-tasks/{task_id}", delete_task_api, methods=["DELETE"]),
    Route("/api/scheduled-tasks/{task_id}/run", run_task_api, methods=["POST"]),
    Route("/api/scheduled-tasks/{task_id}/runs", list_runs_api, methods=["GET"]),
    Route("/api/scheduled-tasks/{task_id}/pin", get_pin_api, methods=["GET"]),
    Route("/api/scheduled-tasks/{task_id}/pin", pin_task_api, methods=["POST"]),
    Route("/api/scheduled-tasks/{task_id}/pin", unpin_task_api, methods=["DELETE"]),
]
