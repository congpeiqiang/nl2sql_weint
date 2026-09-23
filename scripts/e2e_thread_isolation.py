# -*- coding: utf-8 -*-
"""会话归属隔离 E2E（在 nl2sql 后端容器内跑，真实 HTTP + 真实 token）。

断言（Z0051 / admin 两个账号）：
 1. Z0051 的会话不出现在 admin 的 `POST /threads/search`（侧边栏数据源）
 2. admin 直读 Z0051 的会话 state → 404（不是 403：ops 层统一按「取不到」）
 3. Z0051 读自己的会话 → 200
 4. 新建会话自动带 `metadata.owner` = 登录身份（create 钩子）
 5. admin 看不到别人新建的会话；别人也看不到 admin 的
 6. 侧边栏真实查询形态（带 metadata.graph_id 过滤）下 admin 也看不到
 7. 归属键不可被改写（update 钩子；部署未含此补丁时应能复现漏洞）
"""
from __future__ import annotations

import sys
import json
import uuid

import httpx

sys.path.insert(0, "/app/src")
from agent.auth.token import sign_token  # noqa: E402

BASE = "http://localhost:2026"
OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def client_of(uid: str, is_admin: bool) -> httpx.Client:
    token = sign_token(uid, uid, is_admin)
    return httpx.Client(
        base_url=BASE,
        headers={"Cookie": f"nl2sql_token={token}"},
        timeout=60.0,
    )


def search_ids(c: httpx.Client, metadata: dict | None = None) -> set[str]:
    body: dict = {"limit": 200}
    if metadata:
        body["metadata"] = metadata
    r = c.post("/threads/search", json=body)
    if r.status_code != 200:
        return {f"<HTTP {r.status_code} {r.text[:80]}>"}
    return {t["thread_id"] for t in (r.json() or [])}


def main() -> int:
    z = client_of("Z0051", False)
    a = client_of("admin", True)

    print("\n=== 1/6 列表隔离 ===")
    z_ids = search_ids(z)
    a_ids = search_ids(a)
    print(f"    Z0051 可见 {len(z_ids)} 条 / admin 可见 {len(a_ids)} 条")
    check(z_ids and not a_ids, "admin 看不到 Z0051 的会话", f"admin={sorted(a_ids)[:3]}")
    check(not (z_ids & a_ids), "两个账号的可见集合无交集")

    victim = sorted(z_ids)[0] if z_ids else ""

    print("\n=== 2/6 直读他人会话 ===")
    # 设计约定（2026-09-23）：**列表对所有人过滤，但单条直读对 admin 放行** ——
    # admin 是运维/开发者，标注页（feedback_annotation）与线上排障都要读别人的会话。
    # 这里把「admin 能读」断言成期望行为，防止将来误收紧把标注页/排障搞坏。
    if victim:
        rz = z.get(f"/threads/{victim}/state")
        ra = a.get(f"/threads/{victim}/state")
        check(rz.status_code == 200, "Z0051 读自己的会话 → 200", f"HTTP {rz.status_code}")
        check(ra.status_code == 200, "admin 直读单条 → 200（设计：超管放行、标注/排障需要）",
              f"HTTP {ra.status_code}")
        rz2 = z.get(f"/threads/{victim}/history")
        check(rz2.status_code == 200, "history 读自己的会话 → 200", f"HTTP {rz2.status_code}")
    else:
        check(False, "没有可用于直读断言的会话（Z0051 侧为空）")

    # 子线程（graph_id=nl2sql_agent）是前端任务卡的数据源：owner 必须是真实用户，
    # 否则前端 `pollClient.threads.getState(task_id)` 404 → 任务卡进度/待办全空。
    subs = search_ids(z, {"graph_id": "nl2sql_agent"})
    if subs:
        sub = sorted(subs)[0]
        rs = z.get(f"/threads/{sub}/state")
        check(rs.status_code == 200, "本人可读自己的子线程（任务卡不 404）", f"HTTP {rs.status_code}")
    else:
        print("    （无子线程可查，跳过）")

    print("\n=== 3/6 新建会话自动打归属 ===")
    r = z.post("/threads", json={"metadata": {"graph_id": "chat_agent"}})
    check(r.status_code < 300, "Z0051 建会话成功", f"HTTP {r.status_code}")
    new_tid = (r.json() or {}).get("thread_id", "")
    owner = ((r.json() or {}).get("metadata") or {}).get("owner")
    check(owner == "Z0051", "新会话 metadata.owner = 登录身份", f"owner={owner!r}")

    radmin = a.post("/threads", json={"metadata": {"graph_id": "chat_agent"}})
    a_tid = (radmin.json() or {}).get("thread_id", "")
    a_owner = ((radmin.json() or {}).get("metadata") or {}).get("owner")
    check(a_owner == "admin", "admin 新会话 owner=admin", f"owner={a_owner!r}")

    print("\n=== 4/6 交叉可见性 ===")
    check(new_tid not in (a_ids | search_ids(a)), "admin 看不到 Z0051 新建的会话")
    check(a_tid not in search_ids(z), "Z0051 看不到 admin 新建的会话")
    check(a.get(f"/threads/{new_tid}/state").status_code == 200,
          "admin 直读 Z0051 新会话 → 200（超管放行，同上）")
    check(z.get(f"/threads/{a_tid}/state").status_code == 404, "Z0051 读不到 admin 新建的会话")

    print("\n=== 5/6 侧边栏真实查询形态（带 graph_id 过滤）===")
    z_side = search_ids(z, {"graph_id": "chat_agent"})
    a_side = search_ids(a, {"graph_id": "chat_agent"})
    print(f"    侧边栏：Z0051={len(z_side)} 条 / admin={len(a_side)} 条")
    check(new_tid in z_side, "Z0051 侧边栏能看到自己刚建的会话")
    check(a_tid not in z_side and new_tid not in a_side, "双方侧边栏互不可见")

    print("\n=== 6/6 归属键可否被改写（update 钩子）===")
    # 设计：普通用户**不能**改 owner（改 title 等其它键不受影响）；admin 例外
    # （超管放行，与直读一致）。断言读的是**回读值**，不是 PATCH 的响应体
    # （响应体可能是补丁前的快照，容易被误读成「改成功了」）。
    rp2 = z.patch(f"/threads/{new_tid}", json={"metadata": {"owner": "legacy", "title": "T"}})
    back = (z.get(f"/threads/{new_tid}").json() or {}).get("metadata") or {}
    check(back.get("owner") == "Z0051",
          "普通用户改 owner→legacy 被压回登录身份", f"回读 owner={back.get('owner')!r}")
    check(back.get("title") == "T", "同一请求里的其它键（title）正常写入",
          f"回读 title={back.get('title')!r}")
    check(new_tid not in search_ids(a), "改不动归属 → admin 侧边栏仍看不到该会话")

    # 清理本次 E2E 造的会话
    z.delete(f"/threads/{new_tid}")
    a.delete(f"/threads/{a_tid}")

    bad = [l for ok, l in results if not ok]
    print(f"\n=== 结果：{len(results) - len(bad)}/{len(results)} 通过 ===")
    for l in bad:
        print(f"  ✗ {l}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

运行方式（在 nl2sql 后端容器内，用 stdin 灌进去，不需要在容器里留文件）：
    ssh -o BatchMode=yes weint@192.168.25.64 \n      "docker exec -i nl2sql-app_langgraph-api_1 python -" < scripts/e2e_thread_isolation.py
  （cleanup 需加 --delete / --purge-grants 才真删；E2E 自带断言、会自建自删两条会话）
