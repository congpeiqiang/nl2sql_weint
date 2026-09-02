"""MCP SQL server configuration adapter.

Wraps :class:`app.core.config.Settings` into the :class:`McpSqlConfig`
structure expected by :class:`app.db.server.VannaMcpSqlServer`.
"""

from typing import Any, Dict

from mcp_server.db_mcp_server.db.core.settings import settings

def _parse_kv_config(raw: str) -> Dict[str, Any]:
    """Parse a comma-separated ``key=value`` string into a dict.

    Examples::

        _parse_kv_config("project_id=my-gcp-project,cred_file_path=/path/to/creds.json")
        # => {"project_id": "my-gcp-project", "cred_file_path": "/path/to/creds.json"}

        _parse_kv_config("")
        # => {}
    """
    result: Dict[str, Any] = {}
    if not raw:
        return result
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Auto-convert numeric values
        if value.isdigit():
            value = int(value)
        result[key] = value
    return result

class McpSqlConfig:
    """Holds MCP SQL server configuration."""

    def __init__(self, db_type: str, config: Dict[str, Any]):
        self.db_type = db_type
        self.config = config

    @classmethod
    def from_env(cls, db_name: str = "") -> "McpSqlConfig":
        """从集中 store（db_config.json）读取指定库配置；未配置时回退 .env。

        - 优先 ``DbConfigStore``（前端管理面板写入，db_type 按库独立，密码解密）
        - ``db_name`` 为空时取 store 第一条；store 为空则回退 .env 第一条
        - store 中找不到 ``db_name`` 时回退 .env 同名库
        """
        from mcp_server.db_mcp_server.db.core.db_config_store import get_store

        store = get_store()
        try:
            if db_name:
                cfg = store.get(db_name)
            else:
                all_cfgs = store.get_all_decrypted()
                cfg = all_cfgs[0] if all_cfgs else None
            if cfg is not None:
                return cls._from_store_cfg(cfg)
        except KeyError:
            pass  # store 未配置该库 → 回退 .env

        # ── 回退 .env（历史配置）────────────
        dbs = settings.get_databases()
        if not dbs:
            raise ValueError("未配置任何数据库（db_config.json 或 .env 的 DB_1_NAME=...）")

        target_name = db_name or dbs[0]["name"]
        target = next((d for d in dbs if d["name"] == target_name), None)
        if not target:
            # 大小写容错（与 db_config_store.get 同口径，2026-08-23）：避免
            # 大小写不同名在 .env 兜底路径误报「未配置」触发串库级联
            target = next((d for d in dbs if d["name"].lower() == target_name.lower()), None)
        if not target:
            available = [d["name"] for d in dbs]
            raise ValueError(
                f"数据库 '{db_name}' 未配置。可用: {available}"
            )

        config = {
            "host": target["host"],
            "port": target["port"],
            "database": target["name"],
            "user": target["user"],
            "password": target["password"],
        }
        config = {k: v for k, v in config.items() if v}
        config.update(_parse_kv_config(settings.DB_EXTRA_CONFIG))
        return cls(db_type=settings.DB_TYPE, config=config)

    @classmethod
    def _from_store_cfg(cls, cfg) -> "McpSqlConfig":
        """把 store 的一条 DBConfig 转成 runner 构造参数（db_type 按库独立）。

        保留空字符串字段（mysql/clickhouse runner 的 password 为必填，空密码也是合法值）。
        """
        config = {
            "host": cfg.host,
            "port": cfg.port,
            "database": cfg.database,
            "user": cfg.user,
            "password": cfg.password,
        }
        config = {k: v for k, v in config.items() if v is not None}
        if cfg.db_type == "sqlite":
            # SqliteRunner 构造参数只有 database_path
            config = {"database_path": cfg.database or cfg.host or "data/db.sqlite"}
        config.update(cfg.extra_config or {})
        return cls(db_type=cfg.db_type, config=config)
