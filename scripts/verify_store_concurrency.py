# -*- coding: utf-8 -*-
"""P1-8 / P1-9 验证：四类 SQLite 存储在并发下的不变量。

覆盖的存储：traces.sqlite（EventStore）、eval_queue.sqlite（EvalQueueStore）、
fts.sqlite（thread_search）。feedback / trace_bind 只校验 PRAGMA 配置到位。

可证伪的不变量：
  A. traces：40 线程并发写同一个 thread → seq **无重号**，且 seq 集合 == 1..N
  B. traces：两个实例（同一文件的两种路径写法）→ 注册表返回**同一个对象**，
     sqlite3.connect **只被调用一次**
  C. traces：并发下 insert_event 返回的 id **就是自己那一行**（旧实现在锁外读
     last_insert_rowid，会取到别人的 id）
  D. eval_queue：**两个进程**并发 claim → 同一个任务不会被领两次（跨进程原子；
     进程内的锁管不了另一个进程）
  E. fts：归属过滤发生在 LIMIT **之前** —— 别人的命中排满前 N 条时，自己的命中
     仍然要搜得到（旧实现先取前 N 条再剔除 → 静默返回空）
  F. fts：并发读写不抛异常（WAL + busy_timeout + 单连接串行）
  G. 五个存储的 busy_timeout 都显式设到 15s
  H. 负对照：rollback journal 下的读**确实会被** EXCLUSIVE 写挡住（证明 WAL 这个
     改动的价值不是空话）
  I. 负对照：把 insert_event 换回旧写法（计数器在锁外 + lastrowid 在锁外）→
     撞号与取错 id 都会出现（证明 A/C 的断言不是恒真）

负对照的写法沿用 verify_auth_store_concurrency.py 的思路：**不**做 git checkout 式
回滚，而是在同一进程里复刻旧实现（`_legacy_insert` / `_legacy_query` / `_legacy_claim`），
把旧代码里本就存在的竞态窗口用一次 sleep **放大**（真实窗口只有几条字节码宽，不放大
就只能偶发出错，那会让负对照失去判别力）。

唯一的例外是「并发读」那条：它的窗口在 sqlite3 模块的 C 代码里（`execute()` 内部
reset/复用 prepared statement），Python 侧插不进 sleep，于是改用**最多试 4 次、
命中一次即算检出**（`legacy_race_retry`）—— 单次运行实测约 1/5 假绿。

读一遍负对照的输出请注意归因：旧实现在 A/C 两条里**先崩在共享连接的并发读上**
（不持锁的 `execute().fetchone()` 会拿到 None → `NoneType is not subscriptable`），
还没轮到「seq 撞号 / id 取错行」显现。两者是同一个根因（一条连接上的并发访问）
的不同表现，负对照证明的是「旧写法在这套用例下站不住」，标签写的是**意图**而不是
崩点 —— 别把它读成「seq 撞号已被独立复现」。

运行：
    uv run --no-project python scripts/verify_store_concurrency.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

_TMP = tempfile.mkdtemp(prefix="nl2sql-verify-store-conc-")
os.environ["AGENT_DATA_ROOT"] = _TMP  # 覆盖式：本脚本独占一个数据根
os.environ["PYTHONIOENCODING"] = "utf-8"

_HERE = pathlib.Path(__file__).resolve()
_SRC = _HERE.parents[1] / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.eval import eval_queue  # noqa: E402
from agent.trace.event_store import EventStore, get_event_store  # noqa: E402
from agent.trace.event_log import EventType, TraceEvent  # noqa: E402
from api import thread_search  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def safe(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        return False, f"异常: {type(e).__name__}: {e}"


# ── 旧实现复刻（负对照用）────────────────────────────────────
#
# 与修改前的代码逐行等价，只在两处竞态窗口里插了 sleep 把窗口放大：
#   1) seq 的「读计数器 → +1」之间（旧代码在 `with self._write_lock` **之前**发号）
#   2) commit 之后、读 last_insert_rowid 之前（旧代码在锁**外**读）

def _legacy_insert(store: EventStore, event: TraceEvent, widen: float = 0.0) -> int:
    counters = getattr(store, "_legacy_counters", None)
    if counters is None:
        counters = {}
        store._legacy_counters = counters

    if event.seq == 0:
        tid = event.thread_id
        if tid not in counters:
            row = store._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM trace_events WHERE thread_id = ?",
                (tid,),
            ).fetchone()
            counters[tid] = (row[0] if row else 0) + 1
        else:
            time.sleep(widen)  # ← 放大：「读-改-写」窗口
            counters[tid] += 1
        event.seq = counters[tid]

    row = event.to_row()
    with store._lock:
        store._conn.execute(
            """INSERT INTO trace_events
               (seq, thread_id, agent_type, task_id, parent_thread_id,
                event_type, timestamp, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
        store._conn.commit()
    time.sleep(widen)  # ← 放大：commit 与 last_insert_rowid 之间（旧代码在锁外读）
    return store._conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _legacy_query(store: EventStore, thread_id: str) -> list:
    """旧读路径：不持锁地 execute + fetchall（sqlite3 会复用同一条 prepared
    statement → 并发跑同一句 SELECT 时互相 reset）。"""
    return store._conn.execute(
        """SELECT id, seq, thread_id, agent_type, task_id,
                  parent_thread_id, event_type, timestamp, data_json
           FROM trace_events WHERE thread_id = ? ORDER BY thread_id, seq LIMIT ?""",
        (thread_id, 200),
    ).fetchall()


# ── A / B / C：traces.sqlite ─────────────────────────────────

_THREAD = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def _mk_event(marker: int) -> TraceEvent:
    return TraceEvent(
        thread_id=_THREAD,
        agent_type="chat_agent",
        event_type=EventType.TOOL_CALL_END,
        data={"marker": marker},
    )


def case_seq_unique(n_threads: int, per_thread: int, legacy: bool = False) -> tuple[bool, str]:
    db = os.path.join(_TMP, f"traces-{'legacy' if legacy else 'new'}.sqlite")
    if os.path.exists(db):
        os.remove(db)
    store = EventStore(db)
    store.open()

    def work(t: int) -> None:
        for k in range(per_thread):
            ev = _mk_event(t * per_thread + k)
            if legacy:
                _legacy_insert(store, ev, widen=0.002)
            else:
                store.insert_event(ev)

    total = n_threads * per_thread
    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(work, range(n_threads)))

    rows = store._conn.execute(
        "SELECT seq, COUNT(*) c FROM trace_events WHERE thread_id = ?"
        " GROUP BY seq HAVING c > 1", (_THREAD,)
    ).fetchall()
    seqs = [r[0] for r in store._conn.execute(
        "SELECT seq FROM trace_events WHERE thread_id = ?", (_THREAD,)
    ).fetchall()]
    ok = len(rows) == 0 and sorted(seqs) == list(range(1, total + 1))
    return ok, (f"行数 {len(seqs)}（期望 {total}）/ 重号 {len(rows)} 组 / "
                f"seq 集合完整={sorted(seqs) == list(range(1, total + 1))}")


