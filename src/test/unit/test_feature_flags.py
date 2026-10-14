"""功能开关集中校验的测试（Phase 2-5）。

覆盖三类：① 合法值解析正确 ② 非法值**必须抛错**（这是本模块存在的全部理由——
非法值静默回落是「以为开了、其实没开」事故的根源）③ 单例语义（缓存 + 可重置）。
"""
import pytest

from agent.settings.feature_flags import (
    FeatureFlags,
    get_flags,
    reset_flags_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch: pytest.MonkeyPatch):
    """每个用例前后清空单例 + 清掉相关 env，避免相互污染（env 是进程级的）。"""
    for name in (
        "NL2SQL_RETRIEVAL", "NL2SQL_RETRIEVAL_BACKEND", "NL2SQL_FAIL_CIRCUIT",
        "NL2SQL_CUBE_ROUTE_GATE", "NL2SQL_KNOWLEDGE_TRIM", "NL2SQL_DRAIN_SECS",
        "NL2SQL_RETENTION_ENABLED", "NL2SQL_AUTH_DISABLED", "NL2SQL_DBMCP_ENABLED",
        "NL2SQL_LLM_MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_flags_for_tests()
    yield
    reset_flags_for_tests()


class TestDefaults:
    def test_defaults_when_unset(self):
        f = FeatureFlags()
        assert f.retrieval == "off"
        assert f.retrieval_backend == "lance"
        assert f.fail_circuit is True
        assert f.drain_secs == 180
        assert f.auth_disabled is False
        assert f.llm_max_concurrency == 6

    def test_retrieval_modes_are_off_fts_hybrid(self, monkeypatch):
        """`fts` 是合法值——实施手册里写的 `greedy` 是错的（真身见 retrieval/schema.py）。"""
        for mode in ("off", "fts", "hybrid"):
            monkeypatch.setenv("NL2SQL_RETRIEVAL", mode)
            assert FeatureFlags().retrieval == mode

    def test_retrieval_backends(self, monkeypatch):
        for backend in ("lance", "jsonl"):
            monkeypatch.setenv("NL2SQL_RETRIEVAL_BACKEND", backend)
            assert FeatureFlags().retrieval_backend == backend

    def test_bool_accepts_common_spellings(self, monkeypatch):
        for raw in ("1", "true", "TRUE", "yes", "On"):
            monkeypatch.setenv("NL2SQL_FAIL_CIRCUIT", raw)
            assert FeatureFlags().fail_circuit is True
        for raw in ("0", "false", "no", "off"):
            monkeypatch.setenv("NL2SQL_FAIL_CIRCUIT", raw)
            assert FeatureFlags().fail_circuit is False

    def test_llm_concurrency_zero_is_legal(self, monkeypatch):
        """`0` = 关闸（llm_gate.py:224），不是非法值——别把它当负值拒掉。"""
        monkeypatch.setenv("NL2SQL_LLM_MAX_CONCURRENCY", "0")
        assert FeatureFlags().llm_max_concurrency == 0


class TestAuthDisabledStrict:
    """`NL2SQL_AUTH_DISABLED` 只认 `1`/`0`——两个读取点都是 `== "1"` 严格比较。"""

    def test_accepts_one_and_zero(self, monkeypatch):
        monkeypatch.setenv("NL2SQL_AUTH_DISABLED", "1")
        assert FeatureFlags().auth_disabled is True
        monkeypatch.setenv("NL2SQL_AUTH_DISABLED", "0")
        assert FeatureFlags().auth_disabled is False

    def test_rejects_true_because_token_py_would_ignore_it(self, monkeypatch):
        """`=true` 在 token.py 里**不生效**（鉴权照常开）⇒ 必须报错，不能报「已关闭」。"""
        monkeypatch.setenv("NL2SQL_AUTH_DISABLED", "true")
        with pytest.raises(ValueError, match="NL2SQL_AUTH_DISABLED"):
            FeatureFlags()


class TestRejectsInvalid:
    def test_typo_in_retrieval_mode_raises(self, monkeypatch):
        """`hybird` 拼错时必须吵——静默降级纯 FTS 是最隐蔽的那类事故。"""
        monkeypatch.setenv("NL2SQL_RETRIEVAL", "hybird")
        with pytest.raises(ValueError, match="NL2SQL_RETRIEVAL"):
            FeatureFlags()

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("NL2SQL_RETRIEVAL_BACKEND", "faiss")
        with pytest.raises(ValueError, match="NL2SQL_RETRIEVAL_BACKEND"):
            FeatureFlags()

    def test_typo_in_bool_raises(self, monkeypatch):
        monkeypatch.setenv("NL2SQL_RETENTION_ENABLED", "ture")
        with pytest.raises(ValueError, match="NL2SQL_RETENTION_ENABLED"):
            FeatureFlags()

    def test_non_integer_drain_secs_raises(self, monkeypatch):
        monkeypatch.setenv("NL2SQL_DRAIN_SECS", "abc")
        with pytest.raises(ValueError, match="NL2SQL_DRAIN_SECS"):
            FeatureFlags()

    def test_negative_drain_secs_raises(self, monkeypatch):
        monkeypatch.setenv("NL2SQL_DRAIN_SECS", "-1")
        with pytest.raises(ValueError, match="NL2SQL_DRAIN_SECS"):
            FeatureFlags()

    def test_error_message_lists_allowed_values(self, monkeypatch):
        """报错要能直接照着改——列出允许值，而不是只说「不合法」。"""
        monkeypatch.setenv("NL2SQL_RETRIEVAL", "bogus")
        with pytest.raises(ValueError) as ei:
            FeatureFlags()
        assert "fts" in str(ei.value)
        assert "hybrid" in str(ei.value)


class TestSingleton:
    def test_get_flags_caches(self):
        assert get_flags() is get_flags()

    def test_get_flags_validates(self, monkeypatch):
        monkeypatch.setenv("NL2SQL_RETRIEVAL", "nope")
        with pytest.raises(ValueError):
            get_flags()

    def test_reset_rebuilds(self, monkeypatch):
        first = get_flags()
        monkeypatch.setenv("NL2SQL_DRAIN_SECS", "30")
        reset_flags_for_tests()
        assert get_flags() is not first
        assert get_flags().drain_secs == 30

    def test_as_dict_covers_all_fields(self):
        d = FeatureFlags().as_dict()
        assert set(d) >= {
            "retrieval", "retrieval_backend", "fail_circuit", "cube_route_gate",
            "knowledge_trim", "drain_secs", "retention_enabled", "auth_disabled",
            "db_mcp_enabled", "llm_max_concurrency",
        }