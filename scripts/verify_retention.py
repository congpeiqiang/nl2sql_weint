# -*- coding: utf-8 -*-
"""P2-5 存储保留策略（retention）+ 磁盘水位 验证。

跑法：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_retention.py

⚠️ **安全前提（本脚本自己钉死）**：retention 会**真删文件/真删行**。所以：
  ① `AGENT_DATA_ROOT` 指向临时目录（`_WORK`，**必须在 import 任何 agent 模块之前设**：
     `manager._DATA_ROOT` 是导入期读的，设晚了工作区就还指真实目录）；
  ② `retention._ws_subdirs` 仍被替换成**只返回临时目录**（默认实现现在解析的是
     `<AGENT_DATA_ROOT>/workspace`，在本脚本里 = `_WORK/workspace` ⇒ 本就安全；
     这层 stub 是第二道保险，别再让它去遍历盘上任何"真实"工作区）；
  ③ 每段开跑前断言"将被删的路径都在临时目录下"，不在就整段红掉（宁可红，也不要误删）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
sys.path.insert(0, str(_SRC))

_WORK = Path(tempfile.mkdtemp(prefix="nl2sql-verify-p25-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
for _k in ("NL2SQL_AUTH_DISABLED", "NL2SQL_FORCE_PASSWORD_CHANGE", "LANGFUSE_ENABLE"):
    os.environ.pop(_k, None)

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


def _iso_ago(days: float) -> str:
    return datetime.fromtimestamp(
        time.time() - days * 86400.0, timezone.utc
    ).isoformat(timespec="seconds")


def _ws(name: str) -> Path:
    """造一个临时工作区（只在本脚本的临时根下）。"""
    d = _WORK / "ws" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 安全护栏：本脚本只允许在 _WORK 下删东西 ────────────────────────
def _assert_inside_work(p: Path) -> None:
    rp = p.resolve()
    assert str(rp).startswith(str(_WORK.resolve())), f"拒绝操作临时目录之外的路径: {rp}"


def _install_ws_stub(dirs_by_name: dict[str, list[Path]]) -> None:
    """把 `_ws_subdirs` 换成"只给临时目录"，并**顺手校验**它们都在临时根下。"""
    from agent.utils import retention as R

    for name, ds in dirs_by_name.items():
        for d in ds:
            _assert_inside_work(d)

    def _stub(name: str, _map=dirs_by_name) -> list[Path]:
        return [d for d in _map.get(name, []) if d.is_dir()]

    R._ws_subdirs = _stub  # type: ignore[assignment]


def _touch(p: Path, age_days: float, size: int = 64) -> Path:
    """写一个指定"几天前修改"的文件（retention 按 mtime 判龄）。"""
    _assert_inside_work(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    ts = time.time() - age_days * 86400.0
    os.utime(p, (ts, ts))
    return p


# ── ① env 口径 ────────────────────────────────────────────────────
def verify_env_conventions() -> None:
    section("① 天数口径：0=关闭 / 非数字退回默认 / 负值按关闭（与 P2-3、P2-4 同一套约定）")
    from agent.utils import retention as R

    check(R.DEFAULT_DAYS["report"] == 0.0 and R.DEFAULT_DAYS["feedback"] == 0.0,
          "用户可见物（report/feedback）默认**关闭**，机器数据默认开",
          f"report={R.DEFAULT_DAYS['report']} feedback={R.DEFAULT_DAYS['feedback']}")
    check(R._days_for("trace_events") == 30.0, "trace_events 默认 30 天")
    check(R._days_for("workspace_tmp") == 7.0, "workspace_tmp 默认 7 天（草稿，不需留太久）")

    os.environ["NL2SQL_RETENTION_DAYS_TRACE_EVENTS"] = "7"
    check(R._days_for("trace_events") == 7.0, "env 能改天数")
    os.environ["NL2SQL_RETENTION_DAYS_TRACE_EVENTS"] = "0"
    check(R._days_for("trace_events") == 0.0, "0 = 关闭该目标")
    os.environ["NL2SQL_RETENTION_DAYS_TRACE_EVENTS"] = "abc"
    check(R._days_for("trace_events") == 30.0, "负对照：非数字 → 退回默认（绝不静默变 0=关闭）")
    os.environ["NL2SQL_RETENTION_DAYS_TRACE_EVENTS"] = "-5"
    check(R._days_for("trace_events") == -5.0 and R._days_for("trace_events") <= 0,
          "负值按关闭算（<=0 一律不动手）")
    os.environ.pop("NL2SQL_RETENTION_DAYS_TRACE_EVENTS", None)

    os.environ["NL2SQL_RETENTION_ENABLED"] = "0"
    off = R.run_once()
    check(off.get("skipped") and off["removed"] == 0 and not off["targets"],
          "总开关 =0 → 一轮什么都不做（连目标都不评估）", str(off.get("skipped")))
    os.environ["NL2SQL_RETENTION_ENABLED"] = "false"
    check(R.enabled() is False, "'false' 也认（不只是 '0'）")
    os.environ.pop("NL2SQL_RETENTION_ENABLED", None)
    check(R.enabled() is True, "清掉 env → 回到默认开")


# ── ② trace_events / session_lineage（真库真删）──────────────────────
def verify_trace_events() -> None:
    section("② trace_events + session_lineage：按龄删，边界不动")
    from agent.utils import retention as R

    db = _WORK / "shared" / "trace" / "traces.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER NOT NULL,
            thread_id TEXT NOT NULL, agent_type TEXT NOT NULL, task_id TEXT DEFAULT '',
            parent_thread_id TEXT DEFAULT '', event_type TEXT NOT NULL,
            timestamp REAL NOT NULL, data_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE session_lineage (
            thread_id TEXT PRIMARY KEY, parent_thread_id TEXT DEFAULT '',
            agent_type TEXT NOT NULL, created_at REAL NOT NULL,
            status TEXT DEFAULT 'active', metadata_json TEXT NOT NULL DEFAULT '{}');
        """
    )
    old = time.time() - 40 * 86400.0
    fresh = time.time() - 1 * 86400.0
    conn.execute(
        "INSERT INTO trace_events (seq, thread_id, agent_type, event_type, timestamp) VALUES (1,'t-old','nl2sql','x',?)",
        (old,),
    )
    conn.execute(
        "INSERT INTO trace_events (seq, thread_id, agent_type, event_type, timestamp) VALUES (2,'t-new','nl2sql','x',?)",
        (fresh,),
    )
    conn.execute(
        "INSERT INTO session_lineage (thread_id, agent_type, created_at) VALUES ('t-old','nl2sql',?)",
        (old,),
    )
    conn.execute(
        "INSERT INTO session_lineage (thread_id, agent_type, created_at) VALUES ('t-new','nl2sql',?)",
        (fresh,),
    )
    conn.commit()

    orig = R._trace_db
    R._trace_db = lambda: db  # type: ignore[assignment]
    try:
        dry = R.run_once(dry_run=True)
        ev = dry["targets"]["trace_events"]
        check(ev.get("removed") == 2, "dry-run 报出会删 2 条（事件+谱系各 1）", json.dumps(ev))
        check(
            conn.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0] == 2,
            "负对照：dry-run **一行都没删**",
        )
        res = R.run_once()
        ev2 = res["targets"]["trace_events"]
        check(ev2.get("removed") == 2, "真跑删掉 2 条", json.dumps(ev2))
        left = conn.execute("SELECT thread_id FROM trace_events").fetchall()
        check([r[0] for r in left] == ["t-new"], "只删 40 天前的，1 天前的留下", str(left))
        check(
            conn.execute("SELECT COUNT(*) FROM session_lineage").fetchone()[0] == 1,
            "谱系表同样只删过期的",
        )
        check("note" in ev2 and "vacuum" in ev2["note"],
              "如实标注『删行不缩文件，需 --vacuum』（不许让人以为盘立刻小了）")

        # 关掉该目标 → 一行都不动
        os.environ["NL2SQL_RETENTION_DAYS_TRACE_EVENTS"] = "0"
        try:
            res2 = R.run_once()
        finally:
            os.environ.pop("NL2SQL_RETENTION_DAYS_TRACE_EVENTS", None)
        check(res2["targets"]["trace_events"].get("disabled") is True,
              "天数=0 → 该目标被跳过（不是当成「删 0 天前的」=全删）")

        # 真空：删行后用 --vacuum 把文件缩回去
        conn.execute(
            "INSERT INTO trace_events (seq, thread_id, agent_type, event_type, timestamp, data_json)"
            " VALUES (9,'t-big','nl2sql','x',?,?)",
            (old, "y" * 200000),
        )
        conn.commit()
        conn.close()
        before = db.stat().st_size
        R.run_once()
        mid = db.stat().st_size
        vac = R.vacuum("trace")
        check(vac["freed_bytes"] > 0 and vac["after_bytes"] < mid,
              "删行后文件不缩、--vacuum 才缩（正是 note 说的那件事）",
              f"before={before} afterDelete={mid} afterVacuum={vac['after_bytes']}")
    finally:
        R._trace_db = orig  # type: ignore[assignment]


