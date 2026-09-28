# -*- coding: utf-8 -*-
"""P2-3：agent 侧自采指标（两处埋点，共用一个全局 REGISTRY）。

**为什么单独一个模块**：`/metrics` 的暴露面在 `api/metrics.py`（HTTP 层），
但下面两件事只有 agent 内部知道，HTTP 层看不见，也**不能**让 agent 反向
import `api.*`（会把组合根拖进 agent 的 import 图）：

1. **存储锁等待**（`metered_rlock`）——本仓所有 SQLite 存储都是「一条共享连接 +
   一把锁，**读也要进锁**」（见 `sqlite-shared-conn-lock-all-access`）。并发一
   上来，锁是唯一的排队点：请求变慢、CPU 不高、日志无异常，最难归因。把"等锁"
   的秒数采出来，就能一眼区分"在锁上排队"还是"模型慢"。
2. **LLM 调用结果与耗时**（`note_llm_call`）——埋在 `ModelTimeoutMiddleware` 的
   模型调用边界（唯一收口点），按 ok/timeout/error 三态记次数与耗时分布。
   失败率是"该不该扩容/该不该换模型"的直接输入。

两者都只是**写指标**：不抛异常、不阻塞、不改业务语义（指标库本身已在依赖里：
`prometheus_client` 随 langgraph 的 OTel exporter 一起进环境）。

⚠️ 指标名统一 `nl2sql_` 前缀，避免与 langgraph 的 `python_*` / 上游家族撞名。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

_logger = logging.getLogger(__name__)


# ── ① 存储锁等待 ────────────────────────────────────────────────────
# "等锁超过 CONTENTION_SECS" 的判定阈值：0.1s。正常（无竞争）一次 acquire 是
# 微秒级，0.1s 只可能是真的在排队（一次问数里同一把锁会被反复进出几百次）。
CONTENTION_SECS = 0.1

LOCK_WAIT = Histogram(
    "nl2sql_sqlite_lock_wait_seconds",
    "拿到共享连接锁之前的等待时长（等锁=排队；不含持锁执行的时间）",
    labelnames=("store",),
    # 桶从 0.5ms 起：健康时应几乎全落在最低两桶，往 0.1+ 长就是在排队。
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0),
)
LOCK_WAITS_TOTAL = Counter(
    "nl2sql_sqlite_lock_waits_total",
    "进入锁的次数（无论等了多久；含同一线程的可重入进入）",
    labelnames=("store",),
)
LOCK_CONTENTION_TOTAL = Counter(
    "nl2sql_sqlite_lock_contention_total",
    f"等锁超过 {CONTENTION_SECS}s 的次数（= 真竞争，不是微秒级抖动）",
    labelnames=("store",),
)
LOCK_WAITING = Gauge(
    "nl2sql_sqlite_lock_waiting_threads",
    "此刻阻塞在 acquire 上的线程数（该锁的排队长度）",
    labelnames=("store",),
)


class MeteredLock:
    """`threading.RLock` 的透明代理：唯一职责是**量出"等了多久才拿到锁"**。

    **为什么用代理而不是逐个改 `with` 点**：这些存储里 `with _LOCK:` 有上百处，
    逐个包一层既吵又容易漏；改锁的**定义**那一行（`threading.RLock()` →
    `metered_rlock("feedback")`）则一处生效、调用点零改动。

    ⚠️ 语义细节（改这里前先读）：
    - `__enter__` 量的是 **acquire 的耗时**（= 排队），**不是**临界区时长 ——
      后者会把"一条正常但慢的查询"误报成锁竞争。
    - 同一线程可重入进入时等待≈0，仍会记一次"进入"（`waits_total` 略高于
      "真排队次数"是预期的，判断竞争要看 `contention_total` 与直方图）。
    - `__exit__` 只 release、**不**计时；异常按原样透传（返回 False）。
    - 未显式实现的方法（`locked()` 等）由 `__getattr__` 透传给真锁。
    """

    __slots__ = ("_lock", "_store")

    def __init__(self, lock: Any, store: str) -> None:
        self._lock = lock
        self._store = store

    # ── 计时 ──
    def _observe(self, waited: float) -> None:
        try:
            LOCK_WAIT.labels(self._store).observe(waited)
            LOCK_WAITS_TOTAL.labels(self._store).inc()
            if waited >= CONTENTION_SECS:
                LOCK_CONTENTION_TOTAL.labels(self._store).inc()
        except Exception as e:  # noqa: BLE001  埋点绝不影响业务
            _logger.debug("[metrics] 记锁等待失败（忽略）: %s", e)

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """取锁；计量失败只降级、**绝不**改变取锁语义（真锁恰好被 acquire 一次）。"""
        waiting = None
        try:
            waiting = LOCK_WAITING.labels(self._store)
        except Exception:  # noqa: BLE001
            pass
        t0 = time.perf_counter()
        if waiting is not None:
            try:
                waiting.inc()
            except Exception:  # noqa: BLE001
                waiting = None
        try:
            if timeout is None:
                ok = self._lock.acquire(blocking)
            else:
                ok = self._lock.acquire(blocking, timeout)
        finally:
            if waiting is not None:
                try:
                    waiting.dec()
                except Exception:  # noqa: BLE001
                    pass
        self._observe(time.perf_counter() - t0)
        return ok

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "MeteredLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._lock.release()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._lock, name)

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"<MeteredLock store={self._store!r} lock={self._lock!r}>"


def metered_rlock(store: str) -> MeteredLock:
    """建一把带计量的可重入锁（锁的**定义**处用它替换 `threading.RLock()`）。

    `store` 是指标标签（如 `"feedback"` / `"grants"`）——**基数必须有限**，
    不要传库名/会话 id 这类高基数值。
    """
    return MeteredLock(threading.RLock(), store)


# ── ② LLM 调用结果与耗时 ───────────────────────────────────────────
LLM_CALLS_TOTAL = Counter(
    "nl2sql_llm_calls_total",
    "模型调用次数（ok=正常返回；timeout=超时类（已转友好提示）；error=其他异常）",
    labelnames=("outcome",),
)
LLM_LATENCY = Histogram(
    "nl2sql_llm_latency_seconds",
    "模型调用耗时（含失败那次等到的时长）",
    labelnames=("outcome",),
    # 覆盖"秒级首字 + 长上下文"到"超时重试 4×60s"的量级
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240, 480),
)

# ── ③ 模型调用并发闸与退避重试（P3-4）─────────────────────────────
# P2-7 压测里 6 并发问数时**唯一的真实失败**是出站模型调用的连接类错误
# （`openai.APIConnectionError`，SDK 自带的 3 次重试全部失败）。要判断"是不是
# 我们自己把出站并发打太满"，必须能看见**排队**：只看失败率看不出是"上游挂了"
# 还是"我们在门口挤"。
LLM_GATE_TOTAL = Counter(
    "nl2sql_llm_gate_total",
    "模型调用并发闸的取槽结果（free=直接拿到；waited=排队后拿到；"
    "bypassed=等超预算后放行；off=闸关闭；sync_on_loop=同步路径在事件循环上，不排队直接放行）",
    labelnames=("result",),
)
LLM_GATE_WAIT = Histogram(
    "nl2sql_llm_gate_wait_seconds",
    "取槽等待时长（只统计 waited；直接拿到的不进这个分布）",
    buckets=(0.001, 0.01, 0.05, 0.2, 0.5, 1, 5, 15, 60, 120),
)

# 累计放行数（等超预算而放行的次数）。与 `_LLM_FAILURES` 同理由：告警按差值判。
_GATE_BYPASSES = 0
_BYPASSES_LOCK = threading.Lock()

LLM_RETRIES_TOTAL = Counter(
    "nl2sql_llm_retries_total",
    "模型调用的退避重试（recovered=重试后成功；exhausted=重试用尽仍失败）",
    labelnames=("outcome",),
)

# 累计失败数（timeout+error）。用普通整数而不是读 counter 的私有字段：
# `api/metrics.py` 的告警规则按**两次采样的差值**判断"这一窗内新增了几次失败"，
# 需要一个单调递增且只加不读的账。
_LLM_FAILURES = 0
_FAILURES_LOCK = threading.Lock()


def note_llm_call(outcome: str, seconds: float) -> None:
    """记一次模型调用的结果与耗时（`outcome` ∈ ok / timeout / error）。

    必须**不抛异常**：它是每个模型调用的必经之路，埋点坏了不能把问答弄挂。
    """
    global _LLM_FAILURES
    try:
        LLM_CALLS_TOTAL.labels(outcome).inc()
        LLM_LATENCY.labels(outcome).observe(max(0.0, seconds))
        if outcome != "ok":
            with _FAILURES_LOCK:
                _LLM_FAILURES += 1
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 记 LLM 调用失败（忽略）: %s", e)


def llm_failure_count() -> int:
    """累计失败数（单调递增；调用方自己算差值）。"""
    with _FAILURES_LOCK:
        return _LLM_FAILURES


def note_gate_wait(result: str, seconds: float = 0.0) -> None:
    """记一次并发闸的取槽结果（`result` ∈ free / waited / bypassed / off / sync_on_loop）。

    与 `note_llm_call` 同规矩：**绝不抛异常** —— 闸的埋点坏了不能把问答弄挂。
    """
    global _GATE_BYPASSES
    try:
        LLM_GATE_TOTAL.labels(result).inc()
        if result == "waited":
            LLM_GATE_WAIT.observe(max(0.0, seconds))
        if result == "bypassed":
            with _BYPASSES_LOCK:
                _GATE_BYPASSES += 1
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 记闸等待失败（忽略）: %s", e)


def gate_bypass_count() -> int:
    """累计「等超预算而放行」的次数（单调递增；调用方自己算差值）。"""
    with _BYPASSES_LOCK:
        return _GATE_BYPASSES


def note_llm_retry(outcome: str) -> None:
    """记一次退避重试的结局（`outcome` ∈ recovered / exhausted）。不抛异常。"""
    try:
        LLM_RETRIES_TOTAL.labels(outcome).inc()
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 记模型重试失败（忽略）: %s", e)


def llm_meters() -> dict[str, Any]:
    """给自检脚本用的句柄（避免它去 import prometheus_client 的私有字段）。"""
    return {
        "calls": LLM_CALLS_TOTAL,
        "latency": LLM_LATENCY,
        "lock_wait": LOCK_WAIT,
        "lock_waits_total": LOCK_WAITS_TOTAL,
        "lock_contention_total": LOCK_CONTENTION_TOTAL,
        "lock_waiting": LOCK_WAITING,
        "gate_total": LLM_GATE_TOTAL,
        "gate_wait": LLM_GATE_WAIT,
        "retries_total": LLM_RETRIES_TOTAL,
    }
