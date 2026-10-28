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
        params = {"month_start": "2026-10-01"}
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


async def test_pin_task_does_not_pretend_to_check_columns(monkeypatch, tmp_path, now):
    """固化期**不**做列比对（那是个永远为真的死分支）。

    来源会话里没有驱动返回的列名，`extract_spec` 的 `expect_columns` 恒为空；
    基线由验证执行本身定下，漂移在触发时比对（见 test_scheduler_pin_replay 的
    `columns_changed` 用例）。
    """
    s, task = _seed(tmp_path)
    _wire(monkeypatch, columns=("a", "b"))
    await pin_service.pin_task(task, now=now, store=s)
    import json

    spec = json.loads(s.get_task("t1")["pin_spec"])
    assert spec["expect_columns"] == ["a", "b"]


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


async def test_pin_task_returns_preview_sql(monkeypatch, tmp_path, now):
    """前端要展示「以后每次都跑这条」，回填的是**实际下发**的那条（含 limit）。"""
    s, task = _seed(tmp_path)
    _wire(monkeypatch)
    out = await pin_service.pin_task(task, now=now, store=s)
    assert out["preview_sql"] == "SELECT 1"
    assert out["params"] == {"month_start": "2026-10-01"}


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


# ── 物理 SQL 解析（check 结果里内联的往往是**语义层** SQL，物理 SQL 常常只落盘）──
def _client_with_check(check: dict):
    import json as _json

    class _Threads:
        async def get_state(self, thread_id):
            return {
                "values": {
                    "messages": [
                        type("M", (), {"name": "tool", "content": _json.dumps(check)})()
                    ]
                }
            }

    class _Client:
        threads = _Threads()

    return _Client()


def _plan_file(root, rel: str, text: str) -> str:
    """在工作区里落一份物理 SQL 文件，返回 check 里的 VFS 指针。"""
    disk = root / rel
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_text(text, encoding="utf-8")
    return "/workspace/" + rel


def _wren_check(fp: str, *, inline: str = "", chars: int = 5692) -> dict:
    """语义层库的 check 结果：``sql`` 是 wren 方言（引用 MDL 视图），**不能**回放。"""
    check = {
        "status": "success",
        "thread_id": "sub",
        "result": "| a |\n|---|\n| 1 |",
        "sql": "SELECT a FROM v_workhour WHERE dt >= '2026-10-01'",
        "dialect": "mysql",
        "dialect_sql_chars": chars,
        "dialect_sql_file": fp,
    }
    if inline:
        check["dialect_sql"] = inline
    return check


async def test_capture_spec_reads_physical_sql_from_file(monkeypatch, tmp_path, now):
    """物理 SQL 超过 3000 字符时只落盘（内联被省掉）⇒ 固化必须读盘。

    留在 ``check["sql"]`` 里的是**语义层 SQL**（``FROM v_workhour`` 是 MDL 虚拟
    视图），拿它回放必炸。旧实现读不到内联就悄悄退回它 —— 线上 2026-10-27 那次
    固化就是这么带上一条跑不了的语句的。
    """
    s, task = _seed(tmp_path)
    root = tmp_path / "ws"
    monkeypatch.setattr(pin_service, "_workspace_root", lambda: str(root))
    fp = _plan_file(
        root,
        "nl2sql_process_data/sub/wren_plan/x_ab12.sql",
        "WITH t AS (SELECT dt, hours FROM wh)\n"
        "SELECT SUM(hours) FROM t WHERE dt >= '2026-10-01'\n"
        "LIMIT 1001\n",
    )
    spec, _w = await pin_service.capture_spec(
        task, thread_id="th", now=now, store=s, client=_client_with_check(_wren_check(fp))
    )
    assert "v_workhour" not in spec["sql_template"]
    assert spec["sql_template"].startswith("WITH t AS")
    assert "{{p:month_start}}" in spec["sql_template"]


