"""数据库配置管理 API — Starlette 应用（uvicorn 启动，默认端口 8008）。

前端 Next.js 通过 `/api/db-configs` 代理到本服务（同源免 CORS）。
密码只在后端 store 落盘（AES 加密），列表/单条一律脱敏。

启动：
    .venv/Scripts/python.exe -m mcp_server.db_mcp_server.db_config_api
或：
    uvicorn mcp_server.db_mcp_server.db_config_api:app --port 8008
"""
from __future__ import annotations

import json
import logging

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_server.db_mcp_server.db.core.db_config_store import DBConfig, get_store

_logger = logging.getLogger(__name__)

store = get_store()


# ── 连通性测试（轻量：直连驱动，不依赖 runner 类）──────────────
def _test_connection(cfg: DBConfig) -> tuple[bool, str]:
    """按 db_type 用驱动直连测试，返回 (ok, message)。"""
    import socket

    def _quick_connect(host: str, port: int, timeout: float = 3.0) -> None:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()

    try:
        if cfg.db_type == "mysql":
            import pymysql
            conn = pymysql.connect(
                host=cfg.host, port=cfg.port, user=cfg.user,
                password=cfg.password, database=cfg.database or None,
                connect_timeout=3, charset="utf8mb4",
            )
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            conn.close()
        elif cfg.db_type == "clickhouse":
            import clickhouse_connect
            client = clickhouse_connect.get_client(
                host=cfg.host, port=cfg.port, username=cfg.user,
                password=cfg.password, database=cfg.database or "default",
                connect_timeout=3,
            )
            client.query("SELECT 1")
        elif cfg.db_type == "postgres":
            import psycopg2
            conn = psycopg2.connect(
                host=cfg.host, port=cfg.port, user=cfg.user,
                password=cfg.password, dbname=cfg.database,
                connect_timeout=3,
            )
            conn.close()
        elif cfg.db_type == "sqlite":
            import sqlite3
            import os
            path = cfg.database or cfg.host or "data/db.sqlite"
            if not os.path.exists(path):
                return False, f"SQLite 文件不存在: {path}"
            sqlite3.connect(path).close()
        else:
            _quick_connect(cfg.host, cfg.port)
        return True, f"{cfg.db_type} 连接成功"
    except Exception as e:  # noqa: BLE001
        return False, f"{cfg.db_type} 连接失败: {e}"


def _masked_with_semantic(cfg: dict) -> dict:
    """列表/单条响应：masked + semantic 标记。"""
    out = dict(cfg)
    db_name = out.get("database") or out.get("name") or ""
    from agent.utils.semantic_db import get_detector
    try:
        out["semantic"] = get_detector().is_modeled(str(out.get("name", ""))) or get_detector().is_modeled(str(db_name))
    except Exception as e:  # noqa: BLE001
        _logger.warning("[db_config] semantic 检测失败: %s", e)
        out["semantic"] = False
    return out


def _body(request: Request) -> dict:
    data = request.state._json  # 由 JSONMiddleware 填充
    return data or {}


# ── 路由 ─────────────────────────────────────────────────
async def list_configs(request: Request) -> JSONResponse:
    items = [_masked_with_semantic(d) for d in store.list_configs(masked=True)]
    return JSONResponse({"databases": items})


async def get_config(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    try:
        cfg = store.get(name)
    except KeyError:
        return JSONResponse({"error": f"数据库 '{name}' 不存在"}, status_code=404)
    return JSONResponse(_masked_with_semantic(cfg.to_mapping(masked=True)))


async def upsert_config(request: Request) -> JSONResponse:
    data = _body(request)
    name = data.get("name")
    if not name:
        return JSONResponse({"error": "name 必填"}, status_code=400)
    try:
        cfg = DBConfig.from_mapping(data)
        store.upsert(cfg)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"保存失败: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "name": name})


async def delete_config(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    ok = store.delete(name)
    if not ok:
        return JSONResponse({"error": f"数据库 '{name}' 不存在"}, status_code=404)
    return JSONResponse({"ok": True, "name": name})


async def test_config(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    data = _body(request)
    if data:
        # 用请求体构造测试（保存前校验：带明文密码）
        cfg = DBConfig.from_mapping(data)
    else:
        # 测试已保存的配置
        try:
            cfg = store.get(name)
        except KeyError:
            return JSONResponse({"error": f"数据库 '{name}' 不存在"}, status_code=404)
    ok, msg = _test_connection(cfg)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "db-config-api"})


class _JSONBodyMiddleware(BaseHTTPMiddleware):
    """读取 JSON body（Starlette 不自动解析）。"""

    async def dispatch(self, request: Request, call_next):
        request.state._json = {}
        try:
            ct = request.headers.get("content-type", "")
            if "application/json" in ct:
                body = await request.body()
                if body:
                    request.state._json = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            request.state._json = {}
        return await call_next(request)


# ── 应用 ─────────────────────────────────────────────────
routes = [
    Route("/api/db-configs", list_configs, methods=["GET"]),
    Route("/api/db-configs", upsert_config, methods=["POST"]),
    Route("/api/db-configs/{name}", get_config, methods=["GET"]),
    Route("/api/db-configs/{name}", delete_config, methods=["DELETE"]),
    Route("/api/db-configs/{name}/test", test_config, methods=["POST"]),
    Route("/healthz", healthz, methods=["GET"]),
]

app = Starlette(routes=routes, middleware=[
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    Middleware(_JSONBodyMiddleware),
])


def main() -> None:
    import os
    import uvicorn

    port = int(os.getenv("NL2SQL_DB_CONFIG_PORT", "8008"))
    logging.basicConfig(level=logging.INFO)
    print(f"🚀 数据库配置管理 API: http://localhost:{port}/api/db-configs")
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
