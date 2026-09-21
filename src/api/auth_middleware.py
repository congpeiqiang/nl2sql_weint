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
"""
from __future__ import annotations

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


def _is_whitelisted(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _WHITELIST_PREFIXES)


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

        headers = _parse_headers(scope)
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
        scope["state"]["user"] = {
            "user_id": user["user_id"],
            "display_name": user["display_name"],
            "is_admin": user["is_admin"],
        }

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Any) -> None:
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })
