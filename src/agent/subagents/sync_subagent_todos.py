"""后台同步子智能体 todos 到主智能体 state（零前端改动方案）。

核心逻辑：
1. 首次运行：快照主智能体的原始 todos（排除之前残留的子智能体 todos）
2. 每 2 秒轮询子智能体的 write_todos 结果
3. 合并：主智能体原始 todos + 分隔符 + 子智能体 todos
4. 写回主智能体 state → 前端 TasksFilesSidebar 自动更新
5. 子智能体完成后，清除子智能体部分，只保留主智能体原始 todos

追踪集成：在子智能体进度/完成/错误等关键节点写入 EventStore（统一轨迹日志）。
"""
import asyncio
import logging
import os as _os_module
import re
import threading
import time as _time_module
from pathlib import Path
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

# ── 活跃同步线程注册表（P1-3）─────────────────────────────────
# sub_thread_id → 正在运行的 sync 守护线程。SQL 审批恢复端点在续跑子 run 前
# 检查对应 watcher 是否还活着；若已退出（如等待审批超上限），重新 launch 一个，
# 保证子任务完成后 async_tasks 终态/进度同步不丢。
_ACTIVE_LOOPS: dict[str, threading.Thread] = {}
_ACTIVE_LOOPS_LOCK = threading.Lock()


def is_sync_alive(sub_thread_id: str) -> bool:
    """该子任务的 sync watcher 线程是否仍在运行。"""
    with _ACTIVE_LOOPS_LOCK:
        t = _ACTIVE_LOOPS.get(sub_thread_id)
    return t is not None and t.is_alive()


def _get_workspace_manager():
    """延迟导入 WorkspaceManager（避免模块级循环依赖）。"""
    from agent.workspace_manager import get_workspace_manager
    return get_workspace_manager()

# ── 标记常量 ────────────────────────────────────────────────────
# 用于识别哪些 todo 是同步注入的子智能体进度
_SUBAGENT_MARKER = "🔍"
_SUBAGENT_PREFIX = "└ "  # 树状缩进，HTML 中可见

# ── run 终态（P1-7）─────────────────────────────────────────────
# deepagents 的终态集虽包含 cancelled/timeout/interrupted，但 interrupted 对
# nl2sql 已不是终态（审批闸门移除后它是会自恢复的瞬时暂停），故此处不纳入，
# 单独用 INTERRUPTED_STUCK_TIMEOUT 兜底。此前只认 success/error，
# 被取消的任务会在超时兜底后被覆写成 error（cancelled 被盖掉）。
_RUN_DONE_STATUSES = ("success", "error", "cancelled", "timeout")
# 终态 → 最终步骤头的展示文案
_DONE_LABELS = {
    "success": "已完成",
    "error": "执行失败",
    "cancelled": "已取消",
    "timeout": "超时终止",
}

