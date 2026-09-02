"""ThinkingToggleMiddleware — 前端单开关控制模型真实思考 + 模型路由（P1-9）。

需求：前端一个「开启思考过程」开关，开 → 后端真开启思考（reasoning_content 流出、前端显示）；
关 → 后端真关闭思考（无 reasoning_content、前端自然不显示）。
P1-9 扩展：configurable.llm_route 指定模型配置 store 中的 provider，
每次模型调用按 route 重建模型（前端 composer 切模型免重启）。

机制：
- 前端把 enable_thinking（"true"/"false"）随 run 传入 configurable.enable_thinking
  （与 query_keywords / db_name 同一通道，已实证可到达）。
- 本中间件在 wrap_model_call 时读 configurable，用 create_model(enable_thinking=...)
  重建带正确思考开关的模型实例，并 override request.model。
- langchain 在替换之后才执行 request.model.bind_tools(...)（factory.py _execute_model_sync 内
  从 request.model 派生最终调用模型），因此工具绑定/响应格式等不受影响。
- configurable 缺失时返回 None → 不替换，走 import 时的默认模型（两个模型默认思考开）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")


class ThinkingToggleMiddleware(AgentMiddleware):
    """按 configurable.enable_thinking 重建带思考开关的模型。

    进程级缓存：同 (enable_thinking, route, model_name) 三元组复用已创建的模型实例，
    避免 ChatDeepSeek 构造 ~8.2s 重复开销。model_config.json 变更时自动失效。

    auto-continue 继承（P1-10）：同一 thread 的后续 auto-continue run 通常不携带
    llm_route/llm_model/enable_thinking（前端续跑只发系统通知消息，不传 configurable）。
    本中间件记录每个 thread 最后一次显式配置的 cache_key，auto-continue 缺少配置时
    自动复用同一 thread 上次的模型实例，保证整会话使用同一模型。
    """

    # ── 进程级模型实例缓存 ──────────────────────────────────
    _model_cache: dict[tuple, Any] = {}      # (enable, route, model_name) → model 实例
    _model_cache_mtime: float = 0.0          # model_config.json 最后修改时间
    _MAX_CACHE_SIZE = 8                      # 最大缓存条目（防内存泄漏）

    # ── auto-continue 模型继承（per-thread）────────────────────
    _last_thread_key: dict[str, tuple] = {}  # thread_id → 最近一次显式配置的 cache_key
    _MAX_THREAD_KEYS = 200                   # 最大跟踪线程数（防内存泄漏）

    # ── 缓存管理 ──────────────────────────────────────────────

    @staticmethod
    def _model_config_mtime() -> float:
        """获取 model_config.json 的 mtime，用于缓存失效检测。"""
        try:
            from agent.workspace_manager import get_workspace_manager
            path = get_workspace_manager().model_config_path
            return path.stat().st_mtime if path.exists() else 0.0
        except Exception:
            return 0.0

    def _cache_key(
        self, enable: Optional[bool], route: Optional[str], model_name: Optional[str]
    ) -> tuple:
        return (enable, route or "", model_name or "")

    def _cache_get(self, key: tuple) -> Any | None:
        """命中缓存返回模型实例；配置已变更则清空缓存返回 None。"""
        mtime = self._model_config_mtime()
        if mtime != self._model_cache_mtime:
            self._model_cache.clear()
            self._model_cache_mtime = mtime
            return None
        return self._model_cache.get(key)

    def _cache_set(self, key: tuple, model: Any) -> None:
        """写入缓存；超过最大条目时淘汰最旧的（FIFO）。"""
        if len(self._model_cache) >= self._MAX_CACHE_SIZE:
            oldest = next(iter(self._model_cache))
            del self._model_cache[oldest]
        self._model_cache[key] = model
        if self._model_cache_mtime == 0.0:
            self._model_cache_mtime = self._model_config_mtime()

    # ── 工具方法 ──────────────────────────────────────────────

    @staticmethod
    def _get_thread_id() -> str | None:
        """从 langgraph config 取当前 thread_id（用于 auto-continue 模型继承）。"""
        try:
            from langgraph.config import get_config as _cfg
            if _cfg is not None:
                cfg = _cfg()
                tid = cfg.get("configurable", {}).get("thread_id")
                return str(tid) if tid else None
        except Exception:  # noqa: BLE001
            pass
        return None

    def _resolve_overrides(
        self, request: ModelRequest[ContextT]
    ) -> tuple[Optional[bool], Optional[str], Optional[str]]:
        """从运行时 config 读前端传入的 (enable_thinking, llm_route, llm_model)；缺失返回 None。

        注：与 QueryKeywordsMiddleware 同路径——request.runtime.config 恒为空，
        必须走 langgraph.config.get_config()（实证 2026-08-14）。
        """
        configurable = {}
        try:
            from langgraph.config import get_config as _cfg
            if _cfg is not None:
                configurable = (_cfg().get("configurable", {}) or {})
        except Exception:  # noqa: BLE001
            pass
        if not configurable:
            try:
                runtime = getattr(request, "runtime", None)
                config = getattr(runtime, "config", None) or {}
                configurable = config.get("configurable") or {}
            except Exception:  # noqa: BLE001
                configurable = {}
        val = configurable.get("enable_thinking")
        enable = None if val is None else str(val).lower() in ("true", "1", "yes", "on")
        route = configurable.get("llm_route")
        route = str(route) if route else None
        model_name = configurable.get("llm_model")
        model_name = str(model_name) if model_name else None
        return enable, route, model_name

    def _maybe_swap(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """按思考开关 / 模型路由 / 指定模型重建模型并替换 request.model；都缺失时原样返回。

        P1-9：llm_route 指向模型配置 store 中的 provider，llm_model 指定该 provider
        下的具体模型（前端 composer 逐模型选择），每次模型调用重建 → 切模型免重启后端。

        进程级缓存：同 (enable_thinking, route, model_name) 三元组命中时直接复用已创建的
        模型实例（~0s），避免 ChatDeepSeek 构造 ~8.2s 开销。model_config.json 变更时自动清空。

        P1-10 auto-continue 继承：同一 thread 的后续 run（如异步子智能体完成后前端自动
        续跑）不携带 llm_route/llm_model/enable_thinking 时，复用该 thread 上次显式配置
        的模型实例。避免续跑回退到模块级默认模型（可能已欠费/不可用）。
        """
        enable, route, model_name = self._resolve_overrides(request)
        thread_id = self._get_thread_id()

        if enable is None and not route and not model_name:
            # ── auto-continue 继承：复用同 thread 上次的模型 ──
            if thread_id and thread_id in self._last_thread_key:
                prev_key = self._last_thread_key[thread_id]
                cached = self._cache_get(prev_key)
                if cached is not None:
                    _logger.debug(
                        "[ThinkingToggle] auto-continue 继承 thread=%s 上次模型 "
                        "enable=%s, route=%s, model=%s",
                        thread_id[:8], prev_key[0], prev_key[1], prev_key[2],
                    )
                    return request.override(model=cached)
            return request

        key = self._cache_key(enable, route, model_name)
        cached = self._cache_get(key)
        if cached is not None:
            _logger.debug(
                "[ThinkingToggle] 缓存命中 enable_thinking=%s, route=%s, model=%s",
                enable, route, model_name,
            )
            # 记录该 thread 的显式配置（供后续 auto-continue 继承）
            if thread_id:
                self._record_thread_key(thread_id, key)
            return request.override(model=cached)

        from agent.llms.model import create_model

        model = create_model(enable_thinking=enable, route=route, model_name=model_name)
        if model is None:
            return request
        self._cache_set(key, model)
        # 记录该 thread 的显式配置（供后续 auto-continue 继承）
        if thread_id:
            self._record_thread_key(thread_id, key)
        _logger.info(
            "[ThinkingToggle] 新建并缓存模型 enable_thinking=%s, route=%s, model=%s, "
            "缓存条目=%d/%d",
            enable, route, model_name, len(self._model_cache), self._MAX_CACHE_SIZE,
        )
        return request.override(model=model)

    def _record_thread_key(self, thread_id: str, key: tuple) -> None:
        """记录 thread 最新显式配置的 cache_key；超上限时淘汰最旧条目。"""
        if thread_id not in self._last_thread_key:
            if len(self._last_thread_key) >= self._MAX_THREAD_KEYS:
                oldest = next(iter(self._last_thread_key))
                del self._last_thread_key[oldest]
        self._last_thread_key[thread_id] = key

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：按开关替换模型后调用 handler。"""
        modified = self._maybe_swap(request)
        return handler(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用：按开关替换模型后调用 handler。"""
        modified = self._maybe_swap(request)
        result = handler(modified)
        if hasattr(result, "__await__"):
            return await result
        return result
