# -*- coding: utf-8 -*-
"""P1-14 事件循环存活验证（离线：不连库、不起后端、不碰网络）。

**被测的坑**：10 个后台 job 共用**同一个**事件循环，而 `LANGGRAPH_ALLOW_BLOCKING=true`
把 langgraph 的阻塞检测护栏关掉了 —— 于是在 `async def` 里直接做同步 I/O 会**静默**
把全站串起来：一个用户点「语义库构建」（wren CLI 最长 600s），其他人的 SSE 与子任务
一起卡住，日志里什么都没有。修法是把这些同步调用搬进 `agent.utils.offload`
的两个池（见该模块文件头），本脚本负责证明「搬了」并且「搬对了」。

验五件事：

  ① 两个池的**契约**。`offload` 传播 contextvars、`offload_long` 不传播（所以后者
     只能用于不读请求上下文的调用）；**长池被占满时短调用不排队** —— 若两者共用
     一个池，短调用就得等 600s 的构建（用共享池做对照实验，证明分池不是装饰）。
  ② **真实 handler + 慢依赖**：把每个 handler 真正的慢步骤换成 `sleep`（记下它跑在
     哪个线程），调用期间并发跑一个 5ms 探针 —— 断言探针**最大滞后**远小于阻塞时长、
     慢活在非主线程、且慢依赖确实被执行了（不是被跳过所以"不卡"）。
     含一个「纯 `await asyncio.sleep`」基线用例，用来暴露本机的探针噪声底。
  ③ 负对照（**复刻修复前**）：把被测模块里的 `offload`/`offload_long` 换成**就地
     调用** → ② 的每一条断言必须变红。证明 ② 的绿不是恒真。
  ④ `verify_token`（每个受保护请求都走）的实测耗时 vs 一次线程切换的实测耗时 ——
     这是**故意不搬它**的判据：搬一次比它自己还贵，且它读的是进程内缓存（无阻塞 I/O）。
  ⑤ 静态回归：`offload_long` 不得吃到「函数体里读 `langgraph` 请求上下文」的函数
     （contextvars 不传播 → 搬过去就读不到 db_name/thread_id）。规则从代码里**推导**，
     不是手写名单，并带一条合成负对照防它退化成恒真。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_event_loop_liveness.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import asyncio
import contextvars
import os
import pathlib
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

# ⚠️ 必须在 import 业务模块之前：用户表 / auth_secret / 共享目录落点都由它推导
os.environ["AGENT_DATA_ROOT"] = tempfile.mkdtemp(prefix="nl2sql-verify-loop-")
os.environ.pop("NL2SQL_AUTH_DISABLED", None)   # ④ 要真的验签
os.environ.setdefault("NL2SQL_AUTH_SECRET", "verify-event-loop-secret")

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

BLOCK = 0.5          # 慢依赖的阻塞时长（模拟 wren 子进程 / sqlite fsync / 慢盘）
LAG_LIMIT = 0.15     # 探针最大滞后上限：远小于 BLOCK，又远大于本机噪声（基线实测见下）
PROBE_INTERVAL = 0.005

PROJECT = pathlib.Path(os.environ["AGENT_DATA_ROOT"]) / "verify-proj"
ADMIN_USER = {"user_id": "admin", "is_admin": True, "display_name": "管理员"}


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def swap(obj: Any, **attrs: Any) -> Callable[[], None]:
    """把 obj 的属性换成新值（**只换它本来就有的**），返回撤销函数。

    「只换本来有的」是必须的：`agent.auth.backend` 只用 `offload`（没有 `offload_long`），
    无条件 setattr 会凭空造出一个属性 —— 负对照里就撞过一次 AttributeError。
    """
    attrs = {k: v for k, v in attrs.items() if hasattr(obj, k)}
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)

    def _undo() -> None:
        for k, v in saved.items():
            setattr(obj, k, v)

    return _undo


# ── 探针：量「事件循环最长多久没能回来跑我」 ────────────────────────────

class LoopProbe:
    """5ms 周期的滞后探针：滞后 ≈ 期间有人在循环上同步干了多久。

    单次 `await asyncio.sleep(5ms)` 的实际耗时 = 5ms + 唤醒误差；把误差的最大值
    当作「循环被占住」的下界估计。噪声底由基线用例给出（本机约十几 ms）。
    """

    def __init__(self, interval: float = PROBE_INTERVAL) -> None:
        self.interval = interval
        self.max_lag = 0.0
        self.max_lag_at = 0.0        # 最大滞后发生在探针启动后第几毫秒（用来定位是谁堵的）
        self.slow_ticks: list[tuple[float, float]] = []   # (>4×周期 的 tick) = (起始时刻, 滞后)
        self.ticks = 0
        self._t_start = 0.0
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(self.interval)
            lag = time.perf_counter() - t0 - self.interval
            if lag > self.max_lag:
                self.max_lag = lag
                self.max_lag_at = t0 - self._t_start
            if lag > 4 * self.interval:      # 明显异常的一次，留痕（t_start, lag）
                if len(self.slow_ticks) < 12:
                    self.slow_ticks.append((t0 - self._t_start, lag))
            self.ticks += 1

    def start(self) -> None:
        self._stop = asyncio.Event()
        self._t_start = time.perf_counter()
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> float:
        assert self._stop is not None and self._task is not None
        self._stop.set()
        await self._task
        if self.ticks < 3:
            print(f"  · 警告：探针只跳了 {self.ticks} 次 —— 滞后数据不可信")
        return self.max_lag


# ── 假请求（真 Starlette Request，真 path_params，走真 handler 主体）────

def make_request(
    path_params: dict | None = None,
    method: str = "GET",
    json_body: dict | None = None,
) -> Any:
    from starlette.requests import Request

    body = b"" if json_body is None else __import__("json").dumps(json_body).encode("utf-8")
    headers = [(b"content-type", b"application/json")] if json_body is not None else []

    async def _receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "path_params": path_params or {},
            "client": ("127.0.0.1", 12345),
            "server": ("verify", 80),
            "state": {"user": dict(ADMIN_USER)},
        },
        receive=_receive,
    )


# ── 用例：把每个 handler 的慢依赖换成 sleep，并记下执行线程 ─────────────
#
# 每个用例返回 (coro, seen, undo)：coro 是**真实 handler** 的调用（不是复刻），
# 只有它内部那个真正慢的依赖被打桩；seen["thread"] 由打桩函数写入。

def case_baseline(block: float) -> tuple[Any, dict, Callable[[], None]]:
    """基线：没有任何同步阻塞 —— 探针此刻的滞后就是本机噪声底。"""
    async def _coro() -> None:
        await asyncio.sleep(block)

    return _coro(), {}, lambda: None


def case_validate_project(block: float) -> tuple[Any, dict, Callable[[], None]]:
    """`wren context validate` = 子进程（实测生产 build 151 模型 5.7s，最长超时 180s）。"""
    from api import wren_semantic as W

    seen: dict = {}

    def fake_run_wren(project_path, *args, timeout=180):
        seen["fn"] = "_run_wren"
        seen["thread"] = threading.current_thread().name
        seen["args"] = list(args)
        time.sleep(block)
        return True, "MDL 校验通过"

    undo = swap(W, _run_wren=fake_run_wren, _find_project=lambda name: PROJECT)
    return W.validate_project(make_request(path_params={"name": "verify-proj"})), seen, undo


def case_read_knowledge(block: float) -> tuple[Any, dict, Callable[[], None]]:
    """逐个读 knowledge/ 下的 .md 全文（成熟语义库几百个文件，绑挂载上几十~几百 ms）。"""
    from api import wren_semantic as W

    seen: dict = {}
    orig_read_text = pathlib.Path.read_text

    def slow_read_text(self, *a, **k):  # type: ignore[no-untyped-def]
        if not seen.get("slept"):        # 只慢一次：模拟"第一块盘很慢"，不放大总时长
            seen["slept"] = True
            seen["fn"] = "Path.read_text"
            seen["thread"] = threading.current_thread().name
            seen["file"] = self.name
            time.sleep(block)
        return orig_read_text(self, *a, **k)

    undo = swap(W, _find_project=lambda name: PROJECT)
    pathlib.Path.read_text = slow_read_text  # type: ignore[method-assign]

    def _undo() -> None:
        pathlib.Path.read_text = orig_read_text  # type: ignore[method-assign]
        undo()

    return W.read_knowledge(make_request(path_params={"name": "verify-proj"})), seen, _undo


def case_put_feedback(block: float) -> tuple[Any, dict, Callable[[], None]]:
    """反馈写入 = sqlite「读旧值 + 写新值」（每次 commit 都 fsync）。前端批注是连点操作。"""
    from api import message_feedback as M

    seen: dict = {}

    class _SlowStore:
        def get(self, tid, mid):
            seen["get_thread"] = threading.current_thread().name
            return None                      # None = 首次写入（走最长的分支）

        def upsert(self, tid, mid, rating, **kw):
            seen.setdefault("fn", "store.upsert")
            seen["thread"] = threading.current_thread().name
            time.sleep(block)
            return SimpleNamespace(
                sql="", question="", feedback_type="", context={}, rating=rating,
                note=kw.get("note", ""), to_mapping=lambda: {"rating": rating},
            )

        def set_feedback_type(self, *a, **k):
            seen["extra_write"] = True

        def enqueue_annotation(self, *a, **k):
            seen["enqueued"] = True

    undo = swap(
        M,
        store=_SlowStore(),
        require_thread=lambda request, thread_id: dict(ADMIN_USER),   # 归属校验非本项被测
        _schedule_snapshot_backfill=lambda *a, **k: None,             # 旁路线程：不打桩会真起线程
        _schedule_langfuse_score=lambda *a, **k: None,
    )
    req = make_request(
        method="PUT",
        path_params={"thread_id": "t-1", "message_id": "m-1"},
        json_body={"rating": "positive", "note": "口径对"},
    )
    return M.put_feedback(req), seen, undo


def case_stamp_thread_owner(block: float) -> tuple[Any, dict, Callable[[], None]]:
    """建会话时写 grants 账本（sqlite fsync）—— 每个新会话一次，走在 auth 钩子里。"""
    import agent.auth.backend as B

    seen: dict = {}

    def fake_claim(tid, identity):
        seen["fn"] = "claim_thread"
        seen["thread"] = threading.current_thread().name
        seen["args"] = (tid, identity)
        time.sleep(block)
        return 1

    undo = swap(B, claim_thread=fake_claim)
    ctx = SimpleNamespace(user=SimpleNamespace(identity="u-verify"), permissions=[])
    return B._stamp_thread_owner(ctx, {"thread_id": "t-stamp"}), seen, undo


CASES: list[tuple[str, Callable]] = [
    ("validate_project", case_validate_project),
    ("read_knowledge", case_read_knowledge),
    ("put_feedback", case_put_feedback),
    ("_stamp_thread_owner", case_stamp_thread_owner),
]

# 负对照要改的模块（= 每个用例 offload 调用的宿主）
CASE_MODULES: dict[str, str] = {
    "validate_project": "api.wren_semantic",
    "read_knowledge": "api.wren_semantic",
    "put_feedback": "api.message_feedback",
    "_stamp_thread_owner": "agent.auth.backend",
}


async def run_case(make: Callable, block: float) -> tuple[LoopProbe, float, dict]:
    """跑一个用例，返回 (探针, 用例耗时, 打桩处的观测)。"""
    probe = LoopProbe()
    probe.start()
    await asyncio.sleep(0.05)          # 先让探针量到基线再进被测调用
    coro, seen, undo = make(block)
    t0 = time.perf_counter()
    try:
        await coro
    finally:
        undo()
    elapsed = time.perf_counter() - t0
    await probe.stop()
    return probe, elapsed, seen


def assert_case(name: str, probe: LoopProbe, elapsed: float, seen: dict, label: str = "") -> None:
    """② 的断言集（负对照复用同一套，期望它们**变红**）。"""
    tag = f"{name}{label}"
    lag = probe.max_lag
    if probe.slow_ticks:
        print("    · 异常 tick（T+ms → 滞后 ms）："
              + ", ".join(f"{a * 1000:.0f}→{b * 1000:.0f}" for a, b in probe.slow_ticks))
    check(lag < LAG_LIMIT,
          f"{tag}：调用期间事件循环最长只被占 {lag * 1000:.0f}ms",
          f"阻塞 {BLOCK * 1000:.0f}ms，阈值 {LAG_LIMIT * 1000:.0f}ms，用例耗时 {elapsed * 1000:.0f}ms，"
          f"探针跳了 {probe.ticks} 次，最差一次在 T+{probe.max_lag_at * 1000:.0f}ms")
    thread = seen.get("thread", "")
    check(bool(thread) and thread != "MainThread",
          f"{tag}：慢依赖跑在非主线程（{thread or '未观测到'}）")
    check(elapsed >= BLOCK,
          f"{tag}：慢依赖确实被执行了（耗时 ≥ 阻塞时长，不是被跳过）",
          f"{elapsed * 1000:.0f}ms")


# ── ① 两个池的契约 ────────────────────────────────────────────────────

def t1_pools() -> None:
    section("① 池契约：contextvars / 独立线程 / 长池占满不吃短调用")
    from agent.utils import offload as O

    cv: contextvars.ContextVar[str] = contextvars.ContextVar("verify-probe", default="-")
    seen: dict = {}

    def _read_ctx(tag: str) -> None:
        seen[tag] = (threading.current_thread().name, cv.get())
        time.sleep(0.05)

    async def _probe() -> None:
        cv.set("loop-value")          # 模拟请求上下文（真身是 get_config().configurable）
        await O.offload(_read_ctx, "short")
        await O.offload_long(_read_ctx, "long")

    asyncio.run(_probe())
    short_name, short_val = seen["short"]
    long_name, long_val = seen["long"]
    check(short_val == "loop-value",
          "`offload` 传播 contextvars（worker 里读得到本请求的 db_name/thread_id）", short_val)
    check(long_val == "-",
          "`offload_long` **不**传播 contextvars（所以只用于不读上下文的调用）", long_val)
    check(long_name.startswith("offload-long"),
          "长调用跑在独立池的线程上（线程名可辨）", long_name)
    check(short_name != "MainThread" and not short_name.startswith("offload-long"),
          "短调用跑在默认执行器（不是主线程、不占长池）", short_name)

    # 独占性：长池被占满时，短调用还能不能立刻跑完
    async def _hog_then_short() -> float:
        workers = O.long_pool_stats()["max_workers"]
        hogs = [asyncio.ensure_future(O.offload_long(time.sleep, 0.6)) for _ in range(workers)]
        await asyncio.sleep(0.05)     # 让 hogs 真的进池占住线程
        t0 = time.perf_counter()
        await O.offload(time.sleep, 0.0)
        waited = time.perf_counter() - t0
        await asyncio.gather(*hogs)
        return waited

    waited = asyncio.run(_hog_then_short())
    check(waited < 0.2,
          f"长池占满（{ O.long_pool_stats()['max_workers'] } 个长任务在跑）时，短调用仍立刻执行",
          f"等待 {waited * 1000:.1f}ms")

    # 负对照：共用**一个**池就是这个样子（分池不是装饰）
    from concurrent.futures import ThreadPoolExecutor

    def _shared_pool_control() -> float:
        pool = ThreadPoolExecutor(max_workers=O.long_pool_stats()["max_workers"])
        try:
            hogs = [pool.submit(time.sleep, 0.6) for _ in range(O.long_pool_stats()["max_workers"])]
            time.sleep(0.05)
            t0 = time.perf_counter()
            pool.submit(time.sleep, 0.0).result()
            return time.perf_counter() - t0
        finally:
            for h in hogs:
                h.result()
            pool.shutdown()

    shared = _shared_pool_control()
    check(shared >= 0.2,
          "负对照：同一个池被占满时，短调用排队等满 600ms 级长任务",
          f"等待 {shared * 1000:.0f}ms（这就是分池要避免的形态）")


# ── ② 真实 handler 的事件循环存活 ─────────────────────────────────────

async def t2_liveness() -> None:
    section(f"② 真实 handler + {BLOCK * 1000:.0f}ms 慢依赖：循环存活（探针 {PROBE_INTERVAL * 1000:.0f}ms 周期）")
    # 预热一轮（block=0）：把所有**首次成本**先付掉 —— 模块导入（实测单个 handler
    # 的模块首导入 400~800ms，`agent.auth.backend` 要拉起整个 langgraph_sdk）、
    # 字节码、首次建线程、yaml 解析。不预热的话探针量到的是"第一次调用这个 handler"
    # 的导入开销，不是稳态行为（这个坑本脚本第一版就踩了，见下方 slow_ticks 诊断）。
    # 生产里这些导入发生在启动时：`src/api/custom_app.py` 逐个 import 所有 api 模块。
    for _name, _make in CASES:
        await run_case(_make, 0.0)
    probe, elapsed, _ = await run_case(case_baseline, BLOCK)
    print(f"  · 基线（纯 await sleep，无同步阻塞）：探针最大滞后 {probe.max_lag * 1000:.0f}ms "
          f"/ 耗时 {elapsed * 1000:.0f}ms —— 这是本机噪声底，阈值取 {LAG_LIMIT * 1000:.0f}ms")
    for name, make in CASES:
        probe, elapsed, seen = await run_case(make, BLOCK)
        assert_case(name, probe, elapsed, seen)
    # 用例内部的附带断言（不只看"卡不卡"，还看搬对了没）
    _probe, _e, seen = await run_case(case_put_feedback, BLOCK)
    check(seen.get("enqueued") is True,
          "put_feedback：搬走写库之后，后续环节（入标注队列）照常执行",
          f"观测={sorted(seen)}")


# ── ③ 负对照：offload 变就地调用（= 修复前的行为）──────────────────────

async def t3_negative_control() -> None:
    section("③ 负对照：把被测模块的 offload/offload_long 换成就地调用 → 断言必须变红")

    async def _inline(fn, /, *a, **k):        # 复刻修复前：同步调用直接挂在循环上
        return fn(*a, **k)

    for name, make in CASES:
        mod_name = CASE_MODULES[name]
        mod = __import__(mod_name, fromlist=["__x"])
        undo = swap(mod, offload=_inline, offload_long=_inline)
        try:
            probe, elapsed, seen = await run_case(make, BLOCK)
        finally:
            undo()
        lag = probe.max_lag
        red = lag >= LAG_LIMIT and seen.get("thread") == "MainThread"
        check(red, f"{name}：就地调用 → 探针滞后 {lag * 1000:.0f}ms（≥{LAG_LIMIT * 1000:.0f}ms）"
                   f"且慢活在主线程 —— ② 的绿不是恒真",
              f"修复前形态锁定了「一个请求把全站按住 {BLOCK * 1000:.0f}ms」")


# ── ④ verify_token（每个受保护请求都走）为什么故意不搬线程 ─────────────

def t4_verify_token_cost() -> None:
    section("④ verify_token 热路径耗时 vs 一次线程切换（「故意不搬」的实测判据）")
    from agent.auth.token import sign_token, verify_token
    from agent.auth.users import load_users
    from agent.utils import offload as O

    users = load_users()                       # 首次会落盘建表（一次性成本，先付掉）
    admin = next((u for u in users if u.get("user_id") == "admin"), None)
    if admin is None:
        check(False, "④ 前置：用户表里有 admin（用于量 verify_token 成本）")
        return
    tok = sign_token("admin", str(admin.get("display_name", "admin")),
                     bool(admin.get("is_admin")), int(admin.get("token_version", 0)))
    check(verify_token(tok) is not None, "④ 前置：签出来的 token 能验过（否则量的是失败路径）")

    n = 3000
    t0 = time.perf_counter()
    for _ in range(n):
        verify_token(tok)
    per_verify = (time.perf_counter() - t0) / n

    async def _hop_bench() -> float:
        def _noop() -> None:
            pass

        await O.offload(_noop)                 # 预热默认执行器线程
        t0 = time.perf_counter()
        for _ in range(200):
            await O.offload(_noop)
        return (time.perf_counter() - t0) / 200

    per_hop = asyncio.run(_hop_bench())
    check(per_verify < 0.001,
          f"verify_token 是廉价调用（{per_verify * 1e6:.0f}µs/次，读进程内缓存无阻塞 I/O）")
    check(per_verify < per_hop,
          "它比「搬一次线程」还便宜 → 包 offload 只会让每个请求更慢",
          f"verify_token {per_verify * 1e6:.0f}µs vs 线程切换 {per_hop * 1e6:.0f}µs"
          f"（{per_hop / per_verify:.1f}×）")


# ── ⑤ 静态回归：offload_long 不得吃「读请求上下文」的函数 ──────────────

_CONTEXT_API = {"get_config", "_lg_get_config", "get_user_from_context"}


def _callee_name(node: ast.AST) -> str:
    f = getattr(node, "func", node)
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def _context_reading_funcs(src: str) -> set[str]:
    """本文件里「读了 langgraph 请求上下文」的函数/别名名（含嵌套函数与别名赋值）。

    必须做**传递闭包 + 别名传播**，否则会漏掉真正的形态：
      · 传递：`_enhanced_build_check_result` 自己不调 `get_config()`，它是通过
        `_current_db_name()` 读的；
      · 别名：真正被搬走的那个名字是 `_mod._build_check_result`（deepagents 的宿主
        模块 + 本仓替换函数的赋值别名，见 check_progress.py:1239），裸函数名根本不在
        调用点里出现 —— 只看裸 `Name` 的检查器会把这个洞留成"通过"。
    """
    tree = ast.parse(src)
    bodies: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies.setdefault(node.name, []).append(node)
    readers = {n for n, nodes in bodies.items()
               if any(_callee_name(c) in _CONTEXT_API
                      for b in nodes for c in ast.walk(b) if isinstance(c, ast.Call))}
    assigns: list[tuple[list[ast.AST], ast.AST]] = []
    for node in ast.walk(tree):
        targets = list(getattr(node, "targets", []) or [])
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if targets and getattr(node, "value", None) is not None:
            assigns.append((targets, node.value))   # noqa: B909

    # 固定点：**传递闭包与别名传播必须交替迭代** —— 先做别名再做闭包不够：
    # 真实形态是「别名赋值的右边自己不是直读者，而是调 reader 的中间函数」
    # （`_mod._build_check_result = _enhanced_build_check_result`，后者调 `_current_db_name`），
    # 顺序反了就一个都认不出来。本脚本第一版正是这么错的。
    for _ in range(len(bodies) + 2):
        before = set(readers)
        for n, nodes in bodies.items():          # 传递：调了 reader 的函数本身也是 reader
            if n in readers:
                continue
            if any(_callee_name(c) in readers
                   for b in nodes for c in ast.walk(b) if isinstance(c, ast.Call)):
                readers.add(n)
        for targets, value in assigns:           # 别名：`<obj>.<attr> = <reader>`
            if not isinstance(value, ast.Name) or value.id not in readers:
                continue
            for t in targets:
                if isinstance(t, ast.Attribute):
                    readers.add(t.attr)
                elif isinstance(t, ast.Name):
                    readers.add(t.id)
        if readers == before:
            break
    return readers


def _offload_long_names(src: str) -> list[tuple[int, str]]:
    """本文件里 `offload_long(<第一个参数>, ...)` 被搬走的**被调函数名**。

    裸名字（`_run_wren`）与属性名（`_mod._build_check_result`）都收 —— 只收裸名字时，
    把调用点改成属性引用就能绕过本检查。
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and _callee_name(node) == "offload_long" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Name):
                out.append((node.lineno, arg.id))
            elif isinstance(arg, ast.Attribute):
                out.append((node.lineno, arg.attr))
    return out


