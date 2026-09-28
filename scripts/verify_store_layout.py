# -*- coding: utf-8 -*-
"""运行时库「一库一目录」+ 老文件接管验证（2026-09-25，离线，无需后端/库/网络）。

**要钉死的性质**（每条尽量配负对照，防"看起来对了"）：
  ① `store_db_path()` 的形态 = `<data_root>/<name>/<name>.sqlite`；库名带路径分隔符
     一律拒绝（**负对照**：不许拼出数据根目录之外的路径）；
  ② 三个 store 的**默认**落点（`eval_queue` / `trace_bind` / `pending_terminal` 的
     `_db_path()`）都在同名子目录里 —— 真建一次库，断言 `-wal`/`-shm` 也生在同一个
     子目录里、**数据根目录上一个散落的 `.sqlite*` 都没有**（这正是本次改动的目的）；
  ③ **老文件接管**（升级不丢数据，这是本改动唯一有风险的一面）：
     - 真库搬过去后**数据读得出来**（不是只搬了个空壳）；
     - 三件套（主库 + `-wal` + `-shm`）**一起**搬，根上不剩任何残留（**负对照**：
       `-wal` 被落下的老代码里，已提交未 checkpoint 的事务就永久丢了）；
     - **主库搬不动时不搬**（模拟 `os.replace` 失败）→ 返回 False 且**主库仍在原地**，
       恢复后重跑一次即收敛（= "下次启动重试"）；
     - **目标已存在就绝不动**（**负对照**：放一份内容不同的新库 + 一份老库，
       新库内容必须一字不变、老库仍在原地 —— 不许覆盖、不许合并）；
     - 幂等：搬过再来一次 = 空操作；两个**进程**同时抢着搬 ⇒ 恰好一个 True，数据仍可读；
  ④ 保留策略跟着落点走：`retention.measure_areas()` 报的是子目录里的三件套字节，
     **根上同名的老文件不计入**（**负对照**）；`--run` 的 eval_queue 清理**只认新路径**
     （新位置的老 done 行被删、根上同名库里的老 done 行**一行不动**）；
  ⑤ 接线：`custom_app._lifespan` 真的调了 `adopt_all_stores()`，且**排在起补写器/保留线程
     之前**（搬运的前提是「没人打开过那份老库」，顺序反了就是搬一个正在写的库）；
     `adopt_all_stores()` 本身对三个库一次搬齐（真跑）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_store_layout.py

退出码 0 = 全部通过。

⚠️ `AGENT_DATA_ROOT` 由本脚本钉到临时目录，**必须在 import 任何 agent 模块之前**。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_WORK = Path(tempfile.mkdtemp(prefix="nl2sql-verify-storelayout-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
sys.path.insert(0, str(_SRC))

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    results.append((bool(cond), label))
    tag = "PASS" if cond else "FAIL"
    color = "\033[32m" if cond else "\033[31m"
    line = f"  [{color}{tag}\033[0m] {label}"
    if detail:
        line += f"  {detail}"
    print(line, flush=True)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def _mkroot(name: str) -> Path:
    """本脚本只在临时根下造文件/搬文件（和 verify_retention 同款护栏）。"""
    r = _WORK / name
    r.mkdir(parents=True, exist_ok=True)
    assert str(r.resolve()).startswith(str(_WORK.resolve())), f"拒绝操作临时目录之外: {r}"
    return r


def _real_db(p: Path, ddl: str, insert: tuple[str, tuple]) -> None:
    """造一份**真的** SQLite 库（写一行后关连接：数据落在主文件里）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(ddl)
        conn.execute(*insert)
        conn.commit()
    finally:
        conn.close()


_TRACE_DDL = (
    "CREATE TABLE IF NOT EXISTS thread_trace (thread_id TEXT PRIMARY KEY,"
    " trace_id TEXT NOT NULL, root_obs_id TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
)
_EVAL_DDL = (
    "CREATE TABLE IF NOT EXISTS eval_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " kind TEXT NOT NULL, trace_id TEXT NOT NULL, question_thread TEXT NOT NULL DEFAULT '',"
    " payload_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'pending',"
    " attempts INTEGER DEFAULT 0, last_error TEXT DEFAULT '',"
    " created_at TEXT NOT NULL, updated_at TEXT DEFAULT '')"
)


