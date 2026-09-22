"""P1 授权管理 API（管理员专用）。

端点：
    GET    /api/auth/users/auth-list       列出 auth_users.json 中的用户
    POST   /api/auth/users                 新增用户
    PUT    /api/auth/users/{uid}           修改用户（密码/显示名/管理员）
    DELETE /api/auth/users/{uid}           删除用户
    POST   /api/auth/users/reload          重载 auth_users.json（免重启）
    GET    /api/auth/users                 列出 grants 表中的用户
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


async def reload_users(request: Request):
    """重载 auth_users.json（管理员编辑文件后调用，免重启生效）。"""
    require_admin(request)
    from agent.auth.users import reload_users as _reload
    users = _reload()
    return json_response({"ok": True, "count": len(users)})


async def add_user(request: Request):
    """新增用户。body: user_id, password, display_name(可选), is_admin(可选)"""
    require_admin(request)
    data = await parse_body(request)
    user_id = str(data.get("user_id", "") or "").strip()
    password = str(data.get("password", "") or "")
    display_name = str(data.get("display_name", "") or "").strip()
    is_admin = bool(data.get("is_admin", False))
    if not user_id:
        return json_response({"error": "user_id 必填"}, status=400)
    if not password:
        return json_response({"error": "password 必填"}, status=400)
    try:
        from agent.auth.users import add_user as _add
        user = _add(user_id, password, display_name, is_admin)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    return json_response({"ok": True, "user": user}, status=201)


async def update_user(request: Request):
    """修改用户。body: password(可选), display_name(可选), is_admin(可选)"""
    require_admin(request)
    uid = request.path_params["uid"]
    data = await parse_body(request)
    password = data.get("password")
    display_name = data.get("display_name")
    is_admin = data.get("is_admin")
    if password is None and display_name is None and is_admin is None:
        return json_response({"error": "至少传一个字段"}, status=400)
    try:
        from agent.auth.users import update_user as _update
        user = _update(
            uid,
            password=str(password) if password is not None else None,
            display_name=str(display_name) if display_name is not None else None,
            is_admin=bool(is_admin) if is_admin is not None else None,
        )
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    return json_response({"ok": True, "user": user})


async def delete_user(request: Request):
    """删除用户。"""
    require_admin(request)
    uid = request.path_params["uid"]
    # 防止删除自己
    from api._common import get_user
    me = get_user(request)
    if me and me.get("user_id") == uid:
        return json_response({"error": "不能删除自己"}, status=400)
    try:
        from agent.auth.users import remove_user as _remove
        _remove(uid)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    return json_response({"ok": True, "user_id": uid})


async def list_auth_users(request: Request):
    """列出 auth_users.json 中的用户（含 is_admin，区别于 grants 表）。"""
    require_admin(request)
    from agent.auth.users import load_users
    users = [
        {k: v for k, v in u.items() if k != "password_hash"}
        for u in load_users()
    ]
    return json_response({"users": users})


routes: list[BaseRoute] = [
    # 静态路径优先（在 {uid} 之前）
    Route("/api/auth/users/reload", reload_users, methods=["POST"]),
    Route("/api/auth/users/auth-list", list_auth_users, methods=["GET"]),
    Route("/api/auth/users", list_users, methods=["GET"]),
    Route("/api/auth/users", add_user, methods=["POST"]),
    # 参数化路径
    Route("/api/auth/users/{uid}/grants", list_user_grants, methods=["GET"]),
    Route("/api/auth/users/{uid}/grants", grant_user_db, methods=["POST"]),
    Route("/api/auth/users/{uid}", update_user, methods=["PUT"]),
    Route("/api/auth/users/{uid}", delete_user, methods=["DELETE"]),
    Route("/api/auth/users/{uid}/grants/{db}", revoke_user_db, methods=["DELETE"]),
]
