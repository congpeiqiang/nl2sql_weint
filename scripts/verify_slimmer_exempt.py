# -*- coding: utf-8 -*-
"""知识类取料结果免截断验证（离线，无需后端/数据库/网络）。

**要关的口子**（生产 trace `2f98ed67…`，2026-09-25）：四路取料的**规则轴**
`wrenai_<库名>_get_instructions` 返回 `knowledge/rules/*.md` 的无边界拼接（实测
20,919 字符 = 3 个文件），超过 `LARGE_RESULT_TRUNCATE_CHARS`(8000) 被
MessageSlimmerMiddleware 落盘，上下文里只剩 head5/tail5 预览 —— 而 R1~R8 正文正好在被
砍掉的中间。模型读到的头 5 行恰好写着「工时专项见 `报工与工时.md`」，于是拿
read_file/grep/glob/ls 去文件系统找了它 9 次（真路径 `/workspace/<语义库目录>/knowledge/…`
含一段不可推导的目录名，且不在子 agent 的可读通道内），全被 `NL2SQL_FILE_PERMISSIONS` 拒，
紧接着 LLM 调用 243s 超时 ⇒ 子任务报废、357s 零产出。

**修法**：给知识类工具加免截断白名单（`_KNOWLEDGE_EXEMPT_MAX_CHARS`，默认 60000），
配套在系统提示词 §9.1/§十二 与 `wren-retrieve` SKILL.md 里写死「落盘要 read_file 指针路径、
正文里的 `xxx.md` 是来源标注、knowledge/** 不在可读 VFS 通道内」。

本脚本只做**离线断言**：真跑中间件（同步 + 异步两条路径）比对「是否落盘」，并检查
提示词/技能文本里的关键约束还在（防静默回退）。不需要 LLM、不需要 MCP。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_slimmer_exempt.py

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
from types import SimpleNamespace

# 中间件本身不读 AGENT_DATA_ROOT，但 import 链上的 agent.* 可能读 ⇒ 先钉一个临时根，
# 避免本脚本碰到真实数据目录（沿用仓内 verify 脚本的约定）。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-slimmer-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from langchain_core.messages import ToolMessage  # noqa: E402

from agent.middlewares.message_slimmer import (  # noqa: E402
    MessageSlimmerMiddleware,
    _ATTRIBUTION_EXEMPT_MAX_CHARS,
    _KNOWLEDGE_EXEMPT_MAX_CHARS,
    _is_attribution_tool,
    _is_knowledge_tool,
)

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


class FakeBackend:
    """最小 backend：只实现 `_offload_tool_message_content` 需要的 `write`。

    落盘成功返回带 `error=None` 的对象；`fail=True` 时返回 error（用于 fail-open 断言）。
    """

    def __init__(self, fail: bool = False) -> None:
        self.writes: dict[str, str] = {}
        self.fail = fail

    def write(self, path: str, content: str):
        if self.fail:
            return SimpleNamespace(error="disk full")
        self.writes[path] = content
        return SimpleNamespace(error=None)

    async def awrite(self, path: str, content: str):
        """异步路径走的是 `backend.awrite`（`_aoffload_tool_message_content`）——
        只给同步 `write` 会让 async 侧静默 fail-open，测出来的是 fixture 缺方法而不是产品行为。"""
        return self.write(path, content)


def _tool_msg(name: str, content: str, tool_call_id: str = "call_1") -> ToolMessage:
    return ToolMessage(content=content, name=name, tool_call_id=tool_call_id)


def _slimmer(backend: FakeBackend, **kw) -> MessageSlimmerMiddleware:
    kw.setdefault("max_chars_before_truncate", 8000)
    kw.setdefault("chart_exempt_max_chars", 60000)
    return MessageSlimmerMiddleware(backend=backend, **kw)


def _sync(msg: ToolMessage, mw: MessageSlimmerMiddleware) -> ToolMessage:
    return mw._process_tool_message_sync(msg, [])


async def _async(msg: ToolMessage, mw: MessageSlimmerMiddleware) -> ToolMessage:
    return await mw._process_tool_message_async(msg, [])


def _truncated(msg: ToolMessage) -> bool:
    """结果是否已被落盘替换（stub 文本里的两个标志词）。"""
    text = msg.content if isinstance(msg.content, str) else str(msg.content)
    return ("large_tool_results" in text) or ("lines truncated" in text)


# ── ① 具名工具识别（后缀匹配，兼容 wrenai_<库名>_ 前缀）────────────────────────
def t1_name_matching() -> None:
    print("\n① 知识类工具识别")
    yes = [
        "get_instructions",                       # 裸名（提示词里写的形式）
        "wrenai_witops_get_instructions",         # 生产实测真名
        "wrenai_imdb_get_instructions",
        "wrenai_witops_get_all_knowledge",
        "wrenai_witops_list_knowledge",
        "wrenai_Chinook_Aliyun_list_knowledge",   # 库名含大小写/下划线
    ]
    no = [
        "read_file", "execute", "grep", "glob", "ls", "write_file",
        "get_context", "recall_queries", "list_stored_queries", "list_cubes",
        "run_sql", "dry_run", "query_cube", "get_mdl",
    ]
    check(all(_is_knowledge_tool(n) for n in yes), "6 个知识类名全命中（裸名 + 带前缀）",
          "、".join(yes))
    # 负对照：非知识类工具必须一个都不命中（否则会把大结果全留在上下文里）
    bad = [n for n in no if _is_knowledge_tool(n)]
    check(not bad, "14 个非知识类工具全不命中（负对照）", f"误命中={bad}" if bad else "")
    check(not _is_knowledge_tool(None) and not _is_knowledge_tool(""),
          "None / 空名不命中（ToolMessage.name 可能缺失）")


# ── ② 生产场景：20,919 字符的 rules 拼接不落盘 ────────────────────────────────
_RULES_HEAD = (
    "通用约束见 `通用规则.md`，工时专项见 `报工与工时.md`，域口径见 `业务域口径.md`。\n"
    "- 全局：只读查询；产出报表须标注口径来源。\n"
    "- 命名：库名大小写不敏感。\n"
    "- 时间：一律用考勤日期字段。\n"
    "- 样例：SELECT 1;\n"
)
_RULES_BODY = "".join(
    f"### R{i} 报工口径 {i}\n" + ("正文" * 1300) + "\n" for i in range(1, 9)
)
_RULES_PAYLOAD = _RULES_HEAD + _RULES_BODY
assert len(_RULES_PAYLOAD) > 8000, len(_RULES_PAYLOAD)


def t2_knowledge_exempt() -> None:
    print(f"\n② 知识类结果免截断（载荷 {len(_RULES_PAYLOAD)} 字符 ≈ 生产 20,919 的同一形态）")
    be = FakeBackend()
    mw = _slimmer(be)
    out = _sync(_tool_msg("wrenai_witops_get_instructions", _RULES_PAYLOAD), mw)
    check(out.content == _RULES_PAYLOAD, "get_instructions 结果原样保留（未被落盘替换）")
    check(not _truncated(out), "上下文里没有 TOO_LARGE stub / truncated 标记")
    check("R8" in str(out.content), "被砍掉的中段（R1~R8 正文）仍在上下文里")
    check(be.writes == {}, "完全没有写盘（不是「写了但没替换」）", list(be.writes))

    # 各知识轴 + 裸名同等对待
    for name in ("wrenai_imdb_get_all_knowledge", "list_knowledge", "wrenai_x_list_knowledge"):
        be2 = FakeBackend()
        o2 = _sync(_tool_msg(name, _RULES_PAYLOAD), _slimmer(be2))
        check(o2.content == _RULES_PAYLOAD and be2.writes == {}, f"{name} 同样免截断")


# ── ③ 负对照：非知识类大结果必须**照旧**落盘 ──────────────────────────────────
def t3_negative_control() -> None:
    print("\n③ 负对照：非知识类大结果仍走落盘（证明豁免没扩大化）")
    for name in ("read_file", "get_context", "recall_queries", "execute"):
        be = FakeBackend()
        out = _sync(_tool_msg(name, _RULES_PAYLOAD), _slimmer(be))
        check(_truncated(out), f"{name} 的大结果仍被落盘截断")
        check(len(be.writes) == 1, f"{name} 确实写了盘", list(be.writes))

    # 同一份内容换个工具名就落盘 ⇒ 差异只来自工具名，不来自内容
    be_a, be_b = FakeBackend(), FakeBackend()
    kept = _sync(_tool_msg("get_instructions", _RULES_PAYLOAD), _slimmer(be_a))
    dropped = _sync(_tool_msg("read_file", _RULES_PAYLOAD), _slimmer(be_b))
    check(kept.content != dropped.content and be_a.writes == {} and len(be_b.writes) == 1,
          "同内容不同工具名 → 判定差异只由工具名决定")


# ── ④ 体积上限仍生效（不能无上限撑爆上下文）──────────────────────────────────
def t4_cap() -> None:
    print(f"\n④ 免截断上限（_KNOWLEDGE_EXEMPT_MAX_CHARS={_KNOWLEDGE_EXEMPT_MAX_CHARS}）")
    cap = _KNOWLEDGE_EXEMPT_MAX_CHARS
    at_cap = "x" * cap
    over = "x" * (cap + 1)

    be1 = FakeBackend()
    o1 = _sync(_tool_msg("get_instructions", at_cap), _slimmer(be1))
    check(o1.content == at_cap and be1.writes == {}, f"正好 {cap} 字符 → 免截断（含边界）")

    be2 = FakeBackend()
    o2 = _sync(_tool_msg("get_instructions", over), _slimmer(be2))
    check(_truncated(o2) and len(be2.writes) == 1, f"{cap + 1} 字符 → 仍落盘兜底")

    # 显式 None = 不设上限（永不落盘）
    be3 = FakeBackend()
    mw = MessageSlimmerMiddleware(backend=be3, max_chars_before_truncate=8000,
                                  chart_exempt_max_chars=60000,
                                  knowledge_exempt_max_chars=None)
    o3 = _sync(_tool_msg("get_instructions", over), mw)
    check(o3.content == over and be3.writes == {},
          "构造参数传 None → 知识类永不落盘（不设上限）")

    # 低于截断阈值的小结果本来就不该动
    be4 = FakeBackend()
    small = "短内容" * 10
    o4 = _sync(_tool_msg("get_instructions", small), _slimmer(be4))
    check(o4.content == small and be4.writes == {}, "小结果（<8000）本就原样通过")


# ── ⑤ 异步路径行为一致（子 agent 走的是 async）───────────────────────────────
def t5_async_parity() -> None:
    print("\n⑤ 异步路径（子 agent 实际走 awrap_tool_call → _process_tool_message_async）")

    async def run() -> None:
        be_k, be_r = FakeBackend(), FakeBackend()
        mk, mr = _slimmer(be_k), _slimmer(be_r)
        ok = await _async(_tool_msg("wrenai_witops_get_instructions", _RULES_PAYLOAD), mk)
        rd = await _async(_tool_msg("read_file", _RULES_PAYLOAD), mr)
        check(ok.content == _RULES_PAYLOAD and be_k.writes == {},
              "async：知识类结果原样保留、不写盘")
        check(_truncated(rd) and len(be_r.writes) == 1, "async：非知识类仍落盘")
        # 上限同样生效
        be3 = FakeBackend()
        o3 = await _async(_tool_msg("get_instructions", "x" * (_KNOWLEDGE_EXEMPT_MAX_CHARS + 1)),
                          _slimmer(be3))
        check(_truncated(o3) and len(be3.writes) == 1, "async：超上限仍落盘")
        # 真入口：awrap_tool_call（含 wrap 层级）
        be4 = FakeBackend()
        mw = _slimmer(be4)
        req = SimpleNamespace(state={"messages": []})

        async def handler(_req):
            return _tool_msg("wrenai_witops_get_instructions", _RULES_PAYLOAD)

        res = await mw.awrap_tool_call(req, handler)
        check(res.content == _RULES_PAYLOAD and be4.writes == {},
              "async：经 awrap_tool_call 真入口仍免截断")

    asyncio.run(run())


# ── ⑥ fail-open：落盘失败不许丢内容 ─────────────────────────────────────────
def t6_fail_open() -> None:
    print("\n⑥ fail-open（落盘失败时保留原结果）")
    be = FakeBackend(fail=True)
    out = _sync(_tool_msg("read_file", _RULES_PAYLOAD), _slimmer(be))
    check(out.content == _RULES_PAYLOAD, "非知识类落盘失败 → 原结果保留（不回退成空）")


# ── ⑦ 接线：子 agent / 主 agent 真的挂了本中间件 ─────────────────────────────
def t7_wiring() -> None:
    print("\n⑦ 接线（改了默认值但没挂上 = 无效修复）")
    src_nl2sql = (_SRC / "agent/graphs/nl2sql_agent.py").read_text(encoding="utf-8")
    src_main = (_SRC / "agent/main_agent.py").read_text(encoding="utf-8")
    check("MessageSlimmerMiddleware(" in src_nl2sql, "子 agent 图里构造了 MessageSlimmerMiddleware")
    check("MessageSlimmerMiddleware(" in src_main, "主 agent 里构造了 MessageSlimmerMiddleware")
    check("MessageSlimmerMiddleware(backend=" in src_nl2sql,
          "子 agent 侧传了 backend（无 backend 则根本不会落盘，本豁免也无从谈起）")


# ── ⑧ 提示词 / 技能文本约束（防静默回退）─────────────────────────────────────
def t8_prompt_and_skill() -> None:
    print("\n⑧ 提示词与技能文本")
    prompt = (_SRC / "agent/prompt/NL2SQL_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    skill = (_SRC / "agent/shared/skills/nl2sql/wren-retrieve/SKILL.md").read_text(
        encoding="utf-8"
    )

    check("large_tool_results" in prompt, "提示词给出落盘读回路径 /workspace/large_tool_results/<id>")
    check("来源标注" in prompt, "提示词写明正文里的 `xxx.md` 是来源标注、不是要打开的文件")
    check(re.search(r"不在你的可读\s*\n?VFS\s*通道内", prompt) is not None
          or "不在你的可读" in prompt, "提示词写明 knowledge/** 不在可读 VFS 通道内")
    check("§9.1" in prompt or "9.1 " in prompt, "提示词有专门的 §9.1 小节收这些约束")
    check("large_tool_results" in prompt.split("## 十二")[-1],
          "§十二 关键提醒里也留了一条（高可见位）")

    # 旧文案必须消失：它正是这次事故的起点（教模型「按需读取」知识文件）
    check("按需读取" not in prompt, "误导性的「list_knowledge() + 按需读取」已删除")
    deprecated = [ln.strip() for ln in prompt.splitlines() if "按路径读" in ln]
    check(not deprecated, "没有「按路径读知识文件」这类残留指令", deprecated)

    check("large_tool_results" in skill, "wren-retrieve SKILL.md 写了落盘读回路径")
    check("permission denied" in skill, "SKILL.md 点明 knowledge/** 会 permission denied")
    check("禁止" in skill and "knowledge/**" in skill, "SKILL.md 明示禁止去 knowledge/** 找文件")

    # 免截断白名单得在技能里说清楚范围，否则模型会以为 get_context 也不会落盘
    check("get_context" in skill and "仍会落盘" in skill,
          "SKILL.md 说明只有知识类工具免截断，其它两轴仍会落盘")


# ── ⑨ 归因结果免截断（S5：`attribution_analyze`，独立白名单、不动知识料那条）──────
_ATTR_PAYLOAD = json.dumps(
    {
        "measures": ["total_hours"],
        "primary_measure": "total_hours",
        "per_measure": {
            "total_hours": {
                "measure": "total_hours",
                "strategy": "term_wise",
                "unsupported_reason": None,
                "total_change": {
                    "baseline_value": 12345.6, "current_value": 9876.5,
                    "delta": -2469.1, "pct_change": -20.0,
                },
                "dimension_ranking": [
                    {"dimension": f"dim_{i}", "score": -100.0 * i} for i in range(5)
                ],
                "selected_dimensions": [f"dim_{i}" for i in range(3)],
                "per_dimension": {
                    f"dim_{i}": {
                        "dimension": f"dim_{i}",
                        "values": [
                            {
                                "value": f"取值{j}", "delta": -j * 3.5,
                                "contribution_pct": -j * 0.7, "segment_kind": "normal",
                                "drill_down": {"filters": [
                                    {"dimension": f"dim_{i}", "operator": "eq", "value": f"取值{j}"}
                                ]},
                            }
                            for j in range(60)
                        ],
                    }
                    for i in range(3)
                },
                "warnings": [],
            }
        },
        "warnings": [],
        "comparison_metadata": {"baseline_days": 31, "current_days": 31, "queries_executed": 8},
        "queries_executed": 8,
    },
    ensure_ascii=False,
)
assert len(_ATTR_PAYLOAD) > 8000, len(_ATTR_PAYLOAD)


def t9_attribution_exempt() -> None:
    print(f"\n⑨ 归因结果免截断（载荷 {len(_ATTR_PAYLOAD)} 字符，嵌套 JSON 形态）")

    # 具名识别：后缀匹配，且与知识料那条**互不污染**
    yes = ["attribution_analyze", "tool_attribution_analyze"]
    check(all(_is_attribution_tool(n) for n in yes), "归因工具名全命中（裸名 + 带前缀）",
          "、".join(yes))
    bad = [n for n in ("get_instructions", "wrenai_witops_get_instructions", "run_sql",
                       "read_file", "get_context", "build_report") if _is_attribution_tool(n)]
    check(not bad, "非归因工具全不命中（负对照）", f"误命中={bad}" if bad else "")
    check(not _is_attribution_tool(None) and not _is_attribution_tool(""),
          "None / 空名不命中")
    check(not _is_knowledge_tool("attribution_analyze")
          and not _is_attribution_tool("wrenai_witops_get_instructions"),
          "两条白名单各自独立（没把归因塞进知识料那条常量）")

    # 命中：嵌套 JSON 原样进上下文（截断后是半个 JSON，模型会照着读错数字）
    be = FakeBackend()
    out = _sync(_tool_msg("attribution_analyze", _ATTR_PAYLOAD), _slimmer(be))
    check(out.content == _ATTR_PAYLOAD, "归因结果原样保留（未被落盘替换）")
    check(not _truncated(out), "上下文里没有 TOO_LARGE stub / truncated 标记")
    check("dim_2" in str(out.content) and "取值59" in str(out.content),
          "末段数据（dim_2 / 取值59）仍在上下文里 —— 截断丢的正是这些")
    check(be.writes == {}, "完全没有写盘", list(be.writes))

    # 负对照：同一份内容换个工具名就落盘 ⇒ 差异只来自工具名
    be_a, be_b = FakeBackend(), FakeBackend()
    kept = _sync(_tool_msg("attribution_analyze", _ATTR_PAYLOAD), _slimmer(be_a))
    dropped = _sync(_tool_msg("read_file", _ATTR_PAYLOAD), _slimmer(be_b))
    check(kept.content != dropped.content and be_a.writes == {} and len(be_b.writes) == 1,
          "同内容不同工具名 → 判定差异只由工具名决定")

    # 上限仍生效（不能无上限撑爆上下文）
    cap = _ATTRIBUTION_EXEMPT_MAX_CHARS
    be_c, be_d = FakeBackend(), FakeBackend()
    at_cap = "x" * cap
    o_c = _sync(_tool_msg("attribution_analyze", at_cap), _slimmer(be_c))
    o_d = _sync(_tool_msg("attribution_analyze", "x" * (cap + 1)), _slimmer(be_d))
    check(o_c.content == at_cap and be_c.writes == {}, f"正好 {cap} 字符 → 免截断（含边界）")
    check(_truncated(o_d) and len(be_d.writes) == 1, f"{cap + 1} 字符 → 仍落盘兜底")

    # 显式 None = 不设上限
    be_e = FakeBackend()
    mw = MessageSlimmerMiddleware(backend=be_e, max_chars_before_truncate=8000,
                                  chart_exempt_max_chars=60000,
                                  attribution_exempt_max_chars=None)
    o_e = _sync(_tool_msg("attribution_analyze", "x" * (cap + 1)), mw)
    check(o_e.content == "x" * (cap + 1) and be_e.writes == {},
          "构造参数传 None → 归因结果永不落盘（不设上限）")

    # 异步路径（子 agent 走 async）：两条路径都得有这条分支，否则一半场景回退
    async def run() -> None:
        be_f, be_g = FakeBackend(), FakeBackend()
        ok = await _async(_tool_msg("attribution_analyze", _ATTR_PAYLOAD), _slimmer(be_f))
        rd = await _async(_tool_msg("read_file", _ATTR_PAYLOAD), _slimmer(be_g))
        check(ok.content == _ATTR_PAYLOAD and be_f.writes == {},
              "async：归因结果原样保留、不写盘")
        check(_truncated(rd) and len(be_g.writes) == 1, "async：非归因工具仍落盘")

    asyncio.run(run())

    # 接线闸：同步 / 异步**各**有一条分支（只挂一条 = 一半场景静默回退）
    src = (_SRC / "agent/middlewares/message_slimmer.py").read_text(encoding="utf-8")
    check(src.count("if self._is_attribution_exempt(message, content_str):") == 2,
          "同步 + 异步两条处理路径都接了归因豁免",
          src.count("if self._is_attribution_exempt(message, content_str):"))
    check("_ATTRIBUTION_EXEMPT_TOOL_SUFFIXES" in src
          and "_KNOWLEDGE_EXEMPT_TOOL_SUFFIXES" in src,
          "归因白名单是独立常量（没改写知识料那条）")

    # 提示词闸：工具挂在**主 agent** 上（`main_agent.py`），玩法必须写进主 agent 的提示词。
    # ⚠️ 写进 NL2SQL_SYSTEM_PROMPT.md 是**无效**的 —— 那是子 agent 的提示词，而子 agent
    # 的工具面里根本没有 attribution_analyze。
    main_prompt = (_SRC / "agent/prompt/MAIN_AGENT_PROMPT.md").read_text(encoding="utf-8")
    check("attribution_analyze" in main_prompt, "主 agent 提示词列出 attribution_analyze")
    check("## 归因分析 playbook" in main_prompt, "主 agent 提示词有归因 playbook 小节")
    check("不解释" in main_prompt and "因果" in main_prompt,
          "playbook 明写 T3 红线（只做分解、不解释为什么）")
    check("分次调用" in main_prompt, "playbook 写明跨 cube 分次调用")
    check("不要从聊天记录里猜" in main_prompt, "playbook 禁止猜日期/指标名")
    check("ATTRIBUTION_DISABLED" in main_prompt,
          "playbook 说明开关关闭是按设计的拒绝、不要重试")
    nl2sql_prompt = (_SRC / "agent/prompt/NL2SQL_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    check("attribution_analyze" not in nl2sql_prompt,
          "子 agent 提示词**不提**本工具（提了就是教它调一个不在工具面里的东西）")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    print(f"源码根 = {_SRC}")

    t1_name_matching()
    t2_knowledge_exempt()
    t3_negative_control()
    t4_cap()
    t5_async_parity()
    t6_fail_open()
    t7_wiring()
    t8_prompt_and_skill()
    t9_attribution_exempt()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
