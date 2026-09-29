# -*- coding: utf-8 -*-
"""P2-3 指标与最小告警验证（离线，无需后端/数据库/网络）。

**要解决的**（清单原文）：加 `/metrics` + 最小告警；建议指标＝进程内存、**事件循环
延迟**、队列深度（`n_running`/`n_pending`）、LLM 耗时/失败率、SQLite 锁等待、MCP 子
进程数；验收＝**并发压测时能在面板上看到队列深度与事件循环延迟**。

本脚本验九段（每段都尽量打真东西，不 mock 掉被测对象）：
  ① **锁计量原语**（`agent.utils.prom_metrics.MeteredLock`）：真竞争下量出等待时长、
     可重入不误判、异常透传且锁一定释放、`__getattr__` 透传；**负对照**＝同样一把
     裸 `threading.RLock` 竞争后该标签的计数**不动**（证明断言不是恒真）。
  ② **采样**：`sample_once()` 真跑一轮（队列那一路注入确定值）→ 断言 gauge 与读数；
     无数据时**不写假 0**（宁可缺指标，也不要让内存/积压告警永不触发）。
  ③ **事件循环延迟**：真阻塞事件循环 → 采样任务量到 ≥0.3s；**负对照**＝不阻塞时
     同一指标回落到毫秒级，且 `_max` 保留历史峰值。
  ④ **告警**：连续 N 轮才叫、**只在翻转时叫一次**（不刷屏）、恢复时 `[alert-clear]`、
     阈值=0 关闭该条、事件计数型**首轮不把历史累计当新增**。
  ⑤ **端点**：真 Starlette + 真 `custom_app.ROUTES` 里的 `/metrics` → prometheus 文本
     含全部 `nl2sql_*` 家族 + `?format=json` + HEAD + `cache-control: no-store`。
  ⑥ **降级**：上游 `meta_metrics` 拿不到时仍出指标（只缺队列那部分）而不是 500。
  ⑦ **子进程计数**：`/proc` 解析用 fixture 树（含 `(comm 带空格与右括号)` 这个经典坑）
     + 非 Linux 降级为"不产出该指标"。
  ⑧ **接线与暴露面**：`/metrics` 在 ROUTES 里、lifespan 起停采样任务、6 个共享连接
     存储的锁都换成了计量的、**带 Cookie 的外部请求拿不到 /metrics**（真 AuthMiddleware）、
     **nginx 不把 /metrics 转给后端**（落在前端 → 外网路径根本到不了）。
  ⑨ **LLM 埋点**：真 `ModelTimeoutMiddleware` 三态（ok/timeout/error）与耗时
     （含"异步 handler 的耗时在 await 里，只量同步段会量成 0"这条）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_metrics.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time

# ⚠️ 必须在 import 业务模块之前：存储落点由 AGENT_DATA_ROOT 推导
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-metrics-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ.pop("NL2SQL_AUTH_DISABLED", None)
os.environ.pop("NL2SQL_FORCE_PASSWORD_CHANGE", None)
os.environ["NL2SQL_METRICS_INTERVAL_SECS"] = "1"  # 采样断言要快
# ⑤ 要解析真 `langgraph_api.api.meta`：它的 import 链要求这些配置齐（与 start_server.py 同）
os.environ.setdefault("ALLOW_PRIVATE_NETWORK", "true")
os.environ.setdefault("N_JOBS_PER_WORKER", "10")
os.environ.setdefault("DATABASE_URI", ":memory:")
os.environ.setdefault("REDIS_URI", "fake")
os.environ.setdefault("MIGRATIONS_PATH", "__inmem")
os.environ.setdefault("LANGGRAPH_RUNTIME_EDITION", "inmem")
os.environ.setdefault("LANGGRAPH_ALLOW_BLOCKING", "true")
os.environ.setdefault("LANGSERVE_GRAPHS", "{}")
os.environ.pop("LANGFUSE_ENABLE", None)

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[1]
_SRC = _REPO / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from prometheus_client import REGISTRY  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def sample_value(name: str, labels: dict | None = None) -> float | None:
    """读某一枚样本的当前值（prometheus_client 的公开读接口）。"""
    try:
        return REGISTRY.get_sample_value(name, labels or {})
    except Exception:  # noqa: BLE001
        return None


class _Capture(logging.Handler):
    """抓 `api.metrics` 的日志行（告警是"翻日志能看见"的那一层，必须真验）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass

    def take(self) -> list[str]:
        lines, self.lines = self.lines, []
        return lines


def _reset_alerts(api_metrics) -> None:
    """清空告警状态与差值基线（每段子断言从干净状态起步）。"""
    with api_metrics._STATE_LOCK:
        api_metrics._ALERT_STATE.clear()
    api_metrics._LAST_TICK.clear()


