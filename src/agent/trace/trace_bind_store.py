# -*- coding: utf-8 -*-
"""trace 归并绑定的落盘镜像（thread/task → 原查询 trace）——解决重启后 trace 分裂。

背景（2026-09-05，会话 01a07006）：`_THREAD_TRACE_MAP` / `_TASK_TRACE_MAP` 是进程级
dict（langfuse_client），后端重启即清空。重启后的 auto-continue / build_report 续跑
在 `_fallback_thread_route` 找不到 pre-restart 新查询 trace 的 (trace_id, root_obs_id)，
只能「照常新开」→ Langfuse 会话页把一次提问显示成多条 root trace。

Langfuse 数据本身服务端持久，obs id 重启后仍有效 → 只需把绑定表从「进程内存」升级为
「内存 + 落盘双读」。本模块提供 SQLite 落盘镜像：**写**在 langfuse_client 两处登记闸门
（新查询 root 打开、task 派发）同步 upsert；**读**在内存未命中时兜底并回填内存 map
（进程内后续续跑仍走内存快路径）。

设计（对齐 eval_queue.py / feedback/store.py 先例）：
- 单连接 `check_same_thread=False` + 模块级可重入锁（P2-3 起带计量）+ `PRAGMA journal_mode=WAL`
  + 建表幂等。db 放 `<data_root>/trace_bind/trace_bind.sqlite`（**一库一目录**，三件套
  同处一目录；data_root = `get_workspace_manager().data_root`，解析失败回退
  `AGENT_DATA_ROOT` env → 当前目录。路径解析与老文件接管见 `agent/utils/sqlite_paths.py`）。
- 运行中（worker run 活跃期）可安全写：独立 sqlite 文件不触碰 LangGraph thread
  metadata / checkpointer（后者 in-flight 时拒绝写入，见 sql_approval）。
- 语义镜像内存版本：
  - thread_trace：**UPSERT**（同一 thread 新提问覆盖为最新查询，同 langfuse_client 内存覆盖写）；
  - task_trace：**INSERT OR IGNORE**（「已存在不覆盖」幂等，保护原绑定不被重派发覆盖），
    并限长 prune 保留最新 `_TASK_PRUNE_CAP` 行防无限增长。
- 惰性初始化：首次读写才 resolve data_root + 建连（env 可能未加载/拉重）；本模块不
  import langfuse_client（防环）。所有读写失败由调用方 try/except 兜底（退化回现行
  「照常新开」行为），不抛进埋点链路。

用法：
    from agent.trace.trace_bind_store import get_store
    get_store().set_thread(thr, tid, oid)          # 新查询 root 打开时
    get_store().set_task(task_id=..., ...)          # 派发异步子任务时
    get_store().get_thread(thr) / get_task(tid)     # 内存 miss 时兜底
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from agent.utils import sqlite_paths
from agent.utils.prom_metrics import metered_rlock
from agent.utils.sqlite_paths import resolve_store_db

_logger = logging.getLogger(__name__)

# P2-3：带计量的可重入锁（store="trace_bind"）
_LOCK = metered_rlock("trace_bind")

_TASK_PRUNE_CAP = 3000        # task_trace 保留最新行数上限
_TASK_PRUNE_EVERY = 64        # 每多少次 task 写顺带 prune 一次

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_trace (
    thread_id   TEXT PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    root_obs_id TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_trace (
    task_id     TEXT PRIMARY KEY,
    main_thread TEXT NOT NULL DEFAULT '',
    trace_id    TEXT NOT NULL DEFAULT '',
    root_obs_id TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_trace_updated ON task_trace(updated_at);
"""

# 进程内单例（惰性，首次 get_store 才建连）
_STORE: Optional["TraceBindStore"] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    """绑定库路径：`<AGENT_DATA_ROOT>/trace_bind/trace_bind.sqlite`（一库一目录）。

    2026-09-25 起从数据根目录归位到同名子目录（库 + `-wal` + `-shm` 同处一目录；
    根上的老三件套由 `agent.utils.sqlite_paths` 首次建连时接管）。data_root 解析失败时
    回退 AGENT_DATA_ROOT 环境变量（再不行当前目录），保证 CLI / 缺配置场景也能落地。
    """
    return resolve_store_db(sqlite_paths.data_root(), "trace_bind")


