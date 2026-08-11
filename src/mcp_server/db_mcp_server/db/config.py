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
        """从 .env 读取数据库配置。db_name 为空时取第一个，未找到抛异常。"""
        db_type = settings.DB_TYPE
        extra = _parse_kv_config(settings.DB_EXTRA_CONFIG)
        dbs = settings.get_databases()

        if not dbs:
            raise ValueError("未在 .env 中配置任何数据库（需要 DB_1_NAME=...）")

        # db_name 为空时取第一个（服务启动时用）
        target_name = db_name or dbs[0]["name"]
        target = next((d for d in dbs if d["name"] == target_name), None)
        if not target:
            available = [d["name"] for d in dbs]
            raise ValueError(
                f"数据库 '{db_name}' 未在 .env 中配置。可用: {available}"
            )

        config = {
            "host": target["host"],
            "port": target["port"],
            "database": target["name"],
            "user": target["user"],
            "password": target["password"],
        }
        config = {k: v for k, v in config.items() if v}
        config.update(extra)
        return cls(db_type=db_type, config=config)