# ── ③ eval_queue：只删终态 ──────────────────────────────────────────
def verify_eval_queue() -> None:
    section("③ eval_queue：只删**终态**行（pending/running 一律不动）")
    from agent.utils import retention as R

    # 2026-09-25：运行时库归位到同名子目录（一库一目录），清单跟着落点走
    db = _WORK / "eval_queue" / "eval_queue.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE eval_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, trace_id TEXT NOT NULL,
            question_thread TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT DEFAULT '');
        """
    )
    rows = [
        ("done", _iso_ago(40)),      # 老 + 终态 → 删
        ("failed", _iso_ago(40)),    # 老 + 终态 → 删
        ("pending", _iso_ago(40)),   # 老但在途 → **不删**
        ("running", _iso_ago(40)),   # 老但在途 → **不删**
        ("done", _iso_ago(1)),       # 新 → 不删
    ]
    for state, ts in rows:
        conn.execute(
            "INSERT INTO eval_queue (kind, trace_id, state, created_at, updated_at)"
            " VALUES ('judge', ?, ?, ?, ?)",
            (f"tr-{state}-{ts[-5:]}", state, ts, ts),
        )
    conn.commit()
    conn.close()

    res = R.run_once()
    entry = res["targets"]["eval_queue"]
    check(entry.get("removed") == 2, "删掉 2 条（40 天前的 done/failed）", json.dumps(entry))
    conn = sqlite3.connect(str(db))
    left = sorted(r[0] for r in conn.execute("SELECT state FROM eval_queue").fetchall())
    check(left == ["done", "pending", "running"],
          "负对照：老但在途的 pending/running **还在**，新的 done 也在", str(left))
    conn.close()


# ── ④ timestamp 口径（踩过的坑，钉死）───────────────────────────────
def verify_timestamp_format() -> None:
    section("④ 时间戳口径必须与库内同构（空格 vs 'T' 曾让「当天全判成没到期」）")
    from agent.utils import retention as R

    s = R._iso(time.time())
    check("T" in s and s.endswith("+00:00"), "cutoff 是 isoformat 带时区（不是空格分隔）", s)
    # 反证：空格口径在同一天里恒"更小"，会把当天的行全判成没到期
    spaced = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    same_day_iso = datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="seconds")
    check(spaced < same_day_iso,
          "负对照：空格口径确实 < 同刻的 T 口径（说明用空格会漏删）",
          f"{spaced!r} < {same_day_iso!r}")

    from agent.eval import eval_queue as eq

    check(eq._now()[:11] == s[:11] and "T" in eq._now(),
          "eval_queue 写库用的也是 T 口径（两边同构才有可比性）", eq._now())


# ── ⑤ 工作区目录（tmp / large_tool_results / conversation_history）──
def verify_workspace_dirs() -> None:
    section("⑤ 工作区中间产物：按 mtime 删，目录留着、空子目录顺手清掉")
    from agent.utils import retention as R

    ws1, ws2 = _ws("a"), _ws("b")
    old_tmp = _touch(ws1 / "tmp" / "sub" / "old.txt", 10)
    fresh_tmp = _touch(ws1 / "tmp" / "fresh.txt", 0.5)
    old_large = _touch(ws2 / "large_tool_results" / "big.json", 45, size=2048)
    fresh_large = _touch(ws2 / "large_tool_results" / "new.json", 2, size=1024)
    old_hist = _touch(ws1 / "conversation_history" / "h.json", 45)
    # process_data 是**两层嵌套**（thread_id/skill/*.json），与上面三种平铺目录不同
    old_pd = _touch(ws1 / "nl2sql_process_data" / "tid-1" / "sql-query" / "q1_tool-1.json", 45)
    fresh_pd = _touch(ws1 / "nl2sql_process_data" / "tid-2" / "sql-query" / "q2_tool-1.json", 1)

    _install_ws_stub({
        "tmp": [ws1 / "tmp"],
        "large_tool_results": [ws2 / "large_tool_results"],
        "conversation_history": [ws1 / "conversation_history"],
        "nl2sql_process_data": [ws1 / "nl2sql_process_data"],
        "report": [],
    })
    res = R.run_once()
    t = res["targets"]
    check(t["workspace_tmp"].get("removed") == 1 and not old_tmp.exists(),
          "tmp：10 天前的删掉（阈值 7 天）")
    check(fresh_tmp.exists(), "负对照：半天前的留下")
    check(t["large_tool_results"].get("removed") == 1 and not old_large.exists()
          and fresh_large.exists(),
          "large_tool_results：45 天前的删、2 天前的留")
    check(t["conversation_history"].get("removed") == 1 and not old_hist.exists(),
          "conversation_history 同样按 45>30 天删")
    check(t["nl2sql_process_data"].get("removed") == 1 and not old_pd.exists(),
          "nl2sql_process_data：45 天前的删（原先不在保留策略里 ⇒ 只涨不跌）")
    check(fresh_pd.exists(), "负对照：1 天前的 debug dump 留下")
    check(not (ws1 / "nl2sql_process_data" / "tid-1").exists(),
          "清空后 thread_id 子目录也删掉（不留空壳目录）")
    check((ws1 / "tmp").is_dir(), "顶层目录本身留着（只删里面的文件）")
    check(not (ws1 / "tmp" / "sub").exists(), "清空的子目录顺手删掉（不留一堆空目录）")
    check(t["large_tool_results"].get("freed_bytes", 0) >= 2048,
          "报到 freed_bytes（文件是真占盘，与删行不同）",
          str(t["large_tool_results"].get("freed_bytes")))

    # 不存在的目录 → 跳过而不是报错/算 0
    _install_ws_stub({"workspace_tmp": [ws1 / "tmp" / "nope"], "report": []})
    res2 = R.run_once()
    check(res2["targets"]["workspace_tmp"].get("skipped") == "无该目录",
          "目录不存在 → 明确跳过（不是静默 0 条）",
          json.dumps(res2["targets"]["workspace_tmp"]))

    # 危险根：AGENT_DATA_ROOT 被配成文件系统根/家目录 → 拒绝（护栏必须仍然有效）
    check(R._unsafe_root(Path("/")) is True, "文件系统根被认成危险根")
    check(R._unsafe_root(Path.home()) is True, "家目录也被认成危险根")
    check(R._unsafe_root(_WORK) is False, "正常目录不算危险根")

    # 负对照：工作区被指到家目录 → 一个候选目录都不返回。
    # 单工作区后根来自 `WorkspaceManager.active_workspace`，所以这里注入一个"坏管理器"
    # （`_ws_subdirs` 是函数内 `from agent.workspace_manager import get_workspace_manager`，
    #  每次调用都重新取值 ⇒ 打模块属性即可生效）。
    import agent.workspace_manager as _wm_pkg

    class _BadManager:
        active_workspace = Path.home()

    _orig_get = _wm_pkg.get_workspace_manager
    _wm_pkg.get_workspace_manager = lambda: _BadManager()  # type: ignore[assignment]
    try:
        check(R._ws_subdirs("tmp") == [], "负对照：工作区指到家目录 → 一个候选目录都不返回")
    finally:
        _wm_pkg.get_workspace_manager = _orig_get  # type: ignore[assignment]


# ── ⑥ report：文件与账本成对删（且默认关闭）─────────────────────────
def verify_report_pairing() -> None:
    section("⑥ report：默认关闭；打开时**文件与 report_owner 成对删**")
    from agent.utils import retention as R
    from agent.auth import grants

    ws = _ws("r")
    rdir = ws / "report"
    f_old = _touch(rdir / "老报告.md", 40)
    f_new = _touch(rdir / "新报告.md", 1)
    # 另一个工作区的同名文件（用来证明"不是按'账本里不存在的名字'乱清"）
    ws_b = _ws("rb")
    f_other = _touch(ws_b / "report" / "老报告.md", 1)

    grants.record_report_owner("老报告.md", "u1", "t1")
    grants.record_report_owner("新报告.md", "u1", "t1")
    grants.record_report_owner("别人的.md", "u2", "t2")
    check(grants.report_owner_of("老报告.md") == "u1", "前置：账本已登记")

    _install_ws_stub({"report": [rdir], "tmp": [], "large_tool_results": [], "conversation_history": []})
    res = R.run_once()
    check(res["targets"]["report"].get("disabled") is True, "默认关闭：一轮里该目标被跳过")
    check(f_old.exists() and grants.report_owner_of("老报告.md") == "u1",
          "负对照：关闭时文件与账本都不动")

    os.environ["NL2SQL_RETENTION_DAYS_REPORT"] = "30"
    try:
        dry = R.run_once(dry_run=True)
        check(dry["targets"]["report"].get("removed") == 1 and f_old.exists(),
              "打开后 dry-run 报 1 个、但**什么都没删**")
        res2 = R.run_once()
        entry = res2["targets"]["report"]
        check(entry.get("removed") == 1 and not f_old.exists(), "真跑：40 天前的报告被删")
        check(f_new.exists(), "负对照：1 天前的报告留着")
        check(grants.report_owner_of("老报告.md") == "",
              "账本行**同步删掉**（不留「有归属但文件不在」）")
        check(grants.report_owner_of("新报告.md") == "u1"
              and grants.report_owner_of("别人的.md") == "u2",
              "负对照：别人的账本行**没被误删**（不按「文件不存在」乱清）",
              f"owner_rows={entry.get('owner_rows')}")
        check(f_other.exists(), "负对照：另一个工作区的同名文件没被碰")
    finally:
        os.environ.pop("NL2SQL_RETENTION_DAYS_REPORT", None)


# ── ⑦ feedback：默认关闭，且不动标注 ───────────────────────────────
def verify_feedback_off_by_default() -> None:
    section("⑦ feedback：默认关闭（金标数据）；打开也只删打分行、不动标注行")
    from agent.utils import retention as R

    db = _WORK / "shared" / "feedback" / "message_feedback.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE feedback (
            thread_id TEXT NOT NULL, message_id TEXT NOT NULL, rating TEXT NOT NULL,
            note TEXT DEFAULT '', version INTEGER DEFAULT 1, created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '', context_json TEXT DEFAULT '{}', question TEXT DEFAULT '',
            sql TEXT DEFAULT '', feedback_type TEXT DEFAULT '', cube_spec TEXT DEFAULT '',
            PRIMARY KEY (thread_id, message_id));
        CREATE TABLE feedback_annotation (
            thread_id TEXT NOT NULL, message_id TEXT NOT NULL, note TEXT DEFAULT '',
            rating TEXT DEFAULT '', status TEXT DEFAULT 'queued', created_at TEXT DEFAULT '',
            PRIMARY KEY (thread_id, message_id));
        """
    )
    conn.execute(
        "INSERT INTO feedback (thread_id, message_id, rating, created_at, updated_at)"
        " VALUES ('t1','m1','bad',?,?)",
        (_iso_ago(40), _iso_ago(40)),
    )
    conn.execute(
        "INSERT INTO feedback (thread_id, message_id, rating, created_at, updated_at)"
        " VALUES ('t2','m2','good',?,?)",
        (_iso_ago(1), _iso_ago(1)),
    )
    conn.execute(
        "INSERT INTO feedback_annotation (thread_id, message_id, note, created_at)"
        " VALUES ('t1','m1','人工标注',?)",
        (_iso_ago(40),),
    )
    conn.commit()
    conn.close()

    res = R.run_once()
    check(res["targets"]["feedback"].get("disabled") is True, "默认关闭")
    conn = sqlite3.connect(str(db))
    check(conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2,
          "负对照：关闭时一行都不删")
    conn.close()

    os.environ["NL2SQL_RETENTION_DAYS_FEEDBACK"] = "30"
    try:
        res2 = R.run_once()
        check(res2["targets"]["feedback"].get("removed") == 1, "打开后删 40 天前的那条打分")
        conn = sqlite3.connect(str(db))
        left = [r[0] for r in conn.execute("SELECT message_id FROM feedback").fetchall()]
        ann = conn.execute("SELECT COUNT(*) FROM feedback_annotation").fetchone()[0]
        conn.close()
        check(left == ["m2"], "负对照：新的那条还在", str(left))
        check(ann == 1, "**标注行一行没动**（人工劳动，只能人工清）")
    finally:
        os.environ.pop("NL2SQL_RETENTION_DAYS_FEEDBACK", None)


