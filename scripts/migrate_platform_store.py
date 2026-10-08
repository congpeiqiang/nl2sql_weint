# -*- coding: utf-8 -*-
"""平台存储搬迁：inmem pckl → postgres 平台库（蓝绿阶段 1，2026-10-05）。

**背景**：`LANGGRAPH_RUNTIME_EDITION=inmem` 下，平台存储（threads / runs /
assistants / crons）是**每容器一份 pckl**（`.langgraph_api/.langgraph_ops.pckl`，
inmem 每 10s 整份覆盖写，last-writer-wins）⇒ **两个实例并存必然互相抹数据**，
所以蓝绿的前置动作是把它换成 postgres 版型（与已在 postgres 的会话正文
`nl2sql_checkpoint` 分库）。本脚本负责**搬迁其中唯一不能自动重建的东西：
threads 与其 metadata**。

**为什么只有 threads 要迁**（其余三类都有自愈路径）：
  · assistants —— 助手 = 图名 `chat_agent`，新实例启动时 `if_exists=do_nothing`
    自动建（`langgraph_api/graph.py`）；
  · crons —— 本仓未用；
  · runs —— 不迁（前端对历史 run 退化成「轮询 state + 继续按钮」，已知降级）；
  · **threads 的 `metadata.owner` 是硬授权过滤键**，丢了它 = 所有非 admin 用户
    侧边栏同时清空（过滤器表达不了「键不存在」）。这是本脚本存在的唯一理由。

**四段**（默认 dry-run：只打印计划，不写任何东西；加 `--apply` 才真写）：
  ① 离线读 pckl（`pickle.load`，**只读**）—— 不起服务、不共享挂载。
     ⚠️ 必须在**旧实例已停**之后跑：inmem 每 10s 整份覆盖，双进程同挂 = 双向丢数据。
     ⚠️ 直接 unpickle 会报 `KeyError: Config 'REDIS_URI' is missing` —— 见
     `_ensure_readable_env()`（先注入 langgraph config 需要的 env 再 load）。
  ② 对账预扫：`POST /threads/search` 翻页取**新实例已有**的 threads（含 metadata）。
  ③ 逐条补建：pckl 里有、新实例没有的 ⇒ `POST /threads {"if_exists": "do_nothing"}`。
     **走内部身份**（不带 token、不带 X-Forwarded-For）—— 只有内部身份下
     `@auth.on.threads.create` 的 `_stamp_thread_owner` 才**原样保留** payload 里的
     `owner`（非内部身份会被强制改写成调用者）。
 ④ 对账补齐：`SELECT DISTINCT thread_id FROM checkpoints`（真实全集）+ FTS
     `thread_text`（title）→ 找出「两边都缺」的线程补建。**归属三层取**：grants 账本
     `thread_owner(thread_id,user_id)`（最可靠）→ FTS 的 owner 列（该版本实测已无此列）
     → 都没有就**跳过**（不贴 `legacy`：那是公开哨兵，会让别人的会话人人可见，
     而它们在旧实例里本来就看不见 ⇒ 跳过才维持现状）。
  ⑤ `--fixup-timestamps`（默认开）：POST 接口改不了 `created_at/updated_at/
     state_updated_at`（`ThreadCreate` 里没有这三个字段，Go core 建行时填 now()）
     ⇒ 侧边栏按时间分组会全挤到"迁移那一刻"。本段用 pckl 里的真值直写平台库
     （列名运行期探测，缺列即跳过并明确报告）。

**归属账本（grants SQLite）不归本脚本管**：它在 agent_data 卷里（
`<AGENT_DATA_ROOT>/auth/auth.sqlite`），**不随平台存储切换**，老会话的行早就写过。
跑完本脚本后按需对账（现成脚本，幂等 INSERT OR IGNORE，先 dry-run）：
    python scripts/backfill_thread_owner.py            # 只看计划
    python scripts/backfill_thread_owner.py --apply    # 真回填

用法（容器内；不加 --apply 即 dry-run）：
    docker exec -i nl2sql-app_langgraph-api_1 \\
        python - < scripts/migrate_platform_store.py --apply

回滚：本脚本只**增**不改 —— 写入的都是新实例的库（旧 pckl 一字未动、卷保留），
回滚 = 把 compose 切回 inmem 重启（threads 全在，时间戳原样）。
"""
from __future__ import annotations

