# -*- coding: utf-8 -*-
"""标注页「执行并验证 SQL」的语义层通道验证（离线：不连目标库 / 不打 Langfuse / 只写临时 AGENT_DATA_ROOT）。

**被测的坑**（用户报障，2026-10-16）：标注队列里点「执行修正并验证 SQL」报
``执行失败: (1146, "Table 'witops.v_workhour' doesn't exist")``。

根因不是 SQL 写错，而是**通道选错**：``execute_annotation`` → ``_run_preview`` 把人工 SQL
原样交给 dbmcp 直连引擎（pymysql 直连物理 MySQL），而 ``v_workhour`` 是 MDL 里的**虚拟
视图**（``statement`` 定义，底座 ``do_work_hour_detail_extend``）——物理库中根本没有这个
对象（实测 ``SHOW FULL TABLES LIKE 'v_workhour'`` 空集）。而走语义层的模型下发的恰恰是
这类 SQL（``message_feedback._extract_sql`` 取的就是 ``tool_calls[].args.sql``，那正是
语义层语句），标注员多半原样沿用。同文件的「按口径试算」一直是先编译后执行
（``plan_cube_sql_checked`` → 执行 ``dialect_sql``），人工 SQL 这条路缺的正是同一刀。

修法 = ``_to_physical_sql``：已建模库先经 Wren ``dry_plan`` 展开再执行，展不开就回落直连
（＝改动前行为），落库的 gold/bad SQL **仍是人工那条语义层 SQL**（回归集要跟模型产出的
语义 SQL 逐字比对，换成物理 SQL 就没得比了）。

**怎么验的**：起**真** Starlette app（整份 ``feedback_annotation.routes`` + ``AuthMiddleware``
+ 真 token），只打两个端点，断言「到底把哪条 SQL 交给了执行器」。三处打桩，全是"外部世界"：
① ``resolve_wren_ctx_by_db``（指向本脚本的临时 wren 项目，免得依赖工作区里恰好有哪些库）；
② ``_run_preview``（本机没有那台 MySQL，且判据本来就是"交给执行器的 SQL 是什么"）；
③ trace 定位 / Langfuse。**Wren 展开是真跑的**（真 ``dry_plan``、真 ``target/mdl.json``）
—— 展开后的语句是引擎产出的，不是断言里写死的。

**未覆盖**：真正的库往返不在本脚本（展开后的语句能否在 MySQL 上跑，由 ``wren_plan`` 与
``wren dry-plan`` 的逐字节对拍负责，见该模块头注释）。

验十组：
  ⓪ 前置：wren 引擎可用（否则本脚本无判别力）+ 夹具判据是判别性的。
  ① 正例（真事故形态）：语义层视图 SQL ⇒ 交给执行器的是**展开后**的物理 SQL。
  ② 人工自带的 LIMIT/OFFSET 原样保留，**不叠**连接器那条 LIMIT（否则双 LIMIT 语法错）。
  ③ 已建模但写的是物理表（MDL 里没有）⇒ 原样回落直连 + 记下展不开的原因。
  ④ 未建模库 ⇒ 原样（零影响），且**不**留下"展不开"的假原因（否则报错文案会撒谎）。
  ⑤ 失败文案：前缀逐字不变；只有"已建模 + 展不开"才追加语义层提示。
  ⑥ confirm 端点（金标校验）同通道；落库 gold_sql 仍是语义层那条。
  ⑦ 落库纪律：execute 后 ``bad_sql`` = 人工那条（不是展开后的），状态 → validated。
  ⑧ 加法纪律：``_finish`` 既有键逐字不变 + 新键 ``plan_sql``；``dialect_sql`` 仍 = ``plan_sql`` + LIMIT。
  ⑨ fail-open 未被破坏：``plan_run_sql`` 展不开仍返回 ``{}``（check_progress 在用）；
     ``plan_run_sql_checked`` 抛 ``RunSqlPlanError`` 且带引擎原话。
  ⑩ 接线闸（AST）：两个端点都接了 ``_to_physical_sql`` 且结果喂给 ``_run_preview``；
     ``preview_cube_annotation`` 仍走自己的 ``dialect_sql``；``bad_sql=sql`` 未被改写。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_annotation_sql_channel.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import store / auth 之前设置：sqlite 与 auth secret 都落在 <AGENT_DATA_ROOT> 下
os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-annsql-"))
os.environ.setdefault("NL2SQL_AUTH_DISABLED", "0")

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import httpx  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

MODELED = "witops"        # 已建模（假解析指向本脚本的临时项目）
UNMODELED = "plain_db"    # 未建模（→ ("", {})）

ADMIN = "Z9001"
NGINX_IP = "172.18.0.9"   # 容器网段（真实浏览器经 nginx 后的来源）

results: list[tuple[bool, str]] = []

# ── 夹具：最小 MDL（一个**虚拟视图** + 它的底座模型）──────────────────────
# 形态逐字取自 witops 生产库：``v_workhour`` 就是 ``views[]`` 里带 ``statement`` 的那种，
# 底座是物理表（``do_work_hour_detail_extend``）。这里名字换成中性词，结构与之一致。
MDL = {
    "catalog": "wren",
    "schema": "public",
    "models": [
        {
            "name": "t_base",
            "tableReference": {"table": "t_base"},
            "columns": [
                {"name": "id", "type": "INTEGER", "isCalculated": False, "notNull": True, "isPrimaryKey": True},
                {"name": "deleted", "type": "INTEGER", "isCalculated": False, "notNull": False, "isPrimaryKey": False},
                {"name": "dept", "type": "VARCHAR", "isCalculated": False, "notNull": False, "isPrimaryKey": False},
            ],
            "cached": False,
            "primaryKey": "id",
            "properties": {"description": "底座表（物理存在）"},
        }
    ],
    "relationships": [],
    "views": [
        {
            "name": "v_probe",
            "dialect": "mysql",
            "properties": {"description": "已过滤 deleted=0 的探针视图"},
            "statement": "SELECT id, dept FROM t_base WHERE deleted = 0",
        }
    ],
    "cubes": [],
    "dataSource": "mysql",
    "layoutVersion": 3,
}

PROJ = pathlib.Path(os.environ["AGENT_DATA_ROOT"]) / "_proj_annsql"
CONN = {"datasource": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "probe", "user": "u", "password": "p"}

# 夹具 SQL：与报障那句同形（引用 MDL 视图 + 聚合），只是对象名换成夹具的
SEMANTIC_SQL = "SELECT dept, COUNT(*) AS cnt FROM v_probe GROUP BY dept"
SEMANTIC_LIMIT_SQL = "SELECT dept FROM v_probe LIMIT 7"
SEMANTIC_LIMIT_OFFSET_SQL = "SELECT dept FROM v_probe LIMIT 7 OFFSET 3"
# 物理库里存在、但 MDL 里没有：dry_plan 要求引用的对象都在 MDL 里 ⇒ 展不开 ⇒ 回落直连
PHYS_ONLY_SQL = "SELECT id, dept FROM raw_phys_table"
MYSQL_1146 = Exception('(1146, "Table \'witops.v_probe\' doesn\'t exist")')


def seed_project() -> None:
    (PROJ / "target").mkdir(parents=True, exist_ok=True)
    (PROJ / "target" / "mdl.json").write_text(
        json.dumps(MDL, ensure_ascii=False), encoding="utf-8"
    )


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 桩：外部世界 ───────────────────────────────────────────────

_ORIG: dict = {}


def patch_wren_ctx() -> None:
    """``resolve_wren_ctx_by_db`` → 临时项目 / 未建模两档（**唯一**被换掉的解析入口）。"""
    import agent.utils.wren_call_extract as wce

    _ORIG["wce"] = wce
    _ORIG["wce_resolve"] = wce.resolve_wren_ctx_by_db

    def fake(db_name: str) -> tuple:
        return (str(PROJ), CONN) if str(db_name or "").strip() == MODELED else ("", {})

    wce.resolve_wren_ctx_by_db = fake


class ExecStub:
    """``_run_preview`` 替身：记录交给执行器的 SQL，可切换成抛错。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raise_with: Exception | None = None

    async def __call__(self, db_name: str, sql: str, limit: int = 100) -> dict:
        self.calls.append((db_name, sql))
        if self.raise_with is not None:
            raise self.raise_with
        return {"columns": ["a"], "rows": [{"a": 1}], "row_count": 1}

    def last(self) -> str:
        return self.calls[-1][1] if self.calls else ""


