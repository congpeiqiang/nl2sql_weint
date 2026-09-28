# -*- coding: utf-8 -*-
"""P2-1 优雅停机（drain）验证（离线，无需后端/数据库/网络）。

**要解决的问题**：`docker stop` 默认给 10 秒就 SIGKILL，而 langgraph inmem 运行时的
排空窗口是**硬编码 5 秒**（`langgraph_runtime_inmem/queue.py: SHUTDOWN_GRACE_PERIOD_SECS`；
`langgraph_api.config.BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS` 在本版本无人读取）→ 发版时
在跑的 run（一次问数几个后台任务、可能跑几分钟）必被腰斩，症状是"半轮对话/前端转圈"，
不会有人把它当成发版故障。

本脚本验四件事（都不需要真起服务）：
  ① **拦截面**：排空态只拒"新建 run"的 7 条路径（含 `POST /threads/{tid}/runs/stream`
     这条前端真正用的），**放行**取消/查询/列表/定时任务 —— 排空期间用户最需要能用的
     恰恰是"取消我这个跑不完的查询"。拒绝必须发生在入口（下游不许被调用）。
  ② **等待语义**：`wait_for_idle` 真等到清零；预算用尽不装成成功（`drained=False` +
     点名剩余数）；期间可被 `DELETE` 撤销；读不到计数就不等（不凭空拖住发版）。
  ③ **生命周期顺序**：真 `custom_app._lifespan` 退出时 **先排空、后 flush Langfuse**
     （反了的话最后一批 run 的分数只能靠 atexit 兜底）。**破坏性负对照**：预算置 0
     → flush 时任务仍未清零，证明 ③ 的"等到了"不是恒真。
  ④ **端点**：`POST/GET/DELETE /api/admin/drain` 走真 `AuthMiddleware`（管理员 200 /
     非管理员 403 / 未登录 401）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_graceful_drain.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import pathlib
import sys
import tempfile
import time
import types

# ⚠️ 必须在 import 业务模块之前：auth/token 的落点由 AGENT_DATA_ROOT 推导
os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-drain-"))
os.environ.pop("NL2SQL_DRAIN_SECS", None)

_HERE = pathlib.Path(__file__).resolve()
_SRC = _HERE.parents[1] / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
NGINX_IP = "172.18.0.9"
ADMIN = "Z0001"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 假计数（在跑的后台任务数）────────────────────────────────────────

class FakeWorkers:
    """按预设序列返回在跑任务数（最后一次值会一直重复），并记录每次读取的时刻。"""

    def __init__(self, seq: list[int]) -> None:
        self.seq = list(seq) or [0]
        self.calls: list[tuple[float, int]] = []

    def __call__(self) -> int:
        value = self.seq[min(len(self.calls), len(self.seq) - 1)]
        self.calls.append((time.perf_counter(), value))
        return value

    @property
    def zero_ts(self) -> float | None:
        for ts, value in self.calls:
            if value == 0:
                return ts
        return None


@contextlib.contextmanager
def fake_workers(seq: list[int]):
    """替换 `api.drain.active_workers`（模块级函数，等待循环里每次重新读）。"""
    import api.drain as drain

    fake = FakeWorkers(seq)
    original = drain.active_workers
    drain.active_workers = fake  # type: ignore[assignment]
    try:
        yield fake
    finally:
        drain.active_workers = original  # type: ignore[assignment]


@contextlib.contextmanager
def fake_langfuse_flush(sink: list[float]):
    """把 `agent.trace.langfuse_client.flush_langfuse` 换成只记时刻的桩。

    用**替身模块**而不是打真模块：真模块 import 期就会去连 Langfuse，本项是离线用例。
    lifespan 里是 `from agent.trace.langfuse_client import flush_langfuse`（调用时才 import），
    所以放 sys.modules 里就够。
    """
    key = "agent.trace.langfuse_client"
    real = sys.modules.get(key)
    stub = types.ModuleType(key)
    stub.flush_langfuse = lambda *a, **k: sink.append(time.perf_counter())  # type: ignore[attr-defined]
    sys.modules[key] = stub
    try:
        yield sink
    finally:
        if real is not None:
            sys.modules[key] = real
        else:
            sys.modules.pop(key, None)


@contextlib.contextmanager
def drain_budget(secs: str):
    """临时改预算（`wait_for_idle` 每次现读 `budget_secs()`）。"""
    import api.drain as drain

    original = os.environ.get("NL2SQL_DRAIN_SECS")
    os.environ["NL2SQL_DRAIN_SECS"] = secs
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("NL2SQL_DRAIN_SECS", None)
        else:
            os.environ["NL2SQL_DRAIN_SECS"] = original


def reset_drain() -> None:
    import api.drain as drain

    drain.end_drain()


# ── ① 拦截面 ──────────────────────────────────────────────────────────

class StubDownstream:
    """被包裹的下游 app：只记调用并回 200。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, scope, receive, send):
        self.calls.append(f"{scope.get('method')} {scope.get('path')}")
        body = b'{"ok":true}'
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


