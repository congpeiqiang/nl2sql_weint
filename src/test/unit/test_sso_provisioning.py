"""SSO 默认配置播种（公有模型 + 固定库）—— 2026-10-28。

背景：工号首次登录自动建号，但新账号按 09-28 拍板「不继承任何共享配置」⇒ 空模型、
空授权 = 能进聊天框、按发送被 `ModelRequiredMiddleware` 拦下。播种就是把**模板账号**
的 provider 与 grants 复制过去，让「点 AI 就能问」成立。

本文件锁四件事：
1. 真的复制过去了，且复制后的模型是**可用**的（过 `has_usable_model` 这条唯一判据）；
2. 幂等闸判「非空就跳过」——用户自己的模型/授权不能被覆盖；
3. 开关默认关：没配模板账号时一行都不写；
4. 只对工号账号播种（09-28 拍板的适用范围）。
"""
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import agent.auth.grants as grants
import agent.auth.token as token_mod
import agent.auth.users as users
from agent.auth.provisioning import ensure_sso_defaults
from agent.llms.model import has_usable_model
from agent.settings.model_config_store import (
    ModelConfig,
    get_user_store,
    reset_user_stores,
)

TEMPLATE = "ai-shared"
PUBLIC_MODEL = "公司公有模型"


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def seed_env(tmp_path, monkeypatch):
    """隔离数据根、密钥、用户库、授权库、模型 store —— 一个都不能漏。

    `_user_stores` 与 `grants._db_conn` 都是**进程级单例**：不清就会指向上一个
    测试的临时目录，测试之间互相污染（而且看起来是「播种没生效」）。
    """
    data_root = tmp_path / "agent_data"
    data_root.mkdir()
    monkeypatch.setenv("AGENT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("NL2SQL_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("MODEL_CONFIG_SECRET", "test-model-secret")
    monkeypatch.setenv("NL2SQL_AUTH_DISABLED", "0")
    monkeypatch.delenv("NL2SQL_SSO_SEED_FROM", raising=False)
    monkeypatch.setattr(users, "_users_cache", None)
    monkeypatch.setattr(token_mod, "_secret_cache", None)
    monkeypatch.setattr(grants, "_db_conn", None)
    reset_user_stores()
    yield data_root
    reset_user_stores()
    users._users_cache = None
    grants._db_conn = None


def _seed_template(with_model=True, dbs=("ops_db",)):
    """配好模板账号：一个公有 provider（已激活）+ 若干库授权。"""
    if with_model:
        s = get_user_store(TEMPLATE)
        s.upsert(
            ModelConfig(
                name=PUBLIC_MODEL,
                base_url="http://10.0.0.1:8100/v1",
                api_key="sk-public",
                models=[{"id": "qwen-max"}],
                default_model="qwen-max",
            )
        )
        s.set_active(PUBLIC_MODEL)
    for db in dbs:
        grants.grant_db(TEMPLATE, db)


def _model_names(user_id):
    return [p["name"] for p in get_user_store(user_id).list_configs()]


def _grant_dbs(user_id):
    return sorted(g["db_name"] for g in grants.list_user_grants(user_id))


# ── 复制 ────────────────────────────────────────────────────────────


def test_template_model_is_copied(seed_env, monkeypatch):
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()

    ensure_sso_defaults("Z051", "Z051")

    store = get_user_store("Z051")
    assert _model_names("Z051") == [PUBLIC_MODEL]
    assert store.get_active() == PUBLIC_MODEL, "激活项要跟着模板走，否则用户看到的是第一个"
    assert store.get(PUBLIC_MODEL).api_key == "sk-public", "密文能解开 ⇒ 真的能用"


def test_seeded_model_passes_the_usable_model_gate(seed_env, monkeypatch):
    """端到端判据：播种之后 `has_usable_model` 必须为真，否则发送仍被拦。"""
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()
    assert has_usable_model("Z051") is False

    ensure_sso_defaults("Z051", "Z051")

    assert has_usable_model("Z051") is True


def test_template_grants_are_copied(seed_env, monkeypatch):
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template(dbs=("ops_db", "bi_db"))

    ensure_sso_defaults("Z051", "Z051")

    assert _grant_dbs("Z051") == ["bi_db", "ops_db"]
    assert {g["level"] for g in grants.list_user_grants("Z051")} == {"query"}


def test_seeding_is_idempotent(seed_env, monkeypatch):
    """每次登录都会调一次 —— 第二次不能复制出第二个同名 provider。"""
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()

    ensure_sso_defaults("Z051", "Z051")
    ensure_sso_defaults("Z051", "Z051")

    assert _model_names("Z051") == [PUBLIC_MODEL]
    assert _grant_dbs("Z051") == ["ops_db"]


# ── 幂等闸：用户自己的配置不能被覆盖 ────────────────────────────────


def test_existing_model_is_not_overwritten(seed_env, monkeypatch):
    """判「有没有 provider」而不是「active 是否为空」：用户加了没激活的模型也算有。"""
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()
    own = get_user_store("Z051")
    own.upsert(
        ModelConfig(
            name="我自己配的",
            base_url="http://10.0.0.2:8100/v1",
            api_key="sk-own",
            models=[{"id": "my-model"}],
        )
    )

    ensure_sso_defaults("Z051", "Z051")

    assert _model_names("Z051") == ["我自己配的"]
    assert get_user_store("Z051").get_active() == "我自己配的"


def test_existing_grants_are_not_touched(seed_env, monkeypatch):
    """判「有没有授权」而不是「缺哪个库补哪个」：撤销公有库后不能在下次登录被补回来。"""
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()
    grants.grant_db("Z051", "another_db")

    ensure_sso_defaults("Z051", "Z051")

    assert _grant_dbs("Z051") == ["another_db"], "已有授权的人不该被补公有库"


# ── 不该播种的情况 ──────────────────────────────────────────────────


def test_no_template_configured_does_nothing(seed_env):
    """开关默认关：不配模板账号就一行都不写。"""
    _seed_template()

    ensure_sso_defaults("Z051", "Z051")

    assert _model_names("Z051") == []
    assert _grant_dbs("Z051") == []


def test_non_emp_account_is_skipped(seed_env, monkeypatch):
    """09-28 拍板只对普通新建账号有效；这里把适用范围收窄到「工号账号」。"""
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()

    ensure_sso_defaults("lisi", "")

    assert _model_names("lisi") == []
    assert _grant_dbs("lisi") == []


def test_template_is_never_seeded_into_itself(seed_env, monkeypatch):
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()

    ensure_sso_defaults(TEMPLATE, "Z999")

    assert _model_names(TEMPLATE) == [PUBLIC_MODEL]
    assert _grant_dbs(TEMPLATE) == ["ops_db"]


# ── 走真实登录路径 ──────────────────────────────────────────────────


def _login_client():
    import api.sso_routes as sso

    return TestClient(Starlette(routes=list(sso.routes)))


def test_first_login_seeds_model_and_grants(seed_env, monkeypatch):
    """整条链：未知工号 → 自动建号 → 播种 → 用户拿到可用模型与可见库。"""
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)
    _seed_template()

    resp = _login_client().post("/api/sso/login", json={"emp_no": "Z051"})

    assert resp.status_code == 200
    assert users.find_by_emp_no("Z051") is not None
    assert has_usable_model("Z051") is True
    assert _grant_dbs("Z051") == ["ops_db"]


def test_login_still_succeeds_when_seeding_blows_up(seed_env, monkeypatch):
    """播种是附加动作：模板配坏了也不能把登录一起带沟里。"""
    monkeypatch.setenv("NL2SQL_SSO_SEED_FROM", TEMPLATE)

    import api.sso_routes as sso

    def boom(*_a, **_k):
        raise RuntimeError("模型 store 坏了")

    monkeypatch.setattr(sso, "ensure_sso_defaults", boom, raising=True)
    resp = _login_client().post("/api/sso/login", json={"emp_no": "Z051"})

    assert resp.status_code == 200
    assert "set-cookie" in resp.headers
