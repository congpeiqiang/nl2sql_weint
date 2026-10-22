"""定时任务 worker：tick 线程 + 执行线程 + fire/reconcile（方案 §3/§4）。

生命周期：``custom_app._lifespan`` yield 前 ``start_worker()``、停机段**最先**
``stop_worker()``（排在 ``api.drain.begin_drain()`` 之前 —— 排空窗口内不再产生
新 run）。起不来只 warning，绝不拦启动（与 reaper/maintenance/warmer 三先例同款）。

结构（两个 daemon 线程 + 一个队列）：
- **tick 线程**：每 60s 一轮纯同步 SQLite 工作 —— 抢 ``.tick.lock``（蓝绿两色
  并存时只有一边 tick）→ 取 due（上限内才认领）→ CAS claim → 投递 fire/reconcile
  到队列 → 心跳落盘。文件/SQLite 操作不落在事件循环上（P1-14 口径）。
- **exec 线程**：消费队列，每项 ``asyncio.run``（后台线程没有主进程事件循环，
  与 sync_subagent_todos 同款做法）。fire 是 fire-and-forget：不等 run 结束，
  终态由后续 tick 的 reconcile 收口；run 挂死不自建超时 —— 既有看门狗
  （600s 无进展/3600s 绝对）会把它打成终态。

at-most-once：claim 在**执行前**原子推进 next_run_at（store CAS），进程中途死掉
最多丢一次触发；fire 中途死掉的 FIRING 占位按 lease（updated_at 超龄）回收。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from agent.scheduler import service
from agent.scheduler.store import FIRING, ScheduledTaskStore, get_store
from agent.utils.sdk_errors import is_thread_gone

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
FIRE_PER_TICK = 3
"""每轮至多认领并派发 3 个 —— 全局只有 10 个 run 槽，交互式问数优先（方案 §8）。"""

GRACE_MIN_S = 120.0
GRACE_MAX_S = 7200.0
FIRING_LEASE_S = 900
"""在飞占位的回收 lease：> 模型单次超时 300s + 建线程余量；真在跑的 run 用真实
thread_id（不是占位），不受此 lease 影响。"""

MAX_FIRE_RETRY = 3
FIRE_RETRY_DELAY_S = 300
"""fire 失败回拨 5min 重试（排空 503 / run 槽满是瞬时态，蓝绿窗口天然自愈）。"""

RECONCILE_GIVEUP_S = 4200
"""真实 thread 在飞的 give-up lease（review C-1）：> 看门狗绝对上限 3600s + 600s 余量。

run 记录不跨进程存活（inmem 重启即清）——发版/重启后 ``runs.list`` 恒空、404
（thread 被删）或恒非终态（看门狗也失灵）时，若无限「下轮再收」，任务会永久卡在
running_thread_id 上（due 选取排除在飞行、FIRING lease 只认占位符 ``-``）。超龄一律
按 interrupted 收口，任务回到可调度。**必须远大于**排空窗口（600s）与看门狗绝对上限：
活着的 run 会先被看门狗打终态、被正常 reconcile 收走，轮不到 give-up；蓝绿另一色
暂时看不到 run 记录也只是延迟收口，绝不误杀在飞（顶多把已完成的 run 记成
interrupted —— 无损正确性）。"""

TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted", "cancelled"})
"""与 api/task_cancel.py 的终态集合同口径。"""

RUNS_RETENTION_DAYS = 90
PURGE_INTERVAL_S = 3600
"""task_runs 流水保留 90 天（方案 §8），每 1h 清一次 —— 不每 tick 全表 DELETE。

