"""临时用户管理（auth_users.json）。

P0 临时方案：从 AGENT_DATA_ROOT/auth_users.json 读取用户列表。
首次启动无文件 → 自动创建默认管理员 admin/admin123。
SSO 接入后此文件整体替换。

文件格式：
[
  {
    "user_id": "admin",
    "password_hash": "sha256_hex",
    "display_name": "管理员",
    "is_admin": true
  }
]
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from agent.auth.token import User

logger = logging.getLogger(__name__)

_users_cache: list[dict[str, Any]] | None = None


def _users_path() -> Path:
    data_root = os.getenv("AGENT_DATA_ROOT", "")
    if data_root:
        return Path(data_root) / "auth_users.json"
    return Path(__file__).resolve().parents[3] / "auth_users.json"


def _hash_password(password: str) -> str:
    """SHA-256 密码哈希（临时方案，SSO 接入后废弃）。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _default_users() -> list[dict[str, Any]]:
    """默认用户列表（首次启动自动创建）。"""
    return [
        {
            "user_id": "admin",
            "password_hash": _hash_password("admin123"),
            "display_name": "管理员",
            "is_admin": True,
        }
    ]


def reload_users() -> list[dict[str, Any]]:
    """清除缓存并重新加载用户列表（管理员编辑 JSON 后调用，免重启生效）。"""
    global _users_cache
    _users_cache = None
    users = load_users()
    logger.info("[auth] 用户列表已重载，共 %d 个用户", len(users))
    return users


def load_users() -> list[dict[str, Any]]:
    """加载用户列表（惰性加载，进程内缓存）。"""
    global _users_cache
    if _users_cache is not None:
        return _users_cache

    path = _users_path()
    if not path.exists():
        users = _default_users()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(users, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[auth] 自动创建默认用户文件 %s（admin/admin123）", path)
    else:
        try:
            users = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("[auth] 读取用户文件失败 %s: %s", path, e)
            users = _default_users()

    _users_cache = users
    return users


def find_user(username: str) -> dict[str, Any] | None:
    """按 user_id 查找用户。"""
    for u in load_users():
        if u.get("user_id") == username:
            return u
    return None


# ── CRUD（管理员 API，写文件 + 自动清缓存）──────────────

def _save_users(users: list[dict[str, Any]]) -> None:
    """写入用户列表并清除缓存（原子写：先 tmp 再 rename）。"""
    global _users_cache
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(users, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    _users_cache = None
    logger.info("[auth] 用户文件已写入，共 %d 个用户", len(users))


def add_user(
    user_id: str,
    password: str,
    display_name: str = "",
    is_admin: bool = False,
) -> dict[str, Any]:
    """新增用户。返回新用户 dict（不含 password_hash）。"""
    users = load_users()
    if any(u.get("user_id") == user_id for u in users):
        raise ValueError(f"用户 '{user_id}' 已存在")
    if not user_id or not user_id.strip():
        raise ValueError("user_id 不能为空")
    if not password or len(password) < 4:
        raise ValueError("密码至少 4 位")
    new_user = {
        "user_id": user_id.strip(),
        "password_hash": _hash_password(password),
        "display_name": display_name.strip() or user_id.strip(),
        "is_admin": bool(is_admin),
    }
    users.append(new_user)
    _save_users(users)
    return {k: v for k, v in new_user.items() if k != "password_hash"}


def update_user(
    user_id: str,
    password: str | None = None,
    display_name: str | None = None,
    is_admin: bool | None = None,
) -> dict[str, Any]:
    """修改用户。只更新传入的字段。返回更新后的 dict（不含 password_hash）。"""
    users = load_users()
    target = None
    for u in users:
        if u.get("user_id") == user_id:
            target = u
            break
    if target is None:
        raise ValueError(f"用户 '{user_id}' 不存在")
    if password is not None:
        if len(password) < 4:
            raise ValueError("密码至少 4 位")
        target["password_hash"] = _hash_password(password)
    if display_name is not None:
        target["display_name"] = display_name.strip()
    if is_admin is not None:
        target["is_admin"] = bool(is_admin)
    _save_users(users)
    return {k: v for k, v in target.items() if k != "password_hash"}


def remove_user(user_id: str) -> None:
    """删除用户。"""
    users = load_users()
    new_users = [u for u in users if u.get("user_id") != user_id]
    if len(new_users) == len(users):
        raise ValueError(f"用户 '{user_id}' 不存在")
    if not new_users:
        raise ValueError("不能删除最后一个用户")
    _save_users(new_users)


def verify_password(username: str, password: str) -> User | None:
    """校验用户名密码 → User 或 None。"""
    user = find_user(username)
    if not user:
        return None
    if user.get("password_hash") != _hash_password(password):
        return None
    return User(
        user_id=user["user_id"],
        display_name=user.get("display_name", user["user_id"]),
        is_admin=user.get("is_admin", False),
    )
