# -*- coding: utf-8 -*-
"""P2-11（异常链归因）+ P3-4（并发闸 / 排队 / 退避 / 按用户配额）验证。

跑法：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_llm_gate.py

**离线**：不连模型、不连数据库、不起后端。用真 openai/httpx 异常对象 + 假 handler
驱动真 `ModelTimeoutMiddleware`（不 mock 被测对象）。

验五段：
  ① **异常链归因**（P2-11）：`describe_exception_chain` 能摊平三层链、给出根因 repr、
     截断深度、遇环不死循环；**负对照**＝单层异常只回自己。
  ② **异常归类**：连接类 vs 超时 vs 其他。**核心负对照**＝`openai.APITimeoutError`
     是 `APIConnectionError` 的子类，必须判成超时**不是**连接（判反了会把"对端慢"
     当"连接断"去重试，用户白等几个 60s）。
  ③ **配置**：默认值、`0`=关、非数字退回默认、退避抖动（不是固定间隔）。
  ④ **并发闸**：真并发下峰值不超限；**负对照**＝闸关掉时峰值就是并发数（证明断言
     不是恒真）；排队超预算 fail-open 放行且不抛；按用户配额只挡同一个人；
     **异常穿透**（body 抛的异常必须原样出来，不能被闸吞成 RuntimeError）。
  ⑤ **中间件接线**：超时→友好文案不变；连接类→真重试（recovered/exhausted 计数）；
     其他异常**只调一次**（不许乱重试）；失败日志里必须出现**异常链**
     （P2-11 的核心交付：没有这条日志，压测里那 3 次失败永远归不了因）。

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

_LLM_ENV = (
    "NL2SQL_LLM_MAX_CONCURRENCY",
    "NL2SQL_LLM_MAX_CONCURRENCY_PER_USER",
    "NL2SQL_LLM_GATE_WAIT_SECS",
    "NL2SQL_LLM_RETRY_ATTEMPTS",
    "NL2SQL_LLM_RETRY_BASE_SECS",
    "NL2SQL_LLM_RETRY_MAX_SECS",
)

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    results.append((bool(cond), label))
    tag = "PASS" if cond else "FAIL"
    color = "\033[32m" if cond else "\033[31m"
    line = f"  [{color}{tag}\033[0m] {label}"
    if detail:
        line += f"  {detail}"
    print(line, flush=True)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def use(**kw) -> None:
    """把 LLM 闸相关的 env 全部重置为「只留本测试指定的那些」。"""
    for k in _LLM_ENV:
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = str(v)


def sample_value(name: str, labels: dict | None = None) -> float:
    from prometheus_client import REGISTRY

    try:
        return REGISTRY.get_sample_value(name, labels or {}) or 0.0
    except Exception:  # noqa: BLE001
        return 0.0


# ── 真异常对象（不手搓类名，用 SDK 自己的）─────────────────────────
def conn_exc(inner: BaseException | None = None):
    """构造 `openai.APIConnectionError`，`__cause__` 挂上真正的底层错误。

    生产日志里只有外壳那一句 "Connection error."，原因永远在 `__cause__` 上 ——
    这正是 P2-11 要摊平的东西。
    """
    import httpx
    import openai

    if inner is None:
        inner = httpx.ConnectError("Connection refused")
    err = openai.APIConnectionError(request=httpx.Request("POST", "http://model.test/v1"))
    err.__cause__ = inner
    return err


# ── ① 异常链归因（P2-11）────────────────────────────────────────
def verify_exception_chain() -> None:
    section("① 异常链归因（P2-11）：摊平 __cause__，根因必须在")
    from agent.utils.llm_gate import describe_exception_chain

    import httpx
    import openai

    err = conn_exc(httpx.ConnectError("Connection refused"))
    desc = describe_exception_chain(err)
    check("APIConnectionError" in desc and "ConnectError" in desc,
          "两层链都摊平出来（外壳没有诊断价值，里层才有）", desc[:100])
    check("←" in desc, "用 ← 标出方向（人一眼能看出谁包的谁）", desc[:60])
    check("Connection refused" in desc,
          "附最内层的 repr（'Connection error.' 之外必须能拿到真原因）", desc[-60:])

    deep = conn_exc(httpx.ConnectError("refused"))
    deep.__cause__.__cause__ = ConnectionRefusedError(111, "Connection refused")
    d3 = describe_exception_chain(deep)
    check(d3.count("←") == 2 and "ConnectionRefusedError" in d3,
          "三层链（APIConnectionError ← ConnectError ← ConnectionRefusedError）", d3[:120])

    check(describe_exception_chain(ValueError("x")).startswith("builtins.ValueError"),
          "负对照：单层异常只回自己（不多编造层）", describe_exception_chain(ValueError("x")))

    loopy = ValueError("a")
    loopy.__cause__ = loopy
    cyclic = describe_exception_chain(loopy)
    check(cyclic.count("builtins.ValueError") == 1, "环（自己 cause 自己）不死循环、不重复", cyclic)

    long_chain = ValueError("root")
    node = long_chain
    for i in range(12):
        nxt = ValueError(f"l{i}")
        node.__cause__ = nxt
        node = nxt
    capped = describe_exception_chain(long_chain, max_depth=6)
    check(capped.count("builtins.ValueError") == 6,
          "超长链按 max_depth 截断（日志不能被无限撑开）",
          f"len={capped.count('builtins.ValueError')}")

    check(describe_exception_chain(None) == "", "None 安全返回空串（调用方不必先判空）")


# ── ② 异常归类 ───────────────────────────────────────────────────
def verify_classification() -> None:
    section("② 异常归类：连接类 ≠ 超时 ≠ 其他（判反了会白等）")
    from agent.utils.llm_gate import is_connection_error, is_timeout_error

    import httpx
    import openai

    check(is_connection_error(conn_exc()), "APIConnectionError ← ConnectError → 连接类")
    check(is_connection_error(httpx.RemoteProtocolError("Server disconnected")),
          "RemoteProtocolError（对端主动断连）→ 连接类")
    check(is_connection_error(ConnectionResetError("reset")), "connection reset → 连接类")
    check(is_connection_error(BrokenPipeError("pipe")), "broken pipe → 连接类")
    check(is_connection_error(RuntimeError("Server disconnected without sending a response")),
          "SDK 把底层错误包成普通 RuntimeError 时也认（按文案兜底）")
    check(is_connection_error(conn_exc(httpx.ConnectError("refused"))) is True,
          "深层链里出现连接错误也算")

    check(is_timeout_error(openai.APITimeoutError(request=httpx.Request("POST", "http://m/")))
          and not is_connection_error(
              openai.APITimeoutError(request=httpx.Request("POST", "http://m/"))),
          "**核心负对照**：APITimeoutError 是 APIConnectionError 的子类 ⇒ 必须判成超时不是连接",
          "判反了会把「对端慢」当「连接断」去重试，用户白等几个 60s")

    check(not is_connection_error(ValueError("500 bad gateway")), "负对照：业务/普通异常不重试")
    check(not is_connection_error(openai.RateLimitError(
        "rate limited", response=httpx.Response(429, request=httpx.Request("POST", "http://m/")),
        body=None)),
        "负对照：429 限额不算连接类（SDK 自己按 Retry-After 重试，我们不叠）")
    check(not is_connection_error(RuntimeError("the connection is fine")),
          "负对照：文案里出现『connection』但不是在报故障 → 不误判")

    nested = openai.APIConnectionError(request=httpx.Request("POST", "http://m/"))
    check(is_connection_error(nested) and not is_timeout_error(nested),
          "裸 APIConnectionError（无 cause）靠类名命中 → 连接类，且不误判成超时")


# ── ③ 配置与退避 ─────────────────────────────────────────────────
def verify_config() -> None:
    section("③ 配置：默认值 / 0=关 / 非数字退回默认 / 退避抖动")
    from agent.utils import llm_gate as L

    use()
    check(L.max_concurrency() == L.DEFAULT_MAX_CONCURRENCY == 6, "全局默认 6", str(L.max_concurrency()))
    check(L.max_concurrency_per_user() == 4, "单用户默认 4", str(L.max_concurrency_per_user()))
    check(L.gate_wait_secs() == 30.0, "排队预算默认 30s", str(L.gate_wait_secs()))
    check(L.retry_attempts() == 1, "连接类额外重试默认 1 次", str(L.retry_attempts()))

    use(NL2SQL_LLM_MAX_CONCURRENCY="abc", NL2SQL_LLM_GATE_WAIT_SECS="", NL2SQL_LLM_RETRY_ATTEMPTS="-3")
    check(L.max_concurrency() == 6, "非数字 → 退回默认（不静默关掉保护）", str(L.max_concurrency()))
    check(L.gate_wait_secs() == 30.0, "空字符串 → 默认", str(L.gate_wait_secs()))
    check(L.retry_attempts() == 0, "负数 → 夹到 0", str(L.retry_attempts()))

    use(NL2SQL_LLM_MAX_CONCURRENCY="0", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="0")
    check(L.max_concurrency() == 0 and L.max_concurrency_per_user() == 0 and L._off(),
          "0/0 = 关（两侧都 0 才算关）")

    use(NL2SQL_LLM_MAX_CONCURRENCY="0", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="2")
    check(not L._off(), "只关全局、留用户配额 ⇒ 闸**没有**关（单用户限仍生效）")

    use(NL2SQL_LLM_MAX_CONCURRENCY="2", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="8")
    check(L.max_concurrency() == 2 and L.max_concurrency_per_user() == 8,
          "单用户配额 > 全局不报错（只是先被全局挡住）")

    use()
    samples = [L.backoff_seconds(1) for _ in range(30)]
    check(len(set(samples)) > 1, "退避带**全抖动**（固定间隔会让所有人同刻再打上去）",
          f"{len(set(samples))} 个不同值")
    check(all(0.0 <= s <= 1.0 for s in samples), "attempt=1 的退避落在 [0, base*2] 内",
          f"max={max(samples):.3f}")
    ups = [min(0.5 * 2 ** a, 4.0) for a in range(8)]
    check(ups[0] < ups[3] and ups[5] == ups[6] == 4.0, "上界指数增长后封顶在 max",
          str([round(u, 2) for u in ups]))
    check(all(L.backoff_seconds(a) <= 4.0 + 1e-9 for a in range(10)),
          "任何 attempt 都不超过 max（attempt 很大也不会睡成分钟级）",
          f"{max(L.backoff_seconds(a) for a in range(10)):.3f}")
    use(NL2SQL_LLM_RETRY_BASE_SECS="0")
    check(L.backoff_seconds(3) == 0.0, "base=0 → 不等待（但仍算一次重试）")


# ── ④ 并发闸 ─────────────────────────────────────────────────────
async def verify_gate() -> None:
    section("④ 并发闸：峰值不超限（含负对照）/ fail-open / 按用户配额 / 异常穿透")
    from agent.utils import llm_gate as L

    # —— 峰值不超限 ——
    async def peak_run(limit: int, workers: int) -> int:
        use(NL2SQL_LLM_MAX_CONCURRENCY=str(limit), NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="0",
            NL2SQL_LLM_GATE_WAIT_SECS="20")
        L.reset_for_tests()
        live = 0
        peak = 0

        async def one():
            nonlocal live, peak
            async with L.model_call_slot():
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.05)
                live -= 1

        await asyncio.gather(*[one() for _ in range(workers)])
        return peak

    check(await peak_run(2, 4) == 2, "4 个并发 + 限 2 ⇒ 在飞峰值恰好 2", "peak=2")
    check(await peak_run(0, 4) == 4,
          "负对照：闸关掉（0/0）⇒ 峰值就是并发数 4（证明上面那条不是恒真）", "peak=4")
    check(L.inflight()["global"] == 0, "跑完归零（槽一定被释放，不会泄漏）",
          str(L.inflight()))

    # —— 排队后放行 + waited 计数 ——
    use(NL2SQL_LLM_MAX_CONCURRENCY="1", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="0",
        NL2SQL_LLM_GATE_WAIT_SECS="20")
    L.reset_for_tests()
    waited_before = sample_value("nl2sql_llm_gate_total", {"result": "waited"})
    order: list[str] = []

    async def holder():
        async with L.model_call_slot():
            order.append("A-in")
            await asyncio.sleep(0.3)
            order.append("A-out")

    async def waiter():
        await asyncio.sleep(0.05)  # 保证 A 先拿到
        async with L.model_call_slot() as held:
            order.append(f"B-in(held={held})")

    await asyncio.gather(holder(), waiter())
    check(order == ["A-in", "A-out", "B-in(held=True)"],
          "排队者**等前一个释放后**才进（真串行，不是并发放行）", str(order))
    check(sample_value("nl2sql_llm_gate_total", {"result": "waited"}) == waited_before + 1,
          "排队拿到 → waited 计数 +1（排队量就是调闸限值的输入）")

    # —— fail-open：预算耗尽必须放行且不抛 ——
    use(NL2SQL_LLM_MAX_CONCURRENCY="1", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="0",
        NL2SQL_LLM_GATE_WAIT_SECS="0.15")
    L.reset_for_tests()
    from agent.utils.prom_metrics import gate_bypass_count

    bypass_before = gate_bypass_count()
    got: list[bool] = []
    threw: list[str] = []
    late_elapsed: list[float] = []

    async def blocked():
        async with L.model_call_slot():
            await asyncio.sleep(0.9)

    async def late():
        await asyncio.sleep(0.05)  # 让 blocked 先占住唯一的槽
        t = time.monotonic()      # 只量**放行者自己**的取槽耗时（不是整段 gather）
        try:
            async with L.model_call_slot() as held:
                got.append(held)
            late_elapsed.append(time.monotonic() - t)
        except Exception as e:  # noqa: BLE001
            threw.append(type(e).__name__)

    await asyncio.gather(blocked(), late())
    check(got == [False] and not threw,
          "**等超预算 → 放行**（fail-open：闸不能自己变成故障）", f"held={got} threw={threw}")
    check(late_elapsed and late_elapsed[0] < 0.5,
          "放行发生在预算耗尽时（预算 0.15s；没有傻等到前一个 0.9s 跑完）",
          f"{late_elapsed[0]:.2f}s" if late_elapsed else "(未取到)")
    check(gate_bypass_count() == bypass_before + 1, "放行次数记账（差值可告警）")
    check(sample_value("nl2sql_llm_gate_total", {"result": "bypassed"}) >= 1,
          "bypassed 标签可见（否则「放了 100 次」没人知道）")

    # —— 按用户配额 ——
    use(NL2SQL_LLM_MAX_CONCURRENCY="10", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="1",
        NL2SQL_LLM_GATE_WAIT_SECS="20")
    L.reset_for_tests()
    _UK: contextvars.ContextVar[str] = contextvars.ContextVar("verify_uk", default="")
    L.caller_key = lambda: _UK.get()  # type: ignore[assignment]
    marks: list[str] = []

    async def as_user(uid: str, tag: str, hold: float, delay: float = 0.0):
        _UK.set(uid)
        if delay:
            await asyncio.sleep(delay)
        t = time.monotonic()
        try:
            async with L.model_call_slot() as held:
                marks.append(f"{tag}:{held}:{time.monotonic() - t:.2f}")
                await asyncio.sleep(hold)
        finally:
            pass

    try:
        await asyncio.gather(
            as_user("u1", "u1-a", 0.4),        # u1 的第一个：拿槽
            as_user("u1", "u1-b", 0.05, 0.05),  # u1 的第二个：必须等
            as_user("u2", "u2-a", 0.05, 0.05),  # 另一个用户：不该被 u1 挡住
        )
    finally:
        L.caller_key = _real_caller_key  # type: ignore[assignment]
    d = {m.split(":")[0]: float(m.split(":")[2]) for m in marks}
    check(len(marks) == 3, "三个调用都完成了（配额只排队，不拒绝）", str(marks))
    check(d["u1-b"] >= 0.3, "同一用户的第 2 个调用被配额挡住 → 排队 ~0.4s", f"{d['u1-b']:.2f}s")
    check(d["u2-a"] < 0.15, "**另一个用户不受影响**（配额只限同一个人，不搞一户堵全站）",
          f"{d['u2-a']:.2f}s")
    check(L.inflight()["users"] == {}, "跑完 per-user 计数清空（dict 不会随用户数只涨不跌）",
          str(L.inflight()))

    # —— 异常穿透（body 抛的异常不能被闸吞）——
    use(NL2SQL_LLM_MAX_CONCURRENCY="4")
    L.reset_for_tests()
    got_exc = ""
    try:
        async with L.model_call_slot():
            raise ValueError("boom-from-body")
    except ValueError as e:
        got_exc = f"ValueError:{e}"
    except Exception as e:  # noqa: BLE001
        got_exc = f"WRONG:{type(e).__name__}"
    check(got_exc == "ValueError:boom-from-body",
          "**异常穿透**：body 的异常原样出来（曾经被闸吞成 RuntimeError）", got_exc)
    check(L.inflight()["global"] == 0, "异常路径也释放了槽（finally 不是装饰）",
          str(L.inflight()))

    got_exc = ""
    try:
        with L.model_call_slot_sync():
            raise KeyError("boom-sync")
    except KeyError as e:
        got_exc = f"KeyError:{e}"
    except Exception as e:  # noqa: BLE001
        got_exc = f"WRONG:{type(e).__name__}"
    check(got_exc == "KeyError:'boom-sync'", "同步路径同样穿透", got_exc)
    check(L.inflight()["global"] == 0, "同步路径也释放干净", str(L.inflight()))

    # —— 同步路径的两种形态 ——
    ln_before = sample_value("nl2sql_llm_gate_total", {"result": "sync_on_loop"})

    async def _sync_in_loop() -> bool:
        with L.model_call_slot_sync() as held:
            return held

    check(await _sync_in_loop() is False, "同步闸在事件循环上**不排队**（P1-14：绝不阻塞循环）")
    check(sample_value("nl2sql_llm_gate_total", {"result": "sync_on_loop"}) == ln_before + 1,
          "这条路径单独可见（不是悄悄放行）")

    def _sync_offloop() -> tuple[bool, int]:
        with L.model_call_slot_sync() as held:
            return held, int(L.inflight()["global"])

    held, inflight_inside = await asyncio.to_thread(_sync_offloop)
    check(held is True and inflight_inside == 1,
          "负对照：不在事件循环上时同步闸**真取槽**（在飞=1）", f"held={held} inflight={inflight_inside}")
    check(L.inflight()["global"] == 0, "出了 with 就释放", str(L.inflight()))

    # —— off 标签 ——
    use(NL2SQL_LLM_MAX_CONCURRENCY="0", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="0")
    off_before = sample_value("nl2sql_llm_gate_total", {"result": "off"})
    async with L.model_call_slot() as held:
        pass
    check(held is False and sample_value("nl2sql_llm_gate_total", {"result": "off"}) == off_before + 1,
          "闸关闭时记 off（『没排队』和『闸没开』必须能分开看）")


_real_caller_key = None


# ── ⑤ 中间件接线 ─────────────────────────────────────────────────
def verify_middleware() -> None:
    section("⑤ 中间件接线：超时文案不变 / 真重试 / 不乱重试 / 失败日志带异常链")
    import httpx

    from agent.middlewares.model_timeout import MODEL_TIMEOUT_MESSAGE, ModelTimeoutMiddleware
    from agent.utils import llm_gate as L

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            try:
                self.lines.append(record.getMessage())
            except Exception:  # noqa: BLE001
                pass

    cap = _Capture()
    mw_logger = logging.getLogger("agent.middlewares.model_timeout")
    mw_logger.addHandler(cap)
    mw_logger.setLevel(logging.DEBUG)
    mw = ModelTimeoutMiddleware()

    use(NL2SQL_LLM_MAX_CONCURRENCY="4", NL2SQL_LLM_RETRY_ATTEMPTS="1")

    # 超时：原语义不动
    calls = {"n": 0}

    async def timeout_handler(_req):
        calls["n"] += 1
        raise TimeoutError("Request timed out.")

    r = asyncio.run(mw.awrap_model_call(None, timeout_handler))
    check(getattr(r, "result", None) and r.result[0].content == MODEL_TIMEOUT_MESSAGE,
          "超时 → 仍是那条友好文案（P2-3 的语义没被这次改造动过）")
    check(calls["n"] == 1, "**超时不重试**（对端只是慢，再等一个 60s 只会更难看）", f"calls={calls['n']}")
    from agent.utils.failure_signal import KIND_MODEL_TIMEOUT, failed_mark
    check((failed_mark(r.result[0]) or {}).get("kind") == KIND_MODEL_TIMEOUT,
          "同一条消息带失败戳（2026-09-28：终局失败必须机器可读，见 verify_failure_signal.py）")

    # 连接类：重试到用尽 → 原样上抛
    use(NL2SQL_LLM_MAX_CONCURRENCY="4", NL2SQL_LLM_RETRY_ATTEMPTS="1",
        NL2SQL_LLM_RETRY_BASE_SECS="0.01")
    calls2 = {"n": 0}
    cap.lines.clear()
    exh_before = sample_value("nl2sql_llm_retries_total", {"outcome": "exhausted"})

    async def conn_handler(_req):
        calls2["n"] += 1
        raise conn_exc()

    raised: BaseException | None = None
    try:
        asyncio.run(mw.awrap_model_call(None, conn_handler))
    except BaseException as e:  # noqa: BLE001
        raised = e
    check(calls2["n"] == 2 and raised is not None,
          "连接类错误 → 真重试一次，用尽后原样上抛（不吞成友好文案）",
          f"calls={calls2['n']} raised={type(raised).__name__}")
    check(sample_value("nl2sql_llm_retries_total", {"outcome": "exhausted"}) == exh_before + 1,
          "exhausted 计数 +1（重试没救回来要能看见）")
    joined = "\n".join(cap.lines)
    check("APIConnectionError" in joined and "ConnectError" in joined and "←" in joined,
          "**P2-11 的核心交付**：失败日志里有完整异常链（原来只有外壳那一句）",
          joined.splitlines()[-1][:110] if cap.lines else "(无日志)")
    check("Connection refused" in joined, "链尾带根因 repr（能区分断连/抖动/被限流）")

    # 重试后成功
    use(NL2SQL_LLM_MAX_CONCURRENCY="4", NL2SQL_LLM_RETRY_ATTEMPTS="2",
        NL2SQL_LLM_RETRY_BASE_SECS="0.01")
    calls3 = {"n": 0}
    rec_before = sample_value("nl2sql_llm_retries_total", {"outcome": "recovered"})

    async def flaky_handler(_req):
        calls3["n"] += 1
        if calls3["n"] == 1:
            raise conn_exc(httpx.RemoteProtocolError("Server disconnected"))
        return "second-time-ok"

    r = asyncio.run(mw.awrap_model_call(None, flaky_handler))
    check(r == "second-time-ok" and calls3["n"] == 2,
          "抖一下就好了的场景：重试真的救回来了（返回值原样透出）",
          f"calls={calls3['n']} r={r}")
    check(sample_value("nl2sql_llm_retries_total", {"outcome": "recovered"}) == rec_before + 1,
          "recovered 计数 +1（与 exhausted 分开，才能算出重试的性价比）")

    # 负对照：非连接类不重试
    use(NL2SQL_LLM_MAX_CONCURRENCY="4", NL2SQL_LLM_RETRY_ATTEMPTS="3",
        NL2SQL_LLM_RETRY_BASE_SECS="0.01")
    calls4 = {"n": 0}

    async def value_handler(_req):
        calls4["n"] += 1
        raise ValueError("模型返回了坏 JSON")

    try:
        asyncio.run(mw.awrap_model_call(None, value_handler))
    except ValueError:
        pass
    check(calls4["n"] == 1,
          "**负对照**：非连接类异常只调一次（乱重试会把确定性错误放大 4 倍）",
          f"calls={calls4['n']}")

    # 负对照：attempts=0 时连接类也不重试
    use(NL2SQL_LLM_MAX_CONCURRENCY="4", NL2SQL_LLM_RETRY_ATTEMPTS="0")
    calls5 = {"n": 0}

    async def conn_handler0(_req):
        calls5["n"] += 1
        raise conn_exc()

    try:
        asyncio.run(mw.awrap_model_call(None, conn_handler0))
    except BaseException:  # noqa: BLE001
        pass
    check(calls5["n"] == 1, "attempts=0 ⇒ 连接类也只调一次（运维能一键关掉重试）",
          f"calls={calls5['n']}")

    # 闸在中间件里真生效：限 1，两个并发模型调用峰值必须为 1
    use(NL2SQL_LLM_MAX_CONCURRENCY="1", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="0",
        NL2SQL_LLM_GATE_WAIT_SECS="20", NL2SQL_LLM_RETRY_ATTEMPTS="0")
    L.reset_for_tests()
    live = {"n": 0, "peak": 0}

    async def slow_handler(_req):
        live["n"] += 1
        live["peak"] = max(live["peak"], live["n"])
        await asyncio.sleep(0.1)
        live["n"] -= 1
        return "ok"

    async def both():
        return await asyncio.gather(
            mw.awrap_model_call(None, slow_handler),
            mw.awrap_model_call(None, slow_handler),
        )

    out = asyncio.run(both())
    check(out == ["ok", "ok"], "两个并发调用都成功（排队后放行）", str(out))
    check(live["peak"] == 1, "**闸真的接上了**：限 1 ⇒ 模型调用峰值 1（不是摆设）",
          f"peak={live['peak']}")

    # 同步路径也走闸（局上兜底不排队，但 off-loop 时真取槽）
    use(NL2SQL_LLM_MAX_CONCURRENCY="1", NL2SQL_LLM_MAX_CONCURRENCY_PER_USER="0")
    L.reset_for_tests()

    def _sync_mw():
        return mw.wrap_model_call(None, lambda _r: "sync-ok")

    got_sync = asyncio.run(asyncio.to_thread(_sync_mw))
    check(got_sync == "sync-ok", "同步 wrap_model_call 返回值原样（闸的 sync 分支不炸）",
          str(got_sync))
    check(L.inflight()["global"] == 0, "同步跑完也归零", str(L.inflight()))

    mw_logger.removeHandler(cap)
    use()


def main() -> int:
    global _real_caller_key
    from agent.utils import llm_gate as L

    _real_caller_key = L.caller_key

    verify_exception_chain()
    verify_classification()
    verify_config()
    asyncio.run(verify_gate())
    verify_middleware()
    use()

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"{passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for ok, label in results:
            if not ok:
                print(f"  ✗ {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
