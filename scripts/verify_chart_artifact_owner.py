# -*- coding: utf-8 -*-
"""P1-17 图表产物归属验证（离线，无需后端/数据库/网络）。

**要关的口子**：`chart-saver` skill 用 `execute` 调 `save_chart.py` 落盘 `.svg/.png`，
文件进的是**全站共享**的 `report/` 目录，而这条路没有归属记录（子进程拿不到请求身份）。
`can_read_report` 对"无记录"是放行（存量兼容），于是任何登录用户知道文件名就能下载。
P1-3 给 `build_report` 的 `.md/.html` 打了归属，本项补上同一目录里 skill 产出的图。

**为什么在中间件里做**：工具结果回到中间件时还有身份（`configurable.user_id`），
再往里（shell 子进程）就没有了。所以判据是「`execute` 的结果里出现 `✅ 图表已保存: <路径>`」。

⚠️ **清单里写的那条验收是恒真的**（复现见 ⑤）：原文是「两个用户各出一个同名图表，
彼此在 `GET /api/reports` 列表里看不到对方那份」—— 但 `list_reports` 在判权**之前**就把
`.svg/.png` 过滤掉了（只列 `.md/.html/.csv/.json`），所以这条断言在修复前后都通过，
它测的是后缀过滤、不是归属。本脚本改用**能区分的那条**：直接 `GET /api/reports/<图名>?download=1`
（修复前他人 200，修复后 404）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_chart_artifact_owner.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import contextlib
import os
import pathlib
import subprocess
import sys
import tempfile

# ⚠️ 必须在 import grants/token/workspace_manager 之前：它们的落点都由 AGENT_DATA_ROOT 推导
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-chart-owner-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
_SRC = _HERE.parents[1] / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

# ⚠️ 预热 `agent.tools`（必须在 asyncio.run 之外）：它**在 import 期**就去连 MCP 子进程
# （`load_*_tools` 的模块级副作用）。若这一步发生在事件循环里，`_load_mcp_servers` 的
# `asyncio.new_event_loop()` 会对每个 server 抛「Cannot run the event loop while another
# loop is running」，再各退避重试 3 秒 —— 实测把输出淹掉并把脚本拖慢 40 秒。
import agent.tools.mcp_tool  # noqa: E402,F401

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
NGINX_IP = "172.18.0.9"

ALICE = "Z0051"          # 出图的人
BOB = "Z9999"            # 想白嫖的人
ADMIN = "Z0001"
TID_ALICE = "thread-alice-1"

SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>月度销售趋势</text></svg>'
SAVE_CHART = _SRC / "agent/shared/skills/main/chart-saver/scripts/save_chart.py"

results: list[tuple[bool, str]] = []
_NOPATCHED: dict = {}


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 工作区播种（真 WorkspaceManager，只是搬到临时 AGENT_DATA_ROOT）────────

def patch_mcp_loading() -> None:
    """把「起 MCP 子进程」换成桩 —— 切工作区会触发 reload_tools，本项不测 MCP。"""
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


def seed_workspace() -> pathlib.Path:
    """返回工作区的 report 目录（真目录、真判定）。

    2026-09-25：工作区已钉死为 `<AGENT_DATA_ROOT>/workspace`（多工作区注册/切换机件
    删除），所以这里不再"注册 + 激活一个临时工作区"，只把自己钉的
    `AGENT_DATA_ROOT` 之下的那份工作区拿回来用 —— 本脚本在 main() 里把
    `AGENT_DATA_ROOT` 指向临时目录，因而仍是隔离的临时目录。
    """
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    # 与 manager 同一种构造方式比较（避免 `a/b.resolve()` vs `a.resolve()/b` 的差异）
    expected = (pathlib.Path(os.environ["AGENT_DATA_ROOT"]) / "workspace").resolve()
    assert wm.active_workspace == expected, (
        f"工作区未钉在 AGENT_DATA_ROOT/workspace：{wm.active_workspace} != {expected}"
    )
    report_dir = wm.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir


REPORT_DIR: pathlib.Path = pathlib.Path()   # main() 里赋值


# ── 合成请求/身份基础设施 ────────────────────────────────────────────────

def mint(uid: str, is_admin: bool = False) -> str:
    """先登记身份再签（`verify_token` 以用户记录为唯一真源），见 `_auth_test_support.py`。"""
    from agent.auth import grants

    grants.register_user = lambda *a, **k: None  # 中间件放行后会调它，打桩掉磁盘写
    from _auth_test_support import mint_for

    return mint_for(uid, is_admin)


@contextlib.contextmanager
def as_identity(uid: str, tid: str = ""):
    """让 `configurable` 带身份 —— 中间件的 uid/thread_id 都从 `langgraph.config.get_config()` 取。

    打的是**模块属性**：`auth.runtime.caller_identity` 与中间件内部都是
    `from langgraph.config import get_config`（调用时才取属性），所以这一处打桩两条链路都覆盖。
    """
    import langgraph.config as lgc

    original = lgc.get_config
    lgc.get_config = lambda: {"configurable": {"user_id": uid, "thread_id": tid}}  # type: ignore[assignment]
    try:
        yield
    finally:
        lgc.get_config = original  # type: ignore[assignment]


@contextlib.contextmanager
def as_no_identity():
    """完全没有 config 上下文（内部调用/离线）—— 取身份应抛异常并被兜住。"""
    import langgraph.config as lgc

    original = lgc.get_config

    def _boom():
        raise RuntimeError("no langgraph config context")

    lgc.get_config = _boom  # type: ignore[assignment]
    try:
        yield
    finally:
        lgc.get_config = original  # type: ignore[assignment]


def make_request(tool_name: str):
    """真 `ToolCallRequest` 数据类（langchain 1.3.14 是 @dataclass，不是 TypedDict）。"""
    from langchain.agents.middleware.types import ToolCallRequest

    return ToolCallRequest(
        tool_call={"name": tool_name, "args": {"command": "python save_chart.py ..."}, "id": "tc-1"},
        tool=None,
        state={},
        runtime=None,
    )


def run_execute(content: str, *, tool_name: str = "execute", with_middleware: bool = True):
    """把一段工具结果喂进真中间件链，返回中间件交给上层的 result。"""
    from langchain_core.messages import ToolMessage

    from agent.middlewares.chart_artifact_owner import ChartArtifactOwnerMiddleware

    req = make_request(tool_name)
    result = ToolMessage(content=content, tool_call_id="tc-1", name=tool_name)
    if not with_middleware:
        return result  # 负对照：中间件不存在（= 修复前的世界）
    return ChartArtifactOwnerMiddleware().wrap_tool_call(req, lambda r: result)


def save_chart_via_subprocess(name: str) -> str:
    """**真跑** save_chart.py（子进程）→ 返回它落盘的宿主绝对路径。

    用子进程而不是 import 调：本项要验的就是**stdout 契约**（`✅ 图表已保存: <路径>`），
    而契约的产出方是 `main()`；import 只会拿到返回值，测不到打印那句。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(SAVE_CHART), "--content", SVG, "--name", name],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if proc.returncode != 0:
        check(False, f"save_chart.py 子进程执行成功（{name}）", (proc.stderr or "")[-300:])
        return ""
    line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    if not line.startswith("✅ 图表已保存: "):
        check(False, f"save_chart.py 打印契约 `✅ 图表已保存: <路径>`（{name}）", line)
        return ""
    return line[len("✅ 图表已保存: "):].strip()