# ── ① 锁计量原语 ───────────────────────────────────────────────────
def verify_metered_lock() -> None:
    section("① 锁计量原语（MeteredLock）")
    from agent.utils.prom_metrics import (
        LOCK_CONTENTION_TOTAL,
        MeteredLock,
        metered_rlock,
    )

    store = "verify-①"
    lock = metered_rlock(store)

    before = sample_value("nl2sql_sqlite_lock_contention_total", {"store": store}) or 0.0

    # 竞争：另一个线程持锁 0.25s，主线程排队（观测到的等待必须真的到 0.2s 量级）
    holder_ready = threading.Event()

    def _hold() -> None:
        with lock:
            holder_ready.set()
            time.sleep(0.25)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    holder_ready.wait(2)
    t0 = time.perf_counter()
    with lock:
        waited = time.perf_counter() - t0
    holder.join(2)
    check(waited >= 0.2, "真竞争下等锁被量到（≥0.2s）", f"实测 {waited:.3f}s")
    after = sample_value("nl2sql_sqlite_lock_contention_total", {"store": store}) or 0.0
    check(after > before, "竞争计数 +1（写进 nl2sql_sqlite_lock_contention_total）", f"{before}→{after}")
    check(sample_value("nl2sql_sqlite_lock_waits_total", {"store": store}) >= 2, "进入次数被记（waits_total ≥ 2）")

    # 负对照：同一把锁的裸版竞争后，该标签的计数**不动**（证明上面的断言非恒真）
    bare = threading.RLock()
    bare_before = sample_value("nl2sql_sqlite_lock_contention_total", {"store": store}) or 0.0
    ready = threading.Event()

    def _hold_bare() -> None:
        with bare:
            ready.set()
            time.sleep(0.25)

    t = threading.Thread(target=_hold_bare, daemon=True)
    t.start()
    ready.wait(2)
    with bare:
        pass
    t.join(2)
    bare_after = sample_value("nl2sql_sqlite_lock_contention_total", {"store": store}) or 0.0
    check(bare_after == bare_before, "负对照：裸 RLock 竞争不产生任何样本（计数不变）", f"{bare_before}→{bare_after}")

    def _try_other_thread() -> bool:
        """从**另一个线程**试着取锁：能取到 = 真的没人持有（不可靠的"我以为释放了"没用）。

        为什么不用 `lock.locked()`：`threading.RLock()` 返回的 `_thread.RLock`
        **没有** `locked()`（那是 `Lock` 的），而本仓这些锁全是 RLock。
        """
        got: list[bool] = []

        def _probe() -> None:
            acquired = lock.acquire(blocking=False)
            got.append(acquired)
            if acquired:  # ⚠️ 必须在**取锁的那个线程**释放：RLock 归取它的线程所有
                lock.release()

        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(2)
        return bool(got and got[0])

    # 可重入：嵌套进入 → 退出内层后仍持有（换线程取不到）→ 退出外层才真放
    with lock:
        with lock:
            pass
        held_after_inner = not _try_other_thread()
    released_after_outer = _try_other_thread()
    check(held_after_inner and released_after_outer, "可重入语义与真 RLock 一致（嵌套/逐层释放）")

    # 异常透传 + 一定释放
    raised = False
    try:
        with lock:
            raise RuntimeError("boom")
    except RuntimeError:
        raised = True
    check(raised and _try_other_thread(), "临界区抛异常：原样上抛且锁已释放")

    # 非阻塞取锁失败也要被记账（否则"抢不到"这件事在指标里是隐形的）
    # ⚠️ 必须**换线程**试：RLock 是同线程可重入的，自己再 acquire(blocking=False) 会成功
    # （第一版就是这么写错的 —— 那不是"抢不到"）
    waits_before = sample_value("nl2sql_sqlite_lock_waits_total", {"store": store}) or 0.0
    with lock:
        others = _try_other_thread()
    check(others is False, "已持锁时**别的线程** acquire(blocking=False) 返回 False")
    check((sample_value("nl2sql_sqlite_lock_waits_total", {"store": store}) or 0.0) > waits_before,
          "抢不到也记一次进入（失败不该隐形）")

    # 透传：拿一个只带哨兵属性的假锁，断言未定义的名字真的转给了它
    class _DummyLock:
        sentinel = "穿透到了"

        def acquire(self, *a, **k):
            return True

        def release(self) -> None:
            pass

    check(MeteredLock(_DummyLock(), "verify-①").sentinel == "穿透到了",
          "__getattr__ 把未定义的属性透传给真锁")
    check(isinstance(lock, MeteredLock), "metered_rlock 返回的是计量锁（真锁被包在里面）")

    # 6 个共享连接存储的定义处都换成了计量锁（防回退）
    from agent.auth import grants
    from agent.eval import eval_queue
    from agent.feedback import store as feedback_store
    from agent.trace import trace_bind_store
    from agent.utils import prom_metrics
    from api import thread_search

    adopted = {
        "feedback": feedback_store._LOCK,
        "grants": grants._lock,
        "eval_queue": eval_queue._LOCK,
        "trace_bind": trace_bind_store._LOCK,
        "thread_search": thread_search._lock,
    }
    bad = [name for name, obj in adopted.items() if not isinstance(obj, MeteredLock)]
    check(not bad, "5 个模块级共享连接锁全部是计量锁", f"未换={bad or '无'}")
    check(isinstance(prom_metrics.LOCK_WAIT, type(prom_metrics.LOCK_WAIT)), "直方图对象可用")
    # event_store 是实例锁：开一个临时库，断言它的 self._lock 也是计量锁
    from agent.trace.event_store import EventStore

    tmp_store = EventStore(str(_WORK / "trace_events_verify.sqlite"))
    check(isinstance(tmp_store._lock, MeteredLock), "trace event_store 的实例锁是计量锁")
    try:
        tmp_store.close()
    except Exception:  # noqa: BLE001
        pass
    del LOCK_CONTENTION_TOTAL


