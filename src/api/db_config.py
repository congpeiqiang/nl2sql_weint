"""db-config 管理 API 路由（随 langgraph API 同进程/同端口提供）。

由原独立服务 `src/mcp_server/db_mcp_server/db_config_api.py`（端口 8008）迁移至此，
经 `custom_app.py`（LANGGRAPH_HTTP 钩子）合并进 langgraph API（端口 2026）。

路由：
    GET    /api/db-configs              列表（密码脱敏 + semantic 标记）
    POST   /api/db-configs              新增/更新（含该库 MCP 工具热加载）
    GET    /api/db-configs/{name}       单条（脱敏）
    DELETE /api/db-configs/{name}       删除（含该库 MCP 工具即时下线）
    POST   /api/db-configs/{name}/test  连通性测试
    GET    /api/mcp/status              运行期 MCP 工具注册表状态
    POST   /api/mcp/reload              全量对账 MCP 工具注册表（免重启）
    GET    /healthz                     存活检查

（GET /api/wren-projects 已迁至 wren_semantic.py，返回语义库丰富元信息）

热加载（2026-09-19）：新增/删除库不再需要重启后端。写路径失效 detector 缓存后，
把该库的 wrenai server **同步**加载进 `mcp_tool` 的运行期注册表（`ensure_sub_loaded`），
工具立刻对该进程内所有会话可见；删除则立即摘掉。原理与边界见
`agent/middlewares/dynamic_mcp_tools.py` 模块 docstring。
"""
from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body, require_user, require_admin
from mcp_server.db_mcp_server.db.core.db_config_store import DBConfig, get_store

