# -*- coding: utf-8 -*-
"""从消息历史里认领 wren 工具调用，并把 Cube 调用复算成 SQL 快照。

本模块是「Cube 快速通道的调用识别」与「wren 工具名 → 项目/连接」这两组能力的
**唯一实现**，被两处共用：

- ``agent.subagents.check_progress``（报告侧：查询定义节 + 物理 SQL 节）
- ``api.message_feedback``（反馈侧：点赞/差评的 SQL 快照）

**为什么要独立成模块**：``check_progress`` 在 import 时会执行 ``apply_patch()`` /
``apply_read_file_patch()`` 去改写 deepagents 的属性（见该文件尾），API 层不能为了
取一个 SQL 快照而连带跑那些补丁。

背景（为什么 Cube 通道需要「复算」这种东西）：``wrenai_<库名>_query_cube`` 的入参是
cube/measures/dimensions/filters，**结构上就没有 sql 字段**——SQL 由 wren 引擎在服务端
编译，MCP 只回 ``{columns, rows, row_count, truncated}``。于是任何「从 tool_calls 里捞
args.sql」的提取（如 ``api.message_feedback._extract_sql``）对 Cube 通道恒空：报告侧表现
为「执行 SQL」整节消失，反馈侧表现为点赞进不了 Good Set（实测生产 thread 01a0b2a8）。
本模块用 ``wren_plan.plan_cube_sql`` 在进程内把查询定义复算回可执行 SQL 补上这一环。

依赖刻意压到最小：顶层只有 stdlib；``semantic_db`` / ``mcp_tool`` / ``wren_plan`` 全部
函数内延迟 import（``wren_plan`` 顶层本身是纯 stdlib，``wren``/``wren_core`` 也是延迟
import）。任何一步失败都 fail-open 返回空值，**绝不抛**。
"""
from __future__ import annotations

import json
import logging

_logger = logging.getLogger(__name__)


# ── Cube 快速通道的「查询定义」──────────────────────────────────────
# wrenai_<库名>_query_cube 不产生 run_sql（Cube 语义层由 wren 引擎在服务端编译为
# 目标库 SQL 执行），_extract_last_sql 的 `"run_sql" in name` 过滤结构性取空 →
# 报告的「执行 SQL」节整节消失（实测同日同题：run_sql 通道 16087 字含 SQL 节，
# Cube 通道 8450 字零 SQL 字样）。MCP 只回 {columns, rows, row_count, truncated}，
# 拿不到编译后的语句，所以如实给查询定义，不伪造一条不存在的 SQL。
CUBE_TOOL_HINT = "query_cube"
CUBE_ARG_KEYS = (
    "cube", "dimensions", "measures", "filters", "time_dimension", "granularity",
    "segments", "order_by", "limit", "offset",
)


def _msg_content_str(m) -> str:
    """消息 content 归一化为纯文本（兼容 str / list[content-block]）。"""
    if isinstance(m, dict):
        c = m.get("content", "")
    else:
        c = getattr(m, "content", "") or ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text":
                parts.append(str(it.get("text", "")))
            else:
                parts.append(str(it))
        return "\n".join(parts)
    return str(c)


def msg_get(m, key, default=None):
    """消息取字段：``dict`` 与 LangChain 消息对象两种形态都要认。

    **为什么必须有这个**：同一个 ``messages`` 列表在本仓有**两种形态**——

    - 报告侧 ``check_progress`` 与反馈侧 ``message_feedback`` 读的是 LangGraph 客户端
      拿回的状态（HTTP/JSON）→ **dict**；
    - 离线实验 ``run_experiment`` 是进程内 ``ainvoke`` → **LangChain 消息对象**。

    本模块早期只写了 ``isinstance(m, dict)`` 分支，于是进程内那条路径上 Cube 调用
    **一个都认不出来**：``extract_last_cube_call`` 恒返回 ``{}``，而它对外全是
    fail-open，所以既没报错也没日志——表现为「报告/快照静默没有 SQL」而非抛异常。
    """
    if isinstance(m, dict):
        return m.get(key, default)
    return getattr(m, key, default)


