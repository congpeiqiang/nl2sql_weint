"""增强 check_async_task 返回详细进度信息（含每步耗时）。

deepagents 默认的 check_async_task 在任务 running 时只返回
{"status": "running", "thread_id": "xxx"}。本模块 monkey-patch 让它在
running 状态下也拉取 thread 中间态，提取：
  - 整体进度（completed/total）
  - 每步状态 + 耗时
  - 当前步骤 + 最近工具调用
  - AI 最新想法

耗时数据来源（A+C 组合）：
  C: ProgressTrackerMiddleware 本地记录的 step_history
  A: 从 message metadata 时间戳估算（fallback）
"""
import json
import logging
import re
import time as _time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg
from pydantic import BaseModel, Field

from agent.utils.failure_signal import detail_of, last_failed_mark, run_status_for
from agent.utils.offload import offload
from agent.utils.query_tools import is_data_tool
from agent.utils.wren_plan import (
    DEFAULT_ROW_LIMIT,
    PLAN_INLINE_MAX,
    plan_cube_sql,
    plan_run_sql,
    write_plan_file,
)

# Cube 调用识别 + wren 上下文解析的实现已迁到 agent/utils/wren_call_extract：
# 反馈侧（api.message_feedback）也要用同一套逻辑把点赞的 Cube 查询复算成 SQL 快照，
# 而本模块 import 时会执行 apply_patch()（文件尾）改写 deepagents 属性 —— API 层不能
# 为了取一个 SQL 快照而连带跑那些补丁。这里按原私有名转发，本模块内既有引用点与
# 离线套件（d:\tmp\test_wren_ctx_resolve.py 用 cp._resolve_wren_ctx 等）零改动。
from agent.utils.wren_call_extract import (
    CUBE_ARG_KEYS as _CUBE_ARG_KEYS,
    CUBE_TOOL_HINT as _CUBE_TOOL_HINT,
    WRENAI_PREFIX as _WRENAI_PREFIX,
    _msg_content_str,
    call_result_message as _call_result_message,
    cube_arg_lines as _cube_arg_lines,
    extract_cube_calls as _extract_cube_calls,
    extract_last_cube_call as _extract_last_cube_call,
    resolve_wren_ctx as _resolve_wren_ctx,
    tool_result_error as _tool_result_error,
)

_logger = logging.getLogger(__name__)

_PATCHED = False


# ── P1-7 增量读取（since/cursor）─────────────────────────────────────
# check_async_task 加可选 `since` 游标：传上一次返回的 cursor，只回之后的新消息，
# 避免每次都把子线程全部消息摘要塞进主 agent 上下文（对齐 dsh job_output 增量读取）。
class CheckAsyncTaskSinceSchema(BaseModel):
    """check_async_task 输入 schema（增加可选 since 游标）。"""

    task_id: str = Field(
        description="The exact task_id string returned by start_async_task. Pass it verbatim."
    )
    since: Optional[int] = Field(
        default=None,
        description=(
            "Optional message cursor for incremental reads: pass the `cursor` value "
            "returned by a previous check to get only messages added since then. "
            "Omit for a normal full status check."
        ),
    )
    full: Optional[bool] = Field(
        default=None,
        description=(
            "Set true to read the finished task's result WITHOUT the usual "
            "summarization caps (all table rows, up to a much larger limit). Use "
            "this when a previous check's result looks truncated: it returns the "
            "same unchanged result, so there is nothing to gain from re-dispatching "
            "the subagent via update_async_task."
        ),
    )


# 每条增量消息内容截断上限 / 单次最多返回条数（防大结果撑爆主 agent 上下文）
_INCREMENTAL_MAX_CHARS = 800
_INCREMENTAL_MAX_ITEMS = 20


def _brief_message(msg) -> dict:
    """把一条子线程消息压成精简 dict（role + 截断 content）。"""
    if not isinstance(msg, dict):
        return {"role": "unknown", "content": str(msg)[:_INCREMENTAL_MAX_CHARS]}
    role = msg.get("role") or msg.get("type") or "unknown"
    content = msg.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    return {"role": role, "content": content[:_INCREMENTAL_MAX_CHARS]}


# ── 结果摘要化（P0：减少主 agent LLM 输入 token）─────────────────────
_MAX_RESULT_CHARS = 2000      # 结果内容最大字符数
_MAX_RESULT_ROWS = 20         # SQL 表格结果最多保留行数
# check_async_task(full=true)：显式索取"不摘要"的完整原文（P4）。
# 摘要上限之外原本没有正当逃逸通道 → 主 agent 只能用 update_async_task 重派发
# 子任务去"制造新消息"换取原文（trace 72e222c0：4 次重派发、空转 6.5 分钟）。
# 该路径只在主 agent 明确要求时走，不是每次轮询的常态，故上限放大两个数量级，
# 但仍保留上限（超大结果仍走摘要逻辑，只是预算更大）。
_MAX_RESULT_CHARS_FULL = 60000
_MAX_RESULT_ROWS_FULL = 500
# 子 agent 用"（续）"把大表拆多段时的延续标记（不计为数据行/正文）
_CONTINUE_MARK = re.compile(r"^\s*[（(]?续[）)]?\s*$")


def _trim_block(text: str, budget: int) -> str:
    """截断一段非表格文本：保留头尾、中间省略，预算内尽量完整保留关键结论。"""
    if len(text) <= budget:
        return text
    half = budget // 2
    return text[:half].rstrip() + f"\n…({len(text)} 字符，中间省略)…\n" + text[-half:].lstrip()