class TraceBindStore:
    """trace 归并绑定 SQLite 存储（进程锁串行读写，单连接 check_same_thread=False）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        p = Path(path) if path else _db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self._path = p
        self._conn = sqlite3.connect(str(p), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._task_writes = 0
        with _LOCK:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 跨进程写等待（与 feedback/eval_queue 同款）：默认 5s 在多进程写入下不够。
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        _logger.info("[trace_bind] 绑定存储就绪: %s", p)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ── thread_trace ────────────────────────────────────────────
    def set_thread(self, thread_id: str, trace_id: str, root_obs_id: str) -> None:
        """UPSERT thread → 最新新查询 (trace_id, root_obs_id)（镜像内存覆盖写）。"""
        if not thread_id or not trace_id:
            return
        with _LOCK:
            self._conn.execute(
                "INSERT INTO thread_trace(thread_id, trace_id, root_obs_id, updated_at)"
                " VALUES(?, ?, ?, ?)"
                " ON CONFLICT(thread_id) DO UPDATE SET"
                " trace_id=excluded.trace_id, root_obs_id=excluded.root_obs_id,"
                " updated_at=excluded.updated_at",
                (thread_id, trace_id, root_obs_id or "", _now()),
            )
            self._conn.commit()

    def get_thread(self, thread_id: str) -> Optional[tuple[str, str]]:
        """查 thread 最近一次新查询 → (trace_id, root_obs_id)；无 → None。"""
        if not thread_id:
            return None
        with _LOCK:
            row = self._conn.execute(
                "SELECT trace_id, root_obs_id FROM thread_trace WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        if row is None or not row["trace_id"]:
            return None
        return str(row["trace_id"]), str(row["root_obs_id"] or "")

    # ── task_trace ──────────────────────────────────────────────
    def set_task(
        self,
        task_id: str,
        main_thread_id: str = "",
        trace_id: str = "",
        root_obs_id: str = "",
        question: str = "",
        description: str = "",
    ) -> None:
        """INSERT OR IGNORE 登记 task → 所属查询（幂等，保住首绑不被覆盖）。"""
        if not task_id or not trace_id:
            return
        with _LOCK:
            self._conn.execute(
                "INSERT OR IGNORE INTO task_trace"
                "(task_id, main_thread, trace_id, root_obs_id, question, description,"
                " updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (task_id, main_thread_id or "", trace_id, root_obs_id or "",
                 question or "", description or "", _now()),
            )
            self._conn.commit()
            self._task_writes += 1
            if self._task_writes % _TASK_PRUNE_EVERY == 0:
                self._prune_tasks()

    def get_task(self, task_id: str) -> Optional[tuple[str, tuple[str, str, str, str, str]]]:
        """查 task → (匹配的完整 task_id, (main_thread, trace_id, root_obs_id, q, desc))。

        支持前缀匹配（续跑正文里是截短 id）。无 → None。
        """
        if not task_id:
            return None
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM task_trace WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None and len(task_id) < 36:
                row = self._conn.execute(
                    "SELECT * FROM task_trace WHERE task_id LIKE ? ORDER BY updated_at DESC"
                    " LIMIT 1",
                    (task_id + "%",),
                ).fetchone()
        if row is None or not row["trace_id"]:
            return None
        key = str(row["task_id"])
        value5 = (
            str(row["main_thread"] or ""),
            str(row["trace_id"]),
            str(row["root_obs_id"] or ""),
            str(row["question"] or ""),
            str(row["description"] or ""),
        )
        return key, value5

    def _prune_tasks(self) -> None:
        """限长：task_trace 只保留最新 _TASK_PRUNE_CAP 行（按 updated_at）。"""
        try:
            self._conn.execute(
                "DELETE FROM task_trace WHERE task_id IN ("
                "  SELECT task_id FROM task_trace"
                "  ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
                (_TASK_PRUNE_CAP,),
            )
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            _logger.debug("[trace_bind] prune 失败(可忽略): %s", e)


def get_store() -> TraceBindStore:
    """模块级单例（惰性建连）。失败向上抛，由调用方 try/except 兜底。"""
    global _STORE
    if _STORE is None:
        _STORE = TraceBindStore()
    return _STORE


def _reset_store_for_test(store: Optional[TraceBindStore]) -> None:
    """仅测试用：替换/清空模块单例。"""
    global _STORE
    _STORE = store
