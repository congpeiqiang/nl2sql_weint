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


def _summarize_result(content: str) -> str:
    """智能摘要子 agent 返回的结果内容，保留结构信息、截断数据行。

    确保 LLM 能判断是否需要图表，同时避免 34k+ tokens 的 prompt 膨胀。
    """
    if len(content) <= _MAX_RESULT_CHARS:
        return content

    # ── 1. 检测 SQL 表格结果（Markdown 表格格式） ──
    lines = content.split("\n")
    table_lines = [l for l in lines if l.strip().startswith("|")]
    if len(table_lines) >= 3:  # 至少有 header + separator + 1 行数据
        header = table_lines[0]
        sep = table_lines[1] if len(table_lines) > 1 else ""
        data_rows = table_lines[2:]
        kept = data_rows[:_MAX_RESULT_ROWS]
        suffix = (
            f"\n\n*(共 {len(data_rows)} 行数据，以上展示前 {len(kept)} 行；"
            f"完整结果可通过 check_async_task 增量读取获取)*"
        )
        summary = header + "\n" + sep + "\n" + "\n".join(kept) + suffix
        return summary

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


def _extract_last_sql(messages) -> str:
    """从子线程消息提取最后一次 run_sql 的真实执行 SQL。

    报告装配（build_report）需要把真实执行的 SQL 写进报告，但 nl2sql 子 agent
    的最终回复通常只有数据表+分析文字，SQL 只在中间的 run_sql 工具调用里
    （wrenai_<库名>_run_sql / dbmcp_run_sql，sql 在 tool_calls[].args.sql）。
    这里确定性提取，不依赖 LLM 在最终回复中附带 SQL。

    兼容 dict（threads.get_state 返回的 checkpoint）与 LangChain Message 对象。
    仅匹配 run_sql（不含 dry_run 校验调用）；取最后一次即产生最终数据的那条。
    """
    last_tool_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict):
            role = m.get("role") or m.get("type")
            name = m.get("name") or ""
        else:
            role = getattr(m, "type", "")
            name = getattr(m, "name", "") or ""
        if role in ("tool", "tool_result") and "run_sql" in name:
            last_tool_idx = i
    if last_tool_idx < 0:
        return ""
    # 回看该 tool 消息之前的 AI tool_calls，取同名调用 args.sql
    for j in range(last_tool_idx - 1, -1, -1):
        m = messages[j]
        if isinstance(m, dict):
            tool_calls = m.get("tool_calls") or []
        else:
            tool_calls = getattr(m, "tool_calls", None) or []
        for c in tool_calls:
            name = c.get("name") if isinstance(c, dict) else getattr(c, "name", "")
            args = c.get("args") if isinstance(c, dict) else getattr(c, "args", {})
            if (
                isinstance(name, str)
                and name.endswith("run_sql")
                and isinstance(args, dict)
            ):
                sql = args.get("sql", "")
                if isinstance(sql, str) and sql.strip():
                    return sql.strip()
    return ""


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
        elif run["status"] == "error":
            error_detail = run.get("error")
            result["error"] = (
                str(error_detail) if error_detail
                else "The async subagent encountered an error."
            )
        elif run["status"] == "interrupted":
            # P1-3：审批闸门暂停。前端会渲染审批卡，用户操作后自动恢复，
            # 主 agent 无需处理（不要取消、不要重新委派）。
            result["awaiting_user_approval"] = True
            result["note"] = (
                "The subagent is paused waiting for the user to approve a SQL "
                "execution (approval card is shown in the frontend). It will "
                "resume automatically once the user decides. Do NOT cancel or "
                "re-delegate; just tell the user the task awaits their approval."
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
