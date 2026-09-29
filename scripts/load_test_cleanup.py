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

**多用户压测请加 `--accounts load01,…,load06`**：admin 视图下 `classify` 只看 graph_id，
真人派出的子 agent 会话会被一起归类成待删；`--accounts` 改成**每个账号用自己的身份**搜自己的
会话（服务端归属过滤 ⇒ 结构性碰不到别人），且老形态（无 graph_id 且无 title）只列不删。

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


def user_client(uid: str) -> httpx.Client | None:
    """用**账号自己的身份**建 client（容器内手签）。归属过滤在服务端 ⇒ 天然只看得见自己的会话。"""
    from agent.auth.users import find_user, token_version_of

    rec = find_user(uid)
    if rec is None:
        return None
    token = sign_token(uid, uid, bool(rec.get("is_admin")), token_version=token_version_of(rec))
    return httpx.Client(
        base_url=BASE,
        headers={"Cookie": f"nl2sql_token={token}"},
        timeout=60.0,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
    )


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


def scoped_kind(t: dict) -> str:
    """`--accounts` 模式下的归类。**不能复用 `classify`**：它把「无 graph_id 且无 title」的老形态
    也归进「子 agent 会话」，于是老形态与真子会话混在一个桶里 —— 而这两者在收尾时的处置相反
    （前者只列不删）。这里的判据是**正面**的，认不出就单独一个桶。"""
    md = t.get("metadata") or {}
    title = str(md.get("title") or "")
    graph = str(md.get("graph_id") or "")
    if title.startswith("[压测]"):
        return "压测主会话"
    if graph == SUB_AGENT_GRAPH:
        return "子 agent 会话"
    if not graph and not title:
        return "老形态（只列不删）"
    return "真人/其他（不动）"


def sweep_scoped(uids: list[str], since: float, args: argparse.Namespace) -> int:
    """按账号收尾：每个账号**用自己的身份**搜自己的会话（服务端归属过滤 ⇒ 碰不到别人）。

    为什么多用户压测必须走这条：admin 视图能看到**所有人**的会话，而 `classify` 只看
    `metadata.graph_id` —— 真人会话里派出的子 agent 会话同样是 `nl2sql_agent`，落在时间窗内
    就会被一起删。子会话是内部草稿，但那是别人的数据，不该由压测脚本处置。

    另一处收紧：老形态兜底（无 graph_id 且无 title）在这里**不自动删**，只列出来等人确认 ——
    兜底判据本身就承认"认不出"，配上删除动作太危险（本仓的会话删除是不可逆的硬删）。
    """
    ok = dele = skip = 0
    ledger: dict[str, dict] = {}
    for uid in uids:
        cli = user_client(uid)
        if cli is None:
            print(f"[{uid}] ✗ 账号不存在，跳过")
            continue
        threads = [t for t in list_threads(cli, args.limit) if parse_ts(t.get("created_at")) >= since]
        buckets: dict[str, list[dict]] = {}
        for t in threads:
            buckets.setdefault(scoped_kind(t), []).append(t)
        ledger[uid] = {k: len(v) for k, v in buckets.items()}
        print(f"\n[{uid}] 时间窗内会话 {len(threads)}："
              + " / ".join(f"{k} {len(v)}" for k, v in buckets.items()))
        for t in threads[:4]:
            md = t.get("metadata") or {}
            print(f"    · {str(t.get('thread_id',''))[:8]} graph_id={str(md.get('graph_id') or '')!r} "
                  f"title={str(md.get('title') or '')[:28]!r}")

        targets = (buckets.get("压测主会话") or []) + (buckets.get("子 agent 会话") or [])
        for t in buckets.get("老形态（只列不删）") or []:
            skip += 1
            print(f"  [{uid}] 跳过 {str(t.get('thread_id',''))[:8]}：老形态（无 graph_id 且无 title），需人工确认")
        for t in targets:
            tid = t.get("thread_id") or ""
            runs = runs_of(cli, tid)
            if runs is None:
                skip += 1
                print(f"  [{uid}] 跳过 {tid[:8]}：读不到 runs")
                continue
            live = [r for r in runs if (r.get("status") or "") not in TERMINAL_RUN]
            if live:
                skip += 1
                print(f"  [{uid}] 跳过 {tid[:8]}：还有活跃 run {[str(r.get('run_id',''))[:8] for r in live]}")
                continue
            if not args.confirm:
                dele += 1
                continue
            d = cli.delete(f"/threads/{tid}")
            ok += d.status_code < 300
            dele += 1
            print(f"  [{uid}] {'✓' if d.status_code < 300 else '✗'} 删除 {tid[:8]}（{len(runs)} 个 run）HTTP {d.status_code}")

    print(f"\n可删/已删 {dele} 个（成功 {ok}） / 跳过 {skip} 个")
    if not args.confirm:
        print("（未给 --confirm：只台账，没删任何东西）")
    print("\n=== JSON ===")
    print(json.dumps({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "accounts",
        "accounts": ledger,
        "window_minutes": args.minutes,
        "targets": dele,
        "deleted_ok": ok,
        "skipped": skip,
        "confirmed_delete": bool(args.confirm),
    }, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="P2-7 压测收尾台账 / 子会话清理")
    ap.add_argument("--minutes", type=float, default=120.0, help="只看最近 N 分钟内创建的会话")
    ap.add_argument("--limit", type=int, default=400, help="最多拉多少个会话")
    ap.add_argument("--n-queries", type=int, default=0, help="压测总问数（给了才算「子会话/问数」）")
    ap.add_argument("--confirm", action="store_true", help="真的删除子 agent 会话 + 我的 [压测] 主会话（默认只台账）")
    ap.add_argument("--accounts", default="",
                    help="只清这些账号名下的会话（逗号分隔，**用各自身份**读/删）。多用户压测必须给："
                         "admin 视图下 `classify` 只看 graph_id，会把**真人**派出的子 agent 会话也归类成待删")
    args = ap.parse_args()

    since = time.time() - args.minutes * 60.0
    print(f"时间窗：最近 {args.minutes:.0f} 分钟（since={datetime.fromtimestamp(since, timezone.utc).isoformat(timespec='seconds')}）")

    scoped = [x.strip() for x in args.accounts.replace(" ", "").split(",") if x.strip()]
    if scoped:
        return sweep_scoped(scoped, since, args)

    client = admin_client()
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
