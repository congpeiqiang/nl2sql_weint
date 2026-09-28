# -*- coding: utf-8 -*-
"""P1-2「run 入参运行期授权」验证（离线，无需后端 / 数据库 / 网络）。

被验对象：`src/api/langfuse_metadata.py`（钳制入口）+ `src/agent/middlewares/tool_filter.py`
（未选库时的裁剪），即「客户端能通过 run 请求体 config.configurable 拿到什么」这条线。

验四件事：
  ① 伪造 `configurable.user_id`（他人的身份，用来加载他人模型配置含 api_key）→
     被按 AuthMiddleware 校验过的登录身份**覆盖**。
  ② 伪造 `configurable.db_name`（未授权库）→ 整个 run 被 403 拒掉，而不是「静默清空」——
     清空比越权更糟：tool_filter 里 `if not db_name: return tools`，空库名等于给全部库的工具。
  ③ 归属登记（record_thread_db）必须发生在 ② 之后（顺序契约，见 _inject 注释）。
  ④ 未选库（db_name 为空）这条「绕过口」已堵：仍按调用者可见库裁剪 wrenai 工具。

同时验「不该动的没动」：内部调用（子 agent / sync，身份 internal）、管理员、dev 旁路、
`/runs/cancel` 这类没有 config 的端点，一律保持原行为。

运行：
    uv run --no-project python scripts/verify_config_authz.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import 业务模块之前：grants/users/db_config 的落点都由 AGENT_DATA_ROOT 推导，
# 不设会写进仓库（auth_secret 甚至会被发版 tar 打进镜像）。
os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-authz-"))
os.environ.setdefault("NL2SQL_AUTH_DISABLED", "0")

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

ALICE = "alice"
BOB = "bob"
ROOT = "root"
DB_OK = "db_alpha"     # alice 有授权
DB_NO = "db_beta"      # alice 无授权（bob 有）


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n── {title} ──")


# ── 环境播种 ────────────────────────────────────────────

def seed() -> None:
    from agent.auth.grants import grant_db, register_user
    from agent.auth.users import add_user
    from mcp_server.db_mcp_server.db.core.db_config_store import DBConfig, get_store

    store = get_store()
    for name in (DB_OK, DB_NO):
        store.upsert(DBConfig(name=name, db_type="mysql", host="h", port=3306, database=name))

    add_user(ALICE, "pwd-alice-1", "Alice")
    add_user(BOB, "pwd-bob-1", "Bob")
    add_user(ROOT, "pwd-root-1", "Root", is_admin=True)
    for u in (ALICE, BOB, ROOT):
        register_user(u, u)
    grant_db(ALICE, DB_OK)   # alice 只能看 db_alpha
    grant_db(BOB, DB_NO)


def user_dict(uid: str, is_admin: bool = False) -> dict:
    return {"user_id": uid, "display_name": uid, "is_admin": is_admin}


def scope_for(uid: str, is_admin: bool = False, path: str = "/threads/t-alice/runs/stream") -> dict:
    """合成 ASGI scope（与 AuthMiddleware 注入的 state 同形）。"""
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"x-forwarded-for", b"172.18.0.9")],
        "query_string": b"",
        "state": {"user": user_dict(uid, is_admin)},
    }


def run_body(db_name: str = DB_OK, user_id: str | None = None) -> dict:
    configurable: dict = {"enable_thinking": "true", "llm_route": "deepseek", "llm_model": "chat"}
    if db_name:
        configurable["db_name"] = db_name
    if user_id:
        configurable["user_id"] = user_id
    return {
        "assistant_id": "agent",
        "input": {"messages": [{"type": "human", "content": "上月销售额"}]},
        "config": {"configurable": configurable},
    }


# ── ① 钳制函数级断言 ────────────────────────────────────

def case_clamp() -> None:
    from api.langfuse_metadata import _clamp_body, _clamp_config

    section("① configurable 钳制（伪造 user_id / db_name / 运行期键）")

    # —— 伪造 user_id：他人身份 ——
    body = run_body(user_id=BOB)
    err = _clamp_config(body, user_dict(ALICE), "t-alice")
    got = body["config"]["configurable"]["user_id"]
    check(err == "" and got == ALICE, "伪造 configurable.user_id=bob → 覆盖为登录身份 alice",
          f"err={err!r} user_id={got!r}")

    # —— 伪造 langgraph_auth_user_id（同一身份的别名）——
    body = run_body()
    body["config"]["configurable"]["langgraph_auth_user_id"] = BOB
    _clamp_config(body, user_dict(ALICE), "t-alice")
    alias = body["config"]["configurable"].get("langgraph_auth_user_id")
    check(alias == ALICE, "伪造 langgraph_auth_user_id=bob → 覆盖为 alice", f"got={alias!r}")

    # —— 客户端提供的 langgraph_auth_user（内部 auth 对象）一律删除 ——
    body = run_body()
    body["config"]["configurable"]["langgraph_auth_user"] = {"identity": BOB, "permissions": ["admin"]}
    _clamp_config(body, user_dict(ALICE), "t-alice")
    check("langgraph_auth_user" not in body["config"]["configurable"],
          "客户端自带的 langgraph_auth_user（内部 auth 对象）被删除")

    # —— 未伪造时不误伤其它业务键 ——
    body = run_body(db_name=DB_OK)
    _clamp_config(body, user_dict(ALICE), "t-alice")
    cfg = body["config"]["configurable"]
    check(cfg.get("enable_thinking") == "true" and cfg.get("llm_route") == "deepseek"
          and cfg.get("llm_model") == "chat" and cfg.get("db_name") == DB_OK,
          "未伪造的 enable_thinking / llm_route / llm_model / db_name 原样保留",
          f"{sorted(cfg)}")

    # —— 伪造 db_name（未授权库）：必须拒绝，且**不能**只清空 ——
    body = run_body(db_name=DB_NO)
    err = _clamp_config(body, user_dict(ALICE), "t-alice")
    check(err != "", "伪造 db_name=db_beta（alice 无授权）→ 返回拒绝原因（→403）", f"err={err!r}")
    check(body["config"]["configurable"].get("db_name") == DB_NO,
          "被拒时**不清空** db_name（清空=暴露全部库工具，比越权更糟），保持原值交给上层拒绝")

    # —— 授权库放行 ——
    body = run_body(db_name=DB_OK)
    err = _clamp_config(body, user_dict(ALICE), "t-alice")
    check(err == "", "db_name=db_alpha（已授权）→ 放行")

    # —— 未选库不是越权（前端 localStorage 未选过时就是空）——
    body = run_body(db_name="")
    err = _clamp_config(body, user_dict(ALICE), "t-alice")
    check(err == "", "db_name 为空（未选库）→ 不算越权，放行")

    # —— 管理员跨库 ——
    body = run_body(db_name=DB_NO)
    err = _clamp_config(body, user_dict(ROOT, is_admin=True), "t-root")
    check(err == "", "管理员 db_name=db_beta（未显式授权）→ 放行")

    # —— 运行期键：客户端无权提供 thread_id / checkpoint_id ——
    body = run_body()
    body["config"]["configurable"]["thread_id"] = "t-victim"
    body["config"]["configurable"]["checkpoint_id"] = "ck-1"
    _clamp_config(body, user_dict(ALICE), "t-alice")
    cfg = body["config"]["configurable"]
    check("thread_id" not in cfg and "checkpoint_id" not in cfg,
          "伪造 configurable.thread_id / checkpoint_id（他人会话）被丢弃", f"{sorted(cfg)}")

    body = run_body()
    body["config"]["configurable"]["thread_id"] = "t-alice"
    _clamp_config(body, user_dict(ALICE), "t-alice")
    check(body["config"]["configurable"].get("thread_id") == "t-alice",
          "与 URL 一致（服务端自己写的）thread_id 保留，不误删")

    # —— batch 载荷是 list ——
    good, bad = run_body(), run_body(db_name=DB_NO)
    good["config"]["configurable"]["user_id"] = BOB
    bad["config"]["configurable"]["user_id"] = BOB
    err = _clamp_body([good, bad], user_dict(ALICE), "")
    check(err != "" and good["config"]["configurable"]["user_id"] == ALICE,
          "/runs/batch（载荷是 list）→ 逐项钳制：伪造项被覆盖，越权项被拒",
          f"err={err!r}")


# ── ② 路径覆盖 ──────────────────────────────────────────

def case_paths() -> None:
    from api.langfuse_metadata import _RUN_PATH_RE

    section("② 拦截路径覆盖（nginx 把 ^/(threads|runs|...) 全量转发，都可达）")
    must_match = [
        "/threads/t1/runs", "/threads/t1/runs/stream", "/threads/t1/runs/batch",
        "/runs", "/runs/stream", "/runs/wait", "/runs/batch",
    ]
    miss = [p for p in must_match if not _RUN_PATH_RE.match(p)]
    check(not miss, f"{len(must_match)} 个 run 创建端点全部命中（含 stateless 自建会话）",
          f"漏={miss}")

    must_skip = ["/runs/cancel", "/runs/crons", "/runs/crons/search",
                 "/threads/t1/runs/cancel", "/threads/t1", "/api/db-configs"]
    hit = [p for p in must_skip if _RUN_PATH_RE.match(p)]
    check(not hit, "无 config 载荷的端点不误伤（cancel / crons / 非 run 路径）", f"误中={hit}")


# ── ③ 中间件端到端 ──────────────────────────────────────

class _App:
    """假的内部 app：把 receive 读空并记下 body（验证回放是否完整）。"""

    def __init__(self) -> None:
        self.called = 0
        self.body: bytes | None = None
        self.scope: dict | None = None

    async def __call__(self, scope, receive, save):
        self.called += 1
        self.scope = scope
        chunks = []
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        self.body = b"".join(chunks)
        await save({"type": "http.response.start", "status": 200, "headers": []})
        await save({"type": "http.response.body", "body": b"{}"})


def _receive_for(payload: bytes):
    sent = False

    async def _receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    return _receive


async def call_mw(body: dict | list, scope: dict, *, langfuse: bool, app: _App):
    """跑一次中间件；返回 (send 收到的事件列表, 打桩记录)。

    `seen["inject_saw"]` = `_inject` 看到的 configurable（断言钳制先于注入）；
    `seen["ownership_uid"]` = 归属登记拿到的身份。
    """
    import api.langfuse_metadata as lm

    seen: dict = {}
    orig_enabled = lm.langfuse_enabled
    orig_inject = lm.LangfuseMetadataMiddleware._inject
    orig_own = lm.LangfuseMetadataMiddleware._apply_ownership

    async def fake_inject(self, body_, tid, scope_=None):
        # 记录 _inject 看到的 configurable，用于断言「钳制发生在注入之前」
        if isinstance(body_, dict):
            cfg = (body_.get("config") or {}).get("configurable") or {}
            seen["inject_saw"] = json.loads(json.dumps(cfg))
        return body_

    async def fake_ownership(self, tid, uid, inherited=""):
        seen["ownership_uid"] = uid

    lm.langfuse_enabled = lambda: langfuse
    lm.LangfuseMetadataMiddleware._inject = fake_inject
    lm.LangfuseMetadataMiddleware._apply_ownership = fake_ownership
    try:
        raw = json.dumps(body).encode("utf-8")
        events: list = []

        async def _send(msg):
            events.append(msg)

        mw = lm.LangfuseMetadataMiddleware(app)
        await mw(scope, _receive_for(raw), _send)
        return events, seen
    finally:
        lm.langfuse_enabled = orig_enabled
        lm.LangfuseMetadataMiddleware._inject = orig_inject
        lm.LangfuseMetadataMiddleware._apply_ownership = orig_own


def case_middleware() -> None:
    section("③ 中间件端到端（ASGI 层）")

    # —— 越权 db_name：403 且不进入 app ——
    app = _App()
    events, _ = asyncio.run(call_mw(run_body(db_name=DB_NO), scope_for(ALICE), langfuse=False, app=app))
    status = events[0]["status"] if events and events[0]["type"] == "http.response.start" else None
    payload = json.loads(events[-1]["body"]) if events else {}
    check(status == 403, f"越权 run 被 403 挡在 app 之前（app.called={app.called}）", f"status={status}")
    check(app.called == 0 and "forbidden" in payload.get("error", "") and DB_NO in payload.get("detail", ""),
          "403 响应体带上具体库名，且下游 app 完全没被调用", f"{payload}")

    # —— 合法请求：body 必须被**完整回放**（原先 new_body is body 时转发的是已读空的通道）——
    app = _App()
    events, _ = asyncio.run(call_mw(run_body(db_name=DB_OK), scope_for(ALICE), langfuse=False, app=app))
    parsed = json.loads(app.body or b"{}")
    check(app.called == 1 and parsed.get("config", {}).get("configurable", {}).get("db_name") == DB_OK,
          "合法请求：下游 app 读到的 body 完整（receive 回放正确，非空体）",
          f"len={len(app.body or b'')}")

    # —— 用户身份也要交给下游（langfuse 关时同样）——
    app = _App()
    asyncio.run(call_mw(run_body(db_name=DB_OK), scope_for(ALICE), langfuse=False, app=app))
    cfg = json.loads(app.body or b"{}").get("config", {}).get("configurable", {})
    check(cfg.get("user_id") == ALICE, "langfuse 关闭时也把登录身份写进 configurable（下游 create_model 要读）",
          f"{cfg}")

    # —— 顺序契约：_inject 看到的 configurable 已经是钳制后的 ——
    app = _App()
    _, seen = asyncio.run(call_mw(run_body(db_name=DB_OK, user_id=BOB), scope_for(ALICE),
                                  langfuse=True, app=app))
    check(seen.get("inject_saw", {}).get("user_id") == ALICE,
          "_inject 拿到的 configurable.user_id 已是 alice（钳制先于注入与归属登记）",
          f"inject_saw.user_id={seen.get('inject_saw', {}).get('user_id')!r}")

    # —— 归属登记（langfuse 关的那条分支）拿到的也必须是登录身份 ——
    app = _App()
    _, seen = asyncio.run(call_mw(run_body(db_name=DB_OK), scope_for(ALICE), langfuse=False, app=app))
    check(seen.get("ownership_uid") == ALICE, "归属登记（langfuse 关分支）拿到的是登录身份 alice",
          f"ownership_uid={seen.get('ownership_uid')!r}")

    # —— 内部调用（子 agent / sync）：不钳制，保持服务端镜像的原值 ——
    app = _App()
    internal_scope = scope_for("internal")
    internal_scope["headers"] = []          # 容器内部：无 X-Forwarded-For
    events, _ = asyncio.run(call_mw(run_body(db_name=DB_NO, user_id=ALICE), internal_scope,
                                    langfuse=False, app=app))
    parsed = json.loads(app.body or b"{}")
    cfg = parsed.get("config", {}).get("configurable", {})
    st = events[0]["status"] if events else None
    check(st == 200 and cfg.get("user_id") == ALICE and cfg.get("db_name") == DB_NO,
          "内部调用（identity=internal）不钳制：父 run 已校验过的 user_id/db_name 原样透传",
          f"status={st} cfg={cfg}")

    # —— dev 旁路（NL2SQL_AUTH_DISABLED=1 时身份是 dev）也不钳制 ——
    app = _App()
    events, _ = asyncio.run(call_mw(run_body(db_name=DB_NO, user_id=ALICE), scope_for("dev", is_admin=True),
                                    langfuse=False, app=app))
    st = events[0]["status"] if events else None
    check(st == 200 and app.called == 1, "dev 旁路（identity=dev）不钳制", f"status={st}")

    # —— 非 JSON 体：原样回放，不 403、不崩 ——
    app = _App()
    import api.langfuse_metadata as lm2
    ev2: list = []

    async def _send2(msg):
        ev2.append(msg)

    mw2 = lm2.LangfuseMetadataMiddleware(app)
    asyncio.run(mw2(scope_for(ALICE), _receive_for(b"not-json"), _send2))
    check(app.called == 1 and app.body == b"not-json",
          "非 JSON 请求体：原样回放给下游（既不误 403 也不丢 body）", f"body={app.body!r}")


# ── ④ tool_filter：未选库不再等于「全部库」 ──────────────

class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _tools() -> list:
    return [
        _Tool("wrenai_db_alpha_run_sql"),
        _Tool("wrenai_db_alpha_list_models"),
        _Tool("wrenai_db_beta_run_sql"),
        _Tool("dbmcp_run_sql"),
        _Tool("create_chart"),
    ]


class _AsUser:
    """上下文管理器：让 langgraph.config.get_config() 返回指定身份的 configurable。

    ⚠️ 别写成 `lc.get_config = _fake_config(uid)` 这种「函数即补丁」的写法——
    那样赋值回去的其实是原函数，补丁等于没打（本脚本第一版就这么错过）。
    """

    def __init__(self, uid: str, db_name: str = "") -> None:
        self._uid = uid
        self._db = db_name

    def __enter__(self):
        import langgraph.config as lc

        uid, db = self._uid, self._db

        class _Ctx:
            def get(self, key, default=None):
                if key == "configurable":
                    return {"user_id": uid, "db_name": db}
                return default

        self._orig = lc.get_config
        lc.get_config = lambda: _Ctx()
        return self

    def __exit__(self, *exc):
        import langgraph.config as lc

        lc.get_config = self._orig
        return False


def case_tool_filter() -> None:
    from langchain.agents.middleware import ModelRequest

    from agent.middlewares.tool_filter import ToolFilterMiddleware

    section("④ tool_filter：未选库（db_name 为空）不再暴露全部库的语义层工具")

    def _filter_for(uid: str, tools: list) -> list[str]:
        with _AsUser(uid):
            mw = ToolFilterMiddleware()
            req = ModelRequest(model=object(), messages=[], tools=tools)
            out = mw._filter(req)
            return [t.name for t in out.tools]

    names = _filter_for(ALICE, _tools())
    check("wrenai_db_alpha_run_sql" in names and "wrenai_db_beta_run_sql" not in names,
          "alice 未选库 → 只保留已授权库 db_alpha 的 wrenai 工具，db_beta 被裁掉", f"{names}")
    check("dbmcp_run_sql" in names and "create_chart" in names,
          "dbmcp 与图表等非 wrenai 工具保留（fail-open，与既有策略一致）", f"{names}")

    names = _filter_for("internal", _tools())
    check("wrenai_db_beta_run_sql" in names and len(names) == len(_tools()),
          "内部调用（internal）不裁剪：保持原行为", f"{names}")

    names = _filter_for(ROOT, _tools())
    check(len(names) == len(_tools()), "管理员未选库 → 不裁剪（可见全部已配置库）", f"{names}")

    names = _filter_for(BOB, _tools())
    check("wrenai_db_beta_run_sql" in names and "wrenai_db_alpha_run_sql" not in names,
          "bob（只授权 db_beta）→ 保留 db_beta、裁掉 db_alpha", f"{names}")


# ── ⑤ 负对照：拿掉修复，断言必须被检出 ──────────────────

def case_negative() -> None:
    section("⑤ 负对照（证明上面的断言不是恒真）")

    from api.langfuse_metadata import _clamp_config

    # 旧实现：只注入不校验（P1-2 之前的行为）
    def legacy_clamp(body: dict, user: dict, tid: str) -> str:
        cfg = body.setdefault("config", {}).setdefault("configurable", {})
        return ""  # 什么都不做

    body = run_body(user_id=BOB)
    err = legacy_clamp(body, user_dict(ALICE), "t-alice")
    leaked_uid = body["config"]["configurable"]["user_id"]
    check(err == "" and leaked_uid == BOB,
          "负对照①：不钳制时伪造 user_id=bob 存活（→ 会去加载 bob 的模型配置）",
          f"user_id={leaked_uid!r}")

    body = run_body(db_name=DB_NO)
    err = legacy_clamp(body, user_dict(ALICE), "t-alice")
    check(err == "" and body["config"]["configurable"]["db_name"] == DB_NO,
          "负对照②：不校验时越权 db_name=db_beta 直接放行", f"err={err!r}")

    # 现在证明修复版会拦住（同一输入、同一断言口径）
    body = run_body(user_id=BOB, db_name=DB_NO)
    err = _clamp_config(body, user_dict(ALICE), "t-alice")
    check(err != "" and body["config"]["configurable"]["user_id"] == ALICE,
          "对照组：同一输入走修复版 → 身份被覆盖且越权库被拒", f"err={err!r}")

    # 旧 tool_filter 行为：未选库直接返回全部工具（即 `if not db_name: return tools`）
    from langchain.agents.middleware import ModelRequest
    from agent.middlewares.tool_filter import ToolFilterMiddleware

    tools = _tools()
    legacy_kept = [t.name for t in tools]  # 旧行为 = 一个不裁
    with _AsUser(ALICE):
        mw = ToolFilterMiddleware()
        fixed = [t.name for t in mw._filter(ModelRequest(model=object(), messages=[], tools=tools)).tools]
    check("wrenai_db_beta_run_sql" in legacy_kept and "wrenai_db_beta_run_sql" not in fixed,
          "负对照③：旧行为（空库名原样返回）会暴露 db_beta 工具，修复版裁掉", f"fixed={fixed}")


# ── main ───────────────────────────────────────────────

def main() -> int:
    print("P1-2 run 入参运行期授权验证")
    seed()
    case_clamp()
    case_paths()
    case_middleware()
    case_tool_filter()
    case_negative()

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"结果：{passed}/{total} 通过")
    for ok, label in results:
        if not ok:
            print(f"  [{NG}] {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