# ── ⑧ 磁盘水位 + 指标 + 告警规则 ───────────────────────────────────
def verify_disk_and_metrics() -> None:
    section("⑧ 磁盘水位：指标落点 + 告警规则（连续 3 轮才响）")
    from agent.utils import retention as R
    import api.metrics as M

    disk = R.disk_status()
    check(disk.get("free_ratio") is not None and 0 <= disk["free_ratio"] <= 1,
          "disk_status 给出 free/used 比例", json.dumps({k: disk[k] for k in ("free_ratio", "used_ratio")}))
    check(disk.get("path") == str(_WORK), "量的是 AGENT_DATA_ROOT 那个卷", disk.get("path"))

    rules = {r.name: r for r in M._rules()}
    check("disk_low" in rules, "已注册 disk_low 告警规则")
    r = rules["disk_low"]
    check(r.env == "NL2SQL_ALERT_DISK_FREE_PCT", "阈值走 env", r.env)
    check(r.samples == 3, "水位型指标连续 3 轮才响（单次抖动不报）")
    check(r.trip(5.0, 10.0) is True and r.trip(20.0, 10.0) is False,
          "判据是**剩余**比例（越小越危险），不是已用")
    check(r.value({"disk_free_ratio": 0.03}) == 3.0, "读数换算成百分比")
    check(r.value({}) is None, "采不到 → None（不写假 0，否则告警永不触发）")

    readings = __import__("asyncio").run(M.sample_once())
    check(readings.get("disk_free_ratio") is not None, "采样轮真的量到磁盘剩余比例",
          str(readings.get("disk_free_ratio")))
    from prometheus_client import REGISTRY

    names = {m.name for m in REGISTRY.collect()}
    check("nl2sql_disk_free_bytes" in names and "nl2sql_disk_used_ratio" in names,
          "两个磁盘指标已注册进同一个 REGISTRY（/metrics 里能看到）")
    check("nl2sql_data_bytes" in names, "各区域占用指标也已注册（数据来自清理轮缓存）")

    # 首轮之前不产出：把缓存清空后采样不应把它写成 0
    from agent.utils import retention as RR

    saved = RR._LAST_SIZES
    RR._LAST_SIZES = {}
    try:
        M.DATA_BYTES.labels("report").set(12345)  # 先污染，看它会不会被翻成 0
        M.sample_once()  # 不 await 也行：只验"缓存空时不覆盖"
        import asyncio

        asyncio.run(M.sample_once())
        val = REGISTRY.get_sample_value("nl2sql_data_bytes", {"area": "report"})
        check(val == 12345 or val is None,
              "缓存为空时**不写 0**（不假造「这个区域是空的」）", str(val))
    finally:
        RR._LAST_SIZES = saved