# ── ① 提取契约 ────────────────────────────────────────────────────────

def t1_extract() -> None:
    section("① 提取契约：真脚本 stdout → 文件名")
    from agent.middlewares.chart_artifact_owner import saved_chart_names

    # 真脚本 + 真 stdout（契约的产出方）
    dest = save_chart_via_subprocess("月度销售趋势")
    check(bool(dest), "save_chart.py 真实落盘并打印宿主绝对路径", dest)
    real_name = pathlib.Path(dest).name
    check(saved_chart_names(f"✅ 图表已保存: {dest}") == [real_name],
          "宿主绝对路径 → 取到基名", real_name)
    check(real_name.startswith("月度销售趋势_") and real_name.endswith(".svg"),
          "文件名形态 = {基名}_{时间戳}_{随机}.svg", real_name)

    # POSIX 斜杠形态（脚本在非 Windows 宿主 / 回退打 VFS 路径时）
    posix = str(REPORT_DIR).replace("\\", "/") + "/" + real_name
    check(saved_chart_names(f"✅ 图表已保存: {posix}") == [real_name], "正斜杠形态同样取到基名")
    check(saved_chart_names(f"✅ 图表已保存:{posix}") == [real_name], "冒号后无空格也认")

    # 装饰字符（命令行回显 / 反引号包裹 / 中文标点尾巴）
    check(saved_chart_names(f"✅ 图表已保存: `{dest}`）") == [real_name],
          "反引号 + 尾随全角括号被剥掉")
    check(saved_chart_names(f"✅ 图表已保存: {dest},") == [real_name], "尾随半角逗号被剥掉")
    check(saved_chart_names(f"✅ 图表已保存: {dest}\"") == [real_name], "尾随引号被剥掉")
    check(saved_chart_names(f"✅ 图表已保存: \"{dest}\"") == [],
          "**前导**引号：取不到（正则的字符类就把引号排除在外）→ 不登记，宁漏不错")

    # 一次结果里多张图 + 重复
    two = save_chart_via_subprocess("双图A")
    two_name = pathlib.Path(two).name if two else ""
    if two_name:
        text = f"✅ 图表已保存: {dest}\n✅ 图表已保存: {two}\n✅ 图表已保存: {two}"
        check(saved_chart_names(text) == [real_name, two_name],
              "多张图按出现顺序取全、重复只留一次", str(saved_chart_names(text)))

    # 无标记 → 不登记（cat/ls/read 别人的图不该产生任何登记）
    check(saved_chart_names(f"-rw-r--r--  1 u u 1234 {dest}\n") == [],
          "目录列表（无标记）→ 不登记")
    check(saved_chart_names("保存图表失败: 未找到 <svg> 标签") == [], "失败信息 → 不登记")
    check(saved_chart_names("") == [], "空结果 → 不登记")


