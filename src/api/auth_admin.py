"""P1 授权管理 API（管理员专用）。

端点：
    GET    /api/auth/users                 列出所有已登记用户
    GET    /api/auth/users/{uid}/grants    列出某用户的库授权
    POST   /api/auth/users/{uid}/grants    授权用户访问某库
    DELETE /api/auth/users/{uid}/grants/{db}  撤销授权
"""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body, require_admin

_logger = logging.getLogger(__name__)


async def list_users(request: Request):
    """列出所有已登记的用户。"""
    require_admin(request)
    from agent.auth.grants import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT user_id, display_name, source, status, last_seen_at FROM users ORDER BY last_seen_at DESC"
    ).fetchall()
    users = [dict(r) for r in rows]
    return json_response({"users": users})


async def list_user_grants(request: Request):
    """列出某用户的库授权。"""
    require_admin(request)
    uid = request.path_params["uid"]
    from agent.auth.grants import list_user_grants as _list
    grants = _list(uid)
    return json_response({"user_id": uid, "grants": grants})


async def grant_user_db(request: Request):
    """授权用户访问某库。body: db_name(必填), level(可选,默认 query)"""
    require_admin(request)
    uid = request.path_params["uid"]
    data = await parse_body(request)
    db_name = data.get("db_name", "")
    level = data.get("level", "query")
    if not db_name:
        return json_response({"error": "db_name 必填"}, status=400)
    if level not in ("query", "admin"):
        return json_response({"error": "level 必须是 query 或 admin"}, status=400)

    from agent.auth.grants import grant_db
    grant_db(uid, db_name, level)
    _logger.info("[auth.admin] 授权: %s → %s (%s)", uid, db_name, level)
    return json_response({"ok": True, "user_id": uid, "db_name": db_name, "level": level})


async def revoke_user_db(request: Request):
    """撤销用户对某库的授权。"""
    require_admin(request)
    uid = request.path_params["uid"]
    db = request.path_params["db"]

    from agent.auth.grants import revoke_db
    revoke_db(uid, db)
    _logger.info("[auth.admin] 撤销: %s ← %s", uid, db)
    return json_response({"ok": True, "user_id": uid, "db_name": db})


routes: list[BaseRoute] = [
    Route("/api/auth/users", list_users, methods=["GET"]),
    Route("/api/auth/users/{uid}/grants", list_user_grants, methods=["GET"]),
    Route("/api/auth/users/{uid}/grants", grant_user_db, methods=["POST"]),
    Route("/api/auth/users/{uid}/grants/{db}", revoke_user_db, methods=["DELETE"]),
]
