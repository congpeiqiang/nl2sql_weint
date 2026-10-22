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

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from agent.scheduler import service
from agent.scheduler.service import ScheduleError
from api._common import get_user, json_response, parse_body

_logger = logging.getLogger(__name__)


def _err(msg: str, status: int = 400) -> JSONResponse:
    return json_response({"ok": False, "error": msg}, status=status)


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


routes: list[BaseRoute] = [
    Route("/api/scheduled-tasks", list_tasks_api, methods=["GET"]),
    Route("/api/scheduled-tasks", create_task_api, methods=["POST"]),
    Route("/api/scheduled-tasks/preview", preview_task_api, methods=["POST"]),
    Route("/api/scheduled-tasks/{task_id}", patch_task_api, methods=["PATCH"]),
    Route("/api/scheduled-tasks/{task_id}", delete_task_api, methods=["DELETE"]),
    Route("/api/scheduled-tasks/{task_id}/run", run_task_api, methods=["POST"]),
    Route("/api/scheduled-tasks/{task_id}/runs", list_runs_api, methods=["GET"]),
]
