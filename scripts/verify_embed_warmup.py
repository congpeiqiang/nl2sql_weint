# -*- coding: utf-8 -*-
"""嵌入通道**后台预热**验收（离线：无 GPU、不触网，全部走 transport 桩）。

**为什么要这份脚本**：这个机制的每一个「省事写法」都是错的，而且错得**看不出来**：

1. **超时必须远大于冷加载**（默认 120s）。调回 4s 就等于把查询期那个自锁原样搬进后台
   —— 仍然是「加载跑到一半被掐断，Ollama 连加载一起放弃」。这是整个机制的解药，
   所以要有断言锁死「预热的 timeout > 查询期 timeout」。
2. **只试 1 次**（`attempts=1`）。长超时本身就是解药；再带退避重试只是把最坏耗时翻倍，
   而且第二发会把一个**已经在跑**的加载再顶一次。
3. **成功要顺手关掉查询期熔断**。否则预热明明好了，那条 60s 冷却还挂着，白白多降级一分钟。
4. **只在 `NL2SQL_RETRIEVAL=hybrid` 时跑**。`off`/`fts` 下嵌入压根不被调用，预热纯烧请求
   —— 而且会**掩盖**「检索没开」这件事。
5. **`embed_texts(attempts=)` 缺省路径必须与改动前逐字一致**（缺省仍是 `retry_count()+1`）。
   这是新开的 kwarg 唯一不能碰的东西。
6. **预热失败不许开熔断**：预热不是查询，它失败只说明通道真不可用，不该去污染查询期状态。

每一条都有**负对照**（把参数调回错的值，断言脚本会抓）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_embed_warmup.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import sys
import tempfile
import time
from unittest import mock

# ⚠️ 必须在 import 业务模块之前：数据落点由 AGENT_DATA_ROOT 推导
_REPO = pathlib.Path(__file__).resolve().parents[1]
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-embed-warmup-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ["NL2SQL_EMBED_DIM"] = "16"          # 假嵌入 16 维，够验形状闸
os.environ["NL2SQL_EMBED_COOLDOWN"] = "0"      # 缺省关熔断，各段自己开
os.environ.pop("NL2SQL_RETRIEVAL", None)

sys.path.insert(0, str(_REPO / "src"))

from agent.retrieval import embedder  # noqa: E402

results: list[tuple[bool, str, str]] = []
_scene = ["（未分组）"]


def section(title: str) -> None:
    _scene[0] = title
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def check(cond, label: str, extra: str = "") -> None:
    results.append((bool(cond), label, _scene[0]))
    tail = f"  | {extra}" if extra else ""
    print(f"  {'[OK]  ' if cond else '[FAIL]'} {label}{tail}")


@contextlib.contextmanager
def env(**kv):
    """临时设 env，退出时**还原**（不是 pop —— 套件级默认值不能被抹掉）。"""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def capture_logs(logger: logging.Logger):
    """把 logger 的 INFO+ 收进一个 list。**必须显式 setLevel** —— NOTSET 会继承 root 的
    WARNING，INFO 记录根本到不了 handler（这个坑先踩过一次）。"""
    records: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _H()
    level_before = logger.level
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(h)
        logger.setLevel(level_before)


class _Resp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _resp(model: str, n: int = 1, dim: int | None = None):
    d = embedder.dim() if dim is None else dim
    return {"model": model, "data": [{"index": i, "embedding": [0.1] * d} for i in range(n)]}


def _transport(plan: list):
    """按 plan 依次出结果（Exception ⇒ 抛；dict ⇒ 当成功响应）。记调用次数与收到的 timeout。"""
    calls = {"n": 0, "timeouts": []}

    def fake(req, timeout):
        calls["timeouts"].append(timeout)
        i = calls["n"]
        calls["n"] += 1
        step = plan[i] if i < len(plan) else plan[-1]
        if isinstance(step, Exception):
            raise step
        return _Resp(step)

    return fake, calls


def _to():
    return TimeoutError("timed out")


def _ok():
    return _resp(embedder.model_name())


def main() -> int:
    # ── ① 默认值与「预算不变量」─────────────────────────────────────────
    section("① 默认值 + 超时预算不变量")
    check(embedder.warmup_interval_s() == 300.0, "预热间隔默认 300s",
          f"={embedder.warmup_interval_s()}")
    check(embedder.warmup_timeout_s() == 120.0, "预热超时默认 120s",
          f"={embedder.warmup_timeout_s()}")
    check(
        embedder.warmup_timeout_s() > embedder.query_timeout_s(),
        "**预热超时 > 查询期超时**（否则冷加载照样跑不完，机制失效）",
        f"warm={embedder.warmup_timeout_s()}s > query={embedder.query_timeout_s()}s",
    )
    check(
        embedder.warmup_timeout_s() > 17.0,
        "**预热超时 > 冷加载实测上界 17s**（2026-10-13 实测 13.16~17.21s）",
        f"warm={embedder.warmup_timeout_s()}s",
    )

    # 负对照：把 NL2SQL_EMBED_WARMUP_TIMEOUT 调小 ⇒ 读取器如实变小（而不是被忽略/兜底成默认）
    with env(NL2SQL_EMBED_WARMUP_TIMEOUT="4"):
        check(embedder.warmup_timeout_s() == 4.0,
              "负对照：env 能把它调小（说明 120s 是**配出来的**，不是写死到读不回来）")
    with env(NL2SQL_EMBED_WARMUP_TIMEOUT="abc"):
        check(embedder.warmup_timeout_s() == 120.0, "非法值 ⇒ 回落默认 120s")
    with env(NL2SQL_EMBED_WARMUP_INTERVAL="0"):
        check(embedder.warmup_interval_s() == 0.0, "interval=0 可读（= 关掉预热线程）")

    # ── ② warm_up 成功路径 ────────────────────────────────────────────
    section("② warm_up 成功：长超时 / 只试 1 次 / 顺带关熔断")
    with env(NL2SQL_RETRIEVAL="hybrid", NL2SQL_EMBED_COOLDOWN="60"):
        # 先人为把熔断打开：让 embed_one 连超两次
        fake0, _ = _transport([_to(), _to()])
        with mock.patch.object(embedder, "_open", fake0):
            embedder.embed_one("工时")
        check(embedder.breaker_open() is True, "前置：熔断已打开（预热前）")

        fake, calls = _transport([_ok()])
        with mock.patch.object(embedder, "_open", fake):
            with capture_logs(logging.getLogger("agent.retrieval.embedder")) as logs:
                ok = embedder.warm_up(reason="单测")
        check(ok is True, "warm_up 成功返回 True")
        check(calls["n"] == 1, "预热只打 1 次请求（attempts=1，不退避重试）", f"n={calls['n']}")
        check(calls["timeouts"] == [embedder.warmup_timeout_s()],
              "预热用的是 warmup_timeout_s（不是查询期 4s）", f"{calls['timeouts']}")
        check(embedder.breaker_open() is False, "**成功后熔断被关掉**（不必再白降级 60s）")
        check(any("预热成功" in m for m in logs), "打了「预热成功」行（可观测）")
        embedder._reset_breaker_for_test()

    # 负对照：预热超时**必须**大于查询期 —— 把它设成 4s，断言它确实只拿到 4s
    # （证明这条链真的把 env 传下去了，而不是内部写死 120）
    with env(NL2SQL_RETRIEVAL="hybrid", NL2SQL_EMBED_WARMUP_TIMEOUT="7"):
        fake, calls = _transport([_ok()])
        with mock.patch.object(embedder, "_open", fake):
            embedder.warm_up()
        check(calls["timeouts"] == [7.0], "负对照：env 超时确实传到 transport", f"{calls['timeouts']}")

    # ── ③ warm_up 失败路径 ────────────────────────────────────────────
    section("③ warm_up 失败：返回 False / 不打成功行 / **不开熔断**")
    with env(NL2SQL_RETRIEVAL="hybrid", NL2SQL_EMBED_COOLDOWN="60"):
        embedder._reset_breaker_for_test()
        # plan=[超时, 成功] ⇒ 若它退避重试，第二发会成功⇒True。断言它**失败**。
        fake, calls = _transport([_to(), _ok()])
        with mock.patch.object(embedder, "_open", fake):
            with capture_logs(logging.getLogger("agent.retrieval.embedder")) as logs:
                ok = embedder.warm_up(reason="单测失败")
        check(ok is False, "负对照：**首发放超时就直接失败**，绝不退避重试第二发")
        check(calls["n"] == 1, "失败路径也只打 1 次", f"n={calls['n']}")
        check(any("预热失败" in m for m in logs), "打了「预热失败」行")
        check(not any("预热成功" in m for m in logs), "负对照：失败时**没有**成功行（不会假通过）")
        check(embedder.breaker_open() is False,
              "**预热失败不开熔断**（预热不是查询，不许污染查询期状态）")

    # ── ④ 开关闸 ──────────────────────────────────────────────────────
    section("④ 开关闸：只有 hybrid 才预热")
    for mode, want_calls in (("off", 0), ("fts", 0), ("hybrid", 1)):
        with env(NL2SQL_RETRIEVAL=mode, NL2SQL_EMBED_WARMUP_TIMEOUT="7"):
            fake, calls = _transport([_ok()])
            with mock.patch.object(embedder, "_open", fake):
                embedder.warm_up()
            check(calls["n"] == want_calls,
                  f"NL2SQL_RETRIEVAL={mode} ⇒ 请求数 {want_calls}",
                  f"实测 n={calls['n']}")
    with env(NL2SQL_RETRIEVAL=None):
        fake, calls = _transport([_ok()])
        with mock.patch.object(embedder, "_open", fake):
            embedder.warm_up()
        check(calls["n"] == 0, "未配置（默认 off）⇒ 零请求")

    # ── ⑤ 线程生命周期 ────────────────────────────────────────────────
    section("⑤ 预热线程：启动 / 幂等 / interval=0 不启 / 可停")
    first_delay_before = embedder.WARMUP_FIRST_DELAY_S
    try:
        # interval=0 ⇒ 不启动，且不打误导性的「已启动」行
        with env(NL2SQL_RETRIEVAL="hybrid", NL2SQL_EMBED_WARMUP_INTERVAL="0"):
            with capture_logs(logging.getLogger("agent.retrieval.embedder")) as logs:
                started = embedder.start_warmer()
            check(started is False, "interval=0 ⇒ start_warmer 返回 False")
            check(not any("已启动" in m for m in logs),
                  "负对照：没启动就**不许**打「已启动」行（否则日志骗人）")

        embedder.WARMUP_FIRST_DELAY_S = 0.05   # 别在测试里真等 8s
        with env(NL2SQL_RETRIEVAL="hybrid", NL2SQL_EMBED_WARMUP_INTERVAL="0.2"):
            fake, calls = _transport([_ok()])
            with mock.patch.object(embedder, "_open", fake):
                with capture_logs(logging.getLogger("agent.retrieval.embedder")) as logs:
                    check(embedder.start_warmer() is True, "start_warmer 首次返回 True")
                    check(embedder.start_warmer() is False, "幂等：第二次返回 False")
                    time.sleep(0.6)
                    check(calls["n"] >= 1, "线程确实自己打了预热请求", f"n={calls['n']}")
                    check(any("预热成功" in m for m in logs), "线程里的预热留了成功行")
                    embedder.stop_warmer(timeout=1.0)
                    t = embedder._WARMER_THREAD
                    check(t is not None and not t.is_alive(), "stop_warmer 后线程已退出")
                n_after = calls["n"]
                time.sleep(0.5)
                check(calls["n"] == n_after, "停机后不再发请求", f"{n_after} -> {calls['n']}")
    finally:
        embedder.WARMUP_FIRST_DELAY_S = first_delay_before
        embedder.stop_warmer(timeout=1.0)

    # ── ⑥ embed_texts(attempts=) 的语义 + 缺省路径零变化 ───────────────
    section("⑥ embed_texts(attempts=)：新 kwarg 只影响显式传入者")
    with env(NL2SQL_RETRIEVAL="hybrid", NL2SQL_EMBED_RETRIES="2"):
        fake, calls = _transport([_ok()])
        with mock.patch.object(embedder, "_open", fake):
            embedder.embed_texts(["工时"])
        check(calls["n"] == 1, "缺省：第一次就成功 ⇒ 只 1 次")
        # 失败才看得出总次数
        fake, calls = _transport([_to()])
        with mock.patch.object(embedder, "_open", fake):
            embedder.embed_texts(["工时"])
        check(calls["n"] == 3, "缺省仍 = retry_count()+1 = 3（**旧行为零变化**）", f"n={calls['n']}")
        fake, calls = _transport([_to()])
        with mock.patch.object(embedder, "_open", fake):
            embedder.embed_texts(["工时"], attempts=1)
        check(calls["n"] == 1, "显式 attempts=1 ⇒ 只 1 次", f"n={calls['n']}")
        fake, calls = _transport([_to()])
        with mock.patch.object(embedder, "_open", fake):
            embedder.embed_texts(["工时"], attempts=0)
        check(calls["n"] == 1, "attempts=0 被夹到 1（不许出现「零次请求却返回 None」）", f"n={calls['n']}")

    # ── ⑦ 负对照：把预热超时调成查询期那样 ⇒ 冷加载必然失败 ─────────────
    section("⑦ 负对照：预热超时=4s 就退化成查询期那个自锁")
    with env(NL2SQL_RETRIEVAL="hybrid", NL2SQL_EMBED_WARMUP_TIMEOUT="4"):
        # 桩：只有超时 > 冷加载(13s) 时才成功 —— 模拟真实 Ollama 的行为
        cold_load_s = 13.0

        def fake(req, timeout):
            if timeout < cold_load_s:
                raise TimeoutError("timed out")
            return _Resp(_ok())

        with mock.patch.object(embedder, "_open", fake):
            check(embedder.warm_up(reason="4s") is False,
                  "4s 超时 ⇒ 预热失败（**这就是为什么这个数不能调小**）")
    with env(NL2SQL_RETRIEVAL="hybrid"):
        with mock.patch.object(embedder, "_open", fake):
            check(embedder.warm_up(reason="120s") is True,
                  "默认 120s ⇒ 同样的桩能跑完（**解药就是超时**）")

    passed = sum(1 for ok, _label, _scene_ in results if ok)
    total = len(results)
    print(f"\n{'=' * 62}\n{passed}/{total} 通过")
    if passed != total:
        print("失败项（带所属段）：")
        for ok, label, scene in results:
            if not ok:
                print(f"  - [{scene}] {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        import shutil

        shutil.rmtree(_WORK, ignore_errors=True)
    raise SystemExit(code)