⚠️ 「[定时]」会话 **thread** 的清理（每任务保留最近 30 个）**尚未实施**：删 thread
是破坏性操作，既有 retention 维护线程只覆盖文件/trace/eval_queue/feedback，不碰
langgraph threads —— 归属后续阶段，单独 E2E 后再上（见方案 §8 标注）。"""

_QUEUE: "queue.Queue[tuple[str, str]]" = queue.Queue()
_stop = threading.Event()
_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()

_counters = {"ticks": 0, "fires_ok": 0, "fires_error": 0, "folded": 0, "cas_lost": 0}
_last_tick_at = ""
_last_purge_iso = ""


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ── grace / misfire（hermes jobs.py 同款取舍）─────────────────────────


def grace_seconds(kind: str, expr: str, tz: Optional[str] = None) -> float:
    """misfire 宽限 = 周期一半，clamp [120s, 2h]；once/算不出 → 下限 120s。

    对**已入库**的行宽容解析（创建期校验在 service；这里再抛错只会把能跑的任务打死）。
    """
    period: Optional[float] = None
    try:
        if kind == "interval":
            period = float(int(str(expr).strip()) * 60)
        elif kind == "cron":
            period = service.estimate_period_seconds(kind, expr, tz)
    except Exception:
        period = None
    if not period or period <= 0:
        return GRACE_MIN_S
    return max(GRACE_MIN_S, min(GRACE_MAX_S, period / 2.0))


def due_action(row: dict[str, Any], now: datetime) -> tuple[str, bool]:
    """到点行 → (新的 next_run_at, 是否折叠)。

    - grace 内迟到：下一次从**原时间点**顺延（保节奏，hermes catch-up）；
    - 超 grace：折叠全部积压，next 快进到 now 之后 —— **但仍会执行一次**
      （既防重启 burst 刷屏，又防「运行时长>间隔」的任务被永久跳过，#33315）；
    - once：new_next="" （claim 内部落 completed，预扣在先）；
    - 快进后仍落在过去的（grace 下限 > 周期的 dev 放宽场景）持续前推，绝不
      crash-loop 追帧。
    """
    kind = row["sched_kind"]
    expr = row["sched_expr"]
    tz = row.get("tz") or None
    if kind == "once":
        return "", False
    try:
        orig = datetime.fromisoformat(row["next_run_at"])
    except (ValueError, TypeError):
        orig = now
    age = (now - orig).total_seconds()
    folded = age > grace_seconds(kind, expr, tz)
    base = now if folded else orig
    now_iso = _utc_iso(now)
    new_next = service.next_run_after(kind, expr, tz, after=base)
    guard = 0
    while new_next <= now_iso and guard < 2000:
        new_next = service.next_run_after(
            kind, expr, tz, after=datetime.fromisoformat(new_next)
        )
        guard += 1
        folded = True
    return new_next, folded


# ── 跨进程 tick 锁（蓝绿双色共存；hermes gateway/desktop 同款方案）──────


def acquire_tick_lock(path: Path | str):
    """非阻塞抢锁；抢不到返回 None（本轮 tick 直接放弃）。"""
    try:
        f = open(path, "a+")
    except OSError as exc:
        logger.warning("sched: tick.lock 打不开 %s: %s", path, exc)
        return None
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def release_tick_lock(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            handle.close()
        except Exception:
            pass


# ── tick ──────────────────────────────────────────────────────────────


def tick_once(store: Optional[ScheduledTaskStore] = None, now: Optional[datetime] = None) -> dict:
    """一轮调度（同步、纯 SQLite + 队列投递）。返回本轮摘要（测试/日志用）。"""
    global _last_tick_at
    st = store if store is not None else get_store()
    now = now or _now()
    now_iso = _utc_iso(now)
    summary: dict[str, Any] = {"fired": [], "folded": 0, "reconciles": 0, "purged": 0, "lock": False}

    handle = acquire_tick_lock(st.db_path.parent / ".tick.lock")
    if handle is None:
        return summary  # 另一进程/另一色在 tick —— 静默让位
    summary["lock"] = True
    try:
        for row in st.due_tasks(now_iso, limit=FIRE_PER_TICK):
            try:
                new_next, folded = due_action(row, now)
            except Exception as exc:  # 表达式已不可解析（改过 env 下限等）→ 打 error 停调度
                logger.warning("sched: due_action 失败 id=%s: %s", row["id"], exc)
                st.update_task(row["id"], {"state": "error", "last_error": str(exc)[:2000]})
                continue
            if st.claim(row["id"], row["next_run_at"], new_next, now_iso):
                if folded:
                    summary["folded"] += 1
                    _counters["folded"] += 1
                    logger.info("sched: 任务 %s 超 grace 折叠积压（仍执行一次）", row["id"])
                submit_fire(row["id"])
                summary["fired"].append(row["id"])
                _counters["fires_ok"] += 1
            else:
                _counters["cas_lost"] += 1  # 手动 run / 另一色抢先 —— 正常互斥

        for row in st.inflight_tasks():
            rtid = row["running_thread_id"]
            if rtid == FIRING:
                try:
                    age = (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
                except (ValueError, TypeError):
                    age = FIRING_LEASE_S + 1
                if age > FIRING_LEASE_S:
                    st.clear_stale_running(
                        row["id"], "触发中断（进程重启/超时），本次按错过处理"
                    )
                    logger.warning("sched: 回收超龄在飞占位 id=%s", row["id"])
            else:
                submit_reconcile(row["id"])
                summary["reconciles"] += 1

        summary["purged"] = _maybe_purge_runs(st, now, now_iso)

        _counters["ticks"] += 1
        _last_tick_at = now_iso
        _write_heartbeat(st, now_iso)
    finally:
        release_tick_lock(handle)
    return summary


def _maybe_purge_runs(st: ScheduledTaskStore, now: datetime, now_iso: str) -> int:
    """task_runs 90 天清理，每 PURGE_INTERVAL_S 至多一次（进程内时间戳节流）。

    review I-4：purge_old_runs 此前是死代码，task_runs 每 fire 一行永续累积。
    节流状态是进程内的 —— 蓝绿/重启后首 tick 会各清一次，DELETE 幂等无害。
    """
    global _last_purge_iso
    if _last_purge_iso:
        try:
            since = (now - datetime.fromisoformat(_last_purge_iso)).total_seconds()
        except (ValueError, TypeError):
            since = PURGE_INTERVAL_S
        if since < PURGE_INTERVAL_S:
            return 0
    cutoff = _utc_iso(now - timedelta(days=RUNS_RETENTION_DAYS))
    try:
        purged = st.purge_old_runs(cutoff)
    except Exception:  # noqa: BLE001  清理失败不影响调度
        logger.warning("sched: task_runs 清理失败（下轮再试）", exc_info=True)
        return 0
    _last_purge_iso = now_iso
    if purged:
        logger.info("sched: 清理过期 task_runs %s 行（cutoff=%s）", purged, cutoff)
    return purged


def _write_heartbeat(st: ScheduledTaskStore, now_iso: str) -> None:
    """心跳文件：进程活着 ≠ 线程活着 ≠ tick 持续成功 —— 三态诊断的第三态（hermes 同款）。"""
    try:
        hb = st.db_path.parent / "heartbeat"
        hb.write_text(
            json.dumps({"last_tick_at": now_iso, **_counters}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("sched: 心跳写入失败: %s", exc)


# ── 队列投递（tick → exec 线程）──────────────────────────────────────


def submit_fire(task_id: str) -> None:
    _QUEUE.put(("fire", task_id))


def submit_reconcile(task_id: str) -> None:
    _QUEUE.put(("reconcile", task_id))


def _exec_loop() -> None:
    while not _stop.is_set():
        try:
            kind, task_id = _QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            if kind == "fire":
                asyncio.run(_fire(task_id))
            else:
                asyncio.run(_reconcile_task(task_id))
        except Exception:
            logger.exception("sched: %s 处理失败 id=%s", kind, task_id)


def _tick_loop() -> None:
    while not _stop.is_set():
        try:
            tick_once()
        except Exception:
            logger.exception("sched: tick 失败（下一轮继续）")
        _stop.wait(TICK_SECONDS)


# ── fire（方案 §4：完全复用服务端自建 run 链路）───────────────────────


def _make_client():
    """HTTP 自调用（不是 in-process ASGI —— 后台线程没有主进程 app 上下文，
    get_client(url=None) 会炸；sync_subagent_todos.py:312 有同款注释）。"""
    from langgraph_sdk import get_client

    from api._common import _api_base_url

    return get_client(url=_api_base_url())


async def _fire(
    task_id: str,
    store: Optional[ScheduledTaskStore] = None,
    now: Optional[datetime] = None,
    client: Any = None,
) -> None:
    st = store if store is not None else get_store()
    row = st.get_task(task_id)
    if row is None or not row["running_thread_id"]:
        return  # 已删除 / 已被 lease 回收
    now = now or _now()
    now_iso = _utc_iso(now)

    # 触发前复验库权限（授权可能已被回收；fail-closed：验不过记 error 不 fire，
    # 且不回拨重试 —— 回收不是瞬时态，重试只会每周期白撞一次）
    if not service.recheck_db_access(row["owner"], row["db_name"]):
        _fail_fire(st, row, now, f"无权访问数据库: {row['db_name']}（授权已回收）", retry=False)
        return

    try:
        client = client if client is not None else _make_client()
        thread = await client.threads.create(
            metadata={"scheduled_task_id": row["id"], "title": f"[定时] {row['name']}"}
        )
        thread_id = thread["thread_id"]
        content = (
            f"[定时任务] {row['name']}：{row['prompt']}"
            "（这是定时自动执行，请直接完成数据查询并给出结果）"
        )
        configurable: dict[str, Any] = {
            # 契约与 _run_context_config（sync_subagent_todos.py:1945）逐字一致；
            # db_name 必须带 —— 缺了它主 agent 系统提示丢「当前数据库」段、
            # 报告口径核验永久降级（2026-09-26 生产实证）。
            "langgraph_auth_user_id": row["owner"],
            "user_id": row["owner"],
            "db_name": row["db_name"],
            # 定时 run 标记：主 agent 的 HITL 审批闸门据此跳过审批——后台 run 无人
            # 值守，interrupt 会被 langgraph 顶层图抑制（工具不执行、也没有卡片可点）。
            "scheduled_run": True,
        }
        if row["llm_route"]:
            configurable["llm_route"] = row["llm_route"]
        if row["llm_model"]:
            configurable["llm_model"] = row["llm_model"]
        run = await client.runs.create(
            thread_id=thread_id,
            assistant_id="chat_agent",
            input={"messages": [{"role": "user", "content": content}]},
            config={"recursion_limit": 500, "configurable": configurable},
        )
        run_id = run.get("run_id", "") if isinstance(run, dict) else ""
        st.record_run(task_id, thread_id, run_id, now_iso)
        st.set_running_thread(task_id, thread_id, run_id)
        # 归属自动正确：threads.create 钩子先打 internal，langfuse_metadata
        # ._apply_ownership 按 run configurable 的真实 user_id 补打（子 agent 同款）。
        logger.info("sched: 已触发任务 %s → thread %s run %s", task_id, thread_id, run_id)
    except Exception as exc:
        # runs.create 抛错（排空 503 / run 槽满 / API 抖动）→ 5min 回拨重试 ≤3 次
        logger.warning("sched: fire 失败 id=%s: %s", task_id, exc)
        _fail_fire(st, row, now, str(exc), retry=True)


def _fail_fire(
    st: ScheduledTaskStore, row: dict[str, Any], now: datetime, error: str, retry: bool
) -> None:
    task_id = row["id"]
    _counters["fires_error"] += 1
    retry_count = int(row["retry_count"]) + 1
    if not retry or row["sched_kind"] == "once" or row["state"] == "completed":
        # once 预扣已在 claim 耗尽 → 不重试；权限回收同理。保持 claim 排好的 next。
        st.fail_fire(task_id, row["next_run_at"], error, retry_count=0)
    elif retry_count <= MAX_FIRE_RETRY:
        retry_at = _utc_iso(now + timedelta(seconds=FIRE_RETRY_DELAY_S))
        st.fail_fire(task_id, retry_at, error, retry_count=retry_count)
    else:
        # 重试耗尽 → 折叠回正常周期（不算错过一轮，按错过处理）
        try:
            nxt = service.next_run_after(
                row["sched_kind"], row["sched_expr"], row["tz"], after=now
            )
        except Exception:
            nxt = row["next_run_at"]
        st.fail_fire(task_id, nxt, error, retry_count=0)


# ── reconcile（终态回写）──────────────────────────────────────────────


async def _reconcile_task(
    task_id: str,
    store: Optional[ScheduledTaskStore] = None,
    now: Optional[datetime] = None,
    client: Any = None,
) -> None:
    st = store if store is not None else get_store()
    row = st.get_task(task_id)
    if row is None:
        return
    rtid = row["running_thread_id"]
    if not rtid or rtid == FIRING:
        return  # 占位由 tick 的 lease 路径回收
    now = now or _now()
    stale = _inflight_age_seconds(row, now) > RECONCILE_GIVEUP_S

    def _give_up(reason: str) -> None:
        # 按 interrupted 收口：清在飞、写 last_status、task_runs 打终态。
        # complete_run 的 WHERE running_thread_id=? 守卫保证只清自己认领的那条。
        st.complete_run(task_id, rtid, "interrupted", reason[:2000], _utc_iso(now))
        logger.warning("sched: 任务 %s give-up 收口（%s）", task_id, reason)

    try:
        client = client if client is not None else _make_client()
        runs = await client.runs.list(thread_id=rtid, limit=5)
    except Exception as exc:
        # 会话被用户删了（langgraph DELETE 只删注册表行，不 cancel run）：这是
        # 确定性结论，不是瞬时故障 —— 立刻收口清 running_thread_id。否则
        # `due_tasks`（只选 running_thread_id=''）会一直跳过这个任务，要等
        # RECONCILE_GIVEUP_S=70min 超龄才恢复。语义：删会话只取消这一次 run，
        # **下次定时照跑**。
        if is_thread_gone(exc):
            _give_up(f"会话已删除（run 查询 {type(exc).__name__}），本次按取消处理")
            return
        if stale:
            _give_up(f"run 查询失败且在飞超龄（thread 可能被删）: {exc}")
            return
        logger.warning("sched: reconcile 查询失败 id=%s: %s（下轮再收）", task_id, exc)
        return
    if not runs:
        # run 记录不存在：inmem 重启即清 / thread 被删。超龄才收口 —— 未超龄可能
        # 刚 fire 或蓝绿另一色暂时看不到，下轮再看（绝不误杀在飞）。
        if stale:
            _give_up("run 记录丢失（进程重启/线程被删），在飞超龄")
        return
    run = runs[0] if isinstance(runs, list) else runs
    status = str(run.get("status") or "")
    if status not in TERMINAL_STATUSES:
        # 还在跑：挂死本应由既有看门狗（600s 无进展/3600s 绝对）打终态，下轮照常收。
        # 但看门狗也失灵、长期非终态且已超龄时 give-up，否则永久卡死。
        if stale:
            _give_up(f"run 长期非终态（status={status!r}）且在飞超龄，看门狗未收口")
        return
    finished = str(run.get("updated_at") or _utc_iso(now))
    error = "" if status == "success" else f"run 终态: {status}"
    st.complete_run(task_id, rtid, status, error, finished)
    logger.info("sched: 任务 %s 本轮收口 status=%s", task_id, status)


def _inflight_age_seconds(row: dict[str, Any], now: datetime) -> float:
    """在飞行 age = now - updated_at（set_running_thread 落地时写）。解析失败按超龄处理。"""
    try:
        return (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
    except (ValueError, TypeError, KeyError):
        return RECONCILE_GIVEUP_S + 1.0


# ── 生命周期 ──────────────────────────────────────────────────────────


def start_worker() -> None:
    """幂等；daemon 线程，随进程退出。"""
    with _threads_lock:
        if _threads and any(t.is_alive() for t in _threads):
            return
        _stop.clear()
        _threads.clear()
        tick_t = threading.Thread(target=_tick_loop, name="sched-tick", daemon=True)
        exec_t = threading.Thread(target=_exec_loop, name="sched-exec", daemon=True)
        _threads.extend([tick_t, exec_t])
        tick_t.start()
        exec_t.start()
    logger.info("sched: worker 已启动（tick=%ss, fire≤%s/tick）", TICK_SECONDS, FIRE_PER_TICK)


def stop_worker(timeout: float = 5.0) -> None:
    with _threads_lock:
        _stop.set()
        for t in _threads:
            t.join(timeout=timeout)
        _threads.clear()


def worker_alive() -> bool:
    with _threads_lock:
        return bool(_threads) and all(t.is_alive() for t in _threads)


def metrics_snapshot(store: Optional[ScheduledTaskStore] = None) -> dict[str, Any]:
    """/metrics 采样入口（worker 存活 + 心跳龄 + 计数 + 任务量）。"""
    tasks_enabled = 0
    try:
        st = store if store is not None else get_store()
        tasks_enabled = sum(
            1 for r in st.list_tasks(owner=None) if r["enabled"] and r["state"] == "scheduled"
        )
    except Exception:
        tasks_enabled = -1
    return {
        "alive": worker_alive(),
        "last_tick_at": _last_tick_at,
        "queue_depth": _QUEUE.qsize(),
        "tasks_enabled": tasks_enabled,
        **_counters,
    }
