"""P0 鉴权路由：登录 / 登出 / 当前用户 / 自助改密。

临时端点，SSO 接入后 /api/auth/login 和 /api/auth/logout 下线。

P1-12 凭据加固（2026-09-23）：
- 登录与改密都走 `asyncio.to_thread`：PBKDF2 迭代 26 万次是**同步 CPU 活**（实测几十 ms），
  直接在事件循环里跑就是给所有并发请求加一次停顿 —— 与 P1-14 同一条纪律（同步阻塞不占 loop）。
- Cookie 的 `Secure`：**按请求是否走 HTTPS 自动判定**（直连看 scheme、经代理看
  `X-Forwarded-Proto`），可用 `NL2SQL_COOKIE_SECURE=0/1` 强制覆盖。
  为什么不是无条件 `secure=True`：本环境是**明文 HTTP**，浏览器会直接丢掉带 Secure 的
  Cookie → 登录看似成功、后续每个请求 401（"登录不上"的经典假象）。
  真正的解法是运维在 nginx 上做 TLS 终结，那之后这里会自动打开。
"""
from __future__ import annotations

import asyncio
import logging
import os
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Cookie 名称（与 token.py extract_token_from_headers 一致）
COOKIE_NAME = "nl2sql_token"
COOKIE_MAX_AGE = 86400  # 24h（与 token.TOKEN_EXPIRY_SECONDS 一致）


def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _is_https(request: Request) -> bool:
    """请求是否走 HTTPS（直连看 scheme，经 nginx 看 X-Forwarded-Proto）。"""
    proto = (request.headers.get("x-forwarded-proto", "") or "").split(",")[0].strip()
    if proto:
        return proto.lower() == "https"
    return request.url.scheme == "https"


def cookie_secure(request: Request) -> bool:
    """是否给 Cookie 加 `Secure`。env 可强制覆盖（`0`/`false` 关闭，`1`/`true` 打开）。"""
    flag = (os.getenv("NL2SQL_COOKIE_SECURE", "") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return _is_https(request)


def _user_payload(user) -> dict:
    """用户信息响应体（登录 / me 共用；前端据 must_change_password 提示改密）。"""
    return {
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "is_admin": user["is_admin"],
        "must_change_password": bool(user.get("must_change_password", False)),
    }


def _set_auth_cookie(resp: Response, request: Request, token: str) -> None:
    """统一的 Cookie 写法（登录与改密共用，避免两处漂移出"一处 Secure 一处没有"）。"""
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        httponly=True,
        secure=cookie_secure(request),
    )


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

    # 密码哈希是同步 CPU 活（PBKDF2 26 万次迭代）→ 放线程，别占事件循环
    user = await asyncio.to_thread(verify_password, username, password)
    if not user:
        return _json({"error": "invalid credentials"}, 401)

    token = sign_token(
        user["user_id"],
        user["display_name"],
        user["is_admin"],
        token_version=user.get("token_version", 0),
    )

    resp = _json(_user_payload(user))
    _set_auth_cookie(resp, request, token)
    logger.info(
        "[auth] 用户登录: %s%s",
        user["user_id"],
        "（仍在使用初始密码，建议尽快改密）" if user.get("must_change_password") else "",
    )
    return resp


async def logout(request: Request) -> Response:
    """POST /api/auth/logout — 清除 cookie → 204。

    ⚠️ 只清浏览器里的 Cookie，**不吊销**服务端 token（无状态 token 的固有性质）：
    该串凭证在别处被复制走的话，登出后仍能用到过期。要真吊销用
    `POST /api/auth/users/{uid}/revoke`（管理员）或改密（自助，见下）。
    """
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


async def me(request: Request) -> Response:
    """GET /api/auth/me — 校验 cookie → 200 用户信息 / 401。"""
    # NL2SQL_AUTH_DISABLED=1 时直接返回 dev 用户
    if os.getenv("NL2SQL_AUTH_DISABLED", "0") == "1":
        return _json({
            "user_id": "dev",
            "display_name": "Dev User",
            "is_admin": True,
            "must_change_password": False,
        })

    # 从 request.state.user 读取（auth_middleware 已校验并注入）
    user = getattr(request.state, "user", None)
    if not user:
        return _json({"error": "unauthorized"}, 401)

    return _json(_user_payload(user))


async def change_password(request: Request) -> Response:
    """POST /api/auth/change-password — 自助改密（需登录）。

    body: {old_password, new_password}
    成功 → 200 并**换发新 Cookie**：改密会让 `token_version` +1（该账号所有旧 token
    立即失效），当前这个会话若不换发就会被自己踢下线 —— 改完密当场退出登录是最招人烦的
    交互 bug。旧的那串 token 依旧全局无效（这才是吊销的意义）。
    """
    user = getattr(request.state, "user", None)
    if not user or user.get("user_id") in ("internal", ""):
        return _json({"error": "unauthorized"}, 401)

    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid JSON body"}, 400)

    old_password = body.get("old_password", "")
    new_password = body.get("new_password", "")
    if not old_password or not new_password:
        return _json({"error": "old_password and new_password required"}, 400)

    from agent.auth.users import change_password as _change, find_user
    from agent.auth.token import sign_token

    try:
        await asyncio.to_thread(_change, user["user_id"], old_password, new_password)
    except ValueError as e:
        # 不区分"旧密码错"与"新密码不合规"？—— 这里区分（都是登录态下的自助操作，
        # 不存在给攻击者的信息泄露面：能调这个接口就说明已经持有有效凭证）
        return _json({"error": str(e)}, 400)

    record = find_user(user["user_id"]) or {}
    token = sign_token(
        user["user_id"],
        record.get("display_name", user["display_name"]),
        record.get("is_admin", user["is_admin"]),
        token_version=record.get("token_version", 0),
    )
    resp = _json({"ok": True, "must_change_password": False})
    _set_auth_cookie(resp, request, token)
    logger.info("[auth] 用户改密并已吊销其旧 token: %s", user["user_id"])
    return resp


routes: list[Route] = [
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/logout", logout, methods=["POST"]),
    Route("/api/auth/me", me, methods=["GET"]),
    Route("/api/auth/change-password", change_password, methods=["POST"]),
]
