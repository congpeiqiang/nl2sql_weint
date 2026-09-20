"""Monkey-patch deepagents 异步子 agent 的 runs.create，透传父 run 的 configurable。

背景：
- 前端通过 `stream.submit({messages}, {config: {configurable: {...}}})` 传入
  db_name / query_keywords 等配置，主 agent 能读到（`get_config().configurable`）。
- 但 deepagents `AsyncSubAgent` 在创建子 agent run 时（`client.runs.create`）
  未传 `config` → 子 agent run 的 `configurable` 为空，
  `_inject_db_name`（`path_resolver.py`）读不到 db_name，前端选库到不了子 agent。

本 patch 在 `_ClientCache.get_sync` / `get_async` 返回的 client 上包装 `runs.create`，
把当前 run（主 agent）的 configurable 注入 `config` 参数，随子 agent run 一起下发。

必须在 `create_deep_agent` 之前导入（与 sync_launcher.py 相同模式）。
"""
import logging

_logger = logging.getLogger(__name__)

_PATCHED = False

# 不随 configurable 透传的键：这些属于主线程 run 状态，转发会让子 run 混乱
_EXCLUDED_KEYS = ("thread_id", "checkpoint_id", "checkpoint_ns")


def _is_internal_key(key: str) -> bool:
    """判断是否为 langgraph 运行时内部注入键。

    真实运行时 get_config().configurable 会混入大量 __pregel_* 运行时对象
    （__pregel_node_finished 是函数、__pregel_runtime/read/call 是 Runtime/partial 等），
    全部不可 JSON 序列化——原样注入 client.runs.create 会报
    "Type is not JSON serializable: function"。故只透传用户自定义键
    （db_name / query_keywords 等），丢弃 __ 前缀与 langgraph_ 前缀的内部键。
    """
    if key in _EXCLUDED_KEYS:
        return True
    if key.startswith("__"):
        return True
    if key.startswith("langgraph_"):
        return True
    return False


def _current_configurable() -> dict:
    """读取当前 run（主 agent）的 configurable，过滤运行时内部键。"""
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        configurable = cfg.get("configurable", {}) or {}
        result = {k: v for k, v in configurable.items() if not _is_internal_key(k)}
        # 注入追踪上下文：父 thread_id（子 agent 用于建立会话谱系）
        parent_thread_id = configurable.get("thread_id", "")
        if parent_thread_id:
            result["trace_parent_thread_id"] = parent_thread_id
        return result
    except Exception:
        return {}


def _current_parent_thread_id() -> str:
    """读取当前 run（主 agent）的真实 thread_id（在过滤内部键之前取）。"""
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        return str((cfg.get("configurable", {}) or {}).get("thread_id", ""))
    except Exception:
        return ""


