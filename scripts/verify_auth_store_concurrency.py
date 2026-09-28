# -*- coding: utf-8 -*-
"""P1-7 验证：auth 两个存储（users 文件 + grants SQLite）在并发下不丢数据。

可证伪的不变量：
  A. N 个并发 add_user(不同 id) → 用户数恰好 N+1，且磁盘文件可解析、内容一致
  B. N 个并发 add_user(**同一个 id**) → 恰好 1 个成功、其余抛「已存在」
     （旧的 check-then-act 无锁版本会追加两次同一个 id 到同一个 list → 出现重复项）
  C. 并发 remove + update 不复活已删用户、不丢更新
  D. grants：20 个线程同时首访 → `sqlite3.connect` **只被调用一次**
     （旧的双检无锁版本会各建一条连接，多出来的那条泄漏且被覆盖丢失）
  E. grants：200 个并发写（register/claim/record）无 `database is locked`、行数准确

C/D 各配一个**负对照**：把锁换成空实现再跑一遍，必须失败（证明断言不是恒真）。
B 只有正向断言——它是纯时序竞态，进程内复现不稳定，做成负对照会偶发漏检。

运行：
    uv run --no-project python scripts/verify_auth_store_concurrency.py
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

_TMP = tempfile.mkdtemp(prefix="nl2sql-verify-auth-conc-")
os.environ["AGENT_DATA_ROOT"] = _TMP  # 覆盖式：本脚本独占一个数据根

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.auth import grants, users  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


# 负对照用：`with 锁:` 的替身（nullcontext 支持 `with x:` 语法，与 RLock 同形）
_no_lock = contextlib.nullcontext


def reset_users() -> None:
    users._users_cache = None
    users._users_path().unlink(missing_ok=True)


def read_file_users() -> list[dict]:
    return json.loads(users._users_path().read_text(encoding="utf-8"))


# ── A / B / C：users 文件 ───────────────────────────────────

def case_distinct_adds(n: int) -> tuple[bool, str]:
    reset_users()
    users.load_users()  # 建出默认 admin
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda i: users.add_user(f"u{i:03d}", "pw123456"), range(n)))
    got = {u["user_id"] for u in users.load_users()}
    expect = {"admin"} | {f"u{i:03d}" for i in range(n)}
    ok = got == expect
    return ok, f"内存用户数={len(got)}（期望 {len(expect)}）"


def case_duplicate_adds(n: int) -> tuple[bool, str]:
    reset_users()
    users.load_users()
    errors: list[str] = []
    lock = getattr(users, "_users_lock", None)

    def add(_i: int) -> None:
        try:
            users.add_user("dup", "pw123456")
        except ValueError as e:
            errors.append(str(e))

    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(add, range(n)))

    on_mem = [u for u in users.load_users() if u.get("user_id") == "dup"]
    on_disk = [u for u in read_file_users() if u.get("user_id") == "dup"]
    ok = len(on_mem) == 1 and len(on_disk) == 1 and len(errors) == n - 1
    return ok, (f"内存里 dup×{len(on_mem)} / 磁盘 dup×{len(on_disk)} / "
                f"被拒 {len(errors)}（期望 1/1/{n - 1}）")


def case_remove_and_update(n: int) -> tuple[bool, str]:
    reset_users()
    users.load_users()
    ids = [f"c{i:03d}" for i in range(n)]
    for uid in ids:
        users.add_user(uid, "pw123456")
    removed, kept = ids[: n // 2], ids[n // 2:]

    tasks = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        for uid in removed:
            tasks.append(ex.submit(users.remove_user, uid))
        for uid in kept:
            tasks.append(ex.submit(users.update_user, uid, None, f"改名-{uid}", None))
        for t in tasks:
            t.result()

    got = {u["user_id"]: u for u in users.load_users()}
    disk = {u["user_id"]: u for u in read_file_users()}
    ok = (
        all(uid not in got and uid not in disk for uid in removed)          # 不复活
        and all(got[uid]["display_name"] == f"改名-{uid}" for uid in kept)  # 不丢更新
        and all(disk[uid]["display_name"] == f"改名-{uid}" for uid in kept)  # 落盘一致
    )
    return ok, f"剩余 {len(got)}（期望 {n - n // 2 + 1}）"


# ── D / E：grants SQLite ────────────────────────────────────

def case_single_connection(n: int) -> tuple[bool, str]:
    """连接单例：并发首访只应建 1 条连接。

    在 connect 里塞 20ms 睡眠把竞态窗口**拉大**，否则双检的窗口只有几条字节码宽，
    无锁版本也可能侥幸只建一条（那会让负对照失去判别力）。带锁时这 20ms 只付一次。
    """
    grants._db_conn = None
    calls = {"n": 0}
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        calls["n"] += 1
        time.sleep(0.02)  # 拉大窗口：让「无锁双检」必然被多个线程同时穿过
        return real_connect(*a, **k)

    grants.sqlite3.connect = counting_connect
    try:
        with ThreadPoolExecutor(max_workers=n) as ex:
            conns = list(ex.map(lambda _i: id(grants._get_conn()), range(n)))
    finally:
        grants.sqlite3.connect = real_connect
    ok = calls["n"] == 1 and len(set(conns)) == 1
    return ok, f"connect 调用 {calls['n']} 次 / 不同连接对象 {len(set(conns))} 个（期望 1/1）"


def case_concurrent_writes(n: int) -> tuple[bool, str]:
    conn = grants._get_conn()
    conn.execute("DELETE FROM users")
    conn.execute("DELETE FROM thread_owner")
    conn.execute("DELETE FROM thread_db")
    conn.commit()

    errs: list[str] = []

    def work(i: int) -> None:
        try:
            grants.register_user(f"w{i:03d}", f"名字{i}")
            grants.claim_thread(f"t{i:03d}", f"w{i:03d}")
            grants.record_thread_db(f"t{i:03d}", "db_x")
            grants.record_thread_db(f"t{i:03d}", "db_x")  # 幂等：仍应只 1 行
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(work, range(n)))

    rows = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    owners = conn.execute("SELECT COUNT(*) c FROM thread_owner").fetchone()["c"]
    dbs = conn.execute("SELECT COUNT(*) c FROM thread_db").fetchone()["c"]
    ok = not errs and rows == n and owners == n and dbs == n
    return ok, f"users={rows} owner={owners} thread_db={dbs}（期望 {n}/{n}/{n}），异常 {len(errs)} 条"


# ── 主流程（含负对照）───────────────────────────────────────

def safe(fn, *args) -> tuple[bool, str]:
    """跑一个用例，把**抛异常**也归成「未通过」（负对照里异常同样是检出）。

    Windows 上无锁的 `os.replace` 会撞上并发读 → PermissionError(WinError 5)，
    而不是断言失败。两种都是「无锁方案不成立」的同一件事，报告时不该崩在 harness 里。
    """
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return False, f"异常: {type(e).__name__}: {e}"


def run_all(tag: str) -> dict[str, tuple[bool, str]]:
    print(f"\n=== {tag} ===")
    out = {}
    out["A 并发新增 20 个不同用户"] = safe(case_distinct_adds, 20)
    out["B 并发新增 40 次同一 id"] = safe(case_duplicate_adds, 40)
    out["C 并发删除 + 改名"] = safe(case_remove_and_update, 20)
    out["D 连接单例（20 线程首访）"] = safe(case_single_connection, 20)
    out["E 200 次并发写 grants"] = safe(case_concurrent_writes, 200)
    for label, (ok, detail) in out.items():
        check(ok, label, detail)
    return out


def main() -> int:
    saved = (users._users_lock, grants._lock, grants._init_lock)

    run_all("正向：带锁")

    # 负对照：把三把锁都换成空实现。C（并发 replace 撞 WinError 5）与
    # D（窗口已拉大的双检竞态）都是**确定性**的，必须被检出。
    # B 不放进负对照：它是纯时序竞态，本进程内在 Windows 上复现不稳定，
    # 与其做个会偶发漏检的负对照，不如让 B 只承担「正向必须正确」这一条。
    users._users_lock = _no_lock()
    grants._lock = _no_lock()
    grants._init_lock = _no_lock()
    print("\n=== 负对照：锁换成空实现（期望 C/D 被检出）===")
    try:
        neg = {
            "C 并发删除 + 改名": safe(case_remove_and_update, 20),
            "D 连接单例（20 线程首访）": safe(case_single_connection, 20),
        }
    finally:
        users._users_lock, grants._lock, grants._init_lock = saved

    caught = 0
    for label, (ok, detail) in neg.items():
        # 负对照里「失败」= 测试抓到了问题 = 这是好事
        caught += 0 if ok else 1
        check(not ok, f"负对照能被检出：{label}", detail)
    check(caught == 2, "负对照两条都被抓到（证明断言不是恒真）", f"抓到 {caught}/2 条")

    bad = [label for ok, label in results if not ok]
    print(f"\n=== 结果：{len(results) - len(bad)}/{len(results)} 通过 ===")
    for label in bad:
        print(f"  ✗ {label}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
