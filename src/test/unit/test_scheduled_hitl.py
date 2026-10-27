"""定时任务 HITL 审批闸门：只拦变更类动作，允许 approve/reject/edit。

背景：manage_scheduled_task 的 create/pause/resume/remove/run_now 会让 agent
自主安排或改动未来无人值守的运行，必须在执行前经用户确认；list/runs 只读，
放行以免每问一句都弹审批卡。edit 允许用户在卡上直接改 cron/prompt 再批准。
"""
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest

TOOL = "manage_scheduled_task"
MUTATING = ("create", "pause", "resume", "remove", "run_now")
READONLY = ("list", "runs")


def _req(action=None, **extra):
    args = {} if action is None else {"action": action}
    args.update(extra)
    return ToolCallRequest(
        tool_call={"name": TOOL, "args": args, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )


def _hitl() -> HumanInTheLoopMiddleware:
    from agent.main_agent import MAIN_AGENT_MIDDLEWARE

    hits = [m for m in MAIN_AGENT_MIDDLEWARE if isinstance(m, HumanInTheLoopMiddleware)]
    assert len(hits) == 1, f"期望恰好 1 个 HITL 中间件，实际 {len(hits)}"
    return hits[0]


def _when():
    return _hitl().interrupt_on[TOOL]["when"]


def _patch_configurable(monkeypatch, value):
    """模拟当前 run 的 configurable（定时 fire 会写 scheduled_run=True）。"""
    from agent import main_agent

    monkeypatch.setattr(main_agent, "_current_configurable", lambda: value)


def test_tool_registered_on_hitl():
    assert TOOL in _hitl().interrupt_on


def test_allowed_decisions():
    cfg = _hitl().interrupt_on[TOOL]
    assert cfg["allowed_decisions"] == ["approve", "reject", "edit"]


def test_mutating_actions_interrupt(monkeypatch):
    _patch_configurable(monkeypatch, {})
    when = _when()
    for action in MUTATING:
        assert when(_req(action)) is True, f"{action} 应弹审批"


def test_readonly_actions_pass_through(monkeypatch):
    _patch_configurable(monkeypatch, {})
    when = _when()
    for action in READONLY:
        assert when(_req(action)) is False, f"{action} 不应弹审批"


def test_missing_action_passes_through(monkeypatch):
    """action 缺失/非法时交给工具自身校验报错，不弹无意义的审批卡。"""
    _patch_configurable(monkeypatch, {})
    when = _when()
    assert when(_req()) is False
    assert when(_req("bogus")) is False


def test_scheduled_run_auto_approves(monkeypatch):
    """定时触发的后台 run 无人值守：不弹审批卡。

    interrupt 在后台 run 会被 langgraph 顶层图抑制（工具不执行、也没有卡片可点），
    因此这里直接放行（副作用仍过 service 校验 + owner 权限）。
    """
    _patch_configurable(monkeypatch, {"scheduled_run": True})
    when = _when()
    for action in MUTATING:
        assert when(_req(action)) is False, f"定时 run 的 {action} 不应弹审批"


def test_config_missing_falls_back_to_interrupt(monkeypatch):
    """读不到 run config（交互式 run 正常情形）→ 仍按审批兜底。"""
    _patch_configurable(monkeypatch, {})
    assert _when()(_req("create")) is True