# ── ② 只登记 report/ 里真实存在的那一份 ──────────────────────────────

def t2_gates() -> None:
    section("② 收敛：只认「在 report/ 里且真实存在」的文件")
    from agent.middlewares.chart_artifact_owner import saved_chart_names

    # ① 存在性：report/ 里没有这个名字 → 不登记
    ghost = REPORT_DIR / "ghost_chart_deadbeef.svg"
    check(saved_chart_names(f"✅ 图表已保存: {ghost}") == [], "文件不存在 → 不登记")

    # ② 同目录约束：report/ 里有 `x.svg`，但标记指的是 tmp/ 里那份 → 不能认领
    #    （`record_report_owner` 只认基名，认了就等于把 report/ 里同名的别人的图划给自己）
    rel = "shared_name_both_places.svg"
    (REPORT_DIR / rel).write_text(SVG, encoding="utf-8")
    other = REPORT_DIR.parent / "tmp"
    other.mkdir(parents=True, exist_ok=True)
    (other / rel).write_text(SVG, encoding="utf-8")
    check(saved_chart_names(f"✅ 图表已保存: {other / rel}") == [],
          "路径不在 report/（--dir 落别处）→ 不登记，即使 report/ 里有同名文件")
    check(saved_chart_names(f"✅ 图表已保存: {REPORT_DIR / rel}") == [rel],
          "对照：同样是这个名字，路径确实在 report/ 里 → 登记（证明②不是靠文件不存在挡的）")

    # ③ report 的子目录：GET 侧 `_SAFE_FNAME` 不允许分隔符，这种文件根本读不到 → 不需要归属
    sub = REPORT_DIR / "sub"
    sub.mkdir(exist_ok=True)
    (sub / "nested.svg").write_text(SVG, encoding="utf-8")
    check(saved_chart_names(f"✅ 图表已保存: {sub / 'nested.svg'}") == [], "report/ 子目录 → 不登记")

    # ④ 解析不了（工作区管理器异常）→ 不登记而不是炸
    import agent.workspace_manager as wm_mod

    original = wm_mod.get_workspace_manager

    def _boom():
        raise RuntimeError("registry unavailable")

    wm_mod.get_workspace_manager = _boom  # type: ignore[assignment]
    try:
        check(saved_chart_names(f"✅ 图表已保存: {REPORT_DIR / rel}") == [],
              "工作区解析失败 → 不登记（放行口径），不抛异常")
    finally:
        wm_mod.get_workspace_manager = original  # type: ignore[assignment]