import argparse
import os
import pickle
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PCKL = ".langgraph_api/.langgraph_ops.pckl"  # 相对容器 WORKDIR=/app
LEGACY = "legacy"
# 与 auth/ownership.py 的 NON_USER_IDENTITIES 保持一致：这类 owner 不能当"可见性键"
NON_USER = {"", "internal", "dev"}


# ── ① 离线读 pckl ──────────────────────────────────────

def _ensure_readable_env() -> None:
    """unpickle 时会 import langgraph 的类，而 `langgraph_api.config` 在 import 期
    就校验必填项（缺 `REDIS_URI` 直接 KeyError）。这里只补**读文件**所需的最小集，
    不碰真实运行配置（容器里这些 env 本来就有，这里只兜底裸机执行）。"""
    os.environ.setdefault("REDIS_URI", "redis://localhost:6379/0")
    os.environ.setdefault("MIGRATIONS_PATH", "/storage/migrations")


def _as_dict(obj: Any) -> dict[str, Any]:
    """pckl 里的值可能是 dict，也可能是 dataclass/pydantic 对象（取决于 runtime 版本）。"""
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    d = getattr(obj, "__dict__", None)
    return d if isinstance(d, dict) else {}


def _iso(value: Any) -> str:
    """时间戳统一成 ISO 串，便于打印与比对。"""
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def read_pckl(path: Path) -> dict[str, Any]:
    """离线读平台存储 pckl。返回 {'threads': {tid: {...}}, ...}。

    只 `pickle.load`，不实例化任何 runtime、不启动服务。
    """
    if not path.exists():
        raise FileNotFoundError(
            f"读不到 pckl: {path}\n"
            "  · 旧实例确实用 inmem 跑过？(LANGGRAPH_RUNTIME_EDITION=inmem 未设即 inmem)\n"
            "  · 卷 nl2sql_api_meta 挂在 /app/.langgraph_api？换个 --pckl 指过去"
        )
    with path.open("rb") as f:
        data = pickle.load(f)  # noqa: S301 —— 自家运行时写出的文件
    if not isinstance(data, dict):
        raise ValueError(f"pckl 顶层不是 dict（{type(data).__name__}），不是平台存储文件")
    return data