def case_shared_instance() -> tuple[bool, str]:
    db = os.path.join(_TMP, "traces-shared.sqlite")
    variants = [db, os.path.join(_TMP, ".", "traces-shared.sqlite")]
    calls = {"n": 0}
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        calls["n"] += 1
        return real_connect(*a, **k)

    sqlite3.connect = counting_connect
    try:
        a = get_event_store(variants[0])
        b = get_event_store(variants[1])
    finally:
        sqlite3.connect = real_connect
    ok = a is b and calls["n"] == 1
    return ok, f"同一对象={a is b} / connect 调用 {calls['n']} 次（期望 1）"


def case_returned_id_ownership(n_threads: int, per_thread: int,
                               legacy: bool = False) -> tuple[bool, str]:
    db = os.path.join(_TMP, f"traces-id-{'legacy' if legacy else 'new'}.sqlite")
    if os.path.exists(db):
        os.remove(db)
    store = EventStore(db)
    store.open()
    bad: list[int] = []

    def work(t: int) -> None:
        for k in range(per_thread):
            marker = t * per_thread + k
            ev = _mk_event(marker)
            if legacy:
                row_id = _legacy_insert(store, ev, widen=0.002)
            else:
                row_id = store.insert_event(ev)
            # 校验读也必须进锁 —— 否则这条断言本身会因「共享连接的并发读」而抽风
            # （execute → fetchone 之间被别人插进来 → None / InterfaceError）。
            with store._lock:
                got = store._conn.execute(
                    "SELECT data_json FROM trace_events WHERE id = ?", (row_id,)
                ).fetchone()
            if not got or json.loads(got[0]).get("marker") != marker:
                bad.append(marker)

    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(work, range(n_threads)))
    return not bad, f"id 指向别人行的次数 {len(bad)}（期望 0）"


# ── D：eval_queue 跨进程 claim ───────────────────────────────

