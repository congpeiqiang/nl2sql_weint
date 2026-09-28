"""会话全文搜索 API（P1-6，对标 deepseek-harness dsh-session-query + SQLite FTS5）。

GET /api/threads/fts?q=<关键词>&limit=<N>
返回命中会话列表：[{thread_id, title, snippet, matched_count, updated_at}]。

检索层：
- 持久化 FTS5 索引落在 checkpoints.sqlite 同目录的 `fts.sqlite`（表 `thread_text`
  存拼接后的纯文本 + title/updated_at，FTS5 虚拟表 `thread_text_fts` 用 trigram
  分词支持中文子串匹配）。
- 消息来源不走手工 msgpack 反序列化，而是复用 langgraph-api 自身的 HTTP 接口：
  `POST /threads/search`（枚举全部会话 + metadata.title/updated_at）+
  `GET /threads/{tid}/state`（完整消息，由服务端正确反序列化）。与 thread_fork.py
  的自调用范式一致。
- 增量刷新：搜索时（按 `THREAD_FTS_REFRESH_TTL`，默认 5s 节流）枚举会话，
  只对 updated_at 变更过的会话重拉消息重建索引；会话删除时同步清掉索引。
  无写入钩子、无后台常驻进程，索引按需重建。
- 归属：索引行带 `owner`（取自 thread metadata），搜索时在 **SQL 里**按身份过滤
  （非 admin 只见自己 + legacy 存量），不是取完结果再剔除（那会让 LIMIT 先行、
  把自己的命中挤掉）。

中文短词（≤2 字）trigram 匹配不到，回退 `LIKE '%q%'` 扫描 `thread_text.content`
（会话量级下毫秒级）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time

import httpx
from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response
from agent.utils.offload import offload
from agent.utils.prom_metrics import metered_rlock

_logger = logging.getLogger(__name__)

# 索引文件与 checkpoints.sqlite 同目录（全局共享 checkpoint 目录）。
# 2026-08-27 决策：checkpoint/fts 全局共享，不随工作区切换。
def _index_db_path() -> str:
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    wm.shared_checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return str(wm.shared_checkpoint_dir / "fts.sqlite")


# ── 连接与并发 ──────────────────────────────────────────────────────────
#
# 旧实现每次调用 `sqlite3.connect()` + 每次都跑一遍 `_ensure_schema`，且不设
# WAL/busy_timeout。多用户同时搜索时：默认 rollback journal 下**写会挡住读**，
# 两个并发搜索各自写索引 → 直接抛 `database is locked`（5s 后）。现在：
#   · 单条模块级连接（check_same_thread=False）+ `_lock` 串行化写；
#   · WAL（读不阻塞写）+ busy_timeout（跨进程等写锁）；
#   · schema 只保证一次（`_schema_ready`），不再每个请求跑 DDL。
#
# ⚠️ 任何 DB 操作都不许在**持锁期间 await**（HTTP 拉取必须留在锁外），否则
# 同一事件循环上的其他请求会死等这把锁。
# P1-14 起 DB 操作统一经 `agent.utils.offload.offload()` 进工作线程：取锁与释放
# 都发生在那个线程里，主循环上依旧"持锁不 await"，但循环本身不再被同步 sqlite 阻塞。

_lock = metered_rlock("thread_search")  # P2-3 带计量（等锁时长进 /metrics）
_conn: sqlite3.Connection | None = None
_schema_ready = False

# 索引刷新节流（秒）：把「每次搜索都全量枚举会话」降成「至多 N 秒一次」。
# 前端搜索框是逐键触发的，节流窗口内多个用户/多次按键共用一个索引快照。
# 代价是刚发出的消息在窗口内搜不到 —— 5s 量级用户无感。
_REFRESH_TTL = float(os.getenv("THREAD_FTS_REFRESH_TTL", "5"))
_last_refresh_at = 0.0
_refresh_lock = asyncio.Lock()


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


# ── 消息文本提取 ────────────────────────────────────────────────────────

def _extract_message_text(msg: dict) -> str:
    """从一条消息 dict 提取可检索文本（human/ai 的问题与回答 + tool_call 里的 SQL）。"""
    if not isinstance(msg, dict):
        return ""
    typ = msg.get("type") or msg.get("role") or ""
    parts: list[str] = []

    content = msg.get("content", "")
    if isinstance(content, str):
        if content.strip():
            parts.append(content)
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = blk.get("text", "")
                if t and t.strip():
                    parts.append(t)

    # ai 消息里的 tool_calls：把 SQL 参数纳入索引（「历史 SQL 会话复用」副产品）
    if typ in ("ai", "assistant"):
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            args = tc.get("args") or {}
            if isinstance(args, dict) and args.get("sql"):
                parts.append(str(args["sql"]))

    return "\n".join(p for p in parts if p.strip())


def _extract_thread_text(messages) -> str:
    if not isinstance(messages, list):
        return ""
    return "\n".join(
        t for t in (_extract_message_text(m) for m in messages) if t
    )


# ── 索引存储 ────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    """模块级单连接（已建表）。首次调用建连接，之后复用。"""
    global _conn, _schema_ready
    if _conn is not None and _schema_ready:
        return _conn
    with _lock:
        if _conn is None:
            conn = sqlite3.connect(_index_db_path(), check_same_thread=False, timeout=15.0)
            conn.execute("PRAGMA journal_mode=WAL")   # 读不阻塞写
            conn.execute("PRAGMA busy_timeout=15000")  # 跨进程等写锁
            conn.execute("PRAGMA synchronous=NORMAL")
            _conn = conn
        if not _schema_ready:
            _ensure_schema(_conn)
            # 存量库（老版本建的）没有 owner 列 → 增量补上，旧行 owner 留空
            # （查询侧按「非 admin 只认 owner=自己/legacy」处理，空值不会被放行）。
            try:
                cols = {r[1] for r in _conn.execute("PRAGMA table_info(thread_text)")}
                if "owner" not in cols:
                    _conn.execute("ALTER TABLE thread_text ADD COLUMN owner TEXT DEFAULT ''")
                    _conn.commit()
                    _logger.info("[thread_search] fts 索引补 owner 列（存量行留空）")
            except Exception as e:  # noqa: BLE001
                _logger.warning("[thread_search] owner 列迁移失败: %s", e)
            _schema_ready = True
        return _conn


def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS thread_text (
            thread_id TEXT PRIMARY KEY,
            title TEXT,
            updated_at TEXT,
            owner TEXT DEFAULT '',
            content TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS thread_text_fts USING fts5(
            content,
            content_rowid,
            tokenize='trigram'
        );
        """
    )
    conn.commit()


