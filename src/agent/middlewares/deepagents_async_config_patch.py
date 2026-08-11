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


def _current_configurable() -> dict:
    """读取当前 run（主 agent）的 configurable，过滤掉线程状态键。"""
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        configurable = cfg.get("configurable", {}) or {}
        return {k: v for k, v in configurable.items() if k not in _EXCLUDED_KEYS}
    except Exception:
        return {}


def _wrap_runs_create(orig_create):
    """包装 client.runs.create：未显式传 config 时注入当前 configurable。"""
    import functools

    @functools.wraps(orig_create)
    def _patched(*args, **kwargs):
        if "config" not in kwargs:
            configurable = _current_configurable()
            if configurable:
                kwargs = dict(kwargs)
                kwargs["config"] = {"configurable": configurable}
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
