"""定时任务 SQLite 存储层。

铁律（与 ``agent/eval/eval_queue.py`` 同款，全是已发过版的教训）：
- **共享连接 + 一把锁，读也要进锁**：并发读互踩是本仓实证事故。
- 跨进程互斥靠 ``PRAGMA busy_timeout`` + **单语句 CAS UPDATE**（认领 = 比较
  ``next_run_at`` 旧值），不靠应用层读改写。
- 时间戳统一 UTC ISO ``timespec="seconds"``（与 ``eval_queue._now()`` 逐字一致；
  due 比较是**字符串比较**，口径必须一致，T 分隔 + ``+00:00`` 后缀）。

at-most-once 不变量（hermes jobs.py:1389 同款取舍）：``claim()`` 在**执行前**
原子推进 ``next_run_at`` 并置在飞占位——进程中途死掉最多丢一次触发，
绝不 crash-loop 重复触发。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)

FIRING = "-"
"""在飞占位符：claim 成功后、真实 thread_id 落地前的 running_thread_id 值。

进程若在 fire 中途死掉，任务会卡在占位上 —— worker reconcile 按 lease
（updated_at 超龄）回收，见 worker._reconcile。
"""

_MAX_RETRY = 3
"""fire 失败回拨重试上限（worker 用；store 只落调用方算好的值）。"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id                TEXT PRIMARY KEY,
    owner             TEXT NOT NULL,
    name              TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    db_name           TEXT NOT NULL DEFAULT '',
    llm_route         TEXT NOT NULL DEFAULT '',
    llm_model         TEXT NOT NULL DEFAULT '',
    sched_kind        TEXT NOT NULL,
    sched_expr        TEXT NOT NULL,
    sched_display     TEXT NOT NULL DEFAULT '',
    tz                TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    next_run_at       TEXT NOT NULL DEFAULT '',
    enabled           INTEGER NOT NULL DEFAULT 1,
    state             TEXT NOT NULL DEFAULT 'scheduled',
    repeat_times      INTEGER,
    repeat_completed  INTEGER NOT NULL DEFAULT 0,
    running_thread_id TEXT NOT NULL DEFAULT '',
    retry_count       INTEGER NOT NULL DEFAULT 0,
    last_run_at       TEXT NOT NULL DEFAULT '',
    last_status       TEXT NOT NULL DEFAULT '',
    last_error        TEXT NOT NULL DEFAULT '',
    origin_thread_id  TEXT NOT NULL DEFAULT '',
    pin_status        TEXT NOT NULL DEFAULT '',
    pin_spec          TEXT NOT NULL DEFAULT '',
    pin_semantic_version TEXT NOT NULL DEFAULT '',
    pin_saved_at      TEXT NOT NULL DEFAULT '',
    pin_invalid_reason TEXT NOT NULL DEFAULT '',
    pin_source_thread_id TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_due
    ON scheduled_tasks(enabled, state, next_run_at);
CREATE TABLE IF NOT EXISTS task_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    thread_id   TEXT NOT NULL DEFAULT '',
    run_id      TEXT NOT NULL DEFAULT '',
    fired_at    TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT NOT NULL DEFAULT '',
    pin_mode    TEXT NOT NULL DEFAULT 'normal',
    pin_reason  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, id);
"""

_COLUMNS = (
    "id", "owner", "name", "prompt", "db_name", "llm_route", "llm_model",
    "sched_kind", "sched_expr", "sched_display", "tz", "next_run_at",
    "enabled", "state", "repeat_times", "repeat_completed",
    "running_thread_id", "retry_count", "last_run_at", "last_status",
    "last_error", "origin_thread_id",
    "pin_status", "pin_spec", "pin_semantic_version", "pin_saved_at",
    "pin_invalid_reason", "pin_source_thread_id",
    "created_at", "updated_at",
)

_PIN_COLUMNS = (
    "pin_status", "pin_spec", "pin_semantic_version", "pin_saved_at",
    "pin_invalid_reason", "pin_source_thread_id",
)
"""固化查询字段：只能走 ``set_pin`` / ``invalidate_pin`` / ``clear_pin``。