def restore() -> None:
    if "wce" in _ORIG:
        _ORIG["wce"].resolve_wren_ctx_by_db = _ORIG["wce_resolve"]
    if "preview" in _ORIG:
        import api.feedback_annotation as fa

        fa._run_preview = _ORIG["preview"]


# ── app / 请求 ─────────────────────────────────────────────────

def mint(uid: str, is_admin: bool = False) -> str:
    from agent.auth import grants

    grants.register_user = lambda *a, **k: None  # 中间件放行后会调它，打桩掉磁盘写
    from _auth_test_support import mint_for

    return mint_for(uid, is_admin)


def build_app():
    from starlette.applications import Starlette

    from api import feedback_annotation
    from api.auth_middleware import AuthMiddleware

    return AuthMiddleware(Starlette(routes=list(feedback_annotation.routes)))


async def call(app, path: str, body: dict, *, uid: str = ADMIN) -> tuple:
    headers = {"X-Forwarded-For": "1.2.3.4"}
    if uid:
        headers["Cookie"] = f"nl2sql_token={mint(uid, uid == ADMIN)}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(NGINX_IP, 12345)),
        base_url="http://testserver",
    ) as c:
        r = await c.request("POST", path, headers=headers, json=body)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, None


def exec_path(tid: str, mid: str) -> str:
    return f"/api/feedback/annotations/{tid}/{mid}/execute"


