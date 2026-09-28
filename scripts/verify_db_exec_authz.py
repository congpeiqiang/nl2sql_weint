# -*- coding: utf-8 -*-
"""P1-16「执行侧库授权」验证（离线，无需后端 / 真数据库 / 网络 / LLM）。

**被测的坑**：P1-2 只把住了**出站 payload**（`tool_filter` 按用户可见库裁工具），
但「看不见 ≠ 调不到」，两条通道在执行侧完全不看身份：

  · `dbmcp_run_sql(sql=..., db_name="<无授权的库>")` —— 目标库是**工具参数**，
    出站前根本不知道参数里会填哪个库；未选库时 dbmcp 还 fail-open 保留（未建模库上
    它是唯一查询通道）；
  · `wrenai_<slug>_run_sql` —— 库名编码在**工具名**里，而执行侧注册表
    （`mcp_tool.lookup_sub_tool`）只按名字取实例、不看身份：模型幻觉出的工具名、
    以及**历史消息里授权撤销前存的旧工具名**，照样执行。

**怎么验的（尽量不用合成对象）**：用 `langchain.agents.create_agent` 起一个**真图**
（`GenericFakeChatModel` 脚本化产出 tool_calls），中间件挂真的 `QueryGateMiddleware`，
`config={"configurable":{"user_id":...}}` 走真实 `langgraph.config.get_config()` ——
身份、`ToolCallRequest`、中间件链都是真的，唯一打桩的是工具函数本身（记调用次数，
用来断言"到底执行了没有"；tool_filter 的 P1-2 脚本验的是入参钳制，本脚本验的是执行侧）。

验六组：
  ① 基线：有权库上两条通道都照常执行（且探针确认运行期身份真是 configurable 里那个
     —— 否则下面的"拒绝"可能只是身份取空导致的恒真）。
  ② 拒绝：无授权的库上**不执行** + 模型收到明确原因（含库名与可用库清单）——
     dbmcp 参数级、wrenai 工具名级、wrenai 非查询类工具各一例。
  ③ 顺序：无权库上发 dbmcp 时拿到的是**授权**文案而不是"该库已建模，请走语义层"
     —— 规则零必须压过通道硬闸，否则等于告诉越权者"这个库存在且已建模"。
  ④ 管理员：admin 恒有权。
  ⑤ fail-open 的边界（写清理由，别误以为是漏判）：取不到库名（参数空且无标记 /
     wrenai 名反查不到）→ 放行（工具自己会报参数缺失，且反查不到 = 工作区没这个库）；
     内部调用 / 无身份 → 不启用判权。
  ⑥ **破坏性负对照**：把 `_target_db` 打成恒返回 ""（= 拆掉规则零）→ 同一越权调用
     立刻被执行（复刻修复前的行为）。证明 ② 的断言不是恒真。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_db_exec_authz.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import 业务模块之前：grants / users / db_config / auth.sqlite 的落点都由它推导
os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-dbauthz-"))
# 真图 invoke 会把每次 tool 调用当 run 上报（本机若开着 tracing → 403 噪声刷屏）
for _k in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING"):
    os.environ[_k] = "false"

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

ALICE, BOB, ADMIN = "alice", "bob", "admin"
DB_A = "db_alpha"      # 已建模，alice 有权
DB_B = "db_beta"       # 已建模，只有 bob 有权（alice 无权）
DB_U = "db_gamma"      # **未建模**，alice 有权 —— dbmcp 直连的唯一合法场景
DB_X = "db_delta"      # **未建模**，没人有权 —— P1-16 的验收靶子（无规则零就会被查）

BASE = pathlib.Path(os.environ["AGENT_DATA_ROOT"])

# 打桩掉的真实副作用（只为离线可跑，不改变被测逻辑）
_NOPATCHED: dict = {}


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 环境播种 ────────────────────────────────────────────────
#
# ⚠️ 拓扑是有讲究的：`dbmcp_*` 在**已建模**库上会被规则一（通道硬闸）直接拦掉，
# 所以"直连通道能不能执行"这件事**只能在未建模库上观察**。因此：
#   · 未授权的靶子用**未建模**库 db_delta —— 没有规则零时它会一路执行（见 ⑥ 负对照），
#     这才让 ② 的断言成为判别性的（而不是被规则一顺带拦下造成的假绿）。
#   · 已建模的 db_beta 用来验 wrenai 通道（工具名级）与规则顺序（③）。

def patch_mcp_loading() -> None:
    """先 import `mcp_tool` 再把"起 MCP 子进程"换成桩。

    **必须在 `seed()` 之前调**：`agent/tools/mcp_tool.py` 末尾有模块级
    `main_tools = lazy.main_tools` / `sub_tools = lazy.sub_tools`，**import 即加载全部
    MCP server**（起子进程）。若等到播种完再 import，它就会按刚写的 db_config 去真连
    wrenai / dbmcp —— 一个"离线"验证脚本不该拉起一堆子进程（本脚本 2026-09-23 踩过）。
    趁 db_config 还空着先 import，再打桩 `_load_entry`。
    """
    from agent.tools import mcp_tool

    def _fake_load_entry(entry):  # type: ignore[no-untyped-def]
        entry.tools = []
        entry.status = "ok"
        return "ok"

    _NOPATCHED["_load_entry"] = mcp_tool._load_entry
    mcp_tool._load_entry = _fake_load_entry


def restore_mcp_loading() -> None:
    from agent.tools import mcp_tool

    if "_load_entry" in _NOPATCHED:
        mcp_tool._load_entry = _NOPATCHED["_load_entry"]


def seed() -> None:
    from agent.auth.grants import grant_db, register_user
    from agent.auth.users import add_user, update_user
    from agent.utils.semantic_db import get_detector
    from mcp_server.db_mcp_server.db.core.db_config_store import DBConfig, get_store

    store = get_store()
    for name, modeled in ((DB_A, True), (DB_B, True), (DB_U, False), (DB_X, False)):
        proj = BASE / f"_proj_{name}"
        if modeled:
            proj.mkdir(parents=True, exist_ok=True)
        store.upsert(DBConfig(
            name=name, db_type="mysql", host="h", port=3306, database=name,
            wren_project=str(proj) if modeled else "",   # 空 = 未建模
        ))
    get_detector().invalidate()

    add_user(ALICE, "pwd-alice-1", "Alice")
    add_user(BOB, "pwd-bob-1", "Bob")
    # admin 由 users.py 的 _default_users() 自动建好（首次落地即写入）→ 这里只提权
    update_user(ADMIN, password="pwd-admin-1", is_admin=True)
    for u in (ALICE, BOB, ADMIN):
        register_user(u, u)
    grant_db(ALICE, DB_A)
    grant_db(ALICE, DB_U)
    grant_db(BOB, DB_B)


# ── 真图执行（每次一个图，模型脚本化产出 tool_calls）────────
#
# 两种图：默认只有 QueryGateMiddleware；`registry=True` 时把
# `DynamicMCPToolsMiddleware` 挂在最外层，并往运行期注册表里种一个**不在 payload 里**
# 的工具名 —— 复刻生产里"模型幻觉出工具名 / 历史消息里存着授权撤销前的旧名字"这条
# 路径（`_resolve` 会用它 override 掉 request.tool，请求继续往下走 → 到 QueryGate）。

def run_case(
    tool_name: str,
    args: dict,
    uid: str | None,
    *,
    state_msgs: list | None = None,
    registry: bool = False,
) -> dict:
    """跑一次真实 agent 图，返回 {content, status, ran, identity}。

    `ran` = 工具函数**真的执行了**没有（闭包计数）；`identity` = 工具运行时
    `auth.runtime.caller_identity()` 读到的身份（只有执行了才拿得到）。
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, SystemMessage
    from langchain_core.tools import tool

    from agent.middlewares.query_gate import QueryGateMiddleware

    seen: dict = {"ran": 0, "identity": None}
    probe: dict = {}

    class _Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
            return self

    def _mk(name: str):
        def _fn(**kwargs) -> str:
            from agent.auth import runtime as rt

            seen["ran"] += 1
            seen["identity"] = rt.caller_identity()
            probe.update(kwargs)
            return f"ROWS:{name}"

        _fn.__name__ = name
        _fn.__doc__ = f"{name}（桩：只记调用）"
        # ⚠️ 必须带 **kwargs 并以真实签名的形态被调用：参数签名与真实工具同形
        # （sql / db_name / limit），langchain 会先做 args_schema 校验。
        return tool(_fn)

    bound = [
        _mk("dbmcp_run_sql"), _mk("dbmcp_get_db_info"),
        _mk(f"wrenai_{DB_A}_run_sql"), _mk(f"wrenai_{DB_B}_run_sql"),
        _mk(f"wrenai_{DB_B}_get_data_source"),
        _mk("chart_generate"),          # 非库工具：任何情况下都不该被拦
    ]
    middleware = [QueryGateMiddleware()]
    restore = None
    if registry:
        from agent.middlewares.dynamic_mcp_tools import DynamicMCPToolsMiddleware
        from agent.tools import mcp_tool

        planted = _mk(tool_name)        # 同名桩，但**不放进 payload**
        mcp_tool._sub_lookup[tool_name] = planted
        restore = (mcp_tool, tool_name, mcp_tool._sub_registry_warmed)
        mcp_tool._sub_registry_warmed = True
        middleware.insert(0, DynamicMCPToolsMiddleware())

    model = _Model(messages=iter([
        AIMessage(content="", tool_calls=[{
            "name": tool_name, "args": args, "id": "call-1",
        }]),
        AIMessage(content="finish"),
    ]))
    agent = create_agent(model=model, tools=bound, middleware=middleware)
    msgs = [SystemMessage(content=m) for m in (state_msgs or [])]
    msgs.append({"role": "user", "content": "查一下"})
    configurable = {"user_id": uid} if uid is not None else {}
    try:
        out = agent.invoke({"messages": msgs}, config={"configurable": configurable})
    finally:
        if restore is not None:
            mod, name, warmed = restore
            mod._sub_lookup.pop(name, None)
            mod._sub_registry_warmed = warmed

    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    last = tool_msgs[-1] if tool_msgs else None
    return {
        "content": str(getattr(last, "content", "") or ""),
        "status": str(getattr(last, "status", "") or ""),
        "ran": seen["ran"],
        "identity": seen["identity"],
        "args_seen": dict(probe),
    }


