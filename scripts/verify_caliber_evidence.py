# -*- coding: utf-8 -*-
"""业务口径「原文证据」核验验证（离线，不需要后端/数据库/网络/LLM）。

**要关的口子**：生产报告（trace `9c81d3181f2a72f27cf9d092d7185fab`）第 2 节「业务口径」
的 `内容` 是语义层转述、`出处` 写的是 `workhour_analysis（Cube）` / `v_workhour` /
`语义库字段字典` —— **一个知识库文件都没有**；而表格下面那句「口径取自语义库知识库原文…
未做推断」是 `report_builder` **无条件写死**的 ⇒ 报告在撒谎。用户的原话是「业务口径主要
用于使用用户判断生成的答案是否准确」，假出处恰好破坏的就是这个用途。

**两个交付面**（本次都验）：
1. `agent.utils.caliber_evidence` —— 归一化 / 逐字比对 / 出处白名单 / 磁盘语料加载；
2. `agent.middlewares.caliber_gate` —— 终态答复不合规 → `after_model` 返回
   `{"jump_to": "model"}` 打回重写（≤2 次，跨重启由 state 推导计数）。

本脚本的重点是**负对照**（证明闸门真的会拦，而不是摆设）：
- 同义改写 / 只改数字 / 只换语序 / 张冠李戴（引 A 文件的话署 B 文件）→ 一律不通过；
- 省略号切出的过短分片不算证据（哪怕它确实在原文里）；
- 语料读不到 → **绝不打回**（fail-open），报告侧改标「未核验」；
- 图里真触发：不合规 → 模型被调用 **2** 次；而**去掉** `@hook_config(can_jump_to=…)`
  的同位置变体 → 只调用 **1** 次（`jump_to` 被静默丢弃）⇒ 证明那行装饰器是承重的。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_caliber_evidence.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

# 本脚本要建真图（t10）⇒ langchain 会尝试把 trace 发到 LangSmith（实测 403、
# 且**是外网请求**）。离线验证脚本不许有出网副作用，进入 langchain 之前先关掉。
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING"] = "false"

# 语料加载走 wren 项目目录解析，本脚本全程 monkeypatch 掉真实解析，不碰任何生产路径。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-caliber-evidence-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import AgentMiddleware  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from agent.middlewares import caliber_gate as cg  # noqa: E402
from agent.utils import caliber_evidence as ce  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


# ── fixtures：一份磁盘知识库（临时目录，绝不碰真实工作区）─────────────────
_RULES_MD = """# 报工与工时口径

## R1 工时单位
`work_hour` 单位为小时，保留两位小数。

## R3 报工记录有效性
只有 `if_approve = 1` 的 `work_hour` 才计入有效工时；`if_approve = 0` 的报工记录为未审核，不计入。
有效工时口径：`if_approve = 1` 的 `work_hour` 之和。
"""
_METRICS_MD = """# 指标定义

## 人均工时
人均工时 = 有效工时合计 / 在职人数。
在职人数以 `hr_employee.status = 'active'` 为准。
"""
_GLOSSARY_MD = """# 术语表