def confirm_path(tid: str, mid: str) -> str:
    return f"/api/feedback/annotations/{tid}/{mid}/confirm"


def seed_row(tid: str, mid: str) -> None:
    from agent.feedback.store import get_store

    st = get_store()
    st.enqueue_annotation(tid, mid, rating="negative", note="seed", question="种子问题")


# ── ⓪ 前置 ────────────────────────────────────────────────────

def t0_preconditions() -> None:
    section("⓪ 前置：wren 引擎可用 + 夹具判据是判别性的")
    from agent.utils.wren_plan import probe_wren_api

    ok_api = probe_wren_api()
    check(ok_api, "wren 引擎可用（不可用则本脚本整篇无判别力，必须红）")
    if not ok_api:
        return
    from agent.utils.wren_plan import RunSqlPlanError, plan_run_sql_checked

    plan = plan_run_sql_checked(str(PROJ), CONN, SEMANTIC_SQL)
    # 判据来自 wren 侧既有结论（见 wren-semicolon-comment 那条记忆）：展开成功必以 WITH 开头
    check(str(plan.get("plan_sql") or "").startswith("WITH"),
          "语义层视图 SQL 展开成 CTE（输出以 `WITH` 开头，说明真过了 MDL）")
    check("v_probe AS (" in str(plan.get("plan_sql") or ""),
          "展开产物把 MDL 视图物化成同名 CTE")
    check("t_base" in str(plan.get("plan_sql") or ""),
          "底座物理表出现在展开产物里（这就是物理库真正认得的名字）")
    # 负对照：物理库不认识的对象
    try:
        plan_run_sql_checked(str(PROJ), CONN, PHYS_ONLY_SQL)
        check(False, "MDL 里没有的表应展不开（负对照）", "竟然展成了")
    except RunSqlPlanError as e:
        check("not found" in str(e), "MDL 里没有的表 ⇒ 展不开且带引擎原话", str(e)[:90])