def _summarize_result(content: str, full: bool = False) -> str:
    """智能摘要子 agent 返回的结果内容，保留结构信息、截断数据行。

    确保 LLM 能判断是否需要图表，同时避免 34k+ tokens 的 prompt 膨胀。

    full=True（check_async_task 的 full 参数）时改用 _MAX_RESULT_CHARS_FULL /
    _MAX_RESULT_ROWS_FULL 预算，即尽量原样返回——见下面 2026-09-14 修复。仍保留
    上限：超过 full 预算的超大结果继续走同一套摘要逻辑，只是预算更大。

    修复（2026-09-01，trace 8ccef016「78 个部门」幻觉）：
      - 不再丢弃表格上方的关键结论（如"共 152 个名称 / 431 条记录"）——
        旧实现只拼表头+前 20 行，主 agent 看不到结论，把摘要行数误当业务统计；
      - 行数按真实数据行统计：子 agent 用"（续）"把大表拆成多段时，第二段的
        表头/分隔行曾被误计为数据行（76 行数成 78）；
      - 摘要 marker 明确"行数≠业务统计口径"。

    修复（2026-09-14，trace a6f86bbd「查询阶段跑完后进度条重跑」）：
      - 行数不再跨表求和：子 agent 正文写"39 行"，摘要却写"数据表共 53 行"
        （39 行明细表 + 14 行「各人员合计」表被 `sum` 到一起，且只渲染第一张
        表的前 20 行）→ 主 agent 判定数据缺失、update_async_task 重派发子任务；
      - 改为按"逻辑表"分段计数并逐张点名行数（"（续）"拆分的多段仍算同一张表，
        保住 76 行的既有语义）；每张表都渲染各自的表头 + 配额行数，不再只输出
        第一张表（第二张表整段消失本身就是幻觉温床）；
      - marker 增加"未展开的行不是数据缺失…无需重新派发子任务"。

    修复（2026-09-14，trace 72e222c0「结果摘要 20 行上限驱动重派发风暴」）：
      - 三条 marker 里的"完整数据可通过 check_async_task 增量读取获取"在结果
        定格时是假话：`since=N` 与全量读取逐字相同，什么都给不出。主 agent 于是
        用 update_async_task 重派发子任务去"制造新消息"（4 次、约 6.5 分钟空转）。
      - 补齐真正的逃逸通道 `full=True`（本次改动），并把 marker 文案从"增量读取"
        改成实话：要完整原文就 `check_async_task(task_id, full=true)`，不要重派发。
    """
    _max_chars = _MAX_RESULT_CHARS_FULL if full else _MAX_RESULT_CHARS
    _max_rows = _MAX_RESULT_ROWS_FULL if full else _MAX_RESULT_ROWS
    if len(content) <= _max_chars:
        return content

    # ── 1. 解析 Markdown 表格（支持"（续）"拆分的多段表） ──
    lines = content.split("\n")
    pre_lines, post_lines, table_runs = [], [], []
    pending_continue = False  # 刚见过"（续）"→ 下一段与前一段属同一张逻辑表
    cur = None
    for l in lines:
        if l.strip().startswith("|"):
            if cur is None:
                cur = {"header": l, "sep": "", "data": []}
            elif not cur["sep"]:
                cur["sep"] = l
            else:
                cur["data"].append(l)
        else:
            if cur is not None:
                table_runs.append(cur)
                cur = None
            s = l.strip()
            if not s:
                continue  # 空行不计入正文（也不打断"（续）"延续标记）
            if _CONTINUE_MARK.match(s):
                pending_continue = True  # "（续）"标记不计入正文
                continue
            pending_continue = False
            if not table_runs:
                pre_lines.append(l)
            else:
                post_lines.append(l)
    if cur is not None:
        table_runs.append(cur)

    if table_runs:
        # ── 1a. 分段归组为「逻辑表」──
        # 子 agent 用"（续）"把一张大表拆多段（8ccef016：两段 38 行 = 76 行，不能数成 78）；
        # 表头不同则是真正不同的表（a6f86bbd：39 行明细 + 14 行合计 ≠ 53 行）。
        groups: list[dict] = []  # {"header": 首段表头, "segs": [run, ...]}
        for r in table_runs:
            same_as_prev = bool(groups) and (
                pending_continue or r["header"] == groups[-1]["header"]
            )
            if same_as_prev:
                groups[-1]["segs"].append(r)
            else:
                groups.append({"header": r["header"], "segs": [r]})
            pending_continue = False

        n_tables = len(groups)
        rows_per_table = [sum(len(s["data"]) for s in g["segs"]) for g in groups]

        # ── 1b. 行数配额：每张表至少 1 行，余量按顺序补给还有未展开行的表 ──
        quota = [max(1, _max_rows // n_tables)] * n_tables
        take = [0] * n_tables
        budget = _max_rows
        for i in range(n_tables):
            take[i] = min(rows_per_table[i], quota[i])
            budget -= take[i]
        i = 0
        while budget > 0 and any(
            take[k] < rows_per_table[k] for k in range(n_tables)
        ):
            k = i % n_tables
            if take[k] < rows_per_table[k]:
                take[k] += 1
                budget -= 1
            i += 1

        # ── 1c. 每张逻辑表都渲染（保留各自表头），不再只输出第一张表 ──
        rendered, shown = [], 0
        for g, t in zip(groups, take):
            remain = t
            for s in g["segs"]:
                rows = s["data"][:remain]
                if not rows:
                    continue
                rendered.append(
                    s["header"] + "\n" + s["sep"] + "\n" + "\n".join(rows)
                )
                remain -= len(rows)
                shown += len(rows)
                if remain <= 0:
                    break

        if n_tables == 1:
            count_desc = f"数据表共 {rows_per_table[0]} 行"
        else:
            detail = "、".join(
                f"第{i + 1}张 {c} 行" for i, c in enumerate(rows_per_table)
            )
            count_desc = f"数据表 {n_tables} 张（{detail}，逐张统计非合计）"
        suffix = (
            f"\n\n*({count_desc}，以上展示前 {shown} 行；"
            "行数仅为展示表格行数，业务统计口径以结果文字为准；"
            "未展开的行不是数据缺失（结果已定格，since 增量读取同样给不出），"
            "要完整原文就用 check_async_task(task_id, full=true) 读一次，"
            "不要用 update_async_task 重新派发子任务去补数据)*"
        )
        table_part = "\n\n".join(rendered) + suffix
        parts = []
        pre_text = "\n".join(pre_lines).strip()
        if pre_text:
            parts.append(_trim_block(pre_text, _max_chars // 2))
        parts.append(table_part)
        post_text = "\n".join(post_lines).strip()
        if post_text:
            parts.append(_trim_block(post_text, _max_chars // 2))
        return "\n\n".join(parts)

    # ── 2. 检测 JSON 数组结果 ──
    try:
        data = json.loads(content)
        if isinstance(data, list) and len(data) > 0:
            kept = data[:_max_rows]
            summary = json.dumps(kept, ensure_ascii=False, indent=2)
            summary += (
                f"\n\n*(共 {len(data)} 条记录，以上展示前 {len(kept)} 条；"
                f"要完整数据用 check_async_task(task_id, full=true) 读一次，"
                f"不要重新派发子任务)*"
            )
            return summary
    except (json.JSONDecodeError, ValueError):
        pass

    # ── 3. 普通文本：保留头尾 ──
    head = content[:_max_chars // 2]
    tail = content[-(_max_chars // 2):]
    return f"{head}\n\n...({len(content)} 字符，中间已省略；要完整原文用 check_async_task(task_id, full=true) 读一次)...\n\n{tail}"


def _tool_result_shape(messages, i) -> tuple:
    """第 i 条工具结果消息 → ``(返回行数, 列名集合)``；非数据载荷 ``(0, [])``。

    run_sql 与 query_cube 的结果体**结构同构**（``{columns, rows, row_count,
    truncated}``），所以这层解析两边共用：run_sql 走 `_run_sql_meta`，Cube 候选
    走 `_pick_cube_call_for_report`（值匹配要看返回了多少行）。
    """
    try:
        obj = json.loads(_msg_content_str(messages[i]))
    except (json.JSONDecodeError, ValueError):
        return 0, []
    if not isinstance(obj, dict):
        return 0, []
    rows, cols = 0, []
    rc = obj.get("row_count")
    try:
        rc_int = int(rc) if rc is not None else None
    except (TypeError, ValueError):
        rc_int = None
    rr = obj.get("rows")
    # QueryResultOffload 大结果瘦身后 rows 仅前 N 样例、真行数在 row_count：
    # 须优先 row_count，否则 _extract_last_sql 的「返回行数最多 = 产出 SQL」
    # 启发式会把 20 当 431（生产 trace「431 部门人数」228s 静默治本修复）。
    if obj.get("rows_truncated") and rc_int is not None:
        rows = rc_int
    elif isinstance(rr, list):
        rows = len(rr)
    elif rc_int is not None:
        rows = rc_int
    cc = obj.get("columns")
    if isinstance(cc, list):
        cols = [str(x).strip().lower() for x in cc]
    return rows, cols


def _run_sql_meta(messages, i):
    """解析第 i 条 run_sql 工具结果消息：返回 (sql, 返回行数, 列名集合)。

    通过 tool_call_id 与之前 AI 消息的 tool_calls[].id 精确对齐，避免同一 AI
    消息里连续多个 run_sql 时结果错配。行数/列名见 `_tool_result_shape`。
    """
    m = messages[i]
    rows, cols = _tool_result_shape(messages, i)
    tcid = m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
    last_sql = ""
    for j in range(i - 1, -1, -1):
        mj = messages[j]
        if isinstance(mj, dict):
            tcs = mj.get("tool_calls") or []
        else:
            tcs = getattr(mj, "tool_calls", None) or []
        for tc in tcs:
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            if not (isinstance(name, str) and name.endswith("run_sql")):
                continue
            if not isinstance(args, dict):
                continue
            s = args.get("sql", "")
            sql = s.strip() if isinstance(s, str) else ""
            if not sql:
                continue
            if tcid and tc_id and str(tc_id) == str(tcid):
                return sql, rows, cols
            last_sql = sql  # 无 id 匹配时回退：最后一个 run_sql 的 sql
    return last_sql, rows, cols


def _collect_full_result_files(messages) -> list:
    """收集子线程里数据表型大结果落盘文件的 VFS 指针（保序去重）。

    QueryResultOffloadMiddleware 把 >50 行 / 大文本的**数据表型**结果（run_sql 与
    Cube 快速通道 query_cube，清单见 agent.utils.query_tools）瘦身为
    {row_count, rows:[前 N 样例], rows_truncated, full_result_file}。这里从子线程
    消息确定性汇总全量文件清单给主 agent / build_report，供其代码读盘嵌入报告
    （0 模型开销，不依赖模型在最终回复里拼的字符串）。
    """
    files = []
    seen = set()
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role") or m.get("type")
            name = m.get("name") or ""
        else:
            role = getattr(m, "type", "")
            name = getattr(m, "name", "") or ""
        if role not in ("tool", "tool_result"):
            continue
        # 与落盘闸门共用同一份清单（agent.utils.query_tools）：只认 run_sql 会让
        # Cube 通道的 full_result_files 恒空 → 报告缺「完整数据表」节（2026-09-14 修）
        if not is_data_tool(name):
            continue
        try:
            obj = json.loads(_msg_content_str(m))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        fp = obj.get("full_result_file")
        if isinstance(fp, str) and fp and fp not in seen:
            seen.add(fp)
            files.append(fp)
    return files


def _final_table_headers(messages) -> set:
    """取最后一条带 Markdown 表格的 AI 消息的表头列（归一化小写集合）。"""
    for m in reversed(messages):
        if isinstance(m, dict):
            role = m.get("role") or m.get("type")
        else:
            role = getattr(m, "type", "")
        if role not in ("ai", "assistant"):
            continue
        for line in _msg_content_str(m).split("\n"):
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells:
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue  # 分隔行
            return {c.lower() for c in cells if c}
    return set()


def _run_sql_cell_values(messages, i) -> set:
    """解析第 i 条**数据工具**结果 JSON 的 rows[] 单元值集合（去空白）。

    大结果被 QueryResultOffload 瘦身时 rows 只留前 N 样例，仍用样例做值匹配；
    错误文本（非 JSON）返回空集。

    「数据工具」= run_sql **与** query_cube（两者结果体同构），所以 Cube 通道的
    值匹配（`_pick_cube_call_for_report`）直接复用本函数，名字里的 run_sql 是
    历史遗留、不是适用范围。
    """
    m = messages[i]
    try:
        obj = json.loads(_msg_content_str(m))
    except (json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(obj, dict):
        return set()
    vals = set()
    for r in obj.get("rows") or []:
        if not isinstance(r, dict):
            continue
        for v in r.values():
            if v is None:
                continue
            s = str(v).strip()
            if s:
                vals.add(s)
    return vals


def _run_sql_row_values(messages, i) -> list:
    """第 i 条**数据工具**结果**按行**的非空单元值集合（tier0 的行内证据；run_sql
    与 query_cube 共用，见 `_run_sql_cell_values` 的同名说明）。

    与 `_run_sql_cell_values`（整表拍平成一个集合）的差别是**保留「同属一行」的结构**：
    拍平比对时，数据库某列为 NULL、模型在答案里写了占位文案（trace b816d442 的
    `project_name` → 「（空，未匹配到项目）」）会成为**永远无法命中的反证**，把整行判掉。
    按行取证只要求「候选某一行的全部非空值都能在答案某行里找到」——NULL 天然不参与，
    于是它既不是证据也不是反证。错误文本/非 JSON ⇒ 返回空列表。
    """
    try:
        obj = json.loads(_msg_content_str(messages[i]))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(obj, dict):
        return []
    out = []
    for r in obj.get("rows") or []:
        if not isinstance(r, dict):
            continue
        vals = {
            str(v).strip() for v in r.values()
            if v is not None and str(v).strip()
        }
        if vals:
            out.append(vals)
    return out


def _final_answer_data_rows(messages) -> list:
    """最后一条 AI 消息里所有 Markdown 数据表的数据行（每行=归一化单元格列表）。

    跳过表头行/分隔行/空行/“（续）”标记（兼容“（续）”拆分的多段表，每段表头只
    首行、不计入）。值匹配 tier 用这些行去命中 run_sql 返回值，不依赖列名与表头
    语言，兼容“最终答案中文表头 vs run_sql 英文列”的错配。
    """
    for m in reversed(messages):
        if isinstance(m, dict):
            role = m.get("role") or m.get("type")
        else:
            role = getattr(m, "type", "")
        if role not in ("ai", "assistant"):
            continue
        rows = []
        in_table = False  # 每张表首行内容行是表头，不计入
        for line in _msg_content_str(m).split("\n"):
            s = line.strip()
            if not s.startswith("|"):
                in_table = False
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue  # 分隔行
            if not in_table:
                in_table = True  # 新表：首行=表头
                continue
            rows.append(cells)
        if rows:
            return rows
    return []


# ── tier0 值匹配的「渲染形态归一」──────────────────────────────────
# 生产 trace b816d442（2026-10-01，「列出 9 月完成缺陷里耗时最长的 10 条」）：
#   run_sql 返回值是 `complete_time = "2026-09-14T16:33:51"`（ISO，T 分隔），
#   模型写进最终答案表格是 `2026-09-14 16:33:51`（空格分隔）。
# 原实现只做子串比对 ⇒ **每一行都因这两格判不覆盖** ⇒ 真产出 SQL 的 tier0 命中
# 0/10 ⇒ 整条启发式退化到 tier2「行数最多」，而那条诊断聚合
# （`GROUP BY status_code`）恰好也是 10 行，`max()` 平票取**靠前**者 ⇒ 报告/快照里
# 的 SQL 变成探值，物理 SQL 复算与 `judge_sql_origin` 的输入一并错位。
# 结论：**纯渲染形态差异不能让整行覆盖失败**——同一时刻/同一个数的不同写法必须先归一。
_CLOCK_RE = re.compile(r"\d{2}:\d{2}:\d{2}")
# 「合法数字渲染」：**裸数字串** / 正确千分位分组 / 小数 / 尾随 %。刻意不认 `1,2`
# 这类非法分组（会把非数字文本折成 12）、也不认夹在日期或文本里的数字串
# （`2026-09-01` 不是数字）——免得误命中。
_NUM_RENDER_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$")


def _match_norm(s: str) -> str:
    """时刻文本归一：ISO 的 `T` → 空格、去秒后小数、去秒后时区（`Z`/`+08:00`）。

    只归一**渲染形态**，不改变语义。无时钟时刻的字符串原样返回——值集合是按值
    逐个调用的，这条快路径把姓名/文案/短数字全部挡在正则之外。
    """
    if not _CLOCK_RE.search(s):
        return s
    s = re.sub(r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})", r"\1 \2", s)
    s = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", s)
    return re.sub(r"(\d{2}:\d{2}:\d{2})(?:[Zz]|[+-]\d{2}:?\d{2})(?![0-9])", r"\1", s)


def _num_render_key(s: str) -> float | None:
    """数字文本归一：合法数字渲染（裸数字 / 正确千分位 / 小数 / 尾随 %）→ float，否则 None。

    与 `_is_plain_number`（tier0b 用）**刻意分开**：后者宽松（`1,2` 也当数字），
    本函数只认「整串就是一个数」，避免把日期/文本里的数字串折成数值造成误命中。
    裸数字与带分组写法因而等价（`1309` == `1,309`）。
    """
    t = s.strip()
    if not _NUM_RENDER_RE.match(t):
        return None
    try:
        return float(t.replace(",", "").rstrip("%"))
    except ValueError:
        return None


def _digit_hit(hay: str, needle: str) -> bool:
    """纯数字 `needle` 在 `hay` 里的命中是否可信（替代裸子串判定）。

    单个数位（`1`/`2`）只认**完全相等**——它出现在任何数字串里的概率太高，宁可漏；
    两位数及以上按**数位边界**判定（两侧都不是数字），于是 `92` 命中 `92 天（2212 h）`、
    而 `2` / `12` 不命中 `2026-09-01` 里的连续数位。
    """
    if len(needle) < 2:
        return hay == needle
    return re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", hay) is not None


def _value_matches_cell(cc: str, vv: str) -> bool:
    """（**都已归一化**）候选值 `vv` 与答案单元格 `cc` 是否互相命中。

    子串双向匹配（兼容 run_sql 返回 `2026-08-23 00:00:00` 而答案只写 `2026-08-23`）；
    纯数字一侧改判 `_digit_hit`（数位边界 / 单个数字须相等）而非裸子串——既让 `92`
    命中 `92 天（2212 h）`，又不让 `2` / `12` 命中 `2026-09-01` 里的连续数位。两侧都是
    合法数字渲染时按数值相等命中（`43,065` == 43065）。
    """
    if not cc or not vv:
        return False
    if vv in cc and (not vv.isdigit() or _digit_hit(cc, vv)):
        return True
    if cc in vv and (not cc.isdigit() or _digit_hit(vv, cc)):
        return True
    cn, vn = _num_render_key(cc), _num_render_key(vv)
    return cn is not None and vn is not None and cn == vn


def _cell_covered_by_any(cc: str, vals) -> bool:
    """单个（已归一化的）答案单元格是否被候选值集合里的某个值命中。"""
    for v in vals:
        if not v or not v.strip():
            continue
        vv = _match_norm(v.strip().lower())
        if vv and _value_matches_cell(cc, vv):
            return True
    return False


def _row_values_covered(norm_cells: list, row_vals) -> bool:
    """候选**某一行**的全部非空值是否都能在答案该行的单元格里找到（行内证据）。

    比拍平集合更严（必须落在同一行）也更公道（NULL/空值不参与、也就不会成为反证）。

    ⚠️ **单值行不作行内证据**（2026-10-21，prod thread 01a15d6d）：DISTINCT 探值的
    单值行（如 `AI与数据产品线/产品管理部`）经 `_value_matches_cell` 的 `cc in vv`
    子串方向命中答案短单元格（`产品管理部`）⇒ 零成本「覆盖」全部答案行、tier0 假阳性
    绕过通道闸（报告 §4 又印探值 SQL + 「未使用 Cube」反话）。行内取证的本意是 NULL
    占位宽容（b816d442），那种候选行本就是多值的 ⇒ 要求 ≥2 个非空归一值。
    """
    norm_vals = []
    for v in row_vals:
        vv = _match_norm(v.strip().lower())
        if vv:
            norm_vals.append(vv)
    if len(norm_vals) < 2:
        return False
    return all(
        any(_value_matches_cell(cc, vv) for cc in norm_cells)
        for vv in norm_vals
    )


def _final_row_covered_by(cells, vals, row_values=None) -> bool:
    """一行最终表数据行是否被候选 run_sql 返回值集合“整行覆盖”。

    行内每个非空单元格都须被候选某值命中（`_cell_covered_by_any`）；单元格两端先剥
    `**` 加粗。比对前两侧都过 `_match_norm` 做**渲染形态归一**（ISO `T` ↔ 空格、去毫秒/
    时区；见该函数上方的事故说明），所以纯渲染差异不再让整行覆盖失败。

    `row_values`（候选的**按行**值集合，`_run_sql_row_values`）非空时再给一次机会：
    只要候选存在某一行，其全部非空值都能在答案这一行里找到，也算覆盖——数据库该列为
    NULL 而模型写了占位文案时，拍平比对会把这一行判成“无证据”，行内取证则不受影响。

    ⚠️ **数值佐证**（2026-10-21，prod thread 01a15d6d）：覆盖成立后，若答案该行含
    **纯数字**单元格（`_is_plain_number`），至少一个必须被候选值命中——探值/枚举类
    候选（DISTINCT 部门名之类）没有数值列，光靠文本子串命中不得冒充产出。「至少
    一个」而非「全部」：容忍模型派生列（百分比、合计、`92 天（2212 h）` 这类格式化
    时长本就不算纯数字格）。答案行没有纯数字格时保持旧行为。
    """
    norm_cells = []
    for c in cells:
        cc = _match_norm(c.strip().strip("*").strip().lower())
        if cc:
            norm_cells.append(cc)
    if not norm_cells:
        return True
    covered = all(_cell_covered_by_any(cc, vals) for cc in norm_cells)
    if not covered and row_values:
        covered = any(_row_values_covered(norm_cells, rv) for rv in row_values)
    if not covered:
        return False
    num_cells = [cc for cc in norm_cells if _is_plain_number(cc)]
    if num_cells and not any(_cell_covered_by_any(cc, vals) for cc in num_cells):
        return False
    return True


def _is_plain_number(s) -> bool:
    """去千分位逗号/尾随 % 后可按 float 解析 → 视为纯数字（tier0b 标量对齐用）。"""
    if not isinstance(s, str):
        s = str(s)
    t = s.strip().replace(",", "").rstrip("%").strip()
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def _final_numeric_values(messages) -> set | None:
    """最终答案若是「单数值列」摘要表 → 数值 float 集。

    tier0b 专用：产出标量 SQL 的返回单元格值集需覆盖这些数值。识别条件是摘要表
    内每行宽度一致(>=2)，且**恰有一列**整列全为纯数字（其余列是指标名/口径等说明
    文字）。兼容两种答案形态：
      - 「指标|数值」两列摘要（trace d60b 部门数量|431 / 部门人数|1736）；
      - 「统计项|口径|数量」三列口径表（trace 01a07043 部门总数 431 / 去重员工
        总数 280，数值在第 3 列）——原先恰 2 列前提让三列口径表直接 None → tier0b
        跳过 → 2 行 deleted 分布探值凭「行数最多」误选。
    0 个或 ≥2 个全数字列 = 明细/歧义表（id|name|parent_id 两列都数字）→ 返回 None
    不启用本层级，防把明细表的纯数字列当摘要；单列纯数字答案同理不启用。
    """
    rows = _final_answer_data_rows(messages)
    if not rows:
        return None
    w = len(rows[0])
    if w < 2:
        return None
    num_cols = set()
    for j in range(w):
        ok = True
        for cells in rows:
            if len(cells) != w:
                return None  # 宽度不一致（多表拼接）→ 不启用
            v = cells[j].strip().strip("*").strip()
            if not v or not _is_plain_number(v):
                ok = False
                break
        if ok:
            num_cols.add(j)
    if len(num_cols) != 1:
        return None
    j = next(iter(num_cols))
    nums = set()
    for cells in rows:
        v = cells[j].strip().strip("*").strip()
        nums.add(float(v.replace(",", "").rstrip("%").strip()))
    return nums if nums else None


def _cube_produced_data(messages) -> bool:
    """这批消息里是否存在**成功取到数**的 Cube 调用（`query_cube` 非 `sql_only`）。

    判据与 `process_audit.judge_sql_origin` 的 `cube_data_calls` **同源**（同一份
    `scan_tool_calls` + 同一条件 `ok and not sql_only`），不重写第二套。刻意**不**
    把 `sql_only` 预览算进来：预览只是一条被编译出来的 SQL、没返回数据，不能证明
    「数据来自 Cube」。fail-open：扫不出来 ⇒ False ⇒ 调用方走老行为。
    """
    try:
        from agent.utils.process_audit import scan_tool_calls
        for r in scan_tool_calls(messages):
            if (_CUBE_TOOL_HINT in r["tool"] and r["ok"]
                    and not r["args"].get("sql_only")):
                return True
    except Exception as e:  # noqa: BLE001  审计旁路，探测失败不改变任何行为
        _logger.debug("[check_progress] Cube 通道探测失败: %s", f"{type(e).__name__}: {e}")
    return False


def _pick_by_value_match(cands, messages) -> tuple:
    """在候选里选「真的产出了最终答案表」的那一个：``(下标, tier)``，无证据 ``(None, "")``。

    ``cands`` 每项须含 ``values``（整表拍平值集，``_run_sql_cell_values``）、
    ``row_values``（按行值集，``_run_sql_row_values``）、``rows``（返回行数，
    ``_tool_result_shape``）。两层与 `_extract_last_sql` 的 tier0 / tier0b
    **同一套判据**（本仓唯一实现，run_sql 与 Cube 两条通道共用；拆第二份迟早漂移）：

    - ``tier0`` 值匹配：候选返回值能**整行覆盖**最终答案数据表的行数最多者；同命中
      优先 ``rows == 命中`` 的精确对齐、再优先消息序靠后。天然忽略探值
      （CURRENT_DATE / 分布 / Top-N 的返回值不进最终表 ⇒ 命中 0）。
    - ``tier0b`` 标量摘要对齐：tier0 全落空、且最终表恰有一列整列纯数字时，选
      单元格值集**全覆盖**这些数值的候选（同全覆盖优先行数少——单行标量，再消息序
      靠后）。tier0 整行匹配要求每格（含中文指标名）都被命中，数据型返回值不含中文
      指标名 ⇒ 摘要表恒落空，必须靠这层。
    """
    final_rows = _final_answer_data_rows(messages)
    if not final_rows:
        return None, ""
    # ── tier 0：值匹配 ───────────────────────────────────────────
    best_i, best_hit, best_aligned = None, 0, False
    for i, c in enumerate(cands):  # 消息序正序，同质量时后者优先
        if not c.get("values"):
            continue
        hit = sum(
            1 for cells in final_rows
            if _final_row_covered_by(cells, c["values"], c.get("row_values"))
        )
        if hit <= 0:
            continue
        aligned = c.get("rows", 0) > 0 and c["rows"] == hit
        if hit > best_hit or (hit == best_hit and aligned >= best_aligned):
            best_i, best_hit, best_aligned = i, hit, aligned
    if best_i is not None:
        return best_i, "tier0"
    # ── tier 0b：标量摘要对齐 ────────────────────────────────────
    final_nums = _final_numeric_values(messages)
    if not final_nums:
        return None, ""
    full_cover = []
    for i, c in enumerate(cands):
        if not c.get("values") or c.get("rows", 0) <= 0:
            continue
        cnums = {
            float(v.strip().replace(",", "").rstrip("%").strip())
            for v in c["values"] if _is_plain_number(v)
        }
        if final_nums <= cnums:
            full_cover.append((i, c))
    if full_cover:
        # 全覆盖者：行数少（单行标量）优先，其次消息序靠后
        i, _ = min(full_cover, key=lambda t: (t[1].get("rows", 0), -t[0]))
        return i, "tier0b"
    return None, ""


# ── Cube 通道的锚点：值匹配优先（2026-10-16）────────────────────────────
# 生产 thread 01a14503（问「丛培强8-9月工时分析」，报告
# `丛培强2026年8-9月工时分析_2026-10-16_22-04-13_01a14503.md`）：
# 子任务发了 **8 次** `query_cube`（6 次取数 + 2 次 `sql_only` 预览），报告第 2/5 节
# 展示的定义与物理 SQL 是**最后一次成功**那次（`if_leave_form × approve_user_name`，
# 5 行，其数据在正文里一个字都没出现），而正文的「已审核/未审核 152/16」来自
# `if_approve` 那次、「报工天数 43」来自 day 粒度那次、「角色/项目」来自另两次。
# ⇒ 报告第 2/5 节与第 1/6 节结构性错位，且同题多跑每次展示不同定义（10-13 锚在
# `dept_level1`、10-16 16:26 锚在 `user_name`）。
# 根因：run_sql 通道有 tier0/0b 值匹配 + 通道闸，Cube 通道**只有位置判据**。
# 修法：在全部候选上跑同一套值匹配（`_pick_by_value_match`），命中不了就**如实说**
# 并把本轮其余取数调用列出来——派生表场景（模型把 104+48+16 自己加成 168）任何单次
# 调用都匹配不上，此时「列清单 + 注记」是唯一诚实的表达。
_CUBE_MULTI_HEAD = (
    "注：本轮子任务共发出 {n} 次 Cube **取数**调用{preview}，"
    "上面的查询定义只是其中一次（{which}）。"
    "报告正文的数字可能来自其他几次调用，不能只拿本节复现。"
)
_CUBE_MULTI_PREVIEW = "（另有 {p} 次 `sql_only` 预览，未取数）"
_CUBE_WHICH_MATCHED = "值匹配对得上正文表格的那一次"
_CUBE_WHICH_LAST = (
    "最后一次成功的那一次——**未能唯一对上正文表格**"
    "（正文数字多为多次调用汇总或模型二次派生，任何单次调用都复现不了）"
)
_CUBE_MULTI_LIST_HEAD = "本轮全部取数调用（按时间序，★=本节）："
# 「哪一次」的短说法：物理 SQL 节的注记是**单段**文本（同一句里再嵌长解释会变成
# 嵌套括号），只用长说法的一半
_CUBE_WHICH_MATCHED_S = "值匹配对得上正文表格的那次"
_CUBE_WHICH_LAST_S = "最后一次成功的那次（未能唯一对上正文表格）"
_CUBE_PHYSICAL_MULTI = (
    "注：本轮共 {n} 次 Cube 取数调用，本条语句只是其中一次（{which}）；"
    "正文中由其他调用产出的数字不能拿它复现。"
)


def _cube_call_summary_line(call: dict, current: bool) -> str:
    """一次 Cube 取数调用的单行摘要（cube/维度/度量/筛选/时间）。"""
    a = call.get("args") or {}
    parts = ["cube=" + str(a.get("cube") or "?")]
    for k in ("dimensions", "measures", "filters", "time_dimension"):
        v = a.get(k)
        if v:
            parts.append(k + "=" + (v if isinstance(v, str)
                                    else json.dumps(v, ensure_ascii=False)))
    head = a.get("granularity")  # 旧入参形态：粒度在顶层而非 time_dimension 里
    if head:
        parts.append("granularity=" + str(head))
    # 一律用列表项前缀（★ 标出本节那一次）：混用「★」「-」会让 Markdown 把无前缀行
    # 并进上一条列表项，长定义串成一段读不了
    return ("- ★ " if current else "- ") + "；".join(parts)


def _cube_physical_multi(info: dict) -> str:
    """多次取数时给**物理 SQL 节**补的一句（单次返回空串 ⇒ 老报告逐字不变）。

    单段文本、无换行：物理节是 `f"> {plan_note}"` 就地拼接，行内换行会被 Markdown
    折成一个空格，没有块引用逐行问题（`_cube_multi_note` 那边不是因为排版才逐行加
    `> ` 的，是块引用逐行判定）。
    """
    if int((info or {}).get("data_calls") or 0) <= 1:
        return ""
    return _CUBE_PHYSICAL_MULTI.format(
        n=info["data_calls"],
        which=(_CUBE_WHICH_MATCHED_S if info.get("matched") else _CUBE_WHICH_LAST_S),
    )


def _cube_multi_note(info: dict) -> str:
    """多次取数时的如实注记；只取过一次数（或没取到数）返回空串——老报告一字不变。

    每行自带 `> ` 前缀：报告侧渲染是 `md_parts += ["", f"> {sql_note}"]`，Markdown
    块引用**逐行**判定，多行不各自带前缀就会掉出块引用、单换行被折成空格。
    """
    n = int(info.get("data_calls") or 0)
    if n <= 1:
        return ""
    which = _CUBE_WHICH_MATCHED if info.get("matched") else _CUBE_WHICH_LAST
    preview = ""
    if info.get("preview_calls"):
        preview = _CUBE_MULTI_PREVIEW.format(p=info["preview_calls"])
    lines = [
        _CUBE_MULTI_HEAD.format(n=n, preview=preview, which=which),
        ">",
        _CUBE_MULTI_LIST_HEAD,
    ]
    cur_seq = (info.get("call") or {}).get("seq")
    for c in info.get("data_list") or []:
        lines.append(_cube_call_summary_line(c, c.get("seq") == cur_seq))
    return "\n> ".join(lines)


def _pick_cube_call_for_report(messages) -> dict:
    """报告侧 Cube 锚点：**值匹配优先**，退回「最后一次成功」。

    返回 ``{}``（本批消息没有 Cube 调用，调用方走老路径）或::

        {"call": <候选 dict>, "matched": bool, "tier": "tier0"/"tier0b"/"",
         "data_calls": int, "preview_calls": int, "data_list": [候选, …]}

    - **候选池** = 成功且**非** `sql_only` 的调用（`sql_only` 只回编译出的 SQL、不返回
      数据，无值可匹配，与 `judge_sql_origin` 的 `cube_data_calls` 同口径）；池空时退回
      「成功的调用」（可能与改动前一样是预览），仍空则退全部——**任何一侧都不丢节**。
    - `matched=False` 表示值匹配一整层都没命中（本例就是：正文的 168 是模型把
      104+48+16 自己加出来的），此时 `call` = 池内最后一次成功调用，即改动前行为；
      调用方据 `matched` 出如实注记（`_cube_multi_note`）。
    """
    try:
        cands = _extract_cube_calls(messages)
    except Exception as e:  # noqa: BLE001  审计旁路，失败退回老行为
        _logger.debug("[check_progress] Cube 候选枚举失败: %s", f"{type(e).__name__}: {e}")
        return {}
    if not cands:
        return {}
    ok_cands = [c for c in cands if c.get("ok")]
    data = [c for c in ok_cands if not (c.get("args") or {}).get("sql_only")]
    preview = [c for c in ok_cands if (c.get("args") or {}).get("sql_only")]
    pool = data or ok_cands or cands

    scored = []
    for c in pool:
        ri = c.get("result_index")
        rows, _cols = _tool_result_shape(messages, ri) if ri is not None else (0, [])
        scored.append({
            "call": c,
            "rows": rows,
            "values": _run_sql_cell_values(messages, ri) if ri is not None else set(),
            "row_values": _run_sql_row_values(messages, ri) if ri is not None else [],
        })
    vi, tier = _pick_by_value_match(scored, messages)
    matched = vi is not None
    pick = dict(scored[vi]["call"] if matched else pool[-1])
    pick["skipped_failed"] = sum(
        1 for c in cands if not c.get("ok") and c.get("seq", (0, 0)) > pick.get("seq", (0, 0))
    )
    return {
        "call": pick, "matched": matched, "tier": tier,
        "data_calls": len(data), "preview_calls": len(preview), "data_list": data,
    }


def _extract_last_sql(messages) -> str:
    """从子线程消息提取真实执行 SQL，供报告装配（build_report）使用。

    nl2sql 子 agent 的最终回复通常只有数据表+分析文字，SQL 只在中间的 run_sql
    工具调用里（wrenai_<库名>_run_sql / dbmcp_run_sql，sql 在 tool_calls[].args.sql）。
    这里确定性提取，不依赖 LLM 在最终回复中附带 SQL。

    不再简单取"消息序列最后一条 run_sql"：子 agent 在产出最终结果后可能还跑
    探值/核查 SQL（如 DISTINCT 取值、COUNT(*)、HAVING 重名核查），这些不是产出
    结果表的 SQL（trace 8ccef016 曾把 HAVING 重名核查 SQL 误当执行 SQL 附给报告）。

    启发式（按优先级）：
      0) 值匹配：候选 run_sql 返回值集合能整行覆盖最终 AI 消息数据表的行数最多者
         （同命中优先 rows==命中 的精确对齐、其次消息序后者）。最终答案是中文表头
         （工号|姓名…）而 run_sql 列全是英文时，列名∩表头恒空、“行数最多”会选中
         探值分布——trace 99903681 曾把 24 行 date×status GROUP BY 探值误当 12 行
         反连接明细附给报告。值匹配按“行单元值”而非列名/行数，天然选中真正产出
         明细表的那条；纯探值（CURRENT_DATE/MIN-MAX/分布）返回值不进最终表 → hit=0。
      0b) 标量摘要对齐：最终表是「单数值列」摘要表时（整行匹配因中文指标名/口径
         说明无对应返回值而必然落空），选返回单元格值集**覆盖全部最终数值**的候选——
         产出标量（单行多列，如两个 COUNT(*)）是唯一全覆盖者；分布/DISTINCT/Top-N
         探值至多覆盖部分数值 → 出局。识别不限定列数：指标|数值 两列（trace d60b
         431/1736）与 统计项|口径|数量 三列口径表（trace 01a07043 431/280）都启用，
         只要恰有一列整列纯数字。trace d60b 曾把部门人数 Top-5 明细（5 行）误当
         “行数最多”附给 431/1736 标量答案；01a07043 曾把 2 行 deleted 分布误当产出
         附给三列标量答案。同全覆盖者优先行数少、再消息序后者。
      1) 结果列名与子 agent 最终答案表格表头匹配的 run_sql；
      2) 返回行数最多的 run_sql（最终数据表通常是最大结果集）；
      3) 兜底：最后一条 run_sql。

    ⚠️ 兜底层（2/3）之前有一道**通道闸**（2026-09-29 修，生产 trace dcce39a0）：
    tier 0/0b/1 全落空 = 选中的这条 SQL **没有任何「产出了最终表」的证据**，此时若同批
    消息里存在成功取数的 Cube 调用（`_cube_produced_data`）⇒ 数据来自 Cube 通道，那条
    run_sql 只是探值 ⇒ 返回 ""，交回 Cube 通道（调用方的 `else` 分支如实给查询定义 +
    编译后的物理 SQL，来源判 `cube_metric`）。**只在无证据时生效**：任何一层命中
    （值/标量/列名对得上最终表）都照旧返回那条 SQL，无论通道是 Cube 还是 run_sql。
    """
    sqls = []  # [{sql, rows, cols, values, row_values}]
    for i, m in enumerate(messages):
        if isinstance(m, dict):
            role = m.get("role") or m.get("type")
            name = m.get("name") or ""
        else:
            role = getattr(m, "type", "")
            name = getattr(m, "name", "") or ""
        if role not in ("tool", "tool_result") or "run_sql" not in name:
            continue
        sql, rows, cols = _run_sql_meta(messages, i)
        if not sql:
            continue
        sqls.append(
            {
                "sql": sql,
                "rows": rows,
                "cols": cols,
                "values": _run_sql_cell_values(messages, i),
                "row_values": _run_sql_row_values(messages, i),
                "ok": not _tool_result_error(m),
            }
        )

    if not sqls:
        return ""

    # 失败的 run_sql（langchain 错误包装 / 只读拒绝）不可能是「产出结果的 SQL」：
    # 候选里剔掉它们（生产 thread 01a0a850 的 Cube 通道同类问题见
    # _extract_last_cube_call）。**全部失败时退回全集**——不因过滤把 SQL 节整节抹掉，
    # 保持旧行为（此时报告里的 SQL 本就是失败的语句，但至少可查）。
    sqls = [c for c in sqls if c["ok"]] or sqls

    # ── tier 0 / 0b：值匹配（最高优先）──────────────────────────
    _vi, _ = _pick_by_value_match(sqls, messages)
    if _vi is not None:
        return sqls[_vi]["sql"]

    # ── tier 1：列名 ∩ 表头 ───────────────────────────────────────
    final_headers = _final_table_headers(messages)
    if final_headers:
        for s in reversed(sqls):
            if s["cols"] and set(s["cols"]) & final_headers:
                return s["sql"]

    # ── 通道闸：无证据的兜底候选 vs Cube 通道（2026-09-29）─────────────
    # 生产 trace dcce39a0（发版后用户实测）：数据由 4 次 `query_cube`（cube
    # `bug_quality`，measures bug_count / finished_count 按 create_time:day）产出，
    # 但中途跑过一次 `SELECT CURRENT_DATE AS today, DATE_FORMAT(...) ...` 探值——
    # 那是**唯一**一条 run_sql。答案表是「日期|新增|完成」的中文表头日明细，与探值
    # 三列（today/month_start/month_end）无值/列名交集 ⇒ tier0/0b/1 全落空 ⇒ tier2
    # 「行数最多」把这条恒 1 行的探值当成了产出 SQL。后果不止「SQL 选错」：调用方
    # `if sql:` 分支据此压掉了 `else` 里的 Cube 查询定义 + `plan_cube_sql`（真实
    # 物理 SQL **整节消失**），`judge_sql_origin` 也因「锚点之前没有成功 cube 调用」
    # 判成 `llm_from_schema`——报告里那句「模型依据语义库 schema 手写（未使用 Cube
    # 具名指标）」正好与事实相反。
    # 判据：兜底层选中的 SQL 本身**没有任何产出证据**，而同批消息里有成功取数的 Cube
    # 调用 ⇒ 数据来自 Cube。返回 ""（合法返回值，契约里「没有产出 SQL」即空串），
    # 让调用方走 Cube 通道如实渲染。tier 0/0b/1 命中时根本走不到这里，故正例不受影响。
    if _cube_produced_data(messages):
        _logger.info(
            "[check_progress] run_sql 候选无产出证据（tier0/0b/1 全落空）且有成功取数的 "
            "Cube 调用 → 判数据来自 Cube 通道，不用探值充当产出 SQL（候选 %d 条，"
            "行数 %s）",
            len(sqls), [c["rows"] for c in sqls],
        )
        return ""

    # ── tier 2：行数最多 ──────────────────────────────────────────
    best = max(sqls, key=lambda s: s["rows"])
    if best["rows"] > 0:
        return best["sql"]

    # ── tier 3：兜底最后一条 ──────────────────────────────────────
    return sqls[-1]["sql"]


# 选中 SQL 若为多行明细（整表复现最终“明细”数据表），随 check 结果给 build_report
# 附一行口径注：报告里的汇总口径数值（应报工池/已报工等）来自伴生计数查询，不能
# 用这条明细去对 186/174 这类汇总数（trace 99903681 报告「执行 SQL」错选同类背景）。
_DETAIL_SQL_NOTE = (
    "本条 SQL 可直接运行，运行结果即上方『明细』数据表。"
    "报告中的汇总口径数值（如应报工池/已报工总数）由伴生计数查询得出，"
    "请结合『数据结果』口径说明理解本条明细的统计范围。"
)

# wren 语义层通道注记：wrenai_*_run_sql 执行的 SQL 是 wren/PG 方言，由 wren
# 引擎编译为目标库（MySQL）SQL 后执行——「可直接运行」只对语义层成立，用户拿它
# 到 DBeaver 直连 MySQL 必报语法错（2026-09-08 实锤：报告 SQL 含
# `CURRENT_DATE - INTERVAL '1 year'`，PG 字面量，MySQL 需 `INTERVAL 1 YEAR`）。
_WREN_DIALECT_NOTE = (
    "本条 SQL 经 wren 语义层（{tool}）执行，为 wren/PG 方言，"
    "引擎已自动编译为目标库 SQL 后运行。"
    "在 MySQL 客户端（如 DBeaver）直接运行需先转换方言，"
    "如 `INTERVAL '1 year'` → `INTERVAL 1 YEAR`、"
    "`date_trunc('week', d)` → MySQL 日期函数（YEARWEEK/DATE_SUB 等）。"
)
_DETAIL_TAIL_NOTE = (
    "运行结果即上方『明细』数据表。"
    "报告中的汇总口径数值（如应报工池/已报工总数）由伴生计数查询得出，"
    "请结合『数据结果』口径说明理解本条明细的统计范围。"
)

# ── 结果锚点：收尾语顶掉终稿（2026-09-14 修复）─────────────────────────
# 协议要求子 agent 收尾前调 write_todos → 最后一轮必然是工具调用 → 模型随后
# 必然再补一条「工具已执行」性质的短消息。于是 messages[-1] 是收尾语（实测
# 55 / 78 字），而子 agent 自己撰写的终稿（带数据表，实测 2779 / 2336 字）被顶掉。
# 主 agent 拿到的 result 成了空话，只能自己去工作区翻历史文件凑数（trace
# 01a09f1c：报告「数据结果」节变成收尾语，另花 6 次工具调用找数）。
# ⚠ 判据绝不能是「无 tool_calls」——真终稿恰恰带着 write_todos 调用。
_SUBSTANTIVE_MIN_CHARS = 200   # 前一条要被视为「终稿」的长度门槛
_CLOSER_MAX_CHARS = 120        # 末条短于此 → 可能是收尾语（实测 55 / 78 字）
_CLOSER_HARD_SHORT = 80        # 短于此不必看措辞，直接判为收尾语
_CLOSER_RATIO = 3              # 且前一条至少是末条的 3 倍 → 才回退
# 双信号：只看长度会把「中等长度的真答复 + 前文一段长推理」误判（回退太远），
# 所以除长度外还要求末条带收尾措辞。两种实测措辞都命中：
#   「以上为查询结果的全部内容…」(55) / 「查询完毕！以上为…请随时告知」(78)
_CLOSER_MARKERS = (
    "以上为", "以上是", "以上就是", "查询完毕", "如需", "请告知", "请随时",
    "希望对你", "如有需要", "有其他需要",
)


def _looks_like_closer(text: str) -> bool:
    """末条是否像「工具已执行完」的礼貌收尾语（长度 + 措辞双信号）。"""
    t = (text or "").strip()
    if len(t) < _CLOSER_HARD_SHORT:
        return True
    return any(k in t for k in _CLOSER_MARKERS)


def _msg_role(m) -> str:
    """消息角色（兼容 dict 的 type/role 与对象属性）。"""
    if isinstance(m, dict):
        return str(m.get("type") or m.get("role") or "")
    return str(getattr(m, "type", "") or getattr(m, "role", ""))


def _pick_result_message(messages) -> tuple[int, str, str]:
    """选取承载结果的消息：返回 (下标, 文本, 选取原因)。

    取「最后一条有实质内容的 AI 消息」：末条 AI 文本呈收尾语形态时回退一条，
    否则用末条。收尾语形态 = 长度 < _CLOSER_MAX_CHARS 且（< _CLOSER_HARD_SHORT
    或带 _CLOSER_MARKERS 措辞）且前一条长度 ≥ max(_SUBSTANTIVE_MIN_CHARS,
    _CLOSER_RATIO × 末条)。只回退一条——观测到的结构就是「终稿(带 write_todos)
    → tool → 收尾语」，回退更多步会把中间态推理当结果；双信号（长度+措辞）是
    为了不把「中等长度的真答复 + 前文长推理」误判成收尾语。
    无 AI 消息时退回最后一条消息（任何角色），保证与旧行为同构。
    """
    ai_idx = [i for i, m in enumerate(messages)
              if _msg_role(m) in ("ai", "assistant") and _msg_content_str(m).strip()]
    if not ai_idx:
        last = messages[-1]
        return len(messages) - 1, _msg_content_str(last), "last_message_fallback"
    last_i = ai_idx[-1]
    last_txt = _msg_content_str(messages[last_i])
    if len(ai_idx) >= 2:
        prev_i = ai_idx[-2]
        prev_txt = _msg_content_str(messages[prev_i])
        if (len(last_txt) < _CLOSER_MAX_CHARS
                and _looks_like_closer(last_txt)
                and len(prev_txt) >= max(_SUBSTANTIVE_MIN_CHARS,
                                         _CLOSER_RATIO * max(len(last_txt), 1))):
            return prev_i, prev_txt, "preceding_ai"
    return last_i, last_txt, "last_ai"


# ── Cube 快速通道的「查询定义」──────────────────────────────────────
# wrenai_<库名>_query_cube 不产生 run_sql（Cube 语义层由 wren 引擎在服务端编译为
# 目标库 SQL 执行），_extract_last_sql 的 `"run_sql" in name` 过滤结构性取空 →
# 报告的「执行 SQL」节整节消失（实测同日同题：run_sql 通道 16087 字含 SQL 节，
# Cube 通道 8450 字零 SQL 字样）。MCP 只回 {columns, rows, row_count, truncated}，
# 拿不到编译后的语句，所以如实给查询定义，不伪造一条不存在的 SQL。
_CUBE_NOTE = (
    "本条为 Cube 语义层查询定义（非 SQL）：wren 引擎把它编译为目标库 SQL 后执行。"
    "本次未能取到编译后的物理 SQL（不影响查询结果与数据表）。"
)
_CUBE_DEF_NOTE = (
    "以上是本次实际下发 SQL 的 Cube 语义层来源（可读性更好，但不能在 MySQL "
    "直接执行——`v_*` 是 MDL 视图，物理库里不存在）。"
)
_CUBE_SKIPPED_NOTE = (
    "另：本节所取调用之后还有 {n} 次 Cube 调用未成功（引擎没编译出语句、也没产出数据），"
    "本节不展示它们。"
)
_CUBE_ALL_FAILED_NOTE = (
    "本次所有 Cube 调用都未成功（引擎未编译出语句），没有产出数据。"
)


def _extract_last_cube_query(messages) -> str:
    """最后一次 Cube 查询的查询定义文本；非 Cube 通道返回空串。"""
    return "\n".join(_extract_last_cube_call(messages).get("lines") or [])


# ── 物理 SQL：进程内复算并附到 check 结果 ─────────────────────────────
# 动机见 agent/utils/wren_plan 模块头：两条通道的工具返回体里都没有真正下发的
# 语句（Cube 通道连语义层 SQL 都没有），报告里的「执行 SQL」因此要么缺失、要么
# 是粘进 MySQL 必报错的 MDL 视图 SQL。
_PHYSICAL_SQL_HEAD = (
    "以上语句由 wren 引擎编译后**实际下发**到 {dialect}，可直接在 {dialect} "
    "客户端执行（与「数据结果」同源）。"
)
_PHYSICAL_LIMIT_NOTE = (
    "末尾 `LIMIT {n}` 由 wren 连接器追加（为判断结果是否被截断而多取 1 行），不需要可删。"
)
_PHYSICAL_DUP_LIMIT_NOTE = (
    "注：该查询自带 limit，wren 连接器仍会再追加一行 `LIMIT`（上游既有行为），"
    "在客户端执行时只保留一条即可。"
)
_PHYSICAL_WINDOW_NOTE = (
    "注：本次调用带了 {window}——wren 会把 Cube 的 limit/offset 编译进语句、连接器"
    "又追加一条 LIMIT（offset 还会生成 MySQL 不允许的「无 LIMIT 的 OFFSET」），"
    "线上必报语法错；平台已改为在**结果集**上截取，故实际下发语句不含 LIMIT，"
    "窗口大于 {cap} 行时最多只取 {cap} 行。"
)
_SEE_PHYSICAL_NOTE = (
    "可执行版本见「执行 SQL（物理，实际下发）」节（已展开 MDL 视图并转为目标库方言）。"
)
def _physical_sql_note(plan: dict) -> str:
    """物理 SQL 节的注记（含 LIMIT 说明；双 LIMIT / 平台截窗场景如实提示）。"""
    dialect = str(plan.get("dialect") or "").upper() or "目标库"
    note = _PHYSICAL_SQL_HEAD.format(dialect=dialect)
    n = plan.get("limit_appended")
    if n:
        note += _PHYSICAL_LIMIT_NOTE.format(n=n)
    if plan.get("dup_limit"):
        note += _PHYSICAL_DUP_LIMIT_NOTE
    if plan.get("window_limit") is not None or plan.get("window_offset") is not None:
        note += _PHYSICAL_WINDOW_NOTE.format(
            window=_window_text(plan.get("window_limit"), plan.get("window_offset")),
            cap=DEFAULT_ROW_LIMIT,
        )
    return note


def _window_text(limit, offset) -> str:
    """``limit=200、offset=5`` / ``limit=200`` / ``offset=5`` 形态的窗口描述。"""
    parts = []
    if limit is not None:
        parts.append(f"limit={limit}")
    if offset is not None:
        parts.append(f"offset={offset}")
    return "、".join(parts)


def _attach_physical_plan(result: dict, plan: dict, kind: str, label: str) -> bool:
    """复算出的物理 SQL 落到 check 结果：**全量进文件**、结果里只放指针与小字段。

    为什么不内联：实测物理 SQL 3.5~10.9 KB，内联会把整条 check 结果顶破
    MessageSlimmerMiddleware 的 8000 字符阈值 → 结果连同 full_result_files 指针
    被落盘替换成 1000 字符预览。落盘后结果只增约百字节，report_builder 读盘内嵌
    （报告正文本身是文件，不进 state）。

    拿不到 plan（未建模 / 复算失败）时**一个字段都不加**，行为与改动前完全一致。
    返回是否附上。
    """
    if not isinstance(plan, dict) or not plan.get("dialect_sql"):
        return False
    try:
        sql = plan["dialect_sql"]
        ptr = ""
        try:
            from agent.middlewares.langfuse_span import _active_workspace_path

            root = _active_workspace_path()
            ptr = write_plan_file(root, _session_thread_id(), kind, label, sql)
        except Exception as e:  # noqa: BLE001  落盘失败仍有内联兜底
            _logger.debug("[check_progress] 物理 SQL 落盘异常: %s", f"{type(e).__name__}: {e}")

        result["dialect"] = plan.get("dialect") or ""
        result["dialect_sql_chars"] = len(sql)
        result["physical_sql_note"] = _physical_sql_note(plan)
        if plan.get("cube_sql"):
            result["cube_sql"] = plan["cube_sql"]
        if ptr:
            result["dialect_sql_file"] = ptr
        if not ptr or len(sql) <= PLAN_INLINE_MAX:
            # 落盘不可用、或语句本身不长 → 内联一份兜底（report 优先读文件）
            result["dialect_sql"] = sql
        return True
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.debug("[check_progress] 物理 SQL 附加失败: %s", f"{type(e).__name__}: {e}")
        return False


def _selected_sql_exec_meta(messages, sql) -> tuple[str, int]:
    """执行过选中 SQL 的 run_sql 工具名与返回行数（如 ("wrenai_WIT_run_sql", 42)）。

    找不到返回 ("", 0)。工具名用于执行通道判别：
    wrenai_* = wren 语义层（wren/PG 方言，引擎编译为目标库）；
    dbmcp_*  = 直连执行（目标库原生方言，「可直接运行」成立）。
    """
    for i, m in enumerate(messages):
        if isinstance(m, dict):
            role = m.get("role") or m.get("type")
            name = m.get("name") or ""
        else:
            role = getattr(m, "type", "")
            name = getattr(m, "name", "") or ""
        if role not in ("tool", "tool_result") or "run_sql" not in name:
            continue
        s, rows, _ = _run_sql_meta(messages, i)
        if s == sql:
            return name, rows
    return "", 0


def _selected_sql_call_limit(messages, sql):
    """选中的 SQL 那次工具调用的 ``limit`` 入参（None = 未传，wren 默认按 1000 处理）。

    复算实际下发语句时要靠它还原连接器追加的 ``LIMIT n``
    （``n = min(limit or 1000, 10000) + 1``）。多次命中取最后一次（产出最终结果的那次）。
    """
    if not sql:
        return None
    limit = None
    for m in messages:
        tcs = (m.get("tool_calls") if isinstance(m, dict)
               else getattr(m, "tool_calls", None)) or []
        for tc in tcs:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            if not (isinstance(name, str) and name.endswith("run_sql")):
                continue
            if not isinstance(args, dict):
                continue
            s = args.get("sql", "")
            if isinstance(s, str) and s.strip() == sql:
                limit = args.get("limit")
    return limit


def _selected_sql_is_detail(messages, sql) -> bool:
    """选中的 SQL 是否曾以多行明细（rows>1）返回——是则报告附口径注。"""
    _, rows = _selected_sql_exec_meta(messages, sql)
    return rows > 1


def _build_sql_note(messages, sql) -> str:
    """通道感知的报告 SQL 注记（report_builder 渲染为「执行 SQL」节 blockquote）。

    - wren 语义层通道：一律附方言提示（明细再加口径尾巴）——不论明细与否，
      该 SQL 都不能在 MySQL 客户端直跑；
    - 直连/未知通道：仅多行明细时附旧版口径注（「可直接运行」声明此时成立）。
    """
    tool_name, rows = _selected_sql_exec_meta(messages, sql)
    if tool_name.startswith("wrenai_"):
        note = _WREN_DIALECT_NOTE.format(tool=tool_name)
        if rows > 1:
            note += _DETAIL_TAIL_NOTE
        return note
    if rows > 1:
        return _DETAIL_SQL_NOTE
    return ""


# ── dry_run 的 process_data 回填：SQL 与产出最终结果表的 run_sql 对齐 ────
# process_data/{技能}/{tool}-{seq}.json 在 dry_run 工具调用边界落盘，当时只能记录
# 干跑版本 A；若子 agent 执行时改为版本 B 产出最终结果表，前端展示的 SQL（check
# 结果 sql 字段，已取产出 run_sql）会与 process_data 不一致。子任务成功后按
# `_extract_last_sql` 确定的产出 SQL 回填，保留原干跑 SQL 便于排查。
#
# ⚠️ 血案（2026-09-26 修）：这里原先是写死的 `_BACKFILL_SKILL = "sql-generation"`
# —— 那是**启发式族名**、不是技能名。落盘目录后来统一成技能名后，该目录只在
# 「线程活动 skill 恰好过期、dry_run 退回族名」时才存在 ⇒ 回填**常年静默返回 0**
# 且无人察觉（本函数 fail-open，不报错也不打日志）。而 `dry_run` 是 5 个 owner 的
# **共享工具**，活动 skill 是 `wren-perf-optimize`（步骤5「改过必重 dry_run」正是
# 这个形态）或 `wren-execution` 时，文件落在那些目录里 —— 换任何一个固定常量都
# 会漏改，与旧的病同型。故改为**遍历全部技能子目录**：隔离本来就由 `dry_sqls`
# 集合（本子任务干跑过的 SQL）承担，目录不该再当第二把筛子。
_BACKFILL_SKIP_DIRS = ("skill_sop", "wren_plan", "query_result", "_manifest")


def _session_thread_id() -> str:
    """读「会话线程 id」，与 langfuse_span._thread_id 同源。

    process_data 落盘目录按会话线程 id 组织（nl2sql_process_data/{session_thread_id}/…），
    而 check_async_task 拿到的 task["thread_id"] 是子 agent 线程 id。这里读
    config.metadata.langfuse_session_id（HTTP 层注入的会话级权威值），无则回退
    configurable.trace_parent_thread_id / thread_id。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            meta = cfg.get("metadata") or {}
            if meta.get("langfuse_session_id"):
                return str(meta["langfuse_session_id"])
            configurable = cfg.get("configurable") or {}
            if configurable.get("trace_parent_thread_id"):
                return str(configurable["trace_parent_thread_id"])
            if configurable.get("thread_id"):
                return str(configurable["thread_id"])
    except Exception:  # noqa: BLE001
        pass
    return ""


def _current_db_name() -> str:
    """读当前库名（主 agent configurable.db_name，S3-2 口径护栏用）。

    configurable 读不到 → 回落**会话账本** `thread_db`（恰好记着一个库才采用）。

    ⚠️ 兜底不是防御性编程，是生产实证（2026-09-29，会话 `01a0eb59`）：manifest 只由
    **续跑 run** 写，而那条 run 的 config 是 `sync_subagent_todos._run_context_config`
    从「上一个 run」捞 db_name 手拼的——**某一环捞不到就断链**，且下一个续跑又拿本 run
    当「上一个 run」⇒ 该会话后续全空（实测 77 份 manifest：64 填 / 13 空）。断链时
    `configurable` 只剩 user_id，`thread_db` 里却有一行（该表由 HTTP 层在
    `configurable.db_name` 非空时写、且写的是**钳制之后**的值）⇒ 本兜底与
    `report_builder._current_db_name` **同源同闸**（那边 09-26 已踩过同一个坑）。

    影响面不止审计字段：本值还直接喂 `caliber_sql_warning(sql, _current_db_name())`，
    空值 ⇒ `lookup_spec("")` 返回 None ⇒ **S3-2 口径护栏静默失效**。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            db = str((cfg.get("configurable") or {}).get("db_name", "") or "")
            if db:
                return db
    except Exception:  # noqa: BLE001
        pass
    try:
        tid = _session_thread_id()
        if not tid:
            return ""
        from agent.auth.grants import dbs_for_thread

        cands = [d for d in dbs_for_thread(tid) if d]
        if len(cands) == 1:
            _logger.info(
                "[check_progress] configurable 无 db_name，回退会话账本：库 %s", cands[0]
            )
            return cands[0]
        _logger.info(
            "[check_progress] configurable 无 db_name，会话账本记着 %d 个库 → 不猜",
            len(cands),
        )
    except Exception as e:  # noqa: BLE001  兜底失败 = 维持原行为（空串）
        _logger.debug("[check_progress] 会话账本兜底失败: %s", f"{type(e).__name__}: {e}")
    return ""


def _question_identity(thread_id: str = "") -> tuple[str, str]:
    """(question_id, user_question)：config.metadata 优先，缺则查 trace_bind 的子任务绑定。

    ⚠️ 这个兜底同样是生产实证倒逼的（2026-09-29，会话 `01a0eb59`，trace `dcce39a0…`）：
    manifest 只在**续跑 run** 里写，而那条 run 的两个问题身份键都拿不到——
      - `user_question`：续跑 run 的 config 是手拼的（只有 configurable），`input.messages`
        又是以 `[系统通知]` 开头的合成消息 —— `langfuse_metadata._extract_question_summary`
        **有意跳过** `[系统` 前缀 ⇒ HTTP 层虽会合并 metadata（`langfuse_session_id` 等都在），
        但**结构性没有** `user_question` 这个键；
      - `question_id`：`_question_id()` 的三级优先里 1/2 只有子 run 才被注入、3（OTel
        活跃 span）在主 run 链路上拿不到（见 `_current_otel_trace_id` docstring）。
    实测后果：77 份 manifest 里这两个字段 **77/77 全空**，且 `_stem()` 因此退化成
    只用 `sub_thread_id[:8]` ⇒ 同会话多问题的审计件无法按问题区分。

    落盘当刻的权威来源是 `trace_bind` 的 `task_trace`（`langfuse_client` 在**派发
    异步子任务**时登记）：`trace_id` 就是该问题主 run 的 trace id（与 `_question_id()`
    的定义完全一致），`question` 是用户问题原文；键是子任务 id，即本函数的入参。

    fail-open：`langfuse_span` 的两个读值函数本身不抛；查表失败/无行 ⇒ 只补不上的那个
    字段（或原样返回），行为不劣于改动前。
    """
    qid = uq = ""
    try:
        from agent.middlewares.langfuse_span import _question_id, _user_question

        qid = str(_question_id() or "")
        uq = str(_user_question() or "")
    except Exception as e:  # noqa: BLE001  读 config 不是错误路径（离线/测试）
        _logger.debug("[check_progress] 读问题身份失败: %s", f"{type(e).__name__}: {e}")
    if qid and uq:
        return qid, uq
    if thread_id:
        try:
            from agent.trace.trace_bind_store import get_store

            hit = get_store().get_task(str(thread_id))
        except Exception as e:  # noqa: BLE001
            _logger.debug("[check_progress] 查 trace_bind 子任务绑定失败: %s", f"{type(e).__name__}: {e}")
            hit = None
        if hit:
            # get_task 返回 (匹配到的完整 task_id, (main_thread, trace_id, obs, q, desc))
            _key, _val = hit
            _tid, _q = str(_val[1] or ""), str(_val[3] or "")
            qid = qid or _tid
            uq = uq or _q
            _logger.info(
                "[check_progress] 问题身份来自 trace_bind 兜底（续跑 run 的 metadata 没有）："
                " task=%s trace=%s 问题=%s", str(thread_id)[:12],
                (qid or "∅")[:16], (uq or "∅")[:20],
            )
    return qid, uq


# ══════════════════════════════════════════════════════════════════════════
# S8「答案指纹」：这次子任务算的是哪个库 / 哪个指标 / 哪两期 / 哪条 SQL
# ══════════════════════════════════════════════════════════════════════════
# 写入点在图内（`Command(update=...)`，见 `_enhanced_build_check_command`），不走
# `threads.update_state` —— 那里对 in-flight run 是 409 硬闸（见 memory「P2-4」），
# 而此刻主 run 正在跑这次 check。字段名**逐字**沿用磁盘 manifest
# （`process_audit.build_process_manifest`）且复用同一批取值函数，防「state 一份账、
# 磁盘一份账」两套口径漂移 —— 上游要求见 归因分析方案.md §4.9。
_FP_KEY = "_answer_fingerprint"

#: SQL 里处于**比较上下文**的 ISO 日期（`>= '2026-08-01'`），按出现顺序取出。
#: 「必须带比较符」是刻意的：裸日期（`SELECT '2026-08-01'`、格式串、注释、分区边界）
#: 不参与 —— 否则一条含日期字面量的探值 SQL 就能凭空造出两期区间。
_SQL_DATE_PRED_RE = re.compile(r"(?:>=|<=|<>|!=|>|<|=)\s*'?(20\d{2}-\d{2}-\d{2})'?")
#: ISO 日期裸值（用于 cube filters —— 值是日期维的取值，位置本身即比较语义）
_ISO_DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")


def _windows_from_dates(dates: list[str]) -> dict | None:
    """有序 ISO 日期 → ``{"baseline": [s, e], "current": [s, e]}``；形状不认识则 ``None``。

    只认两种**可判**形状，其余一律不猜：
      - **3 个**日期 ⇒ 相邻两期 ``[d0,d1)`` + ``[d1,d2)``（基线结束 = 当期开始，
        日期只出现一次 —— 环比/同比最常见的写法，也是归因内核单测夹具的形状）；
      - **4 个**日期 ⇒ 两段互不相接 ``[d0,d1)`` + ``[d2,d3)``。
    2 个日期 = 只有一期（没有对照期）⇒ ``None``；>4 个 ⇒ 分不清哪两对是工期 ⇒ ``None``。
    半开区间、基线在前（时间序），与 `attribution_core.Window` 的语义一致。
    """
    ds = [d for i, d in enumerate(dates) if d not in dates[:i]]  # 去重保序
    if len(ds) == 3:
        pairs = [(ds[0], ds[1]), (ds[1], ds[2])]
    elif len(ds) == 4:
        pairs = [(ds[0], ds[1]), (ds[2], ds[3])]
    else:
        return None
    if not all(s < e for s, e in pairs) or pairs[0][1] > pairs[1][0]:
        return None
    return {"baseline": [pairs[0][0], pairs[0][1]],
            "current": [pairs[1][0], pairs[1][1]]}


def _extract_windows(messages, final_sql: str) -> dict | None:
    """从最终 SQL / Cube 调用实参里抽两期区间；抽不到 ⇒ ``None``。

    **``None`` 不是失败，是纪律**：归因要的基线/当期必须是**绝对**日期，而「上个月」
    「Q3」这类相对说法**不许**由代码折成日期（猜错会给出一个看着对、实际错期的归因）。
    抽不到就留给工具层要模型显式给出（方案 §4.9）。
    """
    try:
        if final_sql:
            return _windows_from_dates(_SQL_DATE_PRED_RE.findall(final_sql))
        # Cube 通道：日期在 filters 里（`work_date:gte:2026-08-01` / `{"work_date": {...}}`），
        # 不在 Cube 定义的 time_dimensions 里 ⇒ 只能从实参取
        cube = _extract_last_cube_call(messages) or {}
        args = cube.get("args") if isinstance(cube, dict) else None
        if not isinstance(args, dict):
            return None
        flt = args.get("filters")
        seq = list(flt.values()) if isinstance(flt, dict) else (
            flt if isinstance(flt, list) else [])
        blob = " ".join(str(x) for x in seq)
        return _windows_from_dates(_ISO_DATE_RE.findall(blob))
    except Exception as e:  # noqa: BLE001  指纹是旁路，异常只意味着这一格留空
        _logger.debug("[check_progress] 抽两期区间失败: %s", f"{type(e).__name__}: {e}")
        return None


def _build_answer_fingerprint(result: dict, messages, thread_id: str) -> dict:
    """组装「答案指纹」：库 / 问题身份 / 最终 SQL（+摘要）/ cube 口径 / 物理 SQL 文件 / 两期区间。

    只落**已确证**的东西，不落任何结论或数值（数值由归因工具现算，见方案 §4.9）——
    指纹的作用是「同一道题的下一轮追问知道上一轮算的是什么」，不是缓存答案。
    """
    from agent.utils.process_audit import (
        SQL_ORIGIN_MIXED, _digest, _physical_sql_pointer, judge_sql_origin,
    )

    final_sql = str(result.get("sql") or "")
    origin = result.get("sql_origin")  # 成功路径上面已判过，直接用（同源、不重算）
    if not origin:
        try:
            origin = judge_sql_origin(messages, producing_sql=final_sql).get("origin")
        except Exception as e:  # noqa: BLE001  fail-open：来源不明就留空
            _logger.debug("[check_progress] 指纹判 SQL 来源失败: %s", f"{type(e).__name__}: {e}")
            origin = None
    cube_spec: dict = {}
    # 与 manifest 同条件（`build_process_manifest` 只在「无 SQL」或「混合路径」下取 cube
    # 快照）：否则「cube 口径」这一格会与磁盘那份不一致。
    if not final_sql or origin == SQL_ORIGIN_MIXED:
        try:
            from agent.utils.wren_call_extract import cube_snapshot

            cube_spec = (cube_snapshot(messages) or {}).get("spec") or {}
        except Exception as e:  # noqa: BLE001
            _logger.debug("[check_progress] 指纹取 cube 快照失败: %s", f"{type(e).__name__}: {e}")
    qid, uq = _question_identity(thread_id)
    return {
        "db_name": _current_db_name(),
        "question_id": qid,
        "user_question": uq,
        "final_sql": final_sql,
        "final_sql_digest": _digest(final_sql),
        "final_cube_spec": cube_spec,
        "sql_origin": origin,
        "physical_sql_file": _physical_sql_pointer(result),
        "windows": _extract_windows(messages, final_sql),
    }


def _attach_answer_fingerprint(result: dict, messages, thread_id: str) -> None:
    """把指纹挂到 `result` 上（`_FP_KEY` 是内部通道，`_build_check_command` 会 pop 掉）。

    **独立于 manifest 的 try**：manifest 落盘失败不该让指纹也消失（反之亦然）。
    """
    try:
        result[_FP_KEY] = _build_answer_fingerprint(result, messages, thread_id)
    except Exception as e:  # noqa: BLE001  指纹是旁路，异常只意味着这轮没有指纹
        _logger.debug("[check_progress] 组装答案指纹失败: %s", f"{type(e).__name__}: {e}")


def _collect_dry_run_sqls(messages) -> set:
    """收集本子任务干跑（dry_run）过的 SQL。

    用于把 process_data 回填范围限定在当前子任务：进程里一个会话可能跑多个查询
    子任务，process_data 目录按会话线程 id 共享，不限定会把别的任务的干跑文件
    一起改写。按 tool_calls[].name 后缀 dry_run + args.sql 提取。
    """
    dry = set()
    for m in messages:
        if isinstance(m, dict):
            tcs = m.get("tool_calls") or []
        else:
            tcs = getattr(m, "tool_calls", None) or []
        for tc in tcs:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            if not (isinstance(name, str) and name.endswith("dry_run")):
                continue
            s = args.get("sql") if isinstance(args, dict) else ""
            if isinstance(s, str) and s.strip():
                dry.add(s.strip())
    return dry


def _norm_sql(s: str) -> str:
    """SQL 归一化（折叠空白），用于比较时忽略纯空白差异。"""
    return " ".join(s.strip().split())


def _backfill_process_data_sql(messages, producing_sql: str) -> int:
    """把 dry_run 的 process_data 里的 SQL 回填为产出最终结果表的 run_sql。

    范围限定：只改本子任务干跑过的文件（按子线程消息里的 dry_run 工具调用 SQL
    精确匹配），不影响同会话里其它查询任务的中间产物。幂等：与原值一致（含纯
    空白差异）则跳过。返回实际改写文件数。

    扫**全部技能子目录**（不只 `wren-sql-author/`）：`dry_run` 是 5 个 owner 的共享
    工具，落盘归属随当时的活动 skill 变化。见上方 `_BACKFILL_SKIP_DIRS` 的注释。
    """
    producing_sql = (producing_sql or "").strip()
    if not producing_sql or not messages:
        return 0
    dry_sqls = _collect_dry_run_sqls(messages)
    if not dry_sqls:
        return 0
    sid = _session_thread_id()
    if not sid:
        return 0
    try:
        from agent.middlewares.langfuse_span import _active_workspace_path
        root = _active_workspace_path()
    except Exception as e:  # noqa: BLE001
        # 取不到工作区 = **回填整个没做**，但返回值是 0，与「没有需要回填的文件」不可区分
        # （上面那几处 return 0 才是真「无事可做」）。这里必须吵：静默 0 会被当成成功，
        # process_data 里的 SQL 就停留在 dry_run 的探值 SQL 上 —— 与 cube 通道那条
        # 「探值劫持报告 SQL 节」同一类失效。低频（每个子任务收尾一次）。
        _logger.warning("[check_progress] 回填 process_data 取工作区失败，本次回填跳过: %s",
                        f"{type(e).__name__}: {e}")
        return 0
    if not root:
        return 0
    base = Path(root) / "nl2sql_process_data" / sid
    if not base.is_dir():
        return 0
    updated = 0
    for skill_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if skill_dir.name in _BACKFILL_SKIP_DIRS:
            continue  # 非 per-call dump 布局（skill_sop / wren_plan / manifest 等）
        for f in sorted(skill_dir.glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(obj, dict):
                continue
            # 只认干跑文件（平台合成的 `_routing-*.json` 等 `tool` 是 `platform:*`，
            # 不带 input.sql，两道守卫都会跳过它）
            if not str(obj.get("tool") or "").endswith("dry_run"):
                continue
            inp = obj.get("input")
            if not isinstance(inp, dict):
                continue
            cur_sql = str(inp.get("sql") or "").strip()
            if not cur_sql or cur_sql not in dry_sqls:
                continue  # 不是本子任务的干跑文件
            if _norm_sql(cur_sql) == _norm_sql(producing_sql):
                continue  # 已一致，幂等
            inp["sql"] = producing_sql
            obj["_dry_run_sql"] = cur_sql
            obj["_final_sql"] = producing_sql
            obj["_backfilled"] = True
            obj["_backfilled_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
            try:
                f.write_text(json.dumps(obj, ensure_ascii=False, default=str),
                             encoding="utf-8")
                updated += 1
            except OSError:
                _logger.debug("[check_progress] process_data 回填写盘失败: %s", f)
    if updated:
        _logger.info(
            "[check_progress] dry_run process_data 回填 %d 个文件 → 产出 run_sql",
            updated,
        )
    return updated


def _add_incremental(result: dict, thread_values: dict, since: Optional[int]) -> None:
    """给 check 结果附加 cursor；since 给定时只回该游标之后的新消息。"""
    messages = (
        thread_values.get("messages", [])
        if isinstance(thread_values, dict)
        else []
    )
    result["cursor"] = len(messages)
    if since is None:
        return
    try:
        idx = max(int(since), 0)
    except (TypeError, ValueError):
        return
    new_msgs = messages[idx:]
    result["new_messages"] = [_brief_message(m) for m in new_msgs[:_INCREMENTAL_MAX_ITEMS]]
    if len(new_msgs) > _INCREMENTAL_MAX_ITEMS:
        result["new_messages_truncated"] = True


# ══════════════════════════════════════════════════════════════════════
# deepagents async_subagents 替换件（Phase 2-2a 从 apply_patch 内嵌提升）
#
# 提升动机：apply_patch 曾长到 537 行，但其中约 500 行都在内嵌 def 里，真正的
# 『装哪几个件』只有几十行 —— 读的人得先翻过 500 行才看得到装配骨架。
#
# ⚠️ 这些名字必须留在**本模块**：`scripts/verify_*.py` 有 8 个脚本按模块路径
# 读它们，`from x import *` 传不了下划线开头的名字。回归见
# `src/test/unit/test_check_progress_patch.py`。
# ══════════════════════════════════════════════════════════════════════

# ── 终态保护（2026-10-16 生产事故）──────────────────────────────────
# 事故链：watcher 看门狗判超时 → `runs.cancel(action=interrupt)` → 平台 run 状态
# 变 `interrupted` → 主 agent 的 `check_async_task` 读到 interrupted，按下面的
# interrupted 分支归一成 `running` + 「短暂暂停会自动恢复」→
# `_enhanced_build_check_command` 把 `running` 写回 async_tasks，
# **把 watcher 刚落好的 `timeout` 终态打回**，卡片复活、用户白等 35 分钟。
#
# 这里落两条兜底，各自独立生效：
#   ① `_reconcile_result_with_task`：结果产出后与 async_tasks 条目对账，
#      条目已终态 → 结果一律改判为条目终态（工具轮询不得推翻已落终态）。
#   ② `_enhanced_build_check_command` 里的硬闸：无论谁调用都拒绝「终态 → 中间态」。
# 重派发不受影响：`update_async_task` 换 run 时**先**把条目写成 `running`
# （deepagents `async_subagents.py:544`），硬闸因此不成立。
_TERMINAL_TASK_STATUSES = ("success", "error", "timeout", "cancelled")

# interrupted 停留多久就不再算「瞬时暂停」。deepagents 上下文压缩的暂停是秒级；
# 超过这个门槛还停在 interrupted，只会是「被 cancel 后永不恢复」。
_INTERRUPTED_STUCK_GRACE_SECS = 120


def _watchdog_kill_reason(run) -> Optional[str]:
    """该 run 是否被本进程的 watcher 看门狗主动 cancel 过（是 → 原因文案）。

    进程内标记（`sync_subagent_todos.note_watchdog_kill`）是**确定性判据**：
    被看门狗杀掉的 run 与「上下文压缩的瞬时暂停」在 run 状态上完全同形，
    只有杀人者自己知道区别。取不到标记时返回 None，由时长熔断接管 —— 两条判据
    互相独立，重启丢标记也不会退化成「乐观等待」。
    （延迟导入：`sync_subagent_todos` 在本模块 apply_patch 期已被加载，
    模块级导入会形成循环。异常一律 fail-open 视作「没有标记」。）
    """
    try:
        run_id = (run or {}).get("run_id") if isinstance(run, dict) else None
        if not run_id:
            return None
        from agent.subagents.sync_subagent_todos import watchdog_kill_reason
        return watchdog_kill_reason(run_id)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[check_progress] 读看门狗标记失败: %s", f"{type(e).__name__}: {e}")
        return None


def _restart_killed(run) -> bool:
    """该 run 是否被「发版/重启换进程」带走（2026-10-22）。

    判据只有一处：`sync_subagent_todos._killed_by_restart` —— run 停在 interrupted
    且最后一次状态变化早于本进程启动。用户点停止有它自己的证据（async_tasks 已是
    cancelled），不会被判到这里。异常 fail-open 视作「不是重启」。
    （延迟导入：同 `_watchdog_kill_reason`，模块级导入会成环。）
    """
    try:
        from agent.subagents.sync_subagent_todos import _killed_by_restart
        return bool(_killed_by_restart(run))
    except Exception as e:  # noqa: BLE001
        _logger.debug("[check_progress] 重启判据失败（按非重启处理）: %s", f"{type(e).__name__}: {e}")
        return False


def _interrupted_stuck_seconds(run) -> Optional[float]:
    """run 停在 interrupted 已有多久（秒）。

    口径用 `updated_at`：run 状态每次变化都会刷新它，`interrupted` 正是最后一次
    状态变化，故 `now - updated_at` = 已停留在 interrupted 的时长（不能用
    `created_at`，那是 run 创建时刻，长查询跑到第 10 分钟才被中断会被误判）。
    读不到/格式异常返回 None（调用方不得据此熔断，避免误杀）。
    """
    try:
        raw = str((run or {}).get("updated_at") or "").strip()
        if not raw:
            return None
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _reconcile_result_with_task(result: dict, task: dict) -> None:
    """把 check 结果与 async_tasks 条目对账（就地改 result）。

    唯一规则：**条目已终态时，结果不得是非终态**。因为
    `_enhanced_build_check_command` 会用 `result["status"]` 重建整条 async_tasks，
    两者不一致就是「工具轮询把已落终态打回中间态」。
    改判时保留条目里的 `error`（真实的失败原因），并附 `state_note` 说明这是
    谁说的，免得模型把「条目终态 + 结果 running」两套账当成需要自己推理的矛盾。
    """
    try:
        task_status = str((task or {}).get("status") or "")
        result_status = str((result or {}).get("status") or "")
        if task_status not in _TERMINAL_TASK_STATUSES:
            return
        if result_status in _TERMINAL_TASK_STATUSES:
            return
        result["status"] = task_status
        result.pop("note", None)  # 丢弃「会自动恢复」这类乐观说明
        result["error_kind"] = result.get("error_kind") or task_status
        if not result.get("error") and task.get("error"):
            result["error"] = task["error"]
        result["state_note"] = (
            f"This task has already reached a terminal state ({task_status}); "
            f"the live run reported '{result_status}', which a finished task "
            f"never does (a watcher-cancelled run lingers in 'interrupted'). "
            f"Treat the task as {task_status} — it will NOT resume."
        )
        _logger.warning(
            "[check_progress] 条已终态 %s 但 run 报 %s → 结果改判 %s（防终态被轮询打回）",
            task_status, result_status, task_status,
        )
    except Exception as e:  # noqa: BLE001
        _logger.debug("[check_progress] 对账失败(不改判): %s", f"{type(e).__name__}: {e}")


def _enhanced_build_check_result(run, thread_id, thread_values, full=False):
    result = {"status": run["status"], "thread_id": thread_id}
    messages = (
        thread_values.get("messages", [])
        if isinstance(thread_values, dict)
        else []
    )
    # 终态改判（2026-09-28 生产事故）：run 级 status 是 SDK 的事，**内容**里可能躺着
    # 一条失败终局（超时/额度耗尽/未配模型被中间件吞成友好文案后，图照常 END ⇒
    # run 报 success）。不读戳的话，主 agent 会拿到 `status: "success"` + 一段超时
    # 文案，只能在自己的推理里写「marked as success, but the result is actually a
    # timeout error」——契约错，不是模型的错。判定链见 agent/utils/failure_signal.py。
    #
    # ⚠️ 只在 run 已经 success 时读戳：running 时绝不降级（戳可能是上一轮遗留的，
    # 把在跑的 run 判失败会制造新事故）。
    if result["status"] == "success":
        _mark = last_failed_mark(messages)
        if _mark:
            _st = run_status_for(_mark["kind"])
            _detail = detail_of(_mark)
            _logger.warning(
                "[check_progress] 子 run success 但末条是失败终局（kind=%s）→ 改判 %s",
                _mark["kind"], _st,
            )
            run = {**run, "status": _st, "error": _detail or run.get("error")}
            result["status"] = _st
            result["error_kind"] = _mark["kind"]
            if _detail:
                result["error"] = _detail
    if run["status"] == "success":
        sql = ""  # 无消息时保持未定义安全（下面 if messages 分支才赋值）
        if messages:
            # 结果锚点 = 最后一条有实质内容的 AI 消息（不再机械取 messages[-1]）：
            # 子 agent 以工具调用收尾是结构性必然，末条必是短收尾语（见
            # _pick_result_message 注释）。下标一并回传便于排查取错了哪条。
            _src_idx, raw_content, _picked = _pick_result_message(messages)
            summarized = _summarize_result(raw_content, full=bool(full))
            result["result"] = summarized
            result["result_size"] = {
                "chars": len(raw_content),
                "summarized": len(raw_content)
                > (_MAX_RESULT_CHARS_FULL if full else _MAX_RESULT_CHARS),
            }
            result["result_source"] = {
                "index": _src_idx,
                "total": len(messages),
                "picked": _picked,
            }
            if full:
                result["full"] = True
        else:
            result["result"] = "(completed with no output messages)"
        # 报告装配需要真实执行 SQL：从子线程消息提取最后一次 run_sql 附到 result.sql
        sql = _extract_last_sql(messages)
        # SQL 来源判决（cube_metric / cube_metric+llm_outer / llm_from_schema /
        # unknown）—— **写在 if/else 分支之前**，让「有 run_sql」与「纯 Cube
        # 快速通道」两条路都带上标记（报告侧据此决定出不出一行「SQL 生成来源」）。
        # 判据唯一实现在 agent.utils.process_audit.judge_sql_origin，manifest /
        # check 结果 / 报告三处共用同一份，不在这里重写第二套。审计旁路。
        try:
            from agent.utils.process_audit import judge_sql_origin
            _origin = judge_sql_origin(messages, sql)
            result["sql_origin"] = _origin.get("origin")
            result["sql_origin_evidence"] = _origin.get("evidence") or {}
        except Exception as e:  # noqa: BLE001
            _logger.debug("[check_progress] SQL 来源判定失败: %s", f"{type(e).__name__}: {e}")
        if sql:
            result["sql"] = sql
            # 真正下发目标库的物理 SQL：进程内复算后附指针 + 小字段。
            # result["sql"] 是模型写的**语义层** SQL（引用 MDL 视图，粘进 MySQL
            # 跑不了），物理 SQL 另起一节，两者都留、互不覆盖。
            _tool_name, _ = _selected_sql_exec_meta(messages, sql)
            _project, _conn = _resolve_wren_ctx(_tool_name)
            _has_plan = _attach_physical_plan(
                result,
                plan_run_sql(_project, _conn, sql,
                             _selected_sql_call_limit(messages, sql)),
                "run_sql", _tool_name or "run_sql",
            )
            # 通道感知 SQL 注记（_build_sql_note）：wren 语义层通道的 SQL 是
            # wren/PG 方言（引擎编译为目标库执行），「可直接运行」不成立 →
            # 附方言转换提示；直连通道多行明细仍附旧版口径注（别拿明细去对
            # 186/174 之类汇总数）。
            _note = _build_sql_note(messages, sql)
            if _note and _has_plan:
                _note += _SEE_PHYSICAL_NOTE
            if _note:
                result["sql_note"] = _note
            # sql-generation process_data 回填为产出 run_sql（若 dry_run 与
            # 实际执行 SQL 不一致），保证中间产物与前端/报告 SQL 同源
            _backfill_process_data_sql(messages, sql)
            # S3-2 口径后置护栏：最终 SQL 命中「禁止表」→ 告警（软提醒，随 check
            # 结果透传给主 agent，主 agent 决定改默认口径或说明依据）
            try:
                from agent.settings.caliber_spec import caliber_sql_warning
                _warn = caliber_sql_warning(sql, _current_db_name())
                if _warn:
                    result["caliber_warning"] = _warn
            except Exception:  # noqa: BLE001
                pass
        else:
            # 无 run_sql 但走了 Cube 快速通道 → 附「查询定义」（报告里作为
            # 物理 SQL 的语义层来源保留）。run_sql 通道仍以真实 SQL 为准，两者互斥。
            # 锚点选法（2026-10-16，thread 01a14503）：**值匹配优先**——先找「真的
            # 产出了最终答案表」的那次调用（复用 run_sql 通道的 tier0/0b），匹配不上
            # 才退回「最后一次成功」（= 改动前行为），并在多次取数时如实注记。
            _pick = _pick_cube_call_for_report(messages)
            _cube = _pick.get("call") or _extract_last_cube_call(messages)
            if _cube:
                result["cube_query"] = "\n".join(_cube["lines"])
                # `sql_kind` 是历史字段、全仓**无活读者**（删除只制造兼容风险，
                # 故保留原样）。它与新的 `sql_origin` 的关系一句话说清：
                # `sql_kind == "cube"` ⇔ `sql_origin == "cube_metric"`；手写/混合
                # 路径下 `sql_kind` 靠「键缺席」表达，这正是它无法区分混合路径、
                # 需要 `sql_origin` 补位的原因。
                result["sql_kind"] = "cube"
                # 原始定义 + 工具名透传给 build_report：报告「业务口径」两层
                # （LLM 摘要 + 模板结构）要靠它们查 cube 元数据里的中文描述。
                # 传工具名而非项目路径：保持 result 可 JSON 序列化，且报告侧
                # 用同一个 resolve_wren_ctx 自行解析（失败则降级，不影响出报告）。
                result["cube_args"] = _cube.get("args") or {}
                result["cube_tool"] = _cube.get("tool") or ""
                _project, _conn = _resolve_wren_ctx(_cube["tool"])
                _has_plan = _attach_physical_plan(
                    result,
                    plan_cube_sql(_project, _conn, _cube["args"]),
                    "cube", str(_cube["args"].get("cube") or _cube["tool"]),
                )
                # 取到物理 SQL → 定义节改注「来源」；取不到 → 如实说没有
                _note = _CUBE_DEF_NOTE if _has_plan else _CUBE_NOTE
                if not _cube.get("ok"):
                    # 全是失败调用：定义节展示的就是没跑成功的那次，如实标注
                    _note += _CUBE_ALL_FAILED_NOTE
                elif _cube.get("skipped_failed"):
                    # 锚点跳过失败调用 → 说明展示的不是最后一次，防读者误判
                    _note += _CUBE_SKIPPED_NOTE.format(n=_cube["skipped_failed"])
                # 本轮取过不止一次数 → 本节只是其中一次，如实说清并列出全部取数
                # （单次取数时返回空串，报告与改动前逐字节一致）
                _multi = _cube_multi_note(_pick)
                if _multi:
                    _note += _multi
                result["sql_note"] = _note
                # 物理 SQL 节同源同问题：正文数字若不是这一条语句产出的，注记先行说明
                _physical_multi = _cube_physical_multi(_pick)
                if _has_plan and _physical_multi:
                    result["physical_sql_note"] += _physical_multi
        # 大结果全量文件指针（QueryResultOffload 落盘）附到 result，
        # build_report 读盘后把完整结果表嵌入报告正文（0 模型开销）
        full_files = _collect_full_result_files(messages)
        if full_files:
            result["full_result_files"] = full_files
        # 中间产物审计：写 manifest + 平台合成的 routing / cube_summary。
        # 之所以在这里（而不是 langfuse_span 的工具边界）—— 这里是唯一同时具备
        # 「完整子任务消息 + 已算好的锚点 SQL + 用户最终会看到的那份 check 结果」
        # 的地方；工具边界一次只有一条工具消息，看不到全貌。
        # `write_process_artifacts` 自带 fail-open，异常只意味着没有审计件。
        try:
            from agent.middlewares.langfuse_span import _active_workspace_path
            from agent.utils.process_audit import write_process_artifacts
            # 问题身份走 `_question_identity`（metadata → trace_bind 子任务绑定）：
            # 本处所在的是**续跑 run**，其 metadata 结构性没有 user_question（见该函数 docstring）
            _qid, _uq = _question_identity(thread_id)
            _ptrs = write_process_artifacts(
                root=_active_workspace_path(),
                session_thread_id=_session_thread_id(),
                sub_thread_id=thread_id,
                messages=messages,
                question_id=_qid,
                user_question=_uq,
                db_name=_current_db_name(),
                status="success",
                producing_sql=sql,
                result=result,
            )
            if _ptrs.get("manifest"):
                # 小指针（~100 字节），让主 agent/事后审计知道审计件在哪
                result["process_manifest"] = _ptrs["manifest"]
        except Exception as e:  # noqa: BLE001
            _logger.debug("[check_progress] 写中间产物审计失败: %s", f"{type(e).__name__}: {e}")
        # S8 答案指纹：随本次 check 结果一起写回主 agent state（图内 Command），
        # 供追问归因读取。**与 manifest 各自 fail-open**，互不拖累。
        _attach_answer_fingerprint(result, messages, thread_id)
    elif run["status"] == "error":
        error_detail = run.get("error")
        result["error"] = (
            str(error_detail) if error_detail
            else "The async subagent encountered an error."
        )
        # 失败路径也留一份 manifest，闭合审计记录缺口（否则「跑挂了的那些问题」
        # 在审计目录里完全不存在，只剩产物孤儿目录）。
        # status != "success" ⇒ 不写 routing / cube_summary（没有可判的路由结论）。
        # producing_sql 传空 ⇒ `sql_origin` 如实落 `unknown`，不猜。
        try:
            from agent.middlewares.langfuse_span import _active_workspace_path
            from agent.utils.process_audit import write_process_artifacts
            # 同成功路径：问题身份走 `_question_identity`（失败件也要能按问题归属）
            _qid, _uq = _question_identity(thread_id)
            _ptrs = write_process_artifacts(
                root=_active_workspace_path(),
                session_thread_id=_session_thread_id(),
                sub_thread_id=thread_id,
                messages=messages,
                question_id=_qid,
                user_question=_uq,
                db_name=_current_db_name(),
                status="error",
                producing_sql="",
                result=result,
            )
            if _ptrs.get("manifest"):
                result["process_manifest"] = _ptrs["manifest"]
        except Exception as e:  # noqa: BLE001
            _logger.debug("[check_progress] 写失败审计件失败: %s", f"{type(e).__name__}: {e}")
        # 失败路径镜像一份（与成功路径同源同形状）：`final_sql` 空、`sql_origin`
        # 多半 `unknown`，但 `db_name` / 问题身份 / `windows` 一样有效 —— 追问归因
        # 时这几格照样要用（方案 §4.9「失败分支镜像一份」）。
        _attach_answer_fingerprint(result, messages, thread_id)
    elif run["status"] == "interrupted":
        # 审批闸门已移除（2026-08-28 起 sql_approval 只读硬拦截、不再 raise
        # interrupt），但 interrupted 现在有**两个同形来源**，必须分开：
        #   ① deepagents 上下文压缩等瞬时暂停 —— 秒级自恢复；
        #   ② 看门狗 `runs.cancel(action=interrupt)` —— 永不恢复（2026-10-16 事故：
        #      这条被当成 ① 归一成 running，把 watcher 刚落好的 timeout 打回，
        #      还对用户说「会自动恢复」，白等 35 分钟）。
        # 判据取三条独立证据，命中任一条即如实报为失败：
        #   · 重启带走：`updated_at` 早于本进程启动 ⇒ 上一任进程（发版/部署）把它
        #     留下了，既不会自恢复，也不是用户点停止（2026-10-22 用户要求区分）
        #   · 进程内标记：这条 run 就是被我们自己 cancel 的（确定性，无猜测）
        #   · 时长熔断：run 已停在 interrupted 超过 grace，且从未恢复
        _kill_reason = _watchdog_kill_reason(run)
        _stuck_secs = _interrupted_stuck_seconds(run)
        _stuck_too_long = (
            _stuck_secs is not None
            and _stuck_secs > _INTERRUPTED_STUCK_GRACE_SECS
        )
        if _restart_killed(run):
            _logger.warning(
                "[check_progress] 子 run 的 interrupted 早于本进程启动"
                "（updated_at=%s）⇒ 判为服务重启中断（不是用户取消）",
                (run or {}).get("updated_at"),
            )
            result["status"] = "error"
            result["error_kind"] = "service_restart"
            result["note"] = (
                "The subagent was terminated by a service restart/deploy, NOT by "
                "the user pressing stop, and it will NOT resume automatically. "
                "Tell the user it can simply be retried (re-dispatched or the "
                "question re-asked) — do not describe it as 'cancelled by you' or "
                "as a timeout."
            )
        elif _kill_reason or _stuck_too_long:
            _why = _kill_reason or f"stuck in 'interrupted' for {int(_stuck_secs)}s"
            _logger.warning(
                "[check_progress] 子 run 停在 interrupted 且不会恢复（%s）→ 改判 timeout",
                _why,
            )
            result["status"] = "timeout"
            result["error_kind"] = "timeout"
            result["note"] = (
                f"The subagent was terminated ({_why}) and will NOT resume "
                f"automatically. Report this as a failure to the user: it needs "
                f"to be re-dispatched (or the question re-asked) to get a result. "
                f"Do NOT tell the user it will recover on its own."
            )
        else:
            result["status"] = "running"
            result["note"] = (
                "The subagent is briefly paused (e.g. context summarization) and "
                "will resume automatically. Do NOT cancel or re-delegate; check "
                "again later. NOTE: if it is still 'interrupted' several minutes "
                "from now it was terminated by the watchdog, not paused — a later "
                "check will report it as a failure."
            )
    # running / 其他中间态：提取进度（thread_values 一并传入 → 权威源 state.todos 优先）
    if messages or (isinstance(thread_values, dict) and thread_values.get("todos")):
        _extract_progress(result, messages, thread_values)
    return result

def _enhanced_build_check_command(result, task, tool_call_id):
    from langgraph.types import Command
    from langchain_core.messages import ToolMessage

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # ── 终态硬闸（2026-10-16 事故）──
    # 本函数是 `result["status"]` 写进 async_tasks 的**唯一漏斗**，也是最后一个
    # 能拦住「终态被轮询打回中间态」的地方。`_reconcile_result_with_task` 已在
    # 调用点对过账，这里再拦一道：任何调用方（含 scripts/verify_* 直调）都不得
    # 把已终态的条目写回非终态。重派发不受影响——`update_async_task` 换 run 时
    # 条目已被写成 running（deepagents `async_subagents.py:544`）。
    _task_status = str(task.get("status") or "")
    if (
        _task_status in _TERMINAL_TASK_STATUSES
        and str(result.get("status") or "") not in _TERMINAL_TASK_STATUSES
    ):
        _logger.warning(
            "[check_progress] 拒绝把终态 %s 打回 %s（run 侧证据与已落终态矛盾）",
            _task_status, result.get("status"),
        )
    # 判据与文案只有一份：调用点与硬闸共用 _reconcile_result_with_task
    _reconcile_result_with_task(result, task)
    last_updated_at = (
        now if task["status"] != result["status"] else task["last_updated_at"]
    )
    updated_task = {
        "task_id": task["task_id"],
        "agent_name": task["agent_name"],
        "thread_id": task["thread_id"],
        "run_id": task["run_id"],
        "status": result["status"],
        "created_at": task["created_at"],
        "last_checked_at": now,
        "last_updated_at": last_updated_at,
    }
    # M-T5c：保留 description（_tasks_reducer 整条替换，不补会被抹掉）
    if task.get("description"):
        updated_task["description"] = task["description"]
    # 同款：这几个键是 watcher（sync_subagent_todos）写在 async_tasks 里的，本函数
    # 重建整条 → 不显式带上就被主 agent 的一次轮询抹掉。`error` 是失败原因（前端与
    # 失败汇报读它），`failure_reported` 是跨线程/重启的去重标记（抹掉会导致同一条
    # 失败被再汇报一次），`terminal_cause` 区分「重启中断」与「用户取消」的文案成因。
    for _k in ("error", "error_kind", "failure_reported", "terminal_cause"):
        if task.get(_k) is not None and _k not in updated_task:
            updated_task[_k] = task[_k]
    # S8 答案指纹：**先 pop 再 dumps** —— 这个键是 state 内部通道，绝不能出现在给
    # 模型看的 ToolMessage 正文里（否则会被当成可用输入去读/编）。同一份 `result`
    # 既产出消息又产出 state 更新，是本函数作为「两条路径唯一漏斗」的既有性质。
    _fp = result.pop(_FP_KEY, None)
    update: dict = {
        "messages": [ToolMessage(
            json.dumps(result, ensure_ascii=False, indent=2),
            tool_call_id=tool_call_id,
        )],
        "async_tasks": {task["task_id"]: updated_task},
    }
    if isinstance(_fp, dict) and _fp:
        # 按 task_id 键控 + `_merge_dict_by_key` reducer：并发多查询各写各的键，
        # 与 query_headers / subagent_steps_map 同一套机制（不会互相覆盖）。
        update["answer_fingerprints"] = {task["task_id"]: _fp}
    return Command(update=update)

def _reapply_task_descriptions(out, state):
    from langgraph.types import Command

    if not isinstance(out, Command):
        return out
    upd = out.update or {}
    new_tasks = upd.get("async_tasks")
    if not isinstance(new_tasks, dict) or not new_tasks:
        return out
    old_tasks = (state or {}).get("async_tasks") or {}
    merged = {}
    for tid, entry in new_tasks.items():
        old = old_tasks.get(tid) or {}
        if (
            isinstance(entry, dict)
            and not entry.get("description")
            and old.get("description")
        ):
            entry = dict(entry)
            entry["description"] = old["description"]
        merged[tid] = entry
    if merged != new_tasks:
        upd["async_tasks"] = merged
    return out

def _relaunch_sync_after_redispatch(task_id, runtime, out):
    """update_async_task 重派发后重建进度同步器（P0）。

    deepagents 的 update_async_task 走 runs.create(multitask_strategy="interrupt")
    换 run，但**不拉任何 sync watcher**；而 watcher 只由 start_async_task 拉起，
    且终态写入后只多活 ~10s 就退出。于是重派发后 async_tasks[task] 变回 "running"、
    active_queries=true、subagent_steps_map 冻结在中间态 —— 前端卡片永久「执行中」
    （trace a6f86bbd）。这里照抄 api 侧 _ensure_sync_watcher（sql_approval.py /
    task_cancel.py）的存活检查写法：只在 watcher 已退出时拉起（launch_sync 非幂等）。

    pin_run_id=True：只跟踪刚创建的 run，避免窗口期读到旧 run 的终态。
    """
    try:
        from agent.subagents.sync_subagent_todos import (
            is_sync_alive,
            launch_sync,
        )

        if is_sync_alive(task_id):
            return  # watcher 还活着，它自己会看到新 run
        entry = ((getattr(out, "update", None) or {}).get("async_tasks") or {}).get(
            task_id
        )
        if not isinstance(entry, dict):
            entry = (
                (getattr(runtime, "state", None) or {}).get("async_tasks") or {}
            ).get(task_id)
        if not isinstance(entry, dict):
            return
        if str(entry.get("status") or "") != "running":
            return  # 只有「刚被改回 running」的重派发才需要重建 watcher
        main_thread_id = _main_thread_id_from_runtime(runtime)
        if not main_thread_id:
            _logger.warning(
                "[check_progress] 重派发但取不到主线程 id，跳过 watcher 拉起: %s",
                str(task_id)[:8],
            )
            return
        launch_sync(
            main_thread_id,
            task_id,
            str(entry.get("agent_name") or "nl2sql"),
            entry,
            pin_run_id=True,
        )
        _logger.info(
            "[check_progress] 重派发后重建 sync watcher: sub=%s run=%s",
            str(task_id)[:8],
            str(entry.get("run_id"))[:8],
        )
    except Exception as e:  # noqa: BLE001
        # watcher 拉起失败绝不能让工具调用失败
        _logger.warning("[check_progress] 重建 sync watcher 失败: %s", f"{type(e).__name__}: {e}")

def _main_thread_id_from_runtime(runtime) -> Optional[str]:
    """当前 run 所属线程的 id（对主 agent 的工具而言就是主线程 id）。

    `runtime.state` 是图状态通道，不含 thread_id；只能从 run config 取。
    """
    cfg = (getattr(runtime, "config", None) or {}).get("configurable") or {}
    return cfg.get("thread_id") or getattr(
        getattr(runtime, "execution_info", None), "thread_id", None
    )


def _ensure_sync_watcher(task, runtime) -> bool:
    """check_async_task 顺手自愈：state 说任务还在 running，但本进程没有 watcher → 重建。

    为什么需要（2026-10-22 dev-34 事故）：watcher 是**派发进程内的 daemon 线程**
    （见 `sync_subagent_todos.launch_sync`），发版换容器 / 进程重启会把主 run 连同
    它一起杀掉；子 run 却会被新容器当孤儿回收后继续跑（`Background worker expired,
    adding to queue`，实测排队 403s）。于是没人再写 `subagent_steps_map` /
    `async_tasks`，前端卡片永久停在旧快照——子 run 后来 success 也刷不回来
    （事故卡片停在「└ Schema 提取与裁剪 (2s...)」）。

    拉起后交给 watcher 自己的启动自愈（P1-9）：子 run 已结束 → 立刻补写最终 steps
    并清 active_queries；还在跑 → 正常接着盯。所以判据只看「state 说 running」，
    不看 run 侧状态。

    只在 watcher 失联时拉起（`launch_sync` 非幂等）。判据是**本进程**的注册表，
    蓝绿双色并存时可能各起一个——与 `_relaunch_sync_after_redispatch`、
    `api/sql_approval.py`、`api/task_cancel.py` 的既有写法一致。

    Returns: 是否真的拉起了 watcher（供日志与测试断言）。
    """
    if not isinstance(task, dict) or str(task.get("status") or "") != "running":
        return False
    task_id = str(task.get("task_id") or task.get("thread_id") or "")
    if not task_id:
        return False
    try:
        from agent.subagents.sync_subagent_todos import is_sync_alive, launch_sync

        if is_sync_alive(task_id):
            return False
        main_thread_id = _main_thread_id_from_runtime(runtime)
        if not main_thread_id:
            _logger.warning(
                "[check_progress] watcher 已失联但取不到主线程 id，跳过重建: %s",
                task_id[:8],
            )
            return False
        launch_sync(
            main_thread_id,
            task_id,
            str(task.get("agent_name") or "nl2sql"),
            task,
        )
        _logger.info(
            "[check_progress] watcher 失联，check 时重建: sub=%s main=%s run=%s",
            task_id[:8],
            str(main_thread_id)[:8],
            str(task.get("run_id") or "")[:8],
        )
        return True
    except Exception as e:  # noqa: BLE001
        # 自愈失败绝不能让 check 工具调用失败
        _logger.warning(
            "[check_progress] check 时重建 sync watcher 失败: %s",
            f"{type(e).__name__}: {e}",
        )
        return False

def _make_patched_build_check_tool(mod, orig_build_check_tool):
    """_patched_build_check_tool 的工厂。

    参数 = apply_patch 时期的两个闭包捕获：deepagents async_subagents 模块
    与被替换的原函数。显式化之后，本文件里「谁依赖什么」不必再往上翻
    500 行才能看出来。
    """
    def _patched_build_check_tool(clients):
        tool = orig_build_check_tool(clients)

        # ── sync ──
        def _check_sync(
            task_id: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
            since: Optional[int] = None,
            full: Optional[bool] = None,
        ):
            task = mod._resolve_tracked_task(task_id, runtime)
            if isinstance(task, str):
                return task
            client = clients.get_sync(task["agent_name"])
            try:
                run = client.runs.get(
                    thread_id=task["thread_id"], run_id=task["run_id"]
                )
            except Exception as e:
                return f"Failed to get run status: {e}"

            thread_values = {}
            try:
                state = client.threads.get_state(thread_id=task["thread_id"])
                thread_values = state.get("values") or {}
            except Exception:
                try:
                    thread = client.threads.get(thread_id=task["thread_id"])
                    thread_values = thread.get("values") or {}
                except Exception:
                    pass

            result = mod._build_check_result(
                run, task["thread_id"], thread_values, full=bool(full)
            )
            _add_incremental(result, thread_values, since)
            # 与 async_tasks 条目对账：条目已终态则结果不得说「在跑」
            _reconcile_result_with_task(result, task)
            # 发版换容器会杀掉派发进程里的 watcher → 顺手重建（见 _ensure_sync_watcher）
            _ensure_sync_watcher(task, runtime)
            return mod._build_check_command(result, task, runtime.tool_call_id)

        # ── async ──
        async def _check_async(
            task_id: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
            since: Optional[int] = None,
            full: Optional[bool] = None,
        ):
            task = mod._resolve_tracked_task(task_id, runtime)
            if isinstance(task, str):
                return task
            client = clients.get_async(task["agent_name"])
            try:
                run = await client.runs.get(
                    thread_id=task["thread_id"], run_id=task["run_id"]
                )
            except Exception as e:
                return f"Failed to get run status: {e}"

            thread_values = {}
            try:
                state = await client.threads.get_state(
                    thread_id=task["thread_id"]
                )
                thread_values = state.get("values") or {}
                _logger.info(
                    "[check_progress] get_state: keys=%s, msg_count=%d",
                    list(thread_values.keys()),
                    len(thread_values.get("messages") or []),
                )
            except Exception as e:
                _logger.warning("[check_progress] get_state failed: %s", f"{type(e).__name__}: {e}")
                try:
                    thread = await client.threads.get(
                        thread_id=task["thread_id"]
                    )
                    thread_values = thread.get("values") or {}
                except Exception as e2:
                    _logger.warning(
                        "[check_progress] threads.get also failed: %s", e2
                    )

            # P1-14：`_build_check_result` 是**同步**函数，内部串了
            # `plan_run_sql`（进程内复算物理 SQL：建引擎约 0.9s）、结果解析、
            # `_backfill_process_data_sql`（可能回写 thread state）——主 agent 每轮
            # 都可能调 `check_async_task`，直接 await 就是把这段 CPU 挂在共用事件
            # 循环上（§3.3「check_progress 的 CPU 工作」）。
            # 必须用 `offload`（to_thread）而不是 `offload_long`：它内部靠
            # `get_config()` 读本请求的 db_name/thread_id，而 contextvars 只有
            # to_thread 会传播（见 utils/offload 文件头）。
            result = await offload(
                mod._build_check_result, run, task["thread_id"], thread_values,
                full=bool(full),
            )
            _add_incremental(result, thread_values, since)
            # 同上：与 async_tasks 条目对账（终态保护，2026-10-16 事故）
            _reconcile_result_with_task(result, task)
            # 同上：watcher 随派发进程被杀时在这里自愈
            _ensure_sync_watcher(task, runtime)
            return mod._build_check_command(result, task, runtime.tool_call_id)

        tool.func = _check_sync
        tool.coroutine = _check_async
        # P1-7：暴露 since 游标参数 + 说明增量读取用法
        # P4：暴露 full 参数——结果被摘要截断时读原文的正规通道
        tool.args_schema = CheckAsyncTaskSinceSchema
        tool.description = (
            "Check the status of an async subagent task. Returns the current status "
            "and, if complete, the result. Every check also returns a `cursor` "
            "(current message count); pass it back as `since` on a later check to "
            "read only messages added since then (incremental reads). "
            "If the returned result looks truncated (summarized tables / omitted "
            "middle), pass `full=true` to read the complete result text — this is "
            "the correct way to get more data, NOT re-dispatching the subagent: a "
            "finished task's result is fixed, so re-running it produces no new data."
        )
        return tool
    return _patched_build_check_tool

def _make_patched_build_update_tool(orig):
    """_patched_build_update_tool 的工厂。

    参数 = 被替换的原 _build_update_tool（原先以默认参数 `_orig=` 传，提升后
    必须由 apply_patch 显式给出——模块级拿不到 `_mod`）。
    """
    def _patched_build_update_tool(agent_map, clients):
        tool = orig(agent_map, clients)
        orig_func, orig_coro = tool.func, tool.coroutine

        def _w(task_id: str, message: str, runtime: Annotated[ToolRuntime, InjectedToolArg()]):
            out = _reapply_task_descriptions(
                orig_func(task_id, message, runtime), runtime.state
            )
            _relaunch_sync_after_redispatch(task_id, runtime, out)
            return out

        async def _aw(task_id: str, message: str, runtime: Annotated[ToolRuntime, InjectedToolArg()]):
            out = _reapply_task_descriptions(
                await orig_coro(task_id, message, runtime), runtime.state
            )
            _relaunch_sync_after_redispatch(task_id, runtime, out)
            return out

        tool.func, tool.coroutine = _w, _aw
        return tool
    return _patched_build_update_tool

def _make_patched_build_list_tasks_tool(orig):
    """_patched_build_list_tasks_tool 的工厂。

    参数 = 被替换的原 _build_list_tasks_tool。同上行，理由一致。
    """
    def _patched_build_list_tasks_tool(clients):
        tool = orig(clients)
        orig_func, orig_coro = tool.func, tool.coroutine

        def _w(runtime: Annotated[ToolRuntime, InjectedToolArg()], status_filter=None):
            return _reapply_task_descriptions(
                orig_func(runtime, status_filter), runtime.state
            )

        async def _aw(runtime: Annotated[ToolRuntime, InjectedToolArg()], status_filter=None):
            return _reapply_task_descriptions(
                await orig_coro(runtime, status_filter), runtime.state
            )

        tool.func, tool.coroutine = _w, _aw
        return tool
    return _patched_build_list_tasks_tool


def apply_patch():
    """Monkey-patch deepagents async_subagents 模块。幂等。

    Phase 2-2a：原先内嵌在本函数里的 7 个装配件已提升为模块级（见上方那段）。
    本函数只剩「取原函数 → 装替换件」的骨架，一眼能看清补了什么。
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        from deepagents.middleware import async_subagents as _mod
    except ImportError:
        _logger.warning("[check_progress] deepagents not found, skip patch")
        return

    # ── 1. 替换 _build_check_result / _build_check_command：支持 running 中间态 ──
    _mod._build_check_result = _enhanced_build_check_result
    _mod._build_check_command = _enhanced_build_check_command

    # ── 2. 替换 _build_check_tool：running 时也拉取 thread state ──
    # 顺序不可颠倒：先把原函数取出来，再装替换件。
    _mod._build_check_tool = _make_patched_build_check_tool(
        _mod, _mod._build_check_tool
    )

    # ── 3. 替换 _build_update_tool / _build_list_tasks_tool：description 耐久 ──
    # deepagents 的 update_async_task / list_async_tasks 重建 AsyncTask 时丢掉
    # description，且 _tasks_reducer 是整条替换 → 前端任务描述被抹掉（M-T5c）。
    _orig_update = getattr(_mod, "_build_update_tool", None)
    _orig_list = getattr(_mod, "_build_list_tasks_tool", None)
    if _orig_update:
        _mod._build_update_tool = _make_patched_build_update_tool(_orig_update)
    if _orig_list:
        _mod._build_list_tasks_tool = _make_patched_build_list_tasks_tool(_orig_list)

    _logger.info("[check_progress] patched check_async_task for detailed progress")


# ── 格式化工具 ────────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """格式化秒数为可读字符串。"""
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{int(seconds)}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m"


def _compute_step_durations(step_history: list, now: float) -> dict[str, str]:
    """从 step_history 计算每步耗时，返回 {step_name: duration_str}。"""
    durations = {}
    transitions = {}
    for ev in step_history:
        step = ev.get("step", "")
        transitions.setdefault(step, []).append(ev)

    for step, evts in transitions.items():
        started = None
        ended = None
        for ev in evts:
            if ev.get("to") == "in_progress":
                started = ev["ts"]
            elif ev.get("to") == "completed":
                ended = ev["ts"]
        if started and ended:
            durations[step] = _format_duration(ended - started)
        elif started:
            durations[step] = _format_duration(now - started) + "..."
    return durations


# ── 进度提取 ──────────────────────────────────────────────────────────


def _todos_from_state(thread_values: dict | None) -> list:
    """子线程 `state.todos` → `[{"content","status"}]`（**权威进度源**；拿不到返回空）。

    只取 content/status 两个键：下游只认这两个（渲染 + `_extract_timing_from_messages`
    的计时 key 就是 content），多带的键没有读者。非法项（非 dict / content 全空白）
    直接跳过，绝不抛异常。
    """
    if not isinstance(thread_values, dict):
        return []
    out = []
    for item in thread_values.get("todos") or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append({"content": content, "status": str(item.get("status") or "pending")})
    return out


def _extract_progress(result: dict, messages: list, thread_values: dict | None = None):
    """从**子线程 state.todos（权威）**/ thread messages / 本地进度文件提取精简进度。

    ⚠️ 权威源优先（2026-09-28 生产事故）：任务卡（`subagent_steps_map`）读的是
    `state.todos`（由 `ProgressBoundaryMiddleware` 按工具里程碑确定性推进，见
    `sync_subagent_todos._extract_subagent_todos`），而本函数原来只反扫 messages 里的
    `write_todos` ⇒ **两个源会打架**：事故那轮卡片已经停在第 2 步「Schema 提取与裁剪」
    计时 8 分钟，主 agent 却被告知 `0/6 (0%) / current_step=理解建模-清晰度与知识`，
    它据此反复盘问子任务。messages 反扫现在只在拿不到 `state.todos` 时兜底（旧行为不变）。
    """
    todos = _todos_from_state(thread_values)
    latest_ai = ""
    last_tool = ""

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("type")

        if role in ("ai", "assistant"):
            content = msg.get("content", "")
            if content and isinstance(content, str) and content.strip():
                latest_ai = content.strip()
            for tc in msg.get("tool_calls") or []:
                name = tc.get("name", "")
                if name:
                    last_tool = name

        elif role == "tool":
            name = msg.get("name", "")
            content = msg.get("content", "")
            content_str = content if isinstance(content, str) else str(content)
            # `and not todos`：权威源已有进度时，不让过时的 write_todos 回声盖掉它
            if name == "write_todos" and not todos:
                items = re.findall(
                    r"\{'content':\s*'([^']*)',\s*'status':\s*'([^']*)'\}",
                    content_str,
                )
                if items:
                    todos = [{"content": c, "status": s} for c, s in items]

    if not todos:
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if (msg.get("role") or msg.get("type")) not in ("ai", "assistant"):
                continue
            for tc in msg.get("tool_calls") or []:
                if tc.get("name") == "write_todos":
                    args_str = str(tc.get("args", {}))
                    items = re.findall(
                        r"'content':\s*'([^']*)',\s*'status':\s*'([^']*)'",
                        args_str,
                    )
                    if items:
                        todos = [{"content": c, "status": s} for c, s in items]
                        break
            if todos:
                break

    # ── 方案 C: 从本地进度文件读取 step_history ──
    now = _time.time()
    step_durations = {}
    thread_id = result.get("thread_id", "")
    try:
        from agent.subagents.track_progress import read_progress
        local_progress = read_progress(thread_id)
        if local_progress:
            step_history = local_progress.get("step_history", [])
            step_durations = _compute_step_durations(step_history, now)
            started_at = local_progress.get("started_at")
            if started_at:
                result["elapsed"] = _format_duration(now - started_at)
    except Exception as e:
        _logger.debug("[check_progress] read_progress failed: %s", f"{type(e).__name__}: {e}")

    # ── 方案 A fallback: message metadata 时间戳 ──
    if not step_durations:
        _extract_timing_from_messages(
            messages, todos, now, step_durations, result
        )

    # ── 构建精简输出 ──
    if todos:
        completed = sum(1 for t in todos if t["status"] == "completed")
        total = len(todos)
        pct = round(completed / total * 100) if total else 0
        result["progress"] = f"{completed}/{total} ({pct}%)"

        steps = []
        for t in todos:
            dur = step_durations.get(t["content"], "")
            dur_str = f" ({dur})" if dur else ""
            if t["status"] == "completed":
                steps.append(f"✅ {t['content']}{dur_str}")
            elif t["status"] == "in_progress":
                steps.append(f"🔄 {t['content']}{dur_str}")
            else:
                steps.append(f"⬜ {t['content']}")
        result["steps"] = steps

        current = next(
            (t["content"] for t in todos if t["status"] == "in_progress"),
            None,
        )
        if current:
            result["current_step"] = current
    else:
        # 子智能体刚启动，尚未调用 write_todos — 返回默认结构保持格式一致
        result["progress"] = "0/? (初始化中)"
        result["steps"] = ["🔄 初始化..."]
        result["current_step"] = "初始化"

    if last_tool:
        result["current_action"] = last_tool
    if latest_ai:
        first_sentence = re.split(r"[。：\n]", latest_ai)[0]
        result["latest_thinking"] = first_sentence[:100]


def _extract_timing_from_messages(
    messages, todos, now, step_durations, result,
):
    """方案 A fallback: 从 message metadata 时间戳估算耗时。"""
    msg_timestamps = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("type")
        if role not in ("ai", "assistant"):
            continue
        meta = msg.get("response_metadata") or {}
        ts = (
            meta.get("timestamp")
            or meta.get("created_at")
            or (meta.get("model_extra") or {}).get("created")
        )
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    msg_timestamps.append(ts)
                elif isinstance(ts, str):
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    msg_timestamps.append(dt.timestamp())
            except Exception:
                pass

    if len(msg_timestamps) >= 2:
        first_ts = min(msg_timestamps)
        result["elapsed"] = _format_duration(now - first_ts)
        if todos and len(msg_timestamps) > 1:
            sorted_ts = sorted(msg_timestamps)
            total_dur = sorted_ts[-1] - sorted_ts[0]
            per_step = total_dur / len(todos) if todos else 0
            for t in todos:
                if t["status"] == "completed" and per_step > 0:
                    step_durations[t["content"]] = _format_duration(per_step)
                elif t["status"] == "in_progress":
                    elapsed = now - sorted_ts[-1]
                    step_durations[t["content"]] = (
                        _format_duration(elapsed) + "..."
                    )


# 模块加载时自动打补丁
apply_patch()


# ── read_file 全量读取补丁 ────────────────────────────────────────────

_READ_FILE_PATCHED = False
_READ_ALL_LIMIT = 99999  # 足够大的数，等效于"全部读取"


def apply_read_file_patch():
    """把 read_file 默认 limit 从 100 行改为全量读取。幂等。"""
    global _READ_FILE_PATCHED
    if _READ_FILE_PATCHED:
        return
    _READ_FILE_PATCHED = True

    try:
        from deepagents.middleware import filesystem as _fs
    except ImportError:
        _logger.warning("[read_file_patch] filesystem module not found, skip")
        return

    # 1. 修改模块常量
    _fs.DEFAULT_READ_LIMIT = _READ_ALL_LIMIT

    # 2. 用新子类替换 ReadFileSchema（修改 limit 默认值）
    from pydantic import Field

    class ReadFileSchemaFull(_fs.ReadFileSchema):
        """read_file schema — limit 默认全量读取。"""
        limit: int = Field(
            default=_READ_ALL_LIMIT,
            description=(
                "Maximum number of lines to read. "
                f"Defaults to {_READ_ALL_LIMIT} (effectively reads the full file). "
                "Use for pagination of large files."
            ),
        )

    _fs.ReadFileSchema = ReadFileSchemaFull

    # 3. 包装 _create_read_file_tool，把 limit=100 的函数默认值替换掉
    _orig_create = _fs.FilesystemMiddleware._create_read_file_tool

    def _patched_create_read_file_tool(self):
        tool = _orig_create(self)

        _orig_sync = tool.func
        _orig_async = tool.coroutine

        def _wrap_sync(
            file_path: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
            offset: int = 0,
            limit: int = _READ_ALL_LIMIT,
        ):
            return _orig_sync(file_path, runtime, offset=offset, limit=limit)

        async def _wrap_async(
            file_path: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
            offset: int = 0,
            limit: int = _READ_ALL_LIMIT,
        ):
            return await _orig_async(file_path, runtime, offset=offset, limit=limit)

        tool.func = _wrap_sync
        tool.coroutine = _wrap_async
        tool.args_schema = _fs.ReadFileSchema
        return tool

    _fs.FilesystemMiddleware._create_read_file_tool = _patched_create_read_file_tool
    _logger.info("[read_file_patch] read_file default limit → %d", _READ_ALL_LIMIT)


apply_read_file_patch()