def tool_result_error(m) -> bool:
    """工具结果消息是否**明确是失败**（判不出 → ``False``，调用方按老行为保留该调用）。

    失败形态（生产实测）：
    - langchain_mcp_adapters 把 MCP 错误包装成 ``Error executing tool <名>: …``
      （如 ``near 'LIMIT 201'`` / ``Unknown filter dimension 'story_count'``）；
    - ``sql_approval._deny`` 的只读拦截返回 ``status="error"`` 的 ToolMessage；
    - 少数路径回 ``{"error": …}`` 形态 JSON。

    **只把这三类当失败**：宁可少跳一次（把失败调用当成功），也不把成功调用误判为
    失败而丢掉产出数据的定义。
    """
    if isinstance(m, dict):
        if m.get("status") in ("error", "failed"):
            return True
    elif getattr(m, "status", None) in ("error", "failed"):
        return True
    text = (_msg_content_str(m) or "").strip()
    if not text:
        return False
    if text[:64].lower().startswith(("error executing tool", "error:")):
        return True
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    if isinstance(obj, dict):
        if obj.get("isError") is True:
            return True
        if "error" in obj and not any(
            k in obj for k in ("rows", "columns", "row_count")
        ):
            return True
    return False


def call_result_message(messages, idx: int, call_id, name: str):
    """第 ``idx`` 条消息里那次工具调用对应的结果消息；找不到返回 ``None``。

    先按 ``tool_call_id`` 精确对齐（同一条 AI 消息里并发多次调用时唯一可靠，
    如 trace 01a0a850 第 9 步一次发过两个 query_cube），再退回「其后第一条同名
    工具结果」——历史消息缺 id 时的兜底；此时同名的兄弟调用可能错配，最坏后果
    只是把失败调用当成成功（不丢数据、不伪造 SQL）。
    """
    fallback = None
    for j in range(idx + 1, len(messages)):
        m = messages[j]
        if isinstance(m, dict):
            role = m.get("role") or m.get("type")
            mname = m.get("name") or ""
            tcid = m.get("tool_call_id")
        else:
            role = getattr(m, "type", "")
            mname = getattr(m, "name", "") or ""
            tcid = getattr(m, "tool_call_id", None)
        if role not in ("tool", "tool_result"):
            continue
        if call_id and tcid and str(tcid) == str(call_id):
            return m
        if fallback is None and mname == name:
            fallback = m
    return fallback


def cube_arg_lines(name: str, args: dict) -> list:
    """Cube 调用 → 查询定义文本行（首行是工具名）。"""
    lines = [f"工具：{name}（wren 语义层 Cube 通道）"]
    for k in CUBE_ARG_KEYS:
        v = args.get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        lines.append(f"{k}: " + (v if isinstance(v, str)
                                 else json.dumps(v, ensure_ascii=False)))
    return lines


def extract_last_cube_call(messages) -> dict:
    """最后一次**成功**的 Cube 调用 ``{"tool", "args", "lines", "ok", "skipped_failed"}``。

    与 ``_cube_arg_lines`` 同一次扫描：报告侧既要展示查询定义文本，也要
    按 args **复算**物理 SQL（见 agent/utils/wren_plan），所以两者必须来自同一次
    调用——拆两个扫描循环迟早会漂移。

    为什么取「最后一次**成功**」（2026-09-16 修）：子 agent 常在拿到数据后继续试
    过滤/重算，失败的调用会成为最后一次。报告若锚在它上面，展示的是**没跑出任何
    数据**的定义，复算也必然同样失败（生产 thread 01a0a850：数据表来自第 12 步
    成功调用 110 行，第 16 步 `filters=['story_count:gte:20']` 判划失败、
    `Unknown filter dimension`）。判失败的依据见 ``tool_result_error``。

    全部调用都失败时**退回最后一次**（保持旧行为，不假装有成功调用），并在
    ``ok=False`` 里如实标出；``skipped_failed`` 记录被跳过的失败调用数（序在选中
    调用之后的，序 = 消息下标 + 消息内调用位置，故同一轮并发多次调用也算得清），
    供注记说明「展示的不是最后一次调用」。
    """
    cands = []
    for i, m in enumerate(messages):
        calls = (msg_get(m, "tool_calls")
                 or (msg_get(m, "additional_kwargs") or {}).get("tool_calls") or [])
        for pos, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            if CUBE_TOOL_HINT not in name:
                continue
            args = call.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    args = None
            if not isinstance(args, dict):
                continue
            lines = cube_arg_lines(name, args)
            if len(lines) <= 1:
                continue
            res = call_result_message(messages, i, call.get("id"), name)
            cands.append({
                "tool": name, "args": args, "lines": lines,
                # 调用 id：调用方要拿结果正文（评测判执行成败）时按它精确对齐；
                # 只按工具名找会在「成功调用之后还有一次失败调用」时错配。
                "id": call.get("id"),
                # 序 = (消息下标, 消息内第几次调用)：同一轮并发多次调用时后者才算「其后」
                "_seq": (i, pos),
                "ok": not (res is not None and tool_result_error(res)),
            })
    if not cands:
        return {}

    ok_cands = [c for c in cands if c["ok"]]
    if ok_cands:
        pick = ok_cands[-1]
        pick["skipped_failed"] = sum(
            1 for c in cands if not c["ok"] and c["_seq"] > pick["_seq"]
        )
    else:
        pick = cands[-1]
        pick["skipped_failed"] = 0
    pick.pop("_seq", None)
    return pick