# ── ① 正例 ────────────────────────────────────────────────────

async def t1_expand(app, ex: ExecStub) -> None:
    section("① 正例（真事故形态）：语义层视图 SQL ⇒ 执行器拿到的是展开后的物理 SQL")
    tid, mid = "a1111111", "m1"
    seed_row(tid, mid)
    code, body = await call(app, exec_path(tid, mid), {"sql": SEMANTIC_SQL, "db_name": MODELED})
    check(code == 200 and body and body.get("ok"), "端点 200 + ok", f"code={code} body={str(body)[:120]}")
    check(ex.last().startswith("WITH"), "**交给执行器的语句是展开产物**（不是 v_probe 直连）",
          ex.last()[:80].replace("\n", " "))
    check(body.get("expanded") is True, "响应带 expanded=True（前端可提示「已按语义层展开」）")
    check(body.get("executed_sql") == ex.last(), "executed_sql 与实际执行的那条逐字一致")


async def t2_limits(app, ex: ExecStub) -> None:
    section("② 人工自带的 LIMIT/OFFSET 原样保留，不叠连接器那条 LIMIT")
    for tid, mid, sql, tail in (
        ("b2222222", "m2", SEMANTIC_LIMIT_SQL, "LIMIT 7"),
        ("b3333333", "m3", SEMANTIC_LIMIT_OFFSET_SQL, "LIMIT 7 OFFSET 3"),
    ):
        seed_row(tid, mid)
        code, _body = await call(app, exec_path(tid, mid), {"sql": sql, "db_name": MODELED})
        got = ex.last()
        check(code == 200 and got.startswith("WITH"), f"{sql!r} ⇒ 仍走展开", f"code={code}")
        check(got.rstrip().endswith(tail), "尾部 LIMIT/OFFSET 逐字保留", repr(got[-40:]))
        check("LIMIT 1001" not in got,
              "**没有**镜像连接器那条 LIMIT（有它就会与自带 LIMIT / OFFSET 叠成 MySQL 语法错）")


async def t3_phys_fallback(app, ex: ExecStub) -> None:
    section("③ 已建模但写的是物理表（MDL 里没有）⇒ 原样回落直连 + 记下原因")
    from api import feedback_annotation as fa

    _sql, meta = await asyncio.to_thread(fa._to_physical_sql, MODELED, PHYS_ONLY_SQL)
    check(_sql == PHYS_ONLY_SQL, "展不开 ⇒ **原样**返回（改动前行为不变）")
    check(not meta.get("expanded") and meta.get("expand_error"),
          "expanded=False 且 expand_error 非空", str(meta.get("expand_error"))[:90])

    tid, mid = "c4444444", "m4"
    seed_row(tid, mid)
    code, body = await call(app, exec_path(tid, mid), {"sql": PHYS_ONLY_SQL, "db_name": MODELED})
    check(code == 200 and ex.last() == PHYS_ONLY_SQL,
          "端点仍 200：物理 SQL 走直连通道照常执行（本次修的是语义层那条，不是拦人）",
          f"code={code}")
    check(body.get("expanded") is False and not body.get("executed_sql"),
          "未改写就不回 executed_sql（不塞空壳）")


async def t4_unmodeled(app, ex: ExecStub) -> None:
    section("④ 未建模库 ⇒ 原样（零影响），且不留下「展不开」的假原因")
    from api import feedback_annotation as fa

    _sql, meta = await asyncio.to_thread(fa._to_physical_sql, UNMODELED, SEMANTIC_SQL)
    check(_sql == SEMANTIC_SQL and meta == {"expanded": False, "expand_error": ""},
          "未建模 ⇒ 原样 + expand_error 为空", str(meta))
    tid, mid = "d5555555", "m5"
    seed_row(tid, mid)
    code, _ = await call(app, exec_path(tid, mid), {"sql": SEMANTIC_SQL, "db_name": UNMODELED})
    check(code == 200 and ex.last() == SEMANTIC_SQL, "端点把原句交给执行器", f"code={code}")


