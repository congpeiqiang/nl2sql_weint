# -*- coding: utf-8 -*-
"""子任务终态「待补写」登记表 + 补写器（P2-4）。

## 要解决什么

`sync_subagent_todos` 的 watcher 在子任务跑完后要把三样东西写回**主线程** state：
`async_tasks[task].status`（前端自动续跑的唯一依据）、`active_queries[task]=false`
（前端进度卡「执行中」的依据）、`subagent_steps_map[task]`（最终步骤）。

但 LangGraph 对 `threads.update_state` 有硬闸：**主线程只要还有 pending/running 的 run
就返回 409**（`langgraph_api/grpc/ops/threads.py`：`run_count > 0` →
`"Thread is busy with a running job. Cannot update state."`）。而主线程忙**恰恰是常态**：
另一子任务完成后的自动续跑 run、用户正在发的消息、以及**并发压满 10 个 run 槽时排在
pending 的 run**（见 `concurrency-ceiling-10-run-slots`）。

watcher 一直在重试，但有个 `COMPLETE_WRITE_MAX_SECONDS = 300` 的天花板，超了就
`break` 放弃 —— 于是 `active_queries` **永久停在 true**：进度卡永久「执行中」，
而且因为 `async_tasks` 终态根本没落地，**前端的自动续跑永不触发 → 用户的图表/报告
直接丢掉**（不只是显示问题）。

## 怎么修

不可能在那个时刻强行写进去（409 是硬闸，见上），所以只能**稍后补写**。本模块就是
那个「稍后」：watcher 放弃时把**终态载荷**持久化成本表的一行，由一个后台补写器
（`start_reaper`，随进程 lifespan 起停）每 60s 试一次，直到主线程空下来、写完为止。

补写器同时覆盖三种 watcher 必须放手的情形：
1. **主线程短暂忙**（自动续跑/别的子任务）→ 几十秒后补上；
2. **run 槽饱和**导致的长时间 pending → 槽位释放后补上（这正是多用户并发下的形态）；
3. **进程重启**：行在 SQLite 里，重启后补写器立刻重放 —— 这正好接上 P1-9 的另一半
   （P1-9 处理「终态已写、清零没写」，本模块处理「终态压根没写」）；也覆盖
   `.langgraph_ops.pckl` 僵尸 run（恒 running → `run_count > 0` 恒真）经重启清理后的恢复。

## 安全规则（改代码时别退回去）

- **绝不覆盖比它新的状态**：重放前先读子线程最新 run + 主线程 `async_tasks`
  - 最新 run 的 `run_id != watched_run_id`（被 `update_async_task` 重派发了，新 watcher 在管）
    → **丢行不写**（否则会把活的子任务打成终态，正是 a6f86bbd 那一类 bug）；
  - `run_id` 相同但状态非终态 → 丢行；
  - `async_tasks[task].status` 已是终态（别人/别的路径写过了）→ 丢行（幂等）。
- 写入走 `sync_subagent_todos._sync_update_state`（同一把 `_SYNC_WRITE_LOCK`），
  与 watcher 串行，不做第二套写路径。
- 单写者：watcher 一旦登记就**退出**，之后只有补写器写 → 不会重复触发续跑/失败汇报。
- 超过 `NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS`（默认 6h）仍写不进去的行置 `abandoned`
  并记 ERROR（留痕可查，不再重试）；这是「主线程真的永久卡死」的降级，不是静默丢弃。

## 用法

    # 在线：watcher 放弃时登记（自动起补写器，见 custom_app._lifespan）
    # 运维：uv run python -m agent.subagents.pending_terminal --status | --replay
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.utils import sqlite_paths
from agent.utils.prom_metrics import metered_rlock
from agent.utils.sqlite_paths import resolve_store_db

_logger = logging.getLogger(__name__)

# P2-3：带计量的可重入锁（store="pending_terminal"）——与其余存储同一套等锁埋点
_LOCK = metered_rlock("pending_terminal")

DEFAULT_MAX_AGE_SECONDS = 6 * 3600.0   # 超过此时长仍写不进 → abandoned
DEFAULT_KEEP_SECONDS = 7 * 86400.0     # 行保留时长（abandoned 也留，供排查）
DEFAULT_INTERVAL_SECONDS = 60.0        # 补写器轮询间隔

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_terminal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    main_thread_id  TEXT NOT NULL,
    sub_thread_id   TEXT NOT NULL,
    agent_name      TEXT NOT NULL DEFAULT '',
    run_status      TEXT NOT NULL,
    watched_run_id  TEXT NOT NULL DEFAULT '',
    steps_json      TEXT NOT NULL DEFAULT '',   -- '' = 重放时按子线程重算
    task_json       TEXT NOT NULL DEFAULT '{}',
    error           TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'pending',  -- pending|abandoned
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_terminal_state ON pending_terminal(state, id);
-- 幂等：同一 (子线程, 盯的 run) 同时只有一行 pending（watcher 重试/重复放弃不会翻倍）
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_terminal_pending
    ON pending_terminal(sub_thread_id, watched_run_id) WHERE state = 'pending';
"""

