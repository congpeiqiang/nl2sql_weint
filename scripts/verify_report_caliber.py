# -*- coding: utf-8 -*-
"""报告「业务口径」节验证（离线，无需后端/数据库/网络）。

**要关的口子**（生产 trace `294bbc0fd225de5a8def3faf4465c156`，2026-09-25 16:34，
问「丛培强 本月工时分析」）：报告里**没有业务口径**。核实下来的原因有三条，两条已
被生产日志/代码排除、一条为真：

- ❌「口径原文没进模型上下文」——**不成立**。生产日志实证 MessageSlimmer 知识类免截断
  生效：`知识类结果 wrenai_witops_get_instructions 20049 chars 免截断`。Langfuse span
  里那个 92 字符 stub 是 `langfuse_span._finish_span` 自己写的**展示用**指针（配套修了
  文案，带上原长度，避免下次再被它误导）。
- ❌「Cube 通道故障」——**不成立**，但通道确实没走成：`query_cube` 首调 `filters` 写成
  `user_name:=:丛培强`（wren 只认 `eq/neq/in/...`）报 `unknown variant` 直接失败；且全程
  `sql_only: true`（只回编译 SQL、不回数据），最终数据来自 `run_sql`。
- ✅「报告装配层缺口径节」——为真。口径节原先只存在于 **Cube 通道**（Layer A/B/C），
  而 `check_progress` 里 `cube_query` **只在没有 run_sql 时**才回填（与 sql 互斥），
  `report_builder` 又 `if sql:` 优先 ⇒ **SQL 通道的报告天然没有口径节**，口径只能落在
  模型自己写的 `analysis` 里，而这次它只写了「查询口径」（统计范围，不是业务定义）。

**修法**（三通道，都进 `business_caliber` → 同一个渲染器）：
1. 子 agent 最终回复末尾附 `## 业务口径` 块（系统提示词 §十一 + §十二 + `wren-execution`
   SKILL.md 立契约）——**这是主路径**：口径原文（`knowledge/rules/*.md`）只有子 agent
   手里有，主 agent 只看得到摘要过的 check 结果，所以 `build_report` 必须能从结果里**抽**，
   不能只依赖主 agent 转述。
2. 主 agent 显式 `business_caliber=[...]` 传参（优先级最高）。
3. 兜底（env 开关，默认关）：`REPORT_CALIBER_APPENDIX_MAX_CHARS>0` 时按 db_name 解析出
   wren 项目目录，直接把 `knowledge/rules/*.md` 原文附到报告末尾——唯一不依赖模型自觉的通道。

本脚本断言：抽取器容忍四种写法、渲染器不丢内容、真入口端到端出节、Cube/SQL 两通道互斥
行为正确、空口径不留空节、兜底开关行为、**摘要之后口径块仍存活**（设计的关键假设），
以及提示词/技能契约在位（防静默回退）。不需要 LLM、不需要 MCP。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_report_caliber.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys
import tempfile

# report_builder 落盘走 WorkspaceManager（读 AGENT_DATA_ROOT）⇒ 先钉临时根，
# 避免本脚本写进真实工作区（沿用仓内 verify 脚本的约定）。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-caliber-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)
os.environ.setdefault("REPORT_CALIBER_APPENDIX_MAX_CHARS", "0")

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from langchain_core.messages import ToolMessage  # noqa: E402

from agent.tools import report_builder as rb  # noqa: E402
from agent.tools.report_builder import (  # noqa: E402
    BuildReportSchema,
    _build_report_coro,
    _cell,
    _parse_caliber_block,
    _render_business_caliber,
)
from agent.utils.caliber_evidence import CaliberVerdict  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


# ── fixtures ──────────────────────────────────────────────────────────
_CALIBER_BLOCK = """
## 业务口径
- 已审核工时 | `if_approve = 1` 的 `work_hour` 之和 | rules/报工与工时.md R3
- 未审核工时 | `if_approve = 0` 的 `work_hour` 之和 | rules/报工与工时.md R4
"""


def _check_msg(result_text: str, *, sql: str = "", cube_query: str = "",
               extra: dict | None = None) -> ToolMessage:
    obj: dict = {"status": "success", "thread_id": "t-1", "result": result_text}
    if sql:
        obj["sql"] = sql
    if cube_query:
        obj["cube_query"] = cube_query
    obj.update(extra or {})
    return ToolMessage(
        content=json.dumps(obj, ensure_ascii=False),
        name="check_async_task",
        tool_call_id="call_check",
    )


class _Runtime:
    """`_build_report_coro` 只用到 runtime.state["messages"]。"""

    def __init__(self, messages: list) -> None:
        self.state = {"messages": messages}


def _report_dir() -> pathlib.Path:
    from agent.workspace_manager import get_workspace_manager

    return pathlib.Path(get_workspace_manager().report_dir)


def _build(messages: list, **kw) -> tuple[str, str]:
    """跑真入口，返回 (工具返回文本, 新落盘报告全文)。"""
    rd = _report_dir()
    before = set(rd.glob("*.md")) if rd.is_dir() else set()
    msg = asyncio.run(
        _build_report_coro(report_name="口径验证", analysis="解读文本",
                           task_id="", runtime=_Runtime(messages), **kw)
    )
    new = sorted(set(_report_dir().glob("*.md")) - before)
    text = new[-1].read_text(encoding="utf-8") if new else ""
    return msg, text


# ── t1 抽取器：四种写法 ────────────────────────────────────────────────
def t1_parser_forms() -> None:
    print("\n[t1] 抽取器：四种写法 / 边界")
    got = _parse_caliber_block("结论。\n\n## 业务口径\n- a | b | c\n- d | e | f\n")
    check(got == ["a | b | c", "d | e | f"], "标准：## 标题 + 无序列表", got)

    table = (
        "## 业务口径\n"
        "| 口径项 | 内容 | 出处 |\n|---|---|---|\n"
        "| 已审核工时 | 求和 | rules/报工与工时.md R3 |\n"
    )
    check(_parse_caliber_block(table) == ["已审核工时 | 求和 | rules/报工与工时.md R3"],
          "表格形态：表头/分隔行被跳过、数据行还原", _parse_caliber_block(table))

    check(_parse_caliber_block("**业务口径**\n1. a | b | c\n2. d | e | f\n")
          == ["a | b | c", "d | e | f"], "粗体标题 + 有序列表")

    check(_parse_caliber_block("没有这一节") == [], "无标题 → 空（不误抽正文）")
    check(_parse_caliber_block("") == [], "空文本 → 空")

    multi = ("## 业务口径\n- 旧的 | x | y\n\n正文\n\n## 业务口径\n- 新的 | x | y\n")
    check(_parse_caliber_block(multi) == ["新的 | x | y"],
          "取最后一个同名标题（防正文引用过同名小节）", _parse_caliber_block(multi))

    stopped = "## 业务口径\n- a | b | c\n\n## 分析解读\n- 不该被当成口径\n"
    check(_parse_caliber_block(stopped) == ["a | b | c"],
          "下一个标题即本节结束（不吞下一节）")

    many = "## 业务口径\n" + "".join(f"- i{i} | x | y\n" for i in range(30))
    check(len(_parse_caliber_block(many)) == rb._CALIBER_MAX_ITEMS,
          f"上限 {rb._CALIBER_MAX_ITEMS} 条", len(_parse_caliber_block(many)))

    para = "## 业务口径\n下面是本次口径的说明性段落，不是条目。\n"
    check(_parse_caliber_block(para) == [], "标题后无列表符号的说明段落不算条目")


# ── t2 渲染器 ─────────────────────────────────────────────────────────
# 假脚注原文：旧实现**无条件**写的「口径取自语义库知识库原文…未做推断」。生产 trace
# `9c81d3181f2a72f27cf9d092d7185fab` 的表里出处全是 `workhour_analysis（Cube）` /
# `v_workhour` / `语义库字段字典`，一个知识库文件都没有 ⇒ 那句话与表内容无关 = 报告撒谎。
_FAKE_FOOTER = "口径取自语义库知识库原文"
_A = "已审核工时 | 求和 | rules/报工与工时.md R3"
_B = "报工口径 | 报工以 workhour_analysis（Cube）为准 | v_workhour"


def _v(item, ok=False, reason="", closest=""):
    return CaliberVerdict(item=item, ok=ok, reason=reason, closest=closest)


def t2_renderer() -> None:
    print("\n[t2] 渲染器：表格 / 降级 / 转义 / 四态脚注")
    md = _render_business_caliber([_A])
    check("| 口径项 | 内容 | 出处 |" in md, "三段 → 表头存在")
    check("| 已审核工时 | 求和 | rules/报工与工时.md R3 |" in md, "三段 → 数据行存在")
    check(_FAKE_FOOTER not in md, "★ 假脚注绝迹（未核验态）", md.splitlines()[-1][:60])
    check("未对口径内容做原文逐字核验" in md, "未核验态 → 明确声明未核验")

    # 四态：全通过 / 部分 / 全不过 / 未核验
    ok_md = _render_business_caliber([_A], [_v(_A, ok=True)])
    check("逐字一致" in ok_md and _FAKE_FOOTER not in ok_md, "全通过态 → 声明逐字一致")
    bad_md = _render_business_caliber(
        [_A, _B],
        [_v(_A, ok=True), _v(_B, reason="source_not_kb_file", closest="rules/报工与工时.md")],
    )
    check("未通过核验的口径" in bad_md and _FAKE_FOOTER not in bad_md,
          "部分不过 → 不合格条目单列第二张表")
    check("| 报工口径 | 报工以 workhour_analysis（Cube）为准 | v_workhour |" in bad_md,
          "单列行保留模型原文（不删改，用户要看它错在哪）")
    check("出处不是知识库文件名" in bad_md, "单列行给出核验结论")
    allbad = _render_business_caliber(
        [_B], [_v(_B, reason="source_not_kb_file")]
    )
    check("| 口径项 | 内容 | 出处 |" not in allbad, "全不过态 → 不出主表头（没有合规行）")
    check("全部未通过" in allbad and _FAKE_FOOTER not in allbad, "全不过态 → 全不过脚注")
    # 结论：四态都不出现假脚注
    for label, m in (("未核验", md), ("全通过", ok_md), ("部分", bad_md), ("全不过", allbad)):
        check(_FAKE_FOOTER not in m and "未做推断" not in m, f"★ {label}态无假脚注")

    # 模型自己写的「核验通过」自评**绝不进报告**：本侧裁决由代码产生，转述模型自评
    # 就是「报告撒谎」换个人称。渲染输入只有条目，自评行连条目都不是 ⇒ 进不来。
    self_claim = "结论。\n\n## 业务口径\n| 口径项 | 内容 | 出处 |\n|---|---|---|\n| a | b | c |\n\n> 口径核验：2/2 条均已与知识库原文逐字核验通过。\n"
    check(_parse_caliber_block(self_claim) == ["a | b | c"], "自评行（`>` 引用）不被当成口径条目")
    check("逐字核验通过" not in _render_business_caliber(_parse_caliber_block(self_claim)),
          "★ 模型自评的「已核验通过」不进报告（结论只由代码产生）")

    md2 = _render_business_caliber(["只有一段没有分隔符"])
    check("- 只有一段没有分隔符" in md2 and "| 口径项" not in md2,
          "解析不出三段 → 降级列表项（不丢内容）", md2)

    md3 = _render_business_caliber(["口径项 | 只有两段"])
    check("- 口径项 | 只有两段" in md3, "两段 → 同样降级，内容原样保留", md3)
    check("\\|" not in md3, "降级项不转义竖线（不在表格里）", md3)

    check(_render_business_caliber([]) == "", "空输入 → 空串（调用方整节跳过）")
    check(_render_business_caliber(["", "  "]) == "", "空白条目被忽略")

    check(_cell("a|b\nc") == "a\\|b c", "单元格转义竖线 + 折叠换行", _cell("a|b\nc"))

    md4 = _render_business_caliber(["甲 | 含 | 竖线的内容 | rules/x.md"])
    check(md4.count("| 甲 |") == 1 and "rules/x.md" in md4,
          "内容自带竖线时余下字段并入出处（不破表）")


# ── t3 schema ─────────────────────────────────────────────────────────
def t3_schema() -> None:
    print("\n[t3] BuildReportSchema：默认值 + 约束说明")
    s = BuildReportSchema(report_name="r", analysis="a")
    check(s.business_caliber == [], "business_caliber 默认为空列表")
    desc = BuildReportSchema.model_fields["business_caliber"].description or ""
    check("口径项 | 内容 | 出处" in desc, "描述给出三字段格式")
    check("不要编" in desc, "描述含「不要编造出处」约束")
    check("自动从子任务结果" in desc, "描述说明缺省时从子任务结果提取")


# ── t4 端到端：SQL 通道 ───────────────────────────────────────────────
def t4_sql_channel() -> None:
    print("\n[t4] 端到端（SQL 通道）：口径节从子任务结果抽出来并落进报告")
    msg, md = _build([_check_msg("数据在里面\n" + _CALIBER_BLOCK,
                                 sql="SELECT user_name FROM v_workhour")])
    check("报告已生成" in msg, "工具返回成功", msg.splitlines()[0] if msg else "")
    check("## 2. 业务口径" in md, "SQL 通道出现「业务口径」节（本次修复的核心）")
    check("| 已审核工时 | `if_approve = 1` 的 `work_hour` 之和 | rules/报工与工时.md R3 |" in md,
          "口径表含内容与出处")
    check("## 3. 执行 SQL" in md, "后续节序号顺延（3=执行 SQL）")
    check("、业务口径" in msg, "工具返回的「内容：」列出业务口径")
    # 序号连续性（附录序号不得与前节撞号）
    nums = [int(x) for x in re.findall(r"^## (\d+)\. ", md, re.M)]
    check(nums == list(range(1, len(nums) + 1)), "各节序号连续无重复", nums)


def _caliber_section(md: str) -> str:
    """报告里**渲染出来的**「业务口径」节正文（§1 原样内嵌的 result_text 不算）。"""
    m = re.search(r"^## \d+\. 业务口径\n(.*?)(?=^## |\Z)", md, re.M | re.S)
    return m.group(1) if m else ""


# ── t5 显式传参优先 ───────────────────────────────────────────────────
def t5_arg_precedence() -> None:
    print("\n[t5] 显式传参优先于结果抽取")
    _, md = _build([_check_msg("数据\n" + _CALIBER_BLOCK, sql="SELECT 1")],
                   business_caliber=["显式项 | 显式内容 | rules/通用规则.md"])
    sec = _caliber_section(md)
    check("显式项" in sec, "用了 business_caliber 传参")
    check("已审核工时" not in sec,
          "渲染节里未混入结果里的口径块（§1 内嵌的那份已被剥离，见 t11）")


# ── t6 空口径：不留空节 ────────────────────────────────────────────────
def t6_empty() -> None:
    print("\n[t6] 无口径 → 整节不出现，且不虚报")
    msg, md = _build([_check_msg("只有数据，没有口径块", sql="SELECT 1")])
    check("业务口径" not in md, "报告里不出现空的口径节")
    check("、业务口径" not in msg, "工具返回不虚报业务口径")


# ── t7 Cube 通道互斥 + Layer A 让位 ───────────────────────────────────
def t7_cube_channel() -> None:
    print("\n[t7] Cube 通道：Layer A 让位、结构/定义节仍在、只有一节「业务口径」")
    calls = {"n": 0}
    real = rb._render_cube_layer_a

    async def _spy(*a, **kw):
        calls["n"] += 1
        return "LLM 摘要文本"

    cube_obj_extra = {
        "cube_args": {"cube": "workhour_analysis", "measures": ["total_hours"]},
        "cube_tool": "wrenai_X_query_cube",
    }
    try:
        rb._render_cube_layer_a = _spy
        _, md = _build([_check_msg("数据\n" + _CALIBER_BLOCK,
                                   cube_query="- cube: workhour_analysis",
                                   extra=cube_obj_extra)])
        check(calls["n"] == 0, "有结构化口径时不再调 Layer A（省一次 LLM 调用）")
        check(len(re.findall(r"^## \d+\. 业务口径$", md, re.M)) == 1,
              "「业务口径」节只出现一次（§1 内嵌的原文块不算）")
        # 负对照：Layer B（查询结构，与口径无关、无 LLM 调用）不得跟着 Layer A 一起被跳过
        check("## 3. 查询结构" in md,
              "Layer B 查询结构仍在（不因有口径而丢失 —— 曾挂在 Layer A 开关上）")
        check("## 4. 查询定义（Cube 语义层）" in md, "Layer C 查询定义仍在（序号顺延）")

        calls["n"] = 0
        _, md2 = _build([_check_msg("数据，无口径块", cube_query="- cube: x",
                                    extra=cube_obj_extra)])
        check(calls["n"] == 1, "无结构化口径时 Layer A 照旧生效（不回退）")
        check("## 2. 业务口径" in md2 and "LLM 摘要文本" in md2, "Layer A 落到业务口径节")
        check("## 3. 查询结构" in md2, "无口径时 Layer B 序号同样对得上")
    finally:
        rb._render_cube_layer_a = real


# ── t8 兜底附录开关 ───────────────────────────────────────────────────
def t8_appendix_switch() -> None:
    print("\n[t8] 兜底通道（env 开关）：默认关；开启后读 rules/*.md 原文")
    _, md_off = _build([_check_msg("数据", sql="SELECT 1")])
    check("附录：语义库口径原文" not in md_off, "默认关闭 → 不附原文")

    proj = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-fake-semantic-"))
    rules = proj / "knowledge" / "rules"
    rules.mkdir(parents=True)
    (rules / "报工与工时.md").write_text("R3 已审核工时 = if_approve=1 的 work_hour 之和\n",
                                        encoding="utf-8")

    import agent.utils.wren_call_extract as wce

    real_max, real_db = rb._CALIBER_APPENDIX_MAX_CHARS, rb._current_db_name
    real_resolve = wce.resolve_wren_ctx_by_db
    try:
        rb._CALIBER_APPENDIX_MAX_CHARS = 5000
        rb._current_db_name = lambda: "FakeDB"
        wce.resolve_wren_ctx_by_db = lambda db: (str(proj), {})
        _, md_on = _build([_check_msg("数据", sql="SELECT 1")])
        check("附录：语义库口径原文" in md_on, "开关开启 → 出现口径原文节")
        check("已审核工时 = if_approve=1" in md_on, "原文内容在内")
        check("### 报工与工时.md" in md_on, "带文件名小标题（可追溯）")

        wce.resolve_wren_ctx_by_db = lambda db: ("", {})
        _, md_fail = _build([_check_msg("数据", sql="SELECT 1")])
        check("数据" in md_fail and "附录：语义库口径原文" not in md_fail,
              "解析不到项目 → fail-open，报告照出")
    finally:
        rb._CALIBER_APPENDIX_MAX_CHARS = real_max
        rb._current_db_name = real_db
        wce.resolve_wren_ctx_by_db = real_resolve


# ── t9 关键假设：摘要之后口径块仍在 ────────────────────────────────────
def t9_survives_summarization() -> None:
    """`build_report` 拿到的 result 是**摘要过**的子 agent 最终回复 —— 若口径块
    活不过摘要，整个「从结果抽取」的设计就不成立（这才是必须离线钉死的假设）。"""
    print("\n[t9] 摘要之后口径块仍可抽（设计的关键假设）")
    from agent.subagents.check_progress import _summarize_result

    long_text = "结论段。\n\n" + ("正文句子，很长。" * 400) + "\n\n" + _CALIBER_BLOCK
    s1 = _summarize_result(long_text)
    check(len(long_text) > 2000 and len(s1) < len(long_text), "确实被摘要了",
          f"{len(long_text)} → {len(s1)}")
    check(bool(_parse_caliber_block(s1)), "纯文本路径：摘要后仍抽到口径",
          _parse_caliber_block(s1))

    with_table = (
        "结论：合计 120.0 h。\n\n"
        "| 日期 | 工时 |\n|---|---|\n" + "".join(f"| 2026-09-{i:02d} | 8.0 |\n" for i in range(1, 29))
        + "\n" + _CALIBER_BLOCK
    )
    s2 = _summarize_result(with_table)
    check(bool(_parse_caliber_block(s2)), "含表格路径（表后正文按剩余预算保留）：仍抽到口径",
          _parse_caliber_block(s2))


# ── t10 契约文本在位（防静默回退）──────────────────────────────────────
def t10_contracts() -> None:
    print("\n[t10] 提示词 / 技能契约在位")
    nl2sql = (_SRC / "agent" / "prompt" / "NL2SQL_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    check("## 业务口径" in nl2sql and "口径项 | 内容 | 出处" in nl2sql,
          "子 agent 系统提示词 §十一 立了口径块契约")
    check("业务口径要交出去" in nl2sql, "§十二 关键提醒里有对应提醒")
    check("原样抽走" in nl2sql, "说明了主 agent 会抽走这一节（动机可见）")

    main_p = (_SRC / "agent" / "prompt" / "MAIN_AGENT_PROMPT.md").read_text(encoding="utf-8")
    check("business_caliber" in main_p and "业务口径" in main_p,
          "主 agent 提示词提到 business_caliber")

    exe = (_SRC / "agent" / "shared" / "skills" / "nl2sql" / "wren-execution" / "SKILL.md"
           ).read_text(encoding="utf-8")
    check("## 业务口径" in exe and "口径项 | 内容 | 出处" in exe,
          "wren-execution 结果呈现契约含口径块")

    metric = (_SRC / "agent" / "shared" / "skills" / "nl2sql" / "wren-metric-query" / "SKILL.md"
              ).read_text(encoding="utf-8")
    check("user_name:eq:丛培强" in metric, "filters op 正例在位（eq 而非 =）")
    check("user_name:=:丛培强" in metric and "unknown variant" in metric,
          "filters op 反例在位（生产那次失败就是它）")
    check("要出数就摘掉 `sql_only`" in metric,
          "sql_only 只预览不取数的坑已写明（否则报告退回 SQL 通道）")

    span = (_SRC / "agent" / "middlewares" / "langfuse_span.py").read_text(encoding="utf-8")
    check("chars, see {vfs}" in span,
          "Langfuse 展示指针带上原长度（防再被它误导成「模型没拿到内容」）")

    # ── 2026-09-26 补丁：整节省略（生产 trace f222a8a5…）的三处措辞 ──────
    # 那一轮 v4 提示词 + 闸门都在线上，子 agent 手里有 20.9k 口径原文却整节省略 ⇒ 报告零口径。
    # 三处措辞缺任一处，天平就会重新倒向「省略最省事」。
    check("不必也不要自己拼目录前缀" in nl2sql,
          "★ §十一 明说「出处照抄裸文件名、不必自己拼 rules/ 前缀」"
          "（契约原先只给带前缀的例子，而 get_instructions 返回里文件名是裸的）")
    check("本次未依据知识库口径" in nl2sql,
          "★ §十一 给出「确实没用到」的标准写法（让省略有个明确的、可核对的形态）")
    check("本节就是**必写**" in nl2sql and "比率的分母" in nl2sql,
          "★ §十一 把「整节不写」收紧成「仅纯明细列举才可以」，并列了必写的触发条件")
    check("不要因为删条目而把整节省掉" in nl2sql,
          "★ §十一 明确「删条目不删节」")
    check("不必也不要自己拼 `rules/` 前缀" in exe,
          "wren-execution 同步了「裸文件名照抄」措辞")
    check("什么时候才允许整节不写" in exe,
          "wren-execution 同步了「整节不写」的收紧条件")
    check("不要因为删条目而把整节省掉" in exe,
          "wren-execution 同步了「删条目不删节」")
    check("裸名如 `报工与工时.md` 也认" in main_p,
          "主 agent 提示词同步了「裸文件名也认」（它原先可能因「不确定文件名规范」而干脆不传）")


# ── t11 §1 不重复内嵌口径块（P2）──────────────────────────────────────
_CALIBER_BLOCK_THEN_MORE = """
## 业务口径
- 已审核工时 | `if_approve = 1` 的 `work_hour` 之和 | rules/报工与工时.md R3