def denied(res: dict, db: str) -> tuple[bool, str]:
    """拒绝判据：**没执行** + 文案说清"无权"且点名库与可用库。"""
    ok = (
        res["ran"] == 0
        and res["status"] == "error"
        and res["content"].startswith("Error:")
        and "无权访问" in res["content"]
        and db in res["content"]
        and "可用库" in res["content"]
    )
    return ok, res["content"][:100]


def executed(res: dict, tool_name: str) -> tuple[bool, str]:
    """放行判据：执行了，且返回的是工具自己的结果（不是任何中间件的文案）。"""
    ok = res["ran"] == 1 and res["content"] == f"ROWS:{tool_name}"
    return ok, f"ran={res['ran']} content={res['content'][:60]!r}"


# ── 前置：反查与发现集合可用（否则 wrenai 那几条断言会退化成恒真）──

def t0_preconditions() -> None:
    section("⓪ 前置：已建模库发现 + wrenai 工具名反查 + 标记正则")
    from agent.utils.semantic_db import (
        db_name_from_wrenai_tool, get_detector, wrenai_server_name,
    )

    discovered = get_detector().discover()
    check({DB_A, DB_B} <= discovered and DB_U not in discovered and DB_X not in discovered,
          "两个已建模库在发现集合里、两个未建模库不在", str(sorted(discovered)))
    check(wrenai_server_name(DB_B) == f"wrenai_{DB_B}",
          "server 名 = wrenai_<库名>（纯 ASCII 库名原样）", wrenai_server_name(DB_B))
    check(db_name_from_wrenai_tool(f"wrenai_{DB_B}_run_sql") == DB_B,
          "wrenai 工具名能反查回库名（wrenai 通道判权的前提）",
          db_name_from_wrenai_tool(f"wrenai_{DB_B}_run_sql"))
    check(db_name_from_wrenai_tool("wrenai_ghost_run_sql") == "",
          "不存在的 server 反查为空（→ 调用方放行，见 ⑤）")

    # 与 dynamic_prompt 的真实注入文本逐字对齐（nl2sql_agent.py:154/182 带 Markdown 加粗；
    # 早先的正则要求 `——` 后紧跟文字 → 一条都匹配不上，state 兜底是死代码，本次顺带修）
    from agent.middlewares.query_gate import _ACTIVE_DB_RE

    for db, tail in ((DB_B, "**已在 Wren 语义层建模**"), (DB_B, "**未在语义层建模**")):
        hit = _ACTIVE_DB_RE.findall(f"当前数据库: `{db}` —— {tail}。")
        check(bool(hit), f"真实注入文本能解析出当前库（{tail.strip('*')}）", str(hit))


