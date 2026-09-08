"""工作区管理 API 路由（随 langgraph API 同进程/同端口提供）。

路由：
    GET    /api/workspaces              列表（含活跃标记）
    POST   /api/workspaces              注册工作区（需目录路径）
    PUT    /api/workspaces/{name}/activate  切换活跃工作区（即时生效）
    DELETE /api/workspaces/{name}       取消注册（默认不删文件）
    DELETE /api/workspaces/{name}?delete_files=1   彻底删除（含服务器目录，不可恢复）
    GET    /api/workspaces/active       获取当前活跃工作区信息
"""
from __future__ import annotations

import logging
from pathlib import Path

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)


def _validate_workspace_path(path: str) -> Path:
    """校验工作区路径。

    不存在时放行（注册时由 register_workspace 自动创建目录）；
    已存在但非目录（是文件）时拒绝，避免把文件当工作区目录。
    """
    p = Path(path).resolve()
    if p.exists() and not p.is_dir():
        raise ValueError(f"路径已存在但不是目录: {path}")
    return p


# ── 路由 ─────────────────────────────────────────────────


async def list_workspaces(request: Request):
    """列出所有已注册工作区。"""
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    items = wm.list_workspaces()
    return json_response({"workspaces": items, "active": wm.active_name})


async def register_workspace(request: Request):
    """注册新工作区。

    Body: {name: "project-a", path: "D:/workspaces/project-a", display_name?: "项目A"}
    """
    data = await parse_body(request)
    name = str(data.get("name", "") or "").strip()
    path = str(data.get("path", "") or "").strip()
    display_name = str(data.get("display_name", "") or "").strip()

    if not name:
        return json_response({"error": "name 必填"}, status=400)
    if not path:
        return json_response({"error": "path 必填"}, status=400)
    # 目录名 sanitize（只允许字母数字、中文、下划线、连字符）
    import re

    if not re.match(r"^[\w\u4e00-\u9fff-]+$", name):
        return json_response({"error": "name 只能包含字母、数字、中文、下划线、连字符"}, status=400)

    try:
        _validate_workspace_path(path)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)

    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    try:
        result = wm.register_workspace(name, path, display_name)
        return json_response({"ok": True, "workspace": result})
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)


async def activate_workspace(request: Request):
    """切换活跃工作区（即时生效）。"""
    name = request.path_params["name"]
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    try:
        result = wm.activate_workspace(name)
        return json_response(result)
    except KeyError as e:
        return json_response({"error": str(e)}, status=404)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)


async def unregister_workspace(request: Request):
    """取消注册工作区。

    默认只移除注册表条目（不删文件，安全默认）；带 `?delete_files=1` 时彻底删除
    （取消注册 + 删除该工作区服务器目录，不可恢复），后端有路径护栏（禁 default /
    禁活跃工作区 / 禁 data_root 外与保留目录），失败返回 400 并说明原因。
    """
    name = request.path_params["name"]
    if name == "default":
        return json_response({"error": "不能删除默认工作区"}, status=400)

    delete_files = (request.query_params.get("delete_files") or "").lower() in (
        "1", "true", "yes", "on"
    )

    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    try:
        if delete_files:
            result = wm.delete_workspace(name)
            return json_response({"ok": True, "name": name, **result})
        ok = wm.unregister_workspace(name)
        if ok:
            return json_response({"ok": True, "name": name})
        return json_response({"error": f"工作区 '{name}' 不存在"}, status=404)
    except KeyError:
        return json_response({"error": f"工作区 '{name}' 不存在"}, status=404)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)


async def get_active_workspace(request: Request):
    """获取当前活跃工作区信息。"""
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    return json_response(
        {
            "active": wm.active_name,
            "path": str(wm.active_workspace),
            # checkpoint/trace/fts/feedback 全局共享，返回共享路径（不随工作区切换）
            "checkpoint_dir": str(wm.shared_checkpoint_dir),
            "feedback_dir": str(wm.shared_feedback_dir),
            "report_dir": str(wm.report_dir),
            "semantic_dir": str(wm.semantic_dir),
        }
    )


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/workspaces", list_workspaces, methods=["GET"]),
    Route("/api/workspaces", register_workspace, methods=["POST"]),
    Route("/api/workspaces/{name}/activate", activate_workspace, methods=["PUT"]),
    Route("/api/workspaces/{name}", unregister_workspace, methods=["DELETE"]),
    Route("/api/workspaces/active", get_active_workspace, methods=["GET"]),
]