# ── ② 采样 ─────────────────────────────────────────────────────────
async def verify_sampling(api_metrics) -> None:
    section("② 采样：队列深度 / 内存 / MCP / 子进程")
    real_queue = api_metrics._read_queue_snapshot
    real_mcp = api_metrics._read_mcp_registry
    real_rss = api_metrics._read_rss_bytes
    real_children = api_metrics._count_children

    async def _fake_queue():
        return {
            "queue": {
                "n_pending": 7,
                "n_running": 4,
                "pending_runs_wait_time_max_secs": 12.5,
                "pending_runs_wait_time_med_secs": 3.25,
            },
            "workers": {"max": 10, "active": 4, "available": 6},
        }

    api_metrics._read_queue_snapshot = _fake_queue
    api_metrics._read_mcp_registry = lambda: {"ok": 3, "failed": 1}
    api_metrics._read_rss_bytes = lambda: 512 * 1048576
    api_metrics._count_children = lambda *a, **k: 2
    try:
        readings = await api_metrics.sample_once()
    finally:
        api_metrics._read_queue_snapshot = real_queue
        api_metrics._read_mcp_registry = real_mcp
        api_metrics._read_rss_bytes = real_rss
        api_metrics._count_children = real_children

    check(readings.get("running") == 4.0 and readings.get("pending") == 7.0, "读数：n_running=4 / n_pending=7")
    check(sample_value("nl2sql_run_queue_running") == 4.0, "队列深度进 gauge（running）")
    check(sample_value("nl2sql_run_queue_pending") == 7.0, "队列深度进 gauge（pending）")
    check(sample_value("nl2sql_run_queue_wait_seconds", {"quantile": "max"}) == 12.5,
          "pending 等待时长进 gauge（quantile=max）")
    check(sample_value("nl2sql_worker_slots", {"state": "max"}) == 10.0
          and sample_value("nl2sql_worker_slots", {"state": "available"}) == 6.0, "worker 槽位进 gauge")
    check(sample_value("nl2sql_process_rss_bytes") == 512 * 1048576, "进程内存进 gauge")
    check(sample_value("nl2sql_mcp_servers", {"status": "ok"}) == 3.0
          and sample_value("nl2sql_mcp_servers", {"status": "failed"}) == 1.0, "MCP 注册表按状态计数")
    check(sample_value("nl2sql_process_children") == 2.0, "子进程数进 gauge")

    # 无数据 → 不写假 0（真跑一次：本进程不在 langgraph 里，队列那路本来就该拿不到）
    api_metrics._read_queue_snapshot = lambda: _no_data()
    api_metrics._read_rss_bytes = lambda: None
    api_metrics._read_mcp_registry = lambda: None
    api_metrics._count_children = lambda *a, **k: None
    try:
        readings2 = await api_metrics.sample_once()
    finally:
        api_metrics._read_queue_snapshot = real_queue
        api_metrics._read_rss_bytes = real_rss
        api_metrics._read_mcp_registry = real_mcp
        api_metrics._count_children = real_children
    check("running" not in readings2 and "rss_bytes" not in readings2, "拿不到就不产出（不写假 0）")
    check(readings2.get("llm_failures_delta") is not None, "事件计数型读数照常产出（与队列无关）")


async def _no_data():
    return None


