# -*- coding: utf-8 -*-
"""P1 端点鉴权守卫验证（离线，无需后端/数据库/网络）。

做两件事：
  ① 把 `refuse` 清单里的每个端点用**未登录**和**已登录非 owner/非管理员**打一遍，
     断言都被挡在守卫上（401 / 403）——这些端点在 2026-09-23 之前是「登录即可」
     甚至「不登录即可」。
  ② 用 owner / 管理员再打一遍**只读**端点，断言守卫**不会误伤**（状态码不是
     401/403；下游可能 404/500/502，本脚本只关心「有没有被自己的守卫拦下」）。

为什么不用真后端：守卫是纯函数级行为（require_user → scope["state"]["user"]），
用合成 ASGI scope 就能精确断言；跑真后端反而要造会话、造反馈、造工作区。

运行：
    uv run --no-project python scripts/verify_endpoint_guards.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import token/grants 之前设置：sign_token 会把 HMAC 密钥写到
# <AGENT_DATA_ROOT>/auth_secret。不设就会落在 CWD（=仓库根），既污染仓库、
# 又会被发版 tar 打进镜像（发版脚本已用 --exclude=auth_secret 兜一道）。
os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-guards-"))
os.environ.setdefault("NL2SQL_AUTH_DISABLED", "0")

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

NGINX_IP = "172.18.0.9"        # 容器网段（真实浏览器经 nginx 后端的来源）
EXTERNAL_IP = "192.168.25.77"  # 局域网直连（不该被当 internal）
results: list[tuple[bool, str]] = []

Z = "Z0051"
OWNER = "Z0051"
OTHER = "Z9999"


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def mint(uid: str, is_admin: bool = False) -> str:
    """签一个真实可校验的 token（走与线上同一条 sign/verify 链路）。

    P1-12 起 `verify_token` 要求账号在 auth_users.json 里存在（记录是唯一真源），
    所以这里先**登记身份**再签 —— 等价于生产里的"管理员建号 → 用户登录"，
    详见 `scripts/_auth_test_support.py`。
    """
    from agent.auth import grants
    grants.register_user = lambda *a, **k: None  # 中间件放行后会调它，打桩掉磁盘写
    from _auth_test_support import mint_for
    return mint_for(uid, is_admin)


def build_app() -> Starlette:
    """把被测路由模块的真实 routes 装进一个最小 app + 真实 AuthMiddleware。"""
    from api.auth_middleware import AuthMiddleware
    from api import (
        db_config,
        feedback_stats,
        message_feedback,
        report_file,
        thread_run_status,
        trace_routes,
        wren_semantic,
    )

    # wren_semantic 其余路由属语义库管理（另一套 _require_project_access），
    # 本脚本只验 /api/git-ssh-key 这一条，单独挑出来避免引入无关依赖。
    git_key_routes = [
        r for r in wren_semantic.routes
        if getattr(r, "path", "") == "/api/git-ssh-key"
    ]
    assert git_key_routes, "wren_semantic 里没找到 /api/git-ssh-key 路由"

    routes = [
        *report_file.routes,
        *thread_run_status.routes,
        *message_feedback.routes,
        *feedback_stats.routes,
        *trace_routes.routes,
        *db_config.routes,
        *git_key_routes,
    ]
    return AuthMiddleware(Starlette(routes=routes))


async def probe(app, method: str, path: str, *, uid: str | None = None,
                is_admin: bool = False, ip: str = NGINX_IP, xff: str = "1.2.3.4",
                json_body: dict | None = None) -> int:
    """发一个合成请求，返回状态码。

    uid=None → 不带 Cookie 的裸请求；xff="" → 模拟「来自外网入口但无 XFF 头」
    之外的情况（默认带上，跟经 nginx 的真实请求一致）。
    """
    headers: dict[str, str] = {}
    if uid:
        headers["Cookie"] = f"nl2sql_token={mint(uid, is_admin)}"
    if xff:
        headers["X-Forwarded-For"] = xff
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
        base_url="http://testserver",
    ) as c:
        r = await c.request(method, path, headers=headers, json=json_body)
    return r.status_code


FAKE_TID = "11111111-1111-4111-8111-111111111111"  # 非本人名下
MINE_TID = "22222222-2222-4222-8222-222222222222"  # 记在 Z0051 名下


def seed_owner_rows() -> None:
    """直接往 grants 表写两条归属：一条别人的、一条自己的。"""
    from agent.auth import grants
    grants.claim_thread(FAKE_TID, OTHER)
    grants.claim_thread(MINE_TID, OWNER)


# (method, path, body) —— 全部应为「登录了但不是 owner / 不是管理员」也进不去
REFUSE_NONOWNER = [
    ("GET", f"/api/traces/threads/{FAKE_TID}/events", None),
    ("GET", f"/api/traces/threads/{FAKE_TID}/lineage", None),
    ("GET", f"/api/traces/threads/{FAKE_TID}/stats", None),
    ("GET", f"/api/traces/threads/{FAKE_TID}/llm-calls", None),
    ("GET", f"/api/threads/{FAKE_TID}/run-status", None),
    ("GET", f"/api/threads/{FAKE_TID}/feedback", None),
    ("PUT", f"/api/threads/{FAKE_TID}/messages/m1/feedback", {"rating": "positive"}),
    ("DELETE", f"/api/threads/{FAKE_TID}/messages/m1/feedback", None),
]

REFUSE_NONADMIN = [
    ("GET", "/api/traces/tasks/task-abc/events", None),     # traces/tasks（跨 thread）
    ("GET", "/api/feedback/export", None),
    ("GET", "/api/feedback/stats", None),
    ("GET", "/api/mcp/status", None),
    ("POST", "/api/mcp/reload", None),
    ("POST", "/api/db-configs/somedb/test", {"host": "10.0.0.1", "port": 6379}),
]

# 登录即可（非管理员也要能进）：公钥不是机密，语义库只读浏览对已授权用户开放，
# 不该因为「复制公钥」把整个面板变成 admin 专属。
ALLOW_NONADMIN = [
    ("GET", "/api/git-ssh-key", None),
]

# 未登录（外部来源）→ 中间件就该 401，连 handler 都进不去
REFUSE_ANON = [
    ("GET", "/api/reports/whatever.md", None),
    ("HEAD", "/api/reports/whatever.md", None),
    ("GET", "/api/feedback/stats", None),
]

# owner / 管理员必须**不被误伤**的只读端点（下游状态码不限，只要不是 401/403）
ALLOW_OWNER = [
    ("GET", f"/api/traces/threads/{MINE_TID}/events", None),
    ("GET", f"/api/threads/{MINE_TID}/run-status", None),
    ("GET", f"/api/threads/{MINE_TID}/feedback", None),
]
ALLOW_ADMIN = [
    ("GET", "/api/traces/tasks/task-abc/events", None),
    ("GET", "/api/feedback/stats", None),
    # 不含 /api/mcp/status：它进 handler 会真的去加载 MCP 子进程（本机无外网，
    # 要跑 2 轮×3s 重试），所以只验它的 403 分支即可。
]


async def main() -> int:
    app = build_app()
    seed_owner_rows()

    print("\n=== 1/4 未登录 + 外部来源 → 401（中间件层）===")
    for method, path, body in REFUSE_ANON:
        code = await probe(app, method, path, uid=None, ip=EXTERNAL_IP, json_body=body)
        check(code == 401, f"{method} {path} → 401", f"HTTP {code}")

    print("\n=== 2/4 已登录非 owner → 403（require_thread）===")
    for method, path, body in REFUSE_NONOWNER:
        code = await probe(app, method, path, uid=Z, json_body=body)
        check(code == 403, f"{method} {path} → 403", f"HTTP {code}")

    print("\n=== 3/4 已登录非管理员 → 403（require_admin）===")
    for method, path, body in REFUSE_NONADMIN:
        code = await probe(app, method, path, uid=Z, json_body=body)
        check(code == 403, f"{method} {path} → 403", f"HTTP {code}")

    print("\n=== 3b 登录即可的端点（非管理员也放行，非 401/403）===")
    for method, path, body in ALLOW_NONADMIN:
        code = await probe(app, method, path, uid=Z, json_body=body)
        check(code not in (401, 403), f"user {method} {path} → {code}（未被守卫拦）")

    print("\n=== 4/4 owner / 管理员不被误伤（只读端点，非 401/403）===")
    for method, path, body in ALLOW_OWNER:
        code = await probe(app, method, path, uid=OWNER, json_body=body)
        check(code not in (401, 403), f"owner {method} {path} → {code}（未被守卫拦）")
    for method, path, body in ALLOW_ADMIN:
        code = await probe(app, method, path, uid="admin", is_admin=True, json_body=body)
        check(code not in (401, 403), f"admin {method} {path} → {code}（未被守卫拦）")

    bad = [label for ok, label in results if not ok]
    print(f"\n=== 结果：{len(results) - len(bad)}/{len(results)} 通过 ===")
    for label in bad:
        print(f"  ✗ {label}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