# ── ① 基线：有权就照常跑 ─────────────────────────────────────

def t1_baseline() -> None:
    section("① 基线：有权库上两条通道都照常执行")
    res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_U}, ALICE)
    check(*executed(res, "dbmcp_run_sql"),
          "alice 直连自己的**未建模**库 → 执行（dbmcp 的合法场景）")

    res = run_case(f"wrenai_{DB_A}_run_sql", {"sql": "select 1"}, ALICE)
    check(*executed(res, f"wrenai_{DB_A}_run_sql"), "alice 走语义层查自己的库 → 执行")
    check(res["identity"] == ALICE,
          "**身份来自真实 configurable**（不是打桩喂进去的）——否则下面的拒绝可能是恒真",
          f"identity={res['identity']!r}")

    res = run_case(f"wrenai_{DB_B}_run_sql", {"sql": "select 1"}, BOB)
    check(*executed(res, f"wrenai_{DB_B}_run_sql"),
          "bob 走语义层查他自己的库 → 执行（同一张图、同一中间件）")


# ── ② 拒绝 ──────────────────────────────────────────────────

def t2_deny() -> None:
    section("② 无授权的库：不执行 + 明确原因")
    res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_X}, ALICE)
    ok, detail = denied(res, DB_X)
    check(ok, "**验收**：alice 直连未授权库 db_delta → 被拒且模型收到明确原因", detail)
    check(DB_A in res["content"] and DB_U in res["content"],
          "拒绝文案里给出可用库清单（模型能自己换库重试）")
    check(res["args_seen"] == {}, "工具函数**一次都没被调**（不是执行后再拦）")

    res = run_case("dbmcp_get_db_info", {"db_name": DB_X}, ALICE)
    ok, detail = denied(res, DB_X)
    check(ok, "get_db_info 同样判 —— 表清单也是越权信息", detail)

    res = run_case(f"wrenai_{DB_B}_run_sql", {"sql": "select 1"}, ALICE)
    ok, detail = denied(res, DB_B)
    check(ok, "wrenai 通道（库名在工具名里）→ 同样被拒", detail)

    res = run_case(f"wrenai_{DB_B}_get_data_source", {}, ALICE)
    ok, detail = denied(res, DB_B)
    check(ok, "语义层**非查询类**工具（get_data_source）同样判 —— 它照样按库吐业务元数据",
          detail)

    res = run_case("chart_generate", {"option": "{}"}, ALICE)
    check(*executed(res, "chart_generate"), "非库工具（图表）不受影响")