# ── 进程内单例 ──────────────────────────────────────────────
_STORE: Optional["PendingTerminalStore"] = None
_STORE_LOCK = threading.Lock()
_STOP = threading.Event()
_REAPER: Optional[threading.Thread] = None
_REAPER_LOCK = threading.Lock()
# 立即补写一次的信号（登记后不用等下一个 60s 周期）
_WAKE = threading.Event()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        _logger.warning("[pending-terminal] %s=%r 不是数字，退回默认 %s", name, raw, default)
        return default


def _db_path() -> Path:
    """登记表路径：`<AGENT_DATA_ROOT>/pending_terminal/pending_terminal.sqlite`（一库一目录）。

    2026-09-25 起从数据根目录归位到同名子目录（库 + `-wal` + `-shm` 同处一目录；根上的
    老三件套由 `agent.utils.sqlite_paths` 首次建连时接管 —— 那里面可能还躺着**没补写成功
    的终态行**，绝不能丢）。data_root 解析失败时回退 AGENT_DATA_ROOT 环境变量（再不行当前
    目录），保证 CLI / 缺配置场景也能落地（对齐 eval_queue 先例）。
    """
    return resolve_store_db(sqlite_paths.data_root(), "pending_terminal")


class PendingTerminalStore:
    """待补写终态 SQLite 存储（进程级 RLock 串行 + 单连接 check_same_thread=False）。"""

    def __init__(self, path: Path | None = None) -> None:
        p = path or _db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self._path = p
        self._conn = sqlite3.connect(str(p), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    # ── 写 ────────────────────────────────────────────────
    def record(
        self,
        main_thread_id: str,
        sub_thread_id: str,
        agent_name: str,
        run_status: str,
        watched_run_id: str = "",
        steps: Optional[list] = None,
        task: Optional[dict] = None,
        error: str = "",
    ) -> Optional[int]:
        """登记一条待补写终态（同 (子线程, run) 已存在 pending 时更新而非新增）。"""
        steps_json = json.dumps(steps, ensure_ascii=False) if steps else ""
        task_json = json.dumps(task or {}, ensure_ascii=False)
        now = _now()
        with _LOCK:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO pending_terminal
                        (main_thread_id, sub_thread_id, agent_name, run_status,
                         watched_run_id, steps_json, task_json, error, state,
                         created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,'pending',?,?)
                    """,
                    (
                        main_thread_id, sub_thread_id, agent_name, run_status,
                        watched_run_id, steps_json, task_json, error, now, now,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # 已有 pending 行（同一 run）：更新载荷即可，不新增
                self._conn.execute(
                    """UPDATE pending_terminal
                          SET run_status=?, steps_json=?, task_json=?, error=?, updated_at=?
                        WHERE sub_thread_id=? AND watched_run_id=? AND state='pending'""",
                    (
                        run_status, steps_json, task_json, error, now,
                        sub_thread_id, watched_run_id,
                    ),
                )
                self._conn.commit()
                row = self._conn.execute(
                    """SELECT id FROM pending_terminal
                        WHERE sub_thread_id=? AND watched_run_id=? AND state='pending'""",
                    (sub_thread_id, watched_run_id),
                ).fetchone()
                return int(row["id"]) if row else None

    def clear(self, row_id: int) -> None:
        """补写成功 / 判定无需补写 → 删行（facts 已进 state 与日志）。"""
        with _LOCK:
            self._conn.execute("DELETE FROM pending_terminal WHERE id=?", (int(row_id),))
            self._conn.commit()

    def mark_abandoned(self, row_id: int, reason: str = "") -> None:
        with _LOCK:
            self._conn.execute(
                "UPDATE pending_terminal SET state='abandoned', last_error=?, updated_at=? WHERE id=?",
                (str(reason)[:500], _now(), int(row_id)),
            )
            self._conn.commit()

    def bump_attempt(self, row_id: int, err: str = "") -> int:
        with _LOCK:
            self._conn.execute(
                "UPDATE pending_terminal SET attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
                (str(err)[:500], _now(), int(row_id)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT attempts FROM pending_terminal WHERE id=?", (int(row_id),)
            ).fetchone()
            return int(row["attempts"]) if row else 0

    def purge_old(self, keep_seconds: float | None = None) -> int:
        """删除建行超过 keep_seconds 的行（默认 7 天；abandoned 也清，避免无界增长）。"""
        keep = DEFAULT_KEEP_SECONDS if keep_seconds is None else keep_seconds
        cutoff = datetime.now(timezone.utc).timestamp() - keep
        with _LOCK:
            rows = self._conn.execute(
                "SELECT id, created_at FROM pending_terminal"
            ).fetchall()
            victims = []
            for r in rows:
                try:
                    ts = datetime.fromisoformat(str(r["created_at"])).timestamp()
                except Exception:  # noqa: BLE001  时间戳坏了不删（宁可留痕）
                    continue
                if ts < cutoff:
                    victims.append(int(r["id"]))
            for vid in victims:
                self._conn.execute("DELETE FROM pending_terminal WHERE id=?", (vid,))
            if victims:
                self._conn.commit()
            return len(victims)

    # ── 读（也进锁：共享连接上并发读会互踩，见 sqlite-shared-conn-lock-all-access）──
    def rows(self, state: str = "pending") -> list[dict]:
        with _LOCK:
            rs = self._conn.execute(
                "SELECT * FROM pending_terminal WHERE state=? ORDER BY id", (state,)
            ).fetchall()
            return [dict(r) for r in rs]

    def count(self, state: str = "pending") -> int:
        with _LOCK:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM pending_terminal WHERE state=?", (state,)
            ).fetchone()
            return int(r["n"]) if r else 0


def get_store() -> "PendingTerminalStore":
    """进程内单例 store（并发首访只建一条连接）。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = PendingTerminalStore()
        return _STORE


# ── watcher 侧入口 ────────────────────────────────────────────


def record_terminal(
    main_thread_id: str,
    sub_thread_id: str,
    agent_name: str,
    run_status: str,
    watched_run_id: str = "",
    steps: Optional[list] = None,
    task: Optional[dict] = None,
    error: str = "",
) -> Optional[int]:
    """登记待补写终态（best-effort：登记失败只记日志，绝不影响 watcher 收尾/停机）。

    顺手唤醒补写器（登记后不必等下一个轮询周期）。
    """
    try:
        rid = get_store().record(
            main_thread_id, sub_thread_id, agent_name, run_status,
            watched_run_id=watched_run_id or "", steps=steps,
            task=task, error=error,
        )
        _logger.error(
            "[pending-terminal] 已登记待补写终态 sub=%s status=%s run=%s row=%s"
            "（watcher 放手时终态仍未写入主线程，多半是 409 Thread is busy；由补写器接手）",
            sub_thread_id[:8], run_status, str(watched_run_id)[:8], rid,
        )
        _WAKE.set()
        return rid
    except Exception as e:  # noqa: BLE001
        _logger.error(
            "[pending-terminal] 登记失败（该终态将无人补写）: %s", str(e)[:200]
        )
        return None


# ── 补写器 ────────────────────────────────────────────────────


def _is_thread_busy(exc: BaseException) -> bool:
    """判断异常是否为「主线程忙」（409 / in-flight）——这种是**可自愈**的，必须重试。

    不只看 status_code：上游不同版本的文案不同（实测 409 detail 为
    "Thread is busy with a running job. Cannot update state."，历史日志里出现过
    "has in-flight runs"），故文案兜底。判错的代价是「少重试一次」，不致命。
    """
    code = getattr(exc, "status_code", None)
    if code == 409:
        return True
    text = str(exc).lower()
    return "busy" in text or "in-flight" in text or "inflight" in text


async def replay_once(dry_run: bool = False) -> dict:
    """补写一轮：把所有 pending 行尽力写回主线程 state。

    返回 {"scanned","written","dropped","busy","failed","abandoned","error"}。
    不抛异常（补写器的调用方是后台线程/CLI，必须能一直活下去）。
    """
    out = {
        "scanned": 0, "written": 0, "dropped": 0, "busy": 0,
        "failed": 0, "abandoned": 0, "error": "",
    }
    try:
        store = get_store()
        rows = store.rows("pending")
        max_age = _env_float(
            "NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS", DEFAULT_MAX_AGE_SECONDS
        )
        out["scanned"] = len(rows)
        if not rows:
            return out
        for row in rows:
            try:
                await _replay_row(store, row, max_age, dry_run=dry_run, out=out)
            except Exception as e:  # noqa: BLE001  单行失败不影响其余行
                out["failed"] += 1
                _logger.warning(
                    "[pending-terminal] 补写行 %s 异常: %s", row.get("id"), str(e)[:200]
                )
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:300]
        _logger.warning("[pending-terminal] 补写轮次失败: %s", str(e)[:200])
    finally:
        try:
            store = get_store()
            n = store.purge_old()
            if n:
                _logger.info("[pending-terminal] 清理过期行 %d 条", n)
        except Exception:  # noqa: BLE001
            pass
    return out