_logger = logging.getLogger(__name__)


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
            import psycopg
            conn = psycopg.connect(
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


# ── Wren 项目扫描（前端下拉选择 wren_project 数据源）────────────
def _scan_wren_projects() -> list[dict]:
    """扫描磁盘可用的 Wren 项目（含 wren_project.yml 的目录）。

    来源：1) 活跃工作区下的一级子目录；2) .env 的 WREN_PROJECT_PATH
    自身（无论位置）。返回 [{path, name}]，按 path 去重，绝对路径。
    """
    from pathlib import Path
    from agent.workspace_manager import get_workspace_manager

    found: dict[str, str] = {}

    # 1) 活跃工作区下一级子目录
    workspace = get_workspace_manager().semantic_dir
    if workspace.is_dir():
        for sub in sorted(workspace.iterdir()):
            if not sub.is_dir():
                continue
            if "备份" in sub.name or "backup" in sub.name.lower():
                continue  # 排除备份目录
            if (sub / "wren_project.yml").is_file():
                found[str(sub.resolve())] = sub.name

    # 2) 默认项目自身（可能不在工作区下）
    try:
        from agent.settings.setting import settings
        p = settings.WREN_PROJECT_PATH
        if p:
            pp = Path(p)
            if (pp / "wren_project.yml").is_file():
                found[str(pp.resolve())] = pp.name
    except Exception:  # noqa: BLE001
        pass

    return [{"path": k, "name": v} for k, v in sorted(found.items())]


# ── 路由 ─────────────────────────────────────────────────
async def list_configs(request: Request):
    user = require_user(request)
    from agent.auth.grants import visible_dbs
    allowed = visible_dbs(user)
    items = [
        _masked_with_semantic(d)
        for d in get_store().list_configs(masked=True)
        if d.get("name") in allowed
    ]
    return json_response({"databases": items})


async def get_config(request: Request):
    user = require_user(request)
    name = request.path_params["name"]
    from agent.auth.grants import can_access_db
    if not can_access_db(user, name):
        return json_response({"error": f"无权访问数据库: {name}"}, status=403)
    try:
        cfg = get_store().get(name)
    except KeyError:
        return json_response({"error": f"数据库 '{name}' 不存在"}, status=404)
    return json_response(_masked_with_semantic(cfg.to_mapping(masked=True)))


async def upsert_config(request: Request):
    require_admin(request)
    data = await parse_body(request)
    name = data.get("name")
    if not name:
        return json_response({"error": "name 必填"}, status=400)
    try:
        cfg = DBConfig.from_mapping(data)
        get_store().upsert(cfg)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        return json_response({"error": f"保存失败: {e}"}, status=500)
    # wren_project 变更：失效 detector 缓存，semantic 标记下次读取即反映新配置
    try:
        from agent.utils.semantic_db import get_detector
        get_detector().invalidate()
    except Exception:  # noqa: BLE001
        pass
    # 该库的 wrenai 工具立即进运行期注册表（免重启）。同步等待是刻意的：响应体
    # 里带回"加载了几个工具 / 失败原因"，让"配好了但工具没起来"当场可见。
    mcp = await _ensure_mcp_tools(name)
    return json_response({"ok": True, "name": name, "mcp": mcp})


async def delete_config(request: Request):
    require_admin(request)
    name = request.path_params["name"]
    ok = get_store().delete(name)
    if not ok:
        return json_response({"error": f"数据库 '{name}' 不存在"}, status=404)
    try:
        from agent.utils.semantic_db import get_detector
        get_detector().invalidate()
    except Exception:  # noqa: BLE001
        pass
    # 工具立即下线（免重启）：模型清单里不再出现；历史消息里的存量调用由
    # DynamicMCPToolsMiddleware 返回错误 ToolMessage，不会执行陈旧实例
    removed: list = []
    try:
        from agent.tools.mcp_tool import invalidate_sub_entries
        removed = invalidate_sub_entries(name)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[db_config] %s: MCP 工具下线失败: %s", name, e)
    return json_response({
        "ok": True, "name": name,
        "mcp": {"removed_servers": removed},
    })


# ── MCP 工具注册表（运行期，免重启增删库）──────────────────
async def _ensure_mcp_tools(name: str, force: bool = True) -> dict:
    """把某库的语义工具同步进运行期注册表；失败只上报、不抛（保存本身已成功）。"""
    try:
        from agent.tools.mcp_tool import ensure_sub_loaded

        # 必须放线程：内部起 MCP 子进程是阻塞调用（且要用自己的事件循环），
        # 直接 await 会把 API 服务的事件循环卡住秒级
        return await asyncio.to_thread(ensure_sub_loaded, name, force)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[db_config] %s: MCP 工具热加载失败: %s", name, e)
        return {"db_name": name, "status": "error", "error": str(e)}


async def mcp_status(request: Request):
    """运行期 MCP 工具注册表状态（排查"某库工具没起来"的直接抓手）。"""
    from agent.tools.mcp_tool import get_mcp_status

    return json_response(get_mcp_status())


async def reload_mcp(request: Request):
    """全量对账运行期工具注册表：新增/删除库或语义库后免重启生效的手动开关。

    与写路径自动加载同一套逻辑（`refresh_sub_entries`）；响应体即对账结果
    （added/changed/retried/removed/loaded/counts），可直接判断哪个 server 没起来。
    上次加载失败的条目会被**重试**（`retried` 列出），所以它同时是「某库工具一直
    没起来（建库时还没 MDL 之类）」的恢复手段——不必重启后端，也不必去动库配置。
    """
    try:
        from agent.utils.semantic_db import get_detector
        get_detector().invalidate()
    except Exception:  # noqa: BLE001
        pass
    try:
        from agent.tools.mcp_tool import refresh_sub_entries

        result = await asyncio.to_thread(refresh_sub_entries)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[db_config] MCP 工具对账失败: %s", e)
        return json_response({"ok": False, "error": str(e)}, status=500)
    return json_response({"ok": True, **result})


async def test_config(request: Request):
    require_admin(request)
    name = request.path_params["name"]
    data = await parse_body(request)
    if data:
        # 用请求体构造测试（保存前校验：带明文密码）
        cfg = DBConfig.from_mapping(data)
    else:
        # 测试已保存的配置
        try:
            cfg = get_store().get(name)
        except KeyError:
            return json_response({"error": f"数据库 '{name}' 不存在"}, status=404)
    ok, msg = _test_connection(cfg)
    return json_response({"ok": ok, "message": msg}, status=200 if ok else 400)


async def healthz(request: Request):
    return json_response({"ok": True, "service": "db-config-api"})


# ── 路由表（custom_app.py 聚合）──────────────────────────
# 注：GET /api/wren-projects 已迁至 wren_semantic.py（语义库管理模块），
# 返回更丰富的语义库元信息（git/构建状态/关联库）。_scan_wren_projects 保留
# 在此供 wren_semantic 复用（磁盘扫描基准）。
routes: list[BaseRoute] = [
    Route("/api/db-configs", list_configs, methods=["GET"]),
    Route("/api/db-configs", upsert_config, methods=["POST"]),
    Route("/api/db-configs/{name}", get_config, methods=["GET"]),
    Route("/api/db-configs/{name}", delete_config, methods=["DELETE"]),
    Route("/api/db-configs/{name}/test", test_config, methods=["POST"]),
    Route("/api/mcp/status", mcp_status, methods=["GET"]),
    Route("/api/mcp/reload", reload_mcp, methods=["POST"]),
    Route("/healthz", healthz, methods=["GET"]),
]
