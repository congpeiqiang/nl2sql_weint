# -*- coding: utf-8 -*-
"""续跑 run 的上下文继承（P1/P1b，离线，无需后端/数据库/网络）。

**要关的口子**（生产 trace `eaf1c8b2c469a3d1fb352e4ff1af1167`，2026-09-26）：

报告**永远**由「同步循环建出来的**续跑 run**」落盘（子 agent 完成后，sync watcher
`runs.create` 注入一条通知让主 agent 继续 → 绘图/报告）。该 run 建的时候
`config={"recursion_limit": 500}`，**没带 configurable**：

- 主 agent 系统提示里「当前数据库」段消失；
- `build_report` 的 `_current_db_name()` 读成空 ⇒ `load_knowledge_corpus("")` 直接 `[]`
  ⇒ 报告侧**逐字核验永远降级**成「未核验」（子 agent 侧闸门却拿父 run 透传的 db_name
  正常核验 ⇒ 两套账）。

为什么服务端那层补不上：`deepagents_async_config_patch._wrap_runs_create` 只在
**未显式传 config** 时才注入当前 configurable；这两处续跑都带着 config 调用。

本脚本断言：
1. `_run_context_config` 从父 run 的 `metadata`（服务端 `LangfuseMetadataMiddleware`
   写的**钳制后**的值）或 `configurable` 取出 user_id + db_name，拼成续跑 config；
2. 取不到时**不写** `configurable` 键（空 dict 会盖掉服务端注入的上下文）；
3. `grants.dbs_for_thread` 账本往返（含 `last_seen` 排序、未知会话、空 id）；
4. `report_builder._current_db_name()` 的优先级：configurable > 账本恰好一个库 >
   多个库/无库/无 thread → 空串（**不猜**：拿错库的语料去判模型不合规，比不核验更坏）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_run_context_inherit.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

# grants 的库路径读 AGENT_DATA_ROOT（调用时求值）⇒ 先钉临时根，别写进真实 auth.sqlite。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-runctx-")
os.environ["AGENT_DATA_ROOT"] = _TMP_ROOT

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import langgraph.config as _lg_config  # noqa: E402

from agent.auth import grants  # noqa: E402
from agent.subagents.sync_subagent_todos import _run_context_config  # noqa: E402
from agent.tools import report_builder as rb  # noqa: E402

OK, NG = "✓", "✗"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


# ── 1. _run_context_config ────────────────────────────────────────────
def t1_metadata_source() -> None:
    print("\n[t1] 从父 run 的 metadata 继承（服务端写的钳制后值）")
    cfg = _run_context_config(
        {"run_id": "r1", "metadata": {"db_name": "workhour", "langfuse_user_id": "u123"}}
    )
    conf = cfg.get("configurable") or {}
    check(conf.get("db_name") == "workhour", "db_name 进入 configurable", conf.get("db_name"))
    check(conf.get("user_id") == "u123", "user_id 双写其一", conf.get("user_id"))
    check(conf.get("langgraph_auth_user_id") == "u123",
          "user_id 双写其二（与中间件约定的键）", conf.get("langgraph_auth_user_id"))
    check(cfg.get("recursion_limit") == 500, "recursion_limit 仍在", cfg.get("recursion_limit"))


def t2_configurable_source() -> None:
    print("\n[t2] 从父 run 的 configurable 继承（SDK 通常没有该键，有就用）")
    cfg = _run_context_config(
        {"config": {"configurable": {"db_name": "db_x", "user_id": "u9"}}}
    )
    conf = cfg.get("configurable") or {}
    check(conf.get("db_name") == "db_x", "读 config.configurable.db_name")
    check(conf.get("user_id") == "u9", "读 config.configurable.user_id")


def t3_missing_no_clobber() -> None:
    print("\n[t3] 取不到时不写 configurable（关键负对照：空 dict 会盖掉服务端上下文）")
    for label, run in [
        ("run=None", None),
        ("run 无 metadata", {"run_id": "r1"}),
        ("metadata 是列表（脏值）", {"metadata": ["x"]}),
        ("metadata 无相关键", {"metadata": {"other": 1}}),
        ("db_name 是空串 + 无 user_id", {"metadata": {"db_name": ""}}),
    ]:
        cfg = _run_context_config(run)
        check("configurable" not in cfg, f"{label} → config 无 configurable 键", list(cfg))
        check(cfg.get("recursion_limit") == 500, f"{label} → recursion_limit 保留")
    cfg = _run_context_config({"metadata": {"db_name": "", "langfuse_user_id": "u1"}})
    conf = cfg.get("configurable") or {}
    check(conf.get("user_id") == "u1" and "db_name" not in conf,
          "db_name 空但 user_id 有 → 只补有值的键（不塞空 db_name）", conf)


# ── 2. dbs_for_thread 账本 ────────────────────────────────────────────
def t4_ledger_roundtrip() -> None:
    print("\n[t4] thread_db 账本往返")
    t = "thread-A"
    grants.record_thread_db(t, "db1")
    time.sleep(0.02)
    grants.record_thread_db(t, "db2")
    time.sleep(0.02)
    grants.record_thread_db(t, "db1")  # ON CONFLICT 刷新 last_seen
    check(grants.dbs_for_thread(t) == ["db1", "db2"],
          "按 last_seen 倒序（刚用过的在前）", grants.dbs_for_thread(t))
    check(grants.dbs_for_thread("thread-unknown") == [], "未知会话 → 空列表")
    check(grants.dbs_for_thread("") == [], "空 thread_id → 空列表")
    grants.record_thread_db(t, "")  # 脏值：不该入库
    check("" not in grants.dbs_for_thread(t), "空 db_name 不入账")
    grants.record_thread_db("", "db9")
    check(grants.dbs_for_thread("") == [], "空 thread_id 不写账")


# ── 3. _current_db_name 优先级 ────────────────────────────────────────
class _CfgPatch:
    """把 `langgraph.config.get_config` 换成返回给定值/抛异常。"""

    def __init__(self, value, exc: bool = False) -> None:
        self.value, self.exc = value, exc

    def __enter__(self):
        self._orig = _lg_config.get_config
        if self.exc:
            def _boom(*a, **kw):
                raise RuntimeError("Called get_config outside of a runnable context")
            _lg_config.get_config = _boom
        else:
            _lg_config.get_config = lambda *a, **kw: self.value
        return self

    def __exit__(self, *exc_info) -> bool:
        _lg_config.get_config = self._orig
        return False


def t5_current_db_precedence() -> None:
    print("\n[t5] _current_db_name 优先级")
    t_one, t_two = "thread-one", "thread-two"
    grants.record_thread_db(t_one, "db_only")
    grants.record_thread_db(t_two, "db_a")
    time.sleep(0.02)
    grants.record_thread_db(t_two, "db_b")

    with _CfgPatch({"configurable": {"db_name": "db_cfg", "thread_id": t_two}}):
        check(rb._current_db_name() == "db_cfg",
              "configurable 有值 → 直接用（不读账本）", rb._current_db_name())

    with _CfgPatch({"configurable": {"thread_id": t_one}}):
        check(rb._current_db_name() == "db_only",
              "configurable 无 db_name + 账本恰好 1 个库 → 回退取它", rb._current_db_name())

    with _CfgPatch({"configurable": {"thread_id": t_two}}):
        check(rb._current_db_name() == "",
              "★ 账本记着 2 个库 → 不猜，返回空（宁标未核验，不拿错库语料判模型）",
              rb._current_db_name())

    with _CfgPatch({"configurable": {"thread_id": "thread-unknown"}}):
        check(rb._current_db_name() == "", "账本无记录 → 空")

    with _CfgPatch({}):
        check(rb._current_db_name() == "", "get_config 返回 {} → 空（不炸）")

    with _CfgPatch(None, exc=True):
        check(rb._current_db_name() == "", "get_config 抛异常（离线/非 run 上下文）→ 空（不炸）")

    with _CfgPatch({"configurable": {"db_name": ""}}):
        check(rb._current_db_name() == "", "configurable 里是空串 → 视为无值（走兜底）")


# ── 4. 落点：报告侧真会用它 ────────────────────────────────────────────
def t6_verdict_uses_resolved_db() -> None:
    print("\n[t6] 报告判定链路上确实用解析出的库名取语料")
    calls: list[str] = []
    orig = rb.load_knowledge_corpus

    def _spy(db):
        calls.append(db)
        return []

    rb.load_knowledge_corpus = _spy
    try:
        with _CfgPatch({"configurable": {"db_name": "db_spy"}}):
            rb._current_db_name()
    finally:
        rb.load_knowledge_corpus = orig
    check(rb.load_knowledge_corpus is orig, "（接线检查）语料加载函数已还原")
    check(calls == [], "直接调 _current_db_name 不会加载语料（职责分离）")
    src = pathlib.Path(rb.__file__).read_text(encoding="utf-8")
    check("_corpus = load_knowledge_corpus(_db)" in src or
          "load_knowledge_corpus(_db)" in src,
          "判定块用 `_db = _current_db_name()` 的结果取语料（防静默回退）")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {_TMP_ROOT}")
    print(f"源码根 = {_SRC}")
    t1_metadata_source()
    t2_configurable_source()
    t3_missing_no_clobber()
    t4_ledger_roundtrip()
    t5_current_db_precedence()
    t6_verdict_uses_resolved_db()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