def _static_violations(files: list[tuple[str, str]]) -> list[tuple[str, int, str]]:
    """逐**文件**判定（reader 名字集按文件算）。

    不能合成一张全局名字表：`_run` 这种通用名字各文件都有（`git_repo._run` 是 git
    子进程、`wren_semantic._run_wren` 是 CLI 步骤）—— 全局表会把无关的函数名"传染"成
    reader，报出一堆假阳性（本脚本第一版就在 `git_repo.py:136` 上报过一次）。

    已知边界：跨模块的别名（`from x import reader as r`）不在覆盖内。本仓的搬运点都是
    同文件闭包/函数，两条真实形态（直读、宿主模块属性别名）由下面两条合成负对照钉住。
    """
    bad: list[tuple[str, int, str]] = []
    for p, src in files:
        try:
            readers = _context_reading_funcs(src)
            sites = _offload_long_names(src)
        except SyntaxError:
            continue
        bad += [(p, ln, n) for ln, n in sites if n in readers]
    return bad


def t5_static_guard() -> None:
    section("⑤ 静态回归：`offload_long` 不得搬走「读请求上下文」的函数（规则从代码推导）")
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    files = [(str(p.relative_to(root)), p.read_text(encoding="utf-8"))
             for p in sorted(root.rglob("*.py"))]
    readers: set[str] = set()
    sites = 0
    for _p, src in files:
        try:
            readers |= _context_reading_funcs(src)
            sites += len(_offload_long_names(src))
        except SyntaxError:
            continue
    check(sites >= 20, f"解析到了 {sites} 处 offload_long 调用点（解析失败会静默变 0）")
    # 诊断集非空才算「检查器真的在查」——三个名字分别覆盖三条识别路径：
    # 直读、传递（`_enhanced_build_check_result` → `_current_db_name`）、别名（`_mod._x = f`）
    for probe_name, how in (("_session_thread_id", "直读 get_config"),
                            ("_current_db_name", "直读 get_config"),
                            ("_build_check_result", "别名 + 传递")):
        check(probe_name in readers, f"探针识别得出上下文读取者：{probe_name}（{how}）")
    bad = _static_violations(files)
    check(not bad, f"{len(files)} 个文件里没有 `offload_long(<上下文读取者>)`",
          "; ".join(f"{p}:{ln} {n}" for p, ln, n in bad) or "0 处")
    # 合成负对照：两种形态各来一个，检查器都必须报出来
    fake_direct = "\n".join([
        "from langgraph.config import get_config",
        "def _build_check_result(x):",
        "    return get_config().get('configurable', {})",
        "async def _endpoint():",
        "    await offload_long(_build_check_result, 1)",
    ])
    fake_alias = "\n".join([
        "import deepagents.middleware as _mod",
        "def _db_name():",
        "    from langgraph.config import get_config",
        "    return get_config().get('configurable', {}).get('db_name', '')",
        "def _enhanced(run):",
        "    return _db_name()",
        "_mod._build_check_result = _enhanced",
        "async def _endpoint():",
        "    await offload_long(_mod._build_check_result, 1)",
    ])
    hit = _static_violations([("fake_direct.py", fake_direct),
                              ("fake_alias.py", fake_alias)])
    got = sorted({n for _p, _ln, n in hit})
    check(got == ["_build_check_result"],
          "负对照：直读形态与「宿主模块属性 + 别名」形态都被报出来（两种绕过路都堵上）",
          f"报出 {got}")


