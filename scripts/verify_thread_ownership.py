# -*- coding: utf-8 -*-
"""P1-5 会话归属 fail-closed + P1-15 两套账本同写（离线，无需后端/数据库/网络）。

P1-5 把 `grants.owned_thread` 从「未登记就放行」改成「未登记就拒」。这个改动
**只有在写侧没有缺口时才安全**，所以本脚本验三件事：

  ① 账本语义（fail-closed）：未登记→拒、他人→拒、本人→放行、admin→放行、
     `legacy` 哨兵行→放行（必须与 ops 层 `owner_filter` 的 `$or` 分支一致，
     否则同一条会话会「列表里看得见、点进去 403」）。
  ② 缺口 1 —— 建会话钩子：`POST /threads` 原先只写 `metadata.owner`，
     grants 行要等**第一个 run** 才写 → 建了没跑的会话自己人会 403。
     现在 `@auth.on.threads.create` 两套账本一起写。
  ③ 缺口 2 —— 存量回填：`scripts/backfill_thread_owner.py` 的 `plan()` 分桶
     必须把「真实 owner / legacy / 无归属」分对，且**绝不改写已有归属行**
     （改写 = 能把别人的会话认领走）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_thread_ownership.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-threadown-"))

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

ALICE = "Z0051"
BOB = "Z9999"
ADMIN = {"user_id": "admin", "is_admin": True}
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── ① 账本 fail-closed ────────────────────────────────────────────────

def t1_ledger() -> None:
    section("① owned_thread fail-closed（含与 ops 层口径对齐）")
    from agent.auth import grants
    from agent.auth.ownership import LEGACY_OWNER, owner_filter

    alice = {"user_id": ALICE, "is_admin": False}
    bob = {"user_id": BOB, "is_admin": False}

    # 未登记：P1-5 的核心改动（旧行为是放行 —— 见下面的负对照）
    check(grants.owned_thread(alice, "never-registered") is False,
          "未登记会话 → 拒（fail-closed）")
    check(grants.owned_thread({"user_id": "", "is_admin": False}, "never-registered") is False,
          "无身份的调用者 → 拒")

    grants.claim_thread("t-alice", ALICE)
    grants.claim_thread("t-bob", BOB)
    grants.claim_thread("t-legacy", LEGACY_OWNER)
    check(grants.owned_thread(alice, "t-alice") is True, "本人会话 → 放行")
    check(grants.owned_thread(bob, "t-alice") is False, "他人会话 → 拒")
    check(grants.owned_thread(ADMIN, "t-alice") is True, "管理员 → 放行")
    check(grants.owned_thread(bob, "t-legacy") is True,
          "legacy 哨兵行 → 对所有人放行（与 owner_filter 的 $or 分支一致）")

    # 口径一致性：ops 层过滤器与本账本必须认同一件事
    f = owner_filter(ALICE)
    check(f == {"$or": [{"owner": ALICE}, {"owner": LEGACY_OWNER}]},
          "ops 层过滤器 = $or[本人, legacy]（两套账本口径锚在这里）", str(f))

    # 先到者胜：后到的 claim 不能改写归属（否则可抢占别人的会话）
    grants.claim_thread("t-alice", BOB)
    check(grants.owned_thread(bob, "t-alice") is False and
          grants.owned_thread(alice, "t-alice") is True,
          "claim_thread 先到者胜，后来者抢不走会话")


def t1b_negative_control() -> None:
    section("①b 负对照：旧行为（未登记放行）必须能被检出")
    from agent.auth import grants

    # 直接复现旧逻辑，证明「未登记」这件事本身在旧口径下就是放行
    def legacy_owned_thread(user: dict, thread_id: str) -> bool:
        if user.get("is_admin"):
            return True
        row_owner = None  # 未登记 → 旧代码 return True
        return True if row_owner is None else row_owner == user.get("user_id")

    alice = {"user_id": ALICE, "is_admin": False}
    check(legacy_owned_thread(alice, "any-unregistered") is True,
          "旧口径确实对未登记会话放行（= 新口径拒绝的东西）")
    check(grants.owned_thread(alice, "any-unregistered") is False,
          "新口径拒绝同一请求 → 断言不是恒真/恒假", "翻转成功")


# ── ② 建会话钩子同写两套账本 ──────────────────────────────────────────

def t2_create_hook() -> None:
    section("② @auth.on.threads.create 同时写 metadata 与 grants 账本")
    from agent.auth import backend, grants

    class _User:
        def __init__(self, identity):
            self.identity = identity

    class _Ctx:
        def __init__(self, identity, permissions=()):
            self.user = _User(identity)
            self.permissions = list(permissions)

    value = {"thread_id": "hook-t1", "metadata": {}}
    asyncio.run(backend._stamp_thread_owner(_Ctx(ALICE), value))
    check(value["metadata"].get("owner") == ALICE, "metadata.owner 写上登录身份")
    check(grants.owned_thread({"user_id": ALICE, "is_admin": False}, "hook-t1") is True,
          "grants 账本同写 → 建了还没跑 run 的会话自己人就能访问（缺口 1 堵住）")

    # 强制覆盖：请求方自己指定归属无效（否则能把会话塞进别人列表 / 认领成自己）
    forged = {"thread_id": "hook-t2", "metadata": {"owner": BOB}}
    asyncio.run(backend._stamp_thread_owner(_Ctx(ALICE), forged))
    check(forged["metadata"]["owner"] == ALICE, "客户端指定的 owner 被强制覆盖")
    check(grants.owned_thread({"user_id": BOB, "is_admin": False}, "hook-t2") is False,
          "伪造归属者拿不到该会话")

    # internal 身份不写（后端代用户写时归属已由调用方算好，不能覆盖成 internal）
    internal = {"thread_id": "hook-t3", "metadata": {}}
    asyncio.run(backend._stamp_thread_owner(_Ctx("internal"), internal))
    check(internal["metadata"].get("owner") is None, "internal 身份不写 metadata.owner")
    check(grants.owned_thread({"user_id": ALICE, "is_admin": False}, "hook-t3") is False,
          "internal 建的会话不记成任何真实用户（归属由调用方另行补）")


# ── ③ 回填脚本的分桶（缺口 2）────────────────────────────────────────

def t3_backfill() -> None:
    section("③ backfill_thread_owner.plan() 分桶")
    mod = load_module(_HERE.parents[1] / "scripts/backfill_thread_owner.py", "verify_backfill")

    threads = [
        {"thread_id": "a1", "metadata": {"owner": ALICE}},          # 真实用户 → 回填
        {"thread_id": "a2", "metadata": {"owner": ALICE}},          # 已一致 → 不动
        {"thread_id": "b1", "metadata": {"owner": BOB}},            # 真实用户 → 回填
        {"thread_id": "c1", "metadata": {"owner": "legacy"}},       # 公开存量 → 哨兵
        {"thread_id": "d1", "metadata": {}},                        # 无 owner → 不写
        {"thread_id": "d2", "metadata": {"owner": "internal"}},     # internal → 不写
        {"thread_id": "d3", "metadata": {"owner": "dev"}},          # dev → 不写
        {"thread_id": "d4"},                                        # 无 metadata → 不写
        {"thread_id": "e1", "metadata": {"owner": ALICE}},          # 账本记着 BOB → 冲突
    ]
    existing = {"a2": ALICE, "e1": BOB}
    p = mod.plan(threads, existing)

    claimed = dict(p["claim"])
    check(claimed == {"a1": ALICE, "b1": BOB}, "真实 owner 会话进回填桶", str(claimed))
    check(p["legacy"] == [("c1", "legacy")], "legacy 会话写哨兵行（保持公开）")
    check(p["ok"] == [("a2", ALICE)], "已一致的会话不重写")
    check([t for t, _, _ in p["conflict"]] == ["e1"], "账本≠metadata 的会话进冲突桶（不自动改）")
    check(sorted(p["orphan"]) == ["d1", "d2", "d3", "d4"],
          "无归属（缺失/空/internal/dev/无 metadata）全部归入 orphan", str(sorted(p["orphan"])))
    check(all(t not in claimed for t in ("d1", "d2", "d3", "d4")),
          "orphan 不会拿到归属行（= 不会被误认给任何人）")

    # 幂等 + 不改写：把回填结果当作已有账本再算一遍 → 全部落 ok
    after = {**existing, **claimed, "c1": "legacy"}
    p2 = mod.plan(threads, after)
    check(not p2["claim"] and not p2["legacy"],
          "回填结果再算一遍无新增（幂等，可重复跑）")
    check(p2["conflict"] == [("e1", BOB, ALICE)], "冲突项第二轮仍在冲突桶（不会被静默吞掉）")

    # 单条认领路径（人工迁移）
    db = pathlib.Path(os.environ["AGENT_DATA_ROOT"]) / "auth" / "auth.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS thread_owner "
        "(thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute("INSERT OR IGNORE INTO thread_owner VALUES ('x1', ?, 0)", (BOB,))
    conn.commit()
    conn.close()
    n = mod.apply_rows(str(db), [("x1", ALICE), ("x2", ALICE)])
    check(n == 1, "apply_rows 只新增 1 条（x1 已属 BOB，被 IGNORE）", f"新增={n}")
    owner = mod.load_existing(str(db))
    check(owner.get("x1") == BOB and owner.get("x2") == ALICE,
          "人工认领不会从别人手里夺走会话，新会话正常登记")


# ── ④ 端点：REST 层的 403 ─────────────────────────────────────────────

def t4_endpoint() -> None:
    section("④ require_thread 端点：未登记 → 403（原先是放行）")
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from api.auth_middleware import AuthMiddleware
    from api import thread_run_status
    from agent.auth import grants

    grants.register_user = lambda *a, **k: None  # 中间件放行后会写库，打桩掉
    from _auth_test_support import mint_for  # P1-12：verify_token 要求账号存在，先登记再签

    app = AuthMiddleware(Starlette(routes=list(thread_run_status.routes)))
    # 必须是合法 UUID：端点先做格式校验（400），归属校验在其之后
    mine = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    grants.claim_thread(mine, ALICE)

    def status(tid: str, uid: str) -> int:
        headers = {"Cookie": f"nl2sql_token={mint_for(uid)}",
                   "X-Forwarded-For": "1.2.3.4"}

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("172.18.0.9", 1234)),
                base_url="http://testserver",
            ) as c:
                return await c.get(f"/api/threads/{tid}/run-status", headers=headers)

        return asyncio.run(go()).status_code

    check(status(mine, ALICE) != 403, "本人已登记会话 → 守卫不拦（下游可 200/404）",
          str(status(mine, ALICE)))
    check(status(mine, BOB) == 403, "他人已登记会话 → 403（P1-2 之前就有这条）",
          str(status(mine, BOB)))
    code = status(other, ALICE)
    check(code == 403, "未登记会话 → 403（P1-5 前这里是放行）", str(code))


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    t1_ledger()
    t1b_negative_control()
    t2_create_hook()
    t3_backfill()
    t4_endpoint()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