def collect_threads(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 pckl 抽出 threads 表：{thread_id: {metadata, created_at, updated_at, state_updated_at}}。

    ⚠️ **`raw["threads"]` 的形态随 runtime 版本变，两种都要吃**（2026-10-05 生产实测
    是**列表**：263 个元素，每个是含 `thread_id`(UUID 对象) / `metadata` / 三个时间戳的
    dict —— 而假相 pckl 造的字典形态骗过了 51/51 的套件，真机读到 0 条）。
    真实条目还带 `values`(对话状态) / `config` / `interrupts` / `status` / `error`；
    本函数**只取搬迁需要的键**，`values` 由正文库负责（不重复搬）。
    """
    threads = raw.get("threads")
    if isinstance(threads, dict):
        pairs: list[tuple[Any, Any]] = list(threads.items())
    elif isinstance(threads, list):
        pairs = [(None, obj) for obj in threads]
    else:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for tid, obj in pairs:
        rec = _as_dict(obj)
        want = rec.get("thread_id") if rec.get("thread_id") is not None else tid
        if want is None:
            continue
        # 白名单：只留搬迁真正要用的键。**刻意不搬 `values`**（那是对话状态，
        # 正文在 nl2sql_checkpoint 里，搬过来既重复又可能把 34MB 状态带进请求）。
        out[str(want)] = {
            "thread_id": str(want),  # UUID → 字符串，否则 POST /threads 序列化不了
            "metadata": _as_dict(rec.get("metadata") or {}),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
            "state_updated_at": rec.get("state_updated_at"),
        }
    return out


# ── ② 新实例已有 threads ───────────────────────────────

def _base_url() -> str:
    return (os.environ.get("NL2SQL_MIGRATE_URL") or "http://127.0.0.1:2026").rstrip("/")


def _client() -> httpx.Client:
    # trust_env=False：确定性 —— 不吃 HTTP_PROXY/NO_PROXY，本地 127.0.0.1 必须直连。
    # ⚠️ 不带任何认证头，也不带 X-Forwarded-For ⇒ auth 中间件判定为**内部身份**
    #    （见 api/auth_middleware.py 的 has_token / has_xff 双否分支）。
    return httpx.Client(
        base_url=_base_url(),
        timeout=httpx.Timeout(120.0, connect=10.0),
        trust_env=False,
    )


def list_existing(http: httpx.Client) -> dict[str, dict[str, Any]]:
    """翻页取新实例已有的全部 threads（含 metadata）。"""
    out: dict[str, dict[str, Any]] = {}
    offset, page = 0, 1000
    while True:
        r = http.post("/threads/search", json={"limit": page, "offset": offset})
        r.raise_for_status()
        batch = r.json() or []
        for t in batch:
            tid = str(t.get("thread_id") or "")
            if tid:
                out[tid] = t
        if len(batch) < page:
            return out
        offset += page


def owner_of(thread: dict[str, Any]) -> str:
    md = thread.get("metadata") or {}
    owner = md.get("owner") if isinstance(md, dict) else None
    return owner if isinstance(owner, str) else ""


def create_thread(http: httpx.Client, tid: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """POST /threads（内部身份）。`if_exists=do_nothing` ⇒ 幂等，重跑安全。"""
    r = http.post(
        "/threads",
        json={"thread_id": tid, "metadata": metadata, "if_exists": "do_nothing"},
    )
    r.raise_for_status()
    return r.json() or {}


# ── ③/④ 对账源 ────────────────────────────────────────

def checkpoint_thread_ids(uri: str) -> set[str]:
    """会话正文库的真实全集（`checkpoints` 每写一步一行，thread_id 是分流键）。

    这是**比 pckl 更权威**的存在性来源：pckl 可能读旧了/读漏了，而正文就在这。
    """
    if not uri:
        return set()
    try:
        import psycopg
    except ImportError:
        print("⚠️ 没装 psycopg，跳过 checkpoints 对账")
        return set()
    with psycopg.connect(uri, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
            return {str(row[0]) for row in cur.fetchall() if row[0]}


def _fts_path(explicit: str) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    root = os.getenv("AGENT_DATA_ROOT", "").strip()
    if root:
        p = Path(root) / "shared" / "checkpoint" / "fts.sqlite"
        if p.exists():
            return p
    p = Path("src/agent/shared/checkpoint/fts.sqlite")
    return p if p.exists() else None


def fts_rows(path: Path) -> dict[str, dict[str, Any]]:
    """FTS 派生索引里的 {thread_id: {title, updated_at, owner}}（只读）。

    fts.sqlite 是**派生缓存**（枚举 /threads/search + 拉 state 拼出来的），
    但它那列 `owner` 恰好是我们搬迁时要的键 —— 用作 pckl 读漏时的兜底。
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(thread_text)")}
        sel = [c for c in ("thread_id", "title", "updated_at", "owner") if c in cols]
        rows = conn.execute(f"SELECT {', '.join(sel)} FROM thread_text")
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            rec = dict(zip(sel, row))
            tid = str(rec.get("thread_id") or "")
            if tid:
                out[tid] = rec
        return out
    finally:
        conn.close()


def _auth_db_path(explicit: str) -> Path | None:
    """grants 账本（agent_data 卷，不随平台存储切换）的位置。"""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    root = os.getenv("AGENT_DATA_ROOT", "").strip()
    if root:
        p = Path(root) / "auth" / "auth.sqlite"
        if p.exists():
            return p
    p = Path("src/agent/shared/auth/auth.sqlite")
    return p if p.exists() else None


def ledger_owners(path: Path | None) -> dict[str, str]:
    """grants 账本 → {thread_id: user_id}（只读）。

    ⚠️ 列名是 **`user_id` 不是 `owner`**（2026-10-05 实测
    `PRAGMA table_info(thread_owner)` = (thread_id, user_id, created_at)）——
    按 `owner` 查会直接 OperationalError。

    为什么这里要用它：pckl 读漏/漏建的线程，唯一的真实归属来源就是这本账
    （它由 `claim_thread` 在每次建会话时写，**不随平台存储切换**）。
    """
    if path is None or not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(thread_owner)")}
        if not {"thread_id", "user_id"} <= cols:
            return {}
        return {
            str(t): str(u)
            for t, u in conn.execute("SELECT thread_id, user_id FROM thread_owner")
            if t and u
        }
    finally:
        conn.close()


def plan_orphans(
    ckpt_ids: set[str],
    fts: dict[str, dict[str, Any]],
    src: dict[str, dict[str, Any]],
    ledger: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """真实全集里 pckl 没有的那部分 → (期望记录, 无归属被跳过的 thread_id)。

    **owner 用三层来源**：grants 账本（最可靠）→ FTS 的 owner 列（若该版本还有）
    → 都没有 ⇒ **跳过不建**。

    ⚠️ 最后一层**不再回落 `legacy`**：`legacy` 是"公开哨兵"（
    `ownership.owner_filter` 让**所有**登录用户都能看到它）。给无归属的孤儿贴
    `legacy` 会把别人的会话变成人人可见 —— 而它们在旧实例里**本来就不可见**
    （不在平台库 = 侧边栏没有）⇒ 跳过 = 维持现状，落 legacy = 制造隐私回归。
    2026-10-05 实测：FTS 的 `thread_text` 根本没有 owner 列（只有
    thread_id/title/updated_at/content），所以不接账本的话**孤儿会 100% 落 legacy**。
    """
    ledger = ledger or {}
    out: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for tid in (ckpt_ids - set(src)) | (set(fts) - set(src)):
        fr = fts.get(tid) or {}
        owner = ledger.get(tid) or (fr.get("owner") if isinstance(fr.get("owner"), str) else "")
        if not owner or owner in NON_USER:
            skipped.append(tid)
            continue
        md: dict[str, Any] = {"owner": owner}  # 含 "legacy"：它是账本里的真实归属
        if fr.get("title"):
            md["title"] = fr["title"]
        out[tid] = {"thread_id": tid, "metadata": md, "updated_at": fr.get("updated_at")}
    return out, skipped


# ── ⑤ 时间戳回填 ──────────────────────────────────────

def fixup_timestamps(platform_uri: str, want: dict[str, dict[str, Any]]) -> dict[str, int]:
    """把 pckl 里的真时间戳直写平台库。返回 {"updated": n, "skipped": m, "missing_table": 0/1}。

    `ThreadCreate` 没有时间戳字段 ⇒ 走 API 建出来的行 `created_at=now()`，
    侧边栏（按 created_at 分组排序）会全挤到迁移那一刻。列名/表名**运行期探测**：
    官方 schema 的表名是单数 `thread`（阶段 0 实测），万一变体就用 information_schema 找。
    """
    stat = {"updated": 0, "skipped": 0, "missing_table": 0}
    try:
        import psycopg
    except ImportError:
        print("⚠️ 没装 psycopg，跳过时间戳回填")
        return stat
    with psycopg.connect(platform_uri, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN ('thread','threads')"
            )
            names = [r[0] for r in cur.fetchall()]
        if not names:
            stat["missing_table"] = 1
            return stat
        table = names[0]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s",
                (table,),
            )
            cols = {r[0] for r in cur.fetchall()}
        usable = [c for c in ("created_at", "updated_at", "state_updated_at") if c in cols]
        if "created_at" not in usable:
            print(f"⚠️ 表 {table} 没有 created_at 列，跳过时间戳回填")
            return stat
        sets = ", ".join(f"{c}=%s" for c in usable)
        with conn.cursor() as cur:
            for tid, rec in want.items():
                vals = [rec.get(c) for c in usable]
                if all(v is None for v in vals):
                    stat["skipped"] += 1
                    continue
                cur.execute(f"UPDATE {table} SET {sets} WHERE thread_id=%s", [*vals, tid])
                stat["updated"] += cur.rowcount or 0
        conn.commit()
    return stat


# ── main ─────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    """执行迁移。返回 0 = 可以切流；1 = 有失败（失败/丢 owner/连不上），别切。"""
    if args.url:
        os.environ["NL2SQL_MIGRATE_URL"] = args.url
    platform_db = args.platform_db or os.environ.get("DATABASE_URI", "")
    ckpt_db = args.checkpoint_db or os.environ.get("CHECKPOINT_DB_URI", "")
    if os.environ.get("LANGGRAPH_RUNTIME_EDITION", "").strip().lower() != "postgres":
        print("ℹ️ 提示：当前 env 不是 postgres 版型 —— 本脚本应指向**新实例**（postgres 那份）")

    print(f"[迁移] 新实例={_base_url()}")
    print(f"[迁移] 平台库={'已配置' if platform_db else '(空，⑤ 段将跳过)'}"
          f"  正文库={'已配置' if ckpt_db else '(空，④ 段将只剩 FTS 源)'}")
    print(f"[迁移] 模式={'APPLY（真写）' if args.apply else 'DRY-RUN（只看计划）'}")

    # ① 离线读 pckl
    _ensure_readable_env()
    try:
        src = collect_threads(read_pckl(Path(args.pckl)))
    except (FileNotFoundError, ValueError, pickle.UnpicklingError) as e:
        print(f"\n🚫 {e}")
        return 1
    print(f"\n[①] pckl threads = {len(src)}")

    # ④ 的对账源（先备好，③ 只补 pckl 那部分，④ 再补漏网）
    ckpt_ids = checkpoint_thread_ids(ckpt_db)
    print(f"[④] checkpoints 全集 = {len(ckpt_ids)}")
    fpath = _fts_path(args.fts)
    fts = fts_rows(fpath) if fpath else {}
    print(f"[④] FTS 派生索引 = {len(fts)}（{'未找到，跳过' if not fpath else fpath}）")
    apath = _auth_db_path(args.auth_db)
    ledger = ledger_owners(apath)
    print(f"[④] grants 账本 = {len(ledger)}（{'未找到，跳过' if not apath else apath}）")

    # ④ 组合出「真实全集 → 期望 metadata」
    orphans, orphan_skipped = plan_orphans(ckpt_ids, fts, src, ledger)

    # ② 新实例已有
    with _client() as http:
        try:
            existing = list_existing(http)
        except httpx.HTTPError as e:
            print(f"\n🚫 连不上新实例（{e}）—— 先把它起起来（compose up -d langgraph-api）")
            return 1
        print(f"\n[②] 新实例已有 threads = {len(existing)}")

        # ③ 决定要补哪些
        todo = {tid: rec for tid, rec in src.items() if tid not in existing}
        todo.update({tid: rec for tid, rec in orphans.items() if tid not in existing})
        # 已有但 owner 不一致：只报不改（改 = 可能夺走别人的会话，人工判断）
        mismatch = [
            (tid, owner_of(existing[tid]), owner_of(rec))
            for tid, rec in src.items()
            if tid in existing and owner_of(existing[tid]) != owner_of(rec)
        ]

        print(f"\n[③] 待补建 = {len(todo)}（其中 pckl {len([t for t in todo if t in src])}"
              f" / 对账源 {len([t for t in todo if t in orphans])}）")
        by_owner: dict[str, int] = {}
        for rec in todo.values():
            o = (rec.get("metadata") or {}).get("owner") or "(无 owner)"
            by_owner[o] = by_owner.get(o, 0) + 1
        for o, n in sorted(by_owner.items(), key=lambda kv: -kv[1])[:20]:
            print(f"      {o:<20} {n}")
        if orphan_skipped:
            print(f"      ⏭ 对账源里 {len(orphan_skipped)} 条查不到归属（账本/FTS 都没有）"
                  f" ⇒ 跳过不建（维持旧实例的可见性，不贴 legacy）")
        if mismatch:
            print(f"\n⚠️ 已有归属 ≠ pckl 归属 = {len(mismatch)}（脚本不动，人工判断）：")
            for tid, have, want in mismatch[:20]:
                print(f"      {tid}  新实例={have or '(无)'}  pckl={want or '(无)'}")

        if not args.apply:
            print("\n[DRY-RUN] 未写任何东西。加 --apply 真跑。")
            return 0

        # ③ 真写
        created = already = failed = 0
        lost_owner: list[str] = []
        for i, (tid, rec) in enumerate(todo.items(), 1):
            md = dict(rec.get("metadata") or {})
            want_owner = owner_of(rec)
            try:
                got = create_thread(http, tid, md)
            except httpx.HTTPError as e:
                failed += 1
                print(f"      ✗ {tid} {e}")
                continue
            got_owner = owner_of(got)
            if got_owner == want_owner and want_owner:
                created += 1
            elif want_owner and got_owner != want_owner:
                # owner 被改写 = 内部身份判定失效 ⇒ 全量都会丢归属，必须停
                lost_owner.append(f"{tid}: 期望={want_owner} 实得={got_owner or '(无)'}")
                created += 1
            else:
                already += 1
            if i % 50 == 0:
                print(f"      … {i}/{len(todo)}")
        print(f"\n[③] 完成：新建/确认 {created}、已存在 {already}、失败 {failed}")
        if lost_owner:
            print(f"\n🚨 {len(lost_owner)} 条 owner 未按预期保留 —— 内部身份判定可能失效，"
                  "先别切流，逐条核对：")
            for line in lost_owner[:20]:
                print(f"      {line}")

    # ⑤ 时间戳回填
    if not args.no_timestamp_fixup and args.apply:
        want = {}
        for tid, rec in {**src, **orphans}.items():
            want[tid] = {
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "state_updated_at": rec.get("state_updated_at"),
            }
        st = fixup_timestamps(platform_db, want)
        if st["missing_table"]:
            print("\n[⑤] 平台库里没找到 thread(s) 表 —— 新实例还没跑过迁移？跳过")
        else:
            print(f"\n[⑤] 时间戳回填：更新 {st['updated']} 行、无时间戳跳过 {st['skipped']}")
            print("     校验（应能按 created_at 排序看到真实历史时间）：")
            with _client() as http:
                r = http.post("/threads/search", json={
                    "limit": 3, "sort_by": "created_at", "sort_order": "asc",
                    "select": ["thread_id", "created_at", "metadata"],
                })
                if r.status_code < 400:
                    for t in r.json() or []:
                        print(f"       {t.get('created_at')}  {t.get('thread_id')}  "
                              f"owner={(t.get('metadata') or {}).get('owner')}")

    if args.apply:
        print("\n下一步（归属账本对账，幂等，先 dry-run）：")
        print("    python scripts/backfill_thread_owner.py")
        print("    python scripts/backfill_thread_owner.py --apply")
    bad = int(failed) + len(lost_owner)
    if bad:
        print(f"\n🚫 {bad} 条异常 —— **别切流**，先看清上面每条再重跑（本脚本幂等）。")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="平台存储 pckl → postgres 平台库")
    ap.add_argument("--apply", action="store_true", help="真写（默认只打印计划）")
    ap.add_argument("--pckl", default=DEFAULT_PCKL, help=f"旧 pckl 路径（默认 {DEFAULT_PCKL}）")
    ap.add_argument("--url", default="", help="新实例地址（默认 NL2SQL_MIGRATE_URL 或 127.0.0.1:2026）")
    ap.add_argument("--platform-db", default="", help="平台库 URI（默认 DATABASE_URI）")
    ap.add_argument("--checkpoint-db", default="", help="正文库 URI（默认 CHECKPOINT_DB_URI）")
    ap.add_argument("--fts", default="", help="fts.sqlite 路径（默认 <AGENT_DATA_ROOT>/shared/checkpoint/）")
    ap.add_argument("--auth-db", default="", help="grants 账本 auth.sqlite 路径（默认 <AGENT_DATA_ROOT>/auth/）")
    ap.add_argument("--no-timestamp-fixup", action="store_true", help="不做第 ⑤ 段时间戳回填")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
