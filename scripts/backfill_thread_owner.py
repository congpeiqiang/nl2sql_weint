# -*- coding: utf-8 -*-
"""回填 / 认领会话归属（P1-5 的迁移路径，2026-09-23）。

背景：`grants.owned_thread` 已从 **fail-open**（未登记就放行）改成 **fail-closed**。
这要求「能看见的会话 = 有归属行」，而归属行原先只在**建 run 时**才写，于是有两类缺口：
  · 建了但还没跑过 run 的会话（`@auth.on.threads.create` 钩子已补写，新会话不再有缺口）；
  · 09-22 之前建的老会话（本次必须回填）。
不补就跑 fail-closed，用户会「侧边栏看得见自己的会话，点进去 trace/export/反馈全 403」——
因为可见性由 thread 的 `metadata.owner`（ops 层过滤器）决定，而 REST 层看的是本账本。

判定与动作（`metadata.owner` 是权威来源）：
  <真实 user_id>  → 写 `thread_owner` 行（INSERT OR IGNORE，**绝不改写已有行**）
  `legacy`        → 写 `legacy` 哨兵行（保持「对所有人可见」，与 ops 层 owner_filter 一致）
  缺失/空/internal/dev → **不写**（这类会话两套账本都看不见，属存量空壳）。
                        默认只报告，删除请用 `scripts/cleanup_unowned_threads.py --delete`

用法（默认 dry-run，不加 --apply 不写库）：
    python backfill_thread_owner.py                     # 只统计，打印计划
    python backfill_thread_owner.py --apply             # 真回填
    python backfill_thread_owner.py --claim <tid> --user <uid>   # 单条认领（人工迁移）
    python backfill_thread_owner.py --list --orphans    # 列出无归属会话的 tid

**部署顺序硬要求：先 --apply 回填，再发版 fail-closed 的代码**；回滚 = 把
`owned_thread` 的 `row is None → False` 改回 `True`（本脚本只增不删，回滚无需清理）。

容器内跑法（无需在容器里留文件）：
    ssh -o BatchMode=yes weint@<host> \
      "docker exec -i nl2sql-app_langgraph-api_1 python -" < scripts/backfill_thread_owner.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter

import httpx

PAGE = 100
# 与 auth/ownership.py 的 NON_USER_IDENTITIES / LEGACY_OWNER 保持一致
NON_USER = {"", "internal", "dev"}
LEGACY = "legacy"


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


def _db_path() -> str:
    root = os.getenv("AGENT_DATA_ROOT", "")
    if root:
        return os.path.join(root, "auth", "auth.sqlite")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auth", "auth.sqlite"
    )


def list_threads(http: httpx.Client) -> list[dict]:
    out, offset = [], 0
    while True:
        r = http.post("/threads/search", json={"limit": PAGE, "offset": offset})
        r.raise_for_status()
        batch = r.json() or []
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        offset += PAGE


def load_existing(db: str) -> dict[str, str]:
    """已登记的 thread_id → user_id（库不存在 = 空）。"""
    if not os.path.exists(db):
        return {}
    conn = sqlite3.connect(db, timeout=30)
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT thread_id, user_id FROM thread_owner")}
    finally:
        conn.close()


def plan(threads: list[dict], existing: dict[str, str]) -> dict[str, list]:
    """纯函数：把线程列表折算成「要写什么」。返回分桶，便于离线单测。

    桶：claim=[(tid, uid)]  legacy=[(tid, LEGACY)]  ok=[(tid, uid)]（已一致）
        conflict=[(tid, 已有, metadata)]  orphan=[tid]
    """
    out: dict[str, list] = {"claim": [], "legacy": [], "ok": [], "conflict": [], "orphan": []}
    for t in threads:
        tid = t.get("thread_id")
        if not tid:
            continue
        md = t.get("metadata") or {}
        owner = md.get("owner")
        owner = owner if isinstance(owner, str) else ""
        if owner == LEGACY:
            target = LEGACY
        elif owner and owner not in NON_USER:
            target = owner
        else:
            out["orphan"].append(tid)
            continue
        have = existing.get(tid, "")
        if not have:
            out["claim" if target != LEGACY else "legacy"].append((tid, target))
        elif have == target:
            out["ok"].append((tid, target))
        else:
            # 已有归属行 ≠ metadata.owner：不自动改写（改写=可能夺走别人的会话），
            # 列出来人工判断。常见成因：会话被 fork/转移，或早期 claim 记了别人。
            out["conflict"].append((tid, have, target))
    return out


def apply_rows(db: str, rows: list[tuple[str, str]]) -> int:
    """INSERT OR IGNORE 写入（先到者胜，绝不改写已有行）。返回实际新增条数。

    新增条数用「写前写后计数差」而不是 `cursor.rowcount`：`executemany` +
    `OR IGNORE` 的 rowcount 在不同 sqlite3 版本上语义不一致，数差是确定的口径。
    """
    if not rows:
        return 0
    conn = sqlite3.connect(db, timeout=30)
    try:
        before = conn.execute("SELECT COUNT(*) FROM thread_owner").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO thread_owner (thread_id, user_id, created_at) VALUES (?, ?, ?)",
            [(tid, uid, time.time()) for tid, uid in rows],
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM thread_owner").fetchone()[0]
        return after - before
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认只打印计划）")
    ap.add_argument("--claim", default="", help="单条认领：会话 id")
    ap.add_argument("--user", default="", help="单条认领：归属用户 id")
    ap.add_argument("--list", action="store_true", help="打印明细 tid（默认只打聚合）")
    ap.add_argument("--orphans", action="store_true", help="只处理/列出无归属会话")
    args = ap.parse_args()

    db = _db_path()
    print(f"[backfill] api={_base_url()}")
    print(f"[backfill] db={db}")

    # ── 单条认领（人工迁移路径，不依赖 metadata）──
    if args.claim or args.user:
        if not (args.claim and args.user):
            print("[backfill] --claim 与 --user 必须同时给")
            return 2
        n = apply_rows(db, [(args.claim, args.user)])
        # n==0 可能是「已存在相同行」或「已存在别人的行」——都说明写入被 IGNORE 了
        existing = load_existing(db).get(args.claim, "")
        print(f"[backfill] 认领 {args.claim} → {args.user}：新增={n} 当前归属={existing or '(无)'}")
        if n == 0 and existing != args.user:
            print("[backfill] ⚠️ 该会话已被登记为别人，未改写（要转移请先确诊，再手工 SQL）")
        return 0

    with httpx.Client(base_url=_base_url(), timeout=httpx.Timeout(120.0, connect=10.0)) as http:
        threads = list_threads(http)
    print(f"[backfill] 线程总数={len(threads)}")

    existing = load_existing(db)
    buckets = plan(threads, existing)

    if args.orphans and args.list:
        for tid in buckets["orphan"]:
            print(f"  orphan {tid}")
    print("\n=== 计划 ===")
    print(f"  需回填（真实用户，metadata 有 owner）   {len(buckets['claim']):>6}")
    print(f"  需回填（legacy 公开哨兵）              {len(buckets['legacy']):>6}")
    print(f"  已一致（无需动作）                     {len(buckets['ok']):>6}")
    print(f"  冲突（账本≠metadata，人工判断）        {len(buckets['conflict']):>6}")
    print(f"  无归属（缺失/空/internal/dev）         {len(buckets['orphan']):>6}")

    if buckets["claim"]:
        by_user = Counter(uid for _, uid in buckets["claim"])
        print("\n  待回填明细（按用户）：")
        for uid, n in by_user.most_common(20):
            print(f"    {uid:<16} {n}")
        if args.list:
            for tid, uid in buckets["claim"]:
                print(f"      {tid} → {uid}")

    if buckets["conflict"]:
        print("\n  ⚠️ 冲突明细（脚本不动它们）：")
        for tid, have, want in buckets["conflict"][:50]:
            print(f"    {tid}  账本={have}  metadata={want}")

    if buckets["orphan"]:
        print(
            f"\n  无归属 {len(buckets['orphan'])} 条：两套账本都看不见它们（ops 层过滤缺 owner 键 → 不匹配）。\n"
            "  要清理：python scripts/cleanup_unowned_threads.py --delete --purge-grants"
        )

    rows = buckets["claim"] + buckets["legacy"]
    if not args.apply:
        print(f"\n[dry-run] 未写库。加 --apply 回填 {len(rows)} 条。")
        return 0

    written = apply_rows(db, rows)
    after = load_existing(db)
    print(
        f"\n[backfill] 写入完成：计划 {len(rows)} 条，实际新增 {written} 条"
        f"（差额=已有归属行被 IGNORE），库内归属行总数={len(after)}"
    )
    if buckets["orphan"]:
        print(f"[backfill] 仍未登记 {len(buckets['orphan'])} 条（无归属会话）→ 这些会话 REST 层将 403")
    return 0


if __name__ == "__main__":
    sys.exit(main())
