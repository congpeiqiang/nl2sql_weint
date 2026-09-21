"""P1 库级授权——全局唯一判定模块。

所有授权判定都走这里，不在各 handler 里散落 SQL。
存储：<AGENT_DATA_ROOT>/auth/auth.sqlite（全公司 1 份）

表结构：
  users(user_id PK, display_name, source, status, last_seen_at)
  grants(subject_type, subject_id, db_name, level)  -- level: query | admin
  thread_owner(thread_id PK, user_id, created_at)

管理员（is_admin=True）恒有全部权限。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_db_conn: sqlite3.Connection | None = None


def _db_path() -> Path:
    data_root = os.getenv("AGENT_DATA_ROOT", "")
    if data_root:
        return Path(data_root) / "auth" / "auth.sqlite"
    return Path(__file__).resolve().parents[3] / "auth" / "auth.sqlite"


def _get_conn() -> sqlite3.Connection:
    """获取 SQLite 连接（惰性初始化，进程内单例）。"""
    global _db_conn
    if _db_conn is not None:
        return _db_conn

    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _db_conn = conn

    # 建表（首次启动自动创建）
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            display_name TEXT DEFAULT '',
            source TEXT DEFAULT 'local',
            status TEXT DEFAULT 'active',
            last_seen_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS grants (
            subject_type TEXT NOT NULL,   -- 'user' | 'group'
            subject_id TEXT NOT NULL,
            db_name TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'query',  -- 'query' | 'admin'
            PRIMARY KEY (subject_type, subject_id, db_name)
        );
        CREATE TABLE IF NOT EXISTS thread_owner (
            thread_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS thread_db (
            thread_id TEXT NOT NULL,
            db_name TEXT NOT NULL,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            PRIMARY KEY (thread_id, db_name)
        );
    """)
    logger.info("[auth.grants] 授权数据库就绪: %s", path)
    return conn


# ── 用户登记 ──────────────────────────────────────────

def register_user(user_id: str, display_name: str = "") -> None:
    """用户首次出现时自动登记（幂等）。"""
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, display_name, last_seen_at) VALUES (?, ?, ?)",
        (user_id, display_name, time.time()),
    )
    conn.execute(
        "UPDATE users SET last_seen_at = ? WHERE user_id = ?",
        (time.time(), user_id),
    )
    conn.commit()


# ── 授权判定 ──────────────────────────────────────────

def visible_dbs(user: dict[str, Any]) -> set[str]:
    """该用户可见的库名集合。管理员返回全部已配置库。"""
    if user.get("is_admin"):
        return _all_configured_dbs()

    user_id = user.get("user_id", "")
    if not user_id:
        return set()

    conn = _get_conn()
    rows = conn.execute(
        "SELECT db_name FROM grants WHERE subject_type='user' AND subject_id=?",
        (user_id,),
    ).fetchall()

    result = {r["db_name"] for r in rows}

    # 与 corpus 里实际存在的库取交集
    configured = _all_configured_dbs()
    return result & configured


def can_access_db(user: dict[str, Any], db_name: str) -> bool:
    """单库判定。管理员恒 True。"""
    if user.get("is_admin"):
        return True
    if not db_name:
        return False
    return db_name in visible_dbs(user)


def require_db(user: dict[str, Any], db_name: str) -> None:
    """不通过 → 抛 403。"""
    if not can_access_db(user, db_name):
        from starlette.responses import JSONResponse
        raise _forbidden(f"无权访问数据库: {db_name}")


# ── 会话归属 ──────────────────────────────────────────

def claim_thread(thread_id: str, user_id: str) -> None:
    """登记会话归属（首次创建时调用，幂等）。"""
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO thread_owner (thread_id, user_id, created_at) VALUES (?, ?, ?)",
        (thread_id, user_id, time.time()),
    )
    conn.commit()


def owned_thread(user: dict[str, Any], thread_id: str) -> bool:
    """会话可见性 = 仅本人。管理员例外。"""
    if user.get("is_admin"):
        return True
    user_id = user.get("user_id", "")
    if not user_id:
        return False

    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id FROM thread_owner WHERE thread_id=?", (thread_id,)
    ).fetchone()
    if row is None:
        return True  # 未登记的旧会话，放行（向后兼容）
    return row["user_id"] == user_id


def record_thread_db(thread_id: str, db_name: str) -> None:
    """记录会话用过的库（供 trace/报告库维度判定）。"""
    if not thread_id or not db_name:
        return
    now = time.time()
    conn = _get_conn()
    conn.execute(
        """INSERT INTO thread_db (thread_id, db_name, first_seen, last_seen)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(thread_id, db_name) DO UPDATE SET last_seen=?""",
        (thread_id, db_name, now, now, now),
    )
    conn.commit()


# ── 管理员操作 ──────────────────────────────────────────

def grant_db(user_id: str, db_name: str, level: str = "query") -> None:
    """授权用户访问某库。"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO grants (subject_type, subject_id, db_name, level)
           VALUES ('user', ?, ?, ?)
           ON CONFLICT(subject_type, subject_id, db_name) DO UPDATE SET level=?""",
        (user_id, db_name, level, level),
    )
    conn.commit()


def revoke_db(user_id: str, db_name: str) -> None:
    """撤销用户对某库的授权。"""
    conn = _get_conn()
    conn.execute(
        "DELETE FROM grants WHERE subject_type='user' AND subject_id=? AND db_name=?",
        (user_id, db_name),
    )
    conn.commit()


def list_user_grants(user_id: str) -> list[dict[str, str]]:
    """列出用户的所有授权。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT db_name, level FROM grants WHERE subject_type='user' AND subject_id=?",
        (user_id,),
    ).fetchall()
    return [{"db_name": r["db_name"], "level": r["level"]} for r in rows]


# ── 内部辅助 ──────────────────────────────────────────

def _all_configured_dbs() -> set[str]:
    """从 corpus/db_config.json 获取所有已配置的库名。"""
    try:
        from agent.shared.db_config_store import get_db_config_store
        store = get_db_config_store()
        configs = store.list_configs()
        return {c.get("name", "") for c in configs if c.get("name")}
    except Exception:
        logger.warning("[auth.grants] 读取 db_config 失败，返回空集", exc_info=True)
        return set()


def _forbidden(msg: str):
    """返回 403 异常（由调用方 raise）。"""
    from starlette.exceptions import HTTPException
    return HTTPException(status_code=403, detail=msg)