async def test_capture_spec_drops_connector_limit(monkeypatch, tmp_path, now):
    """连接器**无条件**在末尾追加 ``LIMIT n``（n=探测上限+1）⇒ 固化要剥掉它。

    不剥的话每次回放都被静默截在 1001 行，而且这个数字与业务口径毫无关系。
    """
    s, task = _seed(tmp_path)
    root = tmp_path / "ws"
    monkeypatch.setattr(pin_service, "_workspace_root", lambda: str(root))
    fp = _plan_file(
        root,
        "nl2sql_process_data/sub/wren_plan/x_ab12.sql",
        "SELECT dt, hours FROM wh WHERE dt >= '2026-10-01'\nLIMIT 1001\n",
    )
    spec, _w = await pin_service.capture_spec(
        task, thread_id="th", now=now, store=s, client=_client_with_check(_wren_check(fp))
    )
    assert "LIMIT" not in spec["sql_template"].upper()
    assert spec["row_limit"] == 1001  # 折算进行上限，回放再补回同一条 LIMIT


async def test_capture_spec_keeps_model_own_limit(monkeypatch, tmp_path, now):
    """``dup_limit``：模型自己写的 ``LIMIT 10`` 在**前一**行，剥掉的只能是末尾那条。"""
    s, task = _seed(tmp_path)
    root = tmp_path / "ws"
    monkeypatch.setattr(pin_service, "_workspace_root", lambda: str(root))
    fp = _plan_file(
        root,
        "nl2sql_process_data/sub/wren_plan/x_ab12.sql",
        "SELECT dt, hours FROM wh WHERE dt >= '2026-10-01' ORDER BY hours DESC LIMIT 10\n"
        "LIMIT 1001\n",
    )
    spec, _w = await pin_service.capture_spec(
        task, thread_id="th", now=now, store=s, client=_client_with_check(_wren_check(fp))
    )
    assert spec["sql_template"].rstrip().upper().endswith("LIMIT 10")