async def t5_error_text(app, ex: ExecStub) -> None:
    section("⑤ 失败文案：前缀逐字不变；只有「已建模 + 展不开」才追加语义层提示")
    from api.feedback_annotation import _exec_error_text

    plain = _exec_error_text(MYSQL_1146, {"expanded": False, "expand_error": ""})
    check(plain == f"执行失败: {MYSQL_1146}",
          "未建模/已展开的失败 ⇒ 文案与改动前**逐字相同**（线上既有判据不受影响）", repr(plain[:60]))
    hinted = _exec_error_text(MYSQL_1146, {"expanded": False, "expand_error": "not found"})
    check(hinted.startswith(f"执行失败: {MYSQL_1146}") and "已建模" in hinted and "not found" in hinted,
          "已建模 + 展不开 ⇒ 原文 + 语义层提示 + 引擎原话", repr(hinted[:90]))
    gold = _exec_error_text(MYSQL_1146, {"expanded": False, "expand_error": ""},
                            prefix="金标 SQL 执行失败（请先修正）")
    check(gold == f"金标 SQL 执行失败（请先修正）: {MYSQL_1146}",
          "金标路的前缀逐字不变")

    tid, mid = "e6666666", "m6"
    seed_row(tid, mid)
    ex.raise_with = MYSQL_1146
    try:
        code, body = await call(app, exec_path(tid, mid), {"sql": PHYS_ONLY_SQL, "db_name": MODELED})
    finally:
        ex.raise_with = None
    check(code == 400 and "已建模" in (body or {}).get("error", ""),
          "执行失败 ⇒ 400 + 带语义层提示", f"code={code} err={str((body or {}).get('error'))[:80]}")
    from agent.feedback.store import get_store

    rec = get_store().get_annotation(tid, mid)
    check(rec is not None and rec.exec_error.startswith('(1146'),
          "exec_error 落库仍是**原始引擎报错**（文案提示只在响应里，不污染机读字段）",
          repr((rec.exec_error if rec else "")[:40]))


# ── ⑥ confirm 端点 ────────────────────────────────────────────

async def t6_confirm(app, ex: ExecStub) -> None:
    section("⑥ confirm 端点（金标校验）同通道；落库 gold_sql 仍是语义层那条")
    import agent.trace.langfuse_client as lc
    import api.message_feedback as mf

    _ORIG["lf"] = lc.langfuse_enabled
    _ORIG["find_trace"] = mf._find_trace_with_retry
    lc.langfuse_enabled = lambda: False          # Dataset 写入是旁路，关掉免得打网络
    mf._find_trace_with_retry = lambda *a, **k: "tracetest01"

    tid, mid = "f7777777", "m7"
    seed_row(tid, mid)
    try:
        code, body = await call(app, confirm_path(tid, mid),
                                {"gold_sql": SEMANTIC_SQL, "bad_type": "agg_logic_error",
                                 "db_name": MODELED})
    finally:
        lc.langfuse_enabled = _ORIG.pop("lf")
        mf._find_trace_with_retry = _ORIG.pop("find_trace")
    check(code == 200 and body and body.get("ok"), "确认入集 200", f"code={code} body={str(body)[:100]}")
    check(ex.last().startswith("WITH"), "金标校验执行的也是展开后的物理 SQL",
          ex.last()[:70].replace("\n", " "))
    from agent.feedback.store import get_store

    rec = get_store().get_annotation(tid, mid)
    check(rec is not None and rec.gold_sql == SEMANTIC_SQL,
          "**落库的 gold_sql 仍是语义层原句**（回归集要跟模型的语义 SQL 逐字比对）",
          repr((rec.gold_sql if rec else "")[:50]))
    check(rec is not None and rec.gold_result != "", "gold_result 由那次执行填出（结果来自展开后的语句）")


