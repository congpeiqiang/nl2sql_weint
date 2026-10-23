"""定时任务固化查询：日期锚点与模板化（纯函数，无 I/O）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler.query_spec import (
    SpecError,
    build_check_payload,
    extract_spec,
    render_spec,
    render_text,
    resolve_anchors,
    templatize,
    validate_columns,
)

TZ = "Asia/Shanghai"


def _anchors(y=2026, m=10, d=23):
    return resolve_anchors(datetime(y, m, d, 11, 0, tzinfo=ZoneInfo(TZ)), TZ)


def test_anchors_basic():
    a = _anchors()
    assert a["today"] == "2026-10-23"
    assert a["yesterday"] == "2026-10-22"
    assert a["tomorrow"] == "2026-10-24"
    assert a["month_start"] == "2026-10-01"
    assert a["last_month_start"] == "2026-09-01"
    assert a["last_month_end"] == "2026-09-30"
    assert a["year_start"] == "2026-01-01"
    assert a["week_start"] == "2026-10-19"  # 2026-10-23 是周五


def test_anchors_cross_year_and_leap():
    assert _anchors(2026, 1, 5)["last_month_start"] == "2025-12-01"
    assert _anchors(2026, 1, 5)["last_month_end"] == "2025-12-31"
    assert _anchors(2028, 3, 3)["last_month_end"] == "2028-02-29"


def test_anchors_use_task_timezone_not_utc():
    """UTC 03:00 = 北京 11:00；若按 UTC 算，today 会错一天（Review Focus #5）。"""
    utc = datetime(2026, 10, 23, 3, 0, tzinfo=ZoneInfo("UTC"))
    assert resolve_anchors(utc, TZ)["today"] == "2026-10-23"
    # 同一时刻按 UTC 时区算应仍是 23 日，但北京时间 08:00 前的 UTC 时刻会分叉：
    early = datetime(2026, 10, 22, 20, 0, tzinfo=ZoneInfo("UTC"))  # 北京 23 日 04:00
    assert resolve_anchors(early, TZ)["today"] == "2026-10-23"


def test_templatize_single_match():
    out, used, warns = templatize(
        "SELECT 1 WHERE dt >= '2026-10-01' AND dt <= '2026-10-22'", _anchors()
    )
    assert out == "SELECT 1 WHERE dt >= '{{p:month_start}}' AND dt <= '{{p:yesterday}}'"
    assert used == ["month_start", "yesterday"]
    assert warns == []


def test_templatize_replaces_repeated_literal():
    out, used, _ = templatize("a = '2026-10-22' OR b = '2026-10-22'", _anchors())
    assert out == "a = '{{p:yesterday}}' OR b = '{{p:yesterday}}'"
    assert used == ["yesterday"]


def test_templatize_keeps_time_part():
    out, used, _ = templatize("dt > '2026-10-23 00:00:00'", _anchors())
    assert out == "dt > '{{p:today}} 00:00:00'"
    assert used == ["today"]


def test_templatize_ambiguous_on_first_of_month():
    # 2026-11-01 同时是 today 与 month_start
    a = resolve_anchors(datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo(TZ)), TZ)
    with pytest.raises(SpecError) as e:
        templatize("dt = '2026-11-01'", a)
    assert e.value.code == "ambiguous_date"
    assert "month_start" in e.value.detail and "today" in e.value.detail


def test_templatize_binding_resolves_ambiguity():
    a = resolve_anchors(datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo(TZ)), TZ)
    out, used, _ = templatize(
        "dt >= '2026-11-01'", a, bindings={"2026-11-01": "month_start"}
    )
    assert out == "dt >= '{{p:month_start}}'"
    assert used == ["month_start"]


def test_binding_must_match_that_day():
    a = _anchors()
    with pytest.raises(SpecError) as e:
        templatize("dt = '2026-10-09'", a, bindings={"2026-10-09": "month_start"})
    assert e.value.code == "bad_binding"


def test_binding_with_unknown_anchor_is_rejected():
    with pytest.raises(SpecError) as e:
        templatize("dt = '2026-10-01'", _anchors(), bindings={"2026-10-01": "last_week"})
    assert e.value.code == "bad_binding"


def test_zero_match_needs_accept_constants():
    with pytest.raises(SpecError) as e:
        templatize("dt >= '1990-01-01'", _anchors())
    assert e.value.code == "unmatched_date"


def test_zero_match_kept_as_constant_when_accepted():
    out, used, warns = templatize("dt >= '1990-01-01'", _anchors(), accept_constants=True)
    assert out == "dt >= '1990-01-01'"
    assert used == []
    assert warns == ["常量日期保留: 1990-01-01"]


def test_render_roundtrip():
    a = _anchors()
    out, _, _ = templatize("dt = '2026-10-01'", a)
    assert render_text(out, a) == "dt = '2026-10-01'"


def test_render_moves_with_time():
    tmpl, _, _ = templatize("dt = '2026-10-01'", _anchors())
    later = resolve_anchors(datetime(2026, 11, 15, 9, 0, tzinfo=ZoneInfo(TZ)), TZ)
    assert render_text(tmpl, later) == "dt = '2026-11-01'"


# ── spec 提取 / 渲染 / 列漂移 ──────────────────────────────────────────────


