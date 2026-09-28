#!/usr/bin/env python
"""P3-8 出站 SSRF 守卫（`agent/utils/net_guard.py` + `api/model_config.py` 接线）验收。

背景：`POST /api/model-configs/test` 与 `/probe-capabilities` 的 `base_url` 可由请求体给，
服务端代发 HTTP 并把结果回显，而这两个端点只 `require_user`（模型配置是按用户独立 store
⇒ 不能靠"限管理员"解决）。口径（2026-09-25 拍板）：**只堵环回/未指定/链路本地+元数据，
私网一律放行**（本仓模型网关就在私网，如 http://192.168.25.13:8100/v1）。

分五节：
  A. `blocked_reason` 判据表（含 IPv4-mapped IPv6 与「私网必须放行」的负对照）
  B. `check_outbound_url` 的 scheme / 主机 / 解析失败 fail-open / env 逃生门
  C. **真 handler**：环回 base_url → 400 + 文案，且**一次出站都没发**；私网 base_url 照常走
  D. AST 结构：两层防线、`_ssrf_reject` 先于 `offload_long`、「守卫不能只放进会被吞异常的函数」
  E. 反向断言：网络被真打之前就拦下了（用会抛异常的假 urlopen 兜底）

跑法：`uv run --no-sync python scripts/verify_net_guard.py`
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  [FAIL] {name}{(' — ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ── 被测模块 ────────────────────────────────────────────────
from agent.utils.net_guard import (  # noqa: E402
    UnsafeOutboundURLError,
    blocked_reason,
    check_outbound_url,
)


def raises(url: str) -> str:
    """返回拒绝原因（未抛 = 空串）。"""
    try:
        check_outbound_url(url)
    except UnsafeOutboundURLError as e:
        return str(e)
    return ""


# ══ A. blocked_reason 判据表 ═══════════════════════════════
section("A. blocked_reason 判据表")

for ip in ["127.0.0.1", "127.1.2.3", "::1", "0.0.0.0", "::", "169.254.169.254", "169.254.1.1", "fe80::1"]:
    check(f"{ip} 被拒", blocked_reason(ip) != "", blocked_reason(ip))

# 私网必须放行 —— 这是本项最关键的一条负对照（本仓模型网关/数据库都在私网）
for ip in ["192.168.25.13", "192.168.25.64", "10.0.0.5", "172.16.0.1", "8.8.8.8", "1.1.1.1", "100.64.0.1"]:
    check(f"{ip} 放行（私网/公网不是本守卫的职责）", blocked_reason(ip) == "", blocked_reason(ip))

# IPv4-mapped IPv6：`is_loopback` 对 ::ffff:127.0.0.1 为 False，必须显式拆出内嵌 v4
check("::ffff:127.0.0.1 被拒（IPv4-mapped 不能漏）", blocked_reason("::ffff:127.0.0.1") != "")
check("::ffff:169.254.169.254 被拒", blocked_reason("::ffff:169.254.169.254") != "")
check("::ffff:192.168.1.1 放行（mapped 私网）", blocked_reason("::ffff:192.168.1.1") == "")
check("非法 IP 字符串不抛异常、返回空", blocked_reason("not-an-ip") == "")


# ══ B. check_outbound_url ══════════════════════════════════
section("B. check_outbound_url")

check("环回字面量 IP 被拒", "环回" in raises("http://127.0.0.1:2026/v1"))
check("localhost 被拒（走解析路径）", raises("http://localhost:2026") != "")
check("元数据地址被拒", "链路本地" in raises("http://169.254.169.254/latest/meta-data"))
check("file:// 被拒（只允许 http/https）", "只支持 http" in raises("file:///etc/passwd"))
check("ftp:// 被拒", raises("ftp://example.com/x") != "")
check("空串被拒", raises("") != "")
check("缺主机名被拒", raises("http:///v1") != "")
check("私网网关放行（本项目真实形态）", raises("http://192.168.25.13:8100/v1") == "")
check("公网放行", raises("https://api.openai.com/v1") == "")
check("带路径+端口+尾斜杠的私网放行", raises("http://192.168.25.13:8100/v1/") == "")

# 解析失败 → fail-open（守卫不替 DNS 报错；真错由 urlopen 给）
_orig_gai = socket.getaddrinfo


def _boom(host, *a, **kw):
    raise socket.gaierror(-2, "Name or service not known")


socket.getaddrinfo = _boom
try:
    check("解析失败放行（fail-open）", raises("http://no-such-host.invalid/v1") == "")
finally:
    socket.getaddrinfo = _orig_gai

# env 逃生门
os.environ["NL2SQL_SSRF_ALLOW_LOOPBACK"] = "1"
try:
    check("逃生门=1：环回放行（本机 ollama/网关）", raises("http://127.0.0.1:11434/v1") == "")
    check("逃生门=1：0.0.0.0 放行", raises("http://0.0.0.0:8000/v1") == "")
    check("逃生门=1：元数据**仍拒**（没有这种开发场景）", raises("http://169.254.169.254/x") != "")
finally:
    del os.environ["NL2SQL_SSRF_ALLOW_LOOPBACK"]
check("逃生门关掉后环回又拒", raises("http://127.0.0.1:11434/v1") != "")
for truthy in ("true", "YES", "On"):
    os.environ["NL2SQL_SSRF_ALLOW_LOOPBACK"] = truthy
    try:
        check(f"逃生门接受 {truthy!r}", raises("http://127.0.0.1:1/v1") == "")
    finally:
        del os.environ["NL2SQL_SSRF_ALLOW_LOOPBACK"]
os.environ["NL2SQL_SSRF_ALLOW_LOOPBACK"] = "0"
try:
    check("逃生门 '0' = 关（0 是假值不是开启）", raises("http://127.0.0.1:1/v1") != "")
finally:
    del os.environ["NL2SQL_SSRF_ALLOW_LOOPBACK"]


# ══ C. 真 handler ══════════════════════════════════════════
section("C. 真 handler（model_config.test_config / probe_capabilities）")

import api.model_config as mc  # noqa: E402


class _FakeStore:
    def __init__(self, cfg=None):
        self._cfg = cfg

    def get(self, name):
        if self._cfg is None:
            raise KeyError(name)
        return self._cfg


def make_request(body: dict, path_params: dict | None = None):
    from starlette.requests import Request

    payload = json.dumps(body).encode("utf-8")

    async def _receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/model-configs/test",
            "raw_path": b"/api/model-configs/test",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "path_params": path_params or {},
            "client": ("127.0.0.1", 12345),
            "server": ("verify", 80),
        },
        receive=_receive,
    )


async def body_of(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


_outbound: list[str] = []
_orig_probe_models = mc._probe_models
_orig_offload_long = mc.offload_long


def _recording_probe(base_url, api_key, api_protocol="", timeout=10.0):
    _outbound.append(base_url)
    return True, "连接成功（1 个模型）", ["m1"]


async def _inline_offload(fn, *a, **kw):
    return fn(*a, **kw)


def _patch():
    mc._probe_models = _recording_probe
    mc.offload_long = _inline_offload
    mc._get_store = lambda request: _FakeStore()  # type: ignore[assignment]


def _restore():
    mc._probe_models = _orig_probe_models
    mc.offload_long = _orig_offload_long


_patch()
try:
    # ① 请求体给环回 → 400 + 文案，且**没有出站**
    _outbound.clear()
    resp = asyncio.run(mc.test_config(make_request({"base_url": "http://127.0.0.1:2026/v1", "api_key": "k"})))
    data = asyncio.run(body_of(resp))
    check("test_config 环回 → 400", resp.status_code == 400, str(resp.status_code))
    check("test_config 环回 → ok=false", data.get("ok") is False)
    check("test_config 环回 → 文案含「已拒绝」", "已拒绝" in str(data.get("message", "")))
    check("test_config 环回 → 一次出站都没发", _outbound == [], str(_outbound))

    # ② 请求体给元数据地址 → 400，无出站
    _outbound.clear()
    resp = asyncio.run(mc.test_config(make_request({"base_url": "http://169.254.169.254"})))
    check("test_config 元数据 → 400", resp.status_code == 400, str(resp.status_code))
    check("test_config 元数据 → 无出站", _outbound == [])

    # ③ 私网网关 → 放行（这是本项目真实形态，必须走得通）
    _outbound.clear()
    resp = asyncio.run(
        mc.test_config(make_request({"base_url": "http://192.168.25.13:8100/v1", "api_key": "k"}))
    )
    data = asyncio.run(body_of(resp))
    check("test_config 私网 → 200", resp.status_code == 200, str(resp.status_code))
    check("test_config 私网 → 真的去探活了", _outbound == ["http://192.168.25.13:8100/v1"], str(_outbound))
    check("test_config 私网 → ok=true", data.get("ok") is True)

    # ④ 「先存后测」两步绕过：配置里的 base_url 是环回 → 同样被拦
    class _Cfg:
        base_url = "http://127.0.0.1:2026"
        api_key = "k"
        api_protocol = ""

    mc._get_store = lambda request: _FakeStore(_Cfg())  # type: ignore[assignment]
    _outbound.clear()
    resp = asyncio.run(mc.test_config(make_request({"name": "evil"})))
    check("test_config 用已存配置（环回）→ 400（不能只挡请求体）", resp.status_code == 400, str(resp.status_code))
    check("test_config 先存后测 → 无出站", _outbound == [])

    # ⑤ probe_capabilities 同样两道
    _outbound.clear()
    resp = asyncio.run(
        mc.probe_capabilities(make_request({"base_url": "http://localhost:2026", "models": ["m1"]}))
    )
    data = asyncio.run(body_of(resp))
    check("probe_capabilities 环回 → 400", resp.status_code == 400, str(resp.status_code))
    check("probe_capabilities 环回 → error 文案", "已拒绝" in str(data.get("error", "")))

    # ⑥ probe_capabilities 私网 → 不被守卫拦（这里只断言"没因守卫返回 400"）
    mc._probe_model_capabilities = lambda *a, **kw: {}  # type: ignore[assignment]
    mc._probe_single_model = lambda *a, **kw: {"context_window": None, "max_tokens": None}  # type: ignore[assignment]
    resp = asyncio.run(
        mc.probe_capabilities(
            make_request({"base_url": "http://192.168.25.13:8100/v1", "models": ["m1"]})
        )
    )
    check("probe_capabilities 私网 → 200（不因守卫被拒）", resp.status_code == 200, str(resp.status_code))

    # ⑦ 无 base_url / 无 name → 仍是原来的 400（守卫不改变既有分支顺序）
    resp = asyncio.run(mc.test_config(make_request({})))
    check("test_config 空 body → 400 base_url 必填", resp.status_code == 400)
    data = asyncio.run(body_of(resp))
    check("test_config 空 body 文案未变", data.get("error") == "base_url 必填", str(data))

    # ⑧ 反向断言：假 urlopen 会抛异常 —— 若守卫失效，环回用例会走到它（本脚本会炸）
    import urllib.request as _ur

    _orig_urlopen = _ur.urlopen

    def _explode(*a, **kw):
        raise AssertionError("守卫失效：真的发出了出站请求")

    _ur.urlopen = _explode  # type: ignore[assignment]
    try:
        resp = asyncio.run(mc.test_config(make_request({"base_url": "http://127.0.0.1:2026/v1"})))
        check("真 urlopen 被换成炸弹后仍返回 400（=没走到网络）", resp.status_code == 400)
    finally:
        _ur.urlopen = _orig_urlopen
finally:
    _restore()


# ══ D. AST 结构 ════════════════════════════════════════════
section("D. AST 结构（两层防线 + 顺序 + 不会被吞）")

tree = ast.parse((SRC / "api" / "model_config.py").read_text(encoding="utf-8"))


def funcs(node) -> dict:
    return {n.name: n for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


F = funcs(tree)


def calls(node, name: str) -> list:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                out.append(n)
    return out


check("`_ssrf_reject` 已定义", "_ssrf_reject" in F)
_rej = F.get("_ssrf_reject")
check("`_ssrf_reject` 调 check_outbound_url", bool(calls(_rej, "check_outbound_url")) if _rej else False)
check(
    "`_ssrf_reject` 只捕 UnsafeOutboundURLError（不吞别的）",
    any(
        isinstance(h.type, ast.Name) and h.type.id == "UnsafeOutboundURLError"
        for t in ast.walk(_rej)
        if isinstance(t, ast.Try)
        for h in t.handlers
    )
    if _rej
    else False,
)
check(
    "`_ssrf_reject` 失败返回 str(e)、成功返回 ''",
    _rej is not None
    and any(isinstance(r, ast.Return) and isinstance(r.value, ast.Constant) and r.value.value == "" for r in ast.walk(_rej)),
)
check(
    "check_outbound_url 是函数内 import（_ssrf_reject 里）",
    any(isinstance(n, ast.ImportFrom) and n.module == "agent.utils.net_guard" for n in ast.walk(_rej))
    if _rej
    else False,
)


def line_first(node, name: str) -> int:
    c = calls(node, name)
    return min(x.lineno for x in c) if c else 10**9


for fn in ("test_config", "probe_capabilities"):
    node = F.get(fn)
    check(f"{fn} 调用了 _ssrf_reject", bool(calls(node, "_ssrf_reject")) if node else False)
    check(
        f"{fn}: 守卫在 offload_long **之前**",
        line_first(node, "_ssrf_reject") < line_first(node, "offload_long")
        if node
        else False,
        f"guard@{line_first(node,'_ssrf_reject')} offload@{line_first(node,'offload_long')}",
    )

check("`_probe_models` 里也有守卫（纵深防御）", bool(calls(F.get("_probe_models"), "_ssrf_reject")) if F.get("_probe_models") else False)
check(
    "`_probe_model_capabilities` 未把守卫当唯一防线（它 except Exception 会吞）",
    bool(F.get("_probe_model_capabilities"))
    and not calls(F["_probe_model_capabilities"], "_ssrf_reject"),
)
check(
    "`_probe_model_capabilities` docstring 写明守卫在 handler 层",
    bool(F.get("_probe_model_capabilities"))
    and "handler" in ast.get_docstring(F["_probe_model_capabilities"]) or False,
)
check(
    "`_probe_model_capabilities` 的 except 确实是吞异常（说明为什么不能放守卫）",
    bool(F.get("_probe_model_capabilities"))
    and any(
        isinstance(t.handlers[0].type, ast.Name) and t.handlers[0].type.id == "Exception"
        for t in ast.walk(F["_probe_model_capabilities"])
        if isinstance(t, ast.Try) and t.handlers
    ),
)

# 反向断言：私网不能出现在被拒名单里（防有人把 RFC1918 加进去）
ng = (SRC / "agent" / "utils" / "net_guard.py").read_text(encoding="utf-8")
check("net_guard 里没有把私网加进拒绝名单（无 is_private 判据）", "is_private" not in ng)
check("net_guard 的 block 判据只有 loopback/unspecified/link_local", all(k in ng for k in ("is_loopback", "is_unspecified", "is_link_local")))

print(f"\n{PASS}/{PASS + len(FAIL)} 通过")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