def _upsert_thread(thread_id: str, title: str, updated_at: str, owner: str, text: str):
    with _lock:
        conn = _connect()
        rowid = conn.execute(
            "SELECT rowid FROM thread_text WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if rowid:
            rid = rowid[0]
            conn.execute(
                "UPDATE thread_text SET title=?, updated_at=?, owner=?, content=?"
                " WHERE thread_id=?",
                (title, updated_at, owner, text, thread_id),
            )
            conn.execute("DELETE FROM thread_text_fts WHERE content_rowid = ?", (rid,))
        else:
            cur = conn.execute(
                "INSERT INTO thread_text (thread_id, title, updated_at, owner, content)"
                " VALUES (?,?,?,?,?)",
                (thread_id, title, updated_at, owner, text),
            )
            rid = cur.lastrowid
        conn.execute(
            "INSERT INTO thread_text_fts (content, content_rowid) VALUES (?, ?)",
            (text, rid),
        )
        conn.commit()


def _delete_thread(thread_id: str):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT rowid FROM thread_text WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM thread_text_fts WHERE content_rowid = ?", (row[0],))
            conn.execute("DELETE FROM thread_text WHERE thread_id = ?", (thread_id,))
            conn.commit()


def _indexed_snapshot() -> dict[str, tuple[str, str, str]]:
    """已索引会话的 (title, updated_at, owner) 快照（增量比对的基线）。"""
    with _lock:
        conn = _connect()
        return {
            r[0]: (r[1] or "", r[2] or "", r[3] or "")
            for r in conn.execute(
                "SELECT thread_id, title, updated_at, owner FROM thread_text"
            ).fetchall()
        }


# ── 增量刷新 ────────────────────────────────────────────────────────────

async def _refresh_index(http: httpx.AsyncClient, base: str):
    """枚举会话并只重建 updated_at 变更过的会话索引。

    首次索引（无 fts.sqlite）会并发拉取全部会话状态；之后仅拉取变更过的会话。
    调用方应走 `_refresh_if_stale`（TTL 节流 + 单飞），不要直接调。
    """
    threads: list[dict] = []
    offset = 0
    while True:
        resp = await http.post(
            f"{base}/threads/search",
            json={"limit": 100, "offset": offset},
        )
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        threads.extend(batch)
        if len(batch) < 100:
            break
        offset += 100

    stamps = {t["thread_id"]: (t, str(t.get("updated_at") or "")) for t in threads}
    existing = await offload(_indexed_snapshot)  # P1-14：全量读 sqlite，放线程
    # 待重建：变更 / 新增 / **owner 尚空**的会话。
    # title 与 owner 都用 `or ""` 归一化，与 _upsert_thread 存储时的取值一致，
    # 否则无标题会话（metadata.title=None）每次都被判为「变更」→ 全量重建。
    # owner 为空的那一批是历史索引（owner 列是后加的）：必须重拉一次把归属补齐，
    # 否则非 admin 在 SQL 侧按 owner 过滤时**搜不到自己的历史会话**。
    to_rebuild = [
        (tid, t, updated_at)
        for tid, (t, updated_at) in stamps.items()
        if existing.get(tid)
        != (
            (t.get("metadata") or {}).get("title") or "",
            updated_at,
            _owner_of_thread(t),
        )
    ]

    # 并发拉取待重建会话的完整消息（限流，避免首索引打爆自身 HTTP 服务）
    sem = asyncio.Semaphore(8)

    async def _fetch_one(tid: str, t: dict, updated_at: str):
        async with sem:
            try:
                st = await http.get(f"{base}/threads/{tid}/state")
                if st.status_code != 200:
                    return
                values = (st.json() or {}).get("values") or {}
                text = _extract_thread_text(values.get("messages"))
                title = (t.get("metadata") or {}).get("title") or ""
                # DB 写必须在锁内且**不 await**（见文件头并发说明）—— P1-14 把整次
                # 写入（含取锁）搬进线程：锁在工作线程里取得与释放，主循环上不出现
                # "持锁 await"，invariant 不变；但这次写入不再阻塞循环。
                await offload(_upsert_thread, tid, title, updated_at,
                              _owner_of_thread(t), text)
            except Exception as e:  # noqa: BLE001
                _logger.warning("[thread_search] 重建会话 %s 索引失败: %s", tid[:8], e)

    await asyncio.gather(*(_fetch_one(tid, t, ua) for tid, t, ua in to_rebuild))

    # 清理已删除会话
    for tid in set(existing) - set(stamps):
        await offload(_delete_thread, tid)


def _owner_of_thread(t: dict) -> str:
    """从 /threads/search 返回项里读归属（`metadata.owner`，见 auth/ownership.py）。"""
    try:
        from agent.auth.ownership import owner_of

        return owner_of(t.get("metadata"))
    except Exception:  # noqa: BLE001
        return ""


# ── 搜索 ────────────────────────────────────────────────────────────────

def _make_snippet(content: str, q: str, max_len: int = 120) -> str:
    """在 content 中定位首个命中，返回带上下文的片段。"""
    if not content:
        return ""
    idx = content.lower().find(q.lower())
    if idx < 0:
        return content[:max_len]
    start = max(0, idx - max_len // 3)
    end = min(len(content), idx + len(q) + max_len * 2 // 3)
    snippet = content[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return prefix + snippet.strip() + suffix


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search(q: str, limit: int, identity: str = "", is_admin: bool = False) -> list[dict]:
    """FTS5 trigram（≥3 字）为主，短词/无结果回退 LIKE 子串扫描。

    **归属过滤在 SQL 里做，不在结果上做**：旧实现先 `LIMIT` 取前 N 条命中、
    再在 Python 里按归属剔除 —— 若前 N 条全是别人的会话，用户看到的是「无结果」，
    而他自己的命中排在 N 之后（搜索静默失效，且命中的条数越多越容易触发）。
    现在把身份条件下的 `owner IN (...)` 交给数据库，LIMIT 作用在**已过滤**的行上。
    过滤语义与 ops 层 (auth/ownership.owner_filter) 一致：自己 + `legacy` 存量；
    admin 不过滤。owner 为空的行（未打标的旧数据）非 admin 一律看不到 —— 与
    ops 层 fail-closed 的行为保持一致。
    """
    results: list[dict] = []
    matched_ids: set[str] = set()

    if is_admin:
        owner_clause, owner_params = "", []
    else:
        owner_clause = "AND t.owner IN (?, ?)"
        from agent.auth.ownership import LEGACY_OWNER

        owner_params = [identity, LEGACY_OWNER]

    with _lock:
        conn = _connect()
        if len(q) >= 3:
            try:
                # FTS5 查询需转义特殊字符；简单子串用双引号短语
                safe = q.replace('"', '""')
                rows = conn.execute(
                    f"""
                    SELECT t.thread_id, t.title, t.updated_at, t.content,
                           bm25(thread_text_fts) AS score
                    FROM thread_text_fts
                    JOIN thread_text t ON t.rowid = thread_text_fts.content_rowid
                    WHERE thread_text_fts MATCH ? {owner_clause}
                    ORDER BY score
                    LIMIT ?
                    """,
                    (f'"{safe}"', *owner_params, limit),
                ).fetchall()
                for tid, title, updated_at, content, _score in rows:
                    if tid in matched_ids:
                        continue
                    matched_ids.add(tid)
                    results.append({
                        "thread_id": tid,
                        "title": title,
                        "updated_at": updated_at,
                        "snippet": _make_snippet(content or "", q),
                        "matched_count": (content or "").lower().count(q.lower()),
                    })
            except Exception as e:  # noqa: BLE001  FTS 查询语法错误等回退 LIKE
                _logger.debug("[thread_search] FTS 查询失败回退 LIKE: %s", e)

        if not results:
            like = f"%{_escape_like(q)}%"
            rows = conn.execute(
                f"""
                SELECT thread_id, title, updated_at, content FROM thread_text t
                WHERE content LIKE ? ESCAPE '\\' {owner_clause}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, *owner_params, limit),
            ).fetchall()
            for tid, title, updated_at, content in rows:
                if tid in matched_ids:
                    continue
                matched_ids.add(tid)
                results.append({
                    "thread_id": tid,
                    "title": title,
                    "updated_at": updated_at,
                    "snippet": _make_snippet(content or "", q),
                    "matched_count": (content or "").lower().count(q.lower()),
                })
    return results


async def _refresh_if_stale(http: httpx.AsyncClient, base: str) -> None:
    """按 TTL 节流刷新索引；窗口内的并发请求共用一次刷新。

    `_refresh_lock` 保证同一时刻只有一次枚举/重建在跑（其余请求等它完成后直接
    用新索引）——旧实现是每个搜索请求都独立枚举全部会话 + 拉取变更会话 state，
    多用户同时搜 = 同一份 HTTP 洪峰重复 N 遍。
    """
    global _last_refresh_at
    if time.time() - _last_refresh_at < _REFRESH_TTL:
        return
    async with _refresh_lock:
        # 等锁期间别人可能刚刷完 → 拿到锁后再判一次（避免排队洪峰逐个全刷）
        if time.time() - _last_refresh_at < _REFRESH_TTL:
            return
        await _refresh_index(http, base)
        _last_refresh_at = time.time()


# ── 端点 ────────────────────────────────────────────────────────────────

async def search_threads(request: Request):
    from api._common import require_user

    user = require_user(request)
    is_admin = user.get("is_admin", False)
    identity = user.get("user_id", "")

    q = (request.query_params.get("q") or "").strip()
    if not q:
        return json_response({"ok": False, "error": "q 必填"}, status=400)
    try:
        limit = max(1, min(50, int(request.query_params.get("limit", "20"))))
    except (TypeError, ValueError):
        limit = 20

    base = _base_url()
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        await _refresh_if_stale(http, base)

    # 归属过滤已在 _search 的 SQL 里完成（见其 docstring：结果的 LIMIT 之前过滤）。
    # P1-14：FTS/LIKE 查询是同步 sqlite（且要等 `_lock`），搬到线程 —— 搜索框是
    # 逐键触发的，挂在事件循环上会连累所有人。
    results = await offload(_search, q, limit, identity=identity, is_admin=is_admin)

    return json_response({"ok": True, "query": q, "results": results})


routes: list[BaseRoute] = [
    Route("/api/threads/fts", search_threads, methods=["GET"]),
]