# ── ③ 真中间件链：身份 / 工具名 / 结果形态 ────────────────────────────

def t3_middleware() -> None:
    section("③ 中间件链：谁出的图、什么工具、什么形态的结果")
    from agent.auth import grants

    dest = save_chart_via_subprocess("归属登记验证")
    name = pathlib.Path(dest).name if dest else ""
    if not name:
        check(False, "前置：真脚本落盘成功")
        return

    # 认这张图的是 execute 的结果
    with as_identity(ALICE, TID_ALICE):
        run_execute(f"✅ 图表已保存: {dest}")
    check(grants.report_owner_of(name) == ALICE, "登录身份出图 → 记到该用户", name)
    row = grants._get_conn().execute(
        "SELECT thread_id FROM report_owner WHERE filename=?", (name,)
    ).fetchone()
    check((row or [""])[0] == TID_ALICE, "thread_id 一起记（排障时能追到会话）", str(tuple(row or ())))

    # 别的工具（write_file 抄了一段带标记的文本）不得产生归属
    dest2 = save_chart_via_subprocess("非execute工具")
    name2 = pathlib.Path(dest2).name if dest2 else ""
    with as_identity(BOB, "tid-bob"):
        run_execute(f"✅ 图表已保存: {dest2}", tool_name="write_file")
    check(grants.report_owner_of(name2) == "", "工具名不是 execute → 不登记（只认这条产出路径）")

    # content blocks 形态（部分中间件会把 ToolMessage.content 变成 block 列表）
    dest3 = save_chart_via_subprocess("分块结果")
    name3 = pathlib.Path(dest3).name if dest3 else ""
    with as_identity(ALICE, TID_ALICE):
        run_execute([{"type": "text", "text": f"✅ 图表已保存: {dest3}"}])
    check(grants.report_owner_of(name3) == ALICE, "ToolMessage.content 为 content blocks 也认")

    # 内部调用 / dev 旁路：身份是 `internal`/`dev`（非空哨兵）→ 必须**不**登记。
    # 登记了就把图锁给一个不存在的用户 = 真实用户读不到自己刚出的图，比原来的洞更坏。
    for fake in ("internal", "dev", ""):
        d = save_chart_via_subprocess(f"非用户身份{fake or 'empty'}")
        nm = pathlib.Path(d).name if d else ""
        with as_identity(fake, "tid-x"):
            run_execute(f"✅ 图表已保存: {d}")
        check(grants.report_owner_of(nm) == "",
              f"身份 {fake!r} → 不登记（否则真实用户会被锁在门外）")

    # 完全没有 config 上下文（离线/内部直调）→ 不炸、不登记
    d = save_chart_via_subprocess("无身份上下文")
    nm = pathlib.Path(d).name if d else ""
    with as_no_identity():
        r = run_execute(f"✅ 图表已保存: {d}")
    check(grants.report_owner_of(nm) == "" and r is not None,
          "无 config 上下文 → 不登记且工具结果照常返回")

    # 幂等：同一张图重复登记不改写归属
    with as_identity(BOB, "tid-bob"):
        run_execute(f"✅ 图表已保存: {dest}")
    check(grants.report_owner_of(name) == ALICE, "重复登记不改写归属（INSERT OR IGNORE）")


