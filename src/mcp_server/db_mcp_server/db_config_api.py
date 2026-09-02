"""db-config API 过渡 shim（独立 8008 入口，前端切到 2026 后可删除）。

路由实现已迁移至 `src/api/db_config.py`，随 langgraph API（`LANGGRAPH_HTTP.app`
钩子，见 .env）在端口 2026 与 langgraph 原生路由同进程提供。

本文件仅保留独立 8008 启动能力（`main()`）供过渡期/回滚使用：
    .venv/Scripts/python.exe -m mcp_server.db_mcp_server.db_config_api
或：
    uvicorn mcp_server.db_mcp_server.db_config_api:app --port 8008
"""
from __future__ import annotations

import logging

from starlette.applications import Starlette

from api.db_config import routes

_logger = logging.getLogger(__name__)

app = Starlette(routes=routes)


def main() -> None:
    import os
    import uvicorn

    port = int(os.getenv("NL2SQL_DB_CONFIG_PORT", "8008"))
    logging.basicConfig(level=logging.INFO)
    print(f"[db-config] API: http://localhost:{port}/api/db-configs")
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