# ── ① 纯函数：路径形态 + 库名校验 ────────────────────────────────────
def verify_path_shape() -> None:
    section("① 路径形态：<data_root>/<name>/<name>.sqlite（库名不许带路径分隔符）")
    from agent.utils import sqlite_paths as SP

    ok = all(
        SP.store_db_path(_WORK, n) == _WORK / n / f"{n}.sqlite" for n in SP.STORE_NAMES
    )
    check(ok, "三个库的落点都是「同名子目录 + 同名文件」",
          str(SP.store_db_path(_WORK, "trace_bind")))
    check(set(SP.STORE_NAMES) == {"eval_queue", "trace_bind", "pending_terminal"},
          "名册恰好是这三个库（新增库要一起进名册才会被首启接管）", str(SP.STORE_NAMES))

    rejected = []
    for bad in ("../evil", "a/b", "a\\b", "", ".hidden"):
        try:
            SP.store_db_path(_WORK, bad)
        except ValueError:
            rejected.append(bad)
    check(len(rejected) == 5,
          "负对照：库名带分隔符/空/点开头一律 ValueError（拼不出数据根目录之外的路径）",
          f"rejected={rejected}")

    check(SP.store_db_path(_WORK, "eval_queue").parent.parent == _WORK,
          "负对照的另一面：合法库名只多一层目录（不会多出第二层）")


# ── ② 三个 store 的默认落点 + 三件套同处一目录 ──────────────────────
def verify_default_locations() -> None:
    section("② 三个 store 的默认落点都在子目录里，且数据根目录上一个 .sqlite* 都没有")
    from agent.eval import eval_queue as eq
    from agent.subagents import pending_terminal as pt
    from agent.trace import trace_bind_store as tbs

    for name, fn in (("eval_queue", eq._db_path), ("trace_bind", tbs._db_path),
                     ("pending_terminal", pt._db_path)):
        p = fn()
        check(p == _WORK / name / f"{name}.sqlite",
              f"{name}._db_path() 落在 <data_root>/{name}/ 下", str(p))

    # 真建一次库并写一行：断言 -wal/-shm 与主库同处一个子目录
    store = tbs.TraceBindStore()  # 不传 path ⇒ 走默认落点
    try:
        store.set_thread("thr-1", "trace-1", "obs-1")
        d = _WORK / "trace_bind"
        check((d / "trace_bind.sqlite").exists(), "默认落点真的建出了库文件")
        side = sorted(p.name for p in d.glob("trace_bind.sqlite-*"))
        check(side == ["trace_bind.sqlite-shm", "trace_bind.sqlite-wal"],
              "-wal/-shm 生在同一个子目录里（不再散在数据根目录）", str(side))
        stray = sorted(p.name for p in _WORK.glob("*.sqlite*") if p.is_file())
        check(stray == [], "负对照：本次改动的目的达成 —— 数据根目录上零个散落的 .sqlite*",
              str(stray))
    finally:
        store.close()


