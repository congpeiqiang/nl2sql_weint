# -*- coding: utf-8 -*-
"""「拒绝不在可用清单里的工具」文案 + 知识轴主路的契约验证（离线，无需后端/MCP/网络）。

**要关的两个口子**（2026-09-26 生产事故：用户看到「`get_all_knowledge` 本轮不可用」）

1. **文案在撒谎**：`DynamicMCPToolsMiddleware._resolve` 原先返回的 `_REMOVED_HINT` 断言
   「它所属的数据库/语义库已被管理员删除或重新关联」。而 `_resolve` 唯一能证明的是
   「这个名字不在已预热的注册表里」——至少有三种成因（库被删/改名、**本部署的 wren
   不提供该工具**、名字拼写漂移）。这条消息是喂给模型的 `ToolMessage`，模型会把它当
   事实**转述给用户**，于是用户以为语义库被人删了。
   真因已核实：`get_all_knowledge` **从未进过任何 wrenai 发布版**（PyPI 上 0.7.0rc1→0.15.0
   逐版本解析 `wren/mcp_server.py`：≤0.12.0 没有该文件；0.13.0~0.15.0 每版 `@mcp.tool`
   都是 18、全都没有这个工具名）。本机 `.venv` 里那份是被人就地加了 29 行才有的
   （与上游 wheel 逐行 diff 只差这一处插入），提示词当初是照着那份改过的副本写的。

2. **提示词在推荐一个不存在的工具**：§三 把它列为「四路并行取料」必备一路，于是模型
   每轮白调一次 → 每次都收到一条错误消息进上下文。现改为知识轴主路 `list_knowledge()`，
   `get_all_knowledge` 降为「工具清单里确实出现时才用」。

本脚本断言：文案只陈述可证事实且给出替代路径、替代查表命中/不误配、`_resolve` 的四条
分支行为一字不变（只换文案）、三处交付面的契约在位（防静默回退，与
`verify_report_caliber.py` 的「契约在位」用法同款）。

⚠️ 末尾那组只校验**本地文件**：线上提示词来自 Langfuse、运行期技能来自
`<AGENT_DATA_ROOT>/shared/skills`。本脚本证明不了线上已生效 —— 那要 trace
`metadata.prompt.prompt_versions` / 启动日志 `[langfuse] prompt … v<N> 生效`。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_dynamic_mcp_hint.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

# 与仓内 verify 脚本同款：先钉临时根，避免任何落盘写进真实工作区。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-mcphint-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from langchain_core.messages import ToolMessage  # noqa: E402
from langgraph.prebuilt.tool_node import ToolCallRequest  # noqa: E402

from agent.middlewares import dynamic_mcp_tools as dmt  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


# ── 桩：运行期注册表的三个查询函数 ────────────────────────────────────
class _Stub:
    """工具桩，只为拿到对象身份（`is` 比较）。"""

    def __init__(self, name: str) -> None:
        self.name = name


def _patch_registry(entries: dict, warmed: bool):
    """把 `mcp_tool` 里的注册表查询换掉；返回 restore()。

    ⚠️ `_resolve` 是**函数体内** import 这两个名字，所以必须 patch
    `agent.tools.mcp_tool` 模块上的属性，patch 本模块看见的名字无效。
    """
    from agent.tools import mcp_tool

    saved = (mcp_tool.lookup_sub_tool, mcp_tool.sub_registry_warmed)
    mcp_tool.lookup_sub_tool = lambda n: entries.get(n)
    mcp_tool.sub_registry_warmed = lambda: warmed
    return lambda: setattr(mcp_tool, "lookup_sub_tool", saved[0]) or \
        setattr(mcp_tool, "sub_registry_warmed", saved[1])


def _req(name: str, tool: object, call_id: str = "call-x") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": call_id},
        tool=tool,  # type: ignore[arg-type]
        state=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
    )


mw = dmt.DynamicMCPToolsMiddleware()

print("\n== A. 文案只陈述可证事实（F1 的核心） ==")
_H = dmt._REMOVED_HINT
check("已不可用" not in _H, "不再断言「已不可用」（旧措辞整句消失）", _H[:24])
check("已被管理员删除或重新关联" not in _H, "不再断言「已被管理员删除或重新关联」这一唯一成因")
check("当前不在可用工具清单里" in _H, "陈述可证事实：不在当前可用清单里")
check("可能的原因" in _H, "把成因写成「可能的原因」而非断言")
check("不提供这个工具" in _H, "列出「本部署不提供该工具」这一成因（本次事故的真因）")
check("不要再用它" in _H and "不要重试" in _H, "保留「本轮别重试」的指引")
check("wrenai_<库名>_*" in _H and "dbmcp_run_sql" in _H, "保留替代通道指引")

_rendered = _H.format(tool="wrenai_witops_get_all_knowledge", fallback=dmt._fallback_for("wrenai_witops_get_all_knowledge"))
check("wrenai_witops_get_all_knowledge" in _rendered, "format 后工具名已代入")
check("{" not in _rendered and "}" not in _rendered, "format 后无残留占位符")

print("\n== B. 替代做法查表（命中 + 不误配） ==")
_f1 = dmt._fallback_for("wrenai_witops_get_all_knowledge")
check("list_knowledge()" in _f1, "带前缀名 wrenai_<库>_get_all_knowledge → 建议 list_knowledge()", _f1[:44])
check("list_knowledge()" in dmt._fallback_for("get_all_knowledge"), "裸名 get_all_knowledge → 同上")
check("get_context" in dmt._fallback_for("wrenai_demo_describe_schema"), "describe_schema → 建议 get_context")
check("get_context" in dmt._fallback_for("wrenai_demo_get_mdl"), "get_mdl → 建议 get_context")
# 负对照：查不到就返回空串（文案仍然完整，绝不因查表失败丢消息、也不乱建议）
check(dmt._fallback_for("wrenai_demo_run_sql") == "", "负对照：run_sql 无替代建议 → 空串")
check(dmt._fallback_for("get_all_knowledge_v2") == "", "负对照：get_all_knowledge_v2 不误配 → 空串")
check(dmt._fallback_for("wrenai_x_my_get_all_knowledge_extra") == "", "负对照：后缀不匹配不误配 → 空串")
check(dmt._fallback_for("") == "", "负对照：空名字 → 空串")

print("\n== C. _resolve 四条分支行为不变（只换文案） ==")
_inst = _Stub("wrenai_witops_run_sql")
_stale = _Stub("wrenai_witops_run_sql")

# C1 已预热 + 不在注册表 + 是子工具 → 短路返回错误 ToolMessage
_restore = _patch_registry({}, warmed=True)
try:
    _r = mw._resolve(_req("wrenai_witops_get_all_knowledge", _stale))
finally:
    _restore()
check(isinstance(_r, ToolMessage), "已预热+名单外子工具 → 返回 ToolMessage（短路，不执行）")
check(getattr(_r, "status", "") == "error", "  status == error")
check(getattr(_r, "name", "") == "wrenai_witops_get_all_knowledge", "  name == 被调用的工具名")
check(getattr(_r, "tool_call_id", "") == "call-x", "  tool_call_id 与 AIMessage 严格配对")
check("list_knowledge()" in str(getattr(_r, "content", "")), "  错误消息里带上了具体替代做法")
check("已被管理员删除或重新关联" not in str(getattr(_r, "content", "")),
      "  错误消息里不再出现那句假因果（模型无从转述）")

# C2 非子工具 → 原样放行（同一对象）
_restore = _patch_registry({}, warmed=True)
try:
    _q = _req("chart_generate", _stale)
    _r2 = mw._resolve(_q)
finally:
    _restore()
check(_r2 is _q, "非子工具（chart_generate）→ 原样放行，不拦")

# C3 注册表未预热 → fail-open（绝不误杀健康工具）
_restore = _patch_registry({}, warmed=False)
try:
    _q3 = _req("wrenai_witops_run_sql", _stale)
    _r3 = mw._resolve(_q3)
finally:
    _restore()
check(_r3 is _q3, "注册表未预热 → 原样放行（fail-open）")

# C4a 注册表里有同一实例 → 原样放行
_restore = _patch_registry({"wrenai_witops_run_sql": _inst}, warmed=True)
try:
    _q4 = _req("wrenai_witops_run_sql", _inst)
    _r4 = mw._resolve(_q4)
finally:
    _restore()
check(_r4 is _q4, "名字在注册表且实例相同 → 原样放行")

# C4b 注册表里有**不同**实例 → override 成当前实例（重加载后静态列表里的那个是陈旧的）
_restore = _patch_registry({"wrenai_witops_run_sql": _inst}, warmed=True)
try:
    _r5 = mw._resolve(_req("wrenai_witops_run_sql", _stale))
finally:
    _restore()
check(getattr(_r5, "tool", None) is _inst, "重加载后 → override(tool=注册表当前实例)，不用陈旧实例")

# ── D. 三处交付面的契约在位（防静默回退）──────────────────────────────
print("\n== D. 知识轴主路 = list_knowledge（本地文件契约，证明不了线上） ==")
_PROMPT = _SRC / "agent" / "prompt" / "NL2SQL_SYSTEM_PROMPT.md"
_SKILL = _SRC / "agent" / "shared" / "skills" / "nl2sql" / "wren-retrieve" / "SKILL.md"
_AGENTS = _SRC / "agent" / "shared" / "memory" / "AGENTS.md"
_YAML = _SRC / "agent" / "subagents" / "configs" / "nl2sql.yaml"

_pt = _PROMPT.read_text(encoding="utf-8")
_sk = _SKILL.read_text(encoding="utf-8")
_ag = _AGENTS.read_text(encoding="utf-8")
_ym = _YAML.read_text(encoding="utf-8")

check("知识(list_knowledge)" in _pt, "提示词§三 四块料：知识轴写的是 list_knowledge")
check("知识(get_all_knowledge)" not in _pt, "提示词§三 不再把 get_all_knowledge 当必备一路")
check("| 知识面 | `list_knowledge()` |" in _pt, "提示词 §九 取料矩阵：知识面 = list_knowledge()")
check("上游 wren 0.15.0 及以前均未提供" in _pt, "提示词写明上游未提供（含版本区间，可被新版本证伪）")
check("确实出现时才" in _pt, "提示词把 get_all_knowledge 降为「清单里确实出现才用」")

# ⚠️ 与 verify_slimmer_exempt.py 同向的负例：`list_knowledge` 只给文件名，
# 说成「列出后按需读取」会把模型推回「按路径去读知识文件」——那条通道不存在
# （`knowledge/**` 不在可读 VFS 通道内，2026-09 已撞过 9 次 permission denied）。
check("按需读取" not in _pt, "负例：提示词不再出现「按需读取」这种教模型读文件的措辞")
check("只给文件名，不含正文" in _pt or "只返回**文件清单**" in _pt,
      "提示词写明 list_knowledge 只给清单、不给正文")
check("知识**正文**只有两条通道" in _pt, "提示词写明知识正文只有 get_instructions / recall_queries 两条通道")
check("都没有读取工具" in _pt, "提示词写明 metrics/glossary/caveats 正文无读取工具（不存在的通道不假装存在）")

check("| 知识面 | `list_knowledge()` |" in _sk, "SKILL.md 取料矩阵：知识面 = list_knowledge()")
check("按需读取" not in _sk, "负例：SKILL.md 不出现「按需读取」")
check("description:" in _sk.splitlines()[3] and "list_knowledge" in _sk.splitlines()[3],
      "SKILL.md frontmatter description 已换成 list_knowledge（技能索引/召回也看它）")
check("get_all_knowledge" not in _sk.splitlines()[3], "  description 里不再有 get_all_knowledge")

check("`get_instructions` / `list_knowledge`" in _ag, "AGENTS.md 四路取料：知识轴 = list_knowledge")
check("| 取料·知识轴 | `list_knowledge` |" in _ag, "AGENTS.md 工具表：取料·知识轴 = list_knowledge")
check("取料·知识轴 | `get_all_knowledge`" not in _ag, "  AGENTS.md 不再把 get_all_knowledge 当知识轴主路")
check("按需读取" not in _ag, "负例：AGENTS.md 不出现「按需读取」")

# 正向保留：白名单那行**不动** —— 匹配不到无害，上游哪天真发了就自动生效。
check("get_all_knowledge" in _ym, "nl2sql.yaml 白名单仍保留该名（上游将来提供即自动出现）")

print()
_passed = sum(1 for ok, _ in results if ok)
print(f"{_passed}/{len(results)} 通过" + ("" if _passed == len(results) else "   ← 有失败"))
sys.exit(0 if _passed == len(results) else 1)