# ── ④ 端到端授权（真 report_file handler + 真 AuthMiddleware）─────────

def build_app():
    from starlette.applications import Starlette

    from api import report_file
    from api.auth_middleware import AuthMiddleware

    return AuthMiddleware(Starlette(routes=list(report_file.routes)))


async def fetch(app, method: str, path: str, *, uid: str | None = None, is_admin: bool = False):
    import httpx

    headers = {"X-Forwarded-For": "1.2.3.4"}   # 非容器网段 → 不走 internal 旁路
    if uid:
        headers["Cookie"] = f"nl2sql_token={mint(uid, is_admin)}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(NGINX_IP, 12345)),
        base_url="http://testserver",
    ) as c:
        return await c.request(method, path, headers=headers)


async def e2e_checks() -> None:
    from agent.auth import grants

    app = build_app()

    # 修复前会漏的那张图（归属已由 ③ 登记给 ALICE）
    dest = save_chart_via_subprocess("端到端图")
    name = pathlib.Path(dest).name if dest else ""
    with as_identity(ALICE, TID_ALICE):
        run_execute(f"✅ 图表已保存: {dest}")
    check(grants.report_owner_of(name) == ALICE, "端到端：图已登记给 ALICE", name)

    # GET ?download=1 —— 判据用这条，不用列表（列表见下）
    r_alice = await fetch(app, "GET", f"/api/reports/{name}?download=1", uid=ALICE)
    r_bob = await fetch(app, "GET", f"/api/reports/{name}?download=1", uid=BOB)
    r_admin = await fetch(app, "GET", f"/api/reports/{name}?download=1", uid=ADMIN, is_admin=True)
    check(r_alice.status_code == 200 and r_alice.content.decode() == SVG,
          "**验收**：本人下载自己的图 → 200 且内容正确", str(r_alice.status_code))
    check(r_bob.status_code == 404, "**验收**：他人下载 → 404", str(r_bob.status_code))
    check(r_admin.status_code == 200, "管理员可读", str(r_admin.status_code))

    # 404 不泄露存在性：与「文件真的不存在」响应逐字节同形
    r_missing = await fetch(app, "GET", "/api/reports/no_such_file_1234.svg?download=1", uid=BOB)
    check(r_bob.status_code == r_missing.status_code and r_bob.content == r_missing.content,
          "越权 404 与「不存在」404 同状态码同正文（不泄露存在性）")

    # HEAD 也拦（存在性探测本身就是泄露）
    h_alice = await fetch(app, "HEAD", f"/api/reports/{name}", uid=ALICE)
    h_bob = await fetch(app, "HEAD", f"/api/reports/{name}", uid=BOB)
    check(h_alice.status_code == 200, "本人的图：HEAD 探测 200")
    check(h_bob.status_code == 404, "他人的图：HEAD 探测 404（前端附件按钮不该亮）")

    # 未登录：连存在性都不给
    r_anon = await fetch(app, "GET", f"/api/reports/{name}?download=1")
    check(r_anon.status_code >= 400, "未登录 → 拒绝", str(r_anon.status_code))

    # ── 无记录的存量图仍然放行（向后兼容口径）──
    legacy = REPORT_DIR / "legacy_chart_before_p117.svg"
    legacy.write_text(SVG, encoding="utf-8")
    r_legacy = await fetch(app, "GET", f"/api/reports/{legacy.name}?download=1", uid=BOB)
    check(r_legacy.status_code == 200,
          "存量无记录文件放行（老图不会因为本次修复突然打不开）", str(r_legacy.status_code))

    # ── 清单里那条验收为什么是恒真的（这不是断言缺陷，是判据本来就无效）──
    lst = await fetch(app, "GET", "/api/reports", uid=BOB)
    body = lst.content.decode()
    check(name not in body, "他人列表里看不到这张图（此条恒真：.svg 在判权前就被后缀过滤掉）")
    check(legacy.name not in body, "无记录的 .svg 也不在列表里（对上一条的证明）")
    md = REPORT_DIR / "someone_report.md"
    md.write_text("# x", encoding="utf-8")
    grants.record_report_owner(md.name, ALICE, TID_ALICE)
    lst2 = (await fetch(app, "GET", "/api/reports", uid=BOB)).content.decode()
    check(md.name not in lst2, "对照：.md 会被列表列出，且因归属不属于 BOB 而被过滤掉")
    check(md.name in (await fetch(app, "GET", "/api/reports", uid=ALICE)).content.decode(),
          "对照：同一份 .md 在 ALICE 的列表里（列表判权本身是有效的）")


