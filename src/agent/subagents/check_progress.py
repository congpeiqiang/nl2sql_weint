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


def _run_sql_meta(messages, i):
    """解析第 i 条 run_sql 工具结果消息：返回 (sql, 返回行数, 列名集合)。

    通过 tool_call_id 与之前 AI 消息的 tool_calls[].id 精确对齐，避免同一 AI
    消息里连续多个 run_sql 时结果错配。
    """
    m = messages[i]
    rows, cols = 0, []
    try:
        obj = json.loads(_msg_content_str(m))
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
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
    """解析第 i 条 run_sql 工具结果 JSON 的 rows[] 单元值集合（去空白）。

    大结果被 QueryResultOffload 瘦身时 rows 只留前 N 样例，仍用样例做值匹配；
    错误文本（非 JSON）返回空集。
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


def _final_row_covered_by(cells, vals) -> bool:
    """一行最终表数据行是否被候选 run_sql 返回值集合“整行覆盖”。

    行内每个非空单元格都须被候选某值命中：归一化+小写后，单元格含该值 或 该值含
    单元格（兼容 run_sql 返回时间戳 2026-08-23 00:00:00 而答案只写 2026-08-23）。
    仅对 len>=2 的值做子串命中，防纯短数字（如 “1”）跨行大面积误命中；单元格两端
    先剥 ** 加粗。
    """
    for c in cells:
        cc = c.strip().strip("*").strip().lower()
        if not cc:
            continue
        covered = False
        for v in vals:
            if not v or not v.strip():
                continue
            vv = v.strip().lower()
            if not vv:
                continue
            # 纯短数字（<3 位）只精确相等命中、禁子串：防分布类计数/日期的 “1”“12”
            # 子串误伤；单字中文（姓）不受此限——须参与整行覆盖才能选中（中文 len=1）
            if vv.isdigit() and len(vv) < 3 and vv != cc:
                continue
            if vv in cc or cc in vv:
                covered = True
                break
        if not covered:
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
        _logger.debug("[check_progress] Cube 通道探测失败: %s", e)
    return False


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
    sqls = []  # [{sql, rows, cols, values}]
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

    # ── tier 0：值匹配（最高优先）────────────────────────────────
    final_rows = _final_answer_data_rows(messages)
    if final_rows:
        best_sql, best_hit, best_aligned = None, 0, False
        for c in sqls:  # 消息序正序，同质量时后者优先
            if not c["values"]:
                continue
            hit = sum(
                1 for cells in final_rows if _final_row_covered_by(cells, c["values"])
            )
            if hit <= 0:
                continue
            aligned = c["rows"] > 0 and c["rows"] == hit
            if hit > best_hit or (hit == best_hit and aligned >= best_aligned):
                best_sql, best_hit, best_aligned = c["sql"], hit, aligned
        if best_sql:
            return best_sql

    # ── tier 0b：标量摘要对齐（最终表=指标|数值 摘要表）──────────
    # tier0 整行匹配要求每格（含中文指标名）都被候选值命中，数据型返回值不含中文
    # 指标名 → 恒落空；掉到「行数最多」会把更晚的 Top-N 探值（5 行）误选成产出
    # 单行标量（trace d60b 部门数/人数 431/1736：LIMIT 5 明细被附进报告）。
    # 标量产出 SQL 返回单元格值集 == 最终全部数值 → 数值全覆盖者即产出者。
    if final_rows:
        final_nums = _final_numeric_values(messages)
        if final_nums:
            full_cover = []
            for _idx, c in enumerate(sqls):
                if not c["values"] or c["rows"] <= 0:
                    continue
                cnums = {
                    float(v.strip().replace(",", "").rstrip("%").strip())
                    for v in c["values"] if _is_plain_number(v)
                }
                if final_nums <= cnums:
                    full_cover.append((c, _idx))
            if full_cover:
                # 全覆盖者：行数少（单行标量）优先，其次消息序靠后
                c, _ = min(full_cover, key=lambda t: (t[0]["rows"], -t[1]))
                return c["sql"]

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
    "以上取的是**最近一次成功**的 Cube 调用：其后的 {n} 次 Cube 调用未成功"
    "（引擎没编译出语句、也没产出数据）。"
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
            _logger.debug("[check_progress] 物理 SQL 落盘异常: %s", e)

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
        _logger.debug("[check_progress] 物理 SQL 附加失败: %s", e)
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
        _logger.debug("[check_progress] 会话账本兜底失败: %s", e)
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
        _logger.debug("[check_progress] 读问题身份失败: %s", e)
    if qid and uq:
        return qid, uq
    if thread_id:
        try:
            from agent.trace.trace_bind_store import get_store

            hit = get_store().get_task(str(thread_id))
        except Exception as e:  # noqa: BLE001
            _logger.debug("[check_progress] 查 trace_bind 子任务绑定失败: %s", e)
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
    except Exception:  # noqa: BLE001
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


def apply_patch():
    """Monkey-patch deepagents async_subagents 模块。幂等。"""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        from deepagents.middleware import async_subagents as _mod
    except ImportError:
        _logger.warning("[check_progress] deepagents not found, skip patch")
        return

    # ── 1. 替换 _build_check_result：支持 running 中间态 ──────────────
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
                _logger.debug("[check_progress] SQL 来源判定失败: %s", e)
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
                _cube = _extract_last_cube_call(messages)
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
                    result["sql_note"] = _note
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
                _logger.debug("[check_progress] 写中间产物审计失败: %s", e)
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
                _logger.debug("[check_progress] 写失败审计件失败: %s", e)
        elif run["status"] == "interrupted":
            # 审批闸门已移除（2026-08-28 起 sql_approval 只读硬拦截、不再 raise
            # interrupt），当前 interrupted 是 deepagents 上下文压缩等「会自恢复的
            # 瞬时暂停」。归一为 running，让主 agent 继续轮询 check_async_task，
            # 而非误判「等待 SQL 审批」。
            result["status"] = "running"
            result["note"] = (
                "The subagent is briefly paused (e.g. context summarization) and "
                "will resume automatically. Do NOT cancel or re-delegate; check "
                "again later."
            )
        # running / 其他中间态：提取进度（thread_values 一并传入 → 权威源 state.todos 优先）
        if messages or (isinstance(thread_values, dict) and thread_values.get("todos")):
            _extract_progress(result, messages, thread_values)
        return result

    _mod._build_check_result = _enhanced_build_check_result

    # ── 1b. 替换 _build_check_command：json.dumps 加 ensure_ascii=False ──
    def _enhanced_build_check_command(result, task, tool_call_id):
        from langgraph.types import Command
        from langchain_core.messages import ToolMessage

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        # 失败被再汇报一次）。
        for _k in ("error", "error_kind", "failure_reported"):
            if task.get(_k) is not None and _k not in updated_task:
                updated_task[_k] = task[_k]
        return Command(
            update={
                "messages": [ToolMessage(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    tool_call_id=tool_call_id,
                )],
                "async_tasks": {task["task_id"]: updated_task},
            }
        )

    _mod._build_check_command = _enhanced_build_check_command

    # ── 2. 替换 _build_check_tool：running 时也拉取 thread state ──────
    _orig_build_check_tool = _mod._build_check_tool

    def _patched_build_check_tool(clients):
        tool = _orig_build_check_tool(clients)

        # ── sync ──
        def _check_sync(
            task_id: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
            since: Optional[int] = None,
            full: Optional[bool] = None,
        ):
            task = _mod._resolve_tracked_task(task_id, runtime)
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

            result = _mod._build_check_result(
                run, task["thread_id"], thread_values, full=bool(full)
            )
            _add_incremental(result, thread_values, since)
            return _mod._build_check_command(result, task, runtime.tool_call_id)

        # ── async ──
        async def _check_async(
            task_id: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
            since: Optional[int] = None,
            full: Optional[bool] = None,
        ):
            task = _mod._resolve_tracked_task(task_id, runtime)
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
                _logger.warning("[check_progress] get_state failed: %s", e)
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
                _mod._build_check_result, run, task["thread_id"], thread_values,
                full=bool(full),
            )
            _add_incremental(result, thread_values, since)
            return _mod._build_check_command(result, task, runtime.tool_call_id)

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

    _mod._build_check_tool = _patched_build_check_tool

    # ── 3. 替换 _build_update_tool / _build_list_tasks_tool：description 耐久 ──
    # deepagents 的 update_async_task / list_async_tasks 重建 AsyncTask 时丢掉
    # description，且 _tasks_reducer 是整条替换 → 前端任务描述会被抹掉（M-T5c）。
    # 这里在返回前把旧 state 里的 description 补回（check_async_task 已单独修）。
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
            # runtime.state 是图状态通道（不含 thread_id），主线程 id 只能从 config 取
            cfg = (getattr(runtime, "config", None) or {}).get("configurable") or {}
            main_thread_id = cfg.get("thread_id") or getattr(
                getattr(runtime, "execution_info", None), "thread_id", None
            )
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
            _logger.warning("[check_progress] 重建 sync watcher 失败: %s", e)

    _orig_update = getattr(_mod, "_build_update_tool", None)
    _orig_list = getattr(_mod, "_build_list_tasks_tool", None)

    if _orig_update:

        def _patched_build_update_tool(agent_map, clients, _orig=_orig_update):
            tool = _orig(agent_map, clients)
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

        _mod._build_update_tool = _patched_build_update_tool

    if _orig_list:

        def _patched_build_list_tasks_tool(clients, _orig=_orig_list):
            tool = _orig(clients)
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

        _mod._build_list_tasks_tool = _patched_build_list_tasks_tool

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
        _logger.debug("[check_progress] read_progress failed: %s", e)

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