# ── P1-3 SQL 审批等待 ─────────────────────────────────────────
# 该注释已过期：2026-08-28 审批闸门升级为只读硬拦截（不再 raise interrupt），
# 子 run 的 interrupted 不再是「等审批」，而是 deepagents 上下文压缩的瞬时暂停。
# 同步器已在 _RUN_DONE_STATUSES 移除 interrupted（不把瞬时暂停当终态），并用
# INTERRUPTED_STUCK_TIMEOUT 兜底防永久卡死。此常量仅历史审批路径保留。
_AWAIT_APPROVAL_TIMEOUT = 7200  # 等待审批上限 2h（超了按 timeout 强制收尾）


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
    with _ACTIVE_LOOPS_LOCK:
        _ACTIVE_LOOPS[sub_thread_id] = t
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
        with _ACTIVE_LOOPS_LOCK:
            if _ACTIVE_LOOPS.get(sub_thread_id) is threading.current_thread():
                del _ACTIVE_LOOPS[sub_thread_id]


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

    # ── 初始化 EventStore（追踪集成） ──
    _trace_store = None
    try:
        from agent.trace.event_store import EventStore
        from agent.trace.event_log import EventType
        wm = _get_workspace_manager()
        _trace_db = str(wm.shared_trace_db)
        _trace_store = EventStore(_trace_db)
        _trace_store.open()
        # 记录子智能体启动事件
        _trace_store.insert_event_sync(
            thread_id=sub_thread_id,
            event_type=EventType.SUBAGENT_SPAWN,
            agent_type="nl2sql_agent",
            parent_thread_id=main_thread_id,
            task_id=sub_thread_id,
            data={"description": task.get("description", "") if task else ""},
        )
    except Exception as e:
        _logger.debug("[sync] EventStore init failed: %s", e)

    last_sub_todos: Optional[list] = None  # 上一次同步的子智能体 todos
    query_title: Optional[str] = None       # 本任务标题（首次提取）
    sub_agent_done = False
    has_notified_completion = False
    post_complete_cycles = 0
    final_sub_todos: Optional[list] = None  # 子智能体最终步骤（含耗时）
    query_headers_written = False  # 是否已写入 query_headers 到 state
    active_queries_written = False  # 是否已写入 active_queries=true 到 state
    active_queries_cleared = False  # 是否已写入 active_queries=false 到 state
    async_tasks_written = False  # 是否已写入 async_tasks 终止态（主线程 in-flight 时会多次被拒，需重试）
    query_header_entry = None    # 本任务 query_headers 条目（2c 写入时赋值，供 M-T5c 描述兜底）
    description_written = False  # 是否已把任务描述 merge 进 async_tasks（M-T5c，一次性）
    failure_reported_local = False  # 方案1：本 sync 线程内失败汇报是否已尝试（去重，state 标记兜底跨线程）
    # ── P1-3 SQL 审批等待状态 ──
    approval_pending = False          # 子 run 正停在审批 interrupt 上
    approval_relayed = False          # awaiting_approval 已写入主线程 async_tasks
    approval_clear_written = False    # 恢复后已清除 awaiting_approval（写入新条目）
    approval_wait_start: Optional[float] = None
    interrupted_since: Optional[float] = None  # 子 run 进入 interrupted（瞬时暂停态）的时间点
    completion_write_retries = 0  # async_tasks 写入失败重试次数
    completion_started_at: Optional[float] = None  # 进入完成分支的时间点（重试上限判定起点）
    POST_COMPLETE_MAX_CYCLES = 20           # 完成后继续监控 10s (20 × 0.5s) 让耗时稳定
    STALE_RUN_TIMEOUT = 600                 # 子 run 运行时长上限 10min（P1-7，对齐 guards timeout-policy）：超过视为卡死，强制结束（兜底）
    INTERRUPTED_STUCK_TIMEOUT = 120         # 子 run 停在 interrupted 的上限 2min（summarization 瞬时暂停远短于此）：超过视为卡死，强制结束
    COMPLETE_WRITE_MAX_SECONDS = 300        # 完成态写入重试上限：主线程持续 in-flight 时放弃（防僵尸线程）

    # 主智能体步骤耗时追踪（保留原逻辑，仅用于日志/展示，不写回 state）
    prev_main_statuses: dict = {}   # {content: status} 上一次各步骤状态
    main_step_starts: dict = {}     # {content: timestamp} 步骤开始时间
    main_step_durations: dict = {}  # {content: "Xs"} 已完成步骤耗时

    wait_for_run_cycles = 0  # 等待 run 出现的周期数
    loop_start = time.monotonic()  # 本任务开始时间（用于运行时长保护）

    # ── P1-9 启动自愈：async_tasks 终态写入与 active_queries=false 是两个不原子写，
    # 中间进程重启会留下"async_tasks=success 但 active_queries=true"的脏状态 → 前端
    # 永远显示执行中卡片。sync 线程启动时若发现该任务已是终态，不再写 active_queries=true，
    # 直接进完成分支清 false 收尾（也顺手幂等重写终态）。
    try:
        _boot_status = await _read_task_status(client, main_thread_id, sub_thread_id)
        if _boot_status in _RUN_DONE_STATUSES:
            _logger.info(
                "[sync] 任务 %s 启动即终态 %s，跳过 active_queries=true，直接清收尾",
                sub_thread_id[:8], _boot_status,
            )
            run_status = _boot_status
            sub_agent_done = True
            active_queries_written = True
    except Exception as _boot_err:
        _logger.debug("[sync] 启动终态检查失败(继续正常流程): %s", _boot_err)

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

                # ── P1-8 取消优先检查（用户点了前端「停止」→ POST /api/threads/{task_id}/cancel）──
                # 主线程 async_tasks 已被标记 cancelled 时：直接跳过一切等待分支
                # （审批中继/超时兜底），按终态收尾。否则任务正停在 SQL 审批闸门时，
                # run 状态是 success（top-level graph suppress interrupt），取消端点
                # cancel run 是 no-op，sync 下一轮仍会把 awaiting_approval 重新中继
                # 回主线程 → 审批卡死而复生。
                if (
                    await _read_task_status(client, main_thread_id, sub_thread_id)
                    == "cancelled"
                ):
                    _logger.info(
                        "[sync] 任务 %s 已被用户取消，按终态收尾", sub_thread_id[:8]
                    )
                    run_status = "cancelled"
                    payload = None
                    approval_pending = False
                    approval_relayed = True
                else:
                    # ── P1-3 SQL 审批：审批闸门暂停不体现在 run 状态上 ──
                    # 子 agent 是独立 top-level graph（client.runs.create 后台 run），
                    # interrupt() 抛的 GraphInterrupt 被 langgraph 内部抑制（_loop.py 对
                    # top-level graph suppress interrupt → 后台 run 状态显示 "success"），
                    # 但 interrupt 已写入线程 state（tasks[].interrupts，next 指向
                    # HumanInTheLoopMiddleware.after_model）。因此审批检测必须读 state，
                    # 不能只看 run_status —— 否则 "success" 会被直接判终态、审批卡永不出现。
                    payload = await _extract_approval_payload(client, sub_thread_id)

                if payload is not None:
                    # 等待审批中：中继 payload 到主线程供前端渲染审批卡，不做终态处理
                    if not approval_pending:
                        approval_pending = True
                        approval_wait_start = time.monotonic()
                        _logger.info("[sync] 任务 %s 等待 SQL 审批", sub_thread_id[:8])
                    if not approval_relayed:
                        try:
                            base = dict(task or {})
                            base["task_id"] = sub_thread_id
                            base["agent_name"] = agent_name
                            base["status"] = "running"
                            base["awaiting_approval"] = payload
                            base["last_updated_at"] = time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            )
                            await asyncio.to_thread(
                                _sync_update_state,
                                main_thread_id,
                                {"async_tasks": {sub_thread_id: base}},
                            )
                            approval_relayed = True
                            _logger.info(
                                "[sync] 已中继审批请求到 async_tasks[%s]",
                                sub_thread_id[:8],
                            )
                        except Exception as e:
                            _logger.debug(
                                "[sync] 写 awaiting_approval 失败(将重试): %s", str(e)[:80]
                            )
                    # 等待超上限 → 放弃，按 timeout 收尾（兜底防僵尸等待）
                    if (
                        approval_wait_start is not None
                        and (time.monotonic() - approval_wait_start)
                        > _AWAIT_APPROVAL_TIMEOUT
                    ):
                        _logger.warning(
                            "[sync] SQL 审批等待超过 %ds，强制结束 (timeout)",
                            _AWAIT_APPROVAL_TIMEOUT,
                        )
                        try:
                            latest = await _get_latest_run(client, sub_thread_id)
                            if latest and latest.get("run_id"):
                                await client.runs.cancel(
                                    thread_id=sub_thread_id,
                                    run_id=latest["run_id"],
                                )
                        except Exception as e:  # noqa: BLE001
                            _logger.warning("[sync] 取消待审批 run 失败: %s", e)
                        approval_pending = False
                        run_status = "timeout"
                    else:
                        continue  # 保持等待，本轮不做终态处理
                elif approval_pending:
                    # 之前等待审批，现在 run 重新运行/结束 → 用户已决策，清除审批标记。
                    # 写成功才置 approval_pending=False，失败下轮重试。
                    try:
                        latest = await _get_latest_run(client, sub_thread_id)
                        base = dict(task or {})
                        base["task_id"] = sub_thread_id
                        base["agent_name"] = agent_name
                        base["status"] = "running"
                        if latest and latest.get("run_id"):
                            base["run_id"] = latest["run_id"]
                        base["last_updated_at"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                        # 不带 awaiting_approval → keyed 合并整条替换，等同清除
                        await asyncio.to_thread(
                            _sync_update_state,
                            main_thread_id,
                            {"async_tasks": {sub_thread_id: base}},
                        )
                        approval_clear_written = True
                        approval_pending = False
                        # 重置 relay 标记：同一子任务可能被审批多次（如先拦截全表
                        # SELECT、批准后子 agent 又生成 DELETE 再次触发审批）。若不
                        # 复位，第二次审批的 payload 会被 `if not approval_relayed`
                        # 跳过、永不中继到前端 → 任务卡死在「查询执行」。
                        approval_relayed = False
                        _logger.info(
                            "[sync] 审批已决策，任务 %s 继续 (run_id=%s)",
                            sub_thread_id[:8],
                            base.get("run_id"),
                        )
                    except Exception as e:
                        _logger.warning(
                            "[sync] 写审批后 async_tasks 失败(将重试): %s", str(e)[:100]
                        )

                # interrupted 瞬时暂停态兜底：summarization 等会自恢复，几秒内回到
                # running/success；但若持续不恢复（真卡死），单独按 timeout 收尾，避免
                # 0.5s 空转轮询永不结束。注意：不能用 loop_start（那是任务总时长，会误杀
                # 「第 10 分钟才短暂 interrupted、随后正常完成」的合法长查询）。
                if run_status == "interrupted":
                    if interrupted_since is None:
                        interrupted_since = time.monotonic()
                    elif (time.monotonic() - interrupted_since) > INTERRUPTED_STUCK_TIMEOUT:
                        _logger.warning(
                            "[sync] 子 run 停留在 interrupted 超过 %ds，视为卡死，强制结束（分类为 timeout）",
                            INTERRUPTED_STUCK_TIMEOUT,
                        )
                        run_status = "timeout"
                        try:
                            _stale_run_id = (task or {}).get("run_id")
                            if _stale_run_id:
                                await client.runs.cancel(
                                    thread_id=sub_thread_id, run_id=_stale_run_id
                                )
                                _logger.info(
                                    "[sync] 已取消卡死 interrupted 子 run: %s", sub_thread_id[:8]
                                )
                        except Exception as e:  # noqa: BLE001
                            _logger.warning("[sync] 取消卡死 interrupted 子 run 失败: %s", e)
                else:
                    interrupted_since = None

                # 运行时长保护：子 run 一直 running 且超过上限，视为卡死，
                # 强制按 timeout 处理（P1-7 失败分类：不再冒充 error），
                # 确保 active_queries 最终翻转、前端恢复。
                # （正常情况下 run_sql 等工具自身有超时，不会走到这一步，这是兜底。）
                if run_status == "running" and (
                    time.monotonic() - loop_start
                ) > STALE_RUN_TIMEOUT:
                    _logger.warning(
                        "[sync] 子 run 运行超过 %ds，视为卡死，强制结束（分类为 timeout）",
                        STALE_RUN_TIMEOUT,
                    )
                    run_status = "timeout"
                    # 真实 kill：取消卡死的子 run，避免其永远占用服务端资源
                    try:
                        _stale_run_id = (task or {}).get("run_id")
                        if _stale_run_id:
                            await client.runs.cancel(
                                thread_id=sub_thread_id, run_id=_stale_run_id
                            )
                            _logger.info(
                                "[sync] 已取消超时子 run: %s", sub_thread_id[:8]
                            )
                    except Exception as e:  # noqa: BLE001
                        _logger.warning("[sync] 取消超时子 run 失败: %s", e)
                if run_status in _RUN_DONE_STATUSES:
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

            # ── 2e. 首次把任务描述 merge 进 async_tasks[task_id]（M-T5c）──
            # 派发时描述登记在进程级任务注册表（_TASK_TRACE_MAP）；此处落进 state，
            # 让跨进程/重启后前端仍能显示真实描述（而非任务 ID）。
            # 前提：query_headers 已成功写入（主线程非 in-flight，state 可写）。
            # 读-改-写复用 _sync_update_state（_SYNC_WRITE_LOCK 串行化）；仅当
            # entry 缺 description 时触发一次。
            if not description_written and query_headers_written:
                _desc_m5 = _lookup_task_description(sub_thread_id, query_header_entry)
                if _desc_m5:
                    try:
                        _st0 = await client.threads.get_state(thread_id=main_thread_id)
                        _tasks0 = (_st0.get("values") or {}).get("async_tasks") or {}
                        _entry0 = _tasks0.get(sub_thread_id)
                        if isinstance(_entry0, dict) and not _entry0.get("description"):
                            _merged0 = dict(_entry0)
                            _merged0["description"] = _desc_m5
                            await asyncio.to_thread(
                                _sync_update_state,
                                main_thread_id,
                                {"async_tasks": {sub_thread_id: _merged0}},
                            )
                            description_written = True
                            _logger.info(
                                "[sync] M-T5c: async_tasks[%s] 补 description=%s",
                                sub_thread_id[:8], _desc_m5[:50],
                            )
                    except Exception as e:
                        _logger.debug(
                            "[sync] M-T5c: 写 description 失败(将重试): %s", str(e)[:80]
                        )

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
                    # 追踪：记录进度事件
                    if _trace_store:
                        try:
                            _trace_store.insert_event_sync(
                                thread_id=sub_thread_id,
                                event_type=EventType.SUBAGENT_PROGRESS,
                                agent_type="nl2sql_agent",
                                parent_thread_id=main_thread_id,
                                task_id=sub_thread_id,
                                data={"completed": completed, "total": total, "step": steps[-1]["content"] if steps else ""},
                            )
                        except Exception as _e:
                            _logger.debug("[sync] trace progress failed: %s", _e)

            # ── 4. 子智能体完成后：写最终步骤 + async_tasks + active_queries=false ──
            # 注意：主线程在 in-flight run（前一个任务的自动续跑/用户消息处理）期间，
            # update_state 会被 LangGraph 拒绝（"has in-flight runs"）。async_tasks 终止态
            # 一旦写不进 state，前端自动续跑就永远看不到该任务（丢失 bug）。因此
            # async_tasks / active_queries=false 必须**重试到成功**，而不是一次性 try/except。
            else:
                if completion_started_at is None:
                    completion_started_at = time.monotonic()

                # 一次性快照最终步骤 + 写最终 completed steps（best-effort，不重试）
                if not has_notified_completion:
                    has_notified_completion = True
                    final_sub_todos = await _extract_subagent_todos(
                        client, sub_thread_id
                    )
                    _logger.info(
                        "[sync] 快照最终步骤: %d 项",
                        len(final_sub_todos) if final_sub_todos else 0,
                    )
                    task_prefix = sub_thread_id[:8]
                    # P1-7：按终态展示（已取消/超时终止/执行失败…），不再一律「已完成」
                    done_label = _DONE_LABELS.get(run_status, "已完成")
                    if final_sub_todos:
                        completed_steps = [
                            {
                                "id": f"__subagent_header_{task_prefix}__",
                                "content": f"{_SUBAGENT_MARKER} {agent_name} 执行进度 ({done_label})",
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

                # 写 async_tasks 单 key（基于传入 task 字典 + run_status，无读-改-写竞态）。
                # 失败持续重试，直到主线程 in-flight run 结束写入成功，或超上限放弃。
                if not async_tasks_written:
                    # P1-7 取消善后：用户已用 cancel_async_task 取消的任务
                    # （state 里已是 cancelled），sync 检测到的任何终态都不得覆盖
                    # （此前超时兜底会把 cancelled 盖成 error）。
                    if run_status != "cancelled":
                        _existing = await _read_task_status(
                            client, main_thread_id, sub_thread_id
                        )
                        if _existing == "cancelled":
                            async_tasks_written = True
                            _logger.info(
                                "[sync] 任务 %s 已被用户取消，保留 cancelled，不写入 %s",
                                sub_thread_id[:8], run_status,
                            )
                if not async_tasks_written:
                    try:
                        base = dict(task or {})
                        base["task_id"] = sub_thread_id
                        base["agent_name"] = agent_name
                        # 方案1：终态重写不得丢掉 failure_reported 标记（重启自愈/回退
                        # 重写 base 时从现有条目带过来，避免失败汇报重复触发）
                        try:
                            _prev = await _read_async_task(
                                client, main_thread_id, sub_thread_id
                            )
                            if isinstance(_prev, dict) and _prev.get("failure_reported"):
                                base["failure_reported"] = True
                        except Exception:  # noqa: BLE001
                            pass
                        # M-T5c：终态写不能丢掉 description（初始 task 字典无该字段；
                        # 2e 已 merge 进 state，这里从注册表/query_headers 再兜底一次）
                        if not base.get("description"):
                            base["description"] = _lookup_task_description(
                                sub_thread_id, query_header_entry
                            )
                        base["status"] = run_status  # "success" / "error" / "cancelled" / "timeout"
                        # 终态错误详情透传（方案2）：非 success 时取 run.error 截断写入，
                        # 供前端侧边栏展示具体失败原因（此前恒为 None，用户看不到任何原因）。
                        _err_text = await _terminal_error(
                            client, sub_thread_id, run_status
                        )
                        if _err_text:
                            base["error"] = _err_text
                        now_str = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                        base["last_checked_at"] = now_str
                        base["last_updated_at"] = now_str
                        await asyncio.to_thread(_sync_update_state, main_thread_id, {"async_tasks": {sub_thread_id: base}})
                        async_tasks_written = True
                        _logger.info(
                            "[sync] 写 async_tasks[%s] 状态=%s (重试 %d 次)",
                            sub_thread_id[:8], run_status, completion_write_retries,
                        )
                        # 追踪：记录子智能体完成事件
                        if _trace_store:
                            try:
                                _trace_store.insert_event_sync(
                                    thread_id=sub_thread_id,
                                    event_type=EventType.SUBAGENT_COMPLETE,
                                    agent_type="nl2sql_agent",
                                    parent_thread_id=main_thread_id,
                                    task_id=sub_thread_id,
                                    data={"status": run_status},
                                )
                            except Exception as _e:
                                _logger.debug("[sync] trace complete failed: %s", _e)
                    except Exception as e:
                        completion_write_retries += 1
                        # 限频告警：首次与每第 10 次失败记 WARNING，避免主线程长时间
                        # in-flight 时高频重试刷屏（成功时成功日志会带总重试次数）
                        if (
                            completion_write_retries == 1
                            or completion_write_retries % 10 == 0
                        ):
                            _logger.warning(
                                "[sync] 写 async_tasks[%s] 失败(第%d次,将重试): %s",
                                sub_thread_id[:8],
                                completion_write_retries,
                                str(e)[:100],
                            )
                        # 主线程忙时退避 2s，避免高频打 API；等待 in-flight run 结束
                        await asyncio.sleep(2)

                # 清除本任务运行标记（同样可能被 in-flight 拦截，跟随重试）
                if not active_queries_cleared:
                    try:
                        await asyncio.to_thread(_sync_update_state, main_thread_id, {"active_queries": {sub_thread_id: False}})
                        active_queries_cleared = True
                        _logger.info("[sync] 写 active_queries[%s]=false", sub_thread_id[:8])
                    except Exception as e:
                        _logger.warning(
                            "[sync] 写 active_queries[%s]=false 失败: %s",
                            sub_thread_id[:8], str(e)[:100],
                        )

                # ── 方案1：失败终态自动汇报主 agent（修断链 B）──
                # 终态 async_tasks（含方案2 错误详情）+ active_queries=false 都落地后，
                # 若子任务 error/timeout/cancelled，则等主线程空闲（≤30s）后 runs.create
                # 注入 [系统自动通知]，让主 agent 生成用户可读的失败消息。此前失败后无任何
                # 机制触发主 agent 转述 → 前端聊天区静默（只有侧边栏无详情的 ✕）。
                # 去重：state 的 failure_reported 标记（跨线程/重启）+ 本线程 local 标志。
                if (
                    async_tasks_written
                    and active_queries_cleared
                    and run_status in ("error", "timeout", "cancelled")
                    and not failure_reported_local
                ):
                    failure_reported_local = True  # 本线程只尝试一次（_maybe_report 内部 30s 等待）
                    await _maybe_report_failure(
                        client,
                        main_thread_id,
                        sub_thread_id,
                        agent_name,
                        run_status,
                    )

                # async_tasks + active_queries=false 都落地后，进入固定宽限期再退出
                if async_tasks_written and active_queries_cleared:
                    post_complete_cycles += 1
                    # 每 4 个周期（2s）检查一次终态是否被 auto-continue run 中断回退覆盖。
                    # 场景：sync 写入终态 → 前端触发 auto-continue → auto-continue run 被中断
                    # → LangGraph 回退到该 run 开始前的 checkpoint → 若 checkpoint 不含 sync
                    # 的写入（竞态窗口），终态丢失，UI 表现为进度条卡死。
                    if post_complete_cycles % 4 == 0:
                        try:
                            current_status = await _read_task_status(
                                client, main_thread_id, sub_thread_id
                            )
                            if current_status is not None and current_status not in _RUN_DONE_STATUSES:
                                _logger.warning(
                                    "[sync] 终态被回退！当前 async_tasks=%s，重新写入终态=%s",
                                    current_status, run_status,
                                )
                                async_tasks_written = False
                                active_queries_cleared = False
                        except Exception as _e:
                            _logger.warning("[sync] 终态回退检查失败: %s", _e)
                    if post_complete_cycles >= POST_COMPLETE_MAX_CYCLES:
                        _logger.info(
                            "[sync] 退出: cycles=%d", post_complete_cycles
                        )
                        break
                elif (time.monotonic() - completion_started_at) > COMPLETE_WRITE_MAX_SECONDS:
                    # 兜底：主线程长时间 in-flight（如长查询）时放弃重试，避免僵尸线程。
                    # 前端会因 active_queries 仍 true 持续轮询，但 async_tasks 缺失时仍不自动续跑；
                    # 这是极端场景的降级（至少不占线程）。
                    _logger.error(
                        "[sync] 完成写入超过 %ds 仍未成功(主线程持续 in-flight?)，放弃: %s",
                        COMPLETE_WRITE_MAX_SECONDS, sub_thread_id[:8],
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


async def _get_latest_run(client, thread_id: str) -> Optional[dict]:
    """获取线程上最新 run（含 run_id，P1-3 恢复后刷新 async_tasks.run_id 用）。"""
    try:
        runs = await client.runs.list(thread_id=thread_id, limit=1)
        if not runs:
            return None
        return runs[0]
    except Exception as e:
        _logger.warning("[sync] get_latest_run failed: %s", e)
        return None


async def _terminal_error(client, thread_id: str, run_status: str) -> Optional[str]:
    """取终态 run 的错误详情（压平空白 + 截断 500），供 async_tasks 终态写入。

    success / 取不到错误 / 异常时返回 None（不阻塞终态写入）。
    方案2（2026-09-01）：此前 async_tasks 终态恒不带 error，前端侧边栏只能看到
    「执行失败」看不到具体原因；此处把 run["error"]（如 APITimeoutError: Request timed out.）
    透传出去。
    """
    if run_status == "success":
        return None
    try:
        run = await _get_latest_run(client, thread_id)
        raw = (run or {}).get("error") or None
        if not raw:
            return None
        return " ".join(str(raw).split())[:500]
    except Exception:  # noqa: BLE001
        return None


async def _extract_approval_payload(client, sub_thread_id: str) -> Optional[dict]:
    """子线程处于 interrupt 等待时，提取 HITL 审批 payload（P1-3）。

    返回 {"action_requests", "review_configs", "interrupt_id"}；
    非审批类 interrupt（无 action_requests）返回 None。
    """
    try:
        state = await client.threads.get_state(thread_id=sub_thread_id)
        for task in state.get("tasks") or []:
            for intr in task.get("interrupts") or []:
                value = intr.get("value")
                if isinstance(value, dict) and value.get("action_requests"):
                    return {
                        "action_requests": value.get("action_requests"),
                        "review_configs": value.get("review_configs"),
                        "interrupt_id": intr.get("id"),
                    }
        return None
    except Exception as e:
        _logger.warning("[sync] extract_approval_payload failed: %s", e)
        return None


async def _read_task_status(
    client, main_thread_id: str, task_id: str
) -> Optional[str]:
    """读主线程 state 中 async_tasks[task_id] 的当前状态（P1-7 取消善后）。

    用于在 sync 写入终态前判断：该任务是否已被用户取消（cancelled）。
    """
    try:
        state = await client.threads.get_state(thread_id=main_thread_id)
        tasks = (state.get("values") or {}).get("async_tasks") or {}
        entry = tasks.get(task_id)
        if isinstance(entry, dict):
            return entry.get("status")
        return None
    except Exception as e:
        _logger.warning("[sync] read_task_status failed: %s", e)
        return None


def _lookup_task_description(
    task_id: str, query_header_entry: Optional[dict] = None
) -> str:
    """按 task_id 找任务描述（M-T5c）。

    优先级：
    1. 进程级任务注册表（派发时 _wrap_runs_create 登记，含【任务目标】完整描述）；
    2. query_headers 条目（watcher 已写入 state，去 📋 前缀）。
    注册表在跨进程/重启后会丢失，query_headers 持久在 state 里，作兜底。
    """
    desc = ""
    try:
        from agent.trace.langfuse_client import get_task_trace_context
        desc = str(get_task_trace_context(task_id)[4] or "")
    except Exception:
        pass
    if not desc and query_header_entry:
        content = str(query_header_entry.get("content", "") or "")
        if content.startswith("📋 "):
            desc = content[2:]
    return desc


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
    """从子智能体线程提取最新 todos（含每步耗时）。

    2026-09-04（层2 监督机配套）改权威源：**优先读子线程 state 的 `todos` 通道**——
    它由每次 write_todos（模型自发）与 ProgressBoundaryMiddleware（确定性 after_model 推进）
    共同更新；且 todos 通道独立于 messages，不随 auto-compress 剪消息而丢失。
    messages 反扫（写 AI tool_calls / ToolMessage）仅作 state.todos 为空时的兜底。
    """
    try:
        state = await client.threads.get_state(thread_id=sub_thread_id)
        values = state.get("values") or {}
        messages = values.get("messages", [])

        def _render(todo_list: list) -> list:
            """把 todos 渲染成 [{content, status}]，并按本地进度文件附耗时后缀。"""
            items = []
            for t in todo_list:
                if not isinstance(t, dict):
                    continue
                content = str(t.get("content", "") or "").strip()
                if not content:
                    continue
                items.append({
                    "content": content,
                    "status": t.get("status", "pending"),
                })
            if not items:
                return []
            completed_steps = {
                it["content"] for it in items if it["status"] == "completed"
            }
            # 传入 completed_steps：已完成的 step 强制固定耗时（不带 "..."），
            # 避免进度文件缺 completed 事件时误输出 "Xs..."（已完成还在计时）。
            durations = _compute_step_durations(sub_thread_id, completed_steps)

            def _with_duration(content: str) -> str:
                dur = durations.get(content, "")
                return f"{content} ({dur})" if dur else content

            return [
                {"content": _with_duration(it["content"]), "status": it["status"]}
                for it in items
            ]

        # 权威源：state.todos（模型 write_todos + 监督机确定性更新都落这里）
        raw_todos = values.get("todos") or []
        if isinstance(raw_todos, list) and raw_todos:
            rendered = _render(raw_todos)
            if rendered:
                return rendered

        # 兜底：反扫 messages 找最后一次 write_todos（state.todos 为空 / 旧存档线程）
        def _with_duration_fb(content: str) -> str:
            dur = _compute_step_durations(sub_thread_id).get(content, "")
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
                                    "content": _with_duration_fb(t.get("content", "")),
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
                            {"content": _with_duration_fb(c), "status": s}
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
            "请评估数据特征和用户意图：若数据有可视化价值或用户要求图表，"
            "则推荐并渲染图表；若数据适合报告或用户要求报告，则生成分析报告。"
            "简单查询结果直接展示即可，不要等待用户指示。"
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


async def _read_async_task(
    client, main_thread_id: str, sub_thread_id: str
) -> Optional[dict]:
    """读主线程 state 里 async_tasks[sub_thread_id] 条目（不存在/异常返回 None）。"""
    try:
        st = await client.threads.get_state(thread_id=main_thread_id)
        tasks = ((st or {}).get("values") or {}).get("async_tasks") or {}
        entry = tasks.get(sub_thread_id)
        return entry if isinstance(entry, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def _report_subagent_failure(
    client,
    main_thread_id: str,
    sub_thread_id: str,
    agent_name: str,
    run_status: str,
) -> bool:
    """子任务失败终态自动汇报主 agent（方案1，修断链 B）。

    error/timeout/cancelled 终态下：等主线程当前 run 结束（≤30s，用户对话中不打扰），
    然后 runs.create 注入一条 [系统自动通知] 失败说明，让主 agent 生成用户可读的
    失败消息（此前失败后无任何机制触发主 agent 转述 → 前端聊天区静默）。

    返回是否成功触发续跑（成功由调用方写 state 的 failure_reported 去重标记；
    失败/主线程忙返回 False，静默降级不级联）。
    """
    # 终态标签（error/timeout 用失败语义；cancelled 是用户主动取消 → 确认回执）
    _label = {
        "error": "执行失败",
        "timeout": "执行超时",
        "cancelled": "已被取消",
    }.get(run_status, "执行失败")

    try:
        # 1. 等主线程当前 run 结束（最多 30s）。无 run / 已是终态即认为空闲。
        #    子任务失败时主线程最新 run 通常是 launch run（success），立即通过；
        #    仅当用户正在别的对话/查询时才会等待。
        idle = False
        for _i in range(15):
            runs = await client.runs.list(thread_id=main_thread_id, limit=1)
            if not runs:
                idle = True
                break
            if runs[0].get("status") in (
                "success", "error", "cancelled", "timeout", "interrupted",
            ):
                idle = True
                break
            await asyncio.sleep(2)
        if not idle:
            _logger.info(
                "[sync] 方案1: 主线程 30s 内未空闲，跳过失败汇报 sub=%s status=%s",
                sub_thread_id[:8], run_status,
            )
            return False

        # 2. 注入失败通知并续跑。消息带 [系统自动通知] 前缀：
        #    - langfuse_metadata._extract_question_summary 跳过 [系统 → 不污染 user_question
        #    - _extract_subagent_todos / message_feedback 也过滤 [系统 → 不会被当新问题/新查询
        #    错误详情来自方案2 的 async_tasks[task].error（run.error 压平+截断 500）。
        _err = ""
        try:
            _entry = await _read_async_task(
                client, main_thread_id, sub_thread_id
            )
            _err = ((_entry or {}).get("error") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        content = (
            f"[系统自动通知] 子任务 {agent_name} {_label}"
            + (f"：{_err}。" if _err else "。")
            + "请向用户说明失败原因与建议。"
        )
        run = await client.runs.create(
            thread_id=main_thread_id,
            assistant_id="chat_agent",
            input={"messages": [{"role": "user", "content": content}]},
            config={"recursion_limit": 500},
        )
        _rid = run.get("run_id", "unknown") if isinstance(run, dict) else run
        _logger.info(
            "[sync] 方案1: 失败汇报续跑已创建 run=%s sub=%s status=%s",
            _rid, sub_thread_id[:8], run_status,
        )
        return True
    except Exception as e:  # noqa: BLE001
        _logger.error(
            "[sync] 方案1: 失败汇报续跑失败: %s", str(e)[:200]
        )
        return False


async def _maybe_report_failure(
    client,
    main_thread_id: str,
    sub_thread_id: str,
    agent_name: str,
    run_status: str,
) -> None:
    """方案1 入口：state 标记去重（跨线程/重启）→ 触发汇报 → 成功则写 failure_reported。"""
    # 1. 已有标记（此前已汇报）→ 跳过
    try:
        _entry = await _read_async_task(
            client, main_thread_id, sub_thread_id
        )
        if isinstance(_entry, dict) and _entry.get("failure_reported"):
            _logger.debug(
                "[sync] 方案1: %s 已汇报过，跳过", sub_thread_id[:8]
            )
            return
    except Exception:  # noqa: BLE001
        pass

    # 2. 触发（内部等主线程空闲 ≤30s；失败静默降级）
    _ok = await _report_subagent_failure(
        client, main_thread_id, sub_thread_id, agent_name, run_status
    )

    # 3. 成功 → 写 failure_reported 标记（best-effort，读-改-写复用锁）
    if _ok:
        try:
            _entry = await _read_async_task(
                client, main_thread_id, sub_thread_id
            )
            _merged = dict(_entry or {})
            _merged["task_id"] = sub_thread_id
            _merged["failure_reported"] = True
            await asyncio.to_thread(
                _sync_update_state,
                main_thread_id,
                {"async_tasks": {sub_thread_id: _merged}},
            )
            _logger.info(
                "[sync] 方案1: %s failure_reported 已标记", sub_thread_id[:8]
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "[sync] 方案1: 写 failure_reported 失败: %s", str(e)[:100]
            )


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
