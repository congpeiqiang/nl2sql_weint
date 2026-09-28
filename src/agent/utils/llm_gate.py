# -*- coding: utf-8 -*-
"""P2-11 + P3-4：模型调用的**异常归类 + 并发闸 + 退避重试**。

**为什么会有这个模块**（P2-7 压测报告的结论，别丢）：

§九 P2-11：6 并发问数时 12 次问数里 3 次终态失败、1 次恢复，失败的**全是**
`openai.APIConnectionError: Connection error.`。而 `APIConnectionError` 是**壳**——
真正的原因在 `__cause__` 上（`httpx.ConnectError` / `RemoteProtocolError` /
`ReadError`…）。原有代码只记了外层那一句，于是「上游断连 / 代理抖动 / 我们自己的
连接被对端限流」三种完全不同的病，在日志里长得一模一样。所以本模块第一件事是
**把异常链摊平**（`describe_exception_chain`），第二件事才是去治它。

§九 P3-4：SDK 自己的 `max_retries=3` 在生产上**已经全部用尽**（日志里同一 run 同一
条错连续 4 行）⇒「靠 SDK 重试」这条已经失效。剩下的可能只有两类：
① 上游瞬时抖动 —— 靠**带抖动的慢退避**再给一次机会；
② 我们把出站并发打太满（对端按并发限流/直接断连）—— 靠**并发闸**。
两者都无法在当前证据下证伪，所以都做，并且都**可观测**（下次 6 并发压测直接看
`nl2sql_llm_gate_total` 与失败率就能归因，见 §九 的「必须补日志」要求）。
③ 单用户独占额度 —— 靠**按用户配额**（一个用户刷满是另一个用户的故障）。

**三条硬约束**（都是血换的，别改）：

1. **闸不能自己变成故障**。取槽失败一律 **fail-open**（放行 + 计数 + warning）：
   `ModelTimeoutMiddleware` 是模型调用的唯一收口点，它上面任何一段抛异常或死等，
   都会把**全站**问答弄挂。排队超预算 = 放行，不是失败。
2. **同步路径不许在事件循环上等**（P1-14 的口径）。`wrap_model_call` 若在事件循环
   线程里被调到（理论上不该，生产走 async），`time.sleep` 会把全站串起来；此处
   检测到有运行中的 loop 就**不排队直接放行**（单独一个指标标签 `sync_on_loop`），
   宁可少一道闸，不可多一个全站阻塞点。
3. **0 = 关**，非数字 **退回默认**（不静默关掉保护）—— 与 `db/limits.py` 同口径。

**闸的作用域说明**：限制的是「中间件看到的这一整段模型调用」。若某个 handler 直接
返回流对象（不 await），则流式消费那段不在闸内 —— 已知边界，写在这里免得被误读成
「流式也限住了」。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Iterator

from agent.utils.prom_metrics import note_gate_wait

_logger = logging.getLogger(__name__)

# ── ① 异常归类（P2-11）────────────────────────────────────────────

# httpx / openai SDK 的标准超时消息；覆盖 ReadTimeout/ConnectTimeout/WriteTimeout/
# PoolTimeout/APITimeoutError 等（它们 __str__ 都是 "Request timed out."）。
_TIMEOUT_RE = re.compile(
    r"(request|read|connect|write|pool)?\s*(timed\s?out|time\s*out)",
    re.IGNORECASE,
)

# 连接类错误的名字特征。**故意包含 `disconnect`/`protocol`/`transport`**：
# "Server disconnected without sending a response" 是 httpx 抛
# `RemoteProtocolError` 时的文案，压测里那种「对端主动断连」正是它。
_CONN_HINTS = (
    "connectionerror",
    "connecterror",
    "connectionreset",
    "connectionaborted",
    "connectionrefused",
    "remoteerror",
    "remoteprotocol",
    "protocolerror",
    "transporterror",
    "readerror",
    "writeerror",
    "brokenpipe",
    "disconnect",
    "connection",
)
_CONN_TEXT_RE = re.compile(
    r"(connection\s+(error|reset|refused|aborted|closed)|server\s+disconnected"
    r"|peer\s+closed|network\s+is\s+unreachable|remotedisconnected)",
    re.IGNORECASE,
)


def _chain(exc: BaseException | None, max_depth: int = 6) -> list[BaseException]:
    """摊平 `__cause__`/`__context__`，按访问顺序、去重（异常链可能有环）。"""
    out: list[BaseException] = []
    seen: set[int] = set()
    cur = exc
    while cur is not None and len(out) < max_depth:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
        if cur is not None and id(cur) in seen:
            break  # 环：已经在这条链上出现过，不再往下走
    return out


def describe_exception_chain(exc: BaseException, max_depth: int = 6) -> str:
    """把异常链写成一行 `外层 ← 中层 ← 根因`（P2-11 要求的 `type(__cause__)` + `repr`）。

    例：`openai.APIConnectionError ← httpx.ConnectError ← ConnectionRefusedError`。
    只摊平到根因**类名**，再附**最内层**的 `repr` —— 最外层的信息（"Connection error."）
    没有任何诊断价值，真正的原因永远在最里面。
    """
    parts = _chain(exc, max_depth)
    if not parts:
        return ""
    names = [f"{type(e).__module__}.{type(e).__name__}" for e in parts]
    innermost = parts[-1]
    tail = repr(innermost)
    if len(names) > 1:
        return " ← ".join(names) + f"  | 根因 repr: {tail}"
    return f"{names[0]}  | repr: {tail}"


def is_timeout_error(exc: BaseException) -> bool:
    """判断异常是否为「模型/传输层超时」类错误。

    判定链（任一命中即判定）：
    1. httpx.TimeoutException（ReadTimeout 等）；
    2. 内置 TimeoutError（部分 SDK / asyncio.timeout 直接抛）；
    3. 类名含 Timeout（openai.APITimeoutError、requests.ConnectTimeout 等）；
    4. 异常链（__cause__/__context__）任一环命中上述任一条。

    ⚠️ 必须**先于** `is_connection_error` 判断：`openai.APITimeoutError` 是
    `APIConnectionError` 的**子类**，反过来判会把超时也当连接类去重试。
    """
    seen: set[int] = set()

    def _check(e: BaseException | None) -> bool:
        if e is None:
            return False
        if id(e) in seen:
            return False
        seen.add(id(e))
        try:
            import httpx
            if isinstance(e, httpx.TimeoutException):
                return True
        except Exception:  # noqa: BLE001  httpx 未安装不影响（openai 自带）
            pass
        if isinstance(e, TimeoutError):
            return True
        name = type(e).__name__
        if "Timeout" in name or "timedout" in name.lower():
            return True
        if _TIMEOUT_RE.search(str(e)):
            return True
        return _check(e.__cause__) or _check(e.__context__)

    return _check(exc)


def is_connection_error(exc: BaseException) -> bool:
    """判断异常是否为「连接类」错误（可退避重试的那一类）。

    **超时不算连接类**（先过 `is_timeout_error`）—— 超时是「对端还活着但慢」，
    由 `ModelTimeoutMiddleware` 转成友好提示结束本轮；把它拉进来重试只会让用户
    多等几个 60s。
    """
    if is_timeout_error(exc):
        return False
    for e in _chain(exc):
        if isinstance(e, (ConnectionError, ConnectionResetError, BrokenPipeError)):
            return True
        name = type(e).__name__.lower()
        if any(h in name for h in _CONN_HINTS):
            return True
        try:
            if _CONN_TEXT_RE.search(str(e)):
                return True
        except Exception:  # noqa: BLE001  异常 __str__ 自己炸了也不影响判定
            pass
    return False


# ── ② 配置（一律运行期读 env）────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        _logger.warning("[llm-gate] %s=%r 不是整数，退回默认 %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        _logger.warning("[llm-gate] %s=%r 不是数字，退回默认 %s", name, raw, default)
        return default


# 默认 6 是**推导值不是实测最优**：P2-7 在 6 并发用户时观察到连接类失败，而 run 槽
# 天花板是 10（见 concurrency-ceiling-10-run-slots）。先卡在「出错的那一档之下」，
# 靠 `nl2sql_llm_gate_total` 的排队量去调；调不动就说明瓶颈不在这里。
DEFAULT_MAX_CONCURRENCY = 6
# 单用户配额默认 4：两个账号时一个用户最多占 6 个里的 4 个。**只是公平性旋钮**，
# 不要求 ≤ 全局（大于全局时先被全局挡住，不会出错）。
DEFAULT_MAX_CONCURRENCY_PER_USER = 4
# 排队预算 30s：超了放行。为什么不是 120s：P2-7 里「恢复的那次」把 turn 从 8s 拉到
# 50.8s 就已经很难看了，再让用户静止等两分钟不如放行让上游去抖。
DEFAULT_GATE_WAIT_SECS = 30.0
# 重试默认 1 次：SDK 内部已经重试过 3 次（生产上全部用尽），我们这层是**更慢的第二道**，
# 目的是把并发退化成"错峰重发"，不是等一轮长故障过去。再多的次数会把单次模型调用的
# 墙钟时间推向工具超时 300s。
DEFAULT_RETRY_ATTEMPTS = 1
DEFAULT_RETRY_BASE_SECS = 0.5
DEFAULT_RETRY_MAX_SECS = 4.0


def max_concurrency() -> int:
    """全局在飞模型调用上限；`0` = 关闭闸。"""
    return max(0, _env_int("NL2SQL_LLM_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY))


def max_concurrency_per_user() -> int:
    """单用户在飞上限；`0` = 不按用户限。"""
    return max(0, _env_int("NL2SQL_LLM_MAX_CONCURRENCY_PER_USER",
                           DEFAULT_MAX_CONCURRENCY_PER_USER))


def gate_wait_secs() -> float:
    """排队预算（秒）；`<=0` 视为**不排队**（但闸仍然生效：拿不到就放行）。"""
    return _env_float("NL2SQL_LLM_GATE_WAIT_SECS", DEFAULT_GATE_WAIT_SECS)


def retry_attempts() -> int:
    """连接类错误的**额外**重试次数（不含首次）；`0` = 不重试。"""
    return max(0, _env_int("NL2SQL_LLM_RETRY_ATTEMPTS", DEFAULT_RETRY_ATTEMPTS))


def backoff_seconds(attempt: int) -> float:
    """第 `attempt` 次重试前的等待（0 基）：指数 + **全抖动**。

    全抖动（`random.uniform(0, base*2**k)`）而不是固定间隔：失败是并发一起发生的，
    固定间隔会让所有等待者**同刻同时**再打上去，等于没退避（thundering herd）。
    """
    base = _env_float("NL2SQL_LLM_RETRY_BASE_SECS", DEFAULT_RETRY_BASE_SECS)
    cap = _env_float("NL2SQL_LLM_RETRY_MAX_SECS", DEFAULT_RETRY_MAX_SECS)
    if base <= 0:
        return 0.0
    window = min(base * (2 ** max(0, attempt)), max(base, cap))
    return random.uniform(0.0, window)


# ── ③ 并发闸 ─────────────────────────────────────────────────────
# 用「整数计数 + 一把锁」而不是 `Semaphore`：容量必须能**随 env 变化重建**（改
# 并发数不该要求重启），`Semaphore` 建好就改不了。计数在闸关闭时也照常加减 ——
# 这样中途把闸打开，账是准的。
_gate_lock = threading.Lock()
_inflight_global = 0
_inflight_user: dict[str, int] = {}

# 排队轮询：0.02s 起、1.5 倍递增、上限 0.2s。选轮询不是偷懒 —— 通知式等待要给每个
# 等待者造 event，且释放方要摸到所有等待者；本闸的等待者数量天然被 run 槽（10）封顶，
# 20Hz 的判空开销可以忽略，而超时精度是 0.2s（对 30s 预算而言无所谓）。
_POLL_MIN = 0.02
_POLL_MAX = 0.2


def _try_acquire(user_key: str) -> bool:
    """原子的「查两个额度 + 占位」。拿不到返回 False（**不**抛异常）。"""
    global _inflight_global
    gl = max_concurrency()
    ul = max_concurrency_per_user()
    with _gate_lock:
        if gl and _inflight_global >= gl:
            return False
        if ul and user_key and _inflight_user.get(user_key, 0) >= ul:
            return False
        _inflight_global += 1
        if user_key:
            _inflight_user[user_key] = _inflight_user.get(user_key, 0) + 1
        return True


def _release(user_key: str) -> None:
    global _inflight_global
    with _gate_lock:
        if _inflight_global > 0:
            _inflight_global -= 1
        if user_key:
            cur = _inflight_user.get(user_key, 0)
            if cur <= 1:
                _inflight_user.pop(user_key, None)  # 别让 dict 随用户数只涨不跌
            else:
                _inflight_user[user_key] = cur - 1


def inflight() -> dict[str, object]:
    """给验证脚本/运维看的当前占用（不保证快照一致，只读用）。"""
    with _gate_lock:
        return {"global": _inflight_global, "users": dict(_inflight_user)}


def reset_for_tests() -> None:
    """把计数器清零（**只给验证脚本用**；生产路径没有任何地方该调它）。"""
    global _inflight_global
    with _gate_lock:
        _inflight_global = 0
        _inflight_user.clear()


def caller_key() -> str:
    """当前模型调用属于哪个用户（取不到 → `""` = 不启用按用户配额）。

    走 `auth.runtime.caller_identity()`（P1-16 的唯一身份读取实现）：它从
    `langgraph.config.get_config()["configurable"]` 取，那个值是
    `LangfuseMetadataMiddleware` 按登录身份覆盖写入的。**别读
    `request.runtime.config`** —— 实测恒空（见 model-request-runtime-config-empty）。
    """
    try:
        from agent.auth.runtime import caller_identity

        return caller_identity() or ""
    except Exception as e:  # noqa: BLE001  身份读不到就退化成「只受全局闸管」
        _logger.debug("[llm-gate] 读取调用者身份失败（忽略）: %s", e)
        return ""


def _off() -> bool:
    return max_concurrency() <= 0 and max_concurrency_per_user() <= 0


@asynccontextmanager
async def model_call_slot():
    """异步模型调用的并发闸（`async with`）。

    产出 `bool`：**是否真的占到了槽**。调用方不要拿它决定要不要调用（fail-open 时
    也是 False 但照样该调用），只用于日志/断言。

    实现上**取槽与持槽必须分开**：如果把 `yield` 放进 try 里再 catch Exception，
    调用方 body 抛的异常会被当成「闸坏了」吞掉，`contextlib` 接着报
    `generator didn't stop after throw()`（把原始异常换成一条没有诊断价值的
    RuntimeError）。所以这里只 catch 取槽阶段，持槽阶段只有 finally。
    """
    try:
        held, user_key = await _acquire_async()
    except Exception as e:  # noqa: BLE001  闸自己坏了绝不能让问答挂
        _logger.warning("[llm-gate] 取槽异常（放行）: %s", e, exc_info=True)
        held, user_key = False, ""
    try:
        yield held
    finally:
        if held:
            _release(user_key)


@contextmanager
def model_call_slot_sync() -> Iterator[bool]:
    """同步模型调用的并发闸。

    有运行中的事件循环时**不排队**：`time.sleep` 会阻塞整条循环（P1-14 明令禁止），
    宁可这一路少一道闸 —— 记 `sync_on_loop` 单独可见。生产走 async，这条是兜底。
    """
    try:
        held, user_key = _acquire_sync()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[llm-gate] 取槽异常（放行）: %s", e, exc_info=True)
        held, user_key = False, ""
    try:
        yield held
    finally:
        if held:
            _release(user_key)


async def _acquire_async() -> tuple[bool, str]:
    """返回 `(是否持槽, 用来释放的 user_key)`；不持槽时 user_key 恒为 ""。"""
    if _off():
        note_gate_wait("off")
        return False, ""
    user_key = caller_key()
    if _try_acquire(user_key):
        note_gate_wait("free")
        return True, user_key

    started = time.monotonic()
    budget = gate_wait_secs()
    delay = _POLL_MIN
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= budget:
            break
        await asyncio.sleep(min(delay, budget - elapsed))
        if _try_acquire(user_key):
            note_gate_wait("waited", time.monotonic() - started)
            return True, user_key
        delay = min(delay * 1.5, _POLL_MAX)

    waited = time.monotonic() - started
    note_gate_wait("bypassed", waited)
    _logger.warning(
        "[llm-gate] 等槽 %.1fs 超预算（上限 %d / 单用户 %d，在飞 %s），本次放行不排队",
        waited, max_concurrency(), max_concurrency_per_user(), inflight(),
    )
    return False, ""


def _acquire_sync() -> tuple[bool, str]:
    if _off():
        note_gate_wait("off")
        return False, ""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # 在事件循环线程里 —— 不排队（宁可少一道闸，也不阻塞整条循环）
        note_gate_wait("sync_on_loop")
        _logger.debug("[llm-gate] 同步模型调用发生在事件循环上，不排队直接放行")
        return False, ""
    user_key = caller_key()
    if _try_acquire(user_key):
        note_gate_wait("free")
        return True, user_key

    started = time.monotonic()
    budget = gate_wait_secs()
    delay = _POLL_MIN
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= budget:
            break
        time.sleep(min(delay, budget - elapsed))
        if _try_acquire(user_key):
            note_gate_wait("waited", time.monotonic() - started)
            return True, user_key
        delay = min(delay * 1.5, _POLL_MAX)

    note_gate_wait("bypassed", time.monotonic() - started)
    return False, ""
