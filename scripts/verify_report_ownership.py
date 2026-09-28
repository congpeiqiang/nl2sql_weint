# -*- coding: utf-8 -*-
"""P1-3 报告/图表文件隔离验证（离线，无需后端/数据库/网络）。

P1-3 要解决两件事，本脚本两件都验：
  ① **同名不互相覆盖**：`report/` 是全站共享目录，文件名原先只到秒级
     （报告 `{标题}_{ts}.md`、chart-saver `{基名}.svg` 连时间戳都没有、
     generate_echarts 自动落盘的 `{标题}_{ts}.html/.svg`）。
     两个用户对同名主题出图/出报告 → 后写的人静默覆盖前一个人的文件，
     而前一个人的报告里还引用着那个文件名（= 别人的图表被替换）。
  ② **知道文件名 ≠ 有权读**：文件名可猜（标题+时间戳），且目录列表本身
     就是文件名的来源，所以 GET/HEAD/列表三处都必须过归属。
     归属账本 = `auth.grants.report_owner`（落盘时登记）。
     无记录的存量文件放行（与 `owned_thread` 的兼容口径一致）；
     有记录且不是本人 → **404**（与"不存在"同响应，不泄露"这份报告存在"）。

为什么用合成 ASGI 请求而不是真后端：
  `report_file` 的判定是纯函数级（`require_user` + `can_read_report` + 路径解析），
  合成 scope + 真 `AuthMiddleware` + 真 token 就能精确断言；跑真后端反而要造
  会话/工作区/归属行。唯一被打桩的是 `report_file.get_workspace_manager`
  （把 report 根指到临时目录）——**路径穿越防护、归属判定、响应形态全部走真实代码**。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_report_ownership.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import sys
import tempfile
import time

# ⚠️ 必须在 import grants/token 之前设置：它们会把 sqlite / HMAC 密钥写到
# <AGENT_DATA_ROOT>。不设就落在仓库根（污染仓库 + 被发版 tar 打进镜像）。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-report-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
NGINX_IP = "172.18.0.9"
EXTERNAL_IP = "192.168.25.77"

ALICE = "Z0051"     # owner
BOB = "Z9999"       # 非 owner
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 合成请求基础设施（与 verify_endpoint_guards.py 同一套路）────────────

def mint(uid: str, is_admin: bool = False) -> str:
    """P1-12：先登记身份再签（`verify_token` 现在要求账号存在），见 `_auth_test_support.py`。"""
    from agent.auth import grants
    grants.register_user = lambda *a, **k: None  # 中间件放行后会调它，打桩掉磁盘写
    from _auth_test_support import mint_for
    return mint_for(uid, is_admin)


REPORT_DIR = pathlib.Path(_TMP_ROOT) / "report"


def build_app() -> Starlette:
    from api.auth_middleware import AuthMiddleware
    from api import report_file

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    class _WM:  # 只钉住 report 根目录，其余判定走真实代码
        report_dir = REPORT_DIR

    report_file.get_workspace_manager = lambda: _WM()
    return AuthMiddleware(Starlette(routes=list(report_file.routes)))


async def fetch(app, method: str, path: str, *, uid: str | None = None,
                is_admin: bool = False, ip: str = NGINX_IP) -> httpx.Response:
    headers = {}
    if uid:
        headers["Cookie"] = f"nl2sql_token={mint(uid, is_admin)}"
    headers["X-Forwarded-For"] = "1.2.3.4"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
        base_url="http://testserver",
    ) as c:
        return await c.request(method, path, headers=headers)


# ── ① 账本单测 ────────────────────────────────────────────────────────

def t1_ledger() -> None:
    section("① 归属账本（grants.report_owner）")
    from agent.auth import grants

    grants.record_report_owner("alice_only.md", ALICE, "tid-a")
    check(grants.report_owner_of("alice_only.md") == ALICE, "登记后可查到归属人")
    check(grants.report_owner_of("nobody_wrote_this.md") == "",
          "未登记的文件归属为空（= 走放行口径）")

    # 幂等 + 先到者胜：改写归属 = 后来者能把别人的报告认领成自己的
    grants.record_report_owner("alice_only.md", BOB, "tid-b")
    check(grants.report_owner_of("alice_only.md") == ALICE,
          "重复登记不改写归属（INSERT OR IGNORE，先到者胜）")

    alice = {"user_id": ALICE, "is_admin": False}
    bob = {"user_id": BOB, "is_admin": False}
    admin = {"user_id": "admin", "is_admin": True}
    check(grants.can_read_report(alice, "alice_only.md") is True, "本人可读")
    check(grants.can_read_report(bob, "alice_only.md") is False, "他人不可读")
    check(grants.can_read_report(admin, "alice_only.md") is True, "管理员恒可读")
    check(grants.can_read_report(bob, "nobody_wrote_this.md") is True,
          "无记录的存量文件放行（向后兼容口径）")


# ── ② 唯一文件名：共享目录不互相覆盖 ──────────────────────────────────

def t2_unique_names() -> None:
    section("② 同名不覆盖（报告 / 图表 / skill 三条落盘路径）")
    from agent.utils.path_resolver import _reserve_report_file

    d = pathlib.Path(tempfile.mkdtemp(prefix="uniq-"))
    # 同秒、同基名、同扩展名连打 8 次 = 模拟 8 个用户同秒出同名图
    names = [_reserve_report_file(d, "月度销售趋势", ".html")[0] for _ in range(8)]
    check(len(set(names)) == 8, "自动落盘：同秒同基名 8 次得到 8 个不同文件名")
    check(all(n.startswith("月度销售趋势_") and n.endswith(".html") for n in names),
          "命名仍是 {基名}_{时间戳}_{随机}{扩展名}", names[0])
    check(len(list(d.iterdir())) == 8, "8 次占位产生 8 个文件（无互相覆盖）")

    # 原子性：已存在的名字不会被复用（O_EXCL，不是 check-then-act）
    first = names[0]
    later = [_reserve_report_file(d, "月度销售趋势", ".html")[0] for _ in range(3)]
    check(first not in later, "已占用的文件名不会被二次分配")

    # skill 脚本（chart-saver）的命名 —— 原先**无时间戳**，同名 100% 覆盖
    sk = load_module(
        _HERE.parents[1] / "src/agent/shared/skills/main/chart-saver/scripts/save_chart.py",
        "verify_save_chart",
    )
    d2 = pathlib.Path(tempfile.mkdtemp(prefix="chart-"))
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>销售趋势</text></svg>'
    got = [sk.save_chart(svg, "销售趋势", "svg", str(d2)) for _ in range(3)]
    check(len(set(got)) == 3, "chart-saver：同秒同名 3 次得到 3 个不同文件")
    check(all(pathlib.Path(p).is_file() for p in got), "chart-saver 产物都真实落盘")
    check(all(pathlib.Path(p).read_text(encoding="utf-8") == svg for p in got),
          "每一份内容都是完整 SVG（没有被覆盖成半截）")
    check(all("_20" in pathlib.Path(p).name for p in got),
          "chart-saver 文件名带时间戳（原先连时间戳都没有）")

    # 内容解析失败时不留空文件（占位在解析之后）
    try:
        sk.save_chart("<p>不是图表</p>", "坏内容", "svg", str(d2))
        check(False, "非 SVG 内容应报错")
    except ValueError:
        check(len(list(d2.iterdir())) == 3, "解析失败不产生空占位文件")


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── ③ 自动落盘的图表登记归属 ──────────────────────────────────────────

def t3_chart_attribution() -> None:
    section("③ generate_echarts 自动落盘的图表登记归属")
    from agent.auth import grants
    from agent.utils import path_resolver as pr

    work = pathlib.Path(tempfile.mkdtemp(prefix="ws-"))
    pr._get_workspace_dir = lambda: work  # type: ignore[assignment]
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>月度销售趋势</text></svg>'

    # 外部请求：configurable.user_id 已被钳制为登录身份（P1-2），此值可信
    pr._lg_get_config = lambda: {"configurable": {"user_id": ALICE, "thread_id": "tid-1"}}  # type: ignore[assignment]
    name = pr._save_svg_to_workspace(svg)
    check(bool(name) and (work / "report" / name).is_file(), "SVG 自动落盘成功", name)
    check(grants.report_owner_of(name) == ALICE, "落盘即登记归属人")
    bob = {"user_id": BOB, "is_admin": False}
    alice = {"user_id": ALICE, "is_admin": False}
    check(grants.can_read_report(alice, name) is True, "产出者可读自己自动落盘的图")
    check(grants.can_read_report(bob, name) is False, "他人不可读（负对照：不登记时这里会是 True）")

    # 负对照：内部调用 / 无身份 → 不登记 → 走放行口径（证明差异确实来自登记）
    pr._lg_get_config = lambda: {"configurable": {}}  # type: ignore[assignment]
    anon = pr._save_svg_to_workspace(svg)
    check(grants.report_owner_of(anon) == "", "无身份时不登记（内部调用不留归属）")
    check(grants.can_read_report(bob, anon) is True, "无归属文件仍放行（兼容口径）")

    # 负对照：账本写入抛异常不能影响出图
    pr._lg_get_config = lambda: {"configurable": {"user_id": ALICE}}  # type: ignore[assignment]
    orig = grants.record_report_owner
    grants.record_report_owner = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        n2 = pr._save_svg_to_workspace(svg)
        check(bool(n2), "账本故障时图表仍正常落盘（best-effort 不阻断出图）")
    finally:
        grants.record_report_owner = orig


# ── ④ API 端点：GET / HEAD / 列表 ─────────────────────────────────────

def seed_files() -> tuple[str, str, str]:
    """造三个文件：alice 的、bob 的、无归属的存量文件。"""
    from agent.auth import grants

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    alice_f, bob_f, legacy_f = "A_alice_20260923_100000.md", "B_bob_20260923_100000.md", "C_legacy_20260801.md"
    for fn, body in ((alice_f, "# alice 的报告"), (bob_f, "# bob 的报告"), (legacy_f, "# 存量报告")):
        (REPORT_DIR / fn).write_text(body, encoding="utf-8")
    grants.record_report_owner(alice_f, ALICE, "tid-a")
    grants.record_report_owner(bob_f, BOB, "tid-b")
    return alice_f, bob_f, legacy_f


def t4_endpoints() -> None:
    section("④ 端点归属（GET / HEAD / download / 目录列表）")
    import asyncio

    app = build_app()
    alice_f, bob_f, legacy_f = seed_files()

    def get(path, **kw):
        return asyncio.run(fetch(app, "GET", path, **kw))

    # 未登录：连文件名都不该看到（P1 之前这条是完全开放的）
    r = get(f"/api/reports/{alice_f}")
    check(r.status_code == 401, "未登录 GET → 401", str(r.status_code))

    # 命中：自己的 + 存量
    r = get(f"/api/reports/{alice_f}", uid=ALICE)
    check(r.status_code == 200 and "alice" in r.text, "本人 GET 自己的报告 → 200")
    r = get(f"/api/reports/{legacy_f}", uid=BOB)
    check(r.status_code == 200, "无归属存量文件对任何登录用户放行 → 200（向后兼容）")
    r = get(f"/api/reports/{alice_f}", uid="boss", is_admin=True)
    check(r.status_code == 200, "管理员可读他人报告 → 200")

    # 拒绝：他人的
    r_other = get(f"/api/reports/{alice_f}", uid=BOB)
    check(r_other.status_code == 404, "他人 GET → 404", str(r_other.status_code))
    r_missing = get("/api/reports/根本不存在的文件.md", uid=BOB)
    check(r_missing.status_code == 404 and r_missing.text == r_other.text,
          "越权 404 与「不存在」404 同文案（不泄露文件存在性）",
          r_other.text[:24])

    # HEAD：前端「附件按钮」的存在性探测，同一口径
    async def head(path, **kw):
        return await fetch(app, "HEAD", path, **kw)

    r = asyncio.run(head(f"/api/reports/{alice_f}", uid=ALICE))
    check(r.status_code == 200 and r.headers.get("Content-Length") == str(
        len((REPORT_DIR / alice_f).read_bytes())), "本人 HEAD → 200 + 真实字节数")
    r = asyncio.run(head(f"/api/reports/{alice_f}", uid=BOB))
    check(r.status_code == 404, "他人 HEAD → 404（探测也不泄露存在性）")

    # download=1 不能绕过
    r = get(f"/api/reports/{alice_f}?download=1", uid=BOB)
    check(r.status_code == 404, "他人 ?download=1 → 404（下载参数不是旁路）")

    # 目录列表：列表本身是文件名的来源，必须同口径过滤
    r = get("/api/reports", uid=BOB)
    names = {x["filename"] for x in r.json().get("reports", [])}
    check(alice_f not in names, "列表不出现他人文件名")
    check(bob_f in names, "列表包含自己的文件（负对照：不是「全空」）")
    check(legacy_f in names, "列表包含无归属存量文件")
    r = get("/api/reports", uid=ALICE)
    names = {x["filename"] for x in r.json().get("reports", [])}
    check(bob_f not in names and alice_f in names, "镜像对照：换个人看，可见集合随之翻转")
    r = get("/api/reports", uid="boss", is_admin=True)
    names = {x["filename"] for x in r.json().get("reports", [])}
    check(alice_f in names and bob_f in names, "管理员列表可见全部")

    # 路径穿越防护仍在（P1-3 没有把它改坏）
    r = get("/api/reports/..%2F..%2Fmodel_config.json", uid=ALICE)
    check(r.status_code == 404, "路径穿越仍被挡（../../ 解析后必须仍在 report 内）")

    # 图表文件（.html/.svg）与报告同一条通道 → 同一套归属
    chart = "D_alice_chart_20260923_100000_ab12.svg"
    (REPORT_DIR / chart).write_text("<svg/>", encoding="utf-8")
    from agent.auth import grants
    grants.record_report_owner(chart, ALICE, "tid-a")
    r = get(f"/api/reports/{chart}", uid=BOB)
    check(r.status_code == 404, "图表文件同样受归属约束（不是只管 .md）")
    r = get(f"/api/reports/{chart}", uid=ALICE)
    check(r.status_code == 200 and r.headers.get("content-type", "").startswith("text/markdown"),
          "本人可取图表文件（扩展名不参与授权，只影响 media_type）")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {_TMP_ROOT}")
    t1_ledger()
    t2_unique_names()
    t3_chart_attribution()
    t4_endpoints()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
