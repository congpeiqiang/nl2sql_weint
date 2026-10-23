"""定时任务固化查询：日期锚点与模板化（纯函数，无 I/O）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler.query_spec import SpecError, render_text, resolve_anchors, templatize

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
