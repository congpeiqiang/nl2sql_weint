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
- 增量刷新：每次搜索先枚举会话，只对 updated_at 变更过的会话重拉消息重建索引；
  会话删除时同步清掉索引。无写入钩子、无后台常驻进程，索引按需重建。

中文短词（≤2 字）trigram 匹配不到，回退 `LIKE '%q%'` 扫描 `thread_text.content`
（会话量级下毫秒级）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3

import httpx
from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response

_logger = logging.getLogger(__name__)

# 索引文件与 checkpoints.sqlite 同目录（全局共享 checkpoint 目录）。
# 2026-08-27 决策：checkpoint/fts 全局共享，不随工作区切换。
def _index_db_path() -> str:
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    wm.shared_checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return str(wm.shared_checkpoint_dir / "fts.sqlite")


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

def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS thread_text (
            thread_id TEXT PRIMARY KEY,
            title TEXT,
            updated_at TEXT,
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


def _upsert_thread(conn: sqlite3.Connection, thread_id: str, title: str, updated_at: str, text: str):
    rowid = conn.execute(
        "SELECT rowid FROM thread_text WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if rowid:
        rid = rowid[0]
        conn.execute(
            "UPDATE thread_text SET title=?, updated_at=?, content=? WHERE thread_id=?",
            (title, updated_at, text, thread_id),
        )
        conn.execute("DELETE FROM thread_text_fts WHERE content_rowid = ?", (rid,))
    else:
        cur = conn.execute(
            "INSERT INTO thread_text (thread_id, title, updated_at, content) VALUES (?,?,?,?)",
            (thread_id, title, updated_at, text),
        )
        rid = cur.lastrowid
    conn.execute(
        "INSERT INTO thread_text_fts (content, content_rowid) VALUES (?, ?)",
        (text, rid),
    )
    conn.commit()


def _delete_thread(conn: sqlite3.Connection, thread_id: str):
    row = conn.execute(
        "SELECT rowid FROM thread_text WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM thread_text_fts WHERE content_rowid = ?", (row[0],))
        conn.execute("DELETE FROM thread_text WHERE thread_id = ?", (thread_id,))
        conn.commit()


# ── 增量刷新 ────────────────────────────────────────────────────────────

async def _refresh_index(http: httpx.AsyncClient, base: str):
    """枚举会话并只重建 updated_at 变更过的会话索引。

    首次索引（无 fts.sqlite）会并发拉取全部会话状态；之后仅拉取变更过的会话。
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
    conn = sqlite3.connect(_index_db_path())
    try:
        _ensure_schema(conn)
        existing = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT thread_id, title, updated_at FROM thread_text"
            ).fetchall()
        }
        # 待重建：变更/新增的会话
        # 注意 title 用 `or ""` 归一化，与 _upsert_thread 存储时的取值一致，
        # 否则无标题会话（metadata.title=None）每次都被判为「变更」→ 全量重建。
        to_rebuild = [
            (tid, t, updated_at)
            for tid, (t, updated_at) in stamps.items()
            if existing.get(tid)
            != ((t.get("metadata") or {}).get("title") or "", updated_at)
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
                    _upsert_thread(conn, tid, title, updated_at, text)
                except Exception as e:  # noqa: BLE001
                    _logger.warning("[thread_search] 重建会话 %s 索引失败: %s", tid[:8], e)

        await asyncio.gather(*(_fetch_one(tid, t, ua) for tid, t, ua in to_rebuild))

        # 清理已删除会话
        for tid in set(existing) - set(stamps):
            _delete_thread(conn, tid)
    finally:
        conn.close()


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


def _search(conn: sqlite3.Connection, q: str, limit: int) -> list[dict]:
    """FTS5 trigram（≥3 字）为主，短词/无结果回退 LIKE 子串扫描。"""
    results: list[dict] = []
    matched_ids: set[str] = set()

    if len(q) >= 3:
        try:
            # FTS5 查询需转义特殊字符；简单子串用双引号短语
            safe = q.replace('"', '""')
            rows = conn.execute(
                """
                SELECT t.thread_id, t.title, t.updated_at, t.content,
                       bm25(thread_text_fts) AS score
                FROM thread_text_fts
                JOIN thread_text t ON t.rowid = thread_text_fts.content_rowid
                WHERE thread_text_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (f'"{safe}"', limit),
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
            """
            SELECT thread_id, title, updated_at, content FROM thread_text
            WHERE content LIKE ? ESCAPE '\\'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (like, limit),
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


# ── 端点 ────────────────────────────────────────────────────────────────

async def search_threads(request: Request):
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
        await _refresh_index(http, base)

    conn = sqlite3.connect(_index_db_path())
    try:
        _ensure_schema(conn)
        results = _search(conn, q, limit)
    finally:
        conn.close()

    return json_response({"ok": True, "query": q, "results": results})


routes: list[BaseRoute] = [
    Route("/api/threads/fts", search_threads, methods=["GET"]),
]
