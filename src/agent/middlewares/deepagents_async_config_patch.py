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
        metadata["langfuse_trace_name"] = f"nl2sql-agent:{parent_tid}"
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
    return metadata


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
