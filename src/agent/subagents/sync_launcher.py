"""Monkey-patch start_async_task，在创建子智能体后自动启动 todos 同步。

必须在 create_deep_agent 之前导入（与 check_progress.py 相同模式）。
非阻塞模式：start_async_task 立即返回，同步进程在后台更新进度条。
"""
import logging
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg

_logger = logging.getLogger(__name__)

_PATCHED = False


def apply_patch():
    """Monkey-patch deepagents _build_start_tool。幂等。"""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        from deepagents.middleware import async_subagents as _mod
    except ImportError:
        _logger.warning("[sync_launcher] deepagents not found, skip patch")
        return

    _orig_build_start_tool = _mod._build_start_tool

    def _patched_build_start_tool(agent_map, clients, tool_description):
        """包装 _build_start_tool：启动同步进程。"""
        tool = _orig_build_start_tool(agent_map, clients, tool_description)

        _orig_sync = tool.func
        _orig_async = tool.coroutine

        def _maybe_launch_sync(result, runtime):
            """从 start_async_task 返回值中提取子智能体信息并启动同步。"""
            try:
                from langgraph.types import Command

                if not isinstance(result, Command):
                    return

                update = getattr(result, "update", None) or {}
                async_tasks = update.get("async_tasks", {})

                config = getattr(runtime, "config", None) or {}
                configurable = config.get("configurable", {})
                main_thread_id = configurable.get("thread_id")

                if not main_thread_id:
                    _logger.warning(
                        "[sync_launcher] 无法获取主智能体 thread_id"
                    )
                    return

                for task_id, task_info in async_tasks.items():
                    sub_thread_id = task_info.get("thread_id", task_id)
                    agent_name = task_info.get("agent_name", "unknown")

                    from agent.subagents.sync_subagent_todos import launch_sync

                    # 传入完整 AsyncTask 字典（含 run_id/created_at），
                    # 供 sync 线程写 async_tasks 单 key 时使用（避免读-改-写竞态）
                    launch_sync(main_thread_id, sub_thread_id, agent_name, task_info)
                    _logger.info(
                        "[sync_launcher] 已启动同步: %s → %s (%s)",
                        main_thread_id[:8],
                        sub_thread_id[:8],
                        agent_name,
                    )

            except Exception as e:
                _logger.warning(
                    "[sync_launcher] 启动同步失败: %s", e, exc_info=True
                )

        def _wrap_sync(
            description: str,
            subagent_type: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
        ):
            result = _orig_sync(description, subagent_type, runtime)
            _maybe_launch_sync(result, runtime)
            return result

        async def _wrap_async(
            description: str,
            subagent_type: str,
            runtime: Annotated[ToolRuntime, InjectedToolArg()],
        ):
            result = await _orig_async(description, subagent_type, runtime)
            _maybe_launch_sync(result, runtime)
            return result

        tool.func = _wrap_sync
        tool.coroutine = _wrap_async
        return tool

    _mod._build_start_tool = _patched_build_start_tool
    _logger.info("[sync_launcher] patched _build_start_tool (non-blocking)")


# 模块加载时自动打补丁
apply_patch()
