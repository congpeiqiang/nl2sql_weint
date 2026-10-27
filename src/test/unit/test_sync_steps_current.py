"""进度卡「当前步」与耗时展示（2026-10-22 dev-34 实证）。

背景（会话 01a161f0-4947，定时任务）：

- 12:54:15 `ProgressBoundaryMiddleware` 确定性推进 todos 到「查询执行 in_progress」；
- 12:57:42 **模型自己又写了一次 write_todos，把整张表倒退回「Schema 提取与裁剪
  in_progress / 查询执行 pending」**（中间件规则「本轮模型自己写了 write_todos
  就尊重模型」放行）；`_seen_bucket` 的执行桶水位已消费 ⇒ 之后再跑 run_sql 也不会
  重新推进。模型此后 14 分钟没写 todos，卡片就一直停在「└ 查询执行 (15m23s...)」：
  状态已回退成 pending，计时却还在跑（`_compute_step_durations` 只看最后一个
  `in_progress` 事件，不看当前 status，也不看「离开该步」的 `to=pending` 事件）。

两处修：

1. 耗时只在**当前仍是 in_progress**（或展示层锁存的当前步）时带 `...` 续秒；
   一旦有 `to=pending/completed` 的离开事件就停表（冻结），不再假装还在跑。
2. `_merge_steps_monotonic` 给「当前步」也加单调锁存：模型把当前步降回 pending、
   且没有更靠后的步接棒时，保持它仍是 in_progress（header 也才不会误判 completed）。
"""

import time

from agent.subagents import sync_subagent_todos as sst

STEPS = [
    "理解建模-清晰度与知识",
    "Schema 提取与裁剪",
    "Subproblem 分解",
    "Query Plan 生成",
    "SQL 生成与验证",
    "性能优化",
    "查询执行",
    "结果汇总",
]
CURRENT = STEPS[6]
PREFIX = sst._SUBAGENT_PREFIX
TID = "01a161f0-a46a-7250-9d75-4fb62377c32a"


def _todos(*pairs):
    return [{"content": c, "status": s} for c, s in pairs]


def _status_by_content(steps):
    return {
        s["content"][len(PREFIX):]: s["status"]
        for s in steps[1:]
    }


# ── 修 2：当前步单调锁存 ──────────────────────────────────────────


def test_regressed_snapshot_keeps_latched_current_step():
    """模型把当前步降回 pending（且没有更后面的步接棒）⇒ 仍显示为当前步。

    这正是 12:57:42 的快照：Schema 回到 in_progress、查询执行回 pending。
    """
    seen_completed = set(STEPS[:6])
    seen_active = [CURRENT]
    sub = _todos(
        (STEPS[0], "completed"),
        (STEPS[1], "in_progress"),
        *[(s, "pending") for s in STEPS[2:]],
    )

    steps = sst._merge_steps_monotonic(
        None, sub, seen_completed, TID, "nl2sql", seen_active=seen_active
    )

    by_content = _status_by_content(steps)
    assert by_content[CURRENT] == "in_progress", "救回的当前步必须仍是 in_progress"
    assert by_content[STEPS[1]] == "completed", "曾完成的步不许被模型改回 in_progress"
    assert steps[0]["status"] == "in_progress", "header 有人在做 ⇒ 不能标 completed"


def test_latch_does_not_fight_a_later_active_step():
    """模型真的往前走了（结果汇总 in_progress）⇒ 锁存让位，当前步跟着前移。"""
    seen_completed = set(STEPS[:6])
    seen_active = [CURRENT]
    sub = _todos(
        *[(s, "completed") for s in STEPS[:6]],
        (CURRENT, "pending"),
        (STEPS[7], "in_progress"),
    )

    steps = sst._merge_steps_monotonic(
        None, sub, seen_completed, TID, "nl2sql", seen_active=seen_active
    )

    by_content = _status_by_content(steps)
    assert by_content[STEPS[7]] == "in_progress"
    assert by_content[CURRENT] != "in_progress", "有更后面的步接棒时不许锁存"
    assert seen_active == [STEPS[7]], "锁存基线要跟着前移"


def test_latched_step_completed_is_not_resurrected():
    """当前步已经完成、后面还没开始 ⇒ 不复活；header 允许是 completed。"""
    seen_completed = set(STEPS)
    seen_active = [CURRENT]
    sub = _todos(*[(s, "completed") for s in STEPS])

    steps = sst._merge_steps_monotonic(
        None, sub, seen_completed, TID, "nl2sql", seen_active=seen_active
    )

    by_content = _status_by_content(steps)
    assert by_content[CURRENT] == "completed"
    assert all(st == "completed" for st in by_content.values())
    assert steps[0]["status"] == "completed"