# ── ③ 规则顺序 ──────────────────────────────────────────────

def t3_order() -> None:
    section("③ 规则零压过通道硬闸（不泄露「该库已建模」）")
    res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_B}, ALICE)
    check("无权访问" in res["content"], "无权 + 已建模库 → 拿到的**是授权文案**")
    check("已在 Wren" not in res["content"],
          "**不是**通道硬闸文案 —— 越权者不该从错误里读出「这个库存在且已建模」")

    # 有权者在已建模库上直连 → 走的仍是通道硬闸（规则一没被破坏）
    res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_B}, BOB)
    check(res["ran"] == 0 and "已在 Wren" in res["content"],
          "有权者在已建模库上直连 → 仍由通道硬闸拦下（规则一完好）",
          res["content"][:80])


# ── ④ 管理员 ────────────────────────────────────────────────

def t4_admin() -> None:
    section("④ 管理员恒有权")
    res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_X}, ADMIN)
    check(*executed(res, "dbmcp_run_sql"), "admin 直连任意库 → 执行")
    res = run_case(f"wrenai_{DB_B}_run_sql", {"sql": "select 1"}, ADMIN)
    check(*executed(res, f"wrenai_{DB_B}_run_sql"), "admin 走语义层查任意库 → 执行")


# ── ⑤ fail-open 的边界（写清理由）────────────────────────────