_CHILD = r'''
import os, sys, time
sys.path.insert(0, os.environ["NL2SQL_SRC"])
from agent.eval import eval_queue as q

if os.environ.get("LEGACY_CLAIM") == "1":
    def legacy_claim(self, batch=5):
        """旧实现：SELECT pending → （窗口）→ UPDATE running（两步，跨进程不安全）。"""
        with q._LOCK:
            rows = self._conn.execute(
                "SELECT id, kind, trace_id, question_thread, payload_json, attempts"
                " FROM eval_queue WHERE state='pending' ORDER BY id LIMIT ?", (batch,)
            ).fetchall()
            if rows:
                time.sleep(0.05)   # ← 放大：真实窗口只是两条语句之间
            for r in rows:
                self._conn.execute(
                    "UPDATE eval_queue SET state='running', updated_at=? WHERE id=?",
                    (q._now(), r["id"]),
                )
            self._conn.commit()
            return [dict(r) for r in rows]
    q.EvalQueueStore.claim = legacy_claim

store = q.EvalQueueStore()
out = []
while True:
    got = store.claim()
    if not got:
        break
    out.extend(r["id"] for r in got)
sys.stdout.write(",".join(str(i) for i in out))
'''


def _run_claim_children(rows: int, legacy: bool) -> tuple[bool, str]:
    # 子进程用 `EvalQueueStore()` 的默认路径（= eval_queue._db_path()），父进程必须
    # 用同一条 —— 否则两边各写一个文件，谁都领不到东西（曾经踩过）。
    path = eval_queue._db_path()
    if path.exists():
        path.unlink()
    store = eval_queue.EvalQueueStore(path=path)
    for i in range(rows):
        store.enqueue("judge", f"trace-{i:04d}", "", {})

    env = dict(os.environ)
    env["NL2SQL_SRC"] = str(_SRC)
    env["LEGACY_CLAIM"] = "1" if legacy else "0"
    env["PYTHONIOENCODING"] = "utf-8"
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(2)]
    claimed: list[int] = []
    errs = ""
    for p in procs:
        out, err = p.communicate(timeout=120)
        if p.returncode != 0:
            errs += err.decode("utf-8", "replace")[-400:]
        claimed.extend(int(x) for x in out.decode().split(",") if x.strip())

    dupes = len(claimed) - len(set(claimed))
    ok = not errs.strip() and dupes == 0 and len(set(claimed)) == rows
    return ok, (f"领取 {len(claimed)} 次 / 唯一 {len(set(claimed))}（期望 {rows}）/ "
                f"重复领取 {dupes} 次" + (f" / 子进程报错: {errs.strip()[:200]}" if errs.strip() else ""))


# ── J：共享连接上的并发读（P1-9 追加）────────────────────────

def case_concurrent_reads(n: int, per_thread: int, legacy: bool = False,
                          tag: str = "") -> tuple[bool, str]:
    """N 个线程读同一条 SELECT（另有写者）→ 不许出现 None / InterfaceError。

    这是 2026-09-23 实测出来的**真实**故障：sqlite3 模块按 SQL 文本缓存 prepared
    statement 并跨游标复用，两个线程并发跑同一条 SELECT 会互相 reset，8 线程下
    复现 9 次异常/空结果。生产里的等价场景是「多个用户同时轮询同一个 trace
    读接口」以及每个会话访问都要走的 `owned_thread`。

    `tag` 只用于**负对照重试**给每次尝试换一个库文件名（同一进程里连过多次，
    句柄没关就删不掉文件，见 `legacy_race_retry`）。
    """
    db = os.path.join(_TMP, f"traces-read-{'legacy' if legacy else 'new'}{tag}.sqlite")
    if os.path.exists(db):
        os.remove(db)
    store = EventStore(db)
    store.open()
    for i in range(50):
        store.insert_event(_mk_event(i))

    errs: list[str] = []
    empties = {"n": 0}

    def reader(_t: int) -> None:
        for _ in range(per_thread):
            try:
                if legacy:
                    rows = _legacy_query(store, _THREAD)
                else:
                    rows = store.query_events(thread_id=_THREAD, limit=200)
                if not rows:
                    empties["n"] += 1
            except Exception as e:  # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

    def writer(t: int) -> None:
        for k in range(per_thread):
            store.insert_event(_mk_event(10_000 + t * per_thread + k))

    with ThreadPoolExecutor(max_workers=n * 2) as ex:
        futs = [ex.submit(reader, i) for i in range(n)]
        futs += [ex.submit(writer, i) for i in range(n)]
        for f in futs:
            f.result()
    ok = not errs and empties["n"] == 0
    return ok, (f"异常 {len(errs)} 次 / 空结果 {empties['n']} 次（期望 0/0）"
                + (f"；{errs[0][:120]}" if errs else ""))


