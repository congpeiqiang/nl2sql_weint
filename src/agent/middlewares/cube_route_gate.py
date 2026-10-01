"""CubeRouteGateMiddleware — cube 盖得到的聚合题必须走 `query_cube`（每线程一次软门）。

背景（2026-09-30，trace ``9afcd8ea`` / 会话 ``01a0f185``，库 `witops`）：
问题「今年 8 月和 9 月新增和完成缺陷」——`bug_quality` cube **完全覆盖**它
（`bug_count=COUNT(*)`、`finished_count=SUM(CASE WHEN status_code=2 THEN 1 ELSE 0 END)`，
`mention_time`/`complete_time` 都带 month 级，`base_object: v_bug`），模型也**确实**
`describe_cube(name="bug_quality")` 看过它，却**一次 `query_cube` 都没调**，手写了 5 段
`LEFT JOIN ... FROM v_bug ... GROUP BY` 直接 `run_sql`，报告那行落成
「模型依据语义库 schema 手写（未使用 Cube 具名指标）」。

两侧都查过，**这不是模型跑偏，是成文策略允许的**：
``skills/nl2sql/wren-metric-query/SKILL.md`` 写着「多时间轴对比 → 转手写」、
``wren-orchestrator/SKILL.md`` 写着「`query_cube` 是软偏好不是独占车道」，而这道题
恰好要两条时间轴（新增按 `mention_time`、完成按 `complete_time`），正落在豁免里。
所以本中间件**不替代**那次政策改写（提示词 / SKILL.md 同一轮一起收紧），它是
**提示词之外的确定性兜底** —— 文本指引模型不遵守时（`QueryGateMiddleware` 的
docstring 已实证过一次），这里能把它拽回 cube 车道。

规则（三条全中才拦）：
  ① 目标是 `*_run_sql`（含 `run_sql`），**排除** `dbmcp_*`——已建模库上的直连通道
     由 `QueryGateMiddleware` 的通道硬闸兜住，且它压根不经语义层；
  ② SQL 里抽出的**关系集合非空且完全落在某个 cube 的 `base_object` 上**；
  ③ SQL 含聚合函数或 `GROUP BY`。
明细行级 / 窗口函数 / 跨 cube 关联 / 引用底座之外的表 ⇒ 关系集合不 ⊆ 单底座 ⇒ 放行。

软 / 防打扰设计（照抄 `query_gate` 那一套）：
- **每线程至多拦一次**，且用**独立**的 `_CUBE_GATE_REMINDED` 命名空间（**不与** QueryGate
  的 `_REMINDED` 共用——共用会让两个软门互相吃掉提醒机会）。这一次就是**逃生口**的
  机制保证：文案里写明「cube 确实表达不了就直接原样重发」，重发必放行。
- 取不到 db_name / 项目路径 / cube 元信息 → 一律 fail-open 放行（宁可漏拦，不可误拦）。
- `NL2SQL_CUBE_ROUTE_GATE=off` ⇒ 全放行（一键回退，与 `NL2SQL_KNOWLEDGE_TRIM` 同款）。
- 拦下的是「合法的单底座手写聚合查询」时只浪费一轮（模型读指导后重发即放行）。

``status="error"`` + `Error:` 前缀让 langfuse_span._maybe_score 把它当 exec 失败
（`looks_like_exec_error`，与 SqlReadOnly._deny / QueryGate 同款）——⚠️ 观测口径：
执行失败率会小幅抬升，**别误判成回归**。

挂载：``nl2sql_agent._middleware``，紧挨 `QueryGateMiddleware()`（LangfuseSpan 内层 →
被拦的 run_sql 仍在 span 里可见；SqlReadOnly 硬闸外层 → 写/DDL 仍由硬闸兜底）。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

# 线程标识解析复用 QueryGate 那一份（同层中间件、同一套 runtime/configurable 取法）。
# 各自维护一份必然漂移，而漂移的后果是「软门忽然不再触发」这种**静默**回归；顶层
# import 还能让改名在启动时就炸出来。query_gate 顶层只有 stdlib + langchain，无环。
from agent.middlewares.query_gate import _thread_id, _tool_args, _tool_name

_logger = logging.getLogger(__name__)

_ENV_GATE = "NL2SQL_CUBE_ROUTE_GATE"

# 直连通道前缀：已建模库上由 QueryGate 的通道硬闸拦，未建模库没有 cube 可谈。
_DBMCP_PREFIX = "dbmcp_"

# SQL → 被引用的关系名。首字符刻意不允许 `(` / 空格：`FROM (SELECT …)` 这种**派生表**
# 天然匹配不上，正合需要——子查询里的 `FROM v_bug` 会被同一条正则独立命中（真事故
# SQL 的 4 段子查询都取自 `v_bug`，正是靠这一条）。段可带反引号/双引号（MySQL 惯用
# ``FROM `db`.`v_bug` ``）：整条捕获后统一剥引号再取最后一段。
# ⚠️ 已知保守的不精确：正则不认字符串字面量，`WHERE note='from x'` 会把 `x` 当成关系。
# 要命中的前提是它与某个 cube 底座**逐字同名**，且后果只是每线程一次软提醒、可原样
# 重发绕开 —— 为此换一个 SQL 解析器（本仓没有声明 `sqlglot` 依赖）不划算。
_REL_RE = re.compile(
    r"\b(?:from|join)\s+([`\"]?[\w$]+[`\"]?(?:\s*\.\s*[`\"]?[\w$]+[`\"]?)*)",
    re.IGNORECASE,
)
_AGG_RE = re.compile(r"\b(?:count|sum|avg|min|max)\s*\(", re.IGNORECASE)
_GROUP_RE = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)

# 进程内「已提醒过」去重（线程随 run 复用，FIFO 上限防泄漏）。独立命名空间，
# 见模块 docstring「不与 QueryGate 共用」。
_CUBE_GATE_REMINDED: set[str] = set()
_REMINDED_LOCK = threading.Lock()
# 两个进程内结构共用的上限（提醒集合按线程、索引按项目，两者都是「本进程见过的
# 小规模字符串集合」，超限整体清空即可，不需要 LRU）。
_CAP = 4000

# 「cube 底座 → [(cube 名, [measure 名])]」索引，按 <cubes 目录 mtime> 缓存。
_INDEX_CACHE: dict[str, tuple[float, dict]] = {}
_INDEX_LOCK = threading.Lock()

# 错误文案里列多少条 measure（bug_quality 有 21 个；全列可读性反而下降）。
_MAX_MEASURES = 24


def _enabled() -> bool:
    """开关：默认开；`off/0/false/no` 一律放行（一键回退）。"""
    raw = str(os.environ.get(_ENV_GATE, "on")).strip().lower()
    return raw not in ("off", "0", "false", "no")


def _relations(sql: str) -> set[str]:
    """SQL 里被 FROM/JOIN 引用的关系名集合（剥反引号/双引号、取最后一段、小写）。"""
    out: set[str] = set()
    for raw in _REL_RE.findall(sql or ""):
        # `` `db`.`v_bug` `` → `db.v_bug`：引号可按段出现，也可能带空格，一并剥净。
        name = re.sub(r"[`\"]", "", str(raw)).replace(" ", "").lower()
        if not name:
            continue
        out.add(name.rsplit(".", 1)[-1])  # db.v_bug → v_bug
    return out


def _has_aggregate(sql: str) -> bool:
    return bool(_AGG_RE.search(sql or "") or _GROUP_RE.search(sql or ""))


def _current_db() -> str:
    """当前库（`configurable.db_name`）；取不到返回 ""（调用方据此放行）。

    与 `caliber_gate._current_db_name` 同款——**不能用** `request.runtime.config`
    （模型中间件里那个恒空，见 memory `model-request-runtime-config-empty`）。
    """
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str((cfg.get("configurable") or {}).get("db_name", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _cube_index(project_path: str) -> dict:
    """`base_object` → [(cube 名, [measure 名])]。读不到一律空 dict（fail-open）。

    缓存键是 `<cubes 目录>` 的 mtime：新增/删除 cube、改 `metadata.yml` 都会改目录
    mtime（改文件内容改的是**目录项**时间戳），足以让缓存失效，不必逐文件 stat。
    """
    if not project_path:
        return {}
    cubes_dir = Path(str(project_path)) / "cubes"
    try:
        mtime = cubes_dir.stat().st_mtime
    except OSError:
        return {}  # 目录不存在/不可读 ⇒ 这库没 cube 可谈
    with _INDEX_LOCK:
        hit = _INDEX_CACHE.get(project_path)
    if hit and hit[0] == mtime:
        return hit[1]

    index: dict[str, list] = {}
    try:
        from agent.utils.wren_call_extract import list_cubes, load_cube_metadata

        for dir_name in list_cubes(project_path):
            meta = load_cube_metadata(project_path, dir_name)
            base = str(meta.get("base_object") or "").strip().lower()
            if not base:
                continue  # 没声明底座的 cube 无从比对，跳过（不猜）
            cube = str(meta.get("cube_name") or dir_name)
            index.setdefault(base.rsplit(".", 1)[-1], []).append(
                (cube, sorted(meta.get("measures") or {}))
            )
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.warning("[cube_route_gate] 建 cube 索引失败（%s）：%s", project_path, e)
        return {}
    with _INDEX_LOCK:
        if len(_INDEX_CACHE) > _CAP:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[project_path] = (mtime, index)
    return index


def _matched_cubes(rels: set[str], index: dict) -> list[tuple[str, str, list[str]]]:
    """关系集合**完全**落在某底座上 ⇒ 返回 ``[(底座, cube 名, [measure 名])]``。

    刻意不做「部分命中」：跨 cube / 底座之外还有别的表 ⇒ 手写是合理的（cube 表达
    不了跨源关联），此时放行。按 (底座, cube) 排序 ⇒ 命中多个同底座时取
    `[0]` 是确定的（同一份 SQL 每次给同一张 cube，不会来回跳）。
    """
    out: list[tuple[str, str, list[str]]] = []
    for base, items in index.items():
        if rels <= {base}:
            out.extend((base, cube, measures) for cube, measures in items)
    return sorted(out)


def _mark_reminded(tid: str) -> bool:
    """返回 True=本轮应拦；False=已提醒过/无需再拦。"""
    with _REMINDED_LOCK:
        if tid in _CUBE_GATE_REMINDED:
            return False
        if len(_CUBE_GATE_REMINDED) > _CAP:
            _CUBE_GATE_REMINDED.clear()
        _CUBE_GATE_REMINDED.add(tid)
        return True


def _hint(rels: set[str], cube: str, base: str, measures: list[str], prefix: str) -> str:
    """拦截文案：命中什么 → 该用什么 → 以及**逃生口**。三者缺一不可。"""
    shown = "、".join(measures[:_MAX_MEASURES]) or "（本 cube 未声明 measure）"
    more = f"（共 {len(measures)} 个）" if len(measures) > _MAX_MEASURES else ""
    call = f"{prefix}_query_cube" if prefix else "query_cube"
    return (
        f"Error: [cube_route_gate] 本次查询引用的关系（{'、'.join(sorted(rels))}）**完全落在 "
        f"Cube「{cube}」的底座 `{base}` 上**，且是聚合/分组查询 —— 该 Cube 已声明了它的"
        f"具名指标，属于 cube 车道，不允许用裸 SQL 重算。\n"
        f"「{cube}」的 measures（业务口径已定义，数字以这里为准）：{shown}{more}\n"
        f"正确姿势：调 `{call}(cube=\"{cube}\", measures=[\"<指标名>\"], "
        f"time_dimension=\"<时间维度>:<粒度>:<起>,<止>\")`。\n"
        f"⚠️ 一次调用只接受**一个** `time_dimension`：本题若要多条时间轴（如新增按 "
        f"`mention_time`、完成按 `complete_time`）就**分次调用、结果并列**，不要退回手写。\n"
        f"逃生口：如果 Cube 确实表达不了你要的口径（明细行级 / 窗口函数 / 跨 cube 关联 / "
        f"未声明维度），**把刚才那条 SQL 原样重发一次即可** —— 本线程不会再拦。"
    )


class CubeRouteGateMiddleware(AgentMiddleware):
    """`*_run_sql` 上「单底座 + 聚合」的手写 SQL → 拦一次并指到 `query_cube`。"""

    def _gate(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> Any:
        try:
            return self._gate_inner(request, handler)
        except Exception as e:  # noqa: BLE001  软门绝不因自身缺陷中断查询
            _logger.warning("[cube_route_gate] 判定失败，放行：%s", e)
            return handler(request)

    def _gate_inner(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> Any:
        if not _enabled():
            return handler(request)
        name = _tool_name(request)
        # 只拦 wren 语义层的执行工具：`dbmcp_*` 是直连通道（未建模库的合法通道，
        # 且已建模库上已由 QueryGate 通道硬闸拦掉），Cube 通道本身当然放行。
        if not (name == "run_sql" or name.endswith("_run_sql")):
            return handler(request)
        if name.startswith(_DBMCP_PREFIX):
            return handler(request)

        sql = _tool_args(request).get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return handler(request)  # 参数缺失/异常 → 由工具自己报错
        rels = _relations(sql)
        if not rels or not _has_aggregate(sql):
            return handler(request)  # 明细查询 / 抽不出关系 ⇒ 不在本门管辖

        db = _current_db()
        if not db:
            return handler(request)
        try:
            from agent.utils.semantic_db import get_detector, wrenai_server_name

            detector = get_detector()
            project = detector.project_path_for(db) or ""
            prefix = wrenai_server_name(db)
        except Exception as e:  # noqa: BLE001
            _logger.info("[cube_route_gate] 库 %s 语义层上下文取不到，放行：%s", db, e)
            return handler(request)
        if not project:
            return handler(request)  # 未建模库 ⇒ 没有 cube 可言

        hits = _matched_cubes(rels, _cube_index(project))
        if not hits:
            return handler(request)

        tid = _thread_id(request)
        if not tid:
            return handler(request)  # 取不到线程标识 ⇒ fail-open（与 query_gate 一致）
        if not _mark_reminded(tid):
            return handler(request)  # 已提醒过一次 → 放行（软门 + 逃生口）

        base, cube, measures = hits[0]
        tool_call_id = (
            getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""
        )
        _logger.info(
            "[cube_route_gate] 拦截单底座手写聚合 SQL（库 %s / cube %s / 关系 %s / 线程 %s，"
            "软提醒一次）",
            db, cube, sorted(rels), tid[:8],
        )
        return ToolMessage(
            content=_hint(rels, cube, base, measures, prefix),
            name=name,
            tool_call_id=tool_call_id,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        return self._gate(request, handler)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        result = self._gate(request, handler)
        if hasattr(result, "__await__"):
            return await result
        return result


__all__ = [
    "CubeRouteGateMiddleware",
    "_CUBE_GATE_REMINDED",
    "_ENV_GATE",
    "_cube_index",
    "_enabled",
    "_has_aggregate",
    "_hint",
    "_matched_cubes",
    "_relations",
]