def test_latch_matches_content_carrying_duration_suffix():
    """锁存 key 要能吃掉耗时后缀（真实 todos 渲染后形如「查询执行 (14m15s)」）。"""
    seen_completed = set(STEPS[:6])
    seen_active = [CURRENT]
    sub = _todos(
        *[(s, "completed") for s in STEPS[:6]],
        (f"{CURRENT} (14m15s)", "pending"),
        (STEPS[7], "pending"),
    )

    steps = sst._merge_steps_monotonic(
        None, sub, seen_completed, TID, "nl2sql", seen_active=seen_active
    )

    by_content = _status_by_content(steps)
    assert by_content[f"{CURRENT} (14m15s)"] == "in_progress"


def test_without_holder_behaviour_is_unchanged():
    """不传 seen_active（终态渲染等老调用方）⇒ 不做锁存，老行为原样保留。"""
    sub = _todos(
        (STEPS[0], "completed"),
        (STEPS[1], "completed"),
        *[(s, "pending") for s in STEPS[2:]],
    )
    steps = sst._merge_steps_monotonic(None, sub, set(), TID, "nl2sql")
    by_content = _status_by_content(steps)
    assert CURRENT in by_content
    assert by_content[CURRENT] == "pending"


# ── 修 1：耗时停表/续秒 ───────────────────────────────────────────

NOW = 1120.0  # 绝对时间无意义，只用来算差值


def _patch_progress(monkeypatch, history, started_at=1000.0):
    """把本地进度文件的读取替换成固定 step_history。"""
    from agent.subagents import track_progress

    monkeypatch.setattr(
        track_progress,
        "read_progress",
        lambda _tid: {"started_at": started_at, "step_history": history, "todos": []},
    )
    monkeypatch.setattr(time, "time", lambda: NOW)


def test_step_leaving_in_progress_stops_the_clock(monkeypatch):
    """12:57:42 的真实事件序列：in_progress → pending ⇒ 停表，不再续秒。"""
    history = [
        {"step": CURRENT, "from": None, "to": "in_progress", "ts": 1000.0},
        {"step": CURRENT, "from": "in_progress", "to": "pending", "ts": 1060.0},
    ]
    _patch_progress(monkeypatch, history)

    durations = sst._compute_step_durations(TID, completed_steps=set(), active_steps=set())

    assert durations.get(CURRENT) == "1m0s", f"应冻结在离开时刻，实际 {durations.get(CURRENT)!r}"
    assert "..." not in durations.get(CURRENT, ""), "已经不是当前步，不许带续秒后缀"


def test_latched_current_step_keeps_ticking(monkeypatch):
    """展示层锁存的当前步：同样有 to=pending 事件，但它是「假回退」，必须继续计时。"""
    history = [
        {"step": CURRENT, "from": None, "to": "in_progress", "ts": 1000.0},
        {"step": CURRENT, "from": "in_progress", "to": "pending", "ts": 1060.0},
    ]
    _patch_progress(monkeypatch, history)

    durations = sst._compute_step_durations(
        TID, completed_steps=set(), active_steps={CURRENT}
    )

    assert durations.get(CURRENT) == "2m0s...", (
        f"当前步应从最近一次 in_progress 续秒到 now，实际 {durations.get(CURRENT)!r}"
    )


def test_running_step_without_exit_ticks(monkeypatch):
    """只有 in_progress、还没离开 ⇒ 继续计时（原有行为）。"""
    history = [{"step": CURRENT, "from": None, "to": "in_progress", "ts": 1000.0}]
    _patch_progress(monkeypatch, history)

    durations = sst._compute_step_durations(
        TID, completed_steps=set(), active_steps={CURRENT}
    )

    assert durations.get(CURRENT) == "2m0s..."


def test_completed_step_is_frozen(monkeypatch):
    """正常完成的步：用 in_progress→completed 的区间，不带续秒后缀。"""
    history = [
        {"step": STEPS[1], "from": None, "to": "in_progress", "ts": 1000.0},
        {"step": STEPS[1], "from": "in_progress", "to": "completed", "ts": 1030.0},
    ]
    _patch_progress(monkeypatch, history)

    durations = sst._compute_step_durations(
        TID, completed_steps={STEPS[1]}, active_steps=set()
    )

    assert durations.get(STEPS[1]) == "30s"


def test_repeated_execution_uses_last_segment(monkeypatch):
    """同一步重跑（多个 in_progress）⇒ 以最后一段为准（原有语义不许漂移）。"""
    history = [
        {"step": CURRENT, "from": None, "to": "in_progress", "ts": 1000.0},
        {"step": CURRENT, "from": "in_progress", "to": "pending", "ts": 1010.0},
        {"step": CURRENT, "from": "pending", "to": "in_progress", "ts": 1100.0},
    ]
    _patch_progress(monkeypatch, history)

    durations = sst._compute_step_durations(
        TID, completed_steps=set(), active_steps={CURRENT}
    )

    assert durations.get(CURRENT) == "20s...", "应取最后一段（1100→1120）"
