"""定时任务固化查询：触发分叉与回放注入（T15–T18、T20、T21）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler import worker
from agent.scheduler.store import ScheduledTaskStore
from agent.scheduler.worker import _build_replay_input

_SPEC = (
    '{"v":1,"channel":"sql","db_name":"d","tz":"Asia/Shanghai",'
    '"sql_template":"SELECT 1","params":[],"expect_columns":["a"],'
    '"row_limit":5000,"semantic_version":"abc","warnings":[]}'
)


@pytest.fixture
def now():
    return datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _async(v):
    async def f(*a, **k):
        return v

    return f


@pytest.fixture
def store(tmp_path):
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
    return s


@pytest.fixture
def seeded(store):
    return store.get_task("t1")


@pytest.fixture
def client():
    return object()  # 版本闸分支用不到；回放分支的 client 不参与 run_pinned_spec


@pytest.fixture
def active_row(store):
    """行必须**真的**是 active（存进库），否则「成功不失效」这类断言测的是假象。"""
    store.set_pin(
        "t1",
        spec_json=_SPEC,
        semantic_version="abc",
        source_thread_id="th",
        now_iso="2026-10-23T00:00:00Z",
    )
    return store.get_task("t1")


def test_replay_input_pairs_tool_calls():
    payload = {
        "status": "success",
        "thread_id": "pin-replay",
        "result": "x",
        "sql": "SELECT 1",
        "pinned_replay": True,
    }
    inp = _build_replay_input("做报表", payload)
    msgs = inp["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["id"] == msgs[2]["tool_call_id"]
    assert msgs[1]["tool_calls"][0]["name"] == "check_async_task"


def test_replay_input_is_findable_by_report_builder():
    from langchain_core.messages import AIMessage, ToolMessage

    from agent.tools.report_builder import _find_last_check_result

    payload = {
        "status": "success",
        "thread_id": "pin-replay",
        "result": "表格",
        "sql": "SELECT 1",
        "pinned_replay": True,
    }
    inp = _build_replay_input("x", payload)
    a, t = inp["messages"][1], inp["messages"][2]
    msgs = [
        AIMessage(content=a["content"], tool_calls=a["tool_calls"]),
        ToolMessage(
            content=t["content"], tool_call_id=t["tool_call_id"], name="check_async_task"
        ),
    ]
    hit = _find_last_check_result(msgs, "")
    assert hit is not None and hit[0]["sql"] == "SELECT 1"


async def test_version_mismatch_invalidates_and_falls_back(monkeypatch, seeded, store, client, now):
    row = {
        **seeded,
        "pin_status": "active",
        "pin_spec": '{"v":1,"channel":"sql","semantic_version":"old","db_name":"d"}',
        "pin_semantic_version": "old",
    }
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("new"))
    ok, mode, reason, replay_input = await worker._try_pinned_fire(row, store, now, client)
    assert ok is False and mode == "fallback" and replay_input is None
    assert reason.startswith("semantic_version_changed")
    assert store.get_task(row["id"])["pin_status"] == "invalid"


async def test_replay_success_sets_mode(monkeypatch, seeded, store, client, now, active_row):
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    class _Res:
        sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
        columns, rows, row_count, truncated = ["a"], [[1]], 1, False
        params = {"month_start": "2026-10-01"}

    monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))
    ok, mode, reason, replay_input = await worker._try_pinned_fire(
        active_row, store, now, client
    )
    assert ok is True and mode == "replay" and reason == ""
    assert replay_input["messages"][2]["role"] == "tool"
    assert store.get_task("t1")["pin_status"] == "active"  # 成功不失效


async def test_truncated_result_still_replays(monkeypatch, store, client, now, active_row):
    """T20：结果被行上限截断 ≠ 失效——照常回放，截断事实由 check 载荷带出去。"""
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    class _Res:
        sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
        columns, rows, row_count, truncated = ["a"], [[1]], 5000, True
        params = {}

    monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))
    ok, mode, _, replay_input = await worker._try_pinned_fire(
        active_row, store, now, client
    )
    assert ok is True and mode == "replay"
    assert store.get_task("t1")["pin_status"] == "active"
    assert "截断" in replay_input["messages"][2]["content"]


async def test_column_drift_invalidates(monkeypatch, store, client, now, active_row):
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    class _Res:
        sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
        columns, rows, row_count, truncated = ["b"], [[1]], 1, False
        params = {}

    monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))
    ok, mode, reason, replay_input = await worker._try_pinned_fire(
        active_row, store, now, client
    )
    assert ok is False and mode == "fallback" and replay_input is None
    assert reason.startswith("columns_changed")
    assert store.get_task("t1")["pin_status"] == "invalid"


async def test_exec_error_invalidates_without_raising(monkeypatch, store, client, now, active_row):
    from agent.scheduler.pin_exec import PinExecError

    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    async def boom(spec, **kw):
        raise PinExecError("sql_failed", "Table 't' doesn't exist")

    monkeypatch.setattr(worker, "run_pinned_spec", boom)
    ok, mode, reason, replay_input = await worker._try_pinned_fire(
        active_row, store, now, client
    )
    assert ok is False and mode == "fallback"
    assert reason.startswith("sql_failed")
    assert store.get_task("t1")["pin_invalid_reason"].startswith("sql_failed")


async def test_corrupt_spec_falls_back(monkeypatch, store, client, now, seeded):
    row = {**seeded, "pin_status": "active", "pin_semantic_version": "abc", "pin_spec": "{oops"}
    ok, mode, reason, replay_input = await worker._try_pinned_fire(row, store, now, client)
    assert ok is False and mode == "fallback"
    assert reason.startswith("render_failed")
    assert store.get_task("t1")["pin_status"] == "invalid"


async def test_payload_carries_new_thread_id(monkeypatch, store, client, now, active_row):
    """回放载荷的 thread_id 是**这次**新开的会话（报告按它归档/过滤）。"""
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    class _Res:
        sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
        columns, rows, row_count, truncated = ["a"], [[1]], 1, False
        params = {}

    monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))
    _ok, _mode, _reason, replay_input = await worker._try_pinned_fire(
        active_row, store, now, client, thread_id="th-new"
    )
    import json as _json

    payload = _json.loads(replay_input["messages"][2]["content"])
    assert payload["thread_id"] == "th-new"


async def test_db_changed_falls_back(monkeypatch, store, client, now, active_row):
    """任务行换了库 ⇒ 不许拿 spec 里的旧库回放。

    `_fire` 的权限复验验的是**任务行**的库，而回放执行的是 **spec** 的库：两者
    不一致时，回放既可能拿旧库数据冒充新库报表，也等于绕开授权。写路径
    （service.update_task）已经解绑，这道读侧闸保护的是存量 active 行。
    """
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))
    row = {**active_row, "db_name": "other"}
    ok, mode, reason, replay_input = await worker._try_pinned_fire(row, store, now, client)
    assert ok is False and mode == "fallback" and replay_input is None
    assert reason.startswith("db_changed")
    assert store.get_task("t1")["pin_status"] == "invalid"


async def test_pinned_fire_never_raises_on_payload_build(monkeypatch, store, client, now, active_row):
    """契约：`_try_pinned_fire` 绝不抛——载荷组装炸了也要走回退，不能变成 fire 失败。"""
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    class _Res:
        sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
        columns, rows, row_count, truncated = ["a"], [[1]], 1, False
        params = {}

    monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))

    def boom(*a, **k):
        raise TypeError("payload build exploded")

    monkeypatch.setattr(worker, "build_check_payload", boom)
    ok, mode, reason, replay_input = await worker._try_pinned_fire(
        active_row, store, now, client
    )
    assert ok is False and mode == "fallback" and replay_input is None
    assert reason.startswith("render_failed")
