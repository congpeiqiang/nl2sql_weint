# -*- coding: utf-8 -*-
"""manifest 三个身份字段（question_id / user_question / db_name）的取回契约验证（离线）。

**要关的口子**（2026-09-29 生产实证，会话 `01a0eb59` / trace `dcce39a0`）：
77 份 `nl2sql_process_data/*/_manifest/manifest-*.json` 里，`question_id` 与
`user_question` **77/77 全空**，`db_name` **64 填 / 13 空**。三条不同成因：

  ① `user_question`：manifest 只由**续跑 run** 写，而那条 run 的 `input.messages` 是以
     `[系统通知]` 开头的合成消息 —— `langfuse_metadata._extract_question_summary` 有意
     跳过 `[系统` 前缀 ⇒ 该 run 的 metadata 里**结构性没有**这个键；
  ② `question_id`：`langfuse_span._question_id()` 的 1/2 级只在子 run 注入、3 级（OTel
     活跃 span）在主 run 链路上拿不到 ⇒ 恒空；`process_audit._stem()` 因此退化成只用
     `sub_thread_id[:8]`，同会话多问题的审计件**无法按问题区分**（实测 77 份文件名全
     是单段）；
  ③ `db_name`：续跑 run 的 db_name 由 `_run_context_config` 从「上一个 run」捞，某一环
     断链就顺着续跑链**传染整个会话**（实测 `01a0e6a0` 3/3 空）。`report_builder` 早已
     有账本兜底（09-26 trace `eaf1c8b2`），`check_progress` 一行都没有。
     ⚠️ 这个空值不止是审计字段缺失：它直接喂 `caliber_sql_warning(sql, "")`，而
     `lookup_spec("")` 返回 None ⇒ **S3-2 口径护栏静默失效**。

修法：`_question_identity(thread_id)` —— metadata 优先，缺则查 `trace_bind` 的
`task_trace`（键就是子任务 id，值是该问题主 run 的 trace id + 问题原文）；`_current_db_name()`
抄 `report_builder` 的 `dbs_for_thread` 账本兜底（**恰好一个库才采用**）。

本脚本断言：metadata 优先（含「绑定值不覆盖 metadata」负对照）、`trace_bind` 兜底的精确/
前缀命中与「只缺一个就只补一个」、五条 fail-open 负对照、db_name 四条分支（含「多库不猜」）、
**落盘端到端**（qid 填上后文件名恢复 `{qid8}-{sub8}` 契约 + 旧命名负对照）、以及接线闸
（两个调用点都走 `_question_identity`，源码里不再有裸 `question_id=_question_id()`）。

⚠️ 只校验**本地代码**：线上跑的是发版镜像里的 `src`，生产对照必须 `docker exec md5sum`。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_manifest_identity.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import sys
import tempfile
from contextlib import contextmanager
from unittest import mock

# ⚠️ 本脚本会真写 auth.sqlite 与 trace_bind.sqlite ⇒ **必须强制**钉到临时根
# （不像别的套件用 setdefault：外泄到真实数据根会污染生产账本）。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-manifestid-")
os.environ["AGENT_DATA_ROOT"] = _TMP_ROOT

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.auth.grants import record_thread_db  # noqa: E402
from agent.subagents import check_progress as cp  # noqa: E402
from agent.subagents.check_progress import (  # noqa: E402
    _current_db_name,
    _question_identity,
    _session_thread_id,
)
from agent.trace import trace_bind_store as tbs  # noqa: E402
from agent.utils.process_audit import _stem, write_process_artifacts  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


@contextmanager
def cfg(metadata: dict | None = None, configurable: dict | None = None):
    """把 `langgraph.config.get_config` 换成给定 config（各读值函数都在函数内 import）。"""
    c = {"metadata": metadata or {}, "configurable": configurable or {}}
    with mock.patch("langgraph.config.get_config", return_value=c):
        yield c


@contextmanager
def cfg_raises():
    with mock.patch("langgraph.config.get_config", side_effect=RuntimeError("no ctx")):
        yield


# ══════════════════════════════════════════════════════════════════════
# 真实 trace_bind 库（不用假对象：`get_task` 的返回形状正是被测代码依赖的东西）
# ══════════════════════════════════════════════════════════════════════
SUB_FULL = "01a0eb5e-5fb0-79d3-96fc-88f22b885ff9"
SUB_SHORT = "01a0eb5e"
QID_REAL = "dcce39a0c68eaa5011106905a6af2099"
Q_REAL = "重新查一遍，对比这个结果是否相同"


def store():
    tbs._reset_store_for_test(None)
    return tbs.get_store()


def s0_fixture_sanity() -> None:
    section("⓿ 夹具自检（钉根 / 真实库形状 / patch 生效）")
    check(_TMP_ROOT in os.environ["AGENT_DATA_ROOT"],
          "AGENT_DATA_ROOT 已钉临时根（不碰真实账本）", _TMP_ROOT)
    st = store()
    st.set_task(task_id=SUB_FULL, main_thread_id="01a0eb59-fa4b-76b1-bb60-eeb38365f4ef",
                trace_id=QID_REAL, root_obs_id="abc123", question=Q_REAL, description="【任务目标】…")
    hit = st.get_task(SUB_FULL)
    with cfg(metadata={"user_question": "x", "langfuse_parent_trace_id": "y"}):
        got = _question_identity()
    check(hit is not None and hit[1][1] == QID_REAL and hit[1][3] == Q_REAL,
          "真实 set_task/get_task 形状：(key, (main, trace_id, obs, q, desc))",
          f"hit[1][1]={hit[1][1] if hit else None}")
    check(got == ("y", "x"), "get_config 被 patch 后两个读值函数确实看到它", got)


def a_metadata_first() -> None:
    section("① metadata 优先（子 run / 任何带 metadata 的上下文）")
    store().set_task(task_id=SUB_FULL, main_thread_id="m", trace_id=QID_REAL,
                     root_obs_id="", question=Q_REAL)
    with cfg(metadata={"user_question": "应报工池在职人数是多少？",
                       "langfuse_parent_trace_id": "b1123e9bbf402cdf213c2fd79836eac0"}):
        check(_question_identity(SUB_FULL)
              == ("b1123e9bbf402cdf213c2fd79836eac0", "应报工池在职人数是多少？"),
              "两个键都在 ⇒ 逐字取 metadata")
    with cfg(metadata={"user_question": "应报工池在职人数是多少？",
                       "langfuse_parent_trace_id": "b1123e9bbf402cdf213c2fd79836eac0"}):
        got = _question_identity(SUB_FULL)
    check(got[0] == "b1123e9bbf402cdf213c2fd79836eac0" and got[1] == "应报工池在职人数是多少？",
          "负对照：绑定表里有该 task 且值不同 ⇒ **不覆盖** metadata")


def b_trace_bind_fallback() -> None:
    section("② trace_bind 兜底（续跑 run 形态：metadata 无 user_question / 无 trace 键）")
    sess = "01a0eb59-fa4b-76b1-bb60-eeb38365f4ef"
    store().set_task(task_id=SUB_FULL, main_thread_id=sess,
                     trace_id=QID_REAL, root_obs_id="", question=Q_REAL)
    cont = dict(metadata={"langfuse_session_id": sess}, configurable={"thread_id": sess})
    with cfg(**cont):
        check(_question_identity(SUB_FULL) == (QID_REAL, Q_REAL),
              "续跑 run（两个键都没有）⇒ 一次查表同时补回两个字段")
    with cfg(metadata={"langfuse_session_id": sess}, configurable={"thread_id": sess}):
        check(_question_identity(SUB_SHORT) == (QID_REAL, Q_REAL),
              "短 id 前缀命中（get_task 自带前缀分支）")
    with cfg(metadata={"langfuse_session_id": sess, "user_question": "只缺 trace 键"},
             configurable={"thread_id": sess}):
        check(_question_identity(SUB_FULL) == (QID_REAL, "只缺 trace 键"),
              "只缺 question_id ⇒ 只补它，user_question 保留 metadata 原文")
    with cfg(metadata={"langfuse_session_id": sess,
                       "langfuse_parent_trace_id": "aaaabbbbccccdddd"},
             configurable={"thread_id": sess}):
        check(_question_identity(SUB_FULL) == ("aaaabbbbccccdddd", Q_REAL),
              "只缺 user_question ⇒ 只补它，question_id 保留 metadata")


def c_fail_open() -> None:
    section("③ fail-open 负对照（任一环坏掉都只退化成空串，不抛）")
    with cfg(metadata={"langfuse_session_id": "s"}, configurable={"thread_id": "s"}):
        check(_question_identity("ffffffff-0000-0000-0000-000000000000") == ("", ""),
              "负对照1：绑定表无该 task ⇒ ('', '')")
    st = store()
    st.set_task(task_id="ffffffff-1111-1111-1111-111111111111", main_thread_id="m",
                trace_id="", root_obs_id="", question="有问无 trace")
    with cfg(metadata={}, configurable={}):
        check(_question_identity("ffffffff-1111-1111-1111-111111111111") == ("", ""),
              "负对照2：行在但 trace_id 空 ⇒ 不算命中（get_task 自身判空）")
    with mock.patch.object(tbs, "get_store", side_effect=RuntimeError("boom")):
        try:
            got = _question_identity(SUB_FULL)
            ok, err = got == ("", ""), ""
        except Exception as e:  # noqa: BLE001
            got, ok, err = None, False, repr(e)
        check(ok, "负对照3：读库抛异常 ⇒ 吞掉、返回 ('', '')，不炸调用方", err or got)
    st.set_task(task_id=SUB_FULL, main_thread_id="m", trace_id=QID_REAL,
                root_obs_id="", question=Q_REAL)
    with cfg_raises():
        check(_question_identity(SUB_FULL) == (QID_REAL, Q_REAL),
              "负对照4：get_config 抛异常 ⇒ 不炸，仍走绑定表兜底")
        check(_question_identity("") == ("", ""),
              "负对照5：thread_id 空 ⇒ 不查表（metadata 也空 ⇒ 双空）")


def d_db_name() -> None:
    section("④ db_name：configurable → 会话账本（恰好一个库才采用）")

    def db_with(metadata: dict, configurable: dict) -> str:
        with cfg(metadata=metadata, configurable=configurable):
            return _current_db_name()

    T1, T2, T3 = ("th-one", "th-two", "th-three")
    record_thread_db(T1, "witops")
    record_thread_db(T2, "witops")
    record_thread_db(T2, "otherdb")
    with cfg_raises():
        check(_session_thread_id() == "", "负对照：无 config ⇒ 会话线程 id 空（兜底不误取）")
    check(db_with({}, {"thread_id": T1, "db_name": "from-config"}) == "from-config",
          "configurable 有 ⇒ 直接用它")
    check(db_with({"langfuse_session_id": T2}, {}) == "",
          "负对照：账本记着 2 个库 ⇒ **不猜**（宁可标未核验）")
    check(db_with({"langfuse_session_id": T1}, {}) == "witops",
          "configurable 无 + 账本恰好 1 个 ⇒ 采用账本值")
    check(db_with({"langfuse_session_id": T3}, {}) == "",
          "负对照：账本无记录 ⇒ 空串")
    with cfg_raises():
        check(_current_db_name() == "", "负对照：config 抛异常且拿不到会话 id ⇒ 空串")


def e_disk_contract() -> None:
    section("⑤ 落盘端到端：文件名恢复 `{qid8}-{sub8}` 契约 + 三字段逐字回读")
    check(_stem(QID_REAL, SUB_FULL) == "dcce39a0-01a0eb5e",
          "契约1：两个 id 都有 ⇒ `{qid8}-{sub8}`", _stem(QID_REAL, SUB_FULL))
    check(_stem("", SUB_FULL) == SUB_SHORT,
          "契约2（负对照）：question_id 空 ⇒ 退化成单段（就是线上 77 份的形态）")
    sess = "01a0eb59-fa4b-76b1-bb60-eeb38365f4ef"
    ptrs = write_process_artifacts(
        root=_TMP_ROOT, session_thread_id=sess, sub_thread_id=SUB_FULL, messages=[],
        question_id=QID_REAL, user_question=Q_REAL, db_name="witops",
        status="success", producing_sql="SELECT 1 AS a", result={"status": "success"},
    )
    name = f"manifest-dcce39a0-{SUB_SHORT}.json"
    check(ptrs["manifest"].endswith(name), "写出的 manifest 指针用新命名", ptrs["manifest"])
    hits = list(pathlib.Path(_TMP_ROOT).rglob(name))
    check(len(hits) == 1, "文件真的落在 `_manifest/` 且名字就是它", [str(h) for h in hits][:1])
    if hits:
        d = json.loads(hits[0].read_text(encoding="utf-8"))
        check((d.get("question_id"), d.get("user_question"), d.get("db_name"))
              == (QID_REAL, Q_REAL, "witops"),
              "manifest 三个身份字段逐字回读",
              (d.get("question_id"), d.get("user_question"), d.get("db_name")))
    ptrs2 = write_process_artifacts(
        root=_TMP_ROOT, session_thread_id=sess, sub_thread_id=SUB_FULL, messages=[],
        question_id="", user_question="", db_name="", status="error", producing_sql="",
        result={"status": "error"},
    )
    check(ptrs2["manifest"].endswith(f"manifest-{SUB_SHORT}.json"),
          "负对照：question_id 空 ⇒ 退回旧命名（历史文件与新文件不重名）", ptrs2["manifest"])


def f_wiring() -> None:
    section("⑥ 接线闸（两个调用点都走 `_question_identity`，源码级）")
    src = inspect.getsource(cp)
    check(src.count("_qid, _uq = _question_identity(thread_id)") == 2,
          "成功/失败两个落盘点都调用 `_question_identity(thread_id)`",
          src.count("_qid, _uq = _question_identity(thread_id)"))
    check("question_id=_question_id()" not in src and "user_question=_user_question()" not in src,
          "负对照：源码里不再有裸 `_question_id()` / `_user_question()` 直取")
    check("dbs_for_thread" in inspect.getsource(_current_db_name),
          "接线闸：`_current_db_name` 已接账本兜底")


# ══════════════════════════════════════════════════════════════════════
def main() -> None:
    s0_fixture_sanity()
    a_metadata_first()
    b_trace_bind_fallback()
    c_fail_open()
    d_db_name()
    e_disk_contract()
    f_wiring()

    print()
    passed = sum(1 for ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过"
          + ("" if passed == len(results) else "   ← 有失败"))
    if passed == len(results):
        print(f"（落盘根：{_TMP_ROOT}）")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
