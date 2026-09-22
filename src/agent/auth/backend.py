"""Langgraph 官方 custom auth 适配（覆盖 /threads、/runs/* 等原生路由）。

通过 graph.json 的 auth 键加载：
  "auth": {"path": "./src/agent/auth/backend.py:auth"}

与 auth_middleware.py 共用 verify_token()，逻辑不重复。
"""
from __future__ import annotations

import logging

from langgraph_sdk import Auth

from agent.auth.token import extract_token_from_headers, verify_token

logger = logging.getLogger(__name__)

auth = Auth()


@auth.authenticate
async def authenticate(headers: dict) -> dict:
    """从 Cookie/Bearer 提取 token → 校验 → 返回 user dict。

    langgraph SDK 自动把 headers dict 传进来（key 已小写）。
    返回的 dict 会被 langgraph 转成 AuthCredentials + BaseUser，
    其中 display_name 进 configurable["langgraph_auth_user_id"]。
    """
    # headers 可能是 {str: str} 或 {bytes: bytes}，统一处理
    str_headers: dict[str, str] = {}
    for k, v in headers.items():
        sk = k.decode("utf-8") if isinstance(k, bytes) else k
        sv = v.decode("utf-8") if isinstance(v, bytes) else v
        str_headers[sk.lower()] = sv

    token = extract_token_from_headers(str_headers)
    if not token:
        raise Auth.exceptions.AuthenticationError("Missing authentication token")

    user = verify_token(token)
    if not user:
        raise Auth.exceptions.AuthenticationError("Invalid or expired token")

    # langgraph SDK 要求返回 dict，会自动包装成 BaseUser
    # "identity" → 用户唯一标识（必填）
    # "display_name" → 显示名（选填）
    return {
        "identity": user["user_id"],
        "display_name": user["display_name"],
    }
