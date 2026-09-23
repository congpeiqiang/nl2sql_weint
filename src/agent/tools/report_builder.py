"""build_report 工具 — P4 报告程序化装配。

背景：nl2sql 子任务完成后，报告生成曾是耗时大头（约 70s）——模型需要在一
次超长 LLM 输出里手工搬运整张数据表、SQL、洞察与 base64 iframe（合计约 5k+
token），且极易在途中漏掉/改写 iframe 导致报告不可交互。

本工具把「数据 + SQL + 洞察 + echarts iframe」从主 agent 自身对话 state
（runtime.state["messages"]）里程序化提取并拼装成 Markdown 报告落盘，模型
只需输出报告标题与解读文本（约几百 token）。数据来源：

- `check_async_task` 最近一次 success 的 ToolMessage：其 content 是 JSON，
  内含格式化的数据表 + 洞察 + SQL（约 1.2k chars）。
- `generate_echarts` 的 ToolMessage（全部，多图按出现顺序去重收集）：
  content 内含可交互 iframe（每张约 3.5k chars，base64 内嵌）。

文件名含时分秒：`{report_name}_{YYYY-MM-DD_HH-mm-ss}.md`（满足「报告生成
包含时分秒」要求），落盘到当前活跃工作区 report/ 目录，返回 VFS 路径。
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

_RE_IFRAME = re.compile(r"<iframe[\s\S]*?</iframe>", re.IGNORECASE)
_SAFE_FNAME = re.compile(r'[\\/:*?"<>|\r\n]+')
# 全量结果表内嵌上限：超过则只给路径链接（防病理性超大表把报告文件撑爆）
_EMBED_MAX_BYTES = 4 * 1024 * 1024

# 数据结果文本内 ```sql 围栏块（子 agent 常在结果里自带产出 SQL，防报告重复成节）
_SQL_FENCE_RE = re.compile(r"```sql[ \t]*\n?([\s\S]*?)```", re.IGNORECASE)


def _result_already_contains_sql(result_text, sql) -> bool:
    """result_text 是否已含与 sql 同一条 SQL（免报告重复生成「执行 SQL」节）。

    ① 任一 ```sql 围栏块归一化（折叠空白）后 == sql —— 子 agent 把产出 SQL 以多行
    围栏写在结果里时，换行/缩进逐字节不等，归一化后视为同一 SQL（trace d60b
    标量双计数）；② 或 sql 以原文出现在 result_text（保留旧语义防原样重复）。
    """
    if not sql:
        return True
    text = str(result_text or "")
    if not text:
        return False
    norm = lambda s: " ".join(str(s).split())
    for m in _SQL_FENCE_RE.finditer(text):
        if norm(m.group(1)) == norm(sql):
            return True
    return sql in text


def _full_table_section(result_obj) -> str:
    """从 check 结果携带的 full_result_files 读盘拼「完整数据表」一节。

    QueryResultOffloadMiddleware 在 run_sql 边界把大结果表（>50 行）确定性落盘，
    check_progress 已把 VFS 指针汇总到 result.full_result_files。这里**代码读盘**
    （0 模型 token）内嵌到报告正文；VFS `/workspace/` 前缀映射到当前活跃工作区
    磁盘目录（与报告落盘目录同源）。文件缺失 / 超大 / 读取失败时降级只写路径
    链接，不报错。
    """
    if not isinstance(result_obj, dict):
        return ""
    files = result_obj.get("full_result_files")
    if not isinstance(files, list) or not files:
        return ""
    try:
        from agent.workspace_manager import get_workspace_manager

        root = Path(get_workspace_manager().active_workspace)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[build_report] active workspace 读取失败: %s", e)
        return ""
    parts = []
    for fp in files:
        if not isinstance(fp, str) or not fp.startswith("/workspace/"):
            parts.append(f"\n- 全量结果文件：`{fp}`（路径不在工作区，无法内嵌）")
            continue
        rel = fp[len("/workspace/"):]
        disk = root / rel
        try:
            if not disk.is_file():
                parts.append(f"\n- 全量结果文件：`{fp}`（文件缺失，无法内嵌）")
                continue
            size = disk.stat().st_size
            if size > _EMBED_MAX_BYTES:
                parts.append(
                    f"\n- 全量结果文件：`{fp}`（{size} 字节过大，未内嵌，可下载查看）"
                )
                continue
            parts.append(disk.read_text(encoding="utf-8").rstrip())
        except Exception as e:  # noqa: BLE001
            _logger.warning("[build_report] 读全量结果文件失败 %s: %s", fp, e)
            parts.append(f"\n- 全量结果文件：`{fp}`（读取失败，可手动打开查看）")
    if not parts:
        return ""
    return "\n\n".join(parts)


def _wren_plan_sql_section(result_obj) -> str:
    """读回「真正下发物理库执行的 SQL」（check_progress 复算后落盘，附 VFS 指针）。

    与 `_full_table_section` 同一套解析范式（VFS `/workspace/` → 活跃工作区磁盘
    目录）。文件读不到 → 退回 check 结果里的内联 `dialect_sql`；都没有 → 返回
    空串，该节整节不出现（**不伪造**，也绝不拿语义层 SQL 冒充可执行语句）。
    """
    if not isinstance(result_obj, dict):
        return ""
    inline = result_obj.get("dialect_sql")
    inline = inline.strip() if isinstance(inline, str) else ""
    fp = result_obj.get("dialect_sql_file")
    if isinstance(fp, str) and fp.startswith("/workspace/"):
        try:
            from agent.workspace_manager import get_workspace_manager

            root = Path(get_workspace_manager().active_workspace)
            disk = root / fp[len("/workspace/"):]
            if disk.is_file():
                return disk.read_text(encoding="utf-8").rstrip()
            _logger.warning("[build_report] 物理 SQL 文件缺失: %s", fp)
        except Exception as e:  # noqa: BLE001  降级用内联
            _logger.warning("[build_report] 读物理 SQL 文件失败 %s: %s", fp, e)
    return inline


# ── Cube「业务口径」两层渲染 ───────────────────────────────────────────
# 报告原来只给「查询定义（Cube 语义层）」的裸 YAML（cube: xxx / measures: [...]），
# 业务用户看不懂。两层解释叠在它之上：
#   Layer A：LLM 把查询翻成 2-3 句业务语言（可选，失败即跳过）
#   Layer B：模板把 measure/dimension 的技术名换成 cube 元数据里的中文描述
# 两层都 fail-open，最坏情况退回到只有 Layer C（原始定义）——与改动前一致。
# 过滤器运算符 → 中文/数学符号（`genre_name:eq:Rock` → `genre_name = Rock`）
_FILTER_OPS = {
    "eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "ge": "≥",
    "lt": "<", "lte": "≤", "le": "≤", "in": "属于",
    "not_in": "不属于", "contains": "包含", "not_contains": "不包含",
}
# 时间粒度 → 中文（cube 的 granularity 或 time_dimension 尾段）
_GRANULARITY_CN = {
    "day": "按日", "week": "按周", "month": "按月",
    "quarter": "按季度", "year": "按年", "hour": "按小时",
}


def _filter_readable(flt, dimensions_meta: dict) -> str:
    """过滤器表达式 → 人读文本；解析不出就原样返回（绝不猜）。"""
    s = str(flt or "").strip()
    if not s:
        return ""
    parts = s.split(":")
    if len(parts) == 3:
        field, op, value = parts
        field_cn = dimensions_meta.get(field) or field
        op_cn = _FILTER_OPS.get(op.lower(), op)
        return f"{field_cn} {op_cn} {value}"
    if len(parts) == 2:
        field, value = parts
        field_cn = dimensions_meta.get(field) or field
        return f"{field_cn} = {value}"
    return s


def _field_list_readable(names, meta: dict) -> str:
    """字段名列表 →「中文描述 · tech_name」顿号串；无描述时只给技术名。

    分隔符用 ``·`` 而**不是括号**：描述本身常带括号（「工时合计（小时）」），
    再套一层括号就成了 ``工时合计（小时）（total_hours）``——两层括注连排，
    可读性差（生产实测反馈）。
    """
    out = []
    for n in names or []:
        name = str(n)
        desc = (meta or {}).get(name) or ""
        out.append(f"{desc} · {name}" if desc else name)
    return "、".join(out)


def _render_cube_layer_b(cube_args: dict, metadata: dict) -> str:
    """Layer B：模板渲染查询结构（带中文描述）。失败/无内容返回空串。

    输出示例::

        数据模型：sales_analytics（音乐商店销售分析：收入、订单、客单价）
        ├─ 维度：账单国家 · billing_country、音乐流派 · genre_name
        ├─ 度量：总收入 · total_revenue、订单数 · invoice_count
        └─ 筛选：音乐流派 = Rock
    """
    if not isinstance(cube_args, dict) or not cube_args:
        return ""
    cube_name = str(cube_args.get("cube") or "")
    if not cube_name:
        return ""
    meta = metadata if isinstance(metadata, dict) else {}
    cube_desc = str(meta.get("cube_description") or "")
    measures_meta = meta.get("measures") or {}
    dimensions_meta = meta.get("dimensions") or {}

    lines = [f"数据模型：{cube_name}" + (f"（{cube_desc}）" if cube_desc else "")]

    dims = cube_args.get("dimensions")
    if isinstance(dims, list) and dims:
        lines.append("├─ 维度：" + _field_list_readable(dims, dimensions_meta))
    measures = cube_args.get("measures")
    if isinstance(measures, list) and measures:
        lines.append("├─ 度量：" + _field_list_readable(measures, measures_meta))
    filters = cube_args.get("filters")
    if isinstance(filters, list) and filters:
        flist = [x for x in (_filter_readable(f, dimensions_meta) for f in filters) if x]
        if flist:  # 全解析成空串时不留一个光秃秃的「筛选：」标签
            lines.append("├─ 筛选：" + "；".join(flist))

    # 时间：granularity 可能独立给，也可能并进 time_dimension（`invoice_date:month`）
    time_dim = str(cube_args.get("time_dimension") or "")
    granularity = str(cube_args.get("granularity") or "")
    td_field, td_gran = time_dim, ""
    if ":" in time_dim:
        td_field, td_gran = time_dim.split(":", 1)
    gran = granularity or td_gran
    if td_field or gran:
        seg = []
        if td_field:
            seg.append(_field_list_readable([td_field], dimensions_meta))
        if gran:
            gran_cn = _GRANULARITY_CN.get(gran.lower(), gran)
            # 有维度时把粒度括注在后面（`work_date（按月）`），只有粒度时直接给
            seg = [seg[0] + f"（{gran_cn}）"] if seg else [gran_cn]
        lines.append("├─ 时间：" + "".join(seg))

    segments = cube_args.get("segments")
    if isinstance(segments, list) and segments:
        lines.append("├─ 分段：" + "、".join(str(s) for s in segments))
    order_by = cube_args.get("order_by")
    if isinstance(order_by, list) and order_by:
        lines.append("├─ 排序：" + "、".join(str(o) for o in order_by))

    # 末行树形符收尾（把最后一个 ├─ 换成 └─），保持树形可读
    if len(lines) > 1:
        lines[-1] = lines[-1].replace("├─ ", "└─ ", 1)
    return "\n".join(lines)


_CUBE_LAYER_A_PROMPT = """请把下面这次数据查询用 2-3 句中文解释给业务人员听。

要求：
- 用业务语言，不要出现表名、字段名、SQL 术语
- 说明：查了什么对象的数据、按什么口径分组、看了哪些指标
- 若有筛选条件，说明数据范围；若有时间维度，说明周期
- 只输出这段解释本身，不要标题、不要前后缀、不要列表符号

查询信息：
{info}"""


async def _render_cube_layer_a(cube_args: dict, metadata: dict) -> str:
    """Layer A：LLM 把 Cube 查询翻成 2-3 句业务语言。任何失败返回空串（跳过该层）。"""
    if not isinstance(cube_args, dict) or not cube_args:
        return ""
    try:
        from agent.llms.model import create_model

        # 用户身份：模型配置已按用户隔离，必须与主 agent 用同一份配置，
        # 否则会落到全局 store（生产上就是那份过期/不同 key 的配置）。
        # 读法与 ThinkingToggleMiddleware._resolve_overrides 一致：request.runtime.config
        # 恒为空，只能走 langgraph.config.get_config()。
        user_id = None
        try:
            from langgraph.config import get_config as _lg_get_config

            user_id = (_lg_get_config().get("configurable", {}) or {}).get("user_id")
            user_id = str(user_id) if user_id else None
        except Exception:  # noqa: BLE001  取不到就退回全局 store
            pass

        # 短摘要用不着思考链：关掉省时省钱（配置缺失时 create_model 返回 None）
        model = create_model(enable_thinking=False, user_id=user_id)
        if model is None:
            _logger.debug("[build_report] Layer A 跳过：无可用模型配置")
            return ""

        meta = metadata if isinstance(metadata, dict) else {}
        cube_name = str(cube_args.get("cube") or "")
        cube_desc = str(meta.get("cube_description") or "")
        measures_meta = meta.get("measures") or {}
        dimensions_meta = meta.get("dimensions") or {}

        info_lines = [f"数据模型：{cube_name}" + (f"（{cube_desc}）" if cube_desc else "")]
        dims = cube_args.get("dimensions")
        if isinstance(dims, list) and dims:
            info_lines.append("分组维度：" + _field_list_readable(dims, dimensions_meta))
        measures = cube_args.get("measures")
        if isinstance(measures, list) and measures:
            info_lines.append("统计指标：" + _field_list_readable(measures, measures_meta))
        filters = cube_args.get("filters")
        if isinstance(filters, list) and filters:
            flist = [_filter_readable(f, dimensions_meta) for f in filters]
            info_lines.append("筛选条件：" + "；".join(x for x in flist if x))
        gran = str(cube_args.get("granularity") or "")
        time_dim = str(cube_args.get("time_dimension") or "")
        if gran or time_dim:
            info_lines.append("时间维度：" + (gran or time_dim))
        if len(info_lines) <= 1:
            # 只有 cube 名，没别的可解释——省一次 LLM 调用
            return ""

        prompt = _CUBE_LAYER_A_PROMPT.format(info="\n".join(info_lines))
        # model.invoke 是同步的：to_thread 避免阻塞事件循环（本函数在 async 工具里跑）
        import asyncio

        resp = await asyncio.to_thread(model.invoke, prompt)
        content = getattr(resp, "content", None)
        if content is None:
            content = str(resp)
        summary = str(content).strip()
        if len(summary) > 500:
            summary = summary[:500].rstrip() + "…"
        return summary
    except Exception as e:  # noqa: BLE001  报告不能因这一层挂掉
        _logger.warning("[build_report] Layer A 生成失败（跳过）: %s", e)
        return ""


# ── 消息归一化（兼容 dict 与 LangChain BaseMessage）───────────────────
def _msg_name(msg) -> str:
    if isinstance(msg, dict):
        return str(msg.get("name") or msg.get("type") or "")
    return str(getattr(msg, "name", "") or "")


def _msg_content(msg) -> str:
    if isinstance(msg, dict):
        c = msg.get("content", "")
    else:
        c = getattr(msg, "content", "")
    if isinstance(c, str):
        return c
    # content 可能是 list[{type:"text", text:...}]
    if isinstance(c, list):
        parts = []
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text":
                parts.append(str(it.get("text", "")))
            else:
                parts.append(str(it))
        return "\n".join(parts)
    return str(c)


def _find_last_check_result(messages, task_id: str):
    """最近一次 status=success 的 check_async_task 结果。

    check_progress._build_check_command 创建的 ToolMessage **不带 name**（只有
    tool_call_id），因此不能按工具名过滤，改为按内容签名识别：JSON 解析后同时
    具备 status / thread_id / result 字段即视为 check_async_task 结果。

    Returns: (result_obj, result_text) 或 None。result_obj 含 status/thread_id/
    result/result_size 等字段；result_text 是格式化 Markdown（表+洞察+SQL）。
    """
    best = None
    for m in messages:
        if _msg_name(m) not in ("", "tool", "check_async_task"):
            # 非工具消息（user/ai）直接跳过；工具消息继续按内容识别
            role = _msg_name(m)
            if role in ("user", "assistant", "ai", "human", "system"):
                continue
        try:
            obj = json.loads(_msg_content(m))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        # 内容签名：check_async_task 结果必然带这三个键
        if not ({"status", "thread_id", "result"} <= obj.keys()):
            continue
        if obj.get("status") != "success":
            continue
        result_text = obj.get("result")
        if not result_text:
            continue
        if task_id and obj.get("thread_id") and obj.get("thread_id") != task_id:
            continue
        best = (obj, result_text)
    return best


def _find_all_iframes(messages) -> list[str]:
    """全部 generate_echarts 返回的完整 iframe 标签（原样保留，按出现顺序去重）。

    2026-09-08 修复：此前只取最后一张（_find_last_iframe），会话生成两张图时
    报告附录只嵌第 2 张（thread 01a07fd4 堆叠条形图+热力图只剩热力图）。
    去重防同一 iframe 被模型在最终回复里复述时重复嵌入。
    """
    seen: set[str] = set()
    out: list[str] = []
    for msg in messages:
        for m in _RE_IFRAME.finditer(_msg_content(msg)):
            tag = m.group(0)
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


def _current_turn_start(messages) -> int:
    """当前问题轮次的起点：最后一条真实用户消息的下标。

    跳过 [系统自动通知] 注入（子任务超时/完成续跑的系统消息，非用户新问题）——
    超时接管流程里图表生成在通知之后，锚定真实提问才能把它们收进来。
    找不到则回退 0（整个历史，兼容无 human 消息的合成场景）。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict):
            role = m.get("type") or m.get("role")
            text = _msg_content(m)
        else:
            role = getattr(m, "type", "")
            text = _msg_content(m)
        if role in ("human", "user") and not text.lstrip().startswith("[系统自动通知]"):
            return i
    return 0


def _turn_chart_iframes(messages) -> list[str]:
    """当前问题轮次 generate_echarts 产出的全部图表 iframe（按序去重）。

    作用域双重收窄（2026-09-08，用户要求报告只保存当前问题的图）：
    1. 轮次：只扫最后一条真实用户消息之后的片段 → 历史问题的图不进本报告；
    2. 消息角色：只认 tool 结果（图表必然以 generate_echarts 的 ToolMessage
       到达）；AI 文本里的 iframe 复述一律不计——既天然去重本轮复述，也排除
       模型在答复里引用历史轮次图表的边界情况。
    """
    turn = messages[_current_turn_start(messages):]
    tool_msgs = []
    for m in turn:
        if isinstance(m, dict):
            role = m.get("type") or m.get("role")
        else:
            role = getattr(m, "type", "")
        if role in ("tool", "tool_result"):
            tool_msgs.append(m)
    return _find_all_iframes(tool_msgs)


class BuildReportSchema(BaseModel):
    """build_report 输入。"""

    report_name: str = Field(
        description="报告标题 / 文件名（不含扩展名与时间戳），如「各类型电影数量分布」。"
    )
    analysis: str = Field(
        description="对查询结果的分析解读，Markdown 文本（可用 **加粗**、- 列表、### 小节等）。"
    )
    task_id: str = Field(
        default="",
        description="（可选）对应查询的任务 id；不传则自动取最近一次成功的查询结果。",
    )


async def _build_report_coro(
    report_name: str,
    analysis: str,
    task_id: str,
    runtime: Annotated[ToolRuntime, InjectedToolArg()],
) -> str:
    try:
        state = runtime.state or {}
        messages = state.get("messages") or []
    except Exception as e:  # noqa: BLE001  state 不可读时给出明确错误
        _logger.warning("[build_report] runtime.state 读取失败: %s", e)
        return "build_report 失败：无法读取当前对话状态。请稍后重试。"

    hit = _find_last_check_result(messages, task_id)
    if not hit:
        return (
            "build_report 失败：未找到可用的查询结果。"
            "请先用 check_async_task 确认子任务已成功完成（status=success）。"
        )
    _obj, result_text = hit
    # 语义层 SQL：check_async_task 已把子线程最后一次 run_sql 附在 result.sql。
    # **注意它是模型写的形态**（引用 MDL 视图如 v_workhour），物理库里跑不了 →
    # 真正可执行的语句另见下面的 plan_sql。
    sql = _obj.get("sql", "") if isinstance(_obj, dict) else ""
    sql = sql.strip() if isinstance(sql, str) else ""
    # Cube 快速通道（wrenai_*_query_cube）不产生 run_sql → check 结果里没有 sql，
    # 只有 cube_query（查询定义）。没有它时本节会整节消失（同日同题实测：run_sql
    # 通道报告 16087 字含 SQL 节，Cube 通道 8450 字零 SQL 字样）。
    cube_query = _obj.get("cube_query", "") if isinstance(_obj, dict) else ""
    cube_query = cube_query.strip() if isinstance(cube_query, str) else ""

    # 只收「当前问题」轮次的图表（_turn_chart_iframes：轮次锚定 + 仅 tool 结果），
    # 历史问题生成的 iframe 不进本报告（用户明确要求：报告只保存当前 trace 的图）
    iframes = _turn_chart_iframes(messages)

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_name = _SAFE_FNAME.sub("_", report_name).strip(" ._") or "report"
    fname = f"{safe_name}_{now}.md"

    md_parts = [
        f"# {report_name}\n",
        f"> 生成时间：{now}",
        f"> 数据来源：NL2SQL 查询结果",
        f"> 数据截至：{now}（活库数据，可能随时间变动）\n",
        "## 1. 数据结果\n",
        str(result_text),
    ]
    next_section = 2
    # 大结果全量表内嵌（QueryResultOffload 落盘文件，代码读盘 0 token；无则跳过）
    full_table = _full_table_section(_obj)
    if full_table:
        md_parts += [f"\n## {next_section}. 完整数据表\n", full_table]
        next_section += 1
    # 模型最终回复若已含同一条 SQL（原文或围栏块归一化相等）则不重复成节。
    # 注记（sql_note：wren 通道方言提示 / 明细口径注）在跳节时也照样渲染——
    # 否则内嵌 SQL 的 wren/PG 方言没有任何警示，用户直连 MySQL 跑必炸
    # （2026-09-08 INTERVAL '1 year' 实例）。
    # 真正下发目标库执行的语句（check_progress 复算 + 落盘，见 agent/utils/wren_plan）。
    # 上面那条 sql / cube_query 是**语义层**形态（引用 MDL 视图），在物理库里跑不了。
    plan_sql = _wren_plan_sql_section(_obj)
    plan_note = _obj.get("physical_sql_note") if isinstance(_obj, dict) else ""
    if sql:
        _sql_note = _obj.get("sql_note") if isinstance(_obj, dict) else ""
        if _result_already_contains_sql(str(result_text), sql):
            _logger.info("[build_report] 数据结果已含同 SQL（原文/围栏归一化），跳过追加「执行 SQL」节")
            if _sql_note:
                md_parts += ["", f"> {_sql_note}"]
        else:
            md_parts += [f"\n## {next_section}. 执行 SQL\n", f"```sql\n{sql}\n```"]
            if _sql_note:
                md_parts += ["", f"> {_sql_note}"]
            next_section += 1
        # wren 语义层通道（wrenai_*）：语义层 SQL 之上再给物理 SQL（直连通道
        # dbmcp_* 的 SQL 本身就是目标库方言，没有这一节）
        if plan_sql:
            md_parts += [
                f"\n## {next_section}. 执行 SQL（物理，实际下发）\n",
                f"```sql\n{plan_sql}\n```",
            ]
            if plan_note:
                md_parts += ["", f"> {plan_note}"]
            next_section += 1
    elif cube_query:
        # Cube 通道：物理 SQL 在前（用户要的是可粘贴执行的那条），语义层查询定义
        # 在后作为「来源」注解。取不到物理 SQL 时只出定义节，标题**不叫「执行 SQL」**
        # ——用「执行 SQL」会暗示一条并不存在的 SQL。
        if plan_sql:
            md_parts += [
                f"\n## {next_section}. 执行 SQL（由 Cube 语义层编译，实际下发）\n",
                f"```sql\n{plan_sql}\n```",
            ]
            if plan_note:
                md_parts += ["", f"> {plan_note}"]
            next_section += 1
        _cube_note = _obj.get("sql_note") if isinstance(_obj, dict) else ""

        # ── Cube「业务口径」两层：先取 cube 元数据里的中文描述 ──
        # 原始定义（cube_args）+ 工具名由 check_progress 透传；工具名在此重新解析
        # 成项目路径（与 check_progress 同一个 resolve_wren_ctx，失败即降级）。
        _cube_args = _obj.get("cube_args") if isinstance(_obj, dict) else None
        _cube_tool = _obj.get("cube_tool") if isinstance(_obj, dict) else ""
        _cube_meta: dict = {}
        if isinstance(_cube_args, dict) and _cube_args and _cube_tool:
            try:
                from agent.utils.wren_call_extract import (
                    load_cube_metadata,
                    resolve_wren_ctx,
                )

                _project, _ = resolve_wren_ctx(str(_cube_tool))
                _cube_meta = load_cube_metadata(
                    _project, str(_cube_args.get("cube") or "")
                )
            except Exception as e:  # noqa: BLE001  拿不到描述就降级展示技术名
                _logger.warning("[build_report] Cube 元数据加载失败: %s", e)

        # Layer A：LLM 业务口径摘要 / Layer B：模板结构（两层各自 fail-open，
        # 都返回空串时本节整体跳过，直接进下面的原始定义节）
        if isinstance(_cube_args, dict) and _cube_args:
            _layer_a = await _render_cube_layer_a(_cube_args, _cube_meta)
            if _layer_a:
                md_parts += [f"\n## {next_section}. 业务口径\n", _layer_a]
                next_section += 1
            _layer_b = _render_cube_layer_b(_cube_args, _cube_meta)
            if _layer_b:
                # **必须包代码围栏**：Markdown 里段落内的单个换行会被渲染成空格，
                # 树形文本（├─/└─）会塌成一行（生产实测反馈）。与 Layer C 同款式。
                md_parts += [
                    f"\n## {next_section}. 查询结构\n",
                    f"```\n{_layer_b}\n```",
                ]
                next_section += 1

        # Layer C：原始定义（**始终保留**，两层全挂时报告与改动前一致）
        md_parts += [
            f"\n## {next_section}. 查询定义（Cube 语义层）\n",
            f"```yaml\n{cube_query}\n```",
        ]
        if _cube_note:
            md_parts += ["", f"> {_cube_note}"]
        next_section += 1
    md_parts += [f"\n## {next_section}. 分析解读\n", str(analysis).strip()]
    next_section += 1
    if iframes:
        md_parts += [f"\n## {next_section}. 附录：交互式图表\n"]
        for _i, _ifr in enumerate(iframes, 1):
            if len(iframes) > 1:
                # 多图各加小节标题（前端 MarkdownContent 按 iframe 切分原位渲染，
                # 小节标题不影响切分）；单图保持旧版排版不加标题
                md_parts += [f"### 图表 {_i}", _ifr, ""]
            else:
                md_parts += [_ifr]
        md_parts += ["\n> 💡 可交互图表：鼠标悬停查看数值、可缩放。"]
    else:
        md_parts.append(f"\n## {next_section}. 附录\n\n（本次任务未生成交互式图表）")

    md = "\n".join(md_parts)

    # ── 落盘：活跃工作区 report/ 目录 ──
    try:
        from agent.workspace_manager import get_workspace_manager

        report_dir = get_workspace_manager().active_workspace / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        dest = report_dir / fname
        dest.write_text(md, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[build_report] 写盘失败: %s", e)
        return (
            "build_report 部分失败：报告已拼装但写入文件系统出错"
            f"（{type(e).__name__}: {e}）。可改用 write_file 手动落盘。"
        )

    vfs_path = f"/workspace/report/{fname}"
    _logger.info("[build_report] 报告已生成: %s (%d chars)", vfs_path, len(md))
    return (
        f"报告已生成：{vfs_path}\n"
        f"- 标题：{report_name}\n"
        f"- 生成时间：{now}\n"
        f"- 内容：数据结果、分析解读" + (f"、内嵌交互式图表×{len(iframes)}" if iframes else "") + "\n"
        "请在最终回复中告知用户报告文件路径。"
    )


build_report_tool = StructuredTool.from_function(
    coroutine=_build_report_coro,
    name="build_report",
    description=(
        "把已完成的查询结果程序化装配为 Markdown 报告并写入工作区 report/ 目录。"
        "自动提取最近一次 check_async_task 成功的数据结果（数据表+SQL 或 Cube 查询定义+洞察，"
        "wren 语义层通道另附「真正下发物理库执行」的 SQL，可直接粘贴执行）与"
        " generate_echarts 生成的交互式图表（内嵌 iframe，可交互渲染）。"
        "调用前请确保已用 check_async_task 确认子任务完成、并用 generate_echarts 渲染图表。"
        "报告文件名自动包含精确到时分秒的时间戳，无需再用 shell 取时间。"
    ),
    args_schema=BuildReportSchema,
)