WRENAI_PREFIX = "wrenai_"


def resolve_wren_ctx(tool_name: str) -> tuple:
    """wrenai 工具名 → ``(项目路径, 连接字典)``；非 wren 工具或取不到时 ``("", {})``。

    工具名前缀是 server 名（``wrenai_WIT_query_cube``），而 server 名是库名的
    **不可逆 ASCII 骨架**（``WIT运营管理平台数据库`` → ``WIT``，见
    semantic_db.wrenai_server_name）→ 只能按 mcp_tool 建 server 的同一套映射
    （``discover()`` + ``wrenai_server_name``）**正向**匹配。

    陷阱（2026-09-16 修复）：不能从工具名**反推**库名。工具名是
    ``f"{server_name}_{tool_name}"`` 纯拼接，而 wren 的工具名自身含下划线
    （``query_cube`` / ``run_sql`` / ``get_instructions``…），早先按
    ``rsplit("_", 1)[0]`` 只掉了最后一段（``WIT_query_cube`` → ``WIT_query``），
    前缀算成 ``wrenai_WIT_query`` 永不匹配 → Cube 与 SQL 两条通道**恒定**拿不到
    物理 SQL、报告静默退回「查询定义」节。改为按 server 名做**最长前缀匹配**。
    """
    try:
        name = str(tool_name or "")
        if not name.startswith(WRENAI_PREFIX):
            return "", {}
        from agent.utils.semantic_db import get_detector, wrenai_server_name

        detector = get_detector()
        prefix, db = "", ""
        for _d in sorted(detector.discover()):
            _p = wrenai_server_name(_d)
            # 边界取 `server名_`：避免 `wrenai_WIT` 误吞 `wrenai_WIT2_…`
            if name.startswith(f"{_p}_") and len(_p) > len(prefix):
                prefix, db = _p, _d
        if not db:
            _logger.warning(
                "[wren_call_extract] wren 工具名 %r 未匹配任何已建模库（server 名: %s）"
                "→ 本次拿不到物理 SQL",
                name, sorted(wrenai_server_name(d) for d in detector.discover()),
            )
            return "", {}
        project = detector.project_path_for(db) or ""
        if not project:
            _logger.warning(
                "[wren_call_extract] 库 %r 无 wren 项目路径 → 本次拿不到物理 SQL", db
            )
            return "", {}
        from agent.tools.mcp_tool import wren_conn_dict

        conn = wren_conn_dict(db)
        return project, (conn if isinstance(conn, dict) else {})
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.warning("[wren_call_extract] wren 上下文解析失败 %s: %s", tool_name, e)
        return "", {}


