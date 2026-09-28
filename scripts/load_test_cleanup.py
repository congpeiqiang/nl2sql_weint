# -*- coding: utf-8 -*-
"""P2-7 压测的收尾台账 + 清理（**容器内运行**）。

为什么需要它：一次问数除了我建的主会话，**deepagents 还会另建一个子 agent 会话**
（`deepagents/middleware/async_subagents.py:297` 的 `client.threads.create()`，无 metadata、
thread_id 即 task_id）。压测脚本只删自己 `POST /threads` 建的主会话，**子会话会留在生产库里**
—— 那既是残留垃圾，又正好是评估报告 §5.3 里点名「还没实测」的那个量：
**「一次问数实际开几个子任务」**。所以这个脚本一次做两件事：

  1. **台账（只读，默认行为）**：按 `created_at` 时间窗把会话分组
     —— 我建的（title 带 `[压测]`）/ **子 agent 会话**（`metadata.graph_id == 'nl2sql_agent'`）/ 其他（真人会话）。
     并统计「子会话数 ÷ 问数」≈ 一次问数实际开几个子任务。
  2. **清理（须 `--confirm`）**：删「子 agent 会话」与我建的「`[压测]` 主会话」，且**逐条过安全闸**：
     该会话下所有 run 都已终态（绝不删正在跑的）＋ 落在时间窗内。

跑法：
    ssh -o BatchMode=yes weint@192.168.25.64 \\
      "docker exec -i <容器> /app/.venv/bin/python - --minutes 120" \\
      < scripts/load_test_cleanup.py
    # 确认台账无误后再加 --confirm 真删

⚠️ 子会话的判据 = **`metadata.graph_id`**（2026-09-24 实测修正）：老判据写成「无 graph_id
且无 title」是**假阴性** —— 子会话其实**带** `graph_id='nl2sql_agent'`（主会话是 `'chat_agent'`
且带 title），结果 22 个压测子会话被误算成「真人会话」、台账报「疑似子 agent: 0」。
所以台账会把每一类都打样例出来给人看，不要只看计数就删。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, "/app/src")
from agent.auth.token import sign_token  # noqa: E402

BASE = "http://localhost:2026"
TERMINAL_RUN = {"success", "error", "timeout", "interrupted", "cancelled"}


def admin_client() -> httpx.Client:
    """手签一次 admin token 当读/删的凭据（容器内跑，`/app/src` 就是权威代码）。"""
    from agent.auth.users import find_user, token_version_of, load_users

    who = ""
    for u in load_users():
        if u.get("is_admin"):
            who = u.get("user_id") or ""
            break
    if not who:
        raise SystemExit("✗ 没有 admin 账号，退出")
    version = 0
    rec = find_user(who)
    if rec is not None:
        version = token_version_of(rec)
    token = sign_token(who, who, True, token_version=version)
    return httpx.Client(
        base_url=BASE,
        headers={"Cookie": f"nl2sql_token={token}"},
        timeout=60.0,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
    )


def list_threads(client: httpx.Client, limit: int) -> list[dict]:
    """按创建时间倒序列出会话（admin 身份，归属过滤在服务端）。分页拉全。"""
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        r = client.post(
            "/threads/search",
            json={"limit": min(100, limit - len(out)), "offset": offset,
                  "sort_by": "created_at", "sort_order": "desc"},
        )
        if r.status_code >= 300:
            raise SystemExit(f"✗ /threads/search HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        page = body if isinstance(body, list) else (body.get("threads") or body.get("items") or [])
        if not page:
            break
        out.extend(page)
        offset += len(page)
    return out


def parse_ts(v) -> float:
    """会话的 created_at 可能是 ISO 串或 epoch；两种都收，认不出返回 0。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


SUB_AGENT_GRAPH = "nl2sql_agent"  # deepagents 异步子 agent 建的会话用的 graph 名
MAIN_GRAPH = "chat_agent"


