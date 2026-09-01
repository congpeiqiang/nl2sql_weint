"""db-config 管理 API 路由（随 langgraph API 同进程/同端口提供）。

由原独立服务 `src/mcp_server/db_mcp_server/db_config_api.py`（端口 8008）迁移至此，
经 `custom_app.py`（LANGGRAPH_HTTP 钩子）合并进 langgraph API（端口 2026）。

路由：
    GET    /api/db-configs              列表（密码脱敏 + semantic 标记）
    POST   /api/db-configs              新增/更新
    GET    /api/db-configs/{name}       单条（脱敏）
    DELETE /api/db-configs/{name}       删除
    POST   /api/db-configs/{name}/test  连通性测试
    GET    /healthz                     存活检查

（GET /api/wren-projects 已迁至 wren_semantic.py，返回语义库丰富元信息）
"""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body
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
    items = [_masked_with_semantic(d) for d in get_store().list_configs(masked=True)]
    return json_response({"databases": items})


async def get_config(request: Request):
    name = request.path_params["name"]
    try:
        cfg = get_store().get(name)
    except KeyError:
        return json_response({"error": f"数据库 '{name}' 不存在"}, status=404)
    return json_response(_masked_with_semantic(cfg.to_mapping(masked=True)))


async def upsert_config(request: Request):
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
    return json_response({"ok": True, "name": name})


async def delete_config(request: Request):
    name = request.path_params["name"]
    ok = get_store().delete(name)
    if not ok:
        return json_response({"error": f"数据库 '{name}' 不存在"}, status=404)
    try:
        from agent.utils.semantic_db import get_detector
        get_detector().invalidate()
    except Exception:  # noqa: BLE001
        pass
    return json_response({"ok": True, "name": name})


async def test_config(request: Request):
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
    Route("/healthz", healthz, methods=["GET"]),
]