# ── ⑦ 落库纪律 ────────────────────────────────────────────────

async def t7_store(app, ex: ExecStub) -> None:
    section("⑦ 落库纪律：execute 后 bad_sql = 人工那条，状态 → validated")
    from agent.feedback.store import get_store

    tid, mid = "a8888888", "m8"
    seed_row(tid, mid)
    code, _ = await call(app, exec_path(tid, mid), {"sql": SEMANTIC_SQL, "db_name": MODELED})
    rec = get_store().get_annotation(tid, mid)
    check(code == 200 and rec is not None and rec.bad_sql == SEMANTIC_SQL,
          "bad_sql 存的是标注员写的那条（不是展开后的物理 SQL）",
          repr((rec.bad_sql if rec else "")[:50]))
    check(rec is not None and rec.status == "validated", "状态推进到 validated", str(rec.status if rec else None))
    check(rec is not None and rec.exec_error == "", "exec_error 已清空（成功）")


# ── ⑧⑨ wren_plan 侧 ──────────────────────────────────────────

def t8_additive() -> None:
    section("⑧ 加法纪律：_finish 既有键逐字不变 + 新键 plan_sql；dialect_sql 仍 = plan_sql + LIMIT")
    from agent.utils.wren_plan import _finish, _mdl_path

    plan = _finish(_mdl_path(str(PROJ)), CONN, SEMANTIC_SQL)
    check(sorted(plan) == sorted(["cube_sql", "plan_sql", "dialect_sql", "dialect",
                                  "limit_appended", "dup_limit"]),
          "键集合 = 既有五键 + plan_sql（没有多也没有少）", str(sorted(plan)))
    check(plan["dialect"] == "mysql" and plan["limit_appended"] == 1001,
          "既有键取值不变（dialect / limit_appended）",
          f"{plan['dialect']} / {plan['limit_appended']}")
    check(plan["dialect_sql"] == f"{plan['plan_sql'].rstrip().rstrip(';').rstrip()}\nLIMIT 1001",
          "dialect_sql 仍是「plan_sql + 镜像连接器 LIMIT」（口径试算/报告读的就是它）")
    check(plan["dup_limit"] is False and _finish(_mdl_path(str(PROJ)), CONN, SEMANTIC_LIMIT_SQL)["dup_limit"] is True,
          "dup_limit 判据仍按 plan_sql 尾部判别（自带 LIMIT ⇒ True）")


def t9_fail_open() -> None:
    section("⑨ fail-open 未被破坏：plan_run_sql 仍返回 {}、checked 版才抛")
    from agent.utils.wren_plan import RunSqlPlanError, plan_run_sql, plan_run_sql_checked

    check(plan_run_sql(str(PROJ), CONN, PHYS_ONLY_SQL) == {},
          "展不开时 fail-open 仍是 {}（check_progress 等既有调用方的契约）")
    check(plan_run_sql(str(PROJ), CONN, "") == {} and plan_run_sql(None, None, SEMANTIC_SQL) == {},
          "空 SQL / 空项目 ⇒ {}（不抛）")
    try:
        plan_run_sql_checked(str(PROJ), CONN, PHYS_ONLY_SQL)
        check(False, "checked 版应抛 RunSqlPlanError", "没抛")
    except RunSqlPlanError as e:
        check("not found" in str(e), "checked 版抛 RunSqlPlanError 且带引擎原话", str(e)[:80])


# ── ⑩ 接线闸 ──────────────────────────────────────────────────

