"""langgraph API 自定义 app 组合根（注册表）。

被 `LANGGRAPH_HTTP.app` 钩子加载（见 .env），与 langgraph 原生路由合并为同一
Starlette app、同一进程/端口（2026），不改 langgraph-api 源码。

新增自定义 API：在 `src/api/` 建模块暴露 `routes: list[BaseRoute]`，
并在下方 `ROUTES` 里展开一行即可（组合根模式，避免堆进单个文件）。
"""
import logging
import os
import sys
from contextlib import asynccontextmanager  # noqa: E402

# 防御性插 src 进 sys.path：start_server.py 已插；此处兜底 langgraph dev 等
# 未主动插 src 的启动方式，保证 api/mcp_server 包可导入。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.routing import BaseRoute  # noqa: E402

_logger = logging.getLogger(__name__)

import api.auto_title  # noqa: E402
import api.db_config  # noqa: E402
import api.langfuse_metadata  # noqa: E402
import api.message_feedback  # noqa: E402
import api.model_config  # noqa: E402
import api.report_file  # noqa: E402
import api.sql_approval  # noqa: E402
import api.task_cancel  # noqa: E402
import api.thread_compact  # noqa: E402
import api.thread_export  # noqa: E402
import api.thread_fork  # noqa: E402
import api.thread_search  # noqa: E402
import api.workspace  # noqa: E402
import api.wren_semantic  # noqa: E402
import api.trace_routes  # noqa: E402
import api.feedback_stats  # noqa: E402
import api.feedback_annotation  # noqa: E402
import api.experiment  # noqa: E402

ROUTES: list[BaseRoute] = [
    *api.db_config.routes,
    *api.message_feedback.routes,
    *api.auto_title.routes,
    *api.model_config.routes,
    *api.report_file.routes,
    *api.sql_approval.routes,
    *api.task_cancel.routes,
    *api.thread_compact.routes,
    *api.thread_export.routes,
    *api.thread_fork.routes,
    *api.thread_search.routes,
    *api.workspace.routes,
    *api.wren_semantic.routes,
    *api.trace_routes.routes,
    *api.feedback_stats.routes,
    *api.feedback_annotation.routes,
    *api.experiment.routes,
    # 后期新增：import api.<name> + 展开 *api.<name>.routes
]

# M2 监控增强：langgraph server 会提取 custom_app 的 user_middleware 全局应用
# （langgraph_api/server.py），对 run 创建端点注入 Langfuse config.metadata。
# 纯 ASGI 中间件，不缓冲响应（不破坏 /runs/stream 的 SSE）。


@asynccontextmanager
async def _lifespan(app: "Starlette"):
    """自定义 app 生命周期——langgraph server 会把它并入进程停机路径
    （langgraph_api/server.py combine_lifespans，shutdown 逆序执行）。

    此处只挂 **停机 flush**（P0 评估可靠交付，设计文档 §8.2）：显式刷新 Langfuse
    SDK 上送队列，避免进程退出丢最近窗口的分数/trace。不重复任何启动逻辑
    （base runtime 的 lifespan 由 server 自行管理）。flush 不抛异常、不阻断停机。
    """
    yield
    try:
        from agent.trace.langfuse_client import flush_langfuse  # 惰性 import

        flush_langfuse()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 停机 flush Langfuse 失败: %s", e)


app = Starlette(
    routes=ROUTES,
    middleware=[Middleware(api.langfuse_metadata.LangfuseMetadataMiddleware)],
    lifespan=_lifespan,
)
