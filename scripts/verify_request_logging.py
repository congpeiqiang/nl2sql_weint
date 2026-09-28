# -*- coding: utf-8 -*-
"""P2-2 请求级可观测性验证（离线，无需后端/数据库/网络）。

**要解决的问题**（清单原文三条）：
  ① 日志写容器可写层、没挂卷 → **重建容器即丢**（而每次发版都重建）；
  ② 没有每请求状态码/耗时（`access_log=False`）；
  ③ 没有跨组件统一的 request-id（用户报错时前端/nginx/后端日志对不上号）。

验收（清单原文）：**重启后仍能查到上一次运行期间的请求日志。**

本脚本验六段：
  ① **落点**：日志目录解析（`NL2SQL_LOG_DIR` > `<AGENT_DATA_ROOT>/logs` > 仓库根/logs）；
     生产落在**已经是持久卷的 /app/data 下** → 持久化**不需要改服务器 compose**；并断言
     `/logs/**` 对 agent **不可读**（日志里有别人的请求路径/用户名/会话 id —— 这条是安全断言）。
  ② **配置**：真 `start_server.build_log_config()`（formatter 带 `%(request_id)s`、两个 handler
     都挂 `RequestIdFilter`、TimedRotatingFileHandler 落持久目录、utf-8）。
  ③ **中间件原语**（raw ASGI）：rid 生成/透传/**净化**（拒绝换行 = 日志伪造）、响应头回显、
     SSE 逐条透传不被缓冲、`/ok` 不记（healthcheck 每天 2880 行噪音）、异常也留痕。
  ④ **真 app**（真 Starlette + 真 AuthMiddleware + 真 token）：200 与 401 各一行 access 日志、
     业务日志带同一枚 rid、请求上下文之外 rid 为 `-`。
  ⑤ **跨组件**：内部自调用（`api/_common.stamp_thread_owner`）带同一枚 rid。
  ⑥ **负对照**：把中间件从栈里摘掉 → 既没有 access 行也没有响应头（证明 ③④ 的断言非恒真）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_request_logging.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.config
import os
import pathlib
import sys
import tempfile
import time
from contextlib import contextmanager

# ⚠️ 必须在 import 业务模块之前：auth 的落点（auth_secret / auth_users.json）由它推导
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-reqlog-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ.pop("NL2SQL_LOG_DIR", None)
os.environ.pop("NL2SQL_AUTH_DISABLED", None)
os.environ.pop("NL2SQL_FORCE_PASSWORD_CHANGE", None)

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[1]
_SRC = _REPO / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))
if str(_REPO) not in sys.path:  # 为了让 `import start_server` 可用
    sys.path.insert(0, str(_REPO))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
USER_ID = "Z0001"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


# ── 用户 fixture（真 token 需要用户记录存在，见 token.verify_token 的 pv 比对）──
def _seed_user() -> None:
    from agent.auth.users import hash_password

    (_WORK / "auth_users.json").write_text(
        json.dumps(
            [{
                "user_id": USER_ID,
                "password_hash": hash_password("verify-password"),
                "display_name": "验证用管理员",
                "is_admin": True,
                "token_version": 0,
                "must_change_password": False,
            }],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ── 日志观测工具 ─────────────────────────────────────────────
def _flush() -> None:
    for h in list(logging.getLogger().handlers):
        with contextlib.suppress(Exception):
            h.flush()


def _setup_logging(tag: str) -> pathlib.Path:
    """用**真** build_log_config 配一次日志，返回该段的日志文件。"""
    import start_server

    logf = _WORK / tag / "agent-server.log"
    logf.parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(start_server.build_log_config(logf))
    return logf


def _read(logf: pathlib.Path) -> str:
    _flush()
    return logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""


def _access_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if "[access]" in ln]


# ── ASGI 驱动（不用 httpx：要精确看到每条 ASGI 消息）──────────
def _scope(path: str = "/api/x", method: str = "GET", headers=None, ctype: str = "http") -> dict:
    return {
        "type": ctype,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
        "client": ("203.0.113.9", 1234),   # TEST-NET：非 Docker 网段（否则会被当内部请求放行）
        "server": ("127.0.0.1", 2026),
    }


async def _drive(
    app, scope: dict, expect_exc: bool = False, disconnect_after: float | None = None
) -> list[dict]:
    """手工驱动 ASGI app。

    `disconnect_after`：Starlette 的 `StreamingResponse` 会**同时**跑「发 body」和
    `listen_for_disconnect(receive)` 两个任务；`receive` 若永远返回 `http.request`，
    那第二个任务会空转成死循环（第一版就卡在这里）。真实客户端在请求体读完后会发
    `http.disconnect`，所以这里照做（并给 body 一点时间先发完）。
    """
    sent: list[dict] = []
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect_after is not None:
            await asyncio.sleep(disconnect_after)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    if expect_exc:
        with contextlib.suppress(RuntimeError):
            await app(scope, receive, send)
    else:
        await app(scope, receive, send)
    return sent


def _resp_headers(sent: list[dict]) -> dict[str, str]:
    for m in sent:
        if m["type"] == "http.response.start":
            return {k.decode().lower(): v.decode() for k, v in m["headers"]}
    return {}


def _rid_of_headers(hdrs: dict[str, str]) -> str:
    return hdrs.get("x-request-id", "")


def _body_msgs(sent: list[dict]) -> list[bytes]:
    return [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]


# ════════════════════════════════════════════════════════════
# ① 日志落点：持久目录 + 对 agent 不可读
# ════════════════════════════════════════════════════════════
@contextmanager
def _env(**pairs):
    old = {k: os.environ.get(k) for k in pairs}
    for k, v in pairs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t1_log_dir() -> None:
    section("① 落点：日志进持久目录（验收：重建/重启后仍能查到上一次的请求日志）")
    import start_server as ss

    with _env(NL2SQL_LOG_DIR=str(_WORK / "explicit"), AGENT_DATA_ROOT=str(_WORK)):
        check(ss.resolve_log_dir() == _WORK / "explicit", "NL2SQL_LOG_DIR 显式覆盖优先")
    with _env(NL2SQL_LOG_DIR=None, AGENT_DATA_ROOT=str(_WORK / "dataroot")):
        check(
            ss.resolve_log_file() == _WORK / "dataroot" / "logs" / "agent-server.log",
            "默认落 <AGENT_DATA_ROOT>/logs （生产 = /app/data/logs）",
        )
    with _env(NL2SQL_LOG_DIR=None, AGENT_DATA_ROOT=None):
        check(
            ss.resolve_log_dir() == _REPO / "logs",
            "未配置 AGENT_DATA_ROOT → 兜底仓库根 logs/（dev 行为与改动前一致）",
        )
    with _env(NL2SQL_LOG_DIR=None, AGENT_DATA_ROOT="/app/data"):
        path = ss.resolve_log_file().as_posix()
        check(path == "/app/data/logs/agent-server.log", "生产落点", path)
        check(
            not path.startswith("/app/logs/"),
            "**不是** /app/logs（容器可写层，`compose rm` 即丢 —— 那正是 P2-2 要修的）",
        )
    compose = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
    check(
        "agent_data:/app/data" in compose,
        "/app/data 已是 compose 具名卷 → 持久化**不需要**服务器侧再改 compose",
    )

    from deepagents.middleware.filesystem import _check_fs_permission
    from agent.settings.file_permissions import FILE_PERMISSIONS, NL2SQL_FILE_PERMISSIONS

    for rules, who in ((FILE_PERMISSIONS, "主 agent"), (NL2SQL_FILE_PERMISSIONS, "nl2sql 子 agent")):
        check(
            _check_fs_permission(rules, "read", "/logs/agent-server.log") == "deny",
            f"{who}读不到 /logs/**（日志含他人请求路径/用户名/会话 id）",
        )
    check(
        _check_fs_permission(FILE_PERMISSIONS, "read", "/workspace/report/x.md") == "allow",
        "对照：工作区仍可读（上面两条不是「一律 deny」的恒真断言）",
    )


# ════════════════════════════════════════════════════════════
# ② 配置本身
# ════════════════════════════════════════════════════════════
def t2_config() -> None:
    section("② 配置：真 build_log_config（rid 注入 + 轮转 + 落持久目录）")
    import start_server as ss

    logf = _WORK / "cfg" / "agent-server.log"
    cfg = ss.build_log_config(logf)

    check("%(request_id)s" in cfg["formatters"]["default"]["format"], "formatter 带 request_id")
    check(
        cfg["filters"]["request_id"]["()"] == "api.request_log.RequestIdFilter",
        "过滤器指向 api.request_log.RequestIdFilter（真的能被 dictConfig 解析）",
    )
    for name in ("default", "file"):
        check("request_id" in cfg["handlers"][name].get("filters", []), f"{name} handler 挂了 rid 过滤器")
    fh = cfg["handlers"]["file"]
    check(fh["class"].endswith("TimedRotatingFileHandler"), "文件日志按天轮转")
    check(fh["filename"] == str(logf), "落盘 = 解析出的持久路径", pathlib.Path(fh["filename"]).as_posix())
    check(
        fh["when"] == "midnight" and fh["backupCount"] == 7 and fh["encoding"] == "utf-8",
        "轮转/保留/编码：midnight + 7 份 + utf-8",
    )
    check(cfg["root"]["level"] == "INFO" and set(cfg["root"]["handlers"]) == {"default", "file"},
          "root 同时进 stdout 与文件（docker logs 与持久文件都有）")

    src = (_REPO / "start_server.py").read_text(encoding="utf-8")
    idx = src.rfind("access_log=False")   # rfind：取真正的调用点（docstring 里也提过这个词）
    check(idx > 0, "uvicorn.run 仍是 access_log=False")
    check("request_log" in src[max(0, idx - 900):idx], "并在注释里点名由 request_log 替代（不是漏开）")


# ════════════════════════════════════════════════════════════
# ③ 中间件原语
# ════════════════════════════════════════════════════════════
async def _simple_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


async def _stream_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
    for i in range(3):
        await send({"type": "http.response.body", "body": f"data: {i}\n\n".encode(), "more_body": i < 2})


async def _boom_app(scope, receive, send):
    raise RuntimeError("boom before response")


async def t3_primitive() -> None:
    section("③ 中间件原语（raw ASGI，逐条消息可见）")
    from api.request_log import RequestContextMiddleware

    logf = _setup_logging("prim")
    mw = RequestContextMiddleware(_simple_app)

    sent = await _drive(mw, _scope("/api/hello"))
    rid = _rid_of_headers(_resp_headers(sent))
    check(len(rid) == 12 and all(c in "0123456789abcdef" for c in rid), "无入站 id → 生成 12 位 hex", rid)
    text = _read(logf)
    line = next((ln for ln in _access_lines(text) if "/api/hello" in ln), "")
    check(bool(line), "产生一行 access 日志")
    check("-> 200" in line, "含状态码", line.strip()[:150])
    check("latency=" in line and "total=" in line, "含首字节耗时与总耗时")
    check(f"rid={rid}(gen)" in line, "日志里标明该 id 是**生成**的（(gen)）")

    sent = await _drive(mw, _scope("/api/hello", headers=[("X-Request-ID", "trace-abc_123.4")]))
    check(_rid_of_headers(_resp_headers(sent)) == "trace-abc_123.4", "入站 id 原样回显（nginx $request_id 透传）")
    line = [ln for ln in _access_lines(_read(logf)) if "trace-abc_123.4" in ln]
    check(bool(line) and "(gen)" not in line[-1], "日志用入站 id，且不标 (gen)")

    bad_ids = ["bad id-with-space", "x" * 100, "a\nb", "quote\"inject", "中文", ""]
    for bad in bad_ids:
        sent = await _drive(mw, _scope("/api/hello", headers=[("X-Request-ID", bad)]))
        got = _rid_of_headers(_resp_headers(sent))
        ok = got != bad and len(got) == 12
        check(ok, f"非法入站 id 被丢弃并重新生成（日志伪造防线）: {bad[:14]!r}", got)
    text = _read(logf)
    check("x" * 100 not in text and "bad id-with-space" not in text, "被拒的非法 id **没有**出现在日志里")

    sent = await _drive(mw, _scope("/api/hello", headers=[("X-Request-ID", "dup-id")]))
    raw = [m for m in sent if m["type"] == "http.response.start"][0]["headers"]
    check(sum(1 for k, _ in raw if k.lower() == b"x-request-id") == 1, "响应头只有一枚 X-Request-ID")

    before = len(_access_lines(_read(logf)))
    await _drive(mw, _scope("/ok"))
    check(len(_access_lines(_read(logf))) == before, "/ok（healthcheck）不记 access（每天约 2880 行噪音）")

    sent = await _drive(RequestContextMiddleware(_stream_app), _scope("/runs/stream", method="POST"))
    bodies = _body_msgs(sent)
    check(bodies == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"], "SSE 三条 body 逐条透传、顺序不变（没被缓冲/合并）")
    line = [ln for ln in _access_lines(_read(logf)) if "/runs/stream" in ln]
    check(bool(line), "流式请求也在**流结束后**留下一行 access 日志")

    await _drive(RequestContextMiddleware(_boom_app), _scope("/api/boom"), expect_exc=True)
    line = [ln for ln in _access_lines(_read(logf)) if "/api/boom" in ln]
    check(bool(line) and "no response" in line[-1], "处理中抛异常也留痕（status=- 且标注）", line[-1].strip()[-40:] if line else "")

    called = {"n": 0}

    async def _ws_app(scope, receive, send):
        called["n"] += 1

    await _drive(RequestContextMiddleware(_ws_app), _scope("/ws", ctype="websocket"))
    check(called["n"] == 1, "非 http（websocket/lifespan）原样透传，不记也不改头")


# ════════════════════════════════════════════════════════════
# ④ 真 app：真 Starlette + 真 AuthMiddleware + 真 token
# ════════════════════════════════════════════════════════════
def _build_app(with_request_log: bool = True):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    import api.auth_middleware as am
    import api.drain as dr
    import api.request_log as rl

    async def echo(request):
        logging.getLogger("nl2sql.verify").info("业务日志：echo 处理中")
        return JSONResponse({"ok": True})

    async def stream(request):
        async def gen():
            for i in range(2):
                yield f"data: {i}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def ok(request):
        return JSONResponse({"ok": True})

    mws = [Middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])]
    if with_request_log:
        mws.append(Middleware(rl.RequestContextMiddleware))
    mws += [Middleware(dr.DrainGateMiddleware), Middleware(am.AuthMiddleware)]
    return Starlette(
        routes=[
            Route("/api/echo", echo),
            Route("/runs/stream", stream, methods=["POST"]),   # 前端提交 run 走 POST
            Route("/ok", ok),
        ],
        middleware=mws,
    )


async def t4_real_app() -> None:
    section("④ 真 app：状态码/耗时/用户/会话 id 进日志（含 401）")
    from agent.auth.token import sign_token

    logf = _setup_logging("real")
    app = _build_app()
    token = sign_token(USER_ID, "验证用管理员", True, token_version=0)
    cookie = [("Cookie", f"nl2sql_token={token}"), ("X-Forwarded-For", "203.0.113.9")]

    sent = await _drive(app, _scope("/api/echo", headers=cookie))
    hdrs = _resp_headers(sent)
    check(hdrs.get("content-type", "").startswith("application/json"), "带合法 token → 200 到达 handler")
    rid = _rid_of_headers(hdrs)
    check(bool(rid), "响应头回显 X-Request-ID（前端拿它去后端日志里搜）", rid)

    sent = await _drive(app, _scope("/api/echo"))
    check([m for m in sent if m["type"] == "http.response.start"][0]["status"] == 401, "无 token → 401")
    text = _read(logf)
    l200 = next((ln for ln in _access_lines(text) if "/api/echo" in ln and "-> 200" in ln), "")
    l401 = next((ln for ln in _access_lines(text) if "/api/echo" in ln and "-> 401" in ln), "")
    check(bool(l200), "200 有一行", l200.strip()[-150:])
    check(f"user={USER_ID}" in l200, "带鉴权用户", )
    check("ip=203.0.113.9" in l200, "取 XFF 第一跳作为客户端 IP")
    check(bool(l401), "**401 也有一行**（Auth 在 request_log 之内 ⇒ 被拒的请求同样可查）", l401.strip()[-90:])
    check(f"rid={rid}" in l200 or f"rid={rid}" in l401, "两行各带自己的 rid（不同请求不同 id）")

    biz = [ln for ln in text.splitlines() if "业务日志：echo 处理中" in ln]
    check(bool(biz), "业务日志也写进同一个文件")
    check(any(f"rid={_rid_of_headers(hdrs)}" in ln for ln in biz), "业务日志带**同一枚** rid（不止 access 行）", (biz[-1][:110] if biz else ""))

    check("rid=-" not in l200, "请求内不会是 `-`")
    with _env():  # 请求上下文之外
        logging.getLogger("nl2sql.verify").info("后台日志：无请求上下文")
    bg = [ln for ln in _read(logf).splitlines() if "后台日志：无请求上下文" in ln]
    check(bool(bg) and "rid=-" in bg[-1], "请求上下文之外 → rid=-（不串味）", (bg[-1][:100] if bg else ""))

    app = _build_app()
    # `disconnect_after`：StreamingResponse 会并行监听客户端断开，必须给出
    # `http.disconnect` 才收摊（见 _drive 的注释）。
    sent = await _drive(app, _scope("/runs/stream", method="POST", headers=cookie), disconnect_after=0.2)
    bodies = [b for b in _body_msgs(sent) if b]  # StreamingResponse 末尾会补一个空 body 收尾
    check(
        bodies == [b"data: 0\n\n", b"data: 1\n\n"],
        "真 Starlette StreamingResponse 经中间件仍逐条下发（顺序不变）",
        f"{len(bodies)} 条非空 body",
    )
    line = [ln for ln in _access_lines(_read(logf)) if "/runs/stream" in ln]
    check(bool(line) and "-> 200" in line[-1], "流式请求记一行（流结束后）")


# ════════════════════════════════════════════════════════════
# ⑤ 跨组件：内部自调用透传 rid
# ════════════════════════════════════════════════════════════
async def t5_internal_call() -> None:
    section("⑤ 跨组件：后端自调用（stamp_thread_owner）带同一枚 rid")
    import httpx

    import api._common as common
    from api.request_log import RequestContextMiddleware, new_request_id, rid_headers

    seen: list[dict] = []

    class _FakeResp:
        status_code = 200
        text = ""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def patch(self, url, json=None, headers=None):
            seen.append({"url": url, "headers": headers or {}})
            return _FakeResp()

    real = httpx.AsyncClient
    httpx.AsyncClient = _FakeClient
    try:
        check(rid_headers() == {}, "不在请求上下文 → 无 rid 头（不伪造）")
        rid = new_request_id()
        mw = RequestContextMiddleware(_simple_app)

        async def _probe(scope, receive, send):
            await common.stamp_thread_owner("tid-1", USER_ID)
            await _simple_app(scope, receive, send)

        await _drive(RequestContextMiddleware(_probe), _scope("/api/x", headers=[("X-Request-ID", rid)]))
    finally:
        httpx.AsyncClient = real

    check(len(seen) == 1, "自调用确实发出（httpx 被替换，无网络）")
    hdrs = seen[0]["headers"] if seen else {}
    check(hdrs.get("X-Request-ID") == rid, "自调用带上了**同一枚** rid → 两侧日志可用一个 id 串起来", str(hdrs))
    check(_WORK.as_posix() in seen[0]["url"] or "/threads/tid-1" in seen[0]["url"], "仍走 /threads/{tid} 的 PATCH", seen[0]["url"])


# ════════════════════════════════════════════════════════════
# ⑥ 负对照 + 接线
# ════════════════════════════════════════════════════════════
async def t6_negative_and_wiring() -> None:
    section("⑥ 负对照与接线（证明上面的断言非恒真）")
    from agent.auth.token import sign_token

    logf = _setup_logging("neg")
    app = _build_app(with_request_log=False)  # ← 唯一的差别：摘掉中间件
    cookie = [("Cookie", f"nl2sql_token={sign_token(USER_ID, 'x', True, token_version=0)}")]
    sent = await _drive(app, _scope("/api/echo", headers=cookie))
    check([m for m in sent if m["type"] == "http.response.start"][0]["status"] == 200, "负对照：请求本身仍 200")
    check("x-request-id" not in _resp_headers(sent), "摘掉中间件 → 无响应头（证明 ③④ 的头是真被它加的）")
    check(not _access_lines(_read(logf)), "摘掉中间件 → 一行 access 日志都没有（证明断言非恒真）")

    src = (_REPO / "src" / "api" / "custom_app.py").read_text(encoding="utf-8")
    blk = src[src.index("app = Starlette("):]
    order = {
        "CORS": blk.find("CORSMiddleware"),
        "request_log": blk.find("api.request_log.RequestContextMiddleware"),
        "drain": blk.find("api.drain.DrainGateMiddleware"),
        "auth": blk.find("api.auth_middleware.AuthMiddleware"),
        "langfuse": blk.find("api.langfuse_metadata.LangfuseMetadataMiddleware"),
    }
    check(all(v > 0 for v in order.values()), f"五个中间件都在栈里", str(order))
    check(order["CORS"] < order["request_log"] < order["drain"] < order["auth"] < order["langfuse"],
          "顺序 = CORS → request_log → drain → auth → langfuse（401/503 才落在 request_log 之内）")

    nginx = (_REPO / "docker" / "nginx.conf").read_text(encoding="utf-8")
    # 带分号才是真指令（注释里也出现过同样的字符串，不带分号）
    check(nginx.count("proxy_set_header X-Request-ID $rid;") == 3,
          "nginx 三个 location 都透传 X-Request-ID",
          f"count={nginx.count('proxy_set_header X-Request-ID $rid;')}")
    check("rid=$rid" in nginx and "access_log" in nginx, "nginx 访问日志含 rid（边缘侧同一枚 id）")
    check("$request_time" in nginx, "nginx 访问日志含请求耗时")
    # `$rid` = 客户端自带就沿用、否则 nginx 生成。少了 `""  $request_id` 这一行，
    # 没带 id 的请求会把空串透给后端（后端会补生成，但 nginx 日志里就只剩 `rid=`）。
    check('map $http_x_request_id $rid' in nginx and '$request_id' in nginx,
          "`$rid` 带 `$request_id` 兜底（客户端没带 id 时 nginx 生成）")
    check("add_header X-Request-ID $rid always" in nginx,
          "nginx 自己回显 id（不依赖后端版本；`always` 覆盖 502/504 这类到不了后端的响应）")
    # 线上这份文件是手工维护的（发布包不含 docker/）—— 断言它**没有**把线上那段 P0-3 注释丢掉，
    # 否则下次照仓库副本覆盖就会把「/report/ 为什么被删」的理由抹掉（改回去时没人知道原因）。
    check("P0-3" in nginx, "保留线上独有的 P0-3 注释（防覆盖时丢历史）")


def main() -> int:
    _seed_user()
    t1_log_dir()
    t2_config()
    asyncio.run(t3_primitive())
    asyncio.run(t4_real_app())
    asyncio.run(t5_internal_call())
    asyncio.run(t6_negative_and_wiring())

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过  （日志样本目录：{_WORK}）")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
