"""定时任务固化查询：编排层（取结果 → 固化 → 验证 → 落库）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler import pin_service


@pytest.fixture
def now():
    return datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


async def test_semantic_version_uses_commit_not_tag(monkeypatch):
    monkeypatch.setattr(pin_service, "project_path_for", lambda db: "/proj")
    monkeypatch.setattr(
        pin_service,
        "repo_info",
        lambda p: {"commit": "deadbeef" * 5, "tag": "v1.10.2", "branch": "main"},
    )
    assert await pin_service.semantic_version_of("d") == "deadbeef" * 5


async def test_direct_db_has_empty_version(monkeypatch):
    monkeypatch.setattr(pin_service, "project_path_for", lambda db: None)
    assert await pin_service.semantic_version_of("d") == ""


def _seed(tmp_path):
    """一个已建好的任务行 + store。"""
    from agent.scheduler.store import ScheduledTaskStore

    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    s.insert_task(
        {
            "id": "t1",
            "owner": "u",
            "name": "n",
            "prompt": "p",
            "db_name": "d",
            "sched_kind": "interval",
            "sched_expr": "5",
            "tz": "Asia/Shanghai",
            "created_at": "x",
            "updated_at": "x",
        }
    )
    return s, s.get_task("t1")


def _wire(monkeypatch, *, columns=("a",), exec_error=None):
    """把 capture/执行/版本查三处都打成桩，只测编排分支。"""

    async def fake_capture(
        task,
        *,
        thread_id,
        now,
        date_bindings=None,
        accept_constants=False,
        client=None,
    ):
        return (
            {
                "v": 1,
                "channel": "sql",
                "db_name": "d",
                "tz": "Asia/Shanghai",
                "sql_template": "SELECT 1",
                "params": [],
                "expect_columns": ["a"],
                "row_limit": 5000,
                "semantic_version": "",
                "source_thread_id": "th",
                "captured_at": "x",
                "warnings": [],
            },
            [],
        )

    class _Res:
        sql, cube_query, cube_args, cube_tool, channel = "SELECT 1", "", {}, "", "sql"
        rows, truncated, row_count = [[1]], False, 1
        columns: list = []

    async def fake_run(spec, **kw):
        if exec_error is not None:
            raise exec_error
        res = _Res()
        res.columns = list(columns)
        return res

    monkeypatch.setattr(pin_service, "capture_spec", fake_capture)
    monkeypatch.setattr(pin_service, "run_pinned_spec", fake_run)
    monkeypatch.setattr(pin_service, "project_path_for", lambda db: None)  # 直连库


async def test_pin_task_happy_path(monkeypatch, tmp_path, now):
    s, task = _seed(tmp_path)
    _wire(monkeypatch)
    out = await pin_service.pin_task(task, now=now, store=s)
    assert out["pin_status"] == "active"
    row = s.get_task("t1")
    assert row["pin_status"] == "active" and row["pin_source_thread_id"] == "th"
    assert row["pin_semantic_version"] == ""


async def test_pin_task_column_drift_rejected(monkeypatch, tmp_path, now):
    s, task = _seed(tmp_path)
    _wire(monkeypatch, columns=("b",))
    with pytest.raises(pin_service.PinError) as e:
        await pin_service.pin_task(task, now=now, store=s)
    assert e.value.code == "columns_changed"
    assert s.get_task("t1")["pin_status"] == ""  # 失败不落库


async def test_pin_task_never_pins_on_exec_failure(monkeypatch, tmp_path, now):
    from agent.scheduler.pin_exec import PinExecError

    s, task = _seed(tmp_path)
    _wire(monkeypatch, exec_error=PinExecError("sql_failed", "Table 't' doesn't exist"))
    with pytest.raises(pin_service.PinError) as e:
        await pin_service.pin_task(task, now=now, store=s)
    assert e.value.code == "sql_failed"
    assert s.get_task("t1")["pin_spec"] == ""


async def test_pin_task_stores_expect_columns_from_verification(monkeypatch, tmp_path, now):
    """expect_columns 必须来自**验证执行**的真实列，不是来源 run 的文本。"""
    s, task = _seed(tmp_path)
    _wire(monkeypatch, columns=("a",))
    await pin_service.pin_task(task, now=now, store=s)
    spec = __import__("json").loads(s.get_task("t1")["pin_spec"])
    assert spec["expect_columns"] == ["a"]


async def test_capture_spec_without_source_run(monkeypatch, tmp_path, now):
    """既没给 thread_id、流水里也没有 success 记录 → no_source_run。"""
    s, task = _seed(tmp_path)
    with pytest.raises(pin_service.PinError) as e:
        await pin_service.capture_spec(task, thread_id=None, now=now, store=s)
    assert e.value.code == "no_source_run"


async def test_capture_spec_without_check_result(monkeypatch, tmp_path, now):
    """会话里没有 check_async_task 的成功结果 → no_check_result。"""
    s, task = _seed(tmp_path)
    s.record_run("t1", "th", "r1", "2026-10-23T00:00:00Z")
    s.complete_run("t1", "th", "success", "", "2026-10-23T00:01:00Z")

    class _Threads:
        async def get_state(self, thread_id):
            return {"values": {"messages": []}}

    class _Client:
        threads = _Threads()

    with pytest.raises(pin_service.PinError) as e:
        await pin_service.capture_spec(
            task, thread_id=None, now=now, store=s, client=_Client()
        )
    assert e.value.code == "no_check_result"


async def test_capture_spec_picks_latest_success_run(monkeypatch, tmp_path, now):
    s, task = _seed(tmp_path)
    s.record_run("t1", "old", "r0", "2026-10-22T00:00:00Z")
    s.complete_run("t1", "old", "success", "", "2026-10-22T00:01:00Z")
    s.record_run("t1", "new", "r1", "2026-10-23T00:00:00Z")
    s.complete_run("t1", "new", "success", "", "2026-10-23T00:01:00Z")

    seen = {}

    class _Threads:
        async def get_state(self, thread_id):
            seen["thread_id"] = thread_id
            return {
                "values": {
                    "messages": [
                        type(
                            "M",
                            (),
                            {
                                "name": "tool",
                                "content": (
                                    '{"status":"success","thread_id":"sub","result":"| a |",'
                                    '"sql":"SELECT 1 WHERE dt >= \'2026-10-01\'"}'
                                ),
                            },
                        )()
                    ]
                }
            }

    class _Client:
        threads = _Threads()

    spec, _warns = await pin_service.capture_spec(
        task, thread_id=None, now=now, store=s, client=_Client()
    )
    assert seen["thread_id"] == "new"  # 最新那条 success，不是最旧
    assert spec["channel"] == "sql" and spec["source_thread_id"] == "new"
    assert "{{p:month_start}}" in spec["sql_template"]
