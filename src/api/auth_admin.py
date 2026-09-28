"""P1 授权管理 API（管理员专用）。

端点：
    GET    /api/auth/users/auth-list       列出 auth_users.json 中的用户
    POST   /api/auth/users                 新增用户
    PUT    /api/auth/users/{uid}           修改用户（密码/显示名/管理员/首登改密标记）
    DELETE /api/auth/users/{uid}           删除用户
    POST   /api/auth/users/{uid}/revoke    吊销该账号所有 token（P1-12）
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

from agent.utils.offload import offload
from api._common import json_response, parse_body, require_admin

_logger = logging.getLogger(__name__)

# 模块级引用（便于测试打桩；实际实现见 agent.auth.users.revoke_tokens）
from agent.auth.users import revoke_tokens as _revoke


async def list_users(request: Request):
    """列出所有已登记的用户。"""
    require_admin(request)
    from agent.auth.grants import _get_conn

    # P1-14：同步 sqlite 全表读（还带 `_get_conn` 的建表/迁移路径）→ 线程
    def _load() -> list[dict]:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT user_id, display_name, source, status, last_seen_at"
            " FROM users ORDER BY last_seen_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    users = await offload(_load)
    return json_response({"users": users})


async def list_user_grants(request: Request):
    """列出某用户的库授权。"""
    require_admin(request)
    uid = request.path_params["uid"]
    from agent.auth.grants import list_user_grants as _list
    # P1-14：同步 sqlite 读 → 线程
    grants = await offload(_list, uid)
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
    await offload(grant_db, uid, db_name, level)  # P1-14：同步 sqlite 写
    _logger.info("[auth.admin] 授权: %s → %s (%s)", uid, db_name, level)
    return json_response({"ok": True, "user_id": uid, "db_name": db_name, "level": level})


async def revoke_user_db(request: Request):
    """撤销用户对某库的授权。"""
    require_admin(request)
    uid = request.path_params["uid"]
    db = request.path_params["db"]

    from agent.auth.grants import revoke_db
    await offload(revoke_db, uid, db)  # P1-14：同步 sqlite 写
    _logger.info("[auth.admin] 撤销: %s ← %s", uid, db)
    return json_response({"ok": True, "user_id": uid, "db_name": db})


async def reload_users(request: Request):
    """重载 auth_users.json（管理员编辑文件后调用，免重启生效）。"""
    require_admin(request)
    from agent.auth.users import reload_users as _reload
    users = await offload(_reload)  # P1-14：读盘 + 解析
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
        # P1-14：内部是 PBKDF2-260k（~百毫秒级 **CPU**）+ 原子写盘，
        # 卡在事件循环上等于每次建号都让全站等一次哈希
        user = await offload(_add, user_id, password, display_name, is_admin)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    return json_response({"ok": True, "user": user}, status=201)


async def update_user(request: Request):
    """修改用户。body: password(可选), display_name(可选), is_admin(可选),
    must_change_password(可选)"""
    require_admin(request)
    uid = request.path_params["uid"]
    data = await parse_body(request)
    password = data.get("password")
    display_name = data.get("display_name")
    is_admin = data.get("is_admin")
    must_change = data.get("must_change_password")
    if password is None and display_name is None and is_admin is None and must_change is None:
        return json_response({"error": "至少传一个字段"}, status=400)
    try:
        from agent.auth.users import update_user as _update
        user = await offload(
            _update,
            uid,
            password=str(password) if password is not None else None,
            display_name=str(display_name) if display_name is not None else None,
            is_admin=bool(is_admin) if is_admin is not None else None,
            must_change_password=bool(must_change) if must_change is not None else None,
        )
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    return json_response({"ok": True, "user": user})


async def revoke_user_tokens(request: Request):
    """吊销某账号当前所有 token（P1-12）。body 可选: reason（只记日志）。

    场景：怀疑口令外泄但不想改密、设备丢失、账号已停用仍想立刻踢下线。
    实现 = `token_version` +1 → 该账号此前签发的 token 全部立即失效（含其他设备）。
    副作用**如实说明**：目标用户会被踢回登录页，需要用（未变的）密码重新登录。
    """
    require_admin(request)
    uid = request.path_params["uid"]
    try:
        ver = await offload(_revoke, uid)  # P1-14：同步写盘
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    _logger.info("[auth.admin] 吊销 token: %s → token_version=%s", uid, ver)
    return json_response({"ok": True, "user_id": uid, "token_version": ver})


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
        await offload(_remove, uid)  # P1-14：同步写盘
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    return json_response({"ok": True, "user_id": uid})


async def list_auth_users(request: Request):
    """列出 auth_users.json 中的用户（含 is_admin，区别于 grants 表）。"""
    require_admin(request)
    from agent.auth.users import load_users
    # P1-14：读盘 + 解析（含口令哈希的反序列化）→ 线程
    users = await offload(lambda: [
        {k: v for k, v in u.items() if k != "password_hash"}
        for u in load_users()
    ])
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
    # 吊销 token（P1-12）：静态段在 {uid} 之前，避免被当 uid 吃掉
    Route("/api/auth/users/{uid}/revoke", revoke_user_tokens, methods=["POST"]),
    Route("/api/auth/users/{uid}", update_user, methods=["PUT"]),
    Route("/api/auth/users/{uid}", delete_user, methods=["DELETE"]),
    Route("/api/auth/users/{uid}/grants/{db}", revoke_user_db, methods=["DELETE"]),
]
