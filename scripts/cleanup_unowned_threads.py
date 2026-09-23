# -*- coding: utf-8 -*-
"""清理「没有归属人」的存量会话（2026-09-23 用户拍板：已有的会话可以都删除）。

背景：`@auth.on.threads.search` 的归属过滤器对「metadata 里没有 owner 键」是
**fail-closed**（inmem `_check_filter_match`：key 不在 metadata 直接 False），
所以钩子上线后，登录体系（2026-09-21 P0）之前/期间产生的、没有 owner 的会话
会变成「谁都看不到」。要么回填、要么删掉——用户选删掉。

判定「没有归属人」= `metadata.owner` 缺失 / 空 / `internal`（internal 是「调用来自
容器内部」的标记，不是人）。新代码上线后建的会话都会被 create 钩子打上真实登录
身份，所以这个脚本天然只命中存量，不会误删新会话。

用法（在后端容器内跑）：
    python cleanup_unowned_threads.py                 # 只统计，不删（默认）
    python cleanup_unowned_threads.py --delete        # 真删
    python cleanup_unowned_threads.py --delete --purge-grants   # 连带清 auth.sqlite 里的孤儿行

删除后建议重启一次容器再复查（inmem 运行时的 thread 存在内存 + `.langgraph_ops.pckl`
镜像里，重启能验证删除是否真的落到了镜像）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter

import httpx

SUB_GRAPH = "nl2sql_agent"
PAGE = 100
NON_USER = {"", "internal", "dev"}


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


def _db_path() -> str:
    root = os.getenv("AGENT_DATA_ROOT", "")
    if root:
        return os.path.join(root, "auth", "auth.sqlite")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auth", "auth.sqlite")


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true", help="真删（默认只统计）")
    ap.add_argument("--purge-grants", action="store_true", help="连带清 auth.sqlite 孤儿行")
    ap.add_argument("--keep-graph", default="", help="保留某 graph_id 的会话（默认全删）")
    args = ap.parse_args()

    base = _base_url()
    print(f"[cleanup] api={base}")
    with httpx.Client(base_url=base, timeout=httpx.Timeout(120.0, connect=10.0)) as http:
        threads = list_threads(http)
        print(f"[cleanup] 线程总数={len(threads)}")

        targets, kept = [], []
        for t in threads:
            tid = t["thread_id"]
            md = t.get("metadata") or {}
            owner = md.get("owner")
            graph = md.get("graph_id") or "(none)"
            if isinstance(owner, str) and owner not in NON_USER:
                kept.append((tid, owner))
                continue
            if args.keep_graph and graph == args.keep_graph:
                kept.append((tid, owner or "(no owner)"))
                continue
            targets.append(t)

        print("\n=== 待删（按 graph_id × 现有 owner）===")
        for (g, o), n in sorted(
            Counter(
                ((t.get("metadata") or {}).get("graph_id") or "(none)",
                 (t.get("metadata") or {}).get("owner") or "(无 owner)")
                for t in targets
            ).items(),
            key=lambda kv: -kv[1],
        ):
            print(f"  {g:<18} owner={o:<10} {n}")
        print(f"\n  待删合计={len(targets)}  保留={len(kept)}")

        if not args.delete:
            print("\n[dry-run] 未删除。加 --delete 执行。")
            return 0

        ok = fail = 0
        for t in targets:
            tid = t["thread_id"]
            r = http.delete(f"/threads/{tid}")
            if r.status_code < 300:
                ok += 1
            else:
                fail += 1
                print(f"    !! DELETE 失败 {tid}: HTTP {r.status_code} {r.text[:160]}")
        print(f"\n[cleanup] DELETE 成功={ok} 失败={fail}")

        left = len(list_threads(http))
        print(f"[cleanup] 删除后剩余线程={left}（期望=保留数 {len(kept)}）")

    if args.purge_grants:
        db = _db_path()
        if not os.path.exists(db):
            print(f"[cleanup] grants db 不存在，跳过：{db}")
            return 0
        alive = {tid for tid, _ in kept}  # kept 是 (thread_id, owner) 元组
        conn = sqlite3.connect(db, timeout=30)
        dead_owner = [
            r[0] for r in conn.execute("SELECT thread_id FROM thread_owner")
            if r[0] not in alive
        ]
        dead_db = [
            r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM thread_db")
            if r[0] not in alive
        ]
        conn.executemany("DELETE FROM thread_owner WHERE thread_id=?", [(t,) for t in dead_owner])
        conn.executemany("DELETE FROM thread_db WHERE thread_id=?", [(t,) for t in dead_db])
        conn.commit()
        conn.close()
        print(f"[cleanup] 清 thread_owner={len(dead_owner)} thread_db={len(dead_db)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

运行方式（在 nl2sql 后端容器内，用 stdin 灌进去，不需要在容器里留文件）：
    ssh -o BatchMode=yes weint@192.168.25.64 \n      "docker exec -i nl2sql-app_langgraph-api_1 python -" < scripts/cleanup_unowned_threads.py
  （cleanup 需加 --delete / --purge-grants 才真删；E2E 自带断言、会自建自删两条会话）