# ── ⑨ 维护线程生命周期 + lifespan 接线 ─────────────────────────────
def verify_thread_and_wiring() -> None:
    section("⑨ 维护线程：起停/lifespan 接线（与 P2-4 同一套形态）")
    from agent.utils import retention as R

    _install_ws_stub({"tmp": [], "large_tool_results": [], "conversation_history": [], "report": []})
    os.environ["NL2SQL_RETENTION_INTERVAL_SECS"] = "300"
    try:
        first = R.start_maintenance()
        second = R.start_maintenance()
        check(first is True and second is False, "start 幂等（第二次不再起线程）")
        # 首轮是同步跑完的（线程起来就清一轮）→ 等它把 last_run 写上
        deadline = time.monotonic() + 15
        while not R.last_run() and time.monotonic() < deadline:
            time.sleep(0.05)
        check(bool(R.last_run()), "启动即清一轮：last_run 有记录（不等人来问）",
              json.dumps(R.last_run(), ensure_ascii=False))
        R.stop_maintenance(timeout=5)
        check(R._THREAD is None or not R._THREAD.is_alive(), "stop 能停干净")
        again = R.start_maintenance()
        alive = R._THREAD is not None and R._THREAD.is_alive()
        R.stop_maintenance(timeout=5)
        check(again is True and alive, "停了还能再起（lifespan 反复进入不炸）")
    finally:
        R.stop_maintenance(timeout=5)
        os.environ.pop("NL2SQL_RETENTION_INTERVAL_SECS", None)

    # 真 lifespan：起维护 → 服务运行 → 停机时停维护
    import api.custom_app as custom_app

    calls: list[str] = []
    orig_start, orig_stop = R.start_maintenance, R.stop_maintenance
    R.start_maintenance = lambda: (calls.append("start"), True)[1]  # type: ignore[assignment]
    R.stop_maintenance = lambda timeout=2.0: calls.append("stop")  # type: ignore[assignment]

    async def _drive():
        async with custom_app._lifespan(None):
            calls.append("inside")

    try:
        import asyncio

        asyncio.run(asyncio.wait_for(_drive(), timeout=60))
    finally:
        R.start_maintenance, R.stop_maintenance = orig_start, orig_stop  # type: ignore[assignment]
    check(calls[0] == "start" and "inside" in calls and calls[-1] == "stop",
          "真 lifespan：起维护线程 → 服务运行 → 停机时停它", str(calls))
    check(calls.index("start") < calls.index("inside"), "在 yield **之前**起（启动即开始治理磁盘）")

    src = (_SRC / "api" / "custom_app.py").read_text(encoding="utf-8")
    check("start_maintenance()" in src and "stop_maintenance()" in src,
          "接线写在 custom_app（与 P2-3/P2-4 同一个插入点，不改 langgraph 自身）")
    rsrc = (_SRC / "agent" / "utils" / "retention.py").read_text(encoding="utf-8")
    check("asyncio" not in rsrc.split("import argparse")[1].split("def _env_float")[0]
          or "to_thread" not in rsrc,
          "清理是**同步**实现（跑在后台线程里，不经事件循环 —— P1-14 口径）")


