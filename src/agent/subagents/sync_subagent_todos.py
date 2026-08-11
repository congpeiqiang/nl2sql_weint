"""后台同步子智能体 todos 到主智能体 state（零前端改动方案）。

核心逻辑：
1. 首次运行：快照主智能体的原始 todos（排除之前残留的子智能体 todos）
2. 每 2 秒轮询子智能体的 write_todos 结果
3. 合并：主智能体原始 todos + 分隔符 + 子智能体 todos
4. 写回主智能体 state → 前端 TasksFilesSidebar 自动更新
5. 子智能体完成后，清除子智能体部分，只保留主智能体原始 todos
"""
import asyncio
import logging
import re
import threading
from typing import Optional

_logger = logging.getLogger(__name__)
# sync 运行在守护线程，start_server 的 logging 配置不覆盖它；
# 显式加 stdout handler 便于观察真实执行（诊断用）
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[sync] %(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)

# ── 进程级写锁 ──────────────────────────────────────────────────
# 并发多查询时，多个 sync 守护线程会对主线程 state 并发 update_state。
# 实测发现 `as_node="__start__"` 的 update_state 在并发下有读-改-写竞态
# （后写的基于旧值覆盖，reducer 合并失效 → 部分任务的 query_headers/字段丢失）。
# 用进程级锁串行化所有 keyed 字段写入，避免竞态。
_SYNC_WRITE_LOCK = threading.Lock()

# ── 标记常量 ────────────────────────────────────────────────────
# 用于识别哪些 todo 是同步注入的子智能体进度
_SUBAGENT_MARKER = "🔍"
_SUBAGENT_PREFIX = "└ "  # 树状缩进，HTML 中可见


# 步骤耗时后缀格式，如 " (3s)"、" (1m30s)"、" (5s...)"
_DURATION_SUFFIX_RE = re.compile(r"\((\d+[sm])(?:\d+[sm])?\.{0,3}\)\s*$")


def _strip_duration_suffix(content: str) -> str:
    """去除 todo content 末尾附加的耗时后缀（如 " (3s)" / " (5s...)"）。

    计时逻辑用「去掉耗时后的原始 content」作为稳定 key。
    否则附加耗时后 content 变化，下一轮 key 匹配不上，
    会导致状态转换检测失败（completed 的还在计时、in_progress 的未开始计时）。
    """
    return _DURATION_SUFFIX_RE.sub("", content).rstrip()


# ── 公开入口 ────────────────────────────────────────────────────


def launch_sync(
    main_thread_id: str,
    sub_thread_id: str,
    agent_name: str,
    task: Optional[dict] = None,
):
    """在守护线程中启动异步同步任务。每个子任务一个线程。

    Args:
        main_thread_id: 主智能体的 thread_id
        sub_thread_id: 子智能体的 thread_id（= task_id）
        agent_name: 子智能体名称（如 "nl2sql"）
        task: start_async_task 返回的 AsyncTask 字典（含 run_id/created_at/agent_name）
    """
    t = threading.Thread(
        target=_run_sync_loop,
        args=(main_thread_id, sub_thread_id, agent_name, task),
        daemon=True,
        name=f"subagent-sync-{sub_thread_id[:8]}",
    )
    t.start()
    _logger.info(
        "[sync] 启动同步: main=%s, sub=%s, agent=%s",
        main_thread_id[:8],
        sub_thread_id[:8],
        agent_name,
    )


# ── 守护线程入口 ────────────────────────────────────────────────


