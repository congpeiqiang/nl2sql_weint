"""EventStore — SQLite 事件持久化存储。

traces.sqlite 全局共享（默认工作区），与 checkpoint/feedback/fts 隔离模式一致，
不随工作区切换（2026-08-27 决策）。提供建表、CRUD、谱系管理功能。

⚠️ 必须经 `get_event_store(path)` 取实例，不要各自 `EventStore(path).open()`：
同一文件多实例会各建一条连接（fd 泄漏），且旧实现的 `_seq_counters` 是**实例级**
缓存 → 两个实例对同一 thread 各从自己的起点发号 → **seq 撞号**（详见
`get_event_store` 与 `insert_event` 的注释）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .event_log import EventType, TraceEvent
from agent.utils.prom_metrics import metered_rlock

_logger = logging.getLogger(__name__)

# ── SQL DDL ──────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    task_id TEXT DEFAULT '',
    parent_thread_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_thread
    ON trace_events(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_task
    ON trace_events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_parent
    ON trace_events(parent_thread_id);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON trace_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON trace_events(timestamp);

CREATE TABLE IF NOT EXISTS session_lineage (
    thread_id TEXT PRIMARY KEY,
    parent_thread_id TEXT DEFAULT '',
    agent_type TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_lineage_parent
    ON session_lineage(parent_thread_id);
"""