def resolve_wren_ctx_by_db(db_name: str) -> tuple:
    """库名 → ``(项目路径, 连接字典)``；未建模/取不到时 ``("", {})``。

    ``resolve_wren_ctx`` 的**正向孪生**：那边从工具名（含 server 名）反查库，这边
    直接拿库名查——交互式场景（标注页「按口径试算」）手里只有反馈记录里的
    ``db_name``，没有工具名可用。

    与 ``resolve_wren_ctx`` 共用同一套解析（``discover`` 定「是否建模」、
    ``project_path_for`` 取项目、``wren_conn_dict`` 取连接），保证两条路对同一个
    库给出**同一份** ctx；库名先经 ``normalize_db_name`` 归一（大小写 / 物理库名
    与配置名不同名的情形，见该函数 docstring）。fail-open 返回 ``("", {})``。
    """
    try:
        from agent.utils.semantic_db import (
            get_detector,
            normalize_db_name,
        )

        db = normalize_db_name(str(db_name or "").strip())
        if not db:
            return "", {}
        detector = get_detector()
        project = detector.project_path_for(db) or ""
        if not project:
            _logger.warning(
                "[wren_call_extract] 库 %r 未建模（无 wren 项目路径）→ 无法试算 Cube 口径",
                db,
            )
            return "", {}
        from agent.tools.mcp_tool import wren_conn_dict

        conn = wren_conn_dict(db)
        return project, (conn if isinstance(conn, dict) else {})
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.warning("[wren_call_extract] wren 上下文解析失败 %r: %s", db_name, e)
        return "", {}


# ── 对外：Cube 调用 → 定义 + SQL 快照 ────────────────────────────────
# 规范化定义里**刻意不保留**的键：limit/offset 是工具边界参数——wren 0.13 线上直传
# 必错（双 LIMIT / 无 LIMIT 的 OFFSET），已在工具边界剥离、由平台结果集截窗替代，
# 故它们既不在物理 SQL 里，也不该留在「模型写了什么定义」的快照里：否则评测比对
# 会把工具参数差异误判成模型差异，监控也会看到一份线上不存在的定义。
_CUBE_SPEC_DROP = ("limit", "offset")


def normalize_cube_spec(args: dict) -> dict:
    """Cube 入参 → 可比对的规范化定义（列表排序 + 剥工具边界参数）。

    ``dimensions`` / ``measures`` / ``filters`` 是集合语义，顺序不该影响判等，一律
    排序；空值（``None`` / ``""`` / ``[]`` / ``{}``）与 ``cube_arg_lines`` 同口径剔除，
    保证「能读到的定义」与「快照里的定义」逐字一致。
    """
    spec = {}
    for k in CUBE_ARG_KEYS:
        if k in _CUBE_SPEC_DROP:
            continue
        v = (args or {}).get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        spec[k] = sorted(str(x) for x in v) if isinstance(v, list) else v
    return spec


def spec_readable_text(spec: dict) -> str:
    """规范化定义 → 人读多行文本（``k: v`` 行，与报告「查询定义」节同款式）。

    **刻意从 ``spec`` 渲染、而不是复用 ``cube_arg_lines`` 的原始入参**：入集要的是
    「可跨次比对的聚合口径」，而 ``cube_arg_lines`` 还带工具名抬头行与 limit/offset
    ——那是工具边界参数、不在物理 SQL 里也不该进比对（见 ``_CUBE_SPEC_DROP``）。
    两者同源不同用：报告要「模型原样调了什么」，入集要「归一化后的口径长什么样」。
    """
    lines = []
    for k in CUBE_ARG_KEYS:
        v = (spec or {}).get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        lines.append(f"{k}: " + (v if isinstance(v, str)
                                 else json.dumps(v, ensure_ascii=False)))
    return "\n".join(lines)