# ── ③ 事件循环延迟 ─────────────────────────────────────────────────
async def _wait_for_next_sample(prev: float, timeout: float = 8.0) -> float:
    """等到采样轮次真的增加，返回新的轮次数（超时则原样返回）。

    为什么要等而不是 `sleep(固定值)`：采样任务的相位取决于它自己那一轮花了多久
    （首轮要 import MCP 模块），固定等待会在某些机器上刚好错开阻塞窗口 ——
    第一版就是这么假失败的。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        now = sample_value("nl2sql_metrics_samples_total", {"result": "ok"}) or 0.0
        if now > prev:
            return now
    return prev


async def verify_event_loop_lag(api_metrics) -> None:
    section("③ 事件循环延迟：真阻塞才算到")
    saved = (
        api_metrics._read_queue_snapshot,
        api_metrics._read_mcp_registry,
        api_metrics._read_rss_bytes,
        api_metrics._count_children,
    )
    # 采样只留 lag 这一路（其余给快而确定的假值）：避免首轮 import MCP 模块把相位搅乱
    api_metrics._read_queue_snapshot = lambda: _no_data()
    api_metrics._read_mcp_registry = lambda: {"ok": 1, "failed": 0}
    api_metrics._read_rss_bytes = lambda: 128 * 1048576
    api_metrics._count_children = lambda *a, **k: 0
    interval = api_metrics.interval_secs()
    check(interval <= 1.0, "自检用采样间隔（env 生效）", f"{interval}s")
    try:
        rounds = sample_value("nl2sql_metrics_samples_total", {"result": "ok"}) or 0.0
        api_metrics.start()
        rounds = await _wait_for_next_sample(rounds)
        check(rounds > 0, "采样任务真跑起来了（轮次计数在涨）", f"rounds={rounds}")

        # 现在采样任务已进入 sleep(interval)（计数自增与 sleep 之间没有 await 点，
        # 所以看到计数时它的定时器一定已经armed）→ 同步堵住循环 1.4×间隔，必然跨过截止点
        time.sleep(interval * 1.4)
        rounds = await _wait_for_next_sample(rounds)
        lag = sample_value("nl2sql_event_loop_lag_seconds")
        check(lag is not None and lag >= 0.3, "阻塞事件循环后量到延迟（≥0.3s）", f"lag={lag}")
        peak = sample_value("nl2sql_event_loop_lag_max_seconds")
        check(peak is not None and peak >= 0.3, "历史峰值 _max 记下了这次阻塞（面板能看「曾经堵到多少」）",
              f"max={peak}")

        # 负对照：不阻塞的一轮 → 同一指标回落到毫秒级
        rounds = await _wait_for_next_sample(rounds)
        calm = sample_value("nl2sql_event_loop_lag_seconds")
        check(calm is not None and calm < 0.3, "负对照：不阻塞时延迟回到毫秒级", f"lag={calm}")
        check((sample_value("nl2sql_event_loop_lag_max_seconds") or 0) >= 0.3,
              "峰值不被「平静的一轮」冲掉（_max 只增）")
    finally:
        await api_metrics.stop()
        (
            api_metrics._read_queue_snapshot,
            api_metrics._read_mcp_registry,
            api_metrics._read_rss_bytes,
            api_metrics._count_children,
        ) = saved
    check(api_metrics._task is None, "stop() 之后采样任务已清空（可重复起停）")


# ── ④ 告警 ─────────────────────────────────────────────────────────
def verify_alerts(api_metrics) -> None:
    section("④ 最小告警：连续 N 轮 / 只翻转时叫 / 恢复 / 可关 / 差值")
    cap = _Capture()
    logger = logging.getLogger("api.metrics")
    logger.addHandler(cap)
    saved_env = {k: os.environ.get(k) for k in (
        "NL2SQL_ALERT_LOOP_LAG_SECS", "NL2SQL_ALERT_RUNNING_RUNS", "NL2SQL_ALERT_RSS_MB",
        "NL2SQL_ALERT_LLM_FAILURES", "NL2SQL_ALERT_PENDING_RUNS",
        "NL2SQL_ALERT_LOCK_CONTENTIONS", "NL2SQL_ALERT_MCP_FAILED_SERVERS",
        "NL2SQL_ALERT_LLM_GATE_BYPASSES", "NL2SQL_ALERT_DISK_FREE_PCT",
    )}
    try:
        # 其余规则全关，只留延迟那条（阈值 0.5）
        for key in saved_env:
            os.environ[key] = "0"
        os.environ["NL2SQL_ALERT_LOOP_LAG_SECS"] = "0.5"
        _reset_alerts(api_metrics)
        cap.take()

        fired = api_metrics._evaluate_alerts({"event_loop_lag": 2.0})
        check(fired == [], "第 1 轮不叫（连续 3 轮的规则）")
        api_metrics._evaluate_alerts({"event_loop_lag": 2.0})
        fired = api_metrics._evaluate_alerts({"event_loop_lag": 2.0})
        check(fired == ["event_loop_lag"], "第 3 轮才翻转为响", f"fired={fired}")
        check(sample_value("nl2sql_alert_active", {"alert": "event_loop_lag"}) == 1.0, "面板可见：alert_active=1")
        check(sample_value("nl2sql_alerts_total", {"alert": "event_loop_lag"}) == 1.0, "触发计数 +1")
        lines = cap.take()
        check(sum(1 for ln in lines if ln.startswith("[alert] event_loop_lag")) == 1,
              "[alert] 只在翻转时记一条", f"行数={len(lines)}")

        for _ in range(4):
            api_metrics._evaluate_alerts({"event_loop_lag": 3.0})
        check(cap.take() == [], "持续超标不刷屏（一直响期间不再记日志）")
        check(sample_value("nl2sql_alerts_total", {"alert": "event_loop_lag"}) == 1.0, "持续超标也不重复计数")

        api_metrics._evaluate_alerts({"event_loop_lag": 0.01})
        lines = cap.take()
        check(any(ln.startswith("[alert-clear] event_loop_lag") for ln in lines), "[alert-clear] 恢复时记一条")
        check(sample_value("nl2sql_alert_active", {"alert": "event_loop_lag"}) == 0.0, "恢复后面板归 0")

        # 无数据的一轮：不清状态（缺数据不该把在响的告警"洗白"）
        _reset_alerts(api_metrics)
        cap.take()
        api_metrics._evaluate_alerts({"event_loop_lag": 2.0})
        api_metrics._evaluate_alerts({"event_loop_lag": 2.0})
        api_metrics._evaluate_alerts({"event_loop_lag": 2.0})
        api_metrics._evaluate_alerts({})  # 本轮没有 lag 数据
        check(sample_value("nl2sql_alert_active", {"alert": "event_loop_lag"}) == 1.0,
              "本轮无数据：保持原状态（不加也不清）")

        # 阈值 0 = 关闭该条，并把在响的清掉
        os.environ["NL2SQL_ALERT_LOOP_LAG_SECS"] = "0"
        api_metrics._evaluate_alerts({"event_loop_lag": 9.0})
        check(sample_value("nl2sql_alert_active", {"alert": "event_loop_lag"}) == 0.0, "阈值=0 关闭该条并清除")
        check(any(ln.startswith("[alert-clear] event_loop_lag") for ln in cap.take()), "关闭时也留一条 clear 痕迹")

        # 事件计数型：差值语义（首轮不把历史累计当新增）
        for key in saved_env:
            os.environ[key] = "0"
        os.environ["NL2SQL_ALERT_LLM_FAILURES"] = "2"
        _reset_alerts(api_metrics)
        cap.take()
        fired = api_metrics._evaluate_alerts({"llm_failures_delta": api_metrics._delta("llm_failures", 100.0)})
        check(fired == [] and api_metrics._delta("llm_failures", 100.0) == 0.0,
              "首轮不把历史累计当新增（开机即告警是最典型的误报）")
        fired = api_metrics._evaluate_alerts({"llm_failures_delta": api_metrics._delta("llm_failures", 103.0)})
        check(fired == ["llm_failures"], "窗口内新增 3 次失败 ≥2 → 立刻叫（samples=1）", f"fired={fired}")

        # P3-4 新增的那条：模型调用「等槽超预算被放行」——放行 = 闸长期不够用，
        # 也是事件计数型但**连续 3 轮**才叫（单窗口的抖动不值得半夜喊人）
        for key in saved_env:
            os.environ[key] = "0"
        os.environ["NL2SQL_ALERT_LLM_GATE_BYPASSES"] = "2"
        _reset_alerts(api_metrics)
        cap.take()
        fired = api_metrics._evaluate_alerts(
            {"llm_gate_bypass_delta": api_metrics._delta("llm_gate_bypass", 10.0)})
        check(fired == [], "闸放行：首轮不叫（不把历史累计当新增）")
        hits = []
        for total in (12.0, 24.0, 36.0):
            hits = api_metrics._evaluate_alerts(
                {"llm_gate_bypass_delta": api_metrics._delta("llm_gate_bypass", total)})
        check(hits == ["llm_gate_bypass"], "闸放行连续 3 轮 ≥2 → 叫", f"fired={hits}")
        check(sample_value("nl2sql_alert_active", {"alert": "llm_gate_bypass"}) == 1.0,
              "面板可见（运维据此判断该调 NL2SQL_LLM_MAX_CONCURRENCY）")

        # 阈值坏值 → 退回默认而不是 0（0 是"关闭"，静默关掉告警比报错更糟）
        os.environ["NL2SQL_ALERT_LOOP_LAG_SECS"] = "abc"
        check(api_metrics._env_float("NL2SQL_ALERT_LOOP_LAG_SECS", 0.5) == 0.5, "阈值不是数字时退回默认（不会静默关闭）")
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        logger.removeHandler(cap)
        _reset_alerts(api_metrics)


# ── ⑤ 端点 ─────────────────────────────────────────────────────────
async def verify_endpoint() -> None:
    section("⑤ /metrics 端点（真 Starlette + 真 ROUTES）")
    import httpx

    import api.custom_app as custom_app
    import api.metrics as api_metrics

    paths = [getattr(r, "path", None) for r in custom_app.ROUTES]
    check("/metrics" in paths, "/metrics 挂在 custom_app.ROUTES 里")
    check(api_metrics._resolve_meta_metrics() is not None,
          "上游 meta_metrics 解析成功（拿不到就只能降级输出）", f"err={api_metrics._meta_error}")

    transport = httpx.ASGITransport(app=custom_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/metrics", headers={"X-Real-IP": "172.18.0.9"})
        check(r.status_code == 200, "GET /metrics → 200", f"{r.status_code}")
        check(r.headers.get("cache-control") == "no-store", "禁缓存（否则面板显示几分钟前的队列深度）")
        body = r.text
        families = [
            "nl2sql_process_rss_bytes",
            "nl2sql_event_loop_lag_seconds",
            "nl2sql_event_loop_lag_max_seconds",
            "nl2sql_run_queue_running",
            "nl2sql_run_queue_pending",
            "nl2sql_run_queue_wait_seconds",
            "nl2sql_worker_slots",
            "nl2sql_llm_calls_total",
            "nl2sql_llm_latency_seconds",
            "nl2sql_sqlite_lock_wait_seconds",
            "nl2sql_sqlite_lock_contention_total",
            "nl2sql_mcp_servers",
            "nl2sql_process_children",
            "nl2sql_alerts_total",
            "nl2sql_alert_active",
        ]
        missing = [f for f in families if f"# HELP {f} " not in body]
        check(not missing, f"prometheus 文本含全部 {len(families)} 个 nl2sql_* 家族", f"缺={missing or '无'}")
        check("python_gc_objects_collected_total" in body, "上游（langgraph/OTel）的家族也在同一份输出里")

        head = await client.head("/metrics", headers={"X-Real-IP": "172.18.0.9"})
        check(head.status_code == 200 and head.content == b"", "HEAD /metrics → 200 且无 body")

        j = await client.get("/metrics?format=json", headers={"X-Real-IP": "172.18.0.9"})
        check(j.status_code == 200 and j.headers.get("content-type", "").startswith("application/json"),
              "?format=json → JSON 快照", f"{j.status_code}")
        payload = json.loads(j.text)
        check("queue" in payload and "workers" in payload, "JSON 里有 queue / workers（面板的原始数据）",
              f"keys={sorted(payload)[:6]}")
        if payload.get("queue"):
            check("n_running" in payload["queue"] and "n_pending" in payload["queue"],
                  "queue 里有 n_running / n_pending", f"{payload['queue']}")

        bad = await client.get("/metrics?format=bogus", headers={"X-Real-IP": "172.18.0.9"})
        check(bad.status_code == 200 and bad.headers.get("content-type", "").startswith("text/plain"),
              "非法 format 回退 prometheus（不是 500）")


# ── ⑥ 降级 ─────────────────────────────────────────────────────────
async def verify_degraded() -> None:
    section("⑥ 降级：上游变了也不能变空面板")
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    import api.metrics as api_metrics

    saved = (api_metrics._meta_metrics, api_metrics._meta_error, api_metrics._meta_resolved)
    api_metrics._meta_metrics, api_metrics._meta_error, api_metrics._meta_resolved = (
        None, "ModuleNotFoundError: 模拟上游变更", True,
    )
    try:
        app = Starlette(routes=[Route("/metrics", api_metrics.metrics_endpoint, methods=["GET", "HEAD"])])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/metrics")
            check(r.status_code == 200, "降级时仍 200（不是 500）")
            check("nl2sql_event_loop_lag_seconds" in r.text, "降级时自采指标照常输出")
            check(r.text.startswith("# nl2sql: "), "降级在输出里显式标注（不是静默缺指标）")
            j = await client.get("/metrics?format=json")
            check(json.loads(j.text).get("degraded") and "queue" in json.loads(j.text),
                  "降级时 JSON 明确说清缺了什么 + 带告警快照")
    finally:
        api_metrics._meta_metrics, api_metrics._meta_error, api_metrics._meta_resolved = saved


# ── ⑦ 子进程计数 ───────────────────────────────────────────────────
def verify_children(api_metrics) -> None:
    section("⑦ 子进程计数（/proc 解析）")
    proc = _WORK / "fake_proc"
    (proc / "11").mkdir(parents=True, exist_ok=True)
    (proc / "12").mkdir(parents=True, exist_ok=True)
    (proc / "13").mkdir(parents=True, exist_ok=True)
    (proc / "self").mkdir(parents=True, exist_ok=True)  # 非数字目录：跳过
    # 坑：comm 可以带空格与右括号 → 不能用 split()[3] 取 ppid
    (proc / "11" / "stat").write_bytes(b"11 (wren (semantic)) S 999 999 999 0 -1 4194304\n")
    (proc / "12" / "stat").write_bytes(b"12 (db mcp server) R 1 999 999 0 -1 0\n")
    (proc / "13" / "stat").write_bytes(b"13 (truncated")  # 半截文件：跳过而不是崩
    want = api_metrics._count_children(999, proc_root=str(proc))
    check(want == 1, "只数 PPid 匹配的进程（comm 带空格/右括号也解析对）", f"count={want}")
    check(api_metrics._ppid_from_stat(b"7 (x) S 4242 7 7\n") == 4242, "_ppid_from_stat 取的是第 4 个字段")
    check(api_metrics._ppid_from_stat(b"7 (x) S\n") is None, "字段不全 → None（不抛异常）")
    check(api_metrics._count_children(999, proc_root=str(_WORK / "no_such_proc")) is None,
          "非 Linux（无 /proc）→ None = 不产出该指标，而不是假 0")

    # 真机读数（Linux 上应有值；Windows 上应为 None —— 两种都算对，但类型必须对）
    real = api_metrics._count_children()
    check(real is None or isinstance(real, int), "真机子进程数：要么是 int，要么是 None（降级）", f"{real}")
    rss = api_metrics._read_rss_bytes()
    check(rss is None or rss > 1024 * 1024, "真机 RSS 读数合理（>1MB）或明确降级", f"{rss}")


# ── ⑧ 接线与暴露面 ─────────────────────────────────────────────────
async def verify_wiring(api_metrics) -> None:
    section("⑧ 接线与暴露面")
    import api.auth_middleware as auth_mw
    import api.custom_app as custom_app

    check("/metrics" not in " ".join(auth_mw._WHITELIST_PREFIXES), "/metrics 不在 auth 白名单里（要过鉴权）")

    # lifespan 真起停采样任务（不是"写了 start()"这种文本断言）
    await api_metrics.stop()
    api_metrics._task = None
    async with custom_app._lifespan(None):
        started = api_metrics._task is not None and not api_metrics._task.done()
    check(started, "lifespan 进入即起采样任务")
    check(api_metrics._task is None, "lifespan 退出即停采样任务")

    # 真中间件：外部请求（带 Cookie / 带 XFF）拿不到 /metrics；容器内部（无 Cookie 无 XFF）拿到
    from api.metrics import metrics_endpoint  # noqa: F401  确保 app 里的那条路由是同一实现
    from starlette.applications import Starlette
    from starlette.routing import Route

    inner = Starlette(routes=[Route("/metrics", metrics_endpoint, methods=["GET", "HEAD"])])
    app = auth_mw.AuthMiddleware(inner)
    statuses = {}
    for name, headers, client in (
        ("带 Cookie 的外部请求", [(b"cookie", b"nl2sql_token=forged")], ("203.0.113.7", 443)),
        ("带 XFF 的代理请求", [(b"x-forwarded-for", b"1.2.3.4")], ("172.18.0.9", 5000)),
        ("容器内部直连", [], ("172.18.0.9", 5000)),
    ):
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "path": "/metrics",
            "raw_path": b"/metrics", "query_string": b"", "headers": headers,
            "client": client, "server": ("langgraph-api", 2026),
            "scheme": "http", "root_path": "",
        }
        captured: dict = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]

        await app(scope, receive, send)
        statuses[name] = captured.get("status")
    check(statuses["带 Cookie 的外部请求"] in (401, 403), "带 Cookie 的外部请求 → 拒（401/403）",
          f"{statuses['带 Cookie 的外部请求']}")
    check(statuses["带 XFF 的代理请求"] in (401, 403), "带 XFF 的代理请求 → 拒（走 nginx 的也挡）",
          f"{statuses['带 XFF 的代理请求']}")
    check(statuses["容器内部直连"] == 200, "容器内部直连 → 200（运维能取，用户取不到）",
          f"{statuses['容器内部直连']}")

    # nginx：/metrics 不转后端（落到前端 → 外网路径到不了这里）
    nginx = (_REPO / "docker" / "nginx.conf").read_text(encoding="utf-8")
    check("location /metrics" not in nginx and "location = /metrics" not in nginx,
          "nginx 没有 /metrics 的 location（不转给后端）")
    check("proxy_pass http://backend/metrics" not in nginx, "nginx 没有指向后端 /metrics 的 proxy_pass")
    catch_all = [ln.strip() for ln in nginx.splitlines() if ln.strip().startswith("location /")]
    check("location / {" in catch_all, "兜底 location / 存在（/metrics 落到前端）", f"{catch_all[:3]}")


# ── ⑨ LLM 埋点 ─────────────────────────────────────────────────────
async def verify_llm_meters() -> None:
    section("⑨ LLM 耗时/失败率埋点（真 ModelTimeoutMiddleware）")
    from agent.middlewares.model_timeout import ModelTimeoutMiddleware
    from agent.utils.prom_metrics import llm_failure_count

    async def ok_handler(_request):
        await asyncio.sleep(0.12)
        return "ok-response"

    async def timeout_handler(_request):
        await asyncio.sleep(0.05)
        raise TimeoutError("Request timed out.")

    async def error_handler(_request):
        raise ValueError("500 bad gateway")

    mw = ModelTimeoutMiddleware()
    ok_before = sample_value("nl2sql_llm_calls_total", {"outcome": "ok"}) or 0.0
    fail_before = llm_failure_count()

    result = await mw.awrap_model_call(None, ok_handler)
    check(result == "ok-response", "正常调用原样返回（埋点不改语义）")
    check((sample_value("nl2sql_llm_calls_total", {"outcome": "ok"}) or 0.0) == ok_before + 1,
          "ok 计数 +1（主/子 agent 的模型调用都走这条边界）")
    latency_seconds = sample_value("nl2sql_llm_latency_seconds_sum", {"outcome": "ok"}) or 0.0
    check(latency_seconds >= 0.12, "耗时量到 await 那段（只量同步段会量成 0）", f"{latency_seconds:.3f}s")

    friendly = await mw.awrap_model_call(None, timeout_handler)
    check(getattr(friendly, "result", None) and "超时" in friendly.result[0].content,
          "超时 → 友好 AIMessage（原有语义不变）")
    check((sample_value("nl2sql_llm_calls_total", {"outcome": "timeout"}) or 0.0) >= 1, "timeout 计数 +1")
    from agent.utils.failure_signal import KIND_MODEL_TIMEOUT, failed_mark
    check((failed_mark(friendly.result[0]) or {}).get("kind") == KIND_MODEL_TIMEOUT,
          "同一条消息带失败戳（2026-09-28：终局失败必须机器可读，见 verify_failure_signal.py）")
    check(llm_failure_count() == fail_before + 1, "失败账 +1（告警规则读的就是它）")

    raised = False
    try:
        await mw.awrap_model_call(None, error_handler)
    except ValueError:
        raised = True
    check(raised and (sample_value("nl2sql_llm_calls_total", {"outcome": "error"}) or 0.0) >= 1,
          "非超时异常：原样上抛但记为 error（不是静默吞掉）")

    # 同步路径同款（父子 agent 都有同步分支）
    sync_ok = mw.wrap_model_call(None, lambda _r: "sync-ok")
    check(sync_ok == "sync-ok", "同步 wrap_model_call 正常返回")
    before = sample_value("nl2sql_llm_calls_total", {"outcome": "timeout"}) or 0.0

    def _boom(_r):
        raise TimeoutError("Request timed out.")

    sync_friendly = mw.wrap_model_call(None, _boom)
    check((sample_value("nl2sql_llm_calls_total", {"outcome": "timeout"}) or 0.0) == before + 1
          and "超时" in sync_friendly.result[0].content, "同步路径的超时也计进同一族指标")


async def main() -> int:
    import api.metrics as api_metrics

    verify_metered_lock()
    await verify_sampling(api_metrics)
    await verify_event_loop_lag(api_metrics)
    verify_alerts(api_metrics)
    await verify_endpoint()
    await verify_degraded()
    verify_children(api_metrics)
    await verify_wiring(api_metrics)
    await verify_llm_meters()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{'=' * 60}\n{passed}/{len(results)} 通过")
    if passed != len(results):
        print("失败项：")
        for ok, label in results:
            if not ok:
                print(f"  - {label}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
