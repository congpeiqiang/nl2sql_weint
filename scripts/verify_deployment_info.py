# -*- coding: utf-8 -*-
"""部署默认配置端点（``GET /api/deployment-info``）验收（纯本地，不起服务）。

背景（2026-09-28）：前端首屏原先要求用户手填「部署 URL」和「助手 ID」才能进聊天页，
而这两个值**都不是用户能提供的** —— 前者留空即跟随当前访问地址，后者是本部署的图名
（`langgraph.json` 的 `graphs`）。本端点把默认助手 ID 交给前端，使「登录后直接进聊天页」
成立；弹窗只在探测失败时兜底。

这份脚本钉住四件容易悄悄坏掉的事：
  ① 图名来源（真 `langgraph.json` 读得到；文件缺失/空/坏 JSON 一律 ``[]`` 且不抛）；
  ② 端点行为（未登录 401；已登录给对值；主入口被改名时取第一个图名而不是瞎给）；
  ③ **不在 auth 白名单里**（一旦被谁顺手加成白名单，图名就对未登录访客公开了）；
  ④ 在组合根 `custom_app.py` 里**真的注册了**（漏注册 = 前端永远走兜底弹窗，
     而这条路径不会有任何报错，只会静默回退 —— 正是最容易漏的错）。

用法：PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_deployment_info.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

# 必须在 import api.* 之前：auth/grants 等模块会把密钥/库写到 AGENT_DATA_ROOT，
# 不设就落在仓库根（污染仓库 + 可能被发版 tar 打进镜像）。
os.environ.setdefault(
    "AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-devinfo-")
)

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from api import deployment_info as di  # noqa: E402
from api.auth_middleware import _is_whitelisted  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def _req(user: dict | None) -> object:
    """最小 Starlette Request（handler 只读 request.state.user）。"""
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/api/deployment-info", "headers": []}
    if user is not None:
        scope["state"] = {"user": user}
    return Request(scope)


def _call(user: dict | None) -> tuple[int, dict]:
    """同步调 handler，返回 (status, body)。"""
    resp = asyncio.run(di.deployment_info(_req(user)))
    return resp.status_code, json.loads(bytes(resp.body).decode("utf-8"))


print("── ① 图名来源（langgraph.json）──")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="devinfo-"))


def _write(name: str, text: str) -> pathlib.Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


real = di._graph_ids()
check(bool(real), "真实 langgraph.json 读得到图名", f"{real}")
check("chat_agent" in real, "含主入口 chat_agent")
check("nl2sql_agent" in real, "含子图 nl2sql_agent")
check(di._DEFAULT_ASSISTANT_ID in real, "兜底常量与真实图名一致（不会悄悄漂）")

check(di._graph_ids(_write("bad.json", "{ not json")) == [], "坏 JSON → [] 且不抛")
check(di._graph_ids(_write("empty.json", '{"graphs": {}}')) == [], "graphs 为空 → []")
check(di._graph_ids(_write("nog.json", '{"dependencies": []}')) == [], "缺 graphs 段 → []")
check(
    di._graph_ids(_write("list.json", '{"graphs": ["a"]}')) == [],
    "graphs 不是 dict → []（负对照）",
)
check(
    di._graph_ids(tmp / "does-not-exist.json") == [],
    "文件不存在 → [] 且不抛（负对照）",
)
check(
    di._graph_ids(_write("ok.json", '{"graphs": {"g1": {}, "g2": {}}}')) == ["g1", "g2"],
    "正常文件按序返回图名",
)

print("\n── ② 端点行为 ──")

from starlette.exceptions import HTTPException  # noqa: E402

try:
    _call(None)
    check(False, "未登录 → 401（负对照）")
except HTTPException as e:
    check(e.status_code == 401, "未登录 → 401（负对照）", f"status={e.status_code}")

status, body = _call({"user_id": "zhangsan", "is_admin": False})
check(status == 200, "已登录（普通用户）→ 200")
check(body.get("assistant_id") == "chat_agent", "assistant_id = chat_agent", f"{body.get('assistant_id')}")
check(body.get("graph_ids") == real, "graph_ids 与真实图名一致")
check(body.get("source") == "langgraph.json", "source = langgraph.json", f"{body.get('source')}")
check(body.get("ok") is True, "ok = true")

# 主入口被改名：必须自愈成「第一个图名」，而不是硬回常量
_orig = di._graph_ids
di._graph_ids = lambda *a, **k: ["renamed_main", "other"]
try:
    _, body2 = _call({"user_id": "zhangsan"})
    check(body2["assistant_id"] == "renamed_main", "主入口改名 → 取第一个图名（自愈）")
finally:
    di._graph_ids = _orig

# 文件读不到：回落常量，且如实标 source=fallback
di._graph_ids = lambda *a, **k: []
try:
    _, body3 = _call({"user_id": "zhangsan"})
    check(
        body3["assistant_id"] == "chat_agent" and body3["source"] == "fallback",
        "读不到图名 → 回落常量 + source=fallback",
    )
finally:
    di._graph_ids = _orig

print("\n── ③ 鉴权面 ──")

check(
    not _is_whitelisted("/api/deployment-info"),
    "不在 auth 白名单（未登录拿不到图名）",
)

print("\n── ④ 组合根注册 ──")

_root = _HERE.parents[1]
_src = (_root / "src" / "api" / "custom_app.py").read_text(encoding="utf-8")
check("import api.deployment_info" in _src, "custom_app.py 有 import")
check("*api.deployment_info.routes" in _src, "custom_app.py 展开了 routes")
check(
    _src.count("*api.deployment_info.routes") == 1,
    "只注册一次（重复注册 = 路由冲突）",
)

_paths = [getattr(r, "path", "") for r in di.routes]
check(_paths == ["/api/deployment-info"], "路由路径唯一且正确", f"{_paths}")
_methods = sorted(getattr(di.routes[0], "methods", []) or [])
# Starlette 给 GET 路由自动补 HEAD，所以合法集合就是 {GET} 或 {GET, HEAD}；
# 这里只排除写方法（有写方法 = 这个只读端点被改错了）。
check(
    "GET" in _methods and not ({"POST", "PUT", "PATCH", "DELETE"} & set(_methods)),
    "只读（GET，可带 Starlette 自动补的 HEAD）",
    f"{_methods}",
)

print()
_bad = [label for ok, label in results if not ok]
if _bad:
    print(f"{len(results) - len(_bad)}/{len(results)} 通过；失败：")
    for b in _bad:
        print(f"  - {b}")
    sys.exit(1)
print(f"{len(results)}/{len(results)} 通过")
