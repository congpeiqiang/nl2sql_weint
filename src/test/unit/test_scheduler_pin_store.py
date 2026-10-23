"""定时任务固化查询：存储层加列 + 幂等迁移 + pin 写方法。"""

import sqlite3

from agent.scheduler.store import ScheduledTaskStore

PIN_COLS = {
    "pin_status",
    "pin_spec",
    "pin_semantic_version",
    "pin_saved_at",
    "pin_invalid_reason",
    "pin_source_thread_id",
}


def _legacy_db(path):
    """模拟线上老库：只有 2026-10 之前的列。"""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE scheduled_tasks (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
        prompt TEXT NOT NULL, db_name TEXT NOT NULL DEFAULT '',
        sched_kind TEXT NOT NULL, sched_expr TEXT NOT NULL,
        next_run_at TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    c.execute(
        "INSERT INTO scheduled_tasks (id,owner,name,prompt,sched_kind,sched_expr,created_at,updated_at)"
        " VALUES ('t1','u','n','p','interval','5','x','x')"
    )
    c.commit()
    c.close()


def _insert(s):
    s.insert_task(
        {
            "id": "t1",
            "owner": "u",
            "name": "n",
            "prompt": "p",
            "sched_kind": "interval",
            "sched_expr": "5",
            "created_at": "x",
            "updated_at": "x",
        }
    )


def test_new_db_has_pin_columns(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(scheduled_tasks)")}
    assert PIN_COLS <= cols
    run_cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(task_runs)")}
    assert {"pin_mode", "pin_reason"} <= run_cols


def test_legacy_db_is_migrated(tmp_path):
    p = tmp_path / "legacy.sqlite"
    _legacy_db(p)
    s = ScheduledTaskStore(db_path=p)
    row = s.get_task("t1")
    assert row["pin_status"] == "" and row["pin_spec"] == ""


def test_migration_is_idempotent(tmp_path):
    """重启后再开一次库不该炸（ALTER TABLE 重复加列是错误）。"""
    p = tmp_path / "s.sqlite"
    ScheduledTaskStore(db_path=p).close()
    s = ScheduledTaskStore(db_path=p)
    cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(scheduled_tasks)")}
    assert PIN_COLS <= cols


def test_set_invalidate_clear(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    _insert(s)
    s.set_pin(
        "t1",
        spec_json='{"v":1}',
        semantic_version="abc",
        source_thread_id="th",
        now_iso="2026-10-23T00:00:00Z",
    )
    r = s.get_task("t1")
    assert r["pin_status"] == "active" and r["pin_invalid_reason"] == ""
    assert r["pin_spec"] == '{"v":1}'
    assert r["pin_semantic_version"] == "abc"
    assert r["pin_source_thread_id"] == "th"
    assert r["pin_saved_at"] == "2026-10-23T00:00:00Z"

    s.invalidate_pin("t1", "semantic_version_changed: a -> b", "2026-10-24T00:00:00Z")
    r = s.get_task("t1")
    assert r["pin_status"] == "invalid"
    assert r["pin_invalid_reason"].startswith("semantic_version_changed")
    assert r["pin_spec"] == '{"v":1}'  # invalid 不清 spec，便于人工排查

    s.clear_pin("t1", "db_changed: a -> b", "2026-10-25T00:00:00Z")
    r = s.get_task("t1")
    assert r["pin_status"] == "" and r["pin_spec"] == ""
    assert r["pin_semantic_version"] == "" and r["pin_source_thread_id"] == ""
    assert r["pin_invalid_reason"].startswith("db_changed")


def test_pin_methods_return_false_for_missing_task(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    assert s.set_pin("nope", spec_json="{}", semantic_version="", source_thread_id="", now_iso="x") is False
    assert s.invalidate_pin("nope", "x", "y") is False
    assert s.clear_pin("nope", "x", "y") is False


def test_update_task_cannot_write_pin(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    _insert(s)
    assert s.update_task("t1", {"pin_status": "active"}) is False
    assert s.get_task("t1")["pin_status"] == ""


def test_update_task_still_raises_on_unknown_column(tmp_path):
    """已知但不可更新 → False（策略拒绝）；未知列 → 抛（编程错误，别静默）。"""
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    _insert(s)
    try:
        s.update_task("t1", {"DROP TABLE x": "1"})
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("未知列必须抛 ValueError")


def test_record_run_pin_mode(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    _insert(s)
    s.record_run(
        "t1",
        "th",
        "r1",
        "2026-10-23T00:00:00Z",
        pin_mode="fallback",
        pin_reason="columns_changed: missing=[b]",
    )
    run = s.list_runs("t1")[0]
    assert run["pin_mode"] == "fallback"
    assert "columns_changed" in run["pin_reason"]


def test_record_run_defaults_to_normal(tmp_path):
    """旧四参调用必须继续可用，且落到 pin_mode='normal'。"""
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    _insert(s)
    s.record_run("t1", "th", "r1", "2026-10-23T00:00:00Z")
    run = s.list_runs("t1")[0]
    assert run["pin_mode"] == "normal" and run["pin_reason"] == ""
