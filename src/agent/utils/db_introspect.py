"""数据库内省模块 — 连接数据库提取表结构，供语义库新建流程使用。

支持 ClickHouse（HTTP 协议）、MySQL（SQLAlchemy）、PostgreSQL（SQLAlchemy）。
统一返回 `IntrospectResult`，消费方无需关心底层数据库差异。

用法:
    from agent.utils.db_introspect import introspect_database
    from mcp_server.db_mcp_server.db.core.db_config_store import get_store

    cfg = get_store().get("clickhouse")
    result = introspect_database(cfg)
    for t in result.tables:
        print(t.name, len(t.columns), t.primary_key)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

# ── 类型映射（与 gen_models.py 保持一致）──────────────────────
TYPE_MAP: dict[str, str] = {
    # 通用
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER",
    "TINYINT": "INTEGER", "SMALLINT": "INTEGER", "MEDIUMINT": "INTEGER",
    "VARCHAR": "VARCHAR", "CHAR": "VARCHAR", "TEXT": "VARCHAR",
    "NVARCHAR": "VARCHAR", "LONGTEXT": "VARCHAR", "MEDIUMTEXT": "VARCHAR",
    "NUMERIC": "DOUBLE", "DECIMAL": "DOUBLE", "FLOAT": "DOUBLE", "DOUBLE": "DOUBLE",
    "DATETIME": "TIMESTAMP", "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
    "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN",
    "BLOB": "VARCHAR", "LONGBLOB": "VARCHAR",
    # ClickHouse
    "STRING": "VARCHAR", "FIXEDSTRING": "VARCHAR",
    "UINT8": "INTEGER", "UINT16": "INTEGER", "UINT32": "INTEGER", "UINT64": "INTEGER",
    "INT8": "INTEGER", "INT16": "INTEGER", "INT32": "INTEGER", "INT64": "INTEGER",
    "FLOAT32": "DOUBLE", "FLOAT64": "DOUBLE",
    "DATETIME64": "TIMESTAMP",
    # PostgreSQL
    "SERIAL": "INTEGER", "BIGSERIAL": "INTEGER", "SMALLSERIAL": "INTEGER",
    "REAL": "DOUBLE", "MONEY": "DOUBLE",
    "BPCHAR": "VARCHAR", "UUID": "VARCHAR", "JSON": "VARCHAR", "JSONB": "VARCHAR",
    "BYTEA": "VARCHAR", "INTERVAL": "VARCHAR", "INET": "VARCHAR", "CIDR": "VARCHAR",
    "MACADDR": "VARCHAR", "POINT": "VARCHAR", "POLYGON": "VARCHAR",
    "TIMESTAMPTZ": "TIMESTAMP", "TIMETZ": "VARCHAR",
}


def _map_wren_type(raw_type: str) -> str:
    """将原始数据库类型映射为 Wren MDL 类型。"""
    normalized = str(raw_type).upper().split("(")[0].strip()
    return TYPE_MAP.get(normalized, "VARCHAR")


# ── 数据结构 ─────────────────────────────────────────────────
@dataclass
class ColumnInfo:
    name: str
    type: str                        # 原始数据库类型
    wren_type: str                   # 映射后的 Wren 类型
    nullable: bool = True
    comment: str = ""
    is_primary_key: bool = False


@dataclass
class TableInfo:
    name: str
    comment: str = ""
    columns: list[ColumnInfo] = field(default_factory=list)
    primary_key: str | None = None
    row_count: int = 0               # 估算行数，0 表示未获取


@dataclass
class ForeignKeyInfo:
    source_table: str
    source_column: str
    target_table: str
    target_column: str


@dataclass
class IntrospectResult:
    tables: list[TableInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)


# ── 入口 ─────────────────────────────────────────────────────
def introspect_database(cfg: Any) -> IntrospectResult:
    """根据 db_type 选择内省策略，返回统一结构。

    cfg 是 DBConfig 实例（来自 db_config_store），需含已解密的连接信息。
    """
    db_type = str(getattr(cfg, "db_type", "") or "").lower()
    if db_type == "clickhouse":
        return _introspect_clickhouse(cfg)
    elif db_type == "mysql":
        return _introspect_mysql(cfg)
    elif db_type == "postgres":
        return _introspect_postgres(cfg)
    else:
        raise ValueError(f"不支持的内省数据库类型: {db_type}")


# ── ClickHouse（HTTP 协议）───────────────────────────────────
def _introspect_clickhouse(cfg: Any) -> IntrospectResult:
    import requests
    from requests.auth import HTTPBasicAuth

    host = cfg.host
    port = getattr(cfg, "port", 8123) or 8123
    database = cfg.database or "default"
    user = cfg.user or ""
    password = cfg.password or ""
    base_url = f"http://{host}:{port}"
    auth = HTTPBasicAuth(user, password)

    def _query(sql: str, timeout: int = 30) -> str:
        resp = requests.get(
            f"{base_url}/?query={sql}", auth=auth, timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text

    tables: list[TableInfo] = []
    foreign_keys: list[ForeignKeyInfo] = []

    # 1) 表列表
    raw = _query(
        f"SELECT name FROM system.tables WHERE database='{database}'"
    )
    table_names = [t for t in raw.strip().split("\n") if t]

    # 2) 表注释
    table_comments: dict[str, str] = {}
    try:
        raw = _query(
            f"SELECT name, comment FROM system.tables WHERE database='{database}'"
        )
        lines = raw.strip().split("\n")
        if len(lines) > 1 and "\t" in lines[0]:
            for line in lines[1:]:
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1]:
                    table_comments[parts[0]] = parts[1]
    except Exception as e:
        _logger.debug("[db_introspect] ClickHouse 表注释获取失败: %s", e)

    # 3) 列信息
    columns_info: dict[str, list[ColumnInfo]] = {}
    try:
        raw = _query(
            f"SELECT database, table, name, type, comment "
            f"FROM system.columns WHERE database='{database}'"
        )
        lines = raw.strip().split("\n")
        if len(lines) > 0:
            first = lines[0].split("\t")
            is_header = len(first) >= 5 and all(
                c in first for c in ["database", "table", "name", "type", "comment"]
            )
            start = 1 if is_header else 0
            for line in lines[start:]:
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                tbl = parts[1]
                if not tbl:
                    continue
                col = ColumnInfo(
                    name=parts[2],
                    type=parts[3],
                    wren_type=_map_wren_type(parts[3]),
                    nullable=True,
                    comment=parts[4] if len(parts) > 4 else "",
                )
                columns_info.setdefault(tbl, []).append(col)
    except Exception as e:
        _logger.warning("[db_introspect] ClickHouse 列信息获取失败: %s", e)

    # 4) 主键
    pk_map: dict[str, str] = {}
    for tbl in table_names:
        try:
            raw = _query(
                f"SELECT name FROM system.keys "
                f"WHERE database='{database}' AND table='{tbl}' AND type='PRIMARY'"
            )
            lines = raw.strip().split("\n")
            if len(lines) > 1 and "\t" in lines[0]:
                pk_map[tbl] = lines[1].split("\t")[0]
        except Exception:
            pass

    # 5) 外键
    try:
        raw = _query(
            "SELECT source_database, source_table, source_column, "
            "destination_database, destination_table, destination_column "
            "FROM system.foreign_keys "
            f"WHERE source_database='{database}'"
        )
        lines = raw.strip().split("\n")
        if len(lines) > 1 and "\t" in lines[0]:
            for line in lines[1:]:
                parts = line.split("\t")
                if len(parts) >= 6:
                    foreign_keys.append(ForeignKeyInfo(
                        source_table=parts[1],
                        source_column=parts[2],
                        target_table=parts[4],
                        target_column=parts[5],
                    ))
    except Exception as e:
        _logger.debug("[db_introspect] ClickHouse 外键获取失败: %s", e)

    # 组装
    for tbl in table_names:
        cols = columns_info.get(tbl, [])
        pk = pk_map.get(tbl)
        if pk:
            for c in cols:
                if c.name == pk:
                    c.is_primary_key = True
        tables.append(TableInfo(
            name=tbl,
            comment=table_comments.get(tbl, ""),
            columns=cols,
            primary_key=pk,
        ))

    return IntrospectResult(tables=tables, foreign_keys=foreign_keys)


# ── MySQL（SQLAlchemy）───────────────────────────────────────
def _introspect_mysql(cfg: Any) -> IntrospectResult:
    from sqlalchemy import create_engine, inspect, text

    url = (
        f"mysql+pymysql://{cfg.user}:{cfg.password}"
        f"@{cfg.host}:{cfg.port or 3306}/{cfg.database or ''}"
        f"?charset=utf8mb4"
    )
    engine = create_engine(url)
    inspector = inspect(engine)

    tables: list[TableInfo] = []
    foreign_keys: list[ForeignKeyInfo] = []
    try:
        table_names = inspector.get_table_names()
    except Exception as e:
        engine.dispose()
        raise RuntimeError(f"MySQL 获取表列表失败: {e}")

    # 表注释
    table_comments: dict[str, str] = {}
    col_comments: dict[str, dict[str, str]] = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = :db"
            ), {"db": cfg.database})
            table_comments = {r[0]: r[1] for r in rows if r[1]}
            rows = conn.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND COLUMN_COMMENT != ''"
            ), {"db": cfg.database})
            for r in rows:
                col_comments.setdefault(r[0], {})[r[1]] = r[2]
    except Exception as e:
        _logger.debug("[db_introspect] MySQL 注释获取失败: %s", e)

    for tbl in table_names:
        cols_raw = inspector.get_columns(tbl)
        pk_cols = inspector.get_pk_constraint(tbl).get("constrained_columns", [])
        pk = pk_cols[0] if len(pk_cols) == 1 else None
        columns = []
        for c in cols_raw:
            raw_type = str(c["type"]).upper().split("(")[0]
            columns.append(ColumnInfo(
                name=c["name"],
                type=str(c["type"]),
                wren_type=_map_wren_type(raw_type),
                nullable=c.get("nullable", True),
                comment=col_comments.get(tbl, {}).get(c["name"], ""),
                is_primary_key=c["name"] in pk_cols,
            ))
        tables.append(TableInfo(
            name=tbl,
            comment=table_comments.get(tbl, ""),
            columns=columns,
            primary_key=pk,
        ))

        # 外键
        for fk in inspector.get_foreign_keys(tbl):
            if fk.get("referred_table"):
                foreign_keys.append(ForeignKeyInfo(
                    source_table=tbl,
                    source_column=fk["constrained_columns"][0] if fk.get("constrained_columns") else "",
                    target_table=fk["referred_table"],
                    target_column=fk["referred_columns"][0] if fk.get("referred_columns") else "",
                ))

    engine.dispose()
    return IntrospectResult(tables=tables, foreign_keys=foreign_keys)


# ── PostgreSQL（SQLAlchemy）──────────────────────────────────
def _introspect_postgres(cfg: Any) -> IntrospectResult:
    from sqlalchemy import create_engine, inspect

    url = (
        f"postgresql+psycopg://{cfg.user}:{cfg.password}"
        f"@{cfg.host}:{cfg.port or 5432}/{cfg.database or ''}"
    )
    engine = create_engine(url)
    inspector = inspect(engine)

    tables: list[TableInfo] = []
    foreign_keys: list[ForeignKeyInfo] = []
    try:
        # 只取 public schema 的表，排除系统表
        table_names = [
            t for t in inspector.get_table_names(schema="public")
            if not t.startswith("pg_") and not t.startswith("sql_")
        ]
    except Exception as e:
        engine.dispose()
        raise RuntimeError(f"PostgreSQL 获取表列表失败: {e}")

    # 表注释 + 列注释（pg_description）
    table_comments: dict[str, str] = {}
    col_comments: dict[str, dict[str, str]] = {}
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            # 表注释：obj_description(c.oid) 读取 pg_description 中 objsubid=0 的记录
            rows = conn.execute(text(
                "SELECT c.relname, obj_description(c.oid) "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND obj_description(c.oid) IS NOT NULL"
            ))
            for row in rows:
                if row[1]:
                    table_comments[row[0]] = row[1]
            # 列注释
            rows = conn.execute(text(
                "SELECT c.relname AS table_name, a.attname AS column_name, "
                "pg_catalog.col_description(a.attrelid, a.attnum) AS comment "
                "FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON a.attrelid = c.oid "
                "JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND a.attnum > 0 AND NOT a.attisdropped "
                "AND pg_catalog.col_description(a.attrelid, a.attnum) IS NOT NULL"
            ))
            for row in rows:
                col_comments.setdefault(row[0], {})[row[1]] = row[2] or ""
    except Exception as e:
        _logger.debug("[db_introspect] PostgreSQL 注释获取失败: %s", e)

    for tbl in table_names:
        cols_raw = inspector.get_columns(tbl, schema="public")
        pk_cols = inspector.get_pk_constraint(tbl, schema="public").get(
            "constrained_columns", []
        )
        pk = pk_cols[0] if len(pk_cols) == 1 else None
        columns = []
        for c in cols_raw:
            raw_type = str(c["type"]).upper().split("(")[0]
            columns.append(ColumnInfo(
                name=c["name"],
                type=str(c["type"]),
                wren_type=_map_wren_type(raw_type),
                nullable=c.get("nullable", True),
                comment=col_comments.get(tbl, {}).get(c["name"], ""),
                is_primary_key=c["name"] in pk_cols,
            ))
        tables.append(TableInfo(
            name=tbl,
            comment=table_comments.get(tbl, ""),
            columns=columns,
            primary_key=pk,
        ))

        # 外键
        for fk in inspector.get_foreign_keys(tbl, schema="public"):
            if fk.get("referred_table"):
                foreign_keys.append(ForeignKeyInfo(
                    source_table=tbl,
                    source_column=fk["constrained_columns"][0] if fk.get("constrained_columns") else "",
                    target_table=fk["referred_table"],
                    target_column=fk["referred_columns"][0] if fk.get("referred_columns") else "",
                ))

    engine.dispose()
    return IntrospectResult(tables=tables, foreign_keys=foreign_keys)