## 有效工时
指经审批通过的报工工时。
"""


def _make_project(root: pathlib.Path) -> pathlib.Path:
    """造一份 wren 项目目录（只用到 `knowledge/**`，其余为空即可）。"""
    kb = root / "knowledge"
    for sub in ce.KNOWLEDGE_DIRS:
        (kb / sub).mkdir(parents=True, exist_ok=True)
    (kb / "rules" / "报工与工时.md").write_text(_RULES_MD, encoding="utf-8")
    (kb / "rules" / "空文件.md").write_text("   \n", encoding="utf-8")  # 空文件须被跳过
    (kb / "metrics" / "指标定义.md").write_text(_METRICS_MD, encoding="utf-8")
    (kb / "glossary" / "术语表.md").write_text(_GLOSSARY_MD, encoding="utf-8")
    # sql/ 与 caveats/ 故意留空目录 → 必须被安静跳过，不影响其余
    return root


_PROJECT = _make_project(pathlib.Path(_TMP_ROOT) / "proj")


def _patch_corpus(project: pathlib.Path | None) -> None:
    """把 wren 项目解析钉到 fixture（None = 模拟「解析不到项目」）。

    同时钉住闸门侧的库名 —— 不在真 run 里 `get_config()` 取不到 `configurable.db_name`，
    不钉的话闸门永远拿到空库名 ⇒ 语料恒为空 ⇒ 永远不打回，t9/t10 会变成**假通过**。
    """
    import agent.utils.wren_call_extract as wce

    wce.resolve_wren_ctx_by_db = lambda db: ((project, None) if project else (None, None))
    cg._current_db_name = lambda: ("testdb" if project else "")
    ce._CORPUS_CACHE.clear()  # 语料缓存是按库名 + 60s TTL，测试间必须清


def _corpus() -> list[ce.EvidenceFile]:
    _patch_corpus(_PROJECT)
    files = ce.load_knowledge_corpus("testdb")
    if not files:
        raise RuntimeError("fixture 语料加载失败，后续断言无意义")
    return files


RULES = "rules/报工与工时.md"
METRICS = "metrics/指标定义.md"


def _verdict(entry: str, files: list[ce.EvidenceFile]) -> ce.CaliberVerdict:
    out = ce.verify_caliber_entries([entry], files)
    if len(out) != 1:
        raise RuntimeError(f"verify 应返回 1 条结论，实得 {len(out)}")
    return out[0]


# ── t1 归一化 ─────────────────────────────────────────────────────────
def t1_normalize() -> None:
    print("\n[t1] 归一化：该抹平的抹平，不该抹的绝不抹")
    n = ce.normalize_for_match
    check(n("if_approve = 1") == n("if_approve=1"), "空白差异抹平（= 两侧空格）")
    check(n("（Cube）") == n("(Cube)"), "全角括号 → 半角（NFKC）")
    check(n("`work_hour`") == n("work_hour"), "markdown 反引号抹平")
    check(n("**有效工时**") == n("有效工时"), "markdown 强调符抹平")
    check(n("a\\|b") == n("a|b"), "渲染期转义的反斜杠竖线还原")
    check(n("Work_Hour") == n("work_hour"), "英文大小写抹平（casefold）")
    # 负对照：下划线是标识符的一部分，去掉会造出假匹配
    check(n("work_hour") != n("workhour"), "★ 下划线**不**抹平（work_hour ≠ workhour）")
    check(n("work_hour") != n("work hour"), "★ 下划线不抹平 ⇒ 也拦得住空格写法混入")


# ── t2 逐字比对（正例）────────────────────────────────────────────────
def t2_verbatim_positive() -> None:
    print("\n[t2] 逐字比对正例：原样摘 / 带格式差异 / 带省略号")
    files = _corpus()

    v = _verdict(f"有效工时 | `if_approve = 1` 的 `work_hour` 之和 | {RULES} R3", files)
    check(v.ok, "从原文原样摘 → 通过", v.reason)
    check(v.source_ok and v.content_ok, "出处与内容两项各自也判通过")

    v = _verdict(f"有效工时 | if_approve=1的work_hour之和 | {RULES}", files)
    check(v.ok, "格式差异（去空格/反引号/条目号）仍通过", v.reason)

    v = _verdict(f"有效工时 | 只有if_approve=1的work_hour才计入有效工时…报工记录为未审核，不计入 | {RULES}", files)
    check(v.ok, "带 `…` 省略中段（两段各 ≥8 字）→ 通过", v.reason)
    check(v.reason == "segmented", "命中路径标为 segmented", v.reason)


# ── t3 逐字比对（负对照：这才是闸门的价值）──────────────────────────────
def t3_verbatim_negative() -> None:
    print("\n[t3] 逐字比对负对照：改写 / 改数字 / 换语序 / 张冠李戴")
    files = _corpus()

    v = _verdict(f"有效工时 | 审批通过的work_hour之和 | {RULES}", files)
    check(not v.ok and v.reason == "content_not_verbatim", "★ 同义改写（计入→审批通过）→ 不通过", v.reason)

    v = _verdict(f"有效工时 | if_approve = 2 的 work_hour 才计入有效工时 | {RULES}", files)
    check(not v.ok and v.reason == "content_not_verbatim", "★ 只改了一个数字 → 不通过", v.reason)

    v = _verdict(f"有效工时 | work_hour 的 if_approve = 1 才计入有效工时 | {RULES}", files)
    check(not v.ok, "★ 只换了语序 → 不通过", v.reason)

    # 张冠李戴：内容确实在语料里，但**不在**署名的那份文件里
    v = _verdict(f"人均工时 | 人均工时 = 有效工时合计 / 在职人数 | {RULES}", files)
    check(not v.ok and v.reason == "content_not_verbatim",
          "★ 张冠李戴（metrics 的话署 rules 的出处）→ 不通过", v.reason)
    check(v.source_ok, "…但出处本身是真的（失败点确实在内容）")
    # 反向：不署名时同样的内容在 metrics 里找得到 ⇒ 证明上一条不是因为「语料里没这句」
    v = _verdict(f"人均工时 | 人均工时 = 有效工时合计 / 在职人数 | {METRICS}", files)
    check(v.ok, "同一句话署对文件 → 通过（反证上一条是作用域判定，不是找不到）", v.reason)

    v = _verdict(f"有效工时 | 只有if_approve=1的work_hour才计入有效工时…不计入 | {RULES}", files)
    check(not v.ok and v.reason == "ellipsis_fragment_too_short",
          "★ 省略号切出 `不计入`（3 字）→ 过短片段不算证据", v.reason)


# ── t4 短分片防放过 ───────────────────────────────────────────────────
def t4_short_segments() -> None:
    print("\n[t4] 短分片防放过：碰巧命中不算证据")
    files = _corpus()
    rules = next(f for f in files if f.rel_path == RULES)

    # 先证明这两个短词**确实**逐字在原文里 —— 否则下面的「不通过」可能只是因为找不到，
    # 那样的负对照是假的、证明不了任何事。
    check("未审核" in rules.raw_text and "不计入" in rules.raw_text,
          "前提：`未审核`/`不计入` 确实逐字在原文里")
    v = _verdict(f"有效性 | 未审核…不计入 | {RULES}", files)
    check(not v.ok and v.reason == "ellipsis_fragment_too_short",
          "★ 两段都真的在原文里，但都 <8 字 → 仍不通过（防「短词拼凑」）", v.reason)
    check(v.content == "未审核…不计入", "被拒的是内容而非出处", v.reason)

    # 已接受并写明的放宽：**单片段**短词只要逐字为真就通过（弱，但不假）
    v = _verdict(f"有效性 | 未审核 | {RULES}", files)
    check(v.ok, "单片段短词逐字为真 → 通过（已知放宽：弱≠假，见 _match_content docstring）", v.reason)


# ── t5 出处白名单 ─────────────────────────────────────────────────────
def t5_source() -> None:
    print("\n[t5] 出处：知识库文件名 vs 库对象")
    files = _corpus()
    content = "`if_approve = 1` 的 `work_hour` 之和"

    for src in (
        f"{RULES} R3",
        f"knowledge/{RULES}",
        "报工与工时.md",
        f"`{RULES}` R3",
    ):
        v = _verdict(f"有效工时 | {content} | {src}", files)
        check(v.ok, f"可信出处：{src}", v.reason)

    for src in ("workhour_analysis（Cube）", "v_workhour", "语义库字段字典", "MDL", "report/x.md"):
        v = _verdict(f"有效工时 | {content} | {src}", files)
        check(not v.ok and v.reason == "source_not_kb_file", f"★ 不可信出处：{src}", v.reason)
    v = _verdict(f"有效工时 | {content} | workhour_analysis（Cube）", files)
    check("workhour_analysis" in v.reason_human and "v_" in v.reason_human,
          "给模型的诊断点名禁写形态（表名/视图名/Cube 名）", v.reason_human[:40])

    # 出处里出现多个 .md：**每一个**都必须真实，防「A 或 B」式模糊写法蒙过
    v = _verdict(f"有效工时 | {content} | {RULES}, {METRICS}", files)
    check(v.ok, "两个真文件（逗号分隔）→ 通过", v.reason)
    v = _verdict(f"有效工时 | {content} | {RULES}, report/x.md", files)
    check(not v.ok and v.reason == "source_not_kb_file",
          "★ 两个出处里有一个是假的 → 整条不通过", v.reason)
    # 中文连接词会把两个路径**粘成一个 token**（`_SOURCE_MD_RE` 的字符类含 CJK，
    # 见其注释）⇒ 解析不出已存在的文件 ⇒ fail-closed 打回。宁可让模型改成单一名，
    # 也不猜「它想引哪个」。
    v = _verdict(f"有效工时 | {content} | {RULES} 或 {METRICS}", files)
    check(not v.ok and v.reason == "source_not_kb_file",
          "★ 中文连接词粘连成一段 → fail-closed（不猜它想引哪个文件）", v.reason)

    # 出处错但内容确在某个文件里：诊断要点出「其实在哪」
    v = _verdict(f"人均工时 | 人均工时 = 有效工时合计 / 在职人数 | v_workhour", files)
    check(v.closest == METRICS, "出处错时指出内容其实在哪个文件", v.closest)
    check("最接近" not in v.reason_human,
          "★ 原文片段**不**进 reason_human（它会被贴给模型，等于注入它未必取过的料）")


# ── t6 口径目录不许漂移 ───────────────────────────────────────────────
def t6_dirs() -> None:
    print("\n[t6] 知识库子目录集合与 api.wren_semantic 对齐（防静默漂移）")
    import api.wren_semantic as ws

    check(set(ce.KNOWLEDGE_DIRS) == set(ws._KNOWLEDGE_CATEGORIES.values()),
          "★ ce.KNOWLEDGE_DIRS == api.wren_semantic._KNOWLEDGE_CATEGORIES 的值集",
          f"{sorted(ce.KNOWLEDGE_DIRS)} vs {sorted(ws._KNOWLEDGE_CATEGORIES.values())}")
    check("rules" in ce.KNOWLEDGE_DIRS and "metrics" in ce.KNOWLEDGE_DIRS,
          "口径主力目录（rules/metrics）在内")


# ── t7 语料加载 ───────────────────────────────────────────────────────
def t7_corpus() -> None:
    print("\n[t7] 语料加载：只收知识库 md、跳过空文件、失败 fail-open")
    files = _corpus()
    rels = sorted(f.rel_path for f in files)
    check(rels == sorted([RULES, METRICS, "glossary/术语表.md"]),
          "★ 只收 knowledge/{rules,metrics,glossary,caveats,sql}/*.md", rels)
    check(all(f.name and f.raw_text and f.norm_text for f in files), "每份文件都带名/原文/归一化文本")
    check("空文件.md" not in [f.name for f in files], "空白文件被跳过")

    _patch_corpus(None)
    check(ce.load_knowledge_corpus("testdb") == [], "★ 解析不到项目 → 空语料（fail-open）")
    check(ce.load_knowledge_corpus("") == [], "空库名 → 空语料（不误命中别的库）")

    _patch_corpus(_PROJECT)
    import agent.utils.wren_call_extract as wce

    def _boom(db):
        raise RuntimeError("解析炸了")

    wce.resolve_wren_ctx_by_db = _boom
    ce._CORPUS_CACHE.clear()
    check(ce.load_knowledge_corpus("testdb") == [], "★ 解析抛异常 → 空语料（不把报告/闸门拖挂）")

    # 缓存：同一库第二次不再解析（TTL 内）
    calls = {"n": 0}

    def _count(db):
        calls["n"] += 1
        return (_PROJECT, None)

    wce.resolve_wren_ctx_by_db = _count
    ce._CORPUS_CACHE.clear()
    ce.load_knowledge_corpus("testdb")
    ce.load_knowledge_corpus("testdb")
    check(calls["n"] == 1, "TTL 内命中缓存（不反复读盘）", calls["n"])


# ── t8 语料缺失 fail-open（关键负对照）─────────────────────────────────
def t8_fail_open() -> None:
    print("\n[t8] 语料缺失：核验侧宁可说「无法核验」，闸门侧绝不打回")
    entry = f"有效工时 | 只有if_approve=1的work_hour才计入有效工时 | {RULES}"
    out = ce.verify_caliber_entries([entry], [])
    check(len(out) == 1 and not out[0].ok, "空语料 → 一律 ok=False（方向安全：不谎称通过）")
    check(out[0].reason == "corpus_empty", "空语料 → 机器码 corpus_empty", out[0].reason)

    _patch_corpus(None)
    st = {"messages": [HumanMessage("问题"), AIMessage(entry_wrap(entry))]}
    check(cg.CaliberGateMiddleware()._after(st, None) is None,
          "★ 读不到语料 → 不打回（模型不该为系统读不到料背锅）")
    _patch_corpus(_PROJECT)


def entry_wrap(entry: str) -> str:
    return f"结论在此。\n\n## 业务口径\n- {entry}\n"


# ── t9 终态判定 + 打回预算 ────────────────────────────────────────────
def t9_gate() -> None:
    print("\n[t9] 闸门判定：只有「终态 + 有块 + 不过 + 预算未尽」才打回")
    _patch_corpus(_PROJECT)
    good = f"有效工时 | `if_approve = 1` 的 `work_hour` 之和 | {RULES} R3"
    bad = f"有效工时 | 审批通过的work_hour之和 | {RULES} R3"
    mw = cg.CaliberGateMiddleware()

    def st(*msgs):
        return {"messages": list(msgs)}

    check(mw._after(st(HumanMessage("q")), None) is None, "空/无 AI 消息 → 不判")
    check(mw._after(st(HumanMessage("q"), AIMessage(content="", tool_calls=[
        {"name": "wrenai_x_run_sql", "args": {}, "id": "c1"}], )), None) is None,
        "★ 末条 AI 有 tool_calls（非终态）→ 不判（防空转的关键）")
    check(mw._after(st(HumanMessage("q"), AIMessage("[系统自动通知] 别的")), None) is None,
          "系统注入消息 → 不判（避免自我循环）")
    check(mw._after(st(HumanMessage("q"), AIMessage("请问您指的是哪个部门？[需要澄清]")), None) is None,
          "澄清轮 → 不判（追问是终态但不该被口径闸打断）")
    check(mw._after(st(HumanMessage("q"), AIMessage("结论如下，没有口径块。")), None) is None,
          "★ 无块且本轮没取过知识料 → 不判（纯明细列举允许不写块）")
    check(mw._after(st(HumanMessage("q"), AIMessage(entry_wrap(good))), None) is None,
          "全部通过 → 不判")

    out = mw._after(st(HumanMessage("q"), AIMessage(entry_wrap(bad))), None)
    check(isinstance(out, dict) and out.get("jump_to") == "model",
          "★ 不合规 → 返回 jump_to=model（这是重答的唯一通道）", out and out.get("jump_to"))
    check(len(out["messages"]) == 1 and isinstance(out["messages"][0], HumanMessage),
          "同时注入一条 HumanMessage 纠正提示")
    corr = out["messages"][0].content
    check(corr.startswith(cg._RETRY_TAG), "纠正消息用仓内既有 [系统自动通知] 前缀", corr[:30])
    check("重发完整最终答复" in corr and "已修正" in corr,
          "★ 明确要求「重发完整答复」而非短句（短答复会被结果摘要当收尾语丢掉）")
    check("逐字" in corr and "…" in corr, "讲清「逐字 + 可 `…` 省略」")
    check("if_approve" not in corr,
          "★ 纠正消息里**不**夹带原文片段（贴过去等于注入模型本轮未必取过的料）")
    check("有效工时" in corr, "…但要点名是哪一条不过，模型才知道改哪")

    # 预算：本轮已注入 2 条 → 不再打回
    two = st(
        HumanMessage("q"),
        AIMessage(entry_wrap(bad)),
        HumanMessage(cg._RETRY_TAG + " 第 1 次"),
        AIMessage(entry_wrap(bad)),
        HumanMessage(cg._RETRY_TAG + " 第 2 次"),
        AIMessage(entry_wrap(bad)),
    )
    check(cg._retries_in_turn(two["messages"]) == 2, "本轮已注入 2 次（从 state 推导）")
    check(mw._after(two, None) is None, "★ 预算耗尽（≥2）→ 不再打回，交给报告侧如实标注")

    # 预算只算本轮：上一轮的注入消息不能吃掉本轮的额度（跨问题不串）
    prev = st(
        HumanMessage("上一问"),
        HumanMessage(cg._RETRY_TAG + " 上一轮第 1 次"),
        AIMessage(entry_wrap(bad)),
        HumanMessage("这一问"),
        AIMessage(entry_wrap(bad)),
    )
    check(cg._retries_in_turn(prev["messages"]) == 0, "★ 计数只算最后一条真实用户消息之后（跨问题不串）")
    check(isinstance(mw._after(prev, None), dict), "上一轮的注入不消耗本轮的额度")

    check(cg._MAX_RETRIES == 2, f"默认上限 2（env CALIBER_EVIDENCE_MAX_RETRIES 可调）", cg._MAX_RETRIES)
    check(ce.is_caliber_evidence_tool("wrenai_witops_get_instructions"),
          "取料工具识别带 wrenai_<库名>_ 前缀")
    check(not ce.is_caliber_evidence_tool("dbmcp_run_sql"), "数据工具不算取料")


# ── t9b 整节缺失分支（2026-09-26 生产 trace f222a8a5… 逼出来的）──────────
def t9b_missing_block() -> None:
    print("\n[t9b] 整节缺失：取过知识料却没写块 → 打回 1 次（给两条出路），不追过头")
    _patch_corpus(_PROJECT)
    mw = cg.CaliberGateMiddleware()

    def took_pill(msgs):
        """在 msgs 后面补一轮「调过取料工具」的痕迹（AI 带 tool_calls + 结果 + 终稿）。"""
        return {
            "messages": list(msgs) + [
                AIMessage(content="", tool_calls=[{
                    "name": "wrenai_witops_get_instructions", "args": {}, "id": "c1"}]),
                ToolMessage(content="R1 应报工人员池…", tool_call_id="c1"),
            ]
        }

    def said(text):
        return {"messages": list(took_pill([])["messages"]) + [AIMessage(content=text)]}

    plain = "## 查询结果：本月工时\n\n| 指标 | 值 |\n|---|---|\n| 合计 | 120 |\n"
    out = mw._after(said(plain), None)
    check(isinstance(out, dict) and out.get("jump_to") == "model",
          "★ 取过知识料但终稿无块 → jump_to=model 打回", out and out.get("jump_to"))
    corr = out["messages"][0].content if isinstance(out, dict) else ""
    check(corr.startswith(cg._MISSING_TAG), "缺块纠正用独立标签（预算与「不合规」分开算）", corr[:30])
    check("重发完整最终答复" in corr, "★ 同样要求重发完整答复（短答复会被摘要丢弃）")
    check("`## 业务口径`" in corr, "点名要补的块标题（逐字）")
    check("不必也" in corr and "目录前缀" in corr,
          "★ 明说「照抄裸文件名、不必自己拼 rules/ 前缀」——契约过去只给带前缀的例子，"
          "而 get_instructions 返回里文件名是裸的 ⇒ 模型不敢写（生产实证）")
    check("本次未依据知识库口径" in corr, "给出 B 出路：确实没用到就明确声明")

    # 出口一：模型已声明「确实没用口径」→ 放行（循环终点）
    for mark in cg._NO_CALIBER_MARKS:
        check(mw._after(said(plain + f"\n{mark}\n"), None) is None,
              f"★ 声明「{mark}」→ 放行（明确声明的省略是契约允许的）")

    # 出口二：本轮没取过料 → 无从要求
    check(mw._after({"messages": [HumanMessage("q"), AIMessage(plain)]}, None) is None,
          "★ 本轮没取过知识料（纯明细/直连通道）→ 不追")

    # 出口三：预算 1 次，第二次仍缺就认了
    twice = {"messages": list(said(plain)["messages"]) + [
        HumanMessage(cg._MISSING_TAG + " 第 1 次")] + [AIMessage(content=plain)]}
    check(cg._retries_in_turn(twice["messages"], cg._MISSING_TAG) == 1, "缺块计数从 state 推导")
    check(mw._after(twice, None) is None, "★ 缺块已打回 1 次仍缺 → 放行（不无限追）")
    check(cg._MAX_MISSING_RETRIES == 1, "缺块默认上限 1（env CALIBER_MISSING_MAX_RETRIES 可调）",
          cg._MAX_MISSING_RETRIES)

    # 两类预算互不占用：「缺块打回 1 次 → 补出的块出处不合格」这条正常升级路径必须还能打回
    mixed = {"messages": list(said(plain)["messages"]) + [
        HumanMessage(cg._MISSING_TAG + " 第 1 次"),
        AIMessage(content=entry_wrap("有效工时 | 审批通过的work_hour之和 | 报工与工时.md R3")),
    ]}
    check(mw._after(mixed, None) is not None,
          "★ 缺块额度用完不影响「不合规」额度的正常升级路径（两类分开计数）")

    # 上一轮取过料，不能成为本轮索要口径块的依据
    cross = {"messages": [
        HumanMessage("上一问"),
        AIMessage(content="", tool_calls=[{
            "name": "wrenai_witops_get_instructions", "args": {}, "id": "c0"}]),
        ToolMessage(content="R1…", tool_call_id="c0"),
        AIMessage(content="上一问的答复"),
        HumanMessage("这一问"),
        AIMessage(content=plain),
    ]}
    check(mw._after(cross, None) is None, "★ 只有**本轮**取过料才追（跨问题不串）")

    # 有块时走的是「不合规」分支，不会误用缺块预算
    check(cg._retries_in_turn(mixed["messages"]) == 0, "缺块标签不计入「不合规」计数")


# ── t10 图里真触发（含负对照：证明 can_jump_to 是承重的）─────────────────
class _ScriptedModel(BaseChatModel):
    """按脚本逐次回话的假模型（只数调用次数 + 返回预置文本）。"""

    replies: list = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # create_agent 会调
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        i = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.replies[i]))])


@tool
def _noop_tool(x: str = "") -> str:
    """占位工具：只为让图带上 tools 节点（**生产同形** —— 无 tools 时 `loop_exit_node`
    走的是另一条建边分支，位置效应测不出来）。模型从不调用它。"""
    return "ok"


class _NoOpAfterModel(AgentMiddleware):
    """占住 `middleware_w_after_model[0]`（= `loop_exit_node`），复现 deepagents 把
    `TodoListMiddleware` 恒定放首位 ⇒ 用户中间件永远不是 `[0]` 的情形。"""

    def after_model(self, state, runtime):
        return None


class _NoDecoGate(cg.CaliberGateMiddleware):
    """同位置、同逻辑，**只**去掉 `@hook_config(can_jump_to=["model"])` —— 负对照。

    ⚠️ 两个 hook 都要覆盖：`_get_can_jump_to`（`factory.py:493-525`）先看同步
    `after_model`、**再回退看 `aafter_model`**；只覆盖同步那只，基类被装饰过的
    `aafter_model` 仍会供出 `can_jump_to` ⇒ 负对照会**假通过**（第一版就是这么骗过我的）。
    """

    def after_model(self, state, runtime):  # 无装饰器 ⇒ 无 __can_jump_to__
        return self._after(state, runtime)

    async def aafter_model(self, state, runtime):  # 同上，必须一起摘掉
        return self._after(state, runtime)


def t10_graph() -> None:
    print("\n[t10] 真图触发：不合规 → 模型被调 2 次；去掉 can_jump_to → 只调 1 次")
    _patch_corpus(_PROJECT)
    good = f"有效工时 | `if_approve = 1` 的 `work_hour` 之和 | {RULES} R3"
    bad = f"有效工时 | 审批通过的work_hour之和 | {RULES} R3"

    def run(middleware):
        m = _ScriptedModel(replies=[entry_wrap(bad), entry_wrap(good)])
        app = create_agent(model=m, tools=[_noop_tool], middleware=middleware)
        return m, app.invoke({"messages": [HumanMessage("本月工时口径是什么")]})

    m, out = run([_NoOpAfterModel(), cg.CaliberGateMiddleware()])
    check(m.calls == 2, "★ 不合规 → 打回一次后重答（模型被调 2 次）", f"calls={m.calls}")
    check(isinstance(out["messages"][-1], AIMessage) and "有效工时" in str(out["messages"][-1].content),
          "最终答复是模型重写后的那一版（不是纠正提示本身）")

    m, out = run([_NoOpAfterModel(), _NoDecoGate()])
    check(m.calls == 1, "★ 负对照：去掉 can_jump_to ⇒ jump_to 被静默丢弃、只调 1 次", f"calls={m.calls}")
    check(isinstance(out["messages"][-1], HumanMessage) and cg._RETRY_TAG in out["messages"][-1].content,
          "…后果：纠正提示变成末条消息污染尾部（正是要避免的形态）")

    # 位置效应：`[0]` = `loop_exit_node` 时，出边是 `_make_model_to_tools_edge`（无条件读
    # `jump_to`）⇒ **不写装饰器也能跳**。这正是「装饰器是硬要求」的**唯一**原因 ——
    # 我们永远不是 `[0]`。没有这条，负对照只是「某处不加装饰器就坏」，证明不了位置假设。
    m, out = run([_NoDecoGate()])
    check(m.calls == 2, "★ 同代码放 `[0]`（loop_exit_node）→ 不写装饰器也能跳（位置效应成立）",
          f"calls={m.calls}")

    m, out = run([cg.CaliberGateMiddleware()])
    check(m.calls == 2, "带装饰器放 `[0]` → 照常打回（不依赖位置侥幸）", f"calls={m.calls}")

    # 合规答案：一次就结束，证明闸门不会误伤
    m = _ScriptedModel(replies=[entry_wrap(good)])
    app = create_agent(model=m, tools=[_noop_tool], middleware=[_NoOpAfterModel(), cg.CaliberGateMiddleware()])
    app.invoke({"messages": [HumanMessage("本月工时口径是什么")]})
    check(m.calls == 1, "★ 合规答案零打扰（只调 1 次，不误杀）", f"calls={m.calls}")


def t11_wiring() -> None:
    print("\n[t11] 接线不变量：装饰器与挂载位置（防「顺手清理」把闸门清掉）")
    # ★ 最强的一条：装饰器直接查类方法上的元数据 —— 被删掉时这里立刻红。
    #   两个 hook 都要查：`_get_can_jump_to` 同步那只没有时会**回退看异步那只**，
    #   只保住一个也能跑，但语义上另一个就成了哑炮。
    for hook in ("after_model", "aafter_model"):
        fn = getattr(cg.CaliberGateMiddleware, hook)
        check(getattr(fn, "__can_jump_to__", None) == ["model"],
              f"★ {hook} 带 @hook_config(can_jump_to=['model'])",
              getattr(fn, "__can_jump_to__", None))

    # 挂载位置：`after_model` 链 index 越大越先跑 ⇒ 要「跑得更晚」（等 ProgressBoundary
    # 推进完 todos）就必须排在它**前面**。AST 读源码，避免为了断言把整个 graph 模块
    # import 起来（那会拉起 MCP server）。
    import ast

    src = (_SRC / "agent" / "graphs" / "nl2sql_agent.py").read_text(encoding="utf-8")
    order: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_middleware" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            for el in node.value.elts:
                if isinstance(el, ast.Call):
                    name = getattr(el.func, "id", None) or getattr(el.func, "attr", None)
                    if name:
                        order.append(name)
    check(order, "在 nl2sql_agent.py 里定位到 _middleware 列表字面量", order)
    check("CaliberGateMiddleware" in order, "CaliberGateMiddleware 已挂载")
    check("ProgressBoundaryMiddleware" in order, "ProgressBoundaryMiddleware 仍在")
    if "CaliberGateMiddleware" in order and "ProgressBoundaryMiddleware" in order:
        check(order.index("CaliberGateMiddleware") < order.index("ProgressBoundaryMiddleware"),
              "★ 排在 ProgressBoundary **之前**（index 更小 ⇒ after_model 跑得更晚，"
              "先推进完 todos 再打回）")


# ── t12 §1 剥离重复口径块（P2）─────────────────────────────────────────
def t12_strip_block() -> None:
    print("\n[t12] strip_caliber_block：摘掉最后一个口径节，其余原样保留")
    s = ce.strip_caliber_block

    out = s("数据正文\n\n## 业务口径\n- a | b | rules/x.md\n\n## 附注\n尾部说明\n")
    check("业务口径" not in out, "块被摘掉")
    check("数据正文" in out and "## 附注" in out and "尾部说明" in out,
          "块**后面**的节与正文都保留（只摘块，不摘到文末）", repr(out[-20:]))

    out2 = s("数据正文\n\n## 业务口径\n- a | b | rules/x.md\n")
    check(out2.strip() == "数据正文", "块在末尾 → 摘掉后只剩正文", repr(out2))

    check(s("数据正文\n\n## 2. 明细\n| a | b |\n") == "数据正文\n\n## 2. 明细\n| a | b |\n",
          "无口径块 → 逐字原样返回（关键负对照：别误伤正文）")
    check(s("") == "" and s(None) == "", "空输入/None → 空串（不炸）")

    two = ("数据\n\n## 业务口径\n- 第一块 | x | rules/a.md\n\n中间\n\n"
           "## 业务口径\n- 第二块 | y | rules/b.md\n")
    out5 = s(two)
    check("第一块" in out5 and "第二块" not in out5, "只摘最后一个（先前那块留给渲染器优先取）")
    check(s("**业务口径**\n- 加粗写法 | x | rules/a.md\n") .strip() == "",
          "加粗写法的块也认（与 CALIBER_HEADING_RE 同口径）")


def main() -> int:
    print(f"临时数据根 = {_TMP_ROOT}")
    print(f"源码根     = {_SRC}")
    t1_normalize()
    t2_verbatim_positive()
    t3_verbatim_negative()
    t4_short_segments()
    t5_source()
    t6_dirs()
    t7_corpus()
    t8_fail_open()
    t9_gate()
    t9b_missing_block()
    t10_graph()
    t11_wiring()
    t12_strip_block()
    total = len(results)
    bad = sum(1 for ok, _ in results if not ok)
    print("\n" + "=" * 60)
    print(f"{total - bad}/{total} 通过" + (f"，{bad} 条失败：" if bad else ""))
    for ok, label in results:
        if not ok:
            print(f"  {NG} {label}")
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