# ── ⑤ 负对照（破坏性）：把中间件摘掉 → 洞重现 ────────────────────────

async def t5_negative_control() -> None:
    section("⑤ 负对照：中间件不挂（= 修复前）→ 越权读成功")
    dest = save_chart_via_subprocess("负对照图")
    name = pathlib.Path(dest).name if dest else ""
    if not name:
        check(False, "前置：真脚本落盘成功")
        return

    app = build_app()

    # 同一条产出链，**只是中间件不挂** —— 这正是修复前的世界
    run_execute(f"✅ 图表已保存: {dest}", with_middleware=False)
    from agent.auth import grants

    check(grants.report_owner_of(name) == "",
          "中间件不挂 → 图无归属（这本身就是漏洞的成因）")
    r_bob = await fetch(app, "GET", f"/api/reports/{name}?download=1", uid=BOB)
    check(r_bob.status_code == 200,
          "**负对照**：BOB 能下载 ALICE 的图（P1-17 的洞，修复前就是这样）",
          str(r_bob.status_code))

    # 挂上中间件 → 同一张图（此刻仍无记录）以 ALICE 身份重跑产出链 → 立刻被锁住
    with as_identity(ALICE, TID_ALICE):
        run_execute(f"✅ 图表已保存: {dest}")
    r_bob2 = await fetch(app, "GET", f"/api/reports/{name}?download=1", uid=BOB)
    check(r_bob2.status_code == 404,
          "**修复后**：同一张图，BOB 下载 → 404（绿色断言不是恒真）",
          str(r_bob2.status_code))


# ── ⑥ 静态接线 ────────────────────────────────────────────────────────

def t6_wiring() -> None:
    section("⑥ 接线：中间件挂在主 agent 的 middleware 列表里")
    path = _SRC / "agent/main_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "agent.middlewares.chart_artifact_owner"
        and any(a.name == "ChartArtifactOwnerMiddleware" for a in n.names)
        for n in ast.walk(tree)
    )
    check(imported, "main_agent.py 导入了 ChartArtifactOwnerMiddleware")

    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name == "ChartArtifactOwnerMiddleware":
                named |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    check(named == {"chart_owner"}, "实例化为 chart_owner", str(sorted(named)))

    listed = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "create_deep_agent":
            for kw in node.keywords:
                if kw.arg == "middleware" and isinstance(kw.value, ast.List):
                    els = {
                        (e.id if isinstance(e, ast.Name) else getattr(e, "attr", ""))
                        for e in kw.value.elts
                    }
                    listed = bool(named & els) or "ChartArtifactOwnerMiddleware" in els
    check(listed, "chart_owner 确实在 create_deep_agent(middleware=[...]) 里（不是只定义）")

    # 中间件只看 execute：工具名常量是唯一入口，改错了整项失效
    from agent.middlewares.chart_artifact_owner import _SHELL_TOOL

    check(_SHELL_TOOL == "execute", "只对 execute 生效", _SHELL_TOOL)


async def main_async() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    patch_mcp_loading()
    try:
        global REPORT_DIR
        REPORT_DIR = seed_workspace()
        print(f"报告目录（真工作区）= {REPORT_DIR}")

        t1_extract()
        t2_gates()
        t3_middleware()
        await e2e_checks()
        await t5_negative_control()
        t6_wiring()
    finally:
        restore_mcp_loading()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


def main() -> int:
    import asyncio

    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
