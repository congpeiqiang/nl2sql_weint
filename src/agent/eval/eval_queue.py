# -*- coding: utf-8 -*-
"""LLM-judge 落盘待评队列（评估精准化设计方案 P0 §8.3 可靠交付）。

背景：schedule_judge 旧实现每次采样 spawn 一个 fire-and-forget daemon 线程，
进程崩溃/异常退出即丢任务、失败无重试无留痕。本模块把待评任务持久化到
`{AGENT_DATA_ROOT}/eval_queue.sqlite`，由单例守护 worker 拉取执行：
- 进程重启/崩溃后残留 pending/running 自动续跑（补评）；
- 同任务重试上限 `_MAX_ATTEMPTS`，超限置 failed 并留 last_error（可查）；
- 幂等去重：同一 (trace_id, kind) 同一时刻至多 1 条在途（部分唯一索引），
  已 done/failed 不阻塞后续同 key 新任务。

设计（对齐 feedback/store.py 先例）：
- 单连接 `check_same_thread=False` + 模块级 `threading.RLock` + `PRAGMA journal_mode=WAL`
  + 建表幂等；执行体（runner，含 LLM-judge 调用）由 evaluators 注入，本模块
  不 import evaluators（防循环 import）。

用法：
    # 在线：evaluators.schedule_judge 内 enqueue + ensure_worker(执行体)
    # 运维：uv run python -m agent.eval.eval_queue --status | --replay
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

_MAX_ATTEMPTS = 3          # 单任务最多尝试次数（超过置 failed 留痕）
_CLAIM_BATCH = 5           # 每轮领取任务数
_IDLE_SLEEP = 0.5          # 空队列轮询间隔（秒）
_RETRY_SLEEP = 1.0         # store 初始化异常退避（秒）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,
    trace_id         TEXT NOT NULL,
    question_thread  TEXT NOT NULL DEFAULT '',
    payload_json     TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eval_queue_state ON eval_queue(state, id);
-- 幂等去重：同一 (trace_id, kind) 同一时刻至多 1 条在途（pending|running）。
-- 已 done/failed 的行不参与，允许后续同 key 新任务继续入队。
CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_queue_pending
    ON eval_queue(trace_id, kind) WHERE state IN ('pending', 'running');
"""

# ── 进程内单例状态 ─────────────────────────────────────
_STORE: Optional["EvalQueueStore"] = None
_RUNNER: Optional[Callable] = None      # judge 执行体（evaluators 注入）
_STOP = threading.Event()
_WORKER: Optional[threading.Thread] = None
_WORKER_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    """队列文件路径：AGENT_DATA_ROOT/eval_queue.sqlite（data_root = AGENT_DATA_ROOT）。

    workspace_manager 解析失败时回退 AGENT_DATA_ROOT 环境变量（再不行当前目录），
    保证 CLI / 缺配置场景也能落地。
    """
    try:
        from agent.workspace_manager import get_workspace_manager  # 惰性，防 import 环

        root = Path(get_workspace_manager().data_root)
    except Exception:  # noqa: BLE001
        root = Path(os.getenv("AGENT_DATA_ROOT", "") or ".")
    p = root / "eval_queue.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class EvalQueueStore:
    """待评任务 SQLite 存储（进程锁串行读写，单连接 check_same_thread=False）。"""

    def __init__(self, path: Path | None = None) -> None:
        p = path or _db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self._path = p
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        _logger.info("[eval_queue] 待评队列就绪: %s", p)

    # ── 写路径 ──────────────────────────────────────────
    def enqueue(self, kind: str, trace_id: str, question_thread: str, payload: dict) -> bool:
        """入队；同 (trace_id, kind) 已有在途任务时静默跳过（INSERT OR IGNORE）。"""
        with _LOCK:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO eval_queue"
                    "(kind, trace_id, question_thread, payload_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, trace_id, question_thread,
                     json.dumps(payload, ensure_ascii=False), _now(), _now()),
                )
                self._conn.commit()
                return True
            except Exception as e:  # noqa: BLE001
                _logger.debug("[eval_queue] enqueue 失败: %s", e)
                return False

    def reset_running_to_pending(self) -> int:
        """崩溃恢复：上一进程残留的 running 置回 pending（幂等，worker 启动/重放前调用）。"""
        with _LOCK:
            try:
                cur = self._conn.execute(
                    "UPDATE eval_queue SET state='pending', updated_at=? WHERE state='running'",
                    (_now(),),
                )
                self._conn.commit()
                if cur.rowcount:
                    _logger.info("[eval_queue] 恢复 %d 条 running→pending", cur.rowcount)
                return cur.rowcount or 0
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval_queue] reset running→pending 失败: %s", e)
                return 0

    def claim(self, batch: int = _CLAIM_BATCH) -> list[dict]:
        """领取一批 pending 任务并置 running（锁内领取；执行在锁外）。"""
        with _LOCK:
            try:
                rows = self._conn.execute(
                    "SELECT id, kind, trace_id, question_thread, payload_json, attempts"
                    " FROM eval_queue WHERE state='pending' ORDER BY id LIMIT ?",
                    (batch,),
                ).fetchall()
                for r in rows:
                    self._conn.execute(
                        "UPDATE eval_queue SET state='running', updated_at=? WHERE id=?",
                        (_now(), r["id"]),
                    )
                self._conn.commit()
                return [dict(r) for r in rows]
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval_queue] claim 失败: %s", e)
                return []

    def finish(self, row_id: int, ok: bool, error: str = "") -> None:
        """任务收尾：成功置 done；失败 attempts+1（<_MAX_ATTEMPTS 回 pending 重试，否则 failed）。"""
        with _LOCK:
            try:
                row = self._conn.execute(
                    "SELECT attempts FROM eval_queue WHERE id=?", (row_id,)
                ).fetchone()
                attempts = row["attempts"] if row else 0
                state, err = "done", error
                if not ok:
                    attempts += 1
                    state = "failed" if attempts >= _MAX_ATTEMPTS else "pending"
                    err = (error or "")[:500]
                self._conn.execute(
                    "UPDATE eval_queue SET state=?, attempts=?, last_error=?, updated_at=?"
                    " WHERE id=?",
                    (state, attempts, err, _now(), row_id),
                )
                self._conn.commit()
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval_queue] finish(%s) 失败: %s", row_id, e)

    def stats(self) -> dict:
        """各 state 行数（运维/测试用）。"""
        with _LOCK:
            try:
                cur = self._conn.execute("SELECT state, count(*) AS n FROM eval_queue GROUP BY state")
                return {r["state"]: r["n"] for r in cur.fetchall()}
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval_queue] stats 失败: %s", e)
                return {}