def _func_node(src: str, name: str):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def t10_wiring() -> None:
    section("⑩ 接线闸（AST）：两个端点接了展开、cube 路未被波及、bad_sql 未被改写")
    src = (_HERE.parents[1] / "src" / "api" / "feedback_annotation.py").read_text(encoding="utf-8")

    for fname in ("execute_annotation", "confirm_annotation"):
        fn = _func_node(src, fname)
        if fn is None:
            check(False, f"{fname} 找得到", "AST 里没有")
            continue
        # `exec_sql, expand = await asyncio.to_thread(_to_physical_sql, db_name, sql)`
        # ⇒ 元组解包的首个名字就是「要执行的 SQL」
        names = set()
        for n in ast.walk(fn):
            if not isinstance(n, ast.Assign):
                continue
            v = n.value
            if isinstance(v, ast.Await):  # `exec_sql, expand = await asyncio.to_thread(…)`
                v = v.value
            if not isinstance(v, ast.Call) or getattr(v.func, "attr", "") != "to_thread" or not v.args:
                continue
            if getattr(v.args[0], "id", "") != "_to_physical_sql":
                continue
            tgt = n.targets[0]
            first = tgt.elts[0] if isinstance(tgt, ast.Tuple) and tgt.elts else tgt
            if isinstance(first, ast.Name):
                names.add(first.id)
        check(bool(names), f"{fname} 调了 `_to_physical_sql`（放线程里）", str(sorted(names)))
        fed = set()
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_run_preview"
                    and len(n.args) >= 2):
                fed.add(getattr(n.args[1], "id", ""))
        check(bool(names) and bool(fed) and fed <= names,
              f"{fname} 把展开结果喂给 `_run_preview`（不是原始 sql）", f"喂进去的是 {sorted(fed)}")

    # 口径试算仍走自己的 dialect_sql（不该被本改动波及）
    cube = _func_node(src, "preview_cube_annotation")
    cube_src = ast.get_source_segment(src, cube) if cube else ""
    check(cube is not None and '"dialect_sql"' in cube_src and "_run_preview(db_name, physical_sql)" in cube_src,
          "`preview_cube_annotation` 仍用 `dialect_sql` 那条（口径试算的行数口径不变）")

    # 落库的仍是人工那条
    ex = _func_node(src, "execute_annotation")
    ex_src = ast.get_source_segment(src, ex) if ex else ""
    check("bad_sql=sql[:MAX_SNAPSHOT_SQL]" in ex_src,
          "`execute_annotation` 落库仍是 `bad_sql=sql`（人工那条，未被展开产物顶替）")

    # Good Set 路不执行 SQL：不该出现 _run_preview
    good = _func_node(src, "_confirm_good_commit")
    check(good is not None and "_run_preview" not in ast.get_source_segment(src, good),
          "`_confirm_good_commit` 不执行 SQL（点赞入集不经本通道，无需接展开）")

    # check_progress 仍用 fail-open 那支（改名/改语义会在这里红）
    cps = (_HERE.parents[1] / "src" / "agent" / "subagents" / "check_progress.py").read_text(encoding="utf-8")
    check(any(isinstance(n, ast.ImportFrom) and any(a.name == "plan_run_sql" for a in n.names)
              for n in ast.walk(ast.parse(cps))),
          "`check_progress` 仍 import fail-open 的 `plan_run_sql`")


# ── 主流程 ────────────────────────────────────────────────────

async def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    seed_project()
    import api.feedback_annotation as fa

    ex = ExecStub()
    _ORIG["preview"] = fa._run_preview
    _ORIG["wce_resolve"] = None
    patch_wren_ctx()
    fa._run_preview = ex
    app = build_app()
    try:
        t0_preconditions()
        await t1_expand(app, ex)
        await t2_limits(app, ex)
        await t3_phys_fallback(app, ex)
        await t4_unmodeled(app, ex)
        await t5_error_text(app, ex)
        await t6_confirm(app, ex)
        await t7_store(app, ex)
        t8_additive()
        t9_fail_open()
        t10_wiring()
    finally:
        restore()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
