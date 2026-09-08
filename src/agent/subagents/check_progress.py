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
# 子 agent 用"（续）"把大表拆多段时的延续标记（不计为数据行/正文）
_CONTINUE_MARK = re.compile(r"^\s*[（(]?续[）)]?\s*$")


def _trim_block(text: str, budget: int) -> str:
    """截断一段非表格文本：保留头尾、中间省略，预算内尽量完整保留关键结论。"""
    if len(text) <= budget:
        return text
    half = budget // 2
    return text[:half].rstrip() + f"\n…({len(text)} 字符，中间省略)…\n" + text[-half:].lstrip()


def _summarize_result(content: str) -> str:
    """智能摘要子 agent 返回的结果内容，保留结构信息、截断数据行。

    确保 LLM 能判断是否需要图表，同时避免 34k+ tokens 的 prompt 膨胀。

    修复（2026-09-01，trace 8ccef016「78 个部门」幻觉）：
      - 不再丢弃表格上方的关键结论（如"共 152 个名称 / 431 条记录"）——
        旧实现只拼表头+前 20 行，主 agent 看不到结论，把摘要行数误当业务统计；
      - 行数按真实数据行统计：子 agent 用"（续）"把大表拆成多段时，第二段的
        表头/分隔行曾被误计为数据行（76 行数成 78）；
      - 摘要 marker 明确"行数≠业务统计口径"。
    """
    if len(content) <= _MAX_RESULT_CHARS:
        return content

    # ── 1. 解析 Markdown 表格（支持"（续）"拆分的多段表） ──
    lines = content.split("\n")
    pre_lines, post_lines, table_runs = [], [], []
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
            if not l.strip() or _CONTINUE_MARK.match(l.strip()):
                continue  # 空行 / "（续）"标记不计入正文
            if not table_runs:
                pre_lines.append(l)
            else:
                post_lines.append(l)
    if cur is not None:
        table_runs.append(cur)

    if table_runs:
        total_data = sum(len(r["data"]) for r in table_runs)
        kept = []
        for r in table_runs:
            for d in r["data"]:
                if len(kept) >= _MAX_RESULT_ROWS:
                    break
                kept.append(d)
            if len(kept) >= _MAX_RESULT_ROWS:
                break
        first = table_runs[0]
        suffix = (
            f"\n\n*(数据表共 {total_data} 行，以上展示前 {len(kept)} 行；"
            "行数仅为展示表格行数，业务统计口径以结果文字为准；"
            "完整数据可通过 check_async_task 增量读取获取)*"
        )
        table_part = first["header"] + "\n" + first["sep"] + "\n" + "\n".join(kept) + suffix
        parts = []
        pre_text = "\n".join(pre_lines).strip()
        if pre_text:
            parts.append(_trim_block(pre_text, _MAX_RESULT_CHARS // 2))
        parts.append(table_part)
        post_text = "\n".join(post_lines).strip()
        if post_text:
            parts.append(_trim_block(post_text, _MAX_RESULT_CHARS // 2))
        return "\n\n".join(parts)

    # ── 2. 检测 JSON 数组结果 ──
    try:
        data = json.loads(content)
        if isinstance(data, list) and len(data) > 0:
            kept = data[:_MAX_RESULT_ROWS]
            summary = json.dumps(kept, ensure_ascii=False, indent=2)
            summary += (
                f"\n\n*(共 {len(data)} 条记录，以上展示前 {len(kept)} 条；"
                f"完整结果可通过 check_async_task 增量读取获取)*"
            )
            return summary
    except (json.JSONDecodeError, ValueError):
        pass

    # ── 3. 普通文本：保留头尾 ──
    head = content[:_MAX_RESULT_CHARS // 2]
    tail = content[-(_MAX_RESULT_CHARS // 2):]
    return f"{head}\n\n...({len(content)} 字符，中间已省略；完整结果可通过 check_async_task 增量读取获取)...\n\n{tail}"


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
    """收集子线程里 run_sql 大结果落盘文件的 VFS 指针（保序去重）。

    QueryResultOffloadMiddleware 把 >50 行 / 大文本的 run_sql 结果瘦身为
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
        if not (isinstance(name, str) and name.endswith("run_sql")):
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
            }
        )

    if not sqls:
        return ""

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


# ── sql-generation process_data 回填：SQL 与产出最终结果表的 run_sql 对齐 ────
# process_data/sql-generation/*.json 在 dry_run 工具调用边界落盘，当时只能记录
# 干跑版本 A；若子 agent 执行时改为版本 B 产出最终结果表，前端展示的 SQL（check
# 结果 sql 字段，已取产出 run_sql）会与 process_data 不一致。子任务成功后按
# `_extract_last_sql` 确定的产出 SQL 回填，保留原干跑 SQL 便于排查。
_BACKFILL_SKILL = "sql-generation"


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
    """读当前库名（主 agent configurable.db_name，S3-2 口径护栏用）。"""
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            return str((cfg.get("configurable") or {}).get("db_name", "") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


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
    """把 sql-generation 的 process_data 里的 SQL 回填为产出最终结果表的 run_sql。

    范围限定：只改本子任务干跑过的文件（按子线程消息里的 dry_run 工具调用 SQL
    精确匹配），不影响同会话里其它查询任务的中间产物。幂等：与原值一致（含纯
    空白差异）则跳过。返回实际改写文件数。
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
    skill_dir = Path(root) / "nl2sql_process_data" / sid / _BACKFILL_SKILL
    if not skill_dir.exists():
        return 0
    updated = 0
    for f in sorted(skill_dir.glob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        inp = obj.get("input") if isinstance(obj, dict) else None
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
            "[check_progress] sql-generation process_data 回填 %d 个文件 → 产出 run_sql",
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
    def _enhanced_build_check_result(run, thread_id, thread_values):
        result = {"status": run["status"], "thread_id": thread_id}
        messages = (
            thread_values.get("messages", [])
            if isinstance(thread_values, dict)
            else []
        )
        if run["status"] == "success":
            if messages:
                last = messages[-1]
                raw_content = (
                    last.get("content", "") if isinstance(last, dict) else str(last)
                )
                summarized = _summarize_result(raw_content)
                result["result"] = summarized
                result["result_size"] = {
                    "chars": len(raw_content),
                    "summarized": len(raw_content) > _MAX_RESULT_CHARS,
                }
            else:
                result["result"] = "(completed with no output messages)"
            # 报告装配需要真实执行 SQL：从子线程消息提取最后一次 run_sql 附到 result.sql
            sql = _extract_last_sql(messages)
            if sql:
                result["sql"] = sql
                # 通道感知 SQL 注记（_build_sql_note）：wren 语义层通道的 SQL 是
                # wren/PG 方言（引擎编译为目标库执行），「可直接运行」不成立 →
                # 附方言转换提示；直连通道多行明细仍附旧版口径注（别拿明细去对
                # 186/174 之类汇总数）。
                _note = _build_sql_note(messages, sql)
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
            # 大结果全量文件指针（QueryResultOffload 落盘）附到 result，
            # build_report 读盘后把完整结果表嵌入报告正文（0 模型开销）
            full_files = _collect_full_result_files(messages)
            if full_files:
                result["full_result_files"] = full_files
        elif run["status"] == "error":
            error_detail = run.get("error")
            result["error"] = (
                str(error_detail) if error_detail
                else "The async subagent encountered an error."
            )
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
        # running / 其他中间态：提取进度
        if messages:
            _extract_progress(result, messages)
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
                run, task["thread_id"], thread_values
            )
            _add_incremental(result, thread_values, since)
            return _mod._build_check_command(result, task, runtime.tool_call_id)

        # ── async ──
        async def _check_async(
            task_id: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
            since: Optional[int] = None,
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

            result = _mod._build_check_result(
                run, task["thread_id"], thread_values
            )
            _add_incremental(result, thread_values, since)
            return _mod._build_check_command(result, task, runtime.tool_call_id)

        tool.func = _check_sync
        tool.coroutine = _check_async
        # P1-7：暴露 since 游标参数 + 说明增量读取用法
        tool.args_schema = CheckAsyncTaskSinceSchema
        tool.description = (
            "Check the status of an async subagent task. Returns the current status "
            "and, if complete, the result. Every check also returns a `cursor` "
            "(current message count); pass it back as `since` on a later check to "
            "read only messages added since then (incremental reads)."
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

    _orig_update = getattr(_mod, "_build_update_tool", None)
    _orig_list = getattr(_mod, "_build_list_tasks_tool", None)

    if _orig_update:

        def _patched_build_update_tool(agent_map, clients, _orig=_orig_update):
            tool = _orig(agent_map, clients)
            orig_func, orig_coro = tool.func, tool.coroutine

            def _w(task_id: str, message: str, runtime: Annotated[ToolRuntime, InjectedToolArg()]):
                return _reapply_task_descriptions(
                    orig_func(task_id, message, runtime), runtime.state
                )

            async def _aw(task_id: str, message: str, runtime: Annotated[ToolRuntime, InjectedToolArg()]):
                return _reapply_task_descriptions(
                    await orig_coro(task_id, message, runtime), runtime.state
                )

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


def _extract_progress(result: dict, messages: list):
    """从 thread messages 和本地进度文件提取精简进度信息。"""
    todos = []
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
            if name == "write_todos":
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
