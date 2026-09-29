# -*- coding: utf-8 -*-
"""账号级模型隔离验证（2026-09-28，离线，无需后端 / 数据库 / 网络）。

需求：「新创建的账号，不需要继承原有的大模型，要求新增自己的大模型；新增的账号是
**管理员账号**也要隔离大模型。」本次改动之前，`get_user_store` 会在用户文件不存在时
把共享 `model_config.json` **整份拷贝**给该账号（含别账号的 api_key）；即使删掉播种，
模块级 `deepseek_model` + `ThinkingToggleMiddleware` 的 `return request` 仍会**静默**
拿共享/别账号的 key 打模型。故本脚本验「删播种」+「fail-closed 门禁」两件事都在位。

验六组：
  ① 播种已移除：共享有 provider、目标账号无文件 ⇒ 该账号 store **空的**、且**不落文件**；
     同时**负对照**：老账号自己的（历史播种出的）文件仍可用、`get_store()` 仍读得到共享。
  ② 判据只有一份：`has_usable_model` 在「共享有 provider + 本账号空」时必须是 **False**
     （不回落共享），半截配置（缺 api_key / 缺 base_url / models 空）也必须是 False。
  ③ 门禁中间件：有身份且无模型 ⇒ **handler 调用 0 次** + 友好文案；有模型 / 无身份 ⇒
     原样放行（同步与异步两条路径都验）。
  ④ 账号间隔离：A upsert 后 B 仍是空 store；两份 store 是不同对象。
  ⑤ 附带两处（auto_title / thread_compact）：`create_model` 收到的是**各自的 user_id**。
  ⑥ fail-open：store 读取抛异常时按「无模型」处理，不把异常抛给上层。
  ⑦ 哨兵身份（dev / internal）不受影响：`dev` 是**非空串**，若判据写成 `if not user_id`
     会被按账号读 ⇒ 单机开发（AUTH_DISABLED）一个模型都没有；必须走 `is_real_owner`。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_user_model_config.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import 业务模块之前：model_config / workspace 的落点都由 AGENT_DATA_ROOT 推导，
# 不设会写进仓库（共享 model_config.json 含密钥，甚至会被发版 tar 打进镜像）。
_TEST_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-usermodel-")
os.environ["AGENT_DATA_ROOT"] = _TEST_ROOT
os.environ.setdefault("MODEL_CONFIG_SECRET", "verify-only-secret")

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.settings import model_config_store as mcs  # noqa: E402
from agent.settings.model_config_store import ModelConfig, get_store, get_user_store, reset_user_stores  # noqa: E402
from agent.llms.model import has_usable_model  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

NEW_USER = "u_new"        # 本次需求的目标：新账号
OLD_USER = "u_old"        # 改造前就已登录过、有历史副本的账号
ALICE = "alice"
BOB = "bob"

_GOOD = dict(
    base_url="http://127.0.0.1:9999/v1",
    api_key="sk-verify-not-a-real-key",
    models=[{"id": "m1"}],
    default_model="m1",
)


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n── {title} ──")


def user_file(uid: str) -> pathlib.Path:
    return pathlib.Path(_TEST_ROOT) / "users" / uid / "model_config.json"


def shared_file() -> pathlib.Path:
    return pathlib.Path(_TEST_ROOT) / "shared" / "model_config.json"


# ── 组 ① 播种已移除 ──────────────────────────────────────

def case_no_seed() -> None:
    section("① 播种已移除（新账号从空开始）")
    # 共享配置放 2 个 provider（改造前会被整份拷给新账号）
    shared = get_store()
    for name in ("shared_a", "shared_b"):
        shared.upsert(ModelConfig(name=name, **_GOOD))
    check(len(shared.list_configs()) == 2, "共享 store 有 2 个 provider", f"{len(shared.list_configs())}")

    # 老账号：改造**前**就已存在自己的文件（这里直接造出该状态），必须仍可用
    get_user_store(OLD_USER).upsert(ModelConfig(name="old_own", **_GOOD))
    check(user_file(OLD_USER).is_file(), "老账号自己的文件在盘上（历史副本）")

    reset_user_stores()  # 模拟进程重启后重新加载

    # 新账号：无文件 ⇒ 空 store，且**不落文件**
    new_store = get_user_store(NEW_USER)
    check(new_store.list_configs() == [], "新账号 list_configs() 为空（不继承共享）",
          f"{new_store.list_configs()}")
    check(new_store.get_active() == "", "新账号 active 为空")
    check(not user_file(NEW_USER).exists(), "新账号**没有**被落盘 model_config.json")

    # 负对照 A：老账号历史副本不被回溯清理
    check(len(get_user_store(OLD_USER).list_configs()) == 1,
          "负对照 A：老账号自己的 provider 仍在（不回溯清理）")

    # 负对照 B：删的是「播种」不是「共享」——全局 store 仍读得到共享配置
    reset_user_stores()
    check(len(get_store().list_configs()) == 2,
          "负对照 B：get_store() 仍读得到共享 provider", f"{len(get_store().list_configs())}")

    # 源码断言：播种代码与其辅助函数都不在了
    src = inspect.getsource(mcs.get_user_store)
    check("write_text" not in src, "get_user_store 源码无 write_text（不再拷贝）")
    check("_shared_config_path" not in src, "get_user_store 源码不再引用 _shared_config_path")
    mod_src = (_HERE.parents[1] / "src" / "agent" / "settings" / "model_config_store.py").read_text(encoding="utf-8")
    check("_shared_config_path" not in mod_src,
          "模块全文已无 _shared_config_path（死代码已删）")
    check(not hasattr(mcs, "_shared_config_path"), "模块对象上无 _shared_config_path 属性")


# ── 组 ② 判据只有一份 ────────────────────────────────────

def case_single_judgement() -> None:
    section("② 判据 has_usable_model（与 create_model 同一条解析链）")
    reset_user_stores()

    # 关键负对照：共享有 provider、本账号空 ⇒ 必须 False（不回落共享）
    check(user_file(NEW_USER).exists() is False, "前置：新账号仍无文件")
    check(has_usable_model(NEW_USER) is False,
          "关键负对照：共享有 provider 但本账号空 ⇒ False")

    # 完整配置 ⇒ True
    get_user_store(BOB).upsert(ModelConfig(name="own", **_GOOD))
    reset_user_stores()
    check(has_usable_model(BOB) is True, "完整配置 ⇒ True")

    # 半截配置三种 ⇒ False（弱判据「provider 个数 > 0」在这里会误判）。
    # 直接写盘（绕过 upsert 的 base_url 非空校验）——模拟"用户手填了一半就保存/历史遗留"。
    for uid, bad, label in (
        ("u_no_key", dict(base_url="http://x/v1", api_key="", models=[{"id": "m1"}]), "缺 api_key"),
        ("u_no_url", dict(base_url="", api_key="sk-x", models=[{"id": "m1"}]), "缺 base_url"),
        ("u_no_model", dict(base_url="http://x/v1", api_key="sk-x", models=[]), "模型列表为空"),
    ):
        p = user_file(uid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"version": 1, "active": "p", "providers": [{"name": "p", **bad}]},
            ensure_ascii=False,
        ), encoding="utf-8")
        reset_user_stores()
        s = get_user_store(uid)
        check(len(s.list_configs()) > 0, f"负对照前置：{label} 的 store 里有 provider 条目")
        check(has_usable_model(uid) is False, f"负对照：{label} ⇒ False")

    # 无身份：读全局 store（共享有配置 ⇒ True），维持旧行为
    check(has_usable_model(None) is True, "user_id=None 时读全局（有配置 ⇒ True）")
    check(has_usable_model("") is True, "user_id='' 时读全局（有配置 ⇒ True）")


# ── 组 ③ 门禁中间件 ──────────────────────────────────────

def _set_identity(uid: str):
    """把 `configurable.user_id` 放进 langgraph 的 runnable config（中间件就是这么读的）。"""
    from langchain_core.runnables.config import var_child_runnable_config

    token = var_child_runnable_config.set({"configurable": {"user_id": uid}})
    return token


def _clear_identity(token) -> None:
    from langchain_core.runnables.config import var_child_runnable_config

    var_child_runnable_config.reset(token)


def _model_request():
    """真实 ModelRequest（dataclass；中间件不读它的字段，但用真的更接近生产）。"""
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=None, messages=[], system_message=None, tool_choice=None,
        tools=[], response_format=None, state={}, runtime=None, model_settings=None,
    )


def case_middleware() -> None:
    section("③ ModelRequiredMiddleware（无模型 fail-closed，不调用 handler）")
    from agent.middlewares.model_required import NO_MODEL_MESSAGE, ModelRequiredMiddleware

    mw = ModelRequiredMiddleware()
    req = _model_request()
    sentinel = object()
    calls = {"sync": 0, "async": 0}

    def sync_handler(r):
        calls["sync"] += 1
        return sentinel

    async def async_handler(r):
        calls["async"] += 1
        return sentinel

    # 有身份 + 无模型 ⇒ handler 0 次 + 文案
    tok = _set_identity(NEW_USER)
    try:
        calls["sync"] = 0
        resp = mw.wrap_model_call(req, sync_handler)
        check(calls["sync"] == 0, "同步：无模型 ⇒ handler 调用 0 次")
        text = resp.result[0].content
        check("尚未配置可用的大模型" in text, "同步：返回友好文案", text[:34] + "…")
        check(text == NO_MODEL_MESSAGE, "同步：文案即 NO_MODEL_MESSAGE")
        from agent.utils.failure_signal import KIND_MODEL_REQUIRED, failed_mark
        check((failed_mark(resp.result[0]) or {}).get("kind") == KIND_MODEL_REQUIRED,
              "同一条消息带失败戳（零执行=失败，前端给「执行失败+重试」而不是当正常回答）")

        calls["async"] = 0
        aresp = asyncio.run(mw.awrap_model_call(req, async_handler))
        check(calls["async"] == 0, "异步：无模型 ⇒ handler 调用 0 次")
        check("尚未配置可用的大模型" in aresp.result[0].content, "异步：返回友好文案")
    finally:
        _clear_identity(tok)

    # 关键负对照：共享有 provider 而本账号无 ⇒ 仍然 0 次
    calls["sync"] = 0
    tok = _set_identity(NEW_USER)
    try:
        mw.wrap_model_call(req, sync_handler)
    finally:
        _clear_identity(tok)
    check(calls["sync"] == 0 and len(get_store().list_configs()) > 0,
          "关键负对照：共享有配置 + 本账号无 ⇒ 仍 0 次（不回落）")

    # 有身份 + 有模型 ⇒ 原样放行
    calls["sync"] = 0
    tok = _set_identity(BOB)
    try:
        out = mw.wrap_model_call(req, sync_handler)
        check(calls["sync"] == 1 and out is sentinel, "有模型 ⇒ handler 1 次且原样返回")
        calls["async"] = 0
        aout = asyncio.run(mw.awrap_model_call(req, async_handler))
        check(calls["async"] == 1 and aout is sentinel, "有模型（异步）⇒ handler 1 次")
    finally:
        _clear_identity(tok)

    # 无身份 ⇒ 维持旧行为（放行）
    calls["sync"] = 0
    out = mw.wrap_model_call(req, sync_handler)   # 不在 runnable context 里
    check(calls["sync"] == 1 and out is sentinel, "无身份（旧行为）⇒ handler 1 次")

    # 两个图都挂了该中间件，且在最外层
    check(*_assert_wired("agent/main_agent.py", "main_agent.py"), )
    check(*_assert_wired("agent/graphs/nl2sql_agent.py", "nl2sql_agent.py"))

    # is_blocked_for 的 fail-closed
    from agent.middlewares.model_required import is_blocked_for

    check(is_blocked_for("") is False, "is_blocked_for('') ⇒ False（无身份不拦）")
    check(is_blocked_for(NEW_USER) is True, "is_blocked_for(空账号) ⇒ True")


def _assert_wired(rel_path: str, label: str) -> tuple[bool, str]:
    """AST 断言：该图的 middleware 列表里挂了 ModelRequiredMiddleware。"""
    path = _HERE.parents[1] / "src" / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # 找 `middleware=<列表>` 的实参
    targets: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "create_deep_agent":
            for kw in node.keywords:
                if kw.arg == "middleware":
                    targets.append(kw.value)
    if not targets:
        return False, f"{label}: 未找到 create_deep_agent(middleware=...)"

    def is_required(n: ast.AST) -> bool:
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "ModelRequiredMiddleware")

    if rel_path.endswith("main_agent.py"):
        node = targets[0]
        ok = isinstance(node, ast.List) and bool(node.elts) and is_required(node.elts[0])
        return ok, f"{label}: middleware 列表首元素是 ModelRequiredMiddleware"

    # nl2sql_agent：`_middleware` 先 append 若干、再 insert(0, ModelRequiredMiddleware())
    # 且最终 `middleware=_middleware` 被 create_deep_agent 使用
    passed_to_call = any(isinstance(n, ast.Name) and n.id == "_middleware" for n in targets)
    inserted = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert" and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0
                and is_required(node.args[1])):
            inserted = True
    return (passed_to_call and inserted), \
        f"{label}: insert(0, ModelRequiredMiddleware()) 且 middleware=_middleware"


# ── 组 ④ 账号间隔离 ──────────────────────────────────────

def case_isolation() -> None:
    section("④ 账号间隔离")
    reset_user_stores()
    sa, sb = get_user_store(ALICE), get_user_store(BOB)
    check(sa is not sb, "两个账号拿到不同 store 对象")
    before = len(sb.list_configs())
    sa.upsert(ModelConfig(name="a_only", **_GOOD))
    check(all(p["name"] != "a_only" for p in sb.list_configs()),
          "A 新增的 provider 不出现在 B 的列表里")
    check(before == len(sb.list_configs()), "B 的 provider 数未变")

    reset_user_stores()
    check(all(p["name"] != "a_only" for p in get_user_store(BOB).list_configs()),
          "重启缓存后 B 仍看不到 A 的 provider")
    check(any(p["name"] == "a_only" for p in get_user_store(ALICE).list_configs()),
          "重启缓存后 A 仍看得到自己的 provider")

    # 新账号（无文件）的列表 == 前端门禁的输入
    reset_user_stores()
    check(get_user_store("u_brand_new").list_configs() == [],
          "全新账号的 list_configs() 为空 ⇒ 前端 modelConfigured=false")


# ── 组 ⑤ 附带两处按账号解析 ──────────────────────────────

def case_side_endpoints() -> None:
    section("⑤ auto_title / thread_compact 传各自的 user_id")
    from agent import llms
    from api import auto_title, thread_compact

    seen: list[tuple[str, str | None]] = []
    real_create = llms.model.create_model

    def fake_create(*args, **kwargs):
        seen.append((kwargs.get("route") or "", kwargs.get("user_id")))
        return object()

    llms.model.create_model = fake_create
    try:
        auto_title._title_models.clear()
        m_bob = auto_title._get_title_model(BOB)
        m_alice = auto_title._get_title_model(ALICE)
        check(m_bob is not m_alice, "不同账号拿到不同标题模型实例")
        check(seen == [("", BOB), ("", ALICE)],
              "auto_title 把各自 user_id 传给 create_model", f"{seen}")
        m_bob2 = auto_title._get_title_model(BOB)
        check(m_bob2 is m_bob and len(seen) == 2, "同账号命中缓存，不重复建实例")

        seen.clear()
        asyncio.run(thread_compact._generate_summary([{"type": "human", "content": "hi"}], BOB))
        check(seen and seen[0][1] == BOB, "thread_compact 把 user_id 传给 create_model", f"{seen}")
    finally:
        llms.model.create_model = real_create
        auto_title._title_models.clear()

    # 空 user_id ⇒ 不传（None ⇒ 全局 store），维持无身份路径
    seen.clear()
    llms.model.create_model = fake_create
    try:
        auto_title._get_title_model("")
        check(seen and seen[0][1] is None, "user_id 为空 ⇒ 传 None（全局 store）")
    finally:
        llms.model.create_model = real_create
        auto_title._title_models.clear()


# ── 组 ⑥ fail-open / 不抛 ────────────────────────────────

def case_fail_open() -> None:
    section("⑥ 异常不冒泡（按「无模型」处理）")
    from agent.middlewares.model_required import is_blocked_for

    real = mcs.get_user_store

    def boom(uid):
        raise RuntimeError("boom")

    mcs.get_user_store = boom
    try:
        check(has_usable_model("anyone") is False, "store 抛异常 ⇒ has_usable_model False（不抛）")
        check(is_blocked_for("anyone") is True, "is_blocked_for 异常 ⇒ True（fail-closed）")
    finally:
        mcs.get_user_store = real

    # 非法入参不炸
    check(has_usable_model("不存在的账号/../等等") is False, "奇怪账号名 ⇒ False（不炸）")
    try:
        has_usable_model(123)  # type: ignore[arg-type]
        check(True, "非字符串入参不抛异常")
    except Exception as e:  # noqa: BLE001
        check(False, "非字符串入参不抛异常", f"{type(e).__name__}: {e}")


# ── 组 ⑦ 哨兵身份（dev / internal）不受影响 ──────────────

def case_sentinel_identities() -> None:
    section("⑦ 哨兵身份 dev / internal 回退全局（不误伤本地开发）")
    from agent.middlewares.model_required import is_blocked_for

    reset_user_stores()
    # 关键：`dev` 是非空串，若判据写成 `if not user_id` 就会被按账号读 ⇒ 单机开发
    # 一个模型都没有（界面空 + 禁发 + run 被拒）。必须走 is_real_owner。
    check(is_blocked_for("dev") is False, "is_blocked_for('dev') ⇒ False（AUTH_DISABLED 本地开发）")
    check(is_blocked_for("internal") is False, "is_blocked_for('internal') ⇒ False（内部自调用）")
    check(is_blocked_for("legacy") is False, "is_blocked_for('legacy') ⇒ False（存量哨兵）")
    check(is_blocked_for(NEW_USER) is True, "对照：真实账号（空配置）⇒ True")
    check(is_blocked_for(BOB) is False, "对照：真实账号（有配置）⇒ False")

    # store 层：dev / internal 拿到的是**全局** store（共享 provider 可见）
    check(get_user_store("dev") is get_store(), "get_user_store('dev') 就是全局 store")
    check(get_user_store("internal") is get_store(), "get_user_store('internal') 就是全局 store")
    check(len(get_user_store("dev").list_configs()) == len(get_store().list_configs()) > 0,
          "dev 账号在界面上看得到共享 provider（不再空态）")
    check(has_usable_model("dev") is True, "has_usable_model('dev') ⇒ True（共享有配置）")

    # 中间件：dev 身份下放行
    from agent.middlewares.model_required import ModelRequiredMiddleware

    req = _model_request()
    sentinel = object()
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        return sentinel

    tok = _set_identity("dev")
    try:
        out = ModelRequiredMiddleware().wrap_model_call(req, handler)
    finally:
        _clear_identity(tok)
    check(calls["n"] == 1 and out is sentinel, "dev 身份 ⇒ 放行（handler 1 次）")


def main() -> int:
    print("账号级模型隔离验证（2026-09-28）")
    print(f"AGENT_DATA_ROOT={_TEST_ROOT}")
    case_no_seed()
    case_single_judgement()
    case_middleware()
    case_isolation()
    case_side_endpoints()
    case_fail_open()
    case_sentinel_identities()

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"结果：{passed}/{total} 通过")
    for ok, label in results:
        if not ok:
            print(f"  [{NG}] {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