def _current_user_question() -> str:
    """读取当前 run 的 user_question（HTTP 中间件注入的 metadata，≤200 字符）。

    LANGFUSE_ENABLE=false 时中间件不注入 → 返回空（M-T5 注册表 question 兜底为空，
    不影响 trace 路由，仅用于日志/评估展示）。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        return str((cfg.get("metadata", {}) or {}).get("user_question", "") or "")
    except Exception:
        return ""


def _current_otel_trace_id() -> str:
    """读取当前 agent run 的 Langfuse trace_id（M-T2 方案 B）。

    deepagents 异步工作线程丢失 OTel context（已验证），无法用
    opentelemetry.trace.get_current_span() 获取。改用 CallbackHandler
    单例的 last_trace_id 属性——on_chain_start 时写入，跨线程可读。

    并发场景下 last_trace_id 可能被其他请求覆盖（竞态窗口极小：
    工具调用在 on_chain_start 之后立即触发），生产环境若并发高可改
    为 per-run 存储（如 run_id → trace_id dict）。
    """
    # 优先：OTel context 可用时直接读（主 agent 线程同步调用场景）
    try:
        from opentelemetry import trace as otel_trace
        current_span = otel_trace.get_current_span()
        ctx = current_span.get_span_context() if current_span else None
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    # 兜底：从 CallbackHandler 单例读 last_trace_id（跨线程可访问）
    try:
        from agent.trace.langfuse_client import get_langfuse_handler
        handler = get_langfuse_handler()
        if handler is not None:
            tid = getattr(handler, "last_trace_id", None)
            if tid:
                return str(tid)
    except Exception:
        pass
    return ""


def _build_langfuse_metadata() -> dict:
    """构建子 run 的 Langfuse 元数据（M2：session_id=thread_id 兜底分组）。

    主 run 的请求级 metadata 由 LangfuseMetadataMiddleware 在 HTTP 层注入；
    子 run 由 deepagents 在进程内创建（不走 HTTP），故在此补注入同样的
    session/trace_name/tags + workspace/skills，让主、子 trace 在同一 session 分组。
    """
    parent_tid = _current_parent_thread_id()
    metadata: dict = {}
    if parent_tid:
        metadata["langfuse_session_id"] = parent_tid
        # 低基数稳定名（官方 best-practices：name 不含动态值）
        metadata["langfuse_trace_name"] = "nl2sql-agent"
        metadata["langfuse_tags"] = ["nl2sql"]
    try:
        from agent.workspace_manager import get_workspace_manager
        wm = get_workspace_manager()
        metadata.setdefault("workspace", {
            "name": wm.active_name,
            "path": str(wm.active_workspace),
        })
    except Exception:
        pass
    try:
        # M6：与主 run 的 LangfuseMetadataMiddleware 一致，用 Langfuse 版本解析后的清单
        from agent.trace.skill_manifest import get_enriched_skill_manifest
        metadata.setdefault("skills", get_enriched_skill_manifest())
    except Exception:
        pass
    # 问题文本透传：主 run 注入的 user_question → 子 run 也能标注中间产物归属问题
    # （langfuse_span._dump_process_data 用它 + langfuse_parent_trace_id 区分同会话多问题）
    try:
        _q = _current_user_question()
        if _q:
            metadata["user_question"] = _q
    except Exception:
        pass
    return metadata


def _current_root_obs_id(trace_id: str) -> str:
    """从 _ROOT_OBS_MAP 读取当前 trace 的 root observation ID（M-T3b 补充）。

    必须在 orig_create 之前调用：runs.create 同步执行时，子 agent 的
    on_chain_start 会覆盖 _ROOT_OBS_MAP（主/子 agent 共享 trace_id），
    导致后续读取拿到子 agent 的 obs_id 而非主 agent 的。
    """
    if not trace_id:
        return ""
    try:
        from agent.trace.langfuse_client import get_root_observation_id
        return get_root_observation_id(trace_id)
    except Exception:
        return ""


def _wrap_runs_create(orig_create):
    """包装 client.runs.create：未显式传 config 时注入当前 configurable + Langfuse 元数据。"""
    import functools

    @functools.wraps(orig_create)
    def _patched(*args, **kwargs):
        if "config" not in kwargs:
            configurable = _current_configurable()
            if configurable:
                cfg = {"configurable": configurable}
                meta = _build_langfuse_metadata()
                # M-T2：抓主线程 OTel trace_id → 注入 metadata → 子线程的 skill span
                # 用 trace_context=TraceContext(trace_id=...) 嵌套到主 trace 下
                #
                # ⚠ 关键时序：必须在 orig_create 之前捕获！
                # runs.create 可能同步执行子 agent → 子 agent 的 on_chain_start
                # 会覆盖 handler.last_trace_id 和 _ROOT_OBS_MAP（因为 M-T3 让
                # 主/子 agent 共享 trace_id），导致后续读取拿到错误值。
                parent_trace_id = _current_otel_trace_id()
                parent_obs_id = ""
                if parent_trace_id:
                    meta["langfuse_parent_trace_id"] = parent_trace_id
                    parent_obs_id = _current_root_obs_id(parent_trace_id)
                    if parent_obs_id:
                        meta["langfuse_parent_obs_id"] = parent_obs_id
                    _logger.info(
                        "[db_config] M-T2/T3b: injected parent_trace_id=%s "
                        "parent_obs_id=%s",
                        parent_trace_id[:16], parent_obs_id[:16] if parent_obs_id else "(none)",
                    )
                # M-T5：派发异步子任务时登记 task_id → 所属查询的 trace 上下文，
                # 供 auto-continue 按任务路由回原 trace（连问场景归属修正）。
                # task_id = 子任务的独立 LangGraph thread_id（kwargs["thread_id"]）；
                # update_async_task 重派发带 multitask_strategy="interrupt"，跳过，
                # 保护原绑定不被覆盖。
                _task_id = str(kwargs.get("thread_id") or "")
                _is_redispatch = bool(kwargs.get("multitask_strategy"))
                if _task_id and not _is_redispatch:
                    _desc = ""
                    try:
                        _input = kwargs.get("input") or {}
                        _msgs = _input.get("messages") or []
                        if _msgs:
                            _desc = str(_msgs[0].get("content", "") or "")[:500]
                    except Exception:
                        pass
                    try:
                        from agent.trace.langfuse_client import register_task_trace_context
                        register_task_trace_context(
                            task_id=_task_id,
                            main_thread_id=_current_parent_thread_id(),
                            trace_id=parent_trace_id,
                            root_obs_id=parent_obs_id,
                            question=_current_user_question(),
                            description=_desc,
                        )
                        if not _desc:
                            _logger.warning(
                                "[langfuse_m5] 派发 task=%s 描述为空", _task_id[:12]
                            )
                    except Exception as e:  # noqa: BLE001
                        _logger.debug("[langfuse_m5] 登记 task 失败: %s", e)
                if meta:
                    cfg["metadata"] = meta
                kwargs = dict(kwargs)
                kwargs["config"] = cfg
        return orig_create(*args, **kwargs)

    return _patched


def _wrap_client_getter(orig_getter):
    """包装 _ClientCache.get_sync/get_async：给返回的 client 包上 runs.create。"""
    import functools

    @functools.wraps(orig_getter)
    def _patched(self, name):
        client = orig_getter(self, name)
        try:
            if hasattr(client, "runs") and not getattr(client.runs, "_db_config_patched", False):
                client.runs.create = _wrap_runs_create(client.runs.create)
                client.runs._db_config_patched = True
        except Exception as e:
            _logger.warning("[db_config] 包装 client.runs.create 失败: %s", e)
        return client

    return _patched


def apply_patch():
    """Monkey-patch _ClientCache.get_sync/get_async。幂等。"""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        from deepagents.middleware import async_subagents as _mod
    except ImportError:
        _logger.warning("[db_config] deepagents not found, skip config patch")
        return

    _mod._ClientCache.get_sync = _wrap_client_getter(_mod._ClientCache.get_sync)
    _mod._ClientCache.get_async = _wrap_client_getter(_mod._ClientCache.get_async)
    _logger.info("[db_config] patched _ClientCache → 透传 configurable 到异步子 agent run")


# 模块加载时自动打补丁
apply_patch()
