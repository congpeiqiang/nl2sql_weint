"""HMAC-SHA256 token 签发与校验（P0 临时方案）。

Token 格式：base64(json_payload).hex_signature
- payload: {user_id, display_name, is_admin, exp}（exp = unix timestamp）
- signature: HMAC-SHA256(payload_bytes, secret)
- 有效期：24h

密钥来源（优先级高→低）：
1. NL2SQL_AUTH_SECRET 环境变量
2. AGENT_DATA_ROOT/auth_secret 文件（首次自动生成）

SSO 接入后只需替换 verify_token() 内部实现。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_SECONDS = 86400  # 24h


class User(TypedDict):
    user_id: str
    display_name: str
    is_admin: bool


_secret_cache: str | None = None


def _get_secret() -> str:
    """获取 HMAC 密钥（惰性加载，进程内缓存）。"""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    # 1. 环境变量
    secret = os.getenv("NL2SQL_AUTH_SECRET", "")
    if secret:
        _secret_cache = secret
        return secret

    # 2. AGENT_DATA_ROOT/auth_secret 文件
    data_root = os.getenv("AGENT_DATA_ROOT", "")
    if data_root:
        secret_path = Path(data_root) / "auth_secret"
    else:
        secret_path = Path(__file__).resolve().parents[3] / "auth_secret"

    if secret_path.exists():
        _secret_cache = secret_path.read_text(encoding="utf-8").strip()
        return _secret_cache

    # 3. 自动生成并持久化
    secret = secrets.token_hex(32)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secret, encoding="utf-8")
    logger.info("[auth] 自动生成 HMAC 密钥并存入 %s", secret_path)
    _secret_cache = secret
    return secret


def _sign(payload_bytes: bytes) -> str:
    """HMAC-SHA256 签名 → hex 字符串。"""
    return hmac.new(
        _get_secret().encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()


def sign_token(user_id: str, display_name: str, is_admin: bool) -> str:
    """签发 token：base64(payload).hex_signature。"""
    payload = {
        "user_id": user_id,
        "display_name": display_name,
        "is_admin": is_admin,
        "exp": int(time.time()) + TOKEN_EXPIRY_SECONDS,
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii")
    sig = _sign(payload_bytes)
    return f"{payload_b64}.{sig}"


def verify_token(token: str) -> User | None:
    """校验 token → User 或 None（签名无效/过期/格式错误）。

    NL2SQL_AUTH_DISABLED=1 时跳过校验，返回固定 dev 用户。
    """
    # Dev 旁路
    if os.getenv("NL2SQL_AUTH_DISABLED", "0") == "1":
        return User(user_id="dev", display_name="Dev User", is_admin=True)

    if not token or "." not in token:
        return None

    try:
        payload_b64, sig = token.rsplit(".", 1)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)

        # 验签
        expected_sig = _sign(payload_bytes)
        if not hmac.compare_digest(sig, expected_sig):
            return None

        payload = json.loads(payload_bytes)

        # 过期检查
        if payload.get("exp", 0) < time.time():
            return None

        return User(
            user_id=payload["user_id"],
            display_name=payload.get("display_name", payload["user_id"]),
            is_admin=payload.get("is_admin", False),
        )
    except Exception:
        return None


def extract_token_from_headers(headers: dict[str, str]) -> str | None:
    """从 HTTP headers 提取 token（Cookie 优先，fallback Bearer）。

    headers 的 key 统一小写（Starlette 的 scope headers 是 bytes tuple list，
    调用方应先转成 {k.lower(): v} dict）。
    """
    # 1. Cookie: nl2sql_token=<token>
    cookie = headers.get("cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("nl2sql_token="):
            val = part[len("nl2sql_token="):]
            if val:
                return val

    # 2. Authorization: Bearer <token>
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()

    return None
