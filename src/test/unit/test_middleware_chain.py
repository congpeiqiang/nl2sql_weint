"""验证中间件链注册顺序——任何顺序变更必须显式修改此测试。"""


class TestMainAgentMiddlewareChain:
    """主 Agent 中间件链顺序约束。"""

    def _get_middleware_types(self) -> list[str]:
        from agent.main_agent import MAIN_AGENT_MIDDLEWARE
        return [type(mw).__name__ for mw in MAIN_AGENT_MIDDLEWARE]

    def test_chain_length(self):
        """主 Agent 应有 19 层中间件（18 → 19：2026-10-23 加 PinnedReplayMiddleware）。"""
        chain = self._get_middleware_types()
        assert len(chain) == 19, f"期望 19 层，实际 {len(chain)}: {chain}"

    def test_scheduled_hitl_inside_guards(self):
        """定时任务审批闸门紧跟三道模型门禁之后（after_model 逆序执行 ⇒ 录完再拦）。"""
        chain = self._get_middleware_types()
        assert chain[3] == "HumanInTheLoopMiddleware", chain

    def test_guard_middlewares_outermost(self):
        """门禁类中间件必须在最外层（列表头部）。"""
        chain = self._get_middleware_types()
        assert chain[0] == "ModelRequiredMiddleware"
        assert chain[1] == "QuotaErrorMiddleware"
        assert chain[2] == "ModelTimeoutMiddleware"

    def test_vfs_path_resolver_innermost(self):
        """VfsPathResolver 必须最内层（列表尾部）。"""
        chain = self._get_middleware_types()
        assert chain[-1] == "VfsPathResolverMiddleware"

    def test_message_slimmer_before_knowledge_trim(self):
        """MessageSlimmer 必须在 KnowledgeTrim 外层（index 更小）。"""
        chain = self._get_middleware_types()
        slim_idx = chain.index("MessageSlimmerMiddleware")
        trim_idx = chain.index("KnowledgeTrimMiddleware")
        assert slim_idx < trim_idx, f"Slimmer@{slim_idx} 应在 Trim@{trim_idx} 外层"


class TestNl2sqlAgentMiddlewareChain:
    """NL2SQL 子 Agent 中间件链顺序约束。"""

    def _get_middleware_types(self) -> list[str]:
        from agent.graphs.nl2sql_agent import NL2SQL_MIDDLEWARE
        return [type(mw).__name__ for mw in NL2SQL_MIDDLEWARE]

    def test_chain_length(self):
        """子 Agent 应有 22-23 层中间件（sql_approval 可选）。"""
        chain = self._get_middleware_types()
        assert 22 <= len(chain) <= 23, f"期望 22-23 层，实际 {len(chain)}: {chain}"

    def test_model_required_outermost(self):
        """ModelRequired 必须最外层（最后 insert(0) 的结果）。"""
        chain = self._get_middleware_types()
        assert chain[0] == "ModelRequiredMiddleware"

    def test_write_todos_last(self):
        """WriteTodosProtocol 必须最后追加（在条件 sql_approval 之后）。"""
        chain = self._get_middleware_types()
        assert chain[-1] == "WriteTodosProtocolMiddleware"

    def test_failure_circuit_between_slimmer_and_langfuse(self):
        """FailureCircuit 必须在 MessageSlimmer 内层、LangfuseSpan 外层。"""
        chain = self._get_middleware_types()
        fc_idx = chain.index("FailureCircuitMiddleware")
        slim_idx = chain.index("MessageSlimmerMiddleware")
        lf_idx = chain.index("LangfuseSpanMiddleware")
        assert slim_idx < fc_idx < lf_idx