def t5_bounds() -> None:
    section("⑤ 边界：注册表路径 / 标记兜底 / 取不到库名 / 内部调用")
    # ① 注册表路径（生产里"幻觉工具名 / 撤销授权前的旧名字"走的就是这条）：
    #    工具名**不在 payload 里**，只在运行期注册表里 → DynamicMCPToolsMiddleware
    #    用注册表实例 override 掉 request.tool，请求继续往下到 QueryGate。
    res = run_case(f"wrenai_{DB_B}_run_sql", {"sql": "select 1"}, ALICE, registry=True)
    ok, detail = denied(res, DB_B)
    check(ok, "**注册表路径**（工具名不在 payload、只在注册表里）→ 仍被拒", detail)

    res = run_case(f"wrenai_{DB_B}_run_sql", {"sql": "select 1"}, BOB, registry=True)
    check(*executed(res, f"wrenai_{DB_B}_run_sql"),
          "同一条注册表路径上有权者照常执行（证明拦截不是「凡注册表调用全拦」）")

    # ② 参数缺省 + state 标记：前端选了未授权库、模型不传参数 → 仍受约束
    marker = f"当前数据库: `{DB_X}` —— **未在语义层建模**。"
    res = run_case("dbmcp_run_sql", {"sql": "select 1"}, ALICE, state_msgs=[marker])
    ok, detail = denied(res, DB_X)
    check(ok, "参数缺省但 state 标记指向未授权库 → **拒**（前端选库 + 不传参也不能绕）",
          detail)

    # ③ 参数缺省且无标记：db_mcp_server 的 run_sql 会走 _get_runner("") 抛
    #    「db_name 不能为空」，**查不到任何数据** → 不是绕过口，放行给工具自己报错
    res = run_case("dbmcp_run_sql", {"sql": "select 1"}, ALICE)
    check(*executed(res, "dbmcp_run_sql"),
          "db_name 缺省且无标记 → 放行（接口自己会报参数缺失，捞不到数据）")

    # ④ 反查不到库名 → 放行（工作区没这个库）。create_agent 会在工具节点报
    #    "not a valid tool"（不是我们的文案）—— 断言"我们没拦"而非"执行了"。
    res = run_case("wrenai_ghost_run_sql", {"sql": "select 1"}, ALICE)
    check("无权访问" not in res["content"],
          "反查不到库名的 wrenai 名 → 我们**不**判（判权不能因取不到库名误杀）",
          res["content"][:60])

    # ⑤ 内部调用（子 agent / sync 的身份）→ 不启用判权
    res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_X}, "internal")
    check(*executed(res, "dbmcp_run_sql"), "内部调用（身份 internal）→ 不启用判权")
    res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_X}, None)
    check(*executed(res, "dbmcp_run_sql"),
          "configurable 里没有 user_id（离线/无身份）→ 不启用判权")


# ── ⑥ 破坏性负对照 ──────────────────────────────────────────

def t6_negative_control() -> None:
    section("⑥ 负对照（破坏性）：拆掉规则零 → 同一越权调用立刻执行")
    import agent.middlewares.query_gate as qg

    original = qg._target_db
    qg._target_db = lambda request, name: ""  # type: ignore[assignment]
    try:
        res = run_case("dbmcp_run_sql", {"sql": "select 1", "db_name": DB_X}, ALICE)
        check(res["ran"] == 1 and res["content"] == "ROWS:dbmcp_run_sql",
              "**负对照**：判不出库名时规则零整条让路 → 越权查询被执行（复刻修复前）",
              f"ran={res['ran']} content={res['content'][:60]!r}")
        check("无权访问" not in res["content"], "此时拿不到任何拒绝文案（不是恒真断言）")
    finally:
        qg._target_db = original  # type: ignore[assignment]


# ── ⑦ 挂载点（AST 级，不 import 整个图）──────────────────────

def t7_mounted() -> None:
    section("⑦ 挂载点：规则真的在 nl2sql 子 agent 的中间件链里")
    src = (_HERE.parents[1] / "src" / "agent" / "graphs" / "nl2sql_agent.py").read_text(
        encoding="utf-8"
    )
    found = False
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and getattr(node.args[0].func, "id", "") == "QueryGateMiddleware"
        ):
            found = True
    # 说明：这是**文本/AST 级**断言（不 import 该模块——它在 import 期就建模型、要连
    # provider）。只保证"接线没被摘掉"，行为由 ①~⑥ 的真图验证负责。
    check(found, "`_middleware.append(QueryGateMiddleware())` 仍在（子 agent 唯一数据库入口）")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    patch_mcp_loading()   # ⚠️ 必须在 seed() 之前，见该函数 docstring
    try:
        seed()

        t0_preconditions()
        t1_baseline()
        t2_deny()
        t3_order()
        t4_admin()
        t5_bounds()
        t6_negative_control()
        t7_mounted()
    finally:
        restore_mcp_loading()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
