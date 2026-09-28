"""P1 库级授权——全局唯一判定模块。

所有授权判定都走这里，不在各 handler 里散落 SQL。
存储：<AGENT_DATA_ROOT>/auth/auth.sqlite（全公司 1 份）

表结构：
  users(user_id PK, display_name, source, status, last_seen_at)
  grants(subject_type, subject_id, db_name, level)  -- level: query | admin
  thread_owner(thread_id PK, user_id, created_at)
  thread_db(thread_id, db_name, first_seen, last_seen)
  report_owner(filename PK, user_id, thread_id, created_at)   -- P1-3

管理员（is_admin=True）恒有全部权限。

⚠️ **会话归属有两套账本**，必须同时写、口径必须一致（P1-15）：
  · 本表的 `thread_owner`  —— REST 层 `owned_thread` / `require_thread` 用；
  · thread 的 `metadata.owner`（见 `auth/ownership.py`）—— LangGraph ops 层的
    `@auth.on` 过滤器用（侧边栏列表、threads/runs 的读写）。
  写侧统一：`@auth.on.threads.create`（建会话）+ `langfuse_metadata._apply_ownership`
  （建 run，覆盖子 agent 线程）+ `thread_fork`。两套账本的哨兵值也共用
  `auth/ownership.py` 的常量（`legacy` / `internal`）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from agent.auth.ownership import LEGACY_OWNER
from agent.utils.prom_metrics import metered_rlock

logger = logging.getLogger(__name__)

_db_conn: sqlite3.Connection | None = None

# ── 并发保护 ────────────────────────────────────────────────
#
# 全模块**共用一个连接**（`check_same_thread=False`），并且是**写热点**：
#   · 每个带 Cookie 的请求都会 `register_user`（写 2 次；P1-14 起有 60s 备忘 → 常见路径不再写）
#   · 每个 run 创建会 `claim_thread` + `record_thread_db`（各写 1 次）
# 单条 SQL 由 sqlite3 模块自己保证线程安全（SQLITE_THREADSAFE=1 串行模式），
# 但**跨语句的事务**（INSERT + UPDATE + commit）不是原子的：并发下会丢更新
# （最典型的是 `register_user` 的 INSERT OR IGNORE + UPDATE 交错），
# 且 commit 与下一条 execute 之间可能被别的线程插进来 → 事务边界错乱。
#
# ⚠️ **读也要持锁**（2026-09-23 实测）：sqlite3 模块按 SQL 文本缓存 prepared
# statement 并**跨游标复用**，两个线程同时跑同一条 SELECT 会互相 reset 对方的
# 语句 → 一个拿到 `None`、另一个报 `sqlite3.InterfaceError: bad parameter or
# other API misuse`（实测 8 线程读同一条 SQL：9 次异常/空结果，稳定复现）。
# `owned_thread` 是每个会话访问都会调的读路径，前端还会 1~2s 轮询 —— 不是理论风险。
# 单进程共享连接的正确用法就是「一条连接 = 一把锁，读写都进锁」。
# P2-3：带计量的可重入锁（store="grants"）——等锁时长进 /metrics
_lock = metered_rlock("grants")

# 连接建立也持锁：否则两个协程同时首访会各建一个连接，其中一个**泄漏**
# （fd + 一个永不 close 的句柄），且后写入的会覆盖全局变量。
_init_lock = threading.Lock()

# `register_user` 的进程内备忘（P1-14）：
#   · 每个带 Cookie 的请求都会调它，而它原本每次都写 2 条 + commit（fsync）；
#   · 这些写与 `owned_thread`（每次会话访问都读）**共用 `_lock` 和同一个文件**；
#   · TTL 内直接返回 → 高频轮询不再产生任何磁盘 I/O 与锁竞争。
# 只影响管理页「最后活跃时间」的精度（≤60s），不影响任何鉴权判定。
_SEEN_TTL = float(os.getenv("NL2SQL_SEEN_TTL_SECONDS", "60"))
_seen_lock = threading.Lock()
_seen_memo: dict[str, float] = {}


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

    with _init_lock:
        if _db_conn is not None:  # 双检：等锁期间别人已建好
            return _db_conn

        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # 锁等待：不设的话并发写直接抛 `database is locked`（驱动默认 5s 是
        # connect(timeout=) 的默认值，这里显式写出来，避免被误改回 0）。
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
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
        -- P1-3：报告/图表文件的归属映射。文件名 → 产出它的会话与用户。
        -- 报告目录是**全站共享**的（active_workspace/report），文件名又只到秒级，
        -- 没有这张表就无法回答「这份报告是谁的」——GET /api/reports/{filename}
        -- 只能对任何登录用户都放行（= 按文件名可横向读他人报告）。
        CREATE TABLE IF NOT EXISTS report_owner (
            filename TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            thread_id TEXT DEFAULT '',
            created_at REAL NOT NULL
        );
    """)
        logger.info("[auth.grants] 授权数据库就绪: %s", path)
        return conn