async def call(app, method: str, path: str) -> tuple[int, dict, list[tuple[bytes, bytes]]]:
    """直接喂 ASGI scope（不经过 httpx：本项测的就是中间件本身）。"""
    import json

    status = 0
    body = b""
    headers: list[tuple[bytes, bytes]] = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        nonlocal status, body, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    scope = {
        "type": "http", "method": method, "path": path, "headers": [],
        "query_string": b"", "scheme": "http", "http_version": "1.1",
    }
    await app(scope, receive, send)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        parsed = {}
    return status, parsed, headers


# 7 条"新建 run"路径（langgraph_api/api/runs.py 的 ApiRoute 定义）
RUN_CREATE_PATHS = [
    "/runs/stream", "/runs/wait", "/runs", "/runs/batch",
    "/threads/t-1/runs/stream", "/threads/t-1/runs/wait", "/threads/t-1/runs",
]
# 排空期间必须仍可用的（用户要能取消/查询/看列表）。
# ⚠️ 必须带方法：`POST /threads/{tid}/runs` **是**创建 run（该拒），
# `GET` 同路径才是"列出会话的 run"（该放行）—— 只看路径会把两者混为一谈。
RUN_ALLOW_CALLS = [
    ("POST", "/runs/cancel"),                    # 取消一个 run
    ("POST", "/threads/t-1/runs/r-1/cancel"),    # 同上（带会话前缀）
    ("GET", "/threads/t-1/runs"),                # 列出会话的 run
    ("GET", "/threads/t-1/runs/r-1"),            # 单个 run 状态
    ("GET", "/threads/t-1/runs/r-1/join"),       # 切回会话时重新接上流
    ("POST", "/threads/t-1/runs/r-1/join"),
    ("POST", "/runs/crons"),                     # 定时任务增删改查
    ("POST", "/threads/t-1/runs/crons"),
    ("DELETE", "/runs/crons/c-1"),
    ("GET", "/runs/crons"),
    ("GET", "/ok"),                              # 探活
    ("GET", "/api/reports/x.md"),                # 自定义 API（排空期仍要能下载已出的报告）
    ("GET", "/api/threads/t-1/export"),
    ("PUT", "/api/threads/t-1/messages/m-1/feedback"),  # 给刚跑完的回答点赞/点踩
]


async def t1_gate() -> None:
    section("① 拦截面：排空只拒'新建 run'，取消/查询照常")
    import api.drain as drain

    stack = drain.DrainGateMiddleware(StubDownstream())

    # 未排空：一律放行
    for path in RUN_CREATE_PATHS[:3]:
        status, _, _ = await call(stack, "POST", path)
        check(status == 200, f"未排空：POST {path} 放行", str(status))

    drain.begin_drain(reason="verify")
    down = stack.app  # type: ignore[attr-defined]
    for path in RUN_CREATE_PATHS:
        down.calls.clear()
        status, payload, headers = await call(stack, "POST", path)
        check(status == 503, f"排空：POST {path} → 503", str(status))
        check(not down.calls, "   拒绝发生在入口（下游未被调用，不会'先跑一半再拒'）")
        if path == "/runs/stream":  # 响应形态只查一次
            hdr = {k.decode(): v.decode() for k, v in headers}
            check(payload.get("draining") is True and "排空" in payload.get("error", ""),
                  "   503 正文带明确原因（前端可直接展示）", str(payload.get("error")))
            check(hdr.get("retry-after") == "30" and hdr.get("content-type", "").startswith("application/json"),
                  "   带 Retry-After + JSON content-type", str(hdr))

    for method, path in RUN_ALLOW_CALLS:
        down.calls.clear()
        status, _, _ = await call(stack, method, path)
        check(status == 200, f"排空：{method} {path} 放行", str(status))

    # 非 http scope（lifespan / websocket）原样透传（不能因为放行判断炸掉）
    sent: list[dict] = []

    async def ws_send(m):
        sent.append(m)

    await stack({"type": "websocket", "path": "/runs/stream"}, lambda: None, ws_send)
    check(bool(sent), "非 http scope 原样透传（websocket 不被误伤）")

    reset_drain()
    status, _, _ = await call(stack, "POST", "/runs/stream")
    check(status == 200, "撤销排空后恢复接单", str(status))


