"""build_report 工具 — P4 报告程序化装配。

背景：nl2sql 子任务完成后，报告生成曾是耗时大头（约 70s）——模型需要在一
次超长 LLM 输出里手工搬运整张数据表、SQL、洞察与 base64 iframe（合计约 5k+
token），且极易在途中漏掉/改写 iframe 导致报告不可交互。

本工具把「数据 + SQL + 洞察 + echarts iframe」从主 agent 自身对话 state
（runtime.state["messages"]）里程序化提取并拼装成 Markdown 报告落盘，模型
只需输出报告标题与解读文本（约几百 token）。数据来源：

- `check_async_task` 最近一次 success 的 ToolMessage：其 content 是 JSON，
  内含格式化的数据表 + 洞察 + SQL（约 1.2k chars）。
- `generate_echarts` 最近一次的 ToolMessage：content 内含可交互 iframe
  （约 3.5k chars，base64 内嵌）。

文件名含时分秒：`{report_name}_{YYYY-MM-DD_HH-mm-ss}.md`（满足「报告生成
包含时分秒」要求），落盘到当前活跃工作区 report/ 目录，返回 VFS 路径。
"""
import json
import logging
import re
from datetime import datetime
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

_RE_IFRAME = re.compile(r"<iframe[\s\S]*?</iframe>", re.IGNORECASE)
_SAFE_FNAME = re.compile(r'[\\/:*?"<>|\r\n]+')


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


def _find_last_iframe(messages) -> str:
    """最近一个 generate_echarts 返回的完整 iframe 标签（原样保留）。"""
    best = ""
    for m in messages:
        m = _RE_IFRAME.search(_msg_content(m))
        if m:
            best = m.group(0)
    return best


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
    # 真实执行 SQL：check_async_task 已把子线程最后一次 run_sql 附在 result.sql
    sql = _obj.get("sql", "") if isinstance(_obj, dict) else ""
    sql = sql.strip() if isinstance(sql, str) else ""

    iframe = _find_last_iframe(messages)

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_name = _SAFE_FNAME.sub("_", report_name).strip(" ._") or "report"
    fname = f"{safe_name}_{now}.md"

    md_parts = [
        f"# {report_name}\n",
        f"> 生成时间：{now}",
        f"> 数据来源：NL2SQL 查询结果\n",
        "## 1. 数据结果\n",
        str(result_text),
    ]
    next_section = 2
    # 模型最终回复若已含该 SQL（result_text 命中）则不重复成节
    if sql and sql not in str(result_text):
        md_parts += [f"\n## {next_section}. 执行 SQL\n", f"```sql\n{sql}\n```"]
        next_section += 1
    md_parts += [f"\n## {next_section}. 分析解读\n", str(analysis).strip()]
    next_section += 1
    if iframe:
        md_parts += [
            f"\n## {next_section}. 附录：交互式图表\n",
            iframe,
            "\n\n> 💡 可交互图表：鼠标悬停查看数值、可缩放。",
        ]
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
        f"- 内容：数据结果、分析解读" + ("、内嵌交互式图表" if iframe else "") + "\n"
        "请在最终回复中告知用户报告文件路径。"
    )


build_report_tool = StructuredTool.from_function(
    coroutine=_build_report_coro,
    name="build_report",
    description=(
        "把已完成的查询结果程序化装配为 Markdown 报告并写入工作区 report/ 目录。"
        "自动提取最近一次 check_async_task 成功的数据结果（数据表+SQL+洞察）与"
        " generate_echarts 生成的交互式图表（内嵌 iframe，可交互渲染）。"
        "调用前请确保已用 check_async_task 确认子任务完成、并用 generate_echarts 渲染图表。"
        "报告文件名自动包含精确到时分秒的时间戳，无需再用 shell 取时间。"
    ),
    args_schema=BuildReportSchema,
)
