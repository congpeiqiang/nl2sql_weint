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
    def from_env(cls) -> "McpSqlConfig":
        """Build configuration from central :mod:`app.core.config`."""
        db_type = settings.DB_TYPE
        extra = _parse_kv_config(settings.DB_EXTRA_CONFIG)

        if db_type in ("sqlite", "duckdb"):
            path = settings.SQLITE_DB_PATH or extra.pop("database_path", "")
            if not path:
                raise ValueError(
                    f"SQLITE_DB_PATH is required when DB_TYPE={db_type}"
                )
            config: Dict[str, Any] = {"database_path": path}
            config.update(extra)
            return cls(db_type=db_type, config=config)

        # Base relational config
        config = {
            "host": settings.DB_HOST,
            "port": settings.DB_PORT,
            "database": settings.DB_NAME,
            "user": settings.DB_USER,
            "password": settings.DB_PASSWORD,
        }
        # Drop empty values so extra can cleanly override
        config = {k: v for k, v in config.items() if v}
        config.update(extra)
        return cls(db_type=db_type, config=config)