def _get_store() -> EvalQueueStore:
    global _STORE
    if _STORE is None:
        _STORE = EvalQueueStore()
    return _STORE


# ── 对外 API（evaluators.schedule_judge 调用）────────────

def enqueue(kind: str, trace_id: str, question_thread: str = "",
            sql: str = "", result: str = "", report: str = "") -> bool:
    """入队一条 LLM-judge 待评任务（幂等；同步毫秒级，可于工具调用路径调用）。

    异常吞掉返回 False（评估是旁路，不影响主流程；与旧 daemon 线程启动失败同级）。
    """
    try:
        payload: dict = {}
        if sql:
            payload["sql"] = sql
        if result:
            payload["result"] = result
        if report:
            payload["report"] = report
        return _get_store().enqueue(kind, trace_id, question_thread, payload)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[eval_queue] 入队失败: %s", e)
        return False


def ensure_worker(runner: Callable | None = None) -> None:
    """确保单例守护 worker 在跑；首次/变更时注册 runner（judge 执行体）。

    worker 启动即把残留 running 置回 pending（崩溃续跑），此后循环领取执行。
    """
    global _RUNNER
    if runner is not None:
        _RUNNER = runner
    with _WORKER_LOCK:
        global _WORKER
        if _WORKER is not None and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_drain_loop, daemon=True, name="eval-drainer")
        _WORKER.start()
        _logger.info("[eval_queue] 守护 worker 已启动")


def stop_worker(timeout: float = 2.0) -> None:
    """请求 worker 停止（测试/优雅停机用）；不等正在跑的长任务。"""
    _STOP.set()
    with _WORKER_LOCK:
        t = _WORKER
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


def _run_row(store: EvalQueueStore, row: dict) -> None:
    """执行单条 judge 任务（runner 内部软失败静默；抛异常 → 记失败并重试/置 failed）。"""
    runner = _RUNNER
    if runner is None:
        raise RuntimeError("eval_queue runner 未注册")
    payload = json.loads(row.get("payload_json") or "{}")
    runner(
        kind=row["kind"],
        trace_id=row["trace_id"],
        question_thread=row.get("question_thread") or "",
        sql=payload.get("sql") or "",
        result=payload.get("result") or "",
        report=payload.get("report") or "",
    )


def _drain_loop() -> None:
    """守护 worker 主循环：领取 → 执行 → 落结果；空队列休眠。"""
    store: EvalQueueStore | None = None
    while not _STOP.is_set():
        if store is None:
            try:
                store = _get_store()
                store.reset_running_to_pending()
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval_queue] 初始化 store 失败，重试中: %s", e)
                time.sleep(_RETRY_SLEEP)
                continue
        batch = store.claim()
        if not batch:
            time.sleep(_IDLE_SLEEP)
            continue
        for row in batch:
            if _STOP.is_set():
                break
            try:
                _run_row(store, row)
                store.finish(row["id"], ok=True)
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval_queue] 任务失败(id=%s kind=%s): %s",
                                row["id"], row["kind"], e)
                store.finish(row["id"], ok=False, error=str(e))


def drain_all(runner: Callable | None = None) -> dict:
    """同步清空 pending（重启补评 / --replay / 测试用）。返回 {processed, failed}。

    与守护 worker 相同的领取/执行/收尾语义；单次调用直到无 pending 为止。
    """
    global _RUNNER
    if runner is not None:
        _RUNNER = runner
    store = _get_store()
    store.reset_running_to_pending()
    processed = failed = 0
    while True:
        batch = store.claim()
        if not batch:
            break
        for row in batch:
            try:
                _run_row(store, row)
                store.finish(row["id"], ok=True)
                processed += 1
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval_queue] replay 任务失败(id=%s): %s", row["id"], e)
                store.finish(row["id"], ok=False, error=str(e))
                failed += 1
    return {"processed": processed, "failed": failed}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="LLM-judge 落盘待评队列运维工具（设计文档 P0 §8.3）",
    )
    ap.add_argument("--status", action="store_true", help="打印各 state 计数")
    ap.add_argument("--replay", action="store_true",
                    help="同步清空 pending（崩溃补评；会触发真实 LLM-judge）")
    args = ap.parse_args(argv)
    if args.status:
        print("eval_queue state counts:", _get_store().stats())
        return 0
    if args.replay:
        from agent.eval.evaluators import _execute_judge_task  # 惰性，防 import 环

        res = drain_all(runner=_execute_judge_task)
        print("replay done:", res)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