## 附注
口径按自然月统计。
"""


def t11_no_duplicate_section() -> None:
    print("\n[t11] §1 剥掉重复口径块：同一张表只出现一次，附注不被误摘")
    _, md = _build([_check_msg("数据在里面\n" + _CALIBER_BLOCK, sql="SELECT 1")])
    heads = re.findall(r"^#{1,4}\s*(?:\d+\.\s*)?业务口径\s*$", md, re.M)
    check(len(heads) == 1, "报告里口径标题只出现 1 次（= §2 渲染的那一节）",
          f"实际 {len(heads)} 次；正文出现「业务口径」{md.count('业务口径')} 次")
    check("已审核工时" in _caliber_section(md), "口径节内容完整（剥离没伤到渲染源）")
    m1 = re.search(r"^## 1\. 数据结果\n(.*?)(?=^## |\Z)", md, re.M | re.S)
    s1 = m1.group(1) if m1 else ""
    check("已审核工时" not in s1, "§1 里不再出现口径行（原文已剥离）")
    check("数据在里面" in s1, "§1 仍保留子 agent 的数据正文")

    # 负对照：块后面还有别的节时，只摘块、不摘到文末（§1 的正文分节停在下一个 `## `，
    # 所以这里按位置比：附注必须仍在**口径节之前**= 落在 §1 里）。
    _, md2 = _build([_check_msg("数据\n" + _CALIBER_BLOCK_THEN_MORE, sql="SELECT 1")])
    i_note = md2.find("口径按自然月统计")
    m2 = re.search(r"^## \d+\. 业务口径", md2, re.M)
    i_sec2 = m2.start() if m2 else -1
    check(i_note > 0 and i_sec2 > 0, "附注与口径节都在报告里", f"note={i_note} sec2={i_sec2}")
    check(0 < i_note < i_sec2, "★ 块后面的节仍在 §1 内（摘块不摘到文末）")
    check("已审核工时" not in md2[:_caliber_sec_start(md2)],
          "★ 同一份报告：§1 里连口径行也没有（块整段被摘掉）")


def _caliber_sec_start(md: str) -> int:
    m = re.search(r"^## \d+\. 业务口径", md, re.M)
    return m.start() if m else len(md)


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    print(f"源码根 = {_SRC}")
    t1_parser_forms()
    t2_renderer()
    t3_schema()
    t4_sql_channel()
    t5_arg_precedence()
    t6_empty()
    t7_cube_channel()
    t8_appendix_switch()
    t9_survives_summarization()
    t10_contracts()
    t11_no_duplicate_section()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