# ── ③ 老文件接管（本次唯一有风险的一面）──────────────────────────
def verify_adoption() -> None:
    section("③ 老文件接管：数据读得出来 / 三件套一起搬 / 目标存在绝不动")
    from agent.utils import sqlite_paths as SP

    # 3.1 真库搬过去后数据还在
    r1 = _mkroot("adopt-real")
    _real_db(r1 / "trace_bind.sqlite", _TRACE_DDL,
             ("INSERT INTO thread_trace VALUES ('thr-legacy','trace-legacy','obs','now')", ()))
    check(SP.adopt_legacy_store(r1, "trace_bind") is True, "老库被接管（返回 True）")
    check(not (r1 / "trace_bind.sqlite").exists(), "老库文件已从数据根目录消失")
    conn = sqlite3.connect(str(SP.store_db_path(r1, "trace_bind")))
    try:
        got = conn.execute("SELECT trace_id FROM thread_trace").fetchone()
    finally:
        conn.close()
    check(got is not None and got[0] == "trace-legacy",
          "新位置打开就是**原数据**（不是只搬了个空壳）", str(got))
    check(SP.adopt_legacy_store(r1, "trace_bind") is False,
          "幂等：目标已存在 ⇒ 再来一次是空操作（返回 False）")

    # 3.2 三件套一起搬（用假库名，避免 SQLite 校验伪 -wal）
    r2 = _mkroot("adopt-trio")
    for suf in ("", "-wal", "-shm"):
        (r2 / f"dummy.sqlite{suf}").write_text(f"content{suf}", encoding="utf-8")
    check(SP.adopt_legacy_store(r2, "dummy") is True, "接管一个三件套齐全的老库")
    trio = sorted(p.name for p in (r2 / "dummy").glob("dummy.sqlite*"))
    check(trio == ["dummy.sqlite", "dummy.sqlite-shm", "dummy.sqlite-wal"],
          "-wal/-shm 与主库一起搬进子目录（**没被落下**）", str(trio))
    left = sorted(p.name for p in r2.glob("dummy.sqlite*") if p.is_file())
    check(left == [], "负对照：数据根目录上一个残留都没有", str(left))

    # 3.3 主库搬不动 ⇒ 不搬（下次启动重试才收敛）
    r3 = _mkroot("adopt-fail")
    for suf in ("", "-wal", "-shm"):
        (r3 / f"ordering.sqlite{suf}").write_text("x", encoding="utf-8")
    real_replace = os.replace
    try:
        def _fail_main(src, dst):
            if str(dst).endswith("ordering.sqlite"):
                raise OSError(13, "Permission denied（模拟主库搬不动）")
            return real_replace(src, dst)

        os.replace = _fail_main  # type: ignore[assignment]
        moved = SP.adopt_legacy_store(r3, "ordering")
    finally:
        os.replace = real_replace  # type: ignore[assignment]
    check(moved is False and (r3 / "ordering.sqlite").exists(),
          "主库搬不动 ⇒ 返回 False 且**主库仍在原地**（没半搬，没丢东西）")
    check(SP.adopt_legacy_store(r3, "ordering") is True,
          "恢复后重跑即收敛（= 下次启动重试）")
    check((r3 / "ordering" / "ordering.sqlite").exists()
          and not (r3 / "ordering.sqlite").exists(), "重试后三件套齐在新目录")

    # 3.4 目标已存在 ⇒ 绝不覆盖（负对照）
    r4 = _mkroot("adopt-nooverwrite")
    _real_db(r4 / "trace_bind" / "trace_bind.sqlite", _TRACE_DDL,
             ("INSERT INTO thread_trace VALUES ('new','trace-new','o','now')", ()))
    _real_db(r4 / "trace_bind.sqlite", _TRACE_DDL,
             ("INSERT INTO thread_trace VALUES ('old','trace-old','o','now')", ()))
    check(SP.adopt_legacy_store(r4, "trace_bind") is False, "目标已存在 ⇒ 返回 False（不搬）")
    conn = sqlite3.connect(str(r4 / "trace_bind" / "trace_bind.sqlite"))
    try:
        rows = [r[0] for r in conn.execute("SELECT thread_id FROM thread_trace").fetchall()]
    finally:
        conn.close()
    check(rows == ["new"],
          "负对照：新库内容一字未变（不覆盖、不合并），老库仍在根上等人工判断",
          f"rows={rows} / legacy_exists={(r4 / 'trace_bind.sqlite').exists()}")


