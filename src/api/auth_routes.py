"""P0 鉴权路由：登录 / 登出 / 当前用户。

临时端点，SSO 接入后 /api/auth/login 和 /api/auth/logout 下线。
"""
from __future__ import annotations

import json
import logging
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Cookie 名称（与 token.py extract_token_from_headers 一致）
COOKIE_NAME = "nl2sql_token"
COOKIE_MAX_AGE = 86400  # 24h（与 token.TOKEN_EXPIRY_SECONDS 一致）


def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


async def login(request: Request) -> Response:
    """POST /api/auth/login — 校验用户名密码 → Set-Cookie → 200 / 401。"""
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid JSON body"}, 400)

    username = body.get("username", "")
    password = body.get("password", "")
    if not username or not password:
        return _json({"error": "username and password required"}, 400)

    from agent.auth.users import verify_password
    from agent.auth.token import sign_token

    user = verify_password(username, password)
    if not user:
        return _json({"error": "invalid credentials"}, 401)

    token = sign_token(user["user_id"], user["display_name"], user["is_admin"])

    resp = _json({
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "is_admin": user["is_admin"],
    })
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        httponly=True,
    )
    logger.info("[auth] 用户登录: %s", user["user_id"])
    return resp


async def logout(request: Request) -> Response:
    """POST /api/auth/logout — 清除 cookie → 204。"""
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


async def me(request: Request) -> Response:
    """GET /api/auth/me — 校验 cookie → 200 用户信息 / 401。"""
    # NL2SQL_AUTH_DISABLED=1 时直接返回 dev 用户
    import os
    if os.getenv("NL2SQL_AUTH_DISABLED", "0") == "1":
        return _json({
            "user_id": "dev",
            "display_name": "Dev User",
            "is_admin": True,
        })

    # 从 request.state.user 读取（auth_middleware 已校验并注入）
    user = getattr(request.state, "user", None)
    if not user:
        return _json({"error": "unauthorized"}, 401)

    return _json({
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "is_admin": user["is_admin"],
    })


routes: list[Route] = [
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/logout", logout, methods=["POST"]),
    Route("/api/auth/me", me, methods=["GET"]),
]
