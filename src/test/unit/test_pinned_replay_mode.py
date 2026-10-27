"""固化回放模式：摘掉委派工具 + 注入数据源说明（T22、T23）。"""


class T:
    def __init__(self, name):
        self.name = name


def _fake_request(tools):
    class Req:
        def __init__(self, tools):
            self.tools = tools

        def override(self, *, tools):
            return Req(tools)

    return Req(tools)


def _fake_msg_request(text):
    class Msg:  # 只需要 .text，与 dynamic_prompt 的取法一致
        def __init__(self, t):
            self.text = t

    class Req:
        def __init__(self, t):
            self.system_message = Msg(t)

        def override(self, *, system_message):
            return Req(system_message.text)

    return Req(text)


def _render_prompt(m, text="_"):
    """驱动 ``dynamic_prompt`` 中间件（``@dynamic_prompt`` 返回的是**中间件实例**，
    不是函数——只能走 ``wrap_model_call`` 才能看到它最终产出的提示词）。"""
    out = {}

    def handler(req):
        # langchain 的 ``.text`` 是 TextAccessor（代理 str），比较位置前先转成真 str
        out["prompt"] = str(req.system_message.text)
        return "ok"

    m.dynamic_prompt.wrap_model_call(_fake_msg_request(text), handler)
    return out["prompt"]


def test_middleware_strips_delegation(monkeypatch):
    from agent.middlewares.pinned_replay import PinnedReplayMiddleware

    tools = [T("build_report"), T("start_async_task"), T("generate_echarts")]
    req = _fake_request(tools)
    monkeypatch.setattr(
        "agent.middlewares.pinned_replay._cfg", lambda: {"pinned_replay": True}
    )
    out = PinnedReplayMiddleware()._strip(req)
    assert [t.name for t in out.tools] == ["build_report", "generate_echarts"]


def test_middleware_untouched_without_flag(monkeypatch):
    from agent.middlewares.pinned_replay import PinnedReplayMiddleware

    monkeypatch.setattr("agent.middlewares.pinned_replay._cfg", lambda: {})
    req = _fake_request([T("build_report")])
    assert PinnedReplayMiddleware()._strip(req) is req


def test_middleware_keeps_request_when_nothing_to_strip(monkeypatch):
    """工具面里本来就没有委派工具 ⇒ 不新建对象（省一次无谓的 override）。"""
    from agent.middlewares.pinned_replay import PinnedReplayMiddleware

    monkeypatch.setattr(
        "agent.middlewares.pinned_replay._cfg", lambda: {"pinned_replay": True}
    )
    req = _fake_request([T("build_report")])
    assert PinnedReplayMiddleware()._strip(req) is req


def test_dynamic_prompt_injects_section(monkeypatch):
    import agent.main_agent as m

    monkeypatch.setattr(m, "_get_configurable", lambda: {"pinned_replay": True})
    p = _render_prompt(m, "BASE")
    assert "固化回放模式" in p and "禁止委派" in p and p.endswith("BASE")


def test_dynamic_prompt_plain_when_absent(monkeypatch):
    import agent.main_agent as m

    monkeypatch.setattr(m, "_get_configurable", lambda: {})
    assert "固化回放模式" not in _render_prompt(m, "BASE")


def test_dynamic_prompt_section_precedes_db_section(monkeypatch):
    """回放段优先级最高 ⇒ 排在「当前数据库」段之前。"""
    import agent.main_agent as m

    monkeypatch.setattr(
        m, "_get_configurable", lambda: {"pinned_replay": True, "db_name": "d"}
    )
    p = _render_prompt(m, "BASE")
    assert p.index("固化回放模式") < p.index("当前数据库")


def test_middleware_registered_right_after_hitl():
    """回放闸紧接着审批闸（同属「拦截模型可见能力」层）。"""
    import agent.main_agent as m

    names = [type(x).__name__ for x in m.MAIN_AGENT_MIDDLEWARE]
    i = names.index("HumanInTheLoopMiddleware")
    assert names[i + 1] == "PinnedReplayMiddleware", names
