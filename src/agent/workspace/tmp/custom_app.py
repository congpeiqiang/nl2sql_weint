"""langgraph API 自定义 app 组合根（注册表）。

被 `LANGGRAPH_HTTP.app` 钩子加载（见 .env），与 langgraph 原生路由合并为同一
Starlette app、同一进程/端口（2026），不改 langgraph-api 源码。

新增自定义 API：在 `src/api/` 建模块暴露 `routes: list[BaseRoute]`，
并在下方 `ROUTES` 里展开一行即可（组合根模式，避免堆进单个文件）。
"""
import os
import sys

# 防御性插 src 进 sys.path：start_server.py 已插；此处兜底 langgraph dev 等
# 未主动插 src 的启动方式，保证 api/mcp_server 包可导入。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import BaseRoute  # noqa: E402

import api.db_config  # noqa: E402

ROUTES: list[BaseRoute] = [
    *api.db_config.routes,
    # 后期新增：import api.<name> + 展开 *api.<name>.routes
]

app = Starlette(routes=ROUTES)