def classify(t: dict) -> str:
    """会话归类。

    子会话的权威判据是 ``metadata.graph_id`` —— deepagents 的异步子 agent 走
    ``client.threads.create()`` 建的会话**会带** ``graph_id='nl2sql_agent'``、但**没有 title**。
    2026-09-24 实测：判据写成「无 graph_id 且无 title」会**假阴性**（子会话明明带 graph_id），
    22 个压测子会话被误算成「真人会话」。这里改成正面判据 + 老形态兜底。
    """
    md = t.get("metadata") or {}
    title = str(md.get("title") or "")
    graph = str(md.get("graph_id") or "")
    if title.startswith("[压测]"):
        return "我的压测主会话"
    if graph == SUB_AGENT_GRAPH:
        return "子 agent 会话"
    if not graph and not title:
        return "子 agent 会话"  # 兜底：无 metadata 的老形态（可能是子会话）
    return "其他（真人会话）"


def runs_of(client: httpx.Client, tid: str) -> list[dict] | None:
    r = client.get(f"/threads/{tid}/runs")
    if r.status_code >= 300:
        return None
    body = r.json()
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        runs = body.get("runs") or body.get("items") or []
        return runs if isinstance(runs, list) else None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="P2-7 压测收尾台账 / 子会话清理")
    ap.add_argument("--minutes", type=float, default=120.0, help="只看最近 N 分钟内创建的会话")
    ap.add_argument("--limit", type=int, default=400, help="最多拉多少个会话")
    ap.add_argument("--n-queries", type=int, default=0, help="压测总问数（给了才算「子会话/问数」）")
    ap.add_argument("--confirm", action="store_true", help="真的删除子 agent 会话 + 我的 [压测] 主会话（默认只台账）")
    args = ap.parse_args()

    client = admin_client()
    since = time.time() - args.minutes * 60.0
    print(f"时间窗：最近 {args.minutes:.0f} 分钟（since={datetime.fromtimestamp(since, timezone.utc).isoformat(timespec='seconds')}）")

    all_threads = list_threads(client, args.limit)
    in_window = [t for t in all_threads if parse_ts(t.get("created_at")) >= since]
    buckets: dict[str, list[dict]] = {}
    for t in in_window:
        buckets.setdefault(classify(t), []).append(t)

    print(f"\n总会话 {len(all_threads)}，时间窗内 {len(in_window)}")
    for k in ("我的压测主会话", "子 agent 会话", "其他（真人会话）"):
        rows = buckets.get(k) or []
        print(f"  {k}: {len(rows)}")
        for t in rows[:3]:
            md = t.get("metadata") or {}
            print(
                f"    · {t.get('thread_id','')[:8]} status={t.get('status')!r} "
                f"created={t.get('created_at')} graph_id={str(md.get('graph_id') or '')!r} "
                f"title={str(md.get('title') or '')[:30]!r}"
            )

    subs = (buckets.get("子 agent 会话") or []) + (buckets.get("我的压测主会话") or [])
    if args.n_queries:
        print(f"\n子会话/问数 ≈ {len(subs)}/{args.n_queries} = {len(subs) / args.n_queries:.2f} 个（含未被时间窗/分页漏掉的）")

    # 逐条安全闸 + 可选删除
    deletable, skipped = [], []
    for t in subs:
        tid = t.get("thread_id") or ""
        runs = runs_of(client, tid)
        if runs is None:
            skipped.append((tid, "读不到 runs"))
            continue
        live = [r for r in runs if (r.get("status") or "") not in TERMINAL_RUN]
        if live:
            skipped.append((tid, f"还有活跃 run：{[r.get('run_id','')[:8] for r in live]}"))
            continue
        deletable.append((tid, len(runs)))
        if not args.confirm:
            continue
        d = client.delete(f"/threads/{tid}")
        print(f"  {'✓' if d.status_code < 300 else '✗'} 删除 {tid[:8]}（{len(runs)} 个 run）HTTP {d.status_code}")

    print(f"\n可删 {len(deletable)} 个 / 跳过 {len(skipped)} 个")
    for tid, why in skipped:
        print(f"  跳过 {tid[:8]}：{why}")
    if not args.confirm:
        print("\n（未给 --confirm：只台账，没删任何东西）")
    print("\n=== JSON ===")
    print(json.dumps({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_minutes": args.minutes,
        "total_threads": len(all_threads),
        "in_window": len(in_window),
        "counts": {k: len(v) for k, v in buckets.items()},
        "sub_threads": len(subs),
        "sub_runs": {tid: n for tid, n in deletable},
        "sub_runs_total": sum(n for _, n in deletable),
        "deletable": len(deletable),
        "skipped": len(skipped),
        "confirmed_delete": bool(args.confirm),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