def cube_snapshot(messages) -> dict:
    """最后一次成功的 Cube 调用 → 完整快照；取不到返回 ``{}``。

    键：``tool`` / ``spec`` / ``readable`` / ``physical_sql`` / ``cube_sql`` / ``sql``
    / ``skipped_failed``。

    - ``spec``：``normalize_cube_spec`` 的产物，供**监控与评测比对**（同题多跑是否
      用了同一份聚合口径，一眼可比）。
    - ``readable``：人读的多行定义文本，与报告「查询定义（Cube 语义层）」节同源。
    - ``physical_sql`` / ``cube_sql``：复算出的物理 / 语义层 SQL，可能为空串。
    - ``sql``：按 ``MAX_SNAPSHOT_SQL`` 截断线选出的**首选** SQL（物理优先，超限退
      语义层），即 ``cube_sql_snapshot`` 的返回值。
    - ``result``：该次调用的工具结果正文（Cube 回的是 ``{columns, rows, …}`` JSON）。
      评测侧要靠它判「执行是否成功」——与 run_sql 通道的 ``result_text`` 同用途。

    三样东西从**同一次** ``plan_cube_sql`` 复算里出，所以任何调用方都不该为了多拿
    一项而再算一遍（引擎按 ``mdl.json`` 指纹缓存，但复算本身仍有开销）。

    fail-open：复算能力探测不过、库/项目/连接取不到、引擎编译失败 → 一律 ``{}``，
    由调用方按「无 SQL」处理（绝不伪造一条不存在的 SQL）。
    """
    try:
        call = extract_last_cube_call(messages)
        if not call or not call.get("ok"):
            return {}
        args = call.get("args") or {}
        # 结果正文按 call_id 精确对齐（idx=-1 → 全表扫，优先 id 命中）：「最后一次
        # 成功调用之后还有一次失败调用」时只按工具名找会拿到失败那条的正文。
        res = call_result_message(messages, -1, call.get("id"), call.get("tool"))
        out = {
            "tool": call.get("tool") or "",
            "spec": normalize_cube_spec(args),
            "readable": call.get("lines") or [],
            "physical_sql": "",
            "cube_sql": "",
            "sql": "",
            "result": _msg_content_str(res) if res is not None else "",
            "skipped_failed": call.get("skipped_failed", 0),
        }
        project, conn = resolve_wren_ctx(call.get("tool"))
        if not project or not conn:
            # 拿不到引擎上下文：定义仍如实带出（它是从消息里直接读的，不需要复算），
            # 只是没有 SQL。监控侧的 spec 因此不受复算故障影响。
            return out
        from agent.feedback.store import MAX_SNAPSHOT_SQL
        from agent.utils.wren_plan import plan_cube_sql

        plan = plan_cube_sql(project, conn, args)
        physical = str(plan.get("dialect_sql") or "")
        semantic = str(plan.get("cube_sql") or "")
        out["physical_sql"], out["cube_sql"] = physical, semantic
        if physical and len(physical) <= MAX_SNAPSHOT_SQL:
            out["sql"] = physical
        elif semantic and len(semantic) <= MAX_SNAPSHOT_SQL:
            # 物理 SQL 过了截断线：退回语义层 SQL。它短得多但物理库跑不了，属已知
            # 降级——胜过存一条半截 SQL。截断线 2026-09-19 已由 8000 提到 64000
            # （实测最大 10.9KB），此路只在口径极端到编译出 >64KB 物理 SQL 时可达。
            _logger.info(
                "[wren_call_extract] 物理 SQL 超 %d 字符（%d），快照退回语义层 SQL",
                MAX_SNAPSHOT_SQL, len(physical),
            )
            out["sql"] = semantic
        return out
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.warning("[wren_call_extract] Cube 快照生成失败: %s", e)
        return {}


def cube_sql_snapshot(messages) -> str:
    """最后一次成功的 Cube 调用的 SQL 快照；取不到返回 ``""``。

    供反馈侧（``api.message_feedback``）把「点赞的 Cube 查询」落成可入 Good Set 的
    SQL。取的是**物理 SQL**（``dialect_sql``，已在进程内展开 MDL 视图、转成目标库
    方言）——与报告「执行 SQL（物理，实际下发）」节同一条，可直接粘进 MySQL、也能过
    标注页的只读执行校验；而 ``cube_sql`` 引用 MDL 视图（``v_workhour``），物理库里
    根本不存在，执行必失败，故只作超限降级用。

    为什么用「最后一次成功」而不是「最后一次」：与报告锚点同源，见
    ``extract_last_cube_call``。全部调用都失败时它返回 ``ok=False`` → 这里直接给空，
    不返回一条没跑出数据的定义。

    fail-open：复算能力探测不过、库/项目/连接取不到、引擎编译失败 → 一律 ``""``，
    由调用方按「无 SQL」处理（绝不伪造一条不存在的 SQL）。
    """
    return cube_snapshot(messages).get("sql", "")