三条不变量（计划 §A）：
- ``active``  ⇒ ``pin_spec`` 与 ``pin_semantic_version`` 可读；
- ``invalid`` ⇒ ``pin_invalid_reason`` 非空，且 ``pin_spec`` **保留**（供人工排查）；
- ``''``      ⇒ 其余 pin 字段全空（``pin_invalid_reason`` 例外：清 pin 时写入原因）。
"""

_UPDATABLE = frozenset(_COLUMNS) - {
    "id", "owner", "created_at",
    # 认领/收口字段只能走 claim/complete/fail 专用方法（保住 CAS 语义）。
    # retry_count 例外可更新：它只是计数器，at-most-once 不变量不依赖它
    # （claim/fail_fire 各自会重置/递增，允许直写是为了可测「已重试 N 次」状态）。
    "next_run_at", "running_thread_id", "repeat_completed",
    "last_status", "last_run_at",
    # pin 六列同上：普通「编辑任务」不许顺手改固化状态，否则 spec 与
    # semantic_version 会各自被改一半，失效判定就永远对不上了。
    *_PIN_COLUMNS,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ScheduledTaskStore:
    """一实例一库；进程内单例经 ``get_store()``。"""

    def __init__(self, db_path: Optional[Path | str] = None):
        if db_path is None:
            from agent.utils.sqlite_paths import data_root, resolve_store_db

            db_path = resolve_store_db(data_root(), "scheduled_tasks")
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = p  # worker 用：.tick.lock / heartbeat 与库同目录
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(p), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """存量库增量迁移（幂等）：补 pin 六列 / task_runs 两列。

        ``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表不会加列——老库重启后
        必须靠这里补。每列独立 try：某列失败不该挡住其余列，更不能挡住启动
        （同 ``agent/feedback/store.py`` 的 ``_migrate_schema``）。
        """
        pending = tuple(
            ("scheduled_tasks", col, "TEXT NOT NULL DEFAULT ''")
            for col in _PIN_COLUMNS
        ) + (
            ("task_runs", "pin_mode", "TEXT NOT NULL DEFAULT 'normal'"),
            ("task_runs", "pin_reason", "TEXT NOT NULL DEFAULT ''"),
        )
        with self._lock:
            for table, col, decl in pending:
                try:
                    cols = {
                        r["name"]
                        for r in self._conn.execute(f"PRAGMA table_info({table})")
                    }
                    if cols and col not in cols:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
                        )
                        self._conn.commit()
                        _logger.info("[scheduler] 迁移：%s 表新增 %s 列", table, col)
                except Exception as e:  # noqa: BLE001
                    _logger.warning(
                        "[scheduler] %s.%s 列迁移失败（按已有结构继续）: %s",
                        table, col, e,
                    )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── CRUD ──────────────────────────────────────────────────────────

    def insert_task(self, row: dict[str, Any]) -> str:
        unknown = set(row) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"未知字段: {sorted(unknown)}")
        now = _now()
        with self._lock:
            tid = row.get("id") or uuid.uuid4().hex[:12]
            while self._conn.execute(
                "SELECT 1 FROM scheduled_tasks WHERE id=?", (tid,)
            ).fetchone():
                tid = uuid.uuid4().hex[:12]
            values = {
                "id": tid,
                "db_name": "", "llm_route": "", "llm_model": "",
                "sched_display": "", "tz": "Asia/Shanghai", "next_run_at": "",
                "enabled": 1, "state": "scheduled", "repeat_times": None,
                "repeat_completed": 0, "running_thread_id": "", "retry_count": 0,
                "last_run_at": "", "last_status": "", "last_error": "",
                "origin_thread_id": "",
                "pin_status": "", "pin_spec": "", "pin_semantic_version": "",
                "pin_saved_at": "", "pin_invalid_reason": "",
                "pin_source_thread_id": "",
                **row,
                "created_at": now, "updated_at": now,
            }
            cols = ", ".join(values)
            self._conn.execute(
                f"INSERT INTO scheduled_tasks ({cols}) VALUES ({', '.join('?' * len(values))})",
                tuple(values.values()),
            )
            self._conn.commit()
        return tid

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks(self, owner: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if owner is None:
                rows = self._conn.execute(
                    "SELECT * FROM scheduled_tasks ORDER BY created_at DESC, id DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE owner=? ORDER BY created_at DESC, id DESC",
                    (owner,),
                ).fetchall()
        return [dict(r) for r in rows]

    def update_task(self, task_id: str, fields: dict[str, Any]) -> bool:
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            # 拼错的列名是编程错误，静默返回 False 会让人以为更新成功了。
            raise ValueError(f"未知字段: {sorted(unknown)}")
        bad = set(fields) - _UPDATABLE
        if bad:
            # 已知但只能走专用方法（认领/收口/pin）的列：策略拒绝，不抛。
            return False
        if not fields:
            return False
        fields = dict(fields)
        # updated_at 默认自动 touch；显式传值则尊重（lease 语义依赖它可回写）
        ts = fields.pop("updated_at", None) or _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = (
            f"UPDATE scheduled_tasks SET {sets}, updated_at=? WHERE id=?"
            if sets
            else "UPDATE scheduled_tasks SET updated_at=? WHERE id=?"
        )
        vals = (*fields.values(), ts, task_id) if sets else (ts, task_id)
        with self._lock:
            cur = self._conn.execute(sql, vals)
            self._conn.commit()
        return cur.rowcount == 1

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM scheduled_tasks WHERE id=?", (task_id,)
            )
            self._conn.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
            self._conn.commit()
        return cur.rowcount == 1

    def count_by_owner(self, owner: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM scheduled_tasks WHERE owner=?", (owner,)
            ).fetchone()
        return int(row["n"])

    # ── 固化查询 ──────────────────────────────────────────────────────

    def set_pin(
        self,
        task_id: str,
        *,
        spec_json: str,
        semantic_version: str,
        source_thread_id: str,
        now_iso: str,
    ) -> bool:
        """写入（或覆盖）固化 spec 并置 ``pin_status='active'``。

        覆盖是有意的：重新固化 = 用人新确认的那次执行替换旧的，
        旧 spec 连同它的 semantic_version 一起作废。
        """
        with self._lock:
            cur = self._conn.execute(
                """UPDATE scheduled_tasks
                   SET pin_status='active', pin_spec=?, pin_semantic_version=?,
                       pin_saved_at=?, pin_invalid_reason='', pin_source_thread_id=?,
                       updated_at=?
                   WHERE id=?""",
                (spec_json, semantic_version, now_iso, source_thread_id,
                 now_iso, task_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def invalidate_pin(self, task_id: str, reason: str, now_iso: str) -> bool:
        """标失效：**保留** pin_spec（人工排查要看它），只记原因。"""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE scheduled_tasks
                   SET pin_status='invalid', pin_invalid_reason=?, updated_at=?
                   WHERE id=?""",
                ((reason or "")[:500], now_iso, task_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def clear_pin(self, task_id: str, reason: str, now_iso: str) -> bool:
        """彻底解绑：spec 与版本一并清空，只留原因备查。"""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE scheduled_tasks
                   SET pin_status='', pin_spec='', pin_semantic_version='',
                       pin_saved_at='', pin_source_thread_id='',
                       pin_invalid_reason=?, updated_at=?
                   WHERE id=?""",
                ((reason or "")[:500], now_iso, task_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    # ── 调度核心：due 选取 + CAS 认领 ─────────────────────────────────

    def due_tasks(self, now_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        """到点任务：enabled + scheduled + next_run_at<=now（字符串比较，口径见模块头）+ 不在飞。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM scheduled_tasks
                   WHERE enabled=1 AND state='scheduled'
                     AND next_run_at != '' AND next_run_at <= ?
                     AND running_thread_id=''
                   ORDER BY next_run_at ASC LIMIT ?""",
                (now_iso, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def claim(self, task_id: str, expected_next: str, new_next: str, now_iso: str) -> bool:
        """CAS 认领：只有 next_run_at 仍等于旧值且不在飞才成功。

        单语句原子完成（跨进程安全，蓝绿双触发/手动 run 与 tick 竞争全靠它）：
        - 推进 next_run_at（**执行前**，at-most-once）；
        - 置在飞占位 ``FIRING``；
        - 有限次预扣 repeat_completed，耗尽或 once → state='completed'
          （hermes claim_dispatch 同款：预扣在先，one-shot 也不无限重发）；
        - 清 last_error（新一轮从头计）。

        ⚠️ **retry_count 不在这里清零**（review I-1）：fire 失败的回拨重试走的正是
        due→claim→fire 通路，claim 清零会让计数永远在 0→1 震荡、耗尽折叠不可达，
        永久性失败每 5 分钟无限重试。重置只发生在 fire **成功**落真实 thread
        （set_running_thread）与终态收口（complete_run）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            row = dict(row)
            completed = int(row["repeat_completed"])
            state = row["state"]
            if row["repeat_times"] is not None:
                completed += 1
                if completed >= int(row["repeat_times"]):
                    state = "completed"
            if row["sched_kind"] == "once":
                state = "completed"
                new_next = ""
            cur = self._conn.execute(
                """UPDATE scheduled_tasks
                   SET next_run_at=?, running_thread_id=?, repeat_completed=?,
                       state=?, last_error='', updated_at=?
                   WHERE id=? AND next_run_at=? AND running_thread_id=''
                     AND enabled=1 AND state='scheduled'""",
                (new_next, FIRING, completed, state, now_iso,
                 task_id, expected_next),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def reschedule(
        self,
        task_id: str,
        next_run_at: str,
        state: Optional[str] = None,
        enabled: Optional[int] = None,
    ) -> bool:
        """非 CAS 直设 next_run_at —— pause/resume/改周期专用（service 层已做 owner 强制）。

        认领**必须**走 ``claim()``（CAS）；这里只服务「还没到执行语义」的排期改写。
        """
        sets = ["next_run_at=?", "updated_at=?"]
        vals: list[Any] = [next_run_at, _now()]
        if state is not None:
            sets.append("state=?")
            vals.append(state)
        if enabled is not None:
            sets.append("enabled=?")
            vals.append(int(enabled))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id=?",
                (*vals, task_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def set_running_thread(self, task_id: str, thread_id: str, run_id: str) -> None:
        """fire 成功后把占位换成真实 thread（reconcile 锚点）。

        retry_count 在这里清零（review I-1）：fire 真正成功 = 本轮从头计。
        """
        with self._lock:
            self._conn.execute(
                "UPDATE scheduled_tasks SET running_thread_id=?, retry_count=0, updated_at=? WHERE id=?",
                (thread_id, _now(), task_id),
            )
            self._conn.execute(
                "UPDATE task_runs SET run_id=? WHERE task_id=? AND run_id='' AND status='running'",
                (run_id, task_id),
            )
            self._conn.commit()

    def inflight_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE running_thread_id != ''"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 运行流水 ──────────────────────────────────────────────────────

    def record_run(
        self,
        task_id: str,
        thread_id: str,
        run_id: str,
        fired_at: str,
        pin_mode: str = "normal",
        pin_reason: str = "",
    ) -> int:
        """落一条运行流水。``pin_mode``: normal / replay / fallback（见计划 §C）。"""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO task_runs
                       (task_id, thread_id, run_id, fired_at, status, pin_mode, pin_reason)
                   VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (task_id, thread_id, run_id, fired_at, pin_mode, (pin_reason or "")[:500]),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def complete_run(
        self, task_id: str, thread_id: str, status: str, error: str, finished_at: str
    ) -> None:
        """reconcile 终态回写：流水 + 任务行 last_*；清在飞（guarded：只清自己的 thread）。"""
        with self._lock:
            self._conn.execute(
                """UPDATE task_runs SET status=?, error=?, finished_at=?
                   WHERE task_id=? AND thread_id=? AND status='running'""",
                (status, error or "", finished_at, task_id, thread_id),
            )
            self._conn.execute(
                """UPDATE scheduled_tasks
                   SET last_run_at=?, last_status=?, last_error=?,
                       running_thread_id='', retry_count=0, updated_at=?
                   WHERE id=? AND running_thread_id=?""",
                (finished_at, status, error or "", finished_at, task_id, thread_id),
            )
            self._conn.commit()

    def fail_fire(self, task_id: str, retry_at: str, error: str, retry_count: int) -> None:
        """fire 失败：next_run_at 回拨重试、清占位。retry_count 由调用方算（worker 策略层）。"""
        with self._lock:
            self._conn.execute(
                """UPDATE scheduled_tasks
                   SET next_run_at=?, running_thread_id='', retry_count=?,
                       last_status='fire_failed', last_error=?, updated_at=?
                   WHERE id=?""",
                (retry_at, retry_count, (error or "")[:2000], _now(), task_id),
            )
            self._conn.execute(
                """UPDATE task_runs SET status='missed', error=?, finished_at=?
                   WHERE task_id=? AND status='running' AND thread_id != ''""",
                ((error or "")[:2000], _now(), task_id),
            )
            self._conn.commit()

    def clear_stale_running(self, task_id: str, error: str) -> None:
        """回收 fire 中途死掉的占位（updated_at 超龄 lease，worker reconcile 判定后调用）。"""
        with self._lock:
            self._conn.execute(
                """UPDATE scheduled_tasks
                   SET running_thread_id='', last_error=?, updated_at=?
                   WHERE id=? AND running_thread_id=?""",
                ((error or "")[:2000], _now(), task_id, FIRING),
            )
            self._conn.commit()

    def list_runs(self, task_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_old_runs(self, cutoff_iso: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM task_runs WHERE fired_at < ?", (cutoff_iso,)
            )
            self._conn.commit()
        return cur.rowcount


# ── 进程内单例 ────────────────────────────────────────────────────────

_STORE: Optional[ScheduledTaskStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> ScheduledTaskStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ScheduledTaskStore()
        return _STORE


def reset_store_for_tests() -> None:
    """只给单测用：丢弃单例（配合 tmp AGENT_DATA_ROOT）。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None
