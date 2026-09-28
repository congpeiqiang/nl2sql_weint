# -*- coding: utf-8 -*-
"""AuthMiddleware 放行判据验收（纯 ASGI 单测，本地跑，不需要起服务）。

背景（2026-09-23 定位）：中间件的内部旁路原先只判「来源是 Docker 网段 + 无 Cookie」，
而 **nginx 容器自己就在 172.x 网段** —— 于是从 :8080 进来、带 X-Forwarded-For、
不带 Cookie 的请求被认成 internal：`/api/auth/me` 返回 200 + 用户信息 →
前端 AuthGuard 认为「已登录」、不跳 /login → **登录门被整体绕过**，
63 个 /api/* 自定义端点全部敞开。

修法：内部旁路额外要求「**没有** X-Forwarded-For」，与
src/agent/auth/backend.py:56（原生路由那层）同判据。全仓唯一写 XFF 的地方是
docker/nginx.conf，Python 侧没有任何写入点，所以「带 XFF」⟺「经过了外部入口」。

⚠️ 本脚本只覆盖 **ASGI 中间件这一层**（/api/* 自定义端点）。
另一条门在 ops 层：backend.py 的内部旁路只看 header、**看不到对端 IP**，
所以「从 LAN 直连 2026 且不带 XFF」会被认成 internal —— 那条只能靠
把 compose 的 `ports` 收成 `127.0.0.1:2026:2026` 来关（配置项，非本脚本可测）。

用法：uv run python scripts/verify_auth_middleware.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import token/grants 之前设置：sign_token 会把 HMAC 密钥写到
# <AGENT_DATA_ROOT>/auth_secret。不设的话密钥就落在 CWD（=仓库根）里，
# 既污染仓库、又会被发版 tar 打进镜像。指到临时目录，跑完即弃。
os.environ.setdefault(
    "AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-auth-")
)

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from api.auth_middleware import AuthMiddleware  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


# ── 合成 ASGI scope ──────────────────────────────────────────

NGINX_IP = "172.18.0.9"   # nginx 容器：在 Docker 网段里，但请求来自外部
INTERNAL_IP = "172.18.0.5"  # 子 agent / sync 循环等内部调用方
LAN_IP = "192.168.25.77"    # 同网段的其他机器（直连 :2026 的场景）


def make_scope(path: str, client_ip: str, cookie: str = "", xff: str = "") -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if cookie:
        headers.append((b"cookie", f"nl2sql_token={cookie}".encode()))
    if xff:
        headers.append((b"x-forwarded-for", xff.encode()))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "client": (client_ip, 51234),
        "state": {},
    }


class Downstream:
    """下游 app：记录是否被调用、以及中间件注入的 user。"""

    def __init__(self) -> None:
        self.called = False
        self.user: dict | None = None

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.user = (scope.get("state") or {}).get("user")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def run_case(path: str, client_ip: str, cookie: str = "", xff: str = ""):
    """驱动中间件，返回 (下游是否被调用, 注入的 user, 响应状态码)。"""
    scope = make_scope(path, client_ip, cookie, xff)
    down = Downstream()
    status: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]

    await AuthMiddleware(down)(scope, receive, send)
    return down.called, down.user, status.get("code")


# ── 造一个真实 token（顺带把 grants 写库打桩掉，避免污染本地库）────

def mint_token(uid: str, is_admin: bool = False) -> str:
    """P1-12：`verify_token` 以账号记录为唯一真源 → 先登记身份再签（见 `_auth_test_support.py`）。"""
    import agent.auth.grants as grants
    grants.register_user = lambda *a, **k: None  # 打桩：中间件放行后会调它
    from _auth_test_support import mint_for
    return mint_for(uid, is_admin)


async def main() -> int:
    print("=" * 72)
    print("AuthMiddleware 放行判据验收")
    print(f"  AGENT_DATA_ROOT={os.getenv('AGENT_DATA_ROOT', '(未设置)')}")
    print("=" * 72)

    good = mint_token("u_verify", is_admin=False)

    print("\n[1] 本次修的洞：外部（nginx 转发）不带 Cookie —— 必须 401")
    for path in ("/api/auth/me", "/api/db-configs", "/api/reports/secret.md"):
        called, _, code = await run_case(path, NGINX_IP, xff="192.168.25.77")
        check(not called and code == 401, f"{path} 无 Cookie 有 XFF → 401", f"code={code}")
    called, _, code = await run_case("/threads/search", NGINX_IP, xff="192.168.25.77")
    check(not called and code == 401, "/threads/search 无 Cookie 有 XFF → 401", f"code={code}")

    print("\n[2] 内部调用（子 agent / sync 循环）不带 Cookie 也不带 XFF —— 必须放行")
    for path in ("/threads/search", "/api/reports/x.md", "/api/feedback/annotations"):
        called, user, _ = await run_case(path, INTERNAL_IP)
        uid = (user or {}).get("user_id")
        check(called and uid == "internal", f"{path} 内部无 Cookie 无 XFF → internal", f"user={uid}")

    print("\n[3] 正常用户（有效 Cookie）—— 必须放行为真实身份，不能退化成 internal")
    for path in ("/api/auth/me", "/threads/search"):
        called, user, _ = await run_case(path, NGINX_IP, cookie=good, xff="192.168.25.77")
        uid = (user or {}).get("user_id")
        check(called and uid == "u_verify", f"{path} 有效 Cookie → u_verify", f"user={uid}")

    print("\n[4] 无效 Cookie 不能蒙混 —— 401")
    called, _, code = await run_case("/api/auth/me", NGINX_IP, cookie="bogus.token.x", xff="1.2.3.4")
    check(not called and code == 401, "/api/auth/me 无效 Cookie → 401", f"code={code}")

    print("\n[5] 内部来源但带有效 Cookie —— 走 token 校验（不能因来源内部就跳过鉴权）")
    called, user, _ = await run_case("/api/auth/me", INTERNAL_IP, cookie=good)
    uid = (user or {}).get("user_id")
    check(called and uid == "u_verify", f"内部 + 有效 Cookie → u_verify（非 internal）", f"user={uid}")

    print("\n[6] LAN 直连（非 Docker 网段、无 XFF）—— 401（中间件这层的兜底）")
    called, _, code = await run_case("/threads/search", LAN_IP)
    check(not called and code == 401, "/threads/search LAN 直连无 XFF → 401", f"code={code}")

    print("\n[7] 白名单不被误伤")
    for path in ("/api/auth/login", "/api/auth/logout", "/ok", "/_next/static/x.js"):
        called, _, _ = await run_case(path, NGINX_IP, xff="192.168.25.77")
        check(called, f"{path} 白名单 → 放行")

    total = len(results)
    passed = sum(1 for ok, _ in results if ok)
    print("\n" + "=" * 72)
    print(f"结果：{passed}/{total} " + ("全绿" if passed == total else "有失败"))
    print("=" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
