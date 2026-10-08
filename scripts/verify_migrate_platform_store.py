# -*- coding: utf-8 -*-
"""平台存储迁移脚本（scripts/migrate_platform_store.py）验证（离线，无需后端/数据库）。

**要解决的问题**：蓝绿前置的这次搬迁是**一次性、在窗口内、不可重来**的操作，
而它唯一不可自动重建的东西是 threads 的 `metadata.owner`（硬授权过滤键，
丢了 = 所有非 admin 用户侧边栏同时清空）。所以这次搬迁值得一套套件：

  ① **纯函数**：pckl 取值形状（**列表 / dict / dataclass** —— 2026-10-05 生产实测
     是列表，假相只造字典曾让本套件 51/51 全绿地漏掉真机 0 条）、`owner_of` 的脏值、
     对账补齐的 owner 口径（账本 → FTS → **无归属即跳过，绝不回落 `legacy`**）。
  ② **FTS 读取**：真建一份 `fts.sqlite` 读（含缺 `owner` 列的降级路径 —— 生产那份
     `thread_text` 就只有 thread_id/title/updated_at/content）。
  ③ **grants 账本读取**：真建一份 `auth.sqlite`（列名是 `user_id`，不是 `owner`）。
  ④ **时间戳回填**：表名/列名运行期探测 + 缺表/缺列不炸（用假 psycopg 驱动真函数）。
  ⑤ **端到端（假新实例）**：走真 `run()` ——
     · **pckl 用真实形态**（列表 + `thread_id` 是 UUID 对象 + 三个时间戳）；
     · 请求**不带 token、不带 X-Forwarded-For**（这是内部身份，owner 才不会被改写）；
     · 传 `if_exists=do_nothing`（幂等重跑）；thread_id / metadata 原样；
     · 已有实例里已存在的 thread **不重复 POST**；
     · **owner 被改写 ⇒ 退出码 1**（这是全量丢归属的信号，必须拦住切流）；
     · 对账源里查不到归属的 **不建**（否则会把别人的会话贴成人人可见的 `legacy`）；
     · POST 失败 ⇒ 退出码 1；dry-run 一个字节都不写。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_migrate_platform_store.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import pickle
import sqlite3
import sys
import tempfile
import types
import uuid
from datetime import datetime, timezone

import httpx

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
for cand in (_ROOT / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

_spec = importlib.util.spec_from_file_location("migrate_platform_store", _HERE.parent / "migrate_platform_store.py")
mps = importlib.util.module_from_spec(_spec)
sys.modules["migrate_platform_store"] = mps
_spec.loader.exec_module(mps)  # type: ignore[union-attr]

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"   [{detail}]" if detail and not cond else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 造假件 ─────────────────────────────────────────────

def real_thread(tid: str, owner: str | None = None, title: str | None = None,
                created: datetime | None = None) -> dict:
    """**真实 pckl 条目形态**（2026-10-05 从生产 pckl 实测的键集合与类型）。

    刻意保留两个坑点：`thread_id` 是 **UUID 对象**（不是 str）、条目里还带
    `values`（对话状态，不该被搬运）、多余键必须被忽略。
    """
    md: dict = {"assistant_id": "chat_agent", "graph_id": "chat_agent"}
    if owner is not None:
        md["owner"] = owner
    if title:
        md["title"] = title
    ts = created or datetime(2026, 9, 23, 7, 38, 37, tzinfo=timezone.utc)
    return {
        "thread_id": uuid.UUID(tid),
        "metadata": md,
        "created_at": ts,
        "updated_at": ts,
        "state_updated_at": ts,
        "status": "interrupted",
        "config": {"configurable": {"thread_id": tid}},
        "interrupts": {},
        "error": None,
        "values": {"messages": ["不该被搬"]},
    }


def make_pckl(tmp: pathlib.Path, threads) -> pathlib.Path:
    """`threads` 直接按传入形态落盘（**list = 生产形态**，dict = 老形态兼容）。"""
    p = tmp / ".langgraph_ops.pckl"
    with p.open("wb") as f:
        pickle.dump({"threads": threads, "runs": [], "assistants": []}, f, 2)
    return p


def make_auth_db(tmp: pathlib.Path, rows: list[tuple[str, str]],
                 name: str = "auth.sqlite", user_col: str = "user_id") -> pathlib.Path:
    """grants 账本假件。真实列名是 (thread_id, **user_id**, created_at) ——
    刻意留 `user_col` 参数：写成 `owner` 必须读不出来（而不是抛错）。"""
    p = tmp / name
    conn = sqlite3.connect(p)
    try:
        conn.execute(f"CREATE TABLE thread_owner (thread_id TEXT, {user_col} TEXT, created_at TEXT)")
        conn.executemany(
            f"INSERT INTO thread_owner (thread_id, {user_col}, created_at) VALUES (?,?,?)",
            [(t, u, "2026-10-05T00:00:00+00:00") for t, u in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return p


def make_fts(tmp: pathlib.Path, rows: list[tuple], with_owner: bool = True, name: str = "fts.sqlite") -> pathlib.Path:
    p = tmp / name
    n = len(rows[0])
    conn = sqlite3.connect(p)
    try:
        cols = "thread_id TEXT, title TEXT, updated_at TEXT, content TEXT"
        if with_owner:
            cols += ", owner TEXT DEFAULT ''"
        conn.execute(f"CREATE TABLE thread_text ({cols})")
        names = "thread_id, title, updated_at, content" + (", owner" if with_owner else "")
        conn.executemany(
            f"INSERT INTO thread_text ({names}) VALUES ({','.join('?' * n)})", rows
        )
        conn.commit()
    finally:
        conn.close()
    return p


class FakeTransport(httpx.BaseTransport):
    """假新实例：记录每个请求，按脚本回答。"""

    def __init__(self, existing: list[dict], *, drop_owner: bool = False, fail_ids: set[str] | None = None):
        self.existing = existing
        self.drop_owner = drop_owner
        self.fail_ids = fail_ids or set()
        self.posts: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        body = {}
        if request.content:
            import json

            body = json.loads(request.content.decode())
        if request.url.path == "/threads/search":
            off, lim = body.get("offset", 0), body.get("limit", 10)
            page = self.existing[off:off + lim]
            return httpx.Response(200, json=page)
        if request.url.path == "/threads":
            self.posts.append(body)
            tid = str(body.get("thread_id"))
            if tid in self.fail_ids:
                return httpx.Response(500, json={"detail": "boom"})
            md = dict(body.get("metadata") or {})
            if self.drop_owner:
                md.pop("owner", None)
            t = {"thread_id": tid, "metadata": md,
                 "created_at": "2026-10-05T00:00:00+00:00"}
            self.existing.append(t)
            return httpx.Response(201, json=t)
        return httpx.Response(404, json={"detail": "not found"})


def patched_client(transport: FakeTransport):
    return lambda: httpx.Client(base_url="http://fake", transport=transport, trust_env=False)


class FakeCursor:
    def __init__(self, conn: "FakeConn"):
        self.conn = conn
        self.rowcount = 0
        self._rows: list[tuple] = []

    def execute(self, sql: str, params=None):
        self.conn.sql.append((sql, params))
        s = " ".join(sql.split()).lower()
        if "information_schema.tables" in s:
            self._rows = [(self.conn.table,)] if self.conn.table else []
        elif "information_schema.columns" in s:
            self._rows = [(c,) for c in self.conn.columns]
        elif s.startswith("update"):
            self.rowcount = 1 if params and params[-1] in self.conn.known else 0
        else:
            self._rows = []
        return self

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, table: str, columns: set[str], known: set[str]):
        self.table = table
        self.columns = columns
        self.known = known
        self.sql: list[tuple] = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def with_fake_psycopg(conn: FakeConn):
    fake = types.ModuleType("psycopg")

    def connect(*a, **k):
        return conn

    fake.connect = connect  # type: ignore[attr-defined]
    return fake


# ── ① 纯函数 ───────────────────────────────────────────

def t1_pure() -> None:
    section("① 纯函数：pckl 形状 / owner_of / 对账口径")
    tid_a, tid_b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"

    # ── 生产形态：list of dict，thread_id 是 UUID 对象 ──
    lst = mps.collect_threads({"threads": [real_thread(tid_a, "u1"), real_thread(tid_b)]})
    check(set(lst) == {tid_a, tid_b}, "**列表形态**（生产实测）能全部取出", str(sorted(lst)))
    check(lst[tid_a]["thread_id"] == tid_a, "UUID thread_id → 归一成 str（否则 POST 序列化不了）")
    check(lst[tid_a]["metadata"]["owner"] == "u1", "list 形态的 metadata 照读")
    check(lst[tid_b]["metadata"] == {"assistant_id": "chat_agent", "graph_id": "chat_agent"},
          "无 owner 的条目 metadata 不被编造")
    check(lst[tid_a]["created_at"] == datetime(2026, 9, 23, 7, 38, 37, tzinfo=timezone.utc),
          "三个时间戳原样取出")
    check(not any("values" in v for v in lst.values()), "**不搬 `values`**（对话正文归正文库）")

    # ── 老形态：dict / dataclass（兼容，不能丢）──
    raw = {
        "threads": {
            tid_a: {"metadata": {"owner": "u1", "title": "t"}, "created_at": datetime(2026, 1, 1)},
            tid_b: types.SimpleNamespace(metadata={"owner": "legacy"}, created_at=None),
            "not-a-thread": {"foo": 1},
        },
        "runs": {tid_a: {"x": 1}},
    }
    threads = mps.collect_threads(raw)
    check(set(threads) >= {tid_a, tid_b}, "dict 值 + dataclass 值也能取出", str(list(threads)))
    check(threads[tid_a]["thread_id"] == tid_a, "dict 形态 thread_id 同样归一成 str")
    check(mps._as_dict(types.SimpleNamespace(a=1)) == {"a": 1}, "_as_dict 兜底 __dict__")
    check(mps._as_dict(9) == {}, "_as_dict 非映射 → 空 dict")
    check(mps.owner_of({"metadata": {"owner": "u1"}}) == "u1", "owner_of 正常取值")
    check(mps.owner_of({"metadata": {"owner": 7}}) == "", "owner_of 非字符串 → 空")
    check(mps.owner_of({"metadata": "junk"}) == "", "owner_of metadata 非 dict → 空")
    check(mps.owner_of({}) == "", "owner_of 无 metadata → 空")
    check(mps.collect_threads({}) == {}, "pckl 无 threads 键 → 空（不炸）")
    check(mps.collect_threads({"threads": "junk"}) == {}, "threads 是脏类型 → 空（不炸）")
    check(mps._iso(None) == "-", "_iso(None)")

    # ── 对账归属：账本 → FTS → 跳过 ──
    fts = {
        "real": {"owner": "u9", "title": "标题"},   # 老版本 FTS 有 owner 列
        "anon": {"owner": "", "title": "T"},
        "internal": {"owner": "internal"},
        "no-info": {"owner": ""},
    }
    ids = {"real", "anon", "internal", "no-info", "from-ledger", "ledger-wins"}
    ledger = {"from-ledger": "u-led", "ledger-wins": "u-led2"}
    orph, skipped = mps.plan_orphans(ids, fts, {"already": {}}, ledger)
    check(orph["real"]["metadata"]["owner"] == "u9", "对账：FTS owner（老 schema）→ 原样带上")
    check(orph["real"]["metadata"]["title"] == "标题", "对账：title 带上")
    check(set(orph) == {"real", "from-ledger", "ledger-wins"},
          "**无归属的孤儿不进待建表**（anon/internal/no-info 全跳过）", str(sorted(orph)))
    check(orph["from-ledger"]["metadata"]["owner"] == "u-led", "对账：归属取自 grants 账本")
    check(set(skipped) == {"anon", "internal", "no-info"},
          "跳过名单准确（含 non-user 身份）", str(sorted(skipped)))
    check(all(orph[t]["metadata"]["owner"] != "legacy" for t in orph),
          "**绝不回落 `legacy`**（那是公开哨兵 ⇒ 别人的会话会人人可见）")
    check("already" not in orph, "对账：pckl 已有的不重复进孤儿表")
    check(mps.plan_orphans(set(), {}, {"already": {}}, {})[0] == {}, "对账：pckl 已含 id 即便在 ckpt 里也不重复")
    check(mps.plan_orphans({"x"}, {}, {}, {}) == ({}, ["x"]), "无账本无 FTS ⇒ 全跳过")
    chk_legacy, _ = mps.plan_orphans({"x"}, {}, {}, {"x": "legacy"})
    check(chk_legacy["x"]["metadata"]["owner"] == "legacy", "账本里明写 legacy 的照搬（那是真实归属）")


# ── ② FTS 读取 ─────────────────────────────────────────

def t2_fts(tmp: pathlib.Path) -> None:
    section("② FTS 派生索引读取")
    p = make_fts(tmp, [("t1", "标题一", "2026-01-02T03:04:05+00:00", "正文", "u1"),
                       ("t2", "", "", "正文", "")])
    rows = mps.fts_rows(p)
    check(set(rows) == {"t1", "t2"}, "读出两条", str(rows))
    check(rows["t1"]["owner"] == "u1" and rows["t1"]["title"] == "标题一", "title/owner 落在正确列")
    check(mps._fts_path(str(p)) == p, "_fts_path 显式路径存在 → 用它")
    check(mps._fts_path(str(tmp / "nope.sqlite")) is None, "_fts_path 不存在 → None（不炸）")
    p2 = make_fts(tmp, [("t3", "x", "y", "z")], with_owner=False, name="fts_no_owner.sqlite")
    rows2 = mps.fts_rows(p2)
    check("owner" not in rows2["t3"], "缺 owner 列 → 不假装有（降级为 legacy 路径）")
    check(rows2["t3"]["title"] == "x", "缺 owner 列时其余列照读")
    p3 = make_fts(tmp, [("t4", "标题", "2026-01-01T00:00:00+00:00", "正文")],
                  with_owner=False, name="fts_real_schema.sqlite")
    check(set(mps.fts_rows(p3)["t4"]) == {"thread_id", "title", "updated_at"},
          "**生产那份 fts 的真实列集**（无 owner）也能读")


# ── ②b grants 账本 ─────────────────────────────────────

def t2b_ledger(tmp: pathlib.Path) -> None:
    section("②b grants 账本读取（孤儿归属的唯一来源）")
    p = make_auth_db(tmp, [("t1", "u1"), ("t2", "u2"), ("", "u3"), ("t3", "")])
    led = mps.ledger_owners(p)
    check(led == {"t1": "u1", "t2": "u2"}, "读出 thread_id → user_id（空值行丢掉）", str(led))
    check(mps.ledger_owners(tmp / "nope.sqlite") == {}, "账本不存在 → 空 dict（不炸）")
    check(mps.ledger_owners(None) == {}, "账本路径为 None → 空 dict")
    wrong = make_auth_db(tmp, [("t9", "u9")], name="auth_wrong_col.sqlite", user_col="owner")
    check(mps.ledger_owners(wrong) == {},
          "**列名写成 `owner` 时读不出来**（生产真实列是 user_id，写错会静默全丢归属）")
    check(mps._auth_db_path(str(p)) == p, "_auth_db_path 显式路径存在 → 用它")
    check(mps._auth_db_path(str(tmp / "nope.sqlite")) is None, "_auth_db_path 不存在 → None（不炸）")


# ── ③ 时间戳回填 ───────────────────────────────────────

def t3_fixup() -> None:
    section("③ 时间戳回填（假 psycopg 驱动真函数）")
    saved = sys.modules.get("psycopg")
    want = {
        "t1": {"created_at": datetime(2025, 5, 5, tzinfo=timezone.utc), "updated_at": None,
               "state_updated_at": None},
        "t2": {"created_at": None, "updated_at": None, "state_updated_at": None},
    }
    conn = FakeConn("thread", {"thread_id", "created_at", "updated_at", "state_updated_at"}, {"t1", "t2"})
    sys.modules["psycopg"] = with_fake_psycopg(conn)
    try:
        st = mps.fixup_timestamps("postgresql://x/y", want)
    finally:
        if saved is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = saved
    upd = [s for s, _ in conn.sql if s.strip().lower().startswith("update")]
    params = [p for s, p in conn.sql if s.strip().lower().startswith("update")]
    check(st["updated"] == 1, "只更新有时间戳的行", str(st))
    check(st["skipped"] == 1, "全 None 的行跳过", str(st))
    check(upd and "thread" in upd[0], "表名从 information_schema 探测（单数 thread）", upd[0] if upd else "")
    check(params and params[0][-1] == "t1", "WHERE thread_id 用真 id", str(params))
    check(len(params[0]) == 4, "SET 三列 + WHERE 一参", str(params[0]))

    conn2 = FakeConn("", set(), set())
    sys.modules["psycopg"] = with_fake_psycopg(conn2)
    try:
        st2 = mps.fixup_timestamps("postgresql://x/y", want)
    finally:
        sys.modules.pop("psycopg", None)
        if saved is not None:
            sys.modules["psycopg"] = saved
    check(st2["missing_table"] == 1 and st2["updated"] == 0, "找不到表 ⇒ 明确报告，不炸")

    conn3 = FakeConn("thread", {"thread_id", "updated_at"}, set())
    sys.modules["psycopg"] = with_fake_psycopg(conn3)
    try:
        st3 = mps.fixup_timestamps("postgresql://x/y", want)
    finally:
        sys.modules.pop("psycopg", None)
        if saved is not None:
            sys.modules["psycopg"] = saved
    check(st3["updated"] == 0, "没有 created_at 列 ⇒ 整体跳过")


# ── ④ 端到端 ───────────────────────────────────────────

def run_once(tmp: pathlib.Path, transport: FakeTransport, *, apply: bool, pckl: pathlib.Path,
             fts: pathlib.Path, ckpt_ids: set[str], fixup_sink: list | None = None,
             auth_db: pathlib.Path | None = None) -> int:
    saved_client, saved_ckpt, saved_fixup = mps._client, mps.checkpoint_thread_ids, mps.fixup_timestamps
    mps._client = patched_client(transport)  # type: ignore[assignment]
    mps.checkpoint_thread_ids = lambda uri: set(ckpt_ids)  # type: ignore[assignment]
    if fixup_sink is not None:
        mps.fixup_timestamps = lambda uri, want: (fixup_sink.append(want),
                                                  {"updated": len(want), "skipped": 0, "missing_table": 0})[1]  # type: ignore[assignment]
    try:
        args = argparse.Namespace(
            apply=apply, pckl=str(pckl), url="http://fake",
            platform_db="postgresql://fake/platform", checkpoint_db="postgresql://fake/ckpt",
            fts=str(fts), auth_db=str(auth_db or (tmp / "no-auth.sqlite")),
            no_timestamp_fixup=fixup_sink is None,
        )
        return mps.run(args)
    finally:
        mps._client, mps.checkpoint_thread_ids, mps.fixup_timestamps = saved_client, saved_ckpt, saved_fixup


def t4_e2e(tmp: pathlib.Path) -> None:
    section("④ 端到端（假新实例，**真实 pckl 形态**）")
    new_u, c_u, d_u = "aaaaaaaa-0000-0000-0000-000000000001", "cccccccc-0000-0000-0000-000000000003", "dddddddd-0000-0000-0000-000000000004"
    have_u, orphan_u = "bbbbbbbb-0000-0000-0000-000000000002", "eeeeeeee-0000-0000-0000-000000000005"
    # pckl = 列表（生产形态），thread_id 是 UUID 对象
    pckl = make_pckl(tmp, [
        real_thread(new_u, "u1", "新", datetime(2026, 3, 1, tzinfo=timezone.utc)),
        real_thread(have_u, "u2", None, datetime(2026, 2, 1, tzinfo=timezone.utc)),
        real_thread(c_u, "legacy", None, datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ])
    # FTS 用**生产真实列集**（没有 owner 列）⇒ 归属只能来自账本
    fts = make_fts(tmp, [(d_u, "对账补的", "2026-04-01T00:00:00+00:00", "正文"),
                         (orphan_u, "无归属", "2026-04-02T00:00:00+00:00", "正文")],
                   with_owner=False, name="fts_e2e.sqlite")
    ledger = make_auth_db(tmp, [(d_u, "u3")], name="auth_e2e.sqlite")
    existing = [{"thread_id": have_u, "metadata": {"owner": "u2"}}]
    ckpt = {have_u, c_u, d_u, orphan_u}

    # dry-run：一个字节都不写
    tr = FakeTransport(list(existing))
    rc = run_once(tmp, tr, apply=False, pckl=pckl, fts=fts, auth_db=ledger, ckpt_ids=ckpt)
    check(rc == 0 and tr.posts == [], "dry-run：不 POST、退出码 0", f"rc={rc} posts={len(tr.posts)}")

    # apply
    tr = FakeTransport(list(existing))
    sink: list = []
    rc = run_once(tmp, tr, apply=True, pckl=pckl, fts=fts, auth_db=ledger, ckpt_ids=ckpt, fixup_sink=sink)
    posted = {p["thread_id"]: p for p in tr.posts}
    check(rc == 0, "apply：全部成功 → 退出码 0", f"rc={rc}")
    check(set(posted) == {new_u, c_u, d_u}, "要补的正好是 pckl 新增 + 账本可归属的对账漏网", str(sorted(posted)))
    check(orphan_u not in posted, "**无归属的孤儿不建**（贴 legacy = 别人的会话人人可见）")
    check(have_u not in posted, "实例里已有的 thread 不重复 POST")
    check(all(p.get("if_exists") == "do_nothing" for p in tr.posts), "全部带 if_exists=do_nothing（幂等）")
    check(posted[new_u]["metadata"]["owner"] == "u1", "owner 原样带过去")
    check(posted[new_u]["metadata"]["assistant_id"] == "chat_agent",
          "metadata 整份带过去（不只 owner）")
    check(posted[c_u]["metadata"]["owner"] == "legacy", "pckl 里的 legacy 保持 legacy")
    check(posted[d_u]["metadata"]["owner"] == "u3" and posted[d_u]["metadata"]["title"] == "对账补的",
          "对账补建：owner 来自 grants 账本、title 来自 FTS")
    hdrs = tr.headers
    check(all("cookie" not in h and "authorization" not in h for h in hdrs),
          "**不带任何凭证**（内部身份的前提）")
    check(all(not h.get("x-forwarded-for") for h in hdrs),
          "**不带 X-Forwarded-For**（否则被判外部身份 → owner 被改写）")
    check(all(h.get("content-type", "").startswith("application/json") for h in hdrs), "JSON 请求头正常")
    check(len(sink) == 1 and new_u in sink[0] and sink[0][new_u]["created_at"] == datetime(2026, 3, 1, tzinfo=timezone.utc),
          "⑤ 时间戳回填拿到了 pckl 真值")

    # 幂等：再跑一遍，一条都不再 POST
    tr2 = FakeTransport([{"thread_id": t, "metadata": {"owner": o}} for t, o in
                         [(have_u, "u2"), (new_u, "u1"), (c_u, "legacy"), (d_u, "u3")]])
    rc2 = run_once(tmp, tr2, apply=True, pckl=pckl, fts=fts, auth_db=ledger, ckpt_ids=ckpt, fixup_sink=[])
    check(rc2 == 0 and tr2.posts == [], "重跑（幂等）：全已存在 → 不再 POST", f"posts={len(tr2.posts)}")

    # owner 被改写 ⇒ 拦住切流
    tr3 = FakeTransport([], drop_owner=True)
    rc3 = run_once(tmp, tr3, apply=True, pckl=pckl, fts=fts, auth_db=ledger, ckpt_ids=set(), fixup_sink=[])
    check(rc3 == 1, "**owner 被改写 ⇒ 退出码 1**（全量丢归属的信号）", f"rc={rc3}")

    # POST 失败 ⇒ 拦住切流
    tr4 = FakeTransport([], fail_ids={new_u})
    rc4 = run_once(tmp, tr4, apply=True, pckl=pckl, fts=fts, auth_db=ledger, ckpt_ids=set(), fixup_sink=[])
    check(rc4 == 1, "POST 失败 ⇒ 退出码 1", f"rc={rc4}")

    # pckl 读不到 ⇒ 明确报错，不炸
    tr5 = FakeTransport([])
    rc5 = run_once(tmp, tr5, apply=True, pckl=tmp / "nope.pckl", fts=fts, auth_db=ledger, ckpt_ids=set(), fixup_sink=[])
    check(rc5 == 1 and tr5.posts == [], "pckl 不存在 ⇒ 退出码 1、不 POST", f"rc={rc5}")

    # 实例连不上 ⇒ 退出码 1（先起服务）
    saved = mps._client
    mps._client = lambda: httpx.Client(base_url="http://127.0.0.1:1", trust_env=False,
                                       timeout=httpx.Timeout(2.0))
    try:
        args = argparse.Namespace(apply=True, pckl=str(pckl), url="http://127.0.0.1:1",
                                  platform_db="", checkpoint_db="", fts=str(fts),
                                  auth_db=str(ledger), no_timestamp_fixup=True)
        mps.checkpoint_thread_ids = lambda uri: set()
        rc6 = mps.run(args)
    finally:
        mps._client = saved
    check(rc6 == 1, "实例连不上 ⇒ 退出码 1（不假装成功）", f"rc={rc6}")


def t5_wiring() -> None:
    section("⑤ 接线：脚本在仓库里、文档指到它")
    check((_HERE.parent / "migrate_platform_store.py").is_file(), "脚本存在")
    text = (_HERE.parent / "migrate_platform_store.py").read_text(encoding="utf-8")
    check("if_exists" in text and "do_nothing" in text, "幂等契约写在代码里")
    check("backfill_thread_owner.py" in text, "归属账本对账指向现成脚本（不重复实现）")
    check("--auth-db" in text and "ledger_owners" in text, "账本读取已接进 CLI 与 run()")
    check("thread_owner" in text and "user_id" in text,
          "账本表名/列名写死在代码里（列名是 user_id，不是 owner）")
    check("列表" in text and "UUID" in text,
          "代码里记着「threads 可能是列表、thread_id 可能是 UUID」这个坑")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="nl2sql-verify-migrate-", ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t1_pure()
        t2_fts(tmp)
        t2b_ledger(tmp)
        t3_fixup()
        t4_e2e(tmp)
    t5_wiring()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