def legacy_race_retry(n: int, per_thread: int, attempts: int = 4) -> tuple[bool, str]:
    """负对照专用：旧读路径的竞态**最多试 `attempts` 次，命中一次即算检出**。

    为什么不能只跑一次：这条负对照没有像 `_legacy_insert` 那样插 sleep 放大窗口
    （读路径的窗口在 CPython sqlite3 的 C 代码里，Python 侧无从插入），所以单次
    运行**实测约 1/5 的几率不出现任何异常**（2026-09-24：连续 5 次整脚本，
    第 2 次报「异常 0 次 / 空结果 0 次」= 负对照假绿）。那不是判别力问题，
    是单发概率问题 —— 独立进程首跑写的是「异常 9 次 / 空结果 4 次」。

    判别力没有被削弱：假如旧路径真被修好（补上锁），每次尝试都不会命中 →
    仍然红。按实测单次漏检率 ~0.2 估算，4 次全漏 < 0.2%。
    """
    last = ""
    for i in range(attempts):
        ok, detail = case_concurrent_reads(n, per_thread, True, tag=f"-try{i}")
        if not ok:
            return True, f"第 {i + 1}/{attempts} 次命中：{detail}"
        last = detail
    return False, f"{attempts} 次尝试都没出现竞态（最后一次：{last}）"


# ── E / F：fts.sqlite ───────────────────────────────────────

_Q = "销售额"


def _seed_fts(other: int, mine: int) -> None:
    for i in range(other):
        thread_search._upsert_thread(f"oth-{i:03d}", f"别人的报告{i}", "2026-09-23T10:00:00Z",
                                    "other", f"{_Q} 分析报告 第 {i} 期 明细数据")
    for i in range(mine):
        thread_search._upsert_thread(f"mine-{i:03d}", f"我的报告{i}", "2026-09-23T09:00:00Z",
                                    "me", f"{_Q} 分析报告 我的第 {i} 期 明细数据")


def case_owner_filter_before_limit() -> tuple[bool, str]:
    """30 条别人的命中排在前面，自己的 2 条要能搜出来（limit=5）。"""
    thread_search._upsert_thread("z-other-1", "b", "2026-09-23T11:00:00Z", "other",
                                 f"{_Q} 占位")
    got = thread_search._search(_Q, limit=5, identity="me", is_admin=False)
    ids = [r["thread_id"] for r in got]
    mine = [i for i in ids if i.startswith("mine-")]
    ok = len(mine) == 2 and all(not i.startswith("oth-") for i in ids)
    return ok, f"返回 {len(ids)} 条，其中自己的 {len(mine)} 条（期望 2，且不含别人的）"


def case_like_fallback_owner_filter() -> tuple[bool, str]:
    """2 字查询走 LIKE 回退路径，归属过滤同样要生效。"""
    got = thread_search._search("报告", limit=5, identity="me", is_admin=False)
    ids = [r["thread_id"] for r in got]
    ok = bool(ids) and all(i.startswith("mine-") for i in ids)
    return ok, f"返回 {len(ids)} 条，全部是自己的={bool(ids) and all(i.startswith('mine-') for i in ids)}"


def case_fts_concurrent_rw(n: int) -> tuple[bool, str]:
    errs: list[str] = []

    def writer(i: int) -> None:
        try:
            thread_search._upsert_thread(f"w-{i:03d}", "并发写", "2026-09-23T12:00:00Z",
                                         "me", f"{_Q} 并发写入 {i}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    def reader(_i: int) -> None:
        try:
            thread_search._search(_Q, limit=20, identity="me", is_admin=False)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(writer, i) for i in range(n)]
        futs += [ex.submit(reader, i) for i in range(n)]
        for f in futs:
            f.result()
    return not errs, f"异常 {len(errs)} 条" + (f"：{errs[0][:160]}" if errs else "")


# ── G：busy_timeout 配置 ─────────────────────────────────────