async def _replay_row(store, row: dict, max_age: float, *, dry_run: bool, out: dict) -> None:
    """补写单行。三层判定（见模块头「安全规则」）→ 写 → 删行 + 触发续跑/失败汇报。"""
    # 惰性 import：sync_subagent_todos 会 import 本模块，模块级 import 会成环
    from agent.subagents.sync_subagent_todos import (
        _RUN_DONE_STATUSES,
        _extract_subagent_todos,
        _get_latest_run,
        _sync_update_state,
        _terminal_error,
        _maybe_report_failure,
        _notify_main_agent_continue,
        render_final_steps,
    )
    from langgraph_sdk import get_client

    row_id = int(row["id"])
    main_thread_id = str(row["main_thread_id"])
    sub_thread_id = str(row["sub_thread_id"])
    agent_name = str(row["agent_name"] or "nl2sql")
    run_status = str(row["run_status"] or "success")
    watched = str(row["watched_run_id"] or "")

    # 超龄 → 放弃（留痕）：主线程真的长期写不进去，不再占补写轮次
    try:
        age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
            str(row["created_at"])
        ).timestamp()
    except Exception:  # noqa: BLE001
        age = 0.0
    if max_age > 0 and age > max_age:
        if not dry_run:
            store.mark_abandoned(row_id, f"超龄 {int(age)}s 仍写不进去（主线程长期忙？）")
        out["abandoned"] += 1
        _logger.error(
            "[pending-terminal] 放弃补写 sub=%s（登记于 %ss 前）：主线程持续忙，"
            "进度卡会停在「执行中」——按 /metrics 的 nl2sql_run_queue_* 与 "
            "`[alert] run_backlog` 排查是否有 run 卡死",
            sub_thread_id[:8], int(age),
        )
        return

    api_url = os.getenv("LANGGRAPH_API_URL", "http://localhost:2026")
    client = get_client(url=api_url)

    # 判定 1：子线程最新 run 必须还是 watcher 盯的那一个，且已是终态。
    # 被 update_async_task 重派发（新 run 在跑）→ 有新 watcher 负责本任务，丢行不写。
    latest = await _get_latest_run(client, sub_thread_id)
    latest_id = (latest or {}).get("run_id")
    latest_status = (latest or {}).get("status")
    if latest_id is None:
        store.bump_attempt(row_id, "子线程暂无 run（稍后重试）")
        out["busy"] += 1
        return
    if watched and latest_id != watched:
        if dry_run:
            out["dropped"] += 1
            return
        store.clear(row_id)
        out["dropped"] += 1
        _logger.info(
            "[pending-terminal] 丢行 row=%s：子线程已重派发新 run %s（旧 %s），交新 watcher",
            row_id, str(latest_id)[:8], watched[:8],
        )
        return
    if latest_status not in _RUN_DONE_STATUSES:
        if dry_run:
            out["dropped"] += 1
            return
        store.clear(row_id)
        out["dropped"] += 1
        _logger.info(
            "[pending-terminal] 丢行 row=%s：子线程最新 run %s 状态 %s 非终态（任务仍在跑）",
            row_id, str(latest_id)[:8], latest_status,
        )
        return

    # 判定 2：主线程 async_tasks[task] 已是终态 → 别人写过了，幂等丢行
    try:
        st = await client.threads.get_state(thread_id=main_thread_id)
        values = (st or {}).get("values") or {}
    except Exception as e:  # noqa: BLE001
        store.bump_attempt(row_id, f"读主线程 state 失败: {e}")
        out["busy"] += 1
        return
    tasks = values.get("async_tasks") or {}
    entry = tasks.get(sub_thread_id)
    if isinstance(entry, dict) and entry.get("status") in _RUN_DONE_STATUSES:
        if not dry_run:
            store.clear(row_id)
        out["dropped"] += 1
        _logger.info(
            "[pending-terminal] 丢行 row=%s：async_tasks[%s] 已是 %s（别处已写）",
            row_id, sub_thread_id[:8], entry.get("status"),
        )
        return

    # 判定 3（仅 dry_run）：以上都过了 → 本轮会写
    if dry_run:
        out["written"] += 1
        return

    # ── 组载荷 ──
    base = dict(json.loads(row["task_json"] or "{}"))
    base["task_id"] = sub_thread_id
    base["agent_name"] = agent_name
    base["run_id"] = latest_id
    base["status"] = run_status
    if isinstance(entry, dict) and entry.get("failure_reported"):
        base["failure_reported"] = True
    if isinstance(entry, dict) and entry.get("description") and not base.get("description"):
        base["description"] = entry["description"]
    err = str(row["error"] or "")
    if not err:
        try:
            err = await _terminal_error(client, sub_thread_id, run_status) or ""
        except Exception:  # noqa: BLE001
            err = ""
    if err:
        base["error"] = err
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base["last_checked_at"] = now_str
    base["last_updated_at"] = now_str

    payload: dict = {
        "async_tasks": {sub_thread_id: base},
        "active_queries": {sub_thread_id: False},
    }
    # 最终步骤：登记时快照的优先；没快照就用子线程 todos 现算（渲染逻辑与 watcher 共用）
    steps = None
    raw_steps = str(row["steps_json"] or "")
    if raw_steps:
        try:
            steps = json.loads(raw_steps)
        except Exception:  # noqa: BLE001
            steps = None
    if steps is None:
        try:
            todos = await _extract_subagent_todos(client, sub_thread_id)
            if todos:
                steps = render_final_steps(
                    todos, agent_name, sub_thread_id, run_status
                )
        except Exception as e:  # noqa: BLE001
            _logger.debug("[pending-terminal] 重算最终步骤失败(继续写终态): %s", e)
    if steps:
        payload["subagent_steps_map"] = {sub_thread_id: steps}

    # ── 写（与 watcher 同一把进程级写锁）──
    try:
        await asyncio.to_thread(_sync_update_state, main_thread_id, payload)
    except Exception as e:  # noqa: BLE001
        if _is_thread_busy(e):
            n = store.bump_attempt(row_id, f"409 busy: {e}")
            out["busy"] += 1
            _logger.info(
                "[pending-terminal] 主线程仍忙，保留待补写 sub=%s（第 %d 次）",
                sub_thread_id[:8], n,
            )
        else:
            store.bump_attempt(row_id, f"写入失败: {e}")
            out["failed"] += 1
            _logger.warning(
                "[pending-terminal] 补写失败 sub=%s: %s", sub_thread_id[:8], str(e)[:200]
            )
        return

    store.clear(row_id)
    out["written"] += 1
    _logger.warning(
        "[pending-terminal] 补写成功 sub=%s status=%s（步骤 %d 项；watcher 已于 %ss 前放弃）",
        sub_thread_id[:8], run_status, len(steps or []), int(age),
    )

    # 补写成功才算「终态落地」→ 触发下游（watcher 放弃时从未触发过，不会重复）
    if run_status == "success":
        try:
            await _notify_main_agent_continue(client, main_thread_id, agent_name)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[pending-terminal] 补触发续跑失败: %s", str(e)[:200])
    elif run_status in ("error", "timeout", "cancelled"):
        try:
            await _maybe_report_failure(
                client, main_thread_id, sub_thread_id, agent_name, run_status
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("[pending-terminal] 补触发失败汇报失败: %s", str(e)[:200])


def _reaper_loop() -> None:
    """补写线程主体：先立即跑一轮（重启后残留行尽快落地），随后按间隔轮询。"""
    interval = _env_float(
        "NL2SQL_PENDING_TERMINAL_INTERVAL_SECS", DEFAULT_INTERVAL_SECONDS
    )
    loop = asyncio.new_event_loop()
    try:
        while not _STOP.is_set():
            try:
                res = loop.run_until_complete(replay_once())
                if res.get("written") or res.get("dropped") or res.get("abandoned"):
                    _logger.info("[pending-terminal] 补写轮次: %s", res)
            except Exception as e:  # noqa: BLE001
                _logger.warning("[pending-terminal] 补写轮次异常: %s", str(e)[:200])
            _WAKE.wait(timeout=max(interval, 1.0))
            _WAKE.clear()
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def start_reaper() -> bool:
    """启动补写器（幂等：已在跑就返回 False）。进程 lifespan 里调用。"""
    global _REAPER
    with _REAPER_LOCK:
        if _REAPER is not None and _REAPER.is_alive():
            return False
        _STOP.clear()
        _WAKE.clear()
        _REAPER = threading.Thread(
            target=_reaper_loop, daemon=True, name="pending-terminal-reaper"
        )
        _REAPER.start()
        _logger.info("[pending-terminal] 补写器已启动")
        return True


def stop_reaper(timeout: float = 2.0) -> None:
    """停补写器（不抛异常、不阻断停机；残留行下次启动继续补）。"""
    _STOP.set()
    _WAKE.set()  # 立刻从等待中醒来，别等满一个周期
    with _REAPER_LOCK:
        t = _REAPER
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


def status_summary() -> dict:
    """运维/自检用：待补写与已放弃行数 + 库路径。"""
    try:
        store = get_store()
        return {
            "path": str(store.path),
            "pending": store.count("pending"),
            "abandoned": store.count("abandoned"),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


# ── CLI ───────────────────────────────────────────────────────


def _cli() -> int:
    ap = argparse.ArgumentParser(description="子任务终态待补写登记表（P2-4）")
    ap.add_argument("--status", action="store_true", help="查看待补写/已放弃行数")
    ap.add_argument("--list", action="store_true", help="列出待补写行")
    ap.add_argument("--replay", action="store_true", help="立刻补写一轮")
    ap.add_argument("--dry-run", action="store_true", help="只判定不写（配 --replay）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.status or not (args.list or args.replay):
        print(json.dumps(status_summary(), ensure_ascii=False, indent=2))
    if args.list:
        for r in get_store().rows("pending"):
            print(
                json.dumps(
                    {
                        "id": r["id"],
                        "sub": r["sub_thread_id"][:8],
                        "status": r["run_status"],
                        "attempts": r["attempts"],
                        "created_at": r["created_at"],
                        "last_error": r["last_error"],
                    },
                    ensure_ascii=False,
                )
            )
    if args.replay:
        res = asyncio.run(replay_once(dry_run=args.dry_run))
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
