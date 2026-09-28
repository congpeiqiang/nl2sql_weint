"""P0 Token 校验 ASGI 中间件。

纯 ASGI（不缓冲 body，不破坏 SSE）：只读 Cookie / Authorization header。
校验通过 → scope["state"]["user"] = user，请求继续。
校验失败 → 401 JSON {"error": "unauthorized"}。

白名单路径直接放行（不校验）：
- /api/auth/login（登录本身）
- /api/auth/logout
- /ok（compose healthcheck）
- /_next/*（Next.js 静态资源）
- /ui*（LangGraph Studio）
- /docs、/openapi.json（API 文档）

P1-12 强制改密（2026-09-23，**默认只标记不拦截**）：
`NL2SQL_FORCE_PASSWORD_CHANGE=1` 时，`must_change_password` 的账号除下面三个路径外
一律 403（登录、看自己是谁、登出、改密 —— 少一个就把用户锁死）。默认关闭的原因：
拦截需要前端有改密 UI，而前端不在本次发版里；先让后端把标记吐给前端（登录响应与
`/api/auth/me` 都带 `must_change_password`），UI 就绪后再打开开关。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 白名单前缀（path 以这些开头则放行）
_WHITELIST_PREFIXES = (
    "/api/auth/login",
    "/api/auth/logout",
    "/ok",
    "/_next/",
    "/ui",
    "/docs",
    "/openapi.json",
    "/redoc",
)

# 「必须改密」状态下仍可访问的路径（**能自己解套的最小集合**）
_MUST_CHANGE_ALLOWED = (
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/logout",
)


def _is_whitelisted(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _WHITELIST_PREFIXES)


def force_password_change_enabled() -> bool:
    """是否拦截未改密账号（默认关，见模块头）。"""
    return (os.getenv("NL2SQL_FORCE_PASSWORD_CHANGE", "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Docker 内部网络来源 IP（容器间通信），用于子 agent 调用父 API 时放行
_INTERNAL_PREFIXES = (
    "172.",       # Docker bridge 默认 172.16.0.0/12
    "10.",        # 自定义 overlay 网络
    "127.0.0.1",  # localhost
    "::1",        # IPv6 localhost
)


def _is_internal_request(scope: dict[str, Any]) -> bool:
    """判断请求是否来自 Docker 内部网络（容器间通信）。"""
    client = scope.get("client")
    if not client:
        return False
    host = client[0]
    return any(host.startswith(p) for p in _INTERNAL_PREFIXES)


def _parse_headers(scope: dict[str, Any]) -> dict[str, str]:
    """把 ASGI scope 的 headers (list[tuple[bytes, bytes]]) 转成 {str: str}。"""
    result: dict[str, str] = {}
    for k, v in scope.get("headers", []):
        key = k.decode("utf-8", errors="replace").lower()
        val = v.decode("utf-8", errors="replace")
        # 同 key 多值时用逗号拼（Cookie 可能出现多次）
        if key in result:
            result[key] = result[key] + "; " + val
        else:
            result[key] = val
    return result


class AuthMiddleware:
    """纯 ASGI 中间件：只读 header/cookie，不缓冲 body。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # 白名单放行
        if _is_whitelisted(path):
            await self.app(scope, receive, send)
            return

        # Docker 内部网络请求放行（子 agent → 父 API 通信）
        # 两道判据缺一不可，单看来源网段会从 nginx 入口被打穿：
        #   ① 来源是 Docker 网段（nginx 容器也在 172.x，所以这条本身不充分）
        #   ② 既没有 Cookie，也**没有 X-Forwarded-For**
        # X-Forwarded-For 由 nginx 无条件追加（全仓唯一写入点就是 docker/nginx.conf，
        # 没有任何 Python 代码写它），而内部调用（httpx 打 http://langgraph-api:2026）
        # 不带 → 它是「这个请求是否经过外部入口」的可靠判据。
        # 少了②的后果（2026-09-23 实测确认）：浏览器不带 Cookie 访问 :8080/api/auth/me
        # 会被认成 internal，该端点返回 200 + 用户信息 → 前端 AuthGuard 认为已登录、
        # 不跳 /login = **登录门被整体绕过**，且 internal 能过所有 require_user。
        # 同一条判据在 src/agent/auth/backend.py:56 已用于原生路由，两边行为对称。
        headers = _parse_headers(scope)
        has_cookie = bool(headers.get("cookie", ""))
        has_xff = bool(headers.get("x-forwarded-for", ""))
        is_internal = _is_internal_request(scope)
        # 调试日志：显示请求来源和 cookie 状态
        if path.startswith("/threads/") or path.startswith("/runs"):
            client = scope.get("client")
            client_host = client[0] if client else "unknown"
            logger.info(
                "[auth] path=%s client=%s is_internal=%s has_cookie=%s has_xff=%s",
                path, client_host, is_internal, has_cookie, has_xff
            )
        if is_internal and not has_cookie and not has_xff:
            if "state" not in scope:
                scope["state"] = {}
            scope["state"]["user"] = {
                "user_id": "internal",
                "display_name": "Internal",
                "is_admin": False,
            }
            await self.app(scope, receive, send)
            return

        # Dev 旁路
        if os.getenv("NL2SQL_AUTH_DISABLED", "0") == "1":
            if "state" not in scope:
                scope["state"] = {}
            scope["state"]["user"] = {
                "user_id": "dev",
                "display_name": "Dev User",
                "is_admin": True,
            }
            await self.app(scope, receive, send)
            return

        # 提取 + 校验 token
        from agent.auth.token import extract_token_from_headers, verify_token

        token = extract_token_from_headers(headers)

        if not token:
            await self._send_401(send)
            return

        user = verify_token(token)
        if not user:
            await self._send_401(send)
            return

        # 注入 user 到 scope state
        if "state" not in scope:
            scope["state"] = {}
        user_dict = {
            "user_id": user["user_id"],
            "display_name": user["display_name"],
            "is_admin": user["is_admin"],
            # P1-12：带上改密标记，否则 /api/auth/me 永远报 False（前端拿不到提示）
            "must_change_password": bool(user.get("must_change_password", False)),
            "token_version": user.get("token_version", 0),
        }
        scope["state"]["user"] = user_dict

        # P1-12：未改密账号的拦截（默认关，开关见模块头）
        if (
            user_dict["must_change_password"]
            and force_password_change_enabled()
            and not any(path.startswith(p) for p in _MUST_CHANGE_ALLOWED)
        ):
            logger.warning(
                "[auth] 拒绝访问：账号 %s 仍在使用初始密码（path=%s）", user_dict["user_id"], path
            )
            await self._send_403_must_change(send)
            return

        # P1: 自动登记用户到 grants 数据库（首次出现时幂等写入）
        #
        # P1-14：**每个带 Cookie 的请求都会走到这里**，而 register_user 是「两次
        # SQLite 写 + commit（fsync）」的同步调用 —— 直接在事件循环上做，等于把
        # 全站每个请求都串到这块磁盘上（10 个 job 共用一个 loop，见评估报告 §3.1）。
        # 搬到线程里：SQLite 连接本身就是 `check_same_thread=False` + 模块级锁
        # （P1-7 加的），跨线程调用安全，代价只是请求多一次线程切换。
        # 注意与下面 `verify_token` 的区别：那个是**缓存内字典查表**（首个请求读一次
        # 文件），不构成阻塞源，所以刻意**不**包线程（实测见 verify_event_loop_liveness）。
        try:
            from agent.auth.grants import register_user
            await asyncio.to_thread(register_user, user["user_id"], user["display_name"])
        except Exception:  # noqa: BLE001
            pass  # 登记失败不阻断请求

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Any) -> None:
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        await AuthMiddleware._send_json(send, 401, body)

    @staticmethod
    async def _send_403_must_change(send: Any) -> None:
        """未改密 403：body 里给**可自解套的指令**（不然用户只会看到"登录成功但什么都点不开"）。"""
        body = json.dumps({
            "error": "must_change_password",
            "detail": (
                "当前账号仍在使用初始密码，请先修改密码："
                "POST /api/auth/change-password {old_password, new_password}"
            ),
        }, ensure_ascii=False).encode("utf-8")
        await AuthMiddleware._send_json(send, 403, body)

    @staticmethod
    async def _send_json(send: Any, status: int, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })
