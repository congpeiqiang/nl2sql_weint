"""HMAC-SHA256 token 签发与校验（P0 临时方案）。

Token 格式：base64(json_payload).hex_signature
- payload: {user_id, display_name, is_admin, exp, pv}（exp = unix timestamp）
- signature: HMAC-SHA256(payload_bytes, secret)
- 有效期：24h

密钥来源（优先级高→低）：
1. NL2SQL_AUTH_SECRET 环境变量
2. AGENT_DATA_ROOT/auth_secret 文件（首次自动生成）

P1-12 吊销（2026-09-23）：
HMAC 只证明「这串 token 是我们签的」，不证明「它现在还该有效」—— 改完密码旧 token
照样能用到过期。载荷里加 `pv`（= 用户记录的 `token_version`），`verify_token` 拿它
跟**当前**记录比：不等 / 用户已删 → 立即失效。

- 改密、显式吊销 → +1（`users.update_user` / `users.revoke_tokens`）
- 删号 → 记录消失 → `find_user` 返回 None → 失效
- **登录不 +1**（同一账号允许多设备/多标签并存；单会话要另加策略）
- 老 token 没有 `pv` → 按 0；老用户记录没有 `token_version` → 按 0 → **本次升级不会
  把任何在线用户踢下线**（发版瞬间不掉线）
- 身份字段（display_name / is_admin）改为**以记录为准**：降权/改名立即生效，
  不必等 24h 过期（token 只作为"已认证"的凭证，属性一律现查现用）

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


class User(TypedDict, total=False):
    """认证身份。前三个键必填，P1-12 的两个键由 `users.verify_password` 填。

    `total=False` 是**刻意**的：`verify_token` 是唯一同时服务「HTTP 中间件 / LangGraph
    ops 钩子 / 内部旁路」三处的入口，其中内部旁路（无 token）没有用户记录，
    强行要求五个键会让那条路径被塞一堆占位值。缺 `must_change_password` 按 False 读。
    """
    user_id: str
    display_name: str
    is_admin: bool
    must_change_password: bool
    token_version: int


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


def sign_token(
    user_id: str,
    display_name: str,
    is_admin: bool,
    token_version: int = 0,
) -> str:
    """签发 token：base64(payload).hex_signature。

    `token_version` 写进载荷的 `pv`（吊销计数器，见模块头）。调用方应传该账号**当前**
    的 `token_version`（`users.verify_password` 返回的 `user["token_version"]`）；
    默认 0 是为了兼容老调用点，但那样签出的 token 在该账号被吊销后**不会**失效。
    """
    payload = {
        "user_id": user_id,
        "display_name": display_name,
        "is_admin": is_admin,
        "exp": int(time.time()) + TOKEN_EXPIRY_SECONDS,
        "pv": int(token_version or 0),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii")
    sig = _sign(payload_bytes)
    return f"{payload_b64}.{sig}"


def verify_token(token: str) -> User | None:
    """校验 token → User 或 None（签名无效/过期/已吊销/格式错误）。

    NL2SQL_AUTH_DISABLED=1 时跳过校验，返回固定 dev 用户。

    **判无效的四种情形**：签名不符 / 已过期 / **账号已删** / **`pv` 与记录的
    `token_version` 不等（改过密码或显式吊销过）**。后两条是 P1-12 加的。
    用户记录读失败（文件损坏）沿用 `users.load_users` 的兜底，会让找不到记录的 token
    全部失效 —— 这是**故意 fail-closed**：宁可全员重登，也不认一串可能已吊销的凭证。
    """
    # Dev 旁路
    if os.getenv("NL2SQL_AUTH_DISABLED", "0") == "1":
        return User(
            user_id="dev",
            display_name="Dev User",
            is_admin=True,
            must_change_password=False,
            token_version=0,
        )

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

        user_id = payload.get("user_id") or ""
        if not user_id:
            return None

        # P1-12 吊销：以**记录**为准（属性现查现用，token 只证明"已认证"）
        from agent.auth.users import find_user, must_change_password_of, token_version_of

        record = find_user(user_id)
        if record is None:
            logger.info("[auth] token 指向已不存在的账号，判无效: %s", user_id)
            return None
        if int(payload.get("pv", 0)) != token_version_of(record):
            logger.info("[auth] token 已被吊销（改密/显式吊销），判无效: %s", user_id)
            return None

        return User(
            user_id=user_id,
            display_name=record.get("display_name", user_id),
            is_admin=record.get("is_admin", False),
            must_change_password=must_change_password_of(record),
            token_version=token_version_of(record),
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
                # Starlette set_cookie 自动加双引号，浏览器原样回传，需剥掉
                return val.strip('"')

    # 2. Authorization: Bearer <token>
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()

    return None