# ── main ─────────────────────────────────────────────────────────────

def seed() -> None:
    """铺一个最小语义库：target/mdl.json + knowledge/glossary/*.md。"""
    (PROJECT / "target").mkdir(parents=True, exist_ok=True)
    (PROJECT / "wren_project.yml").write_text("name: verify-proj\n", encoding="utf-8")
    (PROJECT / "target" / "mdl.json").write_text(
        '{"models":[{"name":"t1"},{"name":"t2"}],"views":[],"relationships":[],'
        '"cubes":[],"dataSource":"mysql"}',
        encoding="utf-8",
    )
    glossary = PROJECT / "knowledge" / "glossary"
    glossary.mkdir(parents=True, exist_ok=True)
    for i in range(40):
        (glossary / f"term_{i:02d}.md").write_text(
            f"# 术语 {i}\n\n口径说明（第 {i} 条）。\n", encoding="utf-8"
        )
    # 预热重导入：read_knowledge 里的 `from wren.memory.markdown import ...` 发生在
    # handler 内，首次 import 的字节码/依赖加载是真·阻塞（且只发生一次）——
    # 本项测的是「遍历读文件」，所以先把 import 成本付掉，不把它算进探针滞后。
    try:
        import wren.memory.markdown  # noqa: F401
    except Exception as e:  # noqa: BLE001  离线环境可能没装 wren
        print(f"  · 提示：wren 不可 import（{e}）—— read_knowledge 用例会提前失败")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    seed()
    t1_pools()
    asyncio.run(t2_liveness())
    asyncio.run(t3_negative_control())
    t4_verify_token_cost()
    t5_static_guard()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 70}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