@pytest.fixture
def now():
    """固化参考时刻：2026-10-23 11:00 北京（UTC 03:00，见 Review Focus #5）。"""
    return datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo(TZ))


def _check_sql():
    return {
        "status": "success",
        "thread_id": "t-sub",
        "result": "| a | b |\n|---|---|\n| 1 | 2 |",
        "sql": "SELECT dept, SUM(hours) FROM t WHERE dt >= '2026-10-01'",
        "dialect_sql": "SELECT `dept`, SUM(`hours`) FROM `t` WHERE `dt` >= '2026-10-01'",
    }


def test_extract_sql_prefers_dialect_sql(now):
    spec, warns = extract_spec(
        _check_sql(),
        db_name="witops",
        tz="Asia/Shanghai",
        semantic_version="abc123",
        source_thread_id="t-main",
        now=now,
    )
    assert spec["channel"] == "sql"
    assert spec["sql_template"].startswith("SELECT `dept`")
    assert "{{p:month_start}}" in spec["sql_template"]
    assert spec["params"] == ["month_start"]
    assert spec["v"] == 1 and spec["semantic_version"] == "abc123"
    assert warns == []


def test_extract_cube(now):
    check = {
        "status": "success",
        "thread_id": "t-sub",
        "result": "x",
        "sql_kind": "cube",
        "cube_query": "x > '2026-10-01'",
        "cube_args": {
            "cube": "workhour",
            "measures": ["workhour.total_hours"],
            "dimensions": ["dept.name"],
            "filters": ["workhour.dt:gte:2026-10-01"],
            "time_dimension": "workhour.dt:day:2026-10-01,2026-10-24",
        },
        "cube_tool": "query_cube",
    }
    spec, _ = extract_spec(
        check,
        db_name="witops",
        tz="Asia/Shanghai",
        semantic_version="",
        source_thread_id="t",
        now=now,
    )
    assert spec["channel"] == "cube"
    assert spec["cube_template"]["filters"] == ["workhour.dt:gte:{{p:month_start}}"]
    assert (
        spec["cube_template"]["time_dimension"]
        == "workhour.dt:day:{{p:month_start}},{{p:tomorrow}}"
    )
    assert set(spec["cube_template"]) <= {
        "cube",
        "measures",
        "dimensions",
        "filters",
        "time_dimension",
    }


def test_extract_refuses_without_sql_or_cube(now):
    with pytest.raises(SpecError) as e:
        extract_spec(
            {"status": "success", "thread_id": "t", "result": "x"},
            db_name="d",
            tz="Asia/Shanghai",
            semantic_version="",
            source_thread_id="t",
            now=now,
        )
    assert e.value.code == "no_query"


def test_extract_strips_cube_window_keys(now):
    check = {
        "status": "success",
        "thread_id": "t",
        "result": "x",
        "sql_kind": "cube",
        "cube_args": {
            "cube": "c",
            "measures": ["c.m"],
            "limit": 100,
            "offset": 0,
            "granularity": "day",
        },
    }
    with pytest.raises(SpecError) as e:
        extract_spec(
            check,
            db_name="d",
            tz="Asia/Shanghai",
            semantic_version="",
            source_thread_id="t",
            now=now,
        )
    assert e.value.code == "cube_key_unsupported"


def test_render_moves_with_time(now):
    spec, _ = extract_spec(
        _check_sql(),
        db_name="d",
        tz="Asia/Shanghai",
        semantic_version="",
        source_thread_id="t",
        now=now,
    )
    later = now.replace(month=11, day=15)
    out = render_spec(spec, later)
    assert "'2026-11-01'" in out["sql"]
    assert out["params"] == {"month_start": "2026-11-01"}


def test_render_rejects_unknown_spec_version(now):
    with pytest.raises(SpecError) as e:
        render_spec({"v": 99, "channel": "sql", "sql_template": "SELECT 1"}, now)
    assert e.value.code == "spec_version_unknown"


def test_render_rejects_placeholder_not_in_params(now):
    spec = {"v": 1, "channel": "sql", "sql_template": "SELECT '{{p:today}}'", "params": []}
    with pytest.raises(SpecError) as e:
        render_spec(spec, now)
    assert e.value.code == "render_failed"


def test_validate_columns_case_and_order_insensitive():
    spec = {"expect_columns": ["dept", "Sum_Hours"]}
    assert validate_columns(spec, ["SUM_HOURS", "DEPT"])[0] is True


def test_validate_columns_detects_drift():
    spec = {"expect_columns": ["dept", "hours"]}
    ok, detail = validate_columns(spec, ["dept", "cost"])
    assert ok is False
    assert "missing=[hours]" in detail and "extra=[cost]" in detail


def test_check_payload_has_signature_keys():
    spec = {"channel": "sql", "sql_template": "SELECT 1", "expect_columns": ["a"]}
    p = build_check_payload(
        spec,
        thread_id="t-new",
        columns=["a"],
        rows=[(1,)],
        sql="SELECT 1",
        cube_query="",
        cube_args={},
        cube_tool="",
        row_count=1,
        truncated=False,
    )
    assert {"status", "thread_id", "result", "sql"} <= p.keys()
    assert p["status"] == "success" and p["thread_id"] == "t-new"
    assert "固化回放" in p["result"] and "| a |" in p["result"]
    assert p["pinned_replay"] is True