# ── ④ 跨进程抢着搬：恰好一个成功 ────────────────────────────────────
_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from agent.utils.sqlite_paths import adopt_legacy_store
sys.stdout.write("1" if adopt_legacy_store(sys.argv[2], "race") else "0")
"""


def verify_concurrent_adoption() -> None:
    section("④ 两个进程同时抢着搬同一个老库 ⇒ 恰好一个成功，数据仍可读")
    r = _mkroot("adopt-race")
    _real_db(r / "race.sqlite", _TRACE_DDL,
             ("INSERT INTO thread_trace VALUES ('t','trace-race','o','now')", ()))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    procs = [
        subprocess.Popen([sys.executable, "-c", _CHILD, str(_SRC), str(r)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    outs = []
    errs = ""
    for p in procs:
        out, err = p.communicate(timeout=120)
        if p.returncode != 0:
            errs += err.decode("utf-8", "replace")[-300:]
        outs.append(out.decode().strip())
    won = sum(1 for o in outs if o == "1")
    check(not errs.strip(), "两个子进程都正常退出（另一个吃到 FileNotFoundError 也算正常）",
          errs.strip()[:200])
    check(won == 1, "恰好一个进程搬成功（没有双搬/半搬）", f"outs={outs}")
    conn = sqlite3.connect(str(r / "race" / "race.sqlite"))
    try:
        got = conn.execute("SELECT trace_id FROM thread_trace").fetchone()
    finally:
        conn.close()
    check(got is not None and got[0] == "trace-race", "抢完之后数据仍读得出来", str(got))
    check(not (r / "race.sqlite").exists(), "老文件已不在根上")


# ── ⑤ 保留策略跟着落点走 ────────────────────────────────────────────
def verify_retention_follows() -> None:
    section("⑤ 保留策略：量占用与清理都只认新路径（根上同名文件不计入、不动）")
    from agent.eval import eval_queue as eq
    from agent.utils import retention as R

    # 真建 eval_queue 的新库（顺带保证后面放在根上的"诱饵"不会被接管走：目标已存在）
    store = eq.EvalQueueStore()
    decoy = _WORK / "eval_queue.sqlite"
    try:
        before = R.measure_areas()
        expect = sum(
            (Path(f"{store._path}{s}").stat().st_size if Path(f"{store._path}{s}").exists() else 0)
            for s in ("", "-wal", "-shm")
        )
        check(before["eval_queue_db"] == expect and expect > 0,
              "eval_queue_db = 子目录里的库 + -wal + -shm（三件套一起量）",
              f"{before['eval_queue_db']} / {expect}")

        # 负对照：根上放一份同名的老库（真 sqlite，带一条超龄 done 行）——不许被计入
        _real_db(decoy, _EVAL_DDL, (
            "INSERT INTO eval_queue (kind,trace_id,state,created_at,updated_at)"
            " VALUES ('judge','tr-root','done','2020-01-01T00:00:00+00:00',"
            "'2020-01-01T00:00:00+00:00')",
            (),
        ))
        after = R.measure_areas()
        check(after["eval_queue_db"] == before["eval_queue_db"],
              "负对照：根上同名老库**不被计入**（量的是子目录，不是数据根目录）",
              f"{after['eval_queue_db']} == {before['eval_queue_db']}")

        # 清理也只认新路径：新位置那条超龄 done 删掉，根上同名库里的那条一行不动
        _real_db(_WORK / "eval_queue" / "eval_queue.sqlite", _EVAL_DDL, (
            "INSERT INTO eval_queue (kind,trace_id,state,created_at,updated_at)"
            " VALUES ('judge','tr-new','done','2020-01-01T00:00:00+00:00',"
            "'2020-01-01T00:00:00+00:00')",
            (),
        ))
        res = R.run_once()
        entry = res["targets"]["eval_queue"]
        check(entry.get("removed") == 1, "只删了 1 条（新位置那条超龄 done）", str(entry))
        conn = sqlite3.connect(str(decoy))
        try:
            left = [r[0] for r in conn.execute("SELECT trace_id FROM eval_queue").fetchall()]
        finally:
            conn.close()
        check(left == ["tr-root"],
              "负对照：根上同名库里的行**一行没动**（清理没跑偏到数据根目录）", str(left))
    finally:
        decoy.unlink(missing_ok=True)
        # 新库文件的连接还在（EvalQueueStore 没有 close），Windows 下删不掉 ——
        # 交给 main() 末尾的 rmtree(ignore_errors=True)


# ── ⑥ 接线：lifespan 里搬 + 顺序在起 store 之前 ─────────────────────
def verify_wiring() -> None:
    section("⑥ 接线：lifespan 首启一次搬齐，且排在起补写器/保留线程之前")
    from agent.utils import sqlite_paths as SP

    r = _mkroot("adopt-all")
    for name in SP.STORE_NAMES:
        (r / f"{name}.sqlite").write_text("db", encoding="utf-8")
        (r / f"{name}.sqlite-wal").write_text("wal", encoding="utf-8")
    moved = SP.adopt_all_stores(r)
    check(all(moved.values()),
          "adopt_all_stores() 一次把三个库都搬了（不是只搬第一个）", str(moved))
    check(all((r / n / f"{n}.sqlite-wal").exists() for n in SP.STORE_NAMES),
          "三个库的 -wal 都跟着走了")
    check(SP.adopt_all_stores(r) == dict.fromkeys(SP.STORE_NAMES, False),
          "对已归位的根再跑一次 = 全 False（幂等）")
    check(SP.adopt_all_stores(_WORK) == dict.fromkeys(SP.STORE_NAMES, False),
          "对真实数据根跑一次搬不动任何东西（此时已无老文件）")

    src = (_SRC / "api" / "custom_app.py").read_text(encoding="utf-8")
    check("adopt_all_stores" in src, "custom_app._lifespan 里真的调了 adopt_all_stores")
    i_adopt = src.index("from agent.utils.sqlite_paths import adopt_all_stores")
    i_reaper = src.index("from agent.subagents.pending_terminal import start_reaper")
    i_ret = src.index("from agent.utils.retention import start_maintenance")
    check(i_adopt < i_reaper and i_adopt < i_ret,
          "顺序正确：归位在 start_reaper/start_maintenance **之前**"
          "（它俩会建连，搬运前提是没人打开过老库）",
          f"adopt@{i_adopt} < reaper@{i_reaper}, retention@{i_ret}")


def main() -> int:
    print(f"运行时库「一库一目录」+ 老文件接管验证（临时根 {_WORK}）")
    verify_path_shape()
    verify_default_locations()
    verify_adoption()
    verify_concurrent_adoption()
    verify_retention_follows()
    verify_wiring()

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 60}")
    for ok, label in results:
        if not ok:
            print(f"  [FAIL] {label}")
    print(f"{passed}/{total} 通过")
    shutil.rmtree(_WORK, ignore_errors=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