# ── ② 等待语义 ────────────────────────────────────────────────────────

async def t2_wait() -> None:
    section("② 等待语义：真等清零 / 超时不装成功 / 可撤销 / 无计数不等")
    import api.drain as drain

    # 真等到清零
    reset_drain()
    with fake_workers([3, 2, 1, 0]), drain_budget("10"):
        drain.begin_drain(reason="verify")
        state = await drain.wait_for_idle(poll_secs=0.01)
    check(state["drained"] is True and state["remaining"] == 0,
          "连续读到清零才返回 drained=True", f"waited={state['waited_secs']:.2f}s")
    check(0 < state["waited_secs"] < 5, "等待时长 = 真实轮询时间（不是立刻返回）",
          f"{state['waited_secs']:.2f}s")

    # 预算用尽：不装成功，remaining 留在快照里
    reset_drain()
    with fake_workers([5, 5, 5, 5, 5, 5, 5, 5]), drain_budget("1"):
        drain.begin_drain(reason="verify")
        t0 = time.perf_counter()
        state = await drain.wait_for_idle(poll_secs=0.05)
        elapsed = time.perf_counter() - t0
    check(state["drained"] is False and state["remaining"] == 5,
          "**超时不装成功**：仍有 5 个在跑（日志会点名，便于事后归因）", str(state["remaining"]))
    # 这条同时是"预算被 int() 截断成 0"的回归：`int(1.0 - ε) == 0` 会让等待整个跳过，
    # 表现就是 elapsed≈0 —— 不许出现。
    check(1.0 <= elapsed < 1.5, "耗时贴着预算（1s，不是被截断成 0 直接跳过）", f"{elapsed:.2f}s")

    # 期间撤销 → 立即结束（运维改主意）
    reset_drain()
    with fake_workers([4]), drain_budget("30"):
        drain.begin_drain(reason="verify")

        async def undo():
            await asyncio.sleep(0.05)
            drain.end_drain()

        task = asyncio.create_task(undo())
        t0 = time.perf_counter()
        state = await drain.wait_for_idle(poll_secs=0.02)
        await task
        elapsed = time.perf_counter() - t0
    check(state["drained"] is False and elapsed < 3,
          "排空期间被 DELETE 撤销 → 立即返回（不空等 30s）", f"{elapsed:.2f}s")

    # 预算 0 = 只拒绝新请求、不等待
    reset_drain()
    with fake_workers([9]), drain_budget("0"):
        drain.begin_drain(reason="verify")
        t0 = time.perf_counter()
        state = await drain.wait_for_idle()
        elapsed = time.perf_counter() - t0
    check(elapsed < 0.3 and state["drained"] is False,
          "NL2SQL_DRAIN_SECS=0 → 不等待（只拒绝新提交）", f"{elapsed:.2f}s")

    # 计数的真源在第三方包里：名字/语义变了要在这里炸，而不是让排空悄悄退化成"不等待"
    from langgraph_runtime_inmem.queue import get_num_workers

    check(
        isinstance(get_num_workers(), int) and drain.active_workers() == get_num_workers(),
        "计数真源可用：`langgraph_runtime_inmem.queue.get_num_workers()`（未起服务时应为 0）",
        str(get_num_workers()),
    )

    # 读不到计数（非 inmem 运行时 / 包变了）→ 真 `active_workers` 自己兜底成 0。
    # 这里不能打桩 `drain.active_workers`（那是"上层函数抛异常"，兜底不在那条路径上）——
    # 要让真函数里的 import 失败：把 sys.modules 里那项设成 None，import 即 ImportError。
    reset_drain()
    key = "langgraph_runtime_inmem.queue"
    real_mod = sys.modules.get(key)
    real_have = key in sys.modules
    sys.modules[key] = None  # type: ignore[assignment]
    try:
        check(drain.active_workers() == 0, "计数模块不可导入 → 真 `active_workers` 兜底为 0（不抛给调用方）")
        with drain_budget("30"):
            drain.begin_drain(reason="verify")
            t0 = time.perf_counter()
            state = await drain.wait_for_idle(poll_secs=0.01)
            elapsed = time.perf_counter() - t0
        check(state["remaining"] == 0 and elapsed < 0.5,
              "**没有计数就不等**：宁可照旧硬停，也不凭空把发版拖住 30s", f"{elapsed:.2f}s")
    finally:
        if real_have:
            sys.modules[key] = real_mod  # type: ignore[assignment]
        else:
            sys.modules.pop(key, None)

    # 预算解析
    with drain_budget("abc"):
        check(drain.budget_secs() == drain._DEFAULT_BUDGET_SECS, "非法预算值 → 回退默认")
    with drain_budget("45"):
        check(drain.budget_secs() == 45, "NL2SQL_DRAIN_SECS 生效")
    os.environ.pop("NL2SQL_DRAIN_SECS", None)
    check(drain.budget_secs() == drain._DEFAULT_BUDGET_SECS, "未设置 → 默认预算")


