# -*- coding: utf-8 -*-
"""Langfuse 展示 skill 归属表（`_TOOL_OWNER_SKILLS`）与真实技能目录一致性验证（离线）。

**要关的口子**：生产 trace 里的 span 名出现 `skill:nl2sql-understand:wrenai_witops_list_knowledge`，
而盘上**没有**任何 `nl2sql-understand` 目录、Langfuse 里的系统提示词也全是 `wren-*`。
⇒ 这个名字 100% 由 `langfuse_span.py` 里硬编码的 `_TOOL_OWNER_SKILLS` 表产生，且走的是
「唯一归属 → 直接 return owners[0]」那条早退分支（`_resolve_display_skill` 优先级 ③）。

危害不是"名字难看"，而是**同一个技能被拆成两个 tag**：read_file 命中 SKILL.md 走的是
`_skill_name_from_path`（返回**真实目录名** wren-retrieve），其它工具走本表（返回旧名
nl2sql-understand）⇒ `skill:wren-retrieve:read_file` 与 `skill:nl2sql-understand:list_knowledge`
在 Langfuse 里是**两个不同 skill**，按 skill 过滤/聚合、离线实验的 skill 维度分组全被拆开。

**本脚本断言**（全部离线，不需要 LLM/MCP/数据库/网络）：
  ① 表里每个 owner 都是盘上真实存在的技能目录名（防拼写漂移 / 防旧名残留）；
  ② 表里不得出现旧名词汇（sql-of-thought / nl2sql-* 七件套）；
  ③ 生产实测那条调用解析出 `wren-retrieve`（把旧表当负对照，证明断言真有区分度）；
  ④ 唯一归属覆盖过期活动 skill；共享工具只在 owner 内才继承；
  ⑤ 每个技能目录的 frontmatter `name:` == 目录名（另一个"两种 tag"来源）；
  ⑥ 表里每个工具名都在 `TOOL_SKILL_MAP` 里（否则回退启发式时得 None ⇒ 整条 span 被跳过）；
  ⑦ 提示词 / 各 SKILL.md 里的技能名清单与盘上目录一致，且旧名不残留。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_skill_owner_table.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile

# import 链上的 agent.* 可能读 AGENT_DATA_ROOT ⇒ 先钉一个临时根，避免碰真实数据目录
# （沿用仓内 verify 脚本的约定）。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-owner-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.middlewares.langfuse_span import (  # noqa: E402
    TOOL_SKILL_MAP,
    _TOOL_OWNER_SKILLS,
    _resolve_display_skill,
    _skill_name_from_path,
    _tool_owners,
)

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


SKILLS_DIR = _SRC / "agent" / "shared" / "skills" / "nl2sql"
PROMPT_MD = _SRC / "agent" / "prompt" / "NL2SQL_SYSTEM_PROMPT.md"

# 更名前（sql-of-thought 系）的词汇：盘上已无对应目录，出现即在污染 tag。
OLD_NAMES = (
    "sql-of-thought",
    "nl2sql-understand",
    "nl2sql-execution",
    "nl2sql-sql-generation",
    "nl2sql-correction",
    "nl2sql-performance-optimization",
    "nl2sql-clarification",
    "nl2sql-schema-linking",
    "nl2sql-subproblem",
    "nl2sql-query-plan",
)

# 更名前那张表：仅用于负对照——证明 ③ 的断言不是"怎么写都过"。
_LEGACY_TABLE = {
    "list_models": ("sql-of-thought",),
    "list_cubes": ("sql-of-thought",),
    "describe_cube": ("sql-of-thought",),
    "query_cube": ("sql-of-thought",),
    "run_sql": ("nl2sql-execution",),
    "dry_plan": ("nl2sql-sql-generation",),
    "get_context": ("nl2sql-understand",),
    "get_instructions": ("nl2sql-understand",),
    "get_all_knowledge": ("nl2sql-understand",),
    "list_knowledge": ("nl2sql-understand",),
    "recall_queries": ("nl2sql-understand",),
    "describe_schema": ("nl2sql-understand",),
    "describe_model": ("nl2sql-understand",),
    "get_data_source": ("nl2sql-understand",),
    "get_mdl": ("nl2sql-understand",),
    "get_db_info": ("nl2sql-understand",),
}


def _dirs() -> list[str]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def _display(tool_name: str, thread_id: str = "", heuristic: str = "heuristic-x") -> str:
    return _resolve_display_skill(tool_name, {}, thread_id, heuristic)


def _activate(thread_id: str, skill: str) -> None:
    """用**真实机制**把活动 skill 设成 `skill`：deepagents 渐进披露要求模型执行某
    skill 前先 read_file 其 SKILL.md，`_resolve_display_skill` 就是从这个调用里记下
    目录名的。直接往 thread_id 里塞 skill 名是无效的（活动 skill 按 thread_id 存，
    与 thread_id 的取值无关）——第一版断言就栽在这里。"""
    p = f"/shared/skills/nl2sql/{skill}/SKILL.md"
    got = _resolve_display_skill("read_file", {"file_path": p}, thread_id, "artifact-read")
    assert got == skill, f"活动 skill 未生效：{got} != {skill}"


# ── ① owner 都是真实目录名 ───────────────────────────────────────────────────
def t1_owners_are_real_dirs() -> None:
    print("\n① 归属表的 owner 全部是真实技能目录名")
    real = set(_dirs())
    check(bool(real), "取到真实技能目录清单", sorted(real))

    owners = {o for v in _TOOL_OWNER_SKILLS.values() for o in v}
    missing = sorted(owners - real)
    check(not missing, "表里每个 owner 都有同名目录", missing)
    check(all(v for v in _TOOL_OWNER_SKILLS.values()), "没有空 owner 元组")
    check(len(owners) >= 5, "owner 覆盖的技能不止一两个", len(owners))


# ── ② 旧名词汇不残留 ────────────────────────────────────────────────────────
def t2_no_legacy_names() -> None:
    print("\n② 旧名词汇（sql-of-thought 系）已清空")
    owners = {o for v in _TOOL_OWNER_SKILLS.values() for o in v}
    hit = sorted(n for n in owners if any(n.startswith(p) for p in OLD_NAMES))
    check(not hit, "owner 里没有旧名", hit)
    # 表里也不该出现旧名（含注释外的键值）
    leftover = sorted(
        n for n in re.findall(r'"(nl2sql-[a-z-]+|sql-of-thought)"', str(_TOOL_OWNER_SKILLS))
    )
    check(not leftover, "整张表文本里没有旧名", leftover)


# ── ③ 生产实测那条调用 ──────────────────────────────────────────────────────
def t3_production_case() -> None:
    print("\n③ 生产 trace 那条 span（skill:nl2sql-understand:wrenai_witops_list_knowledge）")
    got = _display("wrenai_witops_list_knowledge")
    check(got == "wren-retrieve", "list_knowledge 展示为 wren-retrieve", got)

    # 负对照：同一调用走旧表必须得出旧名 ⇒ 证明断言有区分度
    legacy = _LEGACY_TABLE.get("list_knowledge")
    check(
        legacy is not None and legacy[0] != got,
        "负对照：旧表对同一工具给出的是旧名（断言非恒真）",
        legacy,
    )

    # 唯一归属必须覆盖"过期活动 skill"（模型读完 wren-clarify 就直接跑流水线）
    _activate("t-clarify", "wren-clarify")
    check(
        _display("wrenai_witops_list_knowledge", thread_id="t-clarify") == "wren-retrieve",
        "活动 skill 是 wren-clarify 时，唯一归属仍覆盖它（生产事故的形态）",
        _display("wrenai_witops_list_knowledge", thread_id="t-clarify"),
    )


# ── ④ 唯一归属 vs 共享 ──────────────────────────────────────────────────────
def t4_unique_and_shared() -> None:
    print("\n④ 唯一归属覆盖过期活动 skill；共享工具只在 owner 内继承")
    retrieve_family = (
        "get_context", "get_instructions", "get_all_knowledge", "list_knowledge",
        "recall_queries", "describe_schema", "list_cubes", "get_mdl", "describe_model",
        "get_data_source", "get_db_info", "describe_cube",
    )
    for bare in retrieve_family:
        for name in (bare, f"wrenai_witops_{bare}"):
            got = _display(name)
            if got != "wren-retrieve":
                check(False, f"{name} → wren-retrieve", got)
                break
    check(True, f"取料/探查族 {len(retrieve_family)} 个工具（裸名 + wrenai_ 前缀）全归 wren-retrieve")

    check(_display("wrenai_witops_query_cube") == "wren-metric-query", "query_cube → wren-metric-query", _display("wrenai_witops_query_cube"))
    check(_display("wrenai_witops_dry_plan") == "wren-sql-author", "dry_plan → wren-sql-author", _display("wrenai_witops_dry_plan"))
    check(_display("wrenai_witops_run_sql") == "wren-execution", "run_sql → wren-execution", _display("wrenai_witops_run_sql"))
    _activate("t-orchestrator", "wren-orchestrator")
    check(
        _display("wrenai_witops_run_sql", thread_id="t-orchestrator") == "wren-execution",
        "run_sql 唯一归属压过「活动 skill=编排器」",
    )

    # 共享工具 dry_run：owner 内继承，owner 外回退启发式
    check(_tool_owners("dry_run") == _TOOL_OWNER_SKILLS["dry_run"], "dry_run 是共享工具（>1 owner）")
    bad: list[str] = []
    for owner in _TOOL_OWNER_SKILLS["dry_run"]:
        tid = f"t-shared-{owner}"
        _activate(tid, owner)
        got = _display("wrenai_witops_dry_run", thread_id=tid)
        if got != owner:
            bad.append(f"{owner}→{got}")
    check(not bad, "dry_run 在 owners 内继承活动 skill", bad)

    # 先把活动 skill 设成 owner 外的（wren-clarify），再调 dry_run → 必须回退启发式
    stale = "wren-clarify"
    check(stale not in _TOOL_OWNER_SKILLS["dry_run"], "负对照用的 skill 确实不是 dry_run 的 owner")
    _activate("t-shared-fallback", stale)
    got = _display("wrenai_witops_dry_run", thread_id="t-shared-fallback", heuristic="sql-generation")
    check(got == "sql-generation", "活动 skill 不属于 dry_run 时回退启发式（不张冠李戴）", got)


# ── ⑤ 目录名 == frontmatter name ────────────────────────────────────────────
def t5_dir_matches_frontmatter() -> None:
    print("\n⑤ 技能目录名 == SKILL.md frontmatter name（另一个「两种 tag」来源）")
    bad: list[str] = []
    for name in _dirs():
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r"^name:\s*(\S+)\s*$", text, re.M)
        if not m or m.group(1) != name:
            bad.append(f"{name}→{m.group(1) if m else '?'}")
    check(not bad, "每个目录名与 name 字段一致", bad)

    # read_file 命中 SKILL.md 时提取的就是目录名，须与 owner 表口径一致
    parsed = _skill_name_from_path("/shared/skills/nl2sql/wren-retrieve/SKILL.md")
    check(parsed == "wren-retrieve", "_skill_name_from_path 返回目录名", parsed)
    parsed_main = _skill_name_from_path("/shared/skills/main/chart-saver/SKILL.md")
    check(parsed_main == "chart-saver", "main 侧技能同样返回目录名", parsed_main)


# ── ⑥ owner 表 ⊂ 启发式表 ───────────────────────────────────────────────────
def t6_owner_subset_of_heuristic() -> None:
    print("\n⑥ 归属表里的工具都在启发式表里（否则回退时得 None ⇒ 整条 span 被跳过）")
    missing = sorted(k for k in _TOOL_OWNER_SKILLS if k not in TOOL_SKILL_MAP)
    check(not missing, "没有只在 owner 表里的工具", missing)
    # 反向：启发式表里那些进入 _maybe_score 分支的查询工具必须在 owner 表里（否则展示回退泛化名）
    query_tools = ("run_sql", "query_cube", "dry_run", "dry_plan")
    missing2 = sorted(t for t in query_tools if t not in _TOOL_OWNER_SKILLS)
    check(not missing2, "查询类工具都有真实 skill 归属", missing2)


# ── ⑦ 提示词 / 技能文本一致性 ──────────────────────────────────────────────
def t7_texts_consistent() -> None:
    print("\n⑦ 提示词与技能文本里的技能名清单")
    prompt = PROMPT_MD.read_text(encoding="utf-8")
    dirs = _dirs()
    # 提示词的「技能名称列表」那一段
    line = next((ln for ln in prompt.splitlines() if "技能名称列表" in ln), "")
    check(bool(line), "提示词里有技能名称列表")
    missing = [d for d in dirs if d != "wren-writeback" and f"`{d}`" not in line]
    check(not missing, "§四 技能名称列表覆盖全部会话内技能", missing)
    check("`wren-execution`" in line, "新增的 wren-execution 已登记进技能名称列表")
    check("`wren-writeback`" in line, "wren-writeback（循环外）也登记了")

    hit = sorted(n for n in OLD_NAMES if n in prompt)
    check(not hit, "提示词里没有旧技能名", hit)

    # wren-orchestrator 的能力清单 + 六步流程
    orch = (SKILLS_DIR / "wren-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
    missing2 = [d for d in dirs if d not in orch]
    check(not missing2, "wren-orchestrator 技能清单覆盖全部技能目录", missing2)
    check("wren-execution" in orch, "编排器流程写了 wren-execution")

    # 全 nl2sql 技能 + main 技能 + 提示词：旧名必须绝迹
    scan_dirs = [SKILLS_DIR, SKILLS_DIR.parent / "main"]
    hits: list[str] = []
    for base in scan_dirs:
        for p in base.rglob("SKILL.md"):
            text = p.read_text(encoding="utf-8")
            for n in OLD_NAMES:
                if n in text:
                    hits.append(f"{p.relative_to(_ROOT)}:{n}")
    check(not hits, "所有 SKILL.md 里都没有旧技能名", hits)

    # wren-execution 的落盘/行数契约与其归属（run_sql）对得上
    exe = (SKILLS_DIR / "wren-execution" / "SKILL.md").read_text(encoding="utf-8")
    check("run_sql" in exe, "wren-execution 确实讲的是 run_sql")
    check("LIMIT" in exe and "limit" in exe, "wren-execution 写了行数契约")
    fm = re.search(r"^name:\s*(\S+)\s*$", exe, re.M)
    check(fm is not None and fm.group(1) == "wren-execution", "wren-execution frontmatter name 正确", fm.group(1) if fm else None)
    check(exe.count("---") >= 2, "wren-execution 有 frontmatter 围栏（否则 skills loader 不认）")


# ── ⑧ 模型看得见的字符串里不点名不存在的技能 ────────────────────────────────
def t8_model_facing_strings() -> None:
    """比 owner 表更容易漏的两处：注入进 system prompt 的协议文本、以及作为错误
    信息回给模型的指导文本。里面点名一个不存在的 skill，模型会去 read_file 它。"""
    print("\n⑧ 模型可见字符串里的技能名")
    from agent.middlewares.query_gate import _HINT
    from agent.middlewares.write_todos import WRITE_TODOS_PROTOCOL

    for label, text in (("QueryGate._HINT", _HINT), ("WRITE_TODOS_PROTOCOL", WRITE_TODOS_PROTOCOL)):
        hit = sorted(n for n in OLD_NAMES if n in text)
        check(not hit, f"{label} 里没有旧技能名", hit)

    check("wren-retrieve" in _HINT, "QueryGate 的指导文本点名了真实 Skill 名（wren-retrieve）")
    check(
        "wren-retrieve" in WRITE_TODOS_PROTOCOL,
        "write_todos 协议里的前段技能名指向真实 Skill（wren-retrieve）",
    )

    # progress_boundary 的别名表要与协议文本里的阶段名兼容——改协议步名必须同改别名，
    # 否则确定性推进静默退化成「不动」。这里只锁住两处被协议文本用到的 token。
    from agent.middlewares.progress_boundary import (
        _EXEC_PHASE_ALIASES,
        _SCHEMA_PHASE_ALIASES,
        _matches_any,
    )

    check(
        _matches_any("Schema 提取与裁剪", _SCHEMA_PHASE_ALIASES),
        "协议里的 schema 阶段名仍被 progress_boundary 别名覆盖",
    )
    check(
        _matches_any("查询执行", _EXEC_PHASE_ALIASES),
        "协议里的执行阶段名仍被 progress_boundary 别名覆盖",
    )


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    print(f"技能目录 = {SKILLS_DIR}")
    print(f"技能清单 = {_dirs()}")

    t1_owners_are_real_dirs()
    t2_no_legacy_names()
    t3_production_case()
    t4_unique_and_shared()
    t5_dir_matches_frontmatter()
    t6_owner_subset_of_heuristic()
    t7_texts_consistent()
    t8_model_facing_strings()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