# ── ⑩ CLI ─────────────────────────────────────────────────────────
def verify_cli() -> None:
    section("⑩ CLI（运维入口）")
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    env["AGENT_DATA_ROOT"] = str(_WORK)
    for args, label in ((["--status"], "状态"), (["--run", "--dry-run"], "dry-run 一轮")):
        # ⚠️ Windows 上 `text=True` 默认按 **GBK** 解码子进程 stdout，中文 JSON 会
        # 直接抛 UnicodeDecodeError（`stdout` 变 None）——必须显式 utf-8。
        r = subprocess.run(
            [sys.executable, "-m", "agent.utils.retention", *args],
            cwd=str(_REPO), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        ok = r.returncode == 0
        check(ok, f"`{' '.join(args)}`（{label}）退出码 0", (r.stderr or "")[-160:])
        if ok:
            try:
                json.loads(r.stdout)
                check(True, f"`{' '.join(args)}` 输出 JSON")
            except Exception as e:  # noqa: BLE001
                check(False, f"`{' '.join(args)}` 输出 JSON", str(e)[:120])


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    print(f"P2-5 保留策略 + 磁盘水位验证（临时根 {_WORK}）")
    verify_env_conventions()
    verify_trace_events()
    verify_eval_queue()
    verify_timestamp_format()
    verify_workspace_dirs()
    verify_report_pairing()
    verify_feedback_off_by_default()
    verify_disk_and_metrics()
    verify_thread_and_wiring()
    verify_cli()

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 60}")
    for ok, label in results:
        if not ok:
            print(f"  [FAIL] {label}")
    print(f"{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