# ── ③ 生命周期顺序（真 custom_app._lifespan）──────────────────────────

async def t3_lifespan() -> None:
    section("③ 生命周期：先排空、后 flush（真 `custom_app._lifespan`）")
    import api.drain as drain
    from api import custom_app

    # 正常路径：3 → 0，flush 必须发生在清零之后
    reset_drain()
    sink: list[float] = []
    with fake_workers([3, 3, 0]), fake_langfuse_flush(sink), drain_budget("10"):
        async with custom_app._lifespan(app=None):
            check(not drain.is_draining(), "启动阶段不处于排空态（不影响正常接单）")
        workers = drain.active_workers
    state = drain.drain_state()
    check(state["drained"] is True, "退出时等到了清零", f"waited={state['waited_secs']:.2f}s")
    check(len(sink) == 1, "停机 flush 仍被调用（排空没有吃掉它）", str(len(sink)))
    zero_ts = workers.zero_ts  # type: ignore[attr-defined]
    check(zero_ts is not None and sink and sink[0] > zero_ts,
          "**顺序**：flush 在清零之后（最后一批 run 的 trace 也进本次 flush）",
          f"zero={zero_ts and round(zero_ts, 3)} flush={round(sink[0], 3) if sink else None}")

    # 破坏性负对照：预算 0 → 不等，flush 时任务仍在跑
    reset_drain()
    sink2: list[float] = []
    with fake_workers([7]), fake_langfuse_flush(sink2), drain_budget("0"):
        async with custom_app._lifespan(app=None):
            pass
        workers2 = drain.active_workers
        state2 = drain.drain_state()  # 趁假计数还生效时取快照（否则会读到真计数 0）
    check(state2["drained"] is False and len(sink2) == 1,
          "**负对照**：预算 0 → 不等就 flush（此刻仍有 7 个任务在跑）→ ③ 的'等到了'不是恒真",
          f"waited={state2['waited_secs']:.3f}s remaining={state2['remaining']}")
    check(workers2.calls and workers2.calls[0][1] == 7,  # type: ignore[attr-defined]
          "负对照确实读到了非零计数（不是'本来就没任务'）")

    # 排空自身抛异常也不能阻断停机（flush 仍要跑）
    reset_drain()
    sink3: list[float] = []
    real_wait = drain.wait_for_idle

    async def _boom(*a, **k):
        raise RuntimeError("drain exploded")

    with fake_langfuse_flush(sink3):
        drain.wait_for_idle = _boom  # type: ignore[assignment]
        try:
            async with custom_app._lifespan(app=None):
                pass
        finally:
            drain.wait_for_idle = real_wait  # type: ignore[assignment]
    check(len(sink3) == 1, "排空异常被兜住，停机 flush 照常执行（不阻断停机）")


# ── ④ 端点（真 AuthMiddleware）───────────────────────────────────────