class EventStore:
    """SQLite 事件存储。

    线程安全：SQLite 连接以 check_same_thread=False 共享，**所有**访问（读写）
    都在 `self._lock` 内 —— 不是只锁写。sqlite3 模块按 SQL 文本缓存 prepared
    statement 并跨游标复用，两个线程并发跑同一条 SELECT 会互相 reset：
    一个拿到 None、另一个报 `InterfaceError: bad parameter or other API misuse`
    （实测 8 线程读同一条 SQL 稳定复现 9 次）。trace API 被前端按会话轮询，
    并发读同一句 SELECT 是常态，所以读也必须进锁。
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = metered_rlock("trace_events")  # P2-3 带计量的锁
        self._ready = False
        # 由 get_event_store 注册表创建 → 进程内共享，close() 变成空操作（见 close）。
        self._shared = False

    # ── 生命周期 ─────────────────────────────────────────────────

    def open(self) -> None:
        """打开数据库连接并初始化 schema。"""
        if self._ready:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 跨进程写等待（sqlite3 驱动的 connect 默认 5s，显式放大并写死，避免被改回）。
        # 场景：uvicorn 进程在写、同一份 traces.sqlite 被 CLI/补评脚本并发访问。
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._ready = True
        _logger.info("[EventStore] opened: %s", self._db_path)

    def close(self) -> None:
        """关闭数据库连接（共享实例上是空操作）。"""
        if self._shared:
            _logger.debug("[EventStore] shared instance close() ignored: %s", self._db_path)
            return
        if self._conn and self._ready:
            self._conn.close()
            self._ready = False
            _logger.info("[EventStore] closed: %s", self._db_path)

    @property
    def is_ready(self) -> bool:
        return self._ready and self._conn is not None

    # ── 事件写入 ─────────────────────────────────────────────────

    def insert_event(self, event: TraceEvent) -> int:
        """写入一条事件，返回自增 id。

        两个曾经的并发缺陷（都在本函数内修掉）：

        1. **seq 撞号**：seq 原先在锁外由实例级 `_seq_counters` 发放。两个线程并发
           插入同一 thread 会各读到同一个旧值 → 都算成 N+1 → 库里出现两条 seq=N+1
           （`ORDER BY thread_id, seq` 再排就不确定了）；同一文件多个 EventStore
           实例（chat agent / 每个子任务一个 sync 循环 / trace API）更是各自从
           `MAX(seq)` 重新起算 → 系统性撞号。
           现在 seq 由 **INSERT 语句内部的子查询**取 `MAX(seq)+1`，与插入同一条
           语句、同一个写事务，天然原子，且与实例数、进程数无关。
        2. **lastrowid 取错行**：原先 `last_insert_rowid()` 在**锁外**执行，两个
           线程之间隔着一次 commit，可能读到别人刚插的 id。改用游标自己的
           `lastrowid` 且在锁内取值。
        """
        if not self.is_ready:
            _logger.warning("[EventStore] not ready, skip insert")
            return -1

        explicit_seq = int(event.seq or 0) != 0
        row = event.to_row()  # (seq, thread_id, agent_type, task_id, parent, type, ts, data)

        with self._lock:
            if explicit_seq:
                cur = self._conn.execute(
                    """INSERT INTO trace_events
                       (seq, thread_id, agent_type, task_id, parent_thread_id,
                        event_type, timestamp, data_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
            else:
                # seq 占位用 ? 传原值 0 无所谓：列值由子查询给出
                cur = self._conn.execute(
                    """INSERT INTO trace_events
                       (seq, thread_id, agent_type, task_id, parent_thread_id,
                        event_type, timestamp, data_json)
                       VALUES (
                           COALESCE((SELECT MAX(seq) FROM trace_events WHERE thread_id = ?), 0) + 1,
                           ?, ?, ?, ?, ?, ?, ?
                       )""",
                    (event.thread_id,) + row[1:],
                )
            new_id = cur.lastrowid
            self._conn.commit()
        return int(new_id if new_id is not None else -1)

    def insert_event_sync(
        self,
        thread_id: str,
        event_type: EventType,
        agent_type: str = "chat_agent",
        task_id: str = "",
        parent_thread_id: str = "",
        data: Optional[dict] = None,
    ) -> int:
        """便捷方法：同步写入事件（供 sync 线程使用）。"""
        return self.insert_event(
            TraceEvent(
                thread_id=thread_id,
                agent_type=agent_type,
                task_id=task_id,
                parent_thread_id=parent_thread_id,
                event_type=event_type,
                timestamp=time.time(),
                data=data or {},
            )
        )

    # ── 事件查询 ─────────────────────────────────────────────────

    def query_events(
        self,
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
        event_type: Optional[str] = None,
        seq_from: int = 0,
        seq_to: int = 0,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """按条件查询事件，返回 dict 列表。"""
        if not self.is_ready:
            return []

        where: list[str] = []
        params: list = []

        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        if task_id:
            where.append("task_id = ?")
            params.append(task_id)
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if seq_from > 0:
            where.append("seq >= ?")
            params.append(seq_from)
        if seq_to > 0:
            where.append("seq <= ?")
            params.append(seq_to)

        clause = " AND ".join(where) if where else "1=1"
        sql = f"""SELECT id, seq, thread_id, agent_type, task_id,
                         parent_thread_id, event_type, timestamp, data_json
                  FROM trace_events
                  WHERE {clause}
                  ORDER BY thread_id, seq
                  LIMIT ? OFFSET ?"""
        params.extend([limit, offset])

        # 读也进锁（见类 docstring）：execute 与 fetchall 之间被别的线程插进来会
        # 共用同一条 prepared statement → 拿到 None 或 InterfaceError。
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": r[0],
                "seq": r[1],
                "thread_id": r[2],
                "agent_type": r[3],
                "task_id": r[4] or None,
                "parent_thread_id": r[5] or None,
                "event_type": r[6],
                "timestamp": r[7],
                "data": json.loads(r[8]) if r[8] else {},
            }
            for r in rows
        ]

    def count_events(self, thread_id: str) -> int:
        """统计某 thread 的事件总数。"""
        if not self.is_ready:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trace_events WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return row[0] if row else 0

    def get_llm_calls(self, thread_id: str) -> list[dict]:
        """获取某 thread 的所有 LLM 调用（配对 start/end）。"""
        return self.query_events(
            thread_id=thread_id,
            event_type="llm/call_end",
            limit=500,
        )

    def get_task_events(self, task_id: str) -> list[dict]:
        """获取某个子任务的所有事件（跨 thread）。"""
        return self.query_events(task_id=task_id, limit=500)

    # ── 会话谱系 ─────────────────────────────────────────────────

    def upsert_lineage(
        self,
        thread_id: str,
        parent_thread_id: str = "",
        agent_type: str = "chat_agent",
        status: str = "active",
        metadata: Optional[dict] = None,
    ) -> None:
        """插入或更新会话谱系记录。"""
        if not self.is_ready:
            return
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO session_lineage
                   (thread_id, parent_thread_id, agent_type, created_at, status, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    thread_id,
                    parent_thread_id,
                    agent_type,
                    time.time(),
                    status,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def update_lineage_status(self, thread_id: str, status: str) -> None:
        """更新会话谱系状态。"""
        if not self.is_ready:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE session_lineage SET status = ? WHERE thread_id = ?",
                (status, thread_id),
            )
            self._conn.commit()

    def get_lineage(self, thread_id: str) -> Optional[dict]:
        """获取某 thread 的谱系信息。"""
        if not self.is_ready:
            return None
        with self._lock:
            row = self._conn.execute(
                """SELECT thread_id, parent_thread_id, agent_type, created_at, status, metadata_json
                   FROM session_lineage WHERE thread_id = ?""",
                (thread_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "thread_id": row[0],
            "parent_thread_id": row[1] or None,
            "agent_type": row[2],
            "created_at": row[3],
            "status": row[4],
            "metadata": json.loads(row[5]) if row[5] else {},
        }

    def get_descendants(self, thread_id: str) -> list[dict]:
        """获取某 thread 的所有直接后代。"""
        if not self.is_ready:
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT thread_id, parent_thread_id, agent_type, created_at, status, metadata_json
                   FROM session_lineage WHERE parent_thread_id = ?""",
                (thread_id,),
            ).fetchall()
        return [
            {
                "thread_id": r[0],
                "parent_thread_id": r[1] or None,
                "agent_type": r[2],
                "created_at": r[3],
                "status": r[4],
                "metadata": json.loads(r[5]) if r[5] else {},
            }
            for r in rows
        ]

    def get_ancestor_chain(self, thread_id: str) -> list[dict]:
        """获取某 thread 的祖先链（从根到当前）。"""
        chain: list[dict] = []
        current = thread_id
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            node = self.get_lineage(current)
            if not node:
                break
            chain.append(node)
            current = node.get("parent_thread_id") or ""
        chain.reverse()
        return chain

    def get_full_lineage_tree(self, thread_id: str) -> dict:
        """获取以 thread_id 为根的完整谱系树。

        Returns:
            {
                "thread_id": ...,
                "ancestors": [...],
                "node": {...},
                "descendants": [{...}, ...]
            }
        """
        ancestors = self.get_ancestor_chain(thread_id)
        # ancestors 的最末项是当前节点本身
        node = ancestors[-1] if ancestors else self.get_lineage(thread_id)
        ancestors = ancestors[:-1] if len(ancestors) > 1 else []

        descendants = self.get_descendants(thread_id)
        # 递归加载后代的后代（最多 3 层，防循环）
        for d in descendants:
            d["children"] = self._load_descendants_recursive(
                d["thread_id"], depth=2
            )

        return {
            "thread_id": thread_id,
            "ancestors": ancestors,
            "node": node,
            "descendants": descendants,
        }

    def _load_descendants_recursive(
        self, thread_id: str, depth: int
    ) -> list[dict]:
        if depth <= 0:
            return []
        children = self.get_descendants(thread_id)
        for c in children:
            c["children"] = self._load_descendants_recursive(
                c["thread_id"], depth - 1
            )
        return children

    # ── 统计投影 ─────────────────────────────────────────────────

    def compute_session_stats(self, thread_id: str) -> dict:
        """基于事件日志计算会话统计（替代 TokenMeter state 累积）。"""
        if not self.is_ready:
            return {}

        # 三条统计查询放同一个锁里：既避免共用 prepared statement 的相互 reset，
        # 也顺带让这次统计取到同一个快照（三条分别读会算出互相矛盾的口径）。
        with self._lock:
            # LLM 调用统计
            llm_rows = self._conn.execute(
                """SELECT data_json FROM trace_events
                   WHERE thread_id = ? AND event_type = 'llm/call_end'""",
                (thread_id,),
            ).fetchall()

            # 工具调用统计
            tool_count = self._conn.execute(
                """SELECT COUNT(*) FROM trace_events
                   WHERE thread_id = ? AND event_type = 'tool/call_end'""",
                (thread_id,),
            ).fetchone()[0]

            # 子任务统计
            sub_count = self._conn.execute(
                """SELECT COUNT(*) FROM trace_events
                   WHERE thread_id = ? AND event_type = 'subagent/spawn'""",
                (thread_id,),
            ).fetchone()[0]

        total_llm_ms = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_read_tokens = 0
        total_reasoning_tokens = 0
        step_count = len(llm_rows)

        for (data_json,) in llm_rows:
            try:
                d = json.loads(data_json) if data_json else {}
                total_llm_ms += d.get("elapsed_ms", 0)
                usage = d.get("usage", {})
                total_input_tokens += usage.get("input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)
                input_details = usage.get("input_token_details") or {}
                output_details = usage.get("output_token_details") or {}
                total_cache_read_tokens += input_details.get("cache_read", 0)
                total_reasoning_tokens += output_details.get(
                    "reasoning_tokens", 0
                )
            except Exception:
                pass

        return {
            "thread_id": thread_id,
            "total_llm_ms": total_llm_ms,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_reasoning_tokens": total_reasoning_tokens,
            "step_count": step_count,
            "tool_call_count": tool_count,
            "subagent_count": sub_count,
        }

    # ── 内部 ─────────────────────────────────────────────────────


# ── 进程内共享实例注册表 ─────────────────────────────────────────
#
# 同一份 traces.sqlite 只应有一条连接：sync 循环每个子任务都会 new 一个实例，
# 20 个并发查询 = 20 条永不 close 的连接（每条 WAL 连接都占 fd + -shm 映射）。
# 注册表按**绝对路径**归一（同一文件的不同相对写法不该算两个实例）。

_STORES: dict[str, EventStore] = {}
_STORES_LOCK = threading.Lock()


def get_event_store(db_path: str) -> EventStore:
    """取进程内唯一的 EventStore（已 open，可直接用）。"""
    key = os.path.abspath(str(db_path))
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = EventStore(str(db_path))
            store.open()
            store._shared = True
            _STORES[key] = store
        return store