# ── 用户登记 ──────────────────────────────────────────

def register_user(user_id: str, display_name: str = "") -> None:
    """用户首次出现时自动登记（幂等）。每个带 Cookie 的请求都会走到这里。

    P1-14：加一层**进程内 TTL 备忘**（见 `_SEEN_TTL`）。原先每个请求都做
    「两次写 + commit」，而这张表与 `owned_thread`（**每次会话访问都要查**）
    共用 `_lock` 和同一个 SQLite 文件 —— 多用户高频轮询时，光是刷新
    `last_seen_at` 就在跟所有人的会话鉴权抢锁。可观测行为只差一处：
    管理页 `/api/auth/users` 的「最后活跃时间」精度从「每次请求」降到 ≤60s。
    """
    now = time.time()
    with _seen_lock:
        if now - _seen_memo.get(user_id, 0.0) < _SEEN_TTL:
            return  # 常见路径：不碰磁盘、不抢 `_lock`
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, display_name, last_seen_at) VALUES (?, ?, ?)",
            (user_id, display_name, now),
        )
        conn.execute(
            "UPDATE users SET last_seen_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
    with _seen_lock:
        _seen_memo[user_id] = now


# ── 授权判定 ──────────────────────────────────────────

def visible_dbs(user: dict[str, Any]) -> set[str]:
    """该用户可见的库名集合。管理员返回全部已配置库。"""
    if user.get("is_admin"):
        return _all_configured_dbs()

    user_id = user.get("user_id", "")
    if not user_id:
        return set()

    with _lock:  # 读也持锁：见文件头「读也要持锁」
        conn = _get_conn()
        rows = conn.execute(
            "SELECT db_name FROM grants WHERE subject_type='user' AND subject_id=?",
            (user_id,),
        ).fetchall()
        result = {r["db_name"] for r in rows}

    # 与 corpus 里实际存在的库取交集（读 JSON 配置，不碰本连接 → 出锁后再做）
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
    """登记会话归属（首次创建时调用，幂等）。

    `INSERT OR IGNORE` = **先到者胜**：已存在的行不会被改写，所以调用方不需要
    先查再写（那种 check-then-act 在并发下才是可抢占的）。
    """
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO thread_owner (thread_id, user_id, created_at) VALUES (?, ?, ?)",
            (thread_id, user_id, time.time()),
        )
        conn.commit()


def owned_thread(user: dict[str, Any], thread_id: str) -> bool:
    """会话可见性 = 仅本人（管理员例外，`legacy` 存量公开会话例外）。

    ⚠️ **fail-closed**（2026-09-23，P1-5）：查不到归属行 = **拒绝**。
    原先 `row is None → True` 的放行口径有两个真问题：
      · 它把所有挂了 `require_thread` 的 REST 端点（trace/feedback/export/fork/
        compact/run-status/sql-approval）变成"只要会话没被登记就人人可读"；
      · 登记是**惰性**的（run 创建时才写），所以"没登记"是常态而非异常，
        这条放行口实际上长期敞开。
    改成拒绝的前提是**写侧不再有缺口**，两件事已配套做完：
      1. `@auth.on.threads.create` 钩子在建会话时就 `claim_thread`（backend.py）
         —— 覆盖"建了但还没跑过 run"的新会话；
      2. `scripts/backfill_thread_owner.py` 按 `metadata.owner` 回填存量（部署前先跑）。
    注意与 ops 层 `ownership.owner_filter` 的口径必须一致：那边是
    `$or: [owner=identity, owner=legacy]` —— 所以这里对 `legacy` 哨兵行也放行，
    否则同一条会话会出现"列表里看得见、点进去 403"。
    """
    if user.get("is_admin"):
        return True
    user_id = user.get("user_id", "")
    if not user_id:
        return False

    with _lock:  # 读也持锁：见文件头「读也要持锁」（本函数是每个会话访问的必经读路径）
        conn = _get_conn()
        row = conn.execute(
            "SELECT user_id FROM thread_owner WHERE thread_id=?", (thread_id,)
        ).fetchone()
    if row is None:
        return False  # 未登记 → 拒（写侧缺口已由建会话钩子 + 回填脚本堵住）
    owner = row["user_id"]
    if owner == LEGACY_OWNER:
        return True   # 存量公开会话（与 owner_filter 的 $or 分支对齐）
    return owner == user_id