async def t4_endpoints() -> None:
    section("④ 端点：真 AuthMiddleware 下的管理员门")
    import httpx
    from starlette.applications import Starlette

    import api.drain as drain
    from api.auth_middleware import AuthMiddleware

    def mint_user(uid: str, is_admin: bool):
        from _auth_test_support import mint_for  # P1-12：先登记账号再签 token

        return mint_for(uid, is_admin)

    app = AuthMiddleware(Starlette(routes=list(drain.routes)))

    async def fetch(method: str, path: str, uid: str | None = None, is_admin: bool = False):
        headers = {"X-Forwarded-For": "1.2.3.4"}
        if uid:
            headers["Cookie"] = f"nl2sql_token={mint_user(uid, is_admin)}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(NGINX_IP, 12345)),
            base_url="http://testserver",
        ) as c:
            return await c.request(method, path, headers=headers)

    reset_drain()
    r_anon = await fetch("POST", "/api/admin/drain")
    check(r_anon.status_code == 401, "未登录 → 401", str(r_anon.status_code))

    with fake_workers([0]), drain_budget("10"):
        r_user = await fetch("POST", "/api/admin/drain", uid="Z0051", is_admin=False)
        check(r_user.status_code == 403, "非管理员 → 403", str(r_user.status_code))

        r_admin = await fetch("POST", "/api/admin/drain", uid=ADMIN, is_admin=True)
        check(r_admin.status_code == 200, "管理员 → 200", str(r_admin.status_code))
        payload = r_admin.json()
        check(payload.get("draining") is True and payload.get("reason") == "admin",
              "返回排空态（含来源，便于归因）", str({k: payload.get(k) for k in ("draining", "reason")}))

        r_status = await fetch("GET", "/api/admin/drain", uid=ADMIN, is_admin=True)
        check(r_status.status_code == 200 and r_status.json().get("draining") is True,
              "GET 查状态", str(r_status.status_code))

        # 排空态下，新 run 被拒（端点与 gate 是同一个状态）
        stack = drain.DrainGateMiddleware(StubDownstream())
        status, _, _ = await call(stack, "POST", "/threads/t-1/runs/stream")
        check(status == 503, "排空态与中间件共享同一状态（端点置位后 gate 立即生效）", str(status))

    r_undo = await fetch("DELETE", "/api/admin/drain", uid=ADMIN, is_admin=True)
    check(r_undo.status_code == 200 and r_undo.json().get("draining") is False, "DELETE 恢复接单")
    reset_drain()


# ── ⑤ 静态接线 ────────────────────────────────────────────────────────