def _run_sync_loop(
    main_thread_id: str,
    sub_thread_id: str,
    agent_name: str,
    task: Optional[dict] = None,
):
    """守护线程入口：创建新事件循环运行异步同步逻辑。"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _async_sync_loop(main_thread_id, sub_thread_id, agent_name, task)
        )
    except Exception as e:
        _logger.error("[sync] 同步循环异常: %s", e)
    finally:
        loop.close()


# ── 核心异步同步循环 ────────────────────────────────────────────


def _sync_update_state(thread_id: str, values: dict):
    """在进程级锁内执行 update_state（同步封装，供 asyncio.to_thread 调用）。

    并发多查询时，多个 sync 守护线程会对主线程 state 并发 update_state。
    `as_node="__start__"` 的 update_state 在并发下有读-改-写竞态（后写者基于旧值
    覆盖，reducer 合并失效 → 部分任务字段丢失）。用进程级 threading.Lock 串行化。

    说明：本函数在 asyncio.to_thread 的线程池线程中运行，内部用同步 HTTP client
    在锁内调用（持锁期间不 await，避免阻塞事件循环/死锁）。
    """
    import os as _os
    from langgraph_sdk import get_client as _get_async_client

    _api_url = _os.getenv("LANGGRAPH_API_URL", "http://localhost:2026")
    # 在独立事件循环中执行 async update_state（to_thread 线程内）
    def _do():
        import asyncio as _aio

        async def _inner():
            c = _get_async_client(url=_api_url)
            await c.threads.update_state(
                thread_id=thread_id,
                values=values,
                as_node="__start__",
            )

        _aio.run(_inner())

    with _SYNC_WRITE_LOCK:
        _do()


async def _async_sync_loop(
    main_thread_id: str,
    sub_thread_id: str,
    agent_name: str,
    task: Optional[dict] = None,
):
    """核心异步同步循环。每 0.5 秒检查子智能体进度并写入主智能体 state。

    并发多查询设计：本线程只写「自己的 task_id」对应的 key 字段
    （query_headers / subagent_steps_map / active_queries / async_tasks 单 key），
    通过 LangGraph reducer 合并，避免多线程互相覆盖。
    """
    import os
    import time
    from langgraph_sdk import get_client

    # 用 HTTP client 而非 ASGI in-process：sync 线程是独立守护线程，
    # 没有主进程的 ASGI 应用上下文，get_client(url=None) 的 get_state 会
    # 报 'NoneType' object is not callable，导致 keyed 字段写入不可靠。
    _api_url = os.getenv("LANGGRAPH_API_URL", "http://localhost:2026")
    client = get_client(url=_api_url)
    # 等待子智能体的 run 创建完成
    await asyncio.sleep(3)
    last_sub_todos: Optional[list] = None  # 上一次同步的子智能体 todos
    query_title: Optional[str] = None       # 本任务标题（首次提取）
    sub_agent_done = False
    has_notified_completion = False
    post_complete_cycles = 0
    final_sub_todos: Optional[list] = None  # 子智能体最终步骤（含耗时）
    query_headers_written = False  # 是否已写入 query_headers 到 state
    active_queries_written = False  # 是否已写入 active_queries=true 到 state
    active_queries_cleared = False  # 是否已写入 active_queries=false 到 state
    POST_COMPLETE_MAX_CYCLES = 20           # 完成后继续监控 10s (20 × 0.5s) 让耗时稳定
    STALE_RUN_TIMEOUT = 300                 # 子 run 运行时长上限：超过视为卡死，强制结束（兜底）

    # 主智能体步骤耗时追踪（保留原逻辑，仅用于日志/展示，不写回 state）
    prev_main_statuses: dict = {}   # {content: status} 上一次各步骤状态
    main_step_starts: dict = {}     # {content: timestamp} 步骤开始时间
    main_step_durations: dict = {}  # {content: "Xs"} 已完成步骤耗时

    wait_for_run_cycles = 0  # 等待 run 出现的周期数
    loop_start = time.monotonic()  # 本任务开始时间（用于运行时长保护）

    while True:
        await asyncio.sleep(0.5)  # 加快同步频率，减少 write_todos 覆盖窗口
        try:
            # ── 1. 检查子智能体 run 状态 ──
            if not sub_agent_done:
                run_status = await _get_run_status(client, sub_thread_id)
                if run_status is None:
                    # run 可能还没创建，等待重试（最多 20s）
                    wait_for_run_cycles += 1
                    if wait_for_run_cycles > 10:
                        _logger.warning("[sync] 等待 run 超时，退出")
                        break
                    continue  # 继续等待，不退出
                wait_for_run_cycles = 0  # 重置
                # 运行时长保护：子 run 一直 running 且超过上限，视为卡死，
                # 强制按 error 处理，确保 active_queries 最终翻转、前端恢复。
                # （正常情况下 run_sql 等工具自身有超时，不会走到这一步，这是兜底。）
                if run_status == "running" and (
                    time.monotonic() - loop_start
                ) > STALE_RUN_TIMEOUT:
                    _logger.warning(
                        "[sync] 子 run 运行超过 %ds，视为卡死，强制结束",
                        STALE_RUN_TIMEOUT,
                    )
                    run_status = "error"
                if run_status in ("success", "error"):
                    sub_agent_done = True
                    _logger.info(
                        "[sync] 子智能体 %s，进入标题守护+通知模式",
                        run_status,
                    )

            # ── 2. 每次读取主智能体当前 todos（实时，非快照）──
            current_main_todos = await _read_current_main_todos(
                client, main_thread_id
            )

            # ── 2a. 追踪主智能体步骤状态变化，计算耗时 ──
            import time as _time

            _now = _time.time()
            # 关键：用「去掉耗时后缀的原始 content」作为稳定 key。
            # 否则附加耗时后 content 变化，下一轮 key 匹配不上，
            # 状态转换检测会失效（completed 的还在计时 / in_progress 未开始计时）。
            for t in current_main_todos:
                key = _strip_duration_suffix(t.get("content", ""))
                status = t.get("status", "pending")
                prev_status = prev_main_statuses.get(key)

                if prev_status != status:
                    if status == "in_progress" and key not in main_step_starts:
                        # 步骤开始
                        main_step_starts[key] = _now
                    elif status == "completed" and key in main_step_starts:
                        # 步骤完成，计算耗时
                        secs = int(_now - main_step_starts[key])
                        dur = f"{secs}s" if secs < 60 else f"{secs // 60}m{secs % 60}s"
                        main_step_durations[key] = dur

                prev_main_statuses[key] = status

            # 将耗时附加到 current_main_todos 的 content 上（幂等：基于原始 content 重建）
            for t in current_main_todos:
                raw = t.get("content", "")
                key = _strip_duration_suffix(raw)
                dur = main_step_durations.get(key, "")
                if dur:
                    # 已完成步骤：显示最终耗时（始终用原始 content 重建，避免重复叠加后缀）
                    t["content"] = f"{key} ({dur})"
                elif key in main_step_starts and t.get("status") == "in_progress":
                    # 进行中：显示已耗时
                    elapsed = int(_now - main_step_starts[key])
                    elapsed_str = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m{elapsed % 60}s"
                    t["content"] = f"{key} ({elapsed_str}...)"

            # ── 2b. 首次提取本任务标题（按 task_id 对应，避免并发时取错）──
            if query_title is None:
                query_title = await _extract_task_title(
                    client, main_thread_id, sub_thread_id
                )
                if not query_title:
                    query_title = await _extract_user_query(
                        client, main_thread_id
                    )

            # ── 2c. 写入 query_headers[task_id]（单 key，reducer 合并）──
            if query_title and not query_headers_written:
                display = query_title[:50] + ("..." if len(query_title) > 50 else "")
                query_header_entry = {
                    "id": f"__query_header__{sub_thread_id[:8]}",
                    "content": f"📋 {display}",
                    "status": "query",
                    "task_id": sub_thread_id,
                }
                try:
                    await asyncio.to_thread(_sync_update_state, main_thread_id, {"query_headers": {sub_thread_id: query_header_entry}})
                    query_headers_written = True
                    _logger.info("[sync] 写入 query_headers[%s]: %s", sub_thread_id[:8], display)
                except Exception as e:
                    # 写入失败（可能主Agent run还在运行），下个周期重试
                    _logger.debug("[sync] 写入 query_headers 失败(将重试): %s", str(e)[:80])

            # ── 2d. 写入 active_queries[task_id]=true（本任务运行中）──
            if not active_queries_written:
                try:
                    await asyncio.to_thread(_sync_update_state, main_thread_id, {"active_queries": {sub_thread_id: True}})
                    active_queries_written = True
                    _logger.info("[sync] 写入 active_queries[%s]=true", sub_thread_id[:8])
                except Exception as e:
                    _logger.debug("[sync] 写入 active_queries=true 失败(将重试): %s", str(e)[:80])

            # ── 3. 子智能体运行中：写入 subagent_steps 独立字段 ──
            if not sub_agent_done:
                sub_todos = await _extract_subagent_todos(
                    client, sub_thread_id
                )
                if sub_todos and sub_todos != last_sub_todos:
                    # 构建子智能体步骤列表（含进度头），步骤 id 加 task 前缀防 React key 冲突
                    completed = sum(
                        1 for t in sub_todos if t["status"] == "completed"
                    )
                    total = len(sub_todos)
                    has_in_progress = any(
                        t["status"] == "in_progress" for t in sub_todos
                    )
                    task_prefix = sub_thread_id[:8]
                    steps = [
                        {
                            "id": f"__subagent_header_{task_prefix}__",
                            "content": f"{_SUBAGENT_MARKER} {agent_name} 执行进度 ({completed}/{total})",
                            "status": "in_progress" if has_in_progress else "completed",
                        }
                    ]
                    for i, t in enumerate(sub_todos):
                        steps.append(
                            {
                                "id": f"__subagent_{task_prefix}_{i}__",
                                "content": f"{_SUBAGENT_PREFIX}{t['content']}",
                                "status": t["status"],
                            }
                        )
                    await asyncio.to_thread(_sync_update_state, main_thread_id, {"subagent_steps_map": {sub_thread_id: steps}})
                    _logger.info(
                        "[sync] 写入 subagent_steps_map[%s]: %d 项, header=%s",
                        task_prefix,
                        len(steps),
                        steps[0]["content"] if steps else "empty",
                    )
                    last_sub_todos = sub_todos
                    _logger.info(
                        "[sync] 已同步 %d/%d 步 (主Agent %d项)",
                        completed,
                        total,
                        len(current_main_todos),
                    )

            # ── 4. 子智能体完成后：写最终步骤 + async_tasks 单 key + 结束标记 ──
            else:
                if not has_notified_completion:
                    has_notified_completion = True
                    # 快照子智能体最终步骤（含耗时）
                    final_sub_todos = await _extract_subagent_todos(
                        client, sub_thread_id
                    )
                    _logger.info(
                        "[sync] 快照最终步骤: %d 项",
                        len(final_sub_todos) if final_sub_todos else 0,
                    )
                    # 写最终 completed steps 到 subagent_steps_map[task_id]
                    task_prefix = sub_thread_id[:8]
                    if final_sub_todos:
                        completed_steps = [
                            {
                                "id": f"__subagent_header_{task_prefix}__",
                                "content": f"{_SUBAGENT_MARKER} {agent_name} 执行进度 (已完成)",
                                "status": "completed",
                            }
                        ]
                        for i, t in enumerate(final_sub_todos):
                            completed_steps.append(
                                {
                                    "id": f"__subagent_{task_prefix}_{i}__",
                                    "content": f"{_SUBAGENT_PREFIX}{t['content']}",
                                    "status": "completed",
                                }
                            )
                        try:
                            await asyncio.to_thread(_sync_update_state, main_thread_id, {"subagent_steps_map": {sub_thread_id: completed_steps}})
                            _logger.info(
                                "[sync] 写入最终 subagent_steps_map[%s]: %d 项",
                                task_prefix,
                                len(completed_steps),
                            )
                        except Exception as e:
                            _logger.warning("[sync] 写入最终 steps 失败: %s", e)

                    # 写 async_tasks 单 key（基于传入 task 字典 + run_status，无读-改-写竞态）
                    try:
                        base = dict(task or {})
                        base["task_id"] = sub_thread_id
                        base["agent_name"] = agent_name
                        base["status"] = run_status  # "success" / "error"
                        import time as _time2
                        now_str = _time2.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", _time2.gmtime()
                        )
                        base["last_checked_at"] = now_str
                        base["last_updated_at"] = now_str
                        await asyncio.to_thread(_sync_update_state, main_thread_id, {"async_tasks": {sub_thread_id: base}})
                        _logger.info(
                            "[sync] 写 async_tasks[%s] 状态=%s", sub_thread_id[:8], run_status
                        )
                    except Exception as e:
                        _logger.warning("[sync] 更新 async_tasks 失败: %s", e)

                    # 清除本任务运行标记（固定宽限后退出，不等主线程 todos）
                    try:
                        await asyncio.to_thread(_sync_update_state, main_thread_id, {"active_queries": {sub_thread_id: False}})
                        active_queries_cleared = True
                        _logger.info("[sync] 写 active_queries[%s]=false", sub_thread_id[:8])
                    except Exception as e:
                        _logger.warning("[sync] 写 active_queries=false 失败: %s", e)

                    _logger.info("[sync] 子智能体完成，进入固定宽限期")

                # 后续周期：固定宽限计数后退出（不再依赖主线程 todos 完成，
                # 因为并发时那是全局条件，会被其他任务拖住）
                post_complete_cycles += 1
                if post_complete_cycles >= POST_COMPLETE_MAX_CYCLES:
                    _logger.info(
                        "[sync] 退出: cycles=%d", post_complete_cycles
                    )
                    break

        except Exception as e:
            _logger.warning("[sync] 同步失败: %s", e)

    _logger.info("[sync] 退出: %s", sub_thread_id[:8])


# ── 辅助函数 ────────────────────────────────────────────────────


async def _get_run_status(client, thread_id: str) -> Optional[str]:
    """获取线程上最新 run 的状态。"""
    try:
        runs = await client.runs.list(thread_id=thread_id, limit=1)
        if not runs:
            return None
        return runs[0].get("status", "unknown")
    except Exception as e:
        _logger.warning("[sync] get_run_status failed: %s", e)
        return None


async def _extract_task_title(
    client, main_thread_id: str, sub_thread_id: str
) -> Optional[str]:
    """按 task_id 提取本任务的查询标题。

    并发多查询时，不能用"最后一条用户消息"当标题（那会让所有任务拿到
    同一个最后查询）。正确方式：扫主线程消息，找到 `start_async_task`
    工具调用，其返回的 ToolMessage 中包含本 task_id，取该调用的
    args.description 作为本任务标题。

    Returns:
        本任务的标题；找不到返回 None（调用方回退 _extract_user_query）。
    """
    try:
        state = await client.threads.get_state(thread_id=main_thread_id)
        messages = (state.get("values") or {}).get("messages", [])
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or msg.get("type")
            # 找到 start_async_task 的 AI 工具调用
            if role in ("ai", "assistant"):
                for tc in msg.get("tool_calls") or []:
                    if tc.get("name") != "start_async_task":
                        continue
                    desc = (tc.get("args") or {}).get("description", "")
                    # 检查后续 ToolMessage 是否含本 task_id
                    found = False
                    for later in messages:
                        if not isinstance(later, dict):
                            continue
                        if (later.get("role") or later.get("type")) != "tool":
                            continue
                        tool_msg = str(later.get("content", ""))
                        if later.get("tool_call_id") == tc.get("id") and sub_thread_id in tool_msg:
                            found = True
                            break
                    if found and desc:
                        # description 是完整子任务 prompt（含【任务目标】【数据库名称】等），
                        # 只提取【任务目标】后的用户问题作为标题，避免标题被 prompt 污染
                        desc_str = str(desc).strip()
                        goal_marker = "【任务目标】"
                        idx = desc_str.find(goal_marker)
                        if idx >= 0:
                            after = desc_str[idx + len(goal_marker):].strip()
                            # 截断到下一个【 分隔符
                            end = after.find("【")
                            if end > 0:
                                after = after[:end]
                            return after.strip()
                        return desc_str
        return None
    except Exception as e:
        _logger.warning("[sync] extract_task_title failed: %s", e)
        return None


async def _extract_user_query(client, main_thread_id: str) -> Optional[str]:
    """从主智能体消息中提取最后一条「真正的」用户查询。

    关键：排除系统自动注入的通知消息（auto-continue / [系统自动通知] 等），
    否则 sync 在 auto-continue 之后提取标题时，会把通知文本当成用户查询，
    导致进度条「问题」标题显示成 "[系统自动通知] nl2sql 子智能体已成功完成..."。
    """
    try:
        state = await client.threads.get_state(thread_id=main_thread_id)
        messages = (state.get("values") or {}).get("messages", [])
        # 从后往前找最后一条「非系统」的 human 消息
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or msg.get("type")
            if role not in ("human", "user"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                # 多模态消息：提取第一个 text 块
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content = block.get("text", "")
                        break
            text = str(content).strip()
            if not text:
                continue
            # 跳过系统自动通知 / auto-continue 消息
            if text.startswith("[系统自动通知]") or text.startswith("[系统通知]"):
                continue
            if "子智能体已成功完成数据查询" in text or "请**不要**再次调用 start_async_task" in text:
                continue
            return text
        return None
    except Exception as e:
        _logger.warning("[sync] extract_user_query failed: %s", e)
        return None


async def _read_current_main_todos(client, main_thread_id: str) -> list:
    """读取主智能体当前 todos，过滤掉之前同步注入的子智能体项。

    每次调用都实时读取 state（非快照），确保主智能体自身的
    todo 更新（如标记 completed、新增步骤）不会被覆盖。
    """
    try:
        state = await client.threads.get_state(thread_id=main_thread_id)
        todos = (state.get("values") or {}).get("todos") or []
        return [
            t
            for t in todos
            if not t.get("content", "").startswith(_SUBAGENT_MARKER)
            and not t.get("content", "").startswith(_SUBAGENT_PREFIX)
            and not str(t.get("id", "")).startswith("__subagent_")
            and str(t.get("id", "")) != "__query_header__"
        ]
    except Exception as e:
        _logger.warning("[sync] read_current_main_todos failed: %s", e)
        return []


async def _get_raw_todos(client, main_thread_id: str) -> list:
    """读取主智能体 state 中的原始 todos（不做过滤）。"""
    try:
        state = await client.threads.get_state(thread_id=main_thread_id)
        return (state.get("values") or {}).get("todos") or []
    except Exception as e:
        _logger.warning("[sync] get_raw_todos failed: %s", e)
        return []


async def _snapshot_main_todos(client, main_thread_id: str) -> list:
    """向后兼容别名 — 等同于 _read_current_main_todos。"""
    return await _read_current_main_todos(client, main_thread_id)


def _compute_step_durations(
    sub_thread_id: str, completed_steps: set | None = None
) -> dict:
    """从本地进度文件读取每步耗时，返回 {step_name: duration_str}。

    Args:
        sub_thread_id: 子智能体线程 ID。
        completed_steps: 已知已完成（write_todos status=completed）的 step 集合。
            某些 step 在进度文件中可能缺少 `to:"completed"` 事件（写入时机晚于
            write_todos），导致被误判为进行中并输出 "Xs..."（看起来仍在计时）。
            传入该集合后，对已完成的 step 强制输出固定耗时（不带省略号）。
    """
    import time as _time

    try:
        from agent.subagents.track_progress import read_progress

        progress = read_progress(sub_thread_id)
        if not progress:
            return {}

        step_history = progress.get("step_history", [])
        started_at = progress.get("started_at")  # 整个任务开始时间
        now = _time.time()
        durations = {}
        transitions = {}

        for ev in step_history:
            step = ev.get("step", "")
            transitions.setdefault(step, []).append(ev)

        completed_steps = completed_steps or set()
        for step, evts in transitions.items():
            started = None
            ended = None
            # 取「最后一次」in_progress 与「最后一次」completed。
            # 若 step 重复执行（多个 in_progress），以最后一段执行时间为准。
            for ev in evts:
                if ev.get("to") == "in_progress":
                    started = ev["ts"]
                elif ev.get("to") == "completed":
                    ended = ev["ts"]
            # 如果没有 in_progress 记录但有 completed，用 started_at 作为开始时间
            if not started and ended and started_at:
                started = started_at

            # 计算耗时
            def _fmt(secs: int, suffix: str = "") -> str:
                return f"{secs}s{suffix}" if secs < 60 else f"{secs // 60}m{secs % 60}s{suffix}"

            if started and ended:
                secs = int(ended - started)
                durations[step] = _fmt(max(secs, 0))
            elif started:
                secs = int(now - started)
                if step in completed_steps:
                    # 进度文件缺 completed 事件，但该 step 实际已完成 → 固定耗时，不带 ...
                    durations[step] = _fmt(max(secs, 0))
                else:
                    durations[step] = _fmt(max(secs, 0), "...")
        return durations
    except Exception as e:
        _logger.debug("[sync] compute_step_durations failed: %s", e)
        return {}


async def _extract_subagent_todos(client, sub_thread_id: str) -> list:
    """从子智能体线程提取最新的 write_todos 结果（含每步耗时）。"""
    try:
        state = await client.threads.get_state(thread_id=sub_thread_id)
        messages = (state.get("values") or {}).get("messages", [])

        # 读取本地进度文件获取耗时
        # 传入 completed_steps：write_todos 中 status=completed 的 step 集合，
        # 避免进度文件缺 completed 事件时误输出 "Xs..."（已完成还在计时）。
        durations = _compute_step_durations(sub_thread_id)

        def _with_duration(content: str) -> str:
            dur = durations.get(content, "")
            return f"{content} ({dur})" if dur else content

        # 从后往前找最新的 write_todos
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or msg.get("type")

            # 方式 1：AI 消息中的 tool_calls args
            if role in ("ai", "assistant"):
                for tc in msg.get("tool_calls") or []:
                    if tc.get("name") == "write_todos":
                        todos = tc.get("args", {}).get("todos", [])
                        if todos:
                            completed_steps = {
                                t.get("content", "")
                                for t in todos
                                if t.get("status") == "completed"
                            }
                            durations = _compute_step_durations(
                                sub_thread_id, completed_steps
                            )
                            return [
                                {
                                    "content": _with_duration(t.get("content", "")),
                                    "status": t.get("status", "pending"),
                                }
                                for t in todos
                            ]

            # 方式 2：ToolMessage content 解析（fallback）
            if role == "tool" and msg.get("name") == "write_todos":
                content = msg.get("content", "")
                if isinstance(content, str):
                    items = re.findall(
                        r"\{'content':\s*'([^']*)',\s*'status':\s*'([^']*)'\}",
                        content,
                    )
                    if items:
                        completed_steps = {
                            c for c, s in items if s == "completed"
                        }
                        durations = _compute_step_durations(
                            sub_thread_id, completed_steps
                        )
                        return [
                            {"content": _with_duration(c), "status": s}
                            for c, s in items
                        ]
        return []
    except Exception as e:
        _logger.warning("[sync] extract_subagent_todos failed: %s", e)
        return []


async def _notify_main_agent_continue(
    client, main_thread_id: str, agent_name: str
):
    """子智能体完成后，通知主智能体继续执行后续步骤（绘图、报告）。

    等待主智能体当前 run 结束，然后注入一条 HumanMessage 触发新 run。
    """
    import logging
    _log = logging.getLogger(__name__)

    try:
        # 1. 等待主智能体当前 run 结束（最多等 30s）
        _log.info("[sync] 等待主智能体 run 结束...")
        for i in range(15):
            runs = await client.runs.list(
                thread_id=main_thread_id, limit=1
            )
            if not runs:
                break
            status = runs[0].get("status", "unknown")
            _log.info("[sync] 主智能体 run 状态: %s (第%d次)", status, i + 1)
            if status in ("success", "error"):
                break
            await asyncio.sleep(2)

        # 2. 将通知消息直接作为 runs.create 的输入（而非 update_state）
        #    这样新 run 会看到这条新消息并触发 LLM 处理
        continue_content = (
            f"[系统通知] {agent_name} 子智能体已完成查询任务。"
            "请立即继续执行后续步骤：推荐并渲染图表、生成分析报告。"
            "不要等待用户指示。"
        )
        _log.info("[sync] 注入通知消息并创建新 run")

        # 3. 启动新 run，消息作为 input 传入
        run = await client.runs.create(
            thread_id=main_thread_id,
            assistant_id="chat_agent",
            input={
                "messages": [
                    {"role": "user", "content": continue_content}
                ]
            },
            config={"recursion_limit": 500},
        )
        _log.info(
            "[sync] 已创建新 run: %s，主智能体将继续执行",
            run.get("run_id", "unknown") if isinstance(run, dict) else run,
        )

    except Exception as e:
        _log.error("[sync] 通知主智能体继续失败: %s", e, exc_info=True)


def _merge_todos(
    main_todos: list, sub_todos: list, agent_name: str
) -> list:
    """合并主智能体 todos 和子智能体 todos。

    顺序：已完成的主任务 → 子智能体详细步骤 → 待执行的主任务
    """
    if not sub_todos:
        return list(main_todos)

    # 分离主智能体 todos：已完成 vs 未完成
    completed_main = [t for t in main_todos if t.get("status") == "completed"]
    pending_main = [t for t in main_todos if t.get("status") != "completed"]

    # 子智能体进度头
    completed = sum(1 for t in sub_todos if t["status"] == "completed")
    total = len(sub_todos)
    has_in_progress = any(t["status"] == "in_progress" for t in sub_todos)

    sub_header = {
        "id": "__subagent_header__",
        "content": (
            f"{_SUBAGENT_MARKER} {agent_name} 执行进度"
            f" ({completed}/{total})"
        ),
        "status": "in_progress" if has_in_progress else "completed",
    }

    # 子智能体步骤（带缩进）
    sub_items = [
        {
            "id": f"__subagent_{i}__",
            "content": f"{_SUBAGENT_PREFIX}{t['content']}",
            "status": t["status"],
        }
        for i, t in enumerate(sub_todos)
    ]

    # 合并顺序：已完成主任务 → 子智能体 → 待执行主任务
    return completed_main + [sub_header] + sub_items + pending_main
