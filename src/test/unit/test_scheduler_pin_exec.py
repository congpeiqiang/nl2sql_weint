"""定时任务固化查询：只读执行器（引擎一律打桩，不连真库）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler import pin_exec


@pytest.fixture
def now():
    return datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _sql_spec(**kw):
    spec = {
        "v": 1,
        "channel": "sql",
        "db_name": "d",
        "tz": "Asia/Shanghai",
        "sql_template": "SELECT 1 WHERE dt >= '{{p:yesterday}}'",
        "params": ["yesterday"],
        "expect_columns": ["a"],
        "row_limit": 5000,
    }
    spec.update(kw)
    return spec


async def test_sql_channel_executes_rendered_sql(monkeypatch, now):
    called = {}

    async def fake_run(db_name, sql, limit=1000):
        called.update(db_name=db_name, sql=sql, limit=limit)
        return {"columns": ["a"], "rows": [[1]], "truncated": False}

    monkeypatch.setattr(pin_exec, "run_readonly_sql", fake_run)
    res = await pin_exec.run_pinned_spec(_sql_spec(), now=now)
    assert res.channel == "sql"
    assert "2026-10-22" in called["sql"]
    assert res.sql == called["sql"]
    assert res.columns == ["a"] and res.row_count == 1


async def test_sql_channel_uses_env_row_limit(monkeypatch, now):
    seen = {}

    async def fake_run(db_name, sql, limit=1000):
        seen["limit"] = limit
        return {"columns": [], "rows": [], "truncated": False}

    monkeypatch.setenv("NL2SQL_SCHED_PIN_ROW_LIMIT", "100")
    monkeypatch.setattr(pin_exec, "run_readonly_sql", fake_run)
    await pin_exec.run_pinned_spec(_sql_spec(row_limit=5000), now=now)
    assert seen["limit"] == 100  # 取 min(spec, env)


async def test_cube_channel_compiles_then_executes(monkeypatch, now):
    monkeypatch.setattr(pin_exec, "resolve_wren_ctx_by_db", lambda db: ("/p", object()))
    monkeypatch.setattr(
        pin_exec,
        "plan_cube_sql_checked",
        lambda p, c, a: {"dialect_sql": "SELECT '2026-10-01'", "cube_sql": "x"},
    )

    async def fake_run(db_name, sql, limit=1000):
        return {"columns": ["m"], "rows": [[7]], "truncated": True}

    monkeypatch.setattr(pin_exec, "run_readonly_sql", fake_run)
    spec = {
        "v": 1,
        "channel": "cube",
        "db_name": "d",
        "tz": "Asia/Shanghai",
        "cube_template": {
            "cube": "c",
            "measures": ["c.m"],
            "filters": ["c.dt:gte:{{p:month_start}}"],
        },
        "params": ["month_start"],
        "expect_columns": ["m"],
        "row_limit": 5000,
    }
    res = await pin_exec.run_pinned_spec(spec, now=now)
    assert res.sql == "SELECT '2026-10-01'"
    assert res.truncated is True
    assert res.cube_args["filters"] == ["c.dt:gte:2026-10-01"]
    assert res.cube_tool == "query_cube"


async def test_not_semantic_db(monkeypatch, now):
    monkeypatch.setattr(pin_exec, "resolve_wren_ctx_by_db", lambda db: (None, None))
    spec = {
        "v": 1,
        "channel": "cube",
        "db_name": "d",
        "tz": "Asia/Shanghai",
        "cube_template": {"cube": "c", "measures": ["c.m"]},
        "params": [],
        "expect_columns": [],
        "row_limit": 5000,
    }
    with pytest.raises(pin_exec.PinExecError) as e:
        await pin_exec.run_pinned_spec(spec, now=now)
    assert e.value.code == "not_semantic_db"


async def test_cube_compile_failure(monkeypatch, now):
    from agent.utils.wren_plan import CubePlanError

    monkeypatch.setattr(pin_exec, "resolve_wren_ctx_by_db", lambda db: ("/p", object()))

    def boom(p, c, a):
        raise CubePlanError("Unknown measure c.m")

    monkeypatch.setattr(pin_exec, "plan_cube_sql_checked", boom)
    spec = {
        "v": 1,
        "channel": "cube",
        "db_name": "d",
        "tz": "Asia/Shanghai",
        "cube_template": {"cube": "c", "measures": ["c.m"]},
        "params": [],
        "expect_columns": [],
        "row_limit": 5000,
    }
    with pytest.raises(pin_exec.PinExecError) as e:
        await pin_exec.run_pinned_spec(spec, now=now)
    assert e.value.code == "cube_compile_failed"
    assert "Unknown measure" in e.value.detail


async def test_write_sql_rejected(monkeypatch, now):
    async def fake_run(db_name, sql, limit=1000):
        raise ValueError("仅允许只读查询（检测到 DELETE）")

    monkeypatch.setattr(pin_exec, "run_readonly_sql", fake_run)
    with pytest.raises(pin_exec.PinExecError) as e:
        await pin_exec.run_pinned_spec(_sql_spec(sql_template="DELETE FROM t"), now=now)
    assert e.value.code == "sql_failed"


async def test_timeout_becomes_exec_timeout(monkeypatch, now):
    import asyncio

    async def slow_run(db_name, sql, limit=1000):
        await asyncio.sleep(1)
        return {"columns": [], "rows": [], "truncated": False}

    monkeypatch.setattr(pin_exec, "run_readonly_sql", slow_run)
    with pytest.raises(pin_exec.PinExecError) as e:
        await pin_exec.run_pinned_spec(_sql_spec(), now=now, timeout_s=0.01)
    assert e.value.code == "exec_timeout"


async def test_bad_spec_version(monkeypatch, now):
    with pytest.raises(pin_exec.PinExecError) as e:
        await pin_exec.run_pinned_spec(_sql_spec(v=2), now=now)
    assert e.value.code == "spec_version_unknown"


async def test_readonly_sql_clamps_oversized_limit(monkeypatch):
    """固化回放无人值守：SQL 自带的超大 LIMIT 必须被收敛到 row_limit。

    这是「一次定时把全表拉进内存」的唯一防线——`row_limit` 只在语句没有
    LIMIT 时才会追加，自带 `LIMIT 999999` 的语句必须靠 clamp 压下来。
    """
    import pandas as pd

    import mcp_server.db_mcp_server.db.config as config_mod
    import mcp_server.db_mcp_server.db.db_server as db_server

    class _Cfg:
        db_type = "sqlite"
        config = {}

    monkeypatch.setattr(
        config_mod.McpSqlConfig, "from_env", classmethod(lambda cls, db="": _Cfg())
    )

    seen = {}

    class _Runner:
        def __init__(self, **kw):
            pass

        async def run_sql(self, args, ctx):
            seen["sql"] = args.sql
            return pd.DataFrame({"a": [1]})

    monkeypatch.setattr(db_server, "_load_runner_class", lambda t: _Runner)

    out = await pin_exec.run_readonly_sql("d", "SELECT a FROM t LIMIT 999999", 5000)
    assert "LIMIT 5000" in seen["sql"]
    assert out["executed_sql"] == seen["sql"]