async def test_capture_spec_uses_source_run_time_not_pin_time(monkeypatch, tmp_path, now):
    """来源 run 是 10-27 跑的、10-28 才固化 → 锚点按 10-27 解析，并告警来源陈旧。

    ``2026-10-26`` 在 10-27 是 ``yesterday``、在 10-28（周三）却是 ``week_start``；
    拿固化当天解析会静默把窗口起点固成上个周一（2026-10-28 生产实证）。
    """
    s, task = _seed(tmp_path)
    s.record_run("t1", "th", "r1", "2026-10-27T08:16:26+00:00")
    s.complete_run("t1", "th", "success", "", "2026-10-27T08:20:00+00:00")
    check = {
        "status": "success",
        "thread_id": "sub",
        "result": "| a |",
        "sql_kind": "cube",
        "cube_query": "x",
        "cube_args": {
            "cube": "bug_quality",
            "measures": ["bug_count"],
            "dimensions": ["project_name"],
            "time_dimension": "complete_time:day:2026-10-26,2026-10-27",
        },
        "cube_tool": "query_cube",
    }
    pin_day = datetime(2026, 10, 28, 10, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    # 会话里没有 human 消息 ⇒ 用任务原话消歧（10-27 的 10-26 同时是
    # yesterday/week_start，原话里的「前一天」把候选收窄到 yesterday）
    task = dict(task, prompt="统计前一天的缺陷数量")
    spec, warns = await pin_service.capture_spec(
        task, thread_id=None, now=pin_day, store=s, client=_client_with_check(check)
    )
    td = spec["cube_template"]["time_dimension"]
    assert "{{p:week_start}}" not in td
    assert spec["params"] == ["yesterday", "today"]
    assert any("2026-10-27" in w for w in warns)  # 来源不是今天 → 必须说出来


async def test_capture_spec_same_day_source_has_no_stale_warning(tmp_path, now):
    """来源 run 是当天跑的 → 不告警（正常路径，只有解析基准日变了才提示）。"""
    s, task = _seed(tmp_path)
    s.record_run("t1", "th", "r1", "2026-10-23T00:00:00+00:00")
    s.complete_run("t1", "th", "success", "", "2026-10-23T00:05:00+00:00")
    check = {
        "status": "success",
        "thread_id": "sub",
        "result": "| a |",
        "dialect_sql": "SELECT 1 WHERE dt >= '2026-10-01'",
    }
    spec, warns = await pin_service.capture_spec(
        task, thread_id=None, now=now, store=s, client=_client_with_check(check)
    )
    assert spec["params"] == ["month_start"]
    assert not any("来源" in w for w in warns)


async def test_capture_spec_falls_back_to_thread_run_time(tmp_path, now):
    """来源不是本任务的流水（如从交互会话固化）→ 用该 thread 最近一次 run 的时刻。

    没有这个回落就只剩下固化时刻可用，隔天固化会复现同一个锚点错配。
    """
    import json as _json

    s, task = _seed(tmp_path)
    check = {
        "status": "success",
        "thread_id": "sub",
        "result": "| a |",
        "sql_kind": "cube",
        "cube_query": "x",
        "cube_args": {
            "cube": "bug_quality",
            "measures": ["bug_count"],
            "dimensions": ["project_name"],
            "time_dimension": "complete_time:day:2026-10-26,2026-10-27",
        },
        "cube_tool": "query_cube",
    }

    class _Threads:
        async def get_state(self, thread_id):
            return {"values": {"messages": [
                type("M", (), {"name": "tool", "content": _json.dumps(check)})()
            ]}}

    class _Runs:
        async def list(self, thread_id=None, limit=None):
            return [
                {"created_at": "2026-10-27T08:18:34.541237+00:00", "status": "success"},
                {"created_at": "2026-10-27T08:16:26.761828+00:00", "status": "success"},
            ]

    class _Client:
        threads = _Threads()
        runs = _Runs()

    pin_day = datetime(2026, 10, 28, 10, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    task = dict(task, prompt="统计前一天的缺陷数量")
    spec, warns = await pin_service.capture_spec(
        task, thread_id="th", now=pin_day, store=s, client=_Client()
    )
    assert "{{p:week_start}}" not in spec["cube_template"]["time_dimension"]
    assert spec["params"] == ["yesterday", "today"]
    assert any("2026-10-27" in w for w in warns)


async def test_capture_spec_forwards_ambiguous_candidates(tmp_path, now):
    """歧义候选要穿过编排层到 API（``PinError.extra``），否则前端没得选。"""
    s, task = _seed(tmp_path)
    check = {
        "status": "success",
        "thread_id": "sub",
        "result": "| a |\n|---|\n| 1 |",
        "dialect_sql": "SELECT 1 WHERE dt = '2026-10-26'",
    }
    tue_now = datetime(2026, 10, 27, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(pin_service.PinError) as e:
        await pin_service.capture_spec(
            task, thread_id="th", now=tue_now, store=s,
            client=_client_with_check(check),
        )
    assert e.value.code == "ambiguous_date"
    assert e.value.extra["candidates"] == ["yesterday", "week_start"]


async def test_capture_spec_falls_back_to_task_prompt_for_intent(
    monkeypatch, tmp_path, now
):
    """会话里没有 human 消息时，拿任务原话当消歧证据（定时 run 的 user 消息就含它）。"""
    s, task = _seed(tmp_path)
    task = dict(task, prompt="昨日各人填报工时")
    check = {
        "status": "success",
        "thread_id": "sub",
        "result": "| a |\n|---|\n| 1 |",
        "dialect_sql": "SELECT 1 WHERE dt = '2026-10-26'",
    }
    tue_now = datetime(2026, 10, 27, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    spec, warns = await pin_service.capture_spec(
        task, thread_id="th", now=tue_now, store=s, client=_client_with_check(check)
    )
    assert "{{p:yesterday}}" in spec["sql_template"]
    assert any("yesterday" in w for w in warns)


async def test_capture_spec_fails_loudly_when_physical_sql_unreadable(
    monkeypatch, tmp_path, now
):
    """已知存在物理 SQL（``dialect_sql_chars``）却读不出来 → 明确报错，不退语义层 SQL。"""
    s, task = _seed(tmp_path)
    root = tmp_path / "ws"
    monkeypatch.setattr(pin_service, "_workspace_root", lambda: str(root))
    with pytest.raises(pin_service.PinError) as e:
        await pin_service.capture_spec(
            task,
            thread_id="th",
            now=now,
            store=s,
            client=_client_with_check(
                _wren_check("/workspace/nl2sql_process_data/sub/wren_plan/gone.sql")
            ),
        )
    assert e.value.code == "physical_sql_unreadable"