def t5_wiring() -> None:
    section("⑤ 接线：真 app 的中间件顺序与路由表 + `_lifespan` 顺序")
    from api import custom_app

    # 中间件顺序：直接读真 app 的 user_middleware（比 AST 可靠；此处 app 尚未构建栈，
    # 保持声明顺序）。langgraph 组装时把它整体提到全局（server.py），所以这里就是全局顺序。
    labels = [getattr(m.cls, "__name__", str(m.cls)) for m in custom_app.app.user_middleware]
    check("DrainGateMiddleware" in labels, "DrainGateMiddleware 在 app 的中间件链上", " → ".join(labels))
    check(labels.index("DrainGateMiddleware") < labels.index("AuthMiddleware"),
          "gate 排在 Auth 之前（排空期间先拒、不用做鉴权）")
    check(labels and labels[0] == "CORSMiddleware", "CORS 仍最外层（跨域 cookie 头不被内层中间件吃掉）")

    paths = [getattr(r, "path", "") for r in custom_app.app.routes]
    check("/api/admin/drain" in paths, "排空端点已挂进真路由表", str(custom_app.app.routes[0].path if paths else ""))

    # `_lifespan` 里 drain 必须在 flush 之前（顺序在函数体里 → 只能看 AST）。
    # ⚠️ 必须按 `lineno` 排：`ast.walk` 是广度优先，嵌套的 await 会比同级的语句后出，
    # 直接按遍历次序比会得出反的结论（实测踩过）。
    tree = ast.parse((_SRC / "api/custom_app.py").read_text(encoding="utf-8"))
    body = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_lifespan")
    calls = [ast.unparse(n) for n in sorted(
        (n for n in ast.walk(body) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset)
    )]
    i_wait = next(i for i, c in enumerate(calls) if "wait_for_idle" in c)
    i_flush = next(i for i, c in enumerate(calls) if "flush_langfuse" in c)
    check(i_wait < i_flush, "`_lifespan` 里先 wait_for_idle 再 flush_langfuse", f"wait#{i_wait} < flush#{i_flush}")

    import api.drain as drain

    check(
        drain._RUN_CREATE_RE.match("/threads/abc/runs/stream") is not None
        and drain._RUN_CREATE_RE.match("/runs/cancel") is None
        and drain._RUN_CREATE_RE.match("/threads/abc/runs/r1/cancel") is None
        and drain._RUN_CREATE_RE.match("/runs/crons") is None,
        "白名单正则只认创建路径（cancel / crons 不在内）",
    )
    # 与 langgraph 真实路由表对账：创建型 POST 路径集合 = 我们拦的那 7 条
    check(
        set(RUN_CREATE_PATHS) == {
            "/runs", "/runs/stream", "/runs/wait", "/runs/batch",
            "/threads/t-1/runs", "/threads/t-1/runs/stream", "/threads/t-1/runs/wait",
        } and all(drain._RUN_CREATE_RE.match(p) for p in RUN_CREATE_PATHS),
        "本脚本列的 7 条 = 正则全部命中（runs.py 的 ApiRoute 定义）",
    )

    # 「挂 app 级就够」这条前提写在第三方包的源码里，且**依赖配置**：
    # 只有默认分支才是 `custom_middleware + global_middleware`（全局生效、覆盖原生 run 路径）；
    # 若配 `HTTP_CONFIG.middleware_order = "auth_first"`，自定义中间件退化成只挂自定义路由的
    # Mount 级 → 原生 /threads/{tid}/runs/stream 不受管，排空门形同虚设（且不报错）。
    server_src = next(iter(pathlib.Path(p).resolve() for p in sys.path if (pathlib.Path(p) / "langgraph_api/server.py").is_file()), None)
    text = (server_src / "langgraph_api/server.py").read_text(encoding="utf-8") if server_src else ""
    check("custom_middleware + global_middleware" in text,
          "langgraph 组装时把自定义中间件提到全局（server.py 默认分支）")
    check('get("middleware_order") == "auth_first"' in text,
          "        ↑该行为的前提：存在 auth_first 分支（它会让自定义中间件不再全局生效）")
    configured = []
    for env_file in (".env", ".env.prod", "docker-compose.yml"):
        p = _HERE.parents[1] / env_file
        if p.is_file() and "middleware_order" in p.read_text(encoding="utf-8", errors="ignore"):
            configured.append(env_file)
    check(not configured,
          "**本仓没配 `middleware_order`**（配成 auth_first 会让排空门漏掉原生 run 路径，且静默）",
          str(configured))


def t6_ops_entry() -> None:
    section("⑥ 运维入口：容器内脚本（无口令）能拿到真管理员身份 + 端点不可达时优雅退出")
    import importlib.util

    spec = importlib.util.spec_from_file_location("ops_drain", _HERE.parent / "ops_drain.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    uid, token = mod._admin_token()
    from agent.auth.token import verify_token

    user = verify_token(token)
    check(bool(user) and user.get("is_admin") and user.get("user_id") == uid,
          "签到**能过 verify_token** 的管理员 token（不靠任何口令）", f"uid={uid}")
    check(uid == ADMIN, "挑中的是需要改密之外的第一个管理员", uid)

    # 端点不可达：必须快速失败 + 给出"直接停容器也有排空"的兜底提示（退出码 1）
    import subprocess

    env = dict(os.environ, NL2SQL_OPS_URL="http://127.0.0.1:1", PYTHONIOENCODING="utf-8")
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(_HERE.parent / "ops_drain.py"), "--budget", "1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    check(proc.returncode == 1, "端点不可达 → 退出码 1（不假装成功）", str(proc.returncode))
    check("信号式排空" in out, "**并告诉运维下一步**：直接停容器仍会触发服务端排空", "")
    check(time.perf_counter() - t0 < 30, "快速失败（不卡住发版）")


async def main_async() -> int:
    await t1_gate()
    await t2_wait()
    await t3_lifespan()
    await t4_endpoints()
    t5_wiring()
    t6_ops_entry()
    reset_drain()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