def case_busy_timeout() -> tuple[bool, str]:
    from agent.feedback.store import FeedbackStore
    from agent.trace.trace_bind_store import TraceBindStore

    conns = {
        "traces": get_event_store(str(pathlib.Path(_TMP) / "traces-shared.sqlite"))._conn,
        "eval_queue": eval_queue.EvalQueueStore(path=pathlib.Path(_TMP) / "bt-queue.sqlite")._conn,
        "fts": thread_search._connect(),
        "feedback": FeedbackStore(path=str(pathlib.Path(_TMP) / "bt-fb.db"))._conn,
        "trace_bind": TraceBindStore(path=pathlib.Path(_TMP) / "bt-bind.sqlite")._conn,
    }
    got = {k: c.execute("PRAGMA busy_timeout").fetchone()[0] for k, c in conns.items()}
    ok = all(v == 15000 for v in got.values())
    return ok, " / ".join(f"{k}={v}" for k, v in got.items()) + "（期望全 15000）"


# ── H：WAL 的判别力（负对照）─────────────────────────────────

def case_wal_discriminates() -> tuple[bool, str]:
    """写者持 EXCLUSIVE 时：rollback journal 的读会 busy，WAL 的读照常。

    这条不测我们的代码，测的是「选 WAL」这个决定本身有意义 —— 防止有人把
    PRAGMA journal_mode=WAL 删掉后测试仍然全绿。
    """
    rollback = str(pathlib.Path(_TMP) / "journal-mode-delete.sqlite")
    wal = str(pathlib.Path(_TMP) / "journal-mode-wal.sqlite")
    out = {}
    for label, path, mode in (("rollback", rollback, "DELETE"), ("wal", wal, "WAL")):
        w = sqlite3.connect(path, timeout=0.5)
        w.execute(f"PRAGMA journal_mode={mode}")
        w.execute("CREATE TABLE IF NOT EXISTS t(x INTEGER)")
        w.commit()
        w.execute("BEGIN EXCLUSIVE")
        w.execute("INSERT INTO t VALUES (1)")
        r = sqlite3.connect(path, timeout=0.3)  # 读连接：默认 journal 模式
        try:
            r.execute("SELECT COUNT(*) FROM t").fetchone()
            out[label] = "ok"
        except sqlite3.OperationalError:
            out[label] = "busy"
        finally:
            r.close()
            w.rollback()
            w.close()
    ok = out["rollback"] == "busy" and out["wal"] == "ok"
    return ok, f"rollback={out['rollback']} / wal={out['wal']}（期望 busy / ok）"


# ── 主流程 ───────────────────────────────────────────────────

def main() -> int:
    print("\n=== A 并发写 traces：seq 不撞号 ===")
    check(*safe(case_seq_unique, 40, 25))

    print("\n=== B 共享实例（同一文件不同写法）===")
    check(*safe(case_shared_instance))

    print("\n=== C 并发下 insert_event 返回的 id 归属正确 ===")
    check(*safe(case_returned_id_ownership, 40, 25))

    print("\n=== D 两个进程并发 claim：同一任务不重复领取 ===")
    check(*safe(_run_claim_children, 200, False))

    print("\n=== E fts：归属过滤在 LIMIT 之前 ===")
    _seed_fts(30, 2)
    check(*safe(case_owner_filter_before_limit))
    check(*safe(case_like_fallback_owner_filter))

    print("\n=== F fts：并发读写不抛异常 ===")
    check(*safe(case_fts_concurrent_rw, 12))

    print("\n=== G 五个存储的 busy_timeout ===")
    check(*safe(case_busy_timeout))

    print("\n=== H 负对照：WAL 的判别力 ===")
    check(*safe(case_wal_discriminates))

    print("\n=== J 共享连接上的并发读（读写混跑）===")
    check(*safe(case_concurrent_reads, 8, 150))

    print("\n=== I 负对照：旧写法必须被检出 ===")
    neg_seq = safe(case_seq_unique, 40, 25, True)
    neg_id = safe(case_returned_id_ownership, 40, 25, True)
    neg_claim = safe(_run_claim_children, 200, True)
    for label, res in (("seq 撞号", neg_seq), ("id 取错行", neg_id),
                       ("跨进程重复领取", neg_claim)):
        ok, detail = res
        check(not ok, f"旧写法能被检出：{label}", detail)
    # 并发读这条单独走「最多试 4 次」：它的竞态窗口在 sqlite3 的 C 代码里，
    # 没法像 _legacy_insert 那样插 sleep 放大 → 单次运行会偶发假绿。
    detected, detail = legacy_race_retry(8, 150)
    check(detected, "旧写法能被检出：并发读互踩（不持锁读）", detail)

    bad = [label for ok, label in results if not ok]
    print(f"\n=== 结果：{len(results) - len(bad)}/{len(results)} 通过 ===")
    for label in bad:
        print(f"  ✗ {label}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