def record_thread_db(thread_id: str, db_name: str) -> None:
    """记录会话用过的库（供 trace/报告库维度判定）。"""
    if not thread_id or not db_name:
        return
    now = time.time()
    with _lock:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO thread_db (thread_id, db_name, first_seen, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(thread_id, db_name) DO UPDATE SET last_seen=?""",
            (thread_id, db_name, now, now, now),
        )
        conn.commit()


def dbs_for_thread(thread_id: str) -> list[str]:
    """会话账本里记过的库（按 last_seen 倒序，最近用过的在前）。无记录/读故障 → `[]`。

    写侧是 `record_thread_db`（每个建 run 的请求各写一次，值是**钳制之后**的 db_name）。
    读侧目前两处：报告装配层在 `configurable.db_name` 缺失时按「恰好一个库」兜底取语料
    （见 `agent.tools.report_builder._current_db_name`），以及给未来的库维度可见性判定用。

    读也持锁：见文件头「读也要持锁」（共享连接上并发读同一条 SELECT 会互踩）。
    """
    tid = str(thread_id or "")
    if not tid:
        return []
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT db_name FROM thread_db WHERE thread_id=? ORDER BY last_seen DESC",
            (tid,),
        ).fetchall()
    return [str(r["db_name"]) for r in rows if r["db_name"]]


# ── 报告/图表文件归属（P1-3）─────────────────────────────

def record_report_owner(filename: str, user_id: str, thread_id: str = "") -> None:
    """登记某个报告/图表文件的归属（幂等，首次写入者胜）。

    为什么用 INSERT OR IGNORE：文件是**追加语义**（同一文件名只会被同一个产出者
    写一次，我们有防重名后缀），重复调用只可能是重试/续跑，不该改写已有归属
    （改写=后来者能把别人的报告认领成自己的）。
    """
    if not filename or not user_id:
        return
    with _lock:
        conn = _get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO report_owner (filename, user_id, thread_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (filename, user_id, thread_id or "", time.time()),
        )
        conn.commit()


def report_owner_of(filename: str) -> str:
    """读文件的归属人；无记录返回空串（= 落库前的存量文件 / 未登记）。"""
    if not filename:
        return ""
    with _lock:  # 读也持锁：见文件头「读也要持锁」
        conn = _get_conn()
        row = conn.execute(
            "SELECT user_id FROM report_owner WHERE filename=?", (filename,)
        ).fetchone()
    return row["user_id"] if row else ""


def delete_report_owners(filenames: list[str]) -> int:
    """删掉这些文件名的归属行，返回删除行数（P2-5 retention 用：**文件与账本成对删**）。

    只在文件确实被删掉之后调用，且传的是**刚删掉的那几个名字** —— 不要传"账本里所有
    当前不存在的名字"：同名文件可能属于另一个工作区的 report 目录，那样会误删别人的账。
    不存在的名字是 no-op（幂等）。
    """
    names = [f for f in (filenames or []) if f]
    if not names:
        return 0
    with _lock:
        conn = _get_conn()
        marks = ",".join("?" * len(names))
        cur = conn.execute(f"DELETE FROM report_owner WHERE filename IN ({marks})", names)
        deleted = max(0, int(cur.rowcount or 0))
        conn.commit()
    return deleted


def can_read_report(user: dict[str, Any], filename: str) -> bool:
    """该用户能不能读这个报告/图表文件。

    无记录（存量文件、图表脚本产物）→ 放行：与 `owned_thread` 的兼容口径一致，
    不能因为加了账本就让人读不到自己的历史报告。
    """
    if user.get("is_admin"):
        return True
    owner = report_owner_of(filename)
    if not owner:
        return True
    return owner == str(user.get("user_id") or "")


# ── 管理员操作 ──────────────────────────────────────────

def grant_db(user_id: str, db_name: str, level: str = "query") -> None:
    """授权用户访问某库。"""
    with _lock:
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
    with _lock:
        conn = _get_conn()
        conn.execute(
            "DELETE FROM grants WHERE subject_type='user' AND subject_id=? AND db_name=?",
            (user_id, db_name),
        )
        conn.commit()


def list_user_grants(user_id: str) -> list[dict[str, str]]:
    """列出用户的所有授权。"""
    with _lock:  # 读也持锁：见文件头「读也要持锁」
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
        from mcp_server.db_mcp_server.db.core.db_config_store import get_store
        store = get_store()
        configs = store.list_configs(masked=True)
        return {c.get("name", "") for c in configs if c.get("name")}
    except Exception:
        logger.warning("[auth.grants] 读取 db_config 失败，返回空集", exc_info=True)
        return set()


def _forbidden(msg: str):
    """返回 403 异常（由调用方 raise）。"""
    from starlette.exceptions import HTTPException
    return HTTPException(status_code=403, detail=msg)
