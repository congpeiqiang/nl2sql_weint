"""
通用数据库 → WrenAI MDL 生成器

从 config/connection.json 读取连接信息，自动内省并生成模型。
支持 MySQL (pymysql)、PostgreSQL (psycopg2)、SQLite (内置)。

用法: python gen_models.py [config/connection.json]
"""

import json, os, sys, yaml

TYPE_MAP = {
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER",
    "TINYINT": "INTEGER", "SMALLINT": "INTEGER", "SERIAL": "INTEGER",
    "VARCHAR": "VARCHAR", "CHAR": "VARCHAR", "TEXT": "VARCHAR", "LONGTEXT": "VARCHAR",
    "NVARCHAR": "VARCHAR",
    "NUMERIC": "DOUBLE", "DECIMAL": "DOUBLE", "FLOAT": "DOUBLE",
    "DOUBLE": "DOUBLE", "REAL": "DOUBLE",
    "DATETIME": "TIMESTAMP", "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
    "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN",
}

CONN_FILE = sys.argv[1] if len(sys.argv) > 1 else "config/connection.json"
MODELS_DIR = "models"

with open(CONN_FILE, "r", encoding="utf-8") as f:
    cfg = json.load(f)

ds = cfg["datasource"]
print(f"Datasource: {ds}")


def write_model(table, columns, pk_col, **extra):
    model_name = table.lower() + "_t"
    model = {
        "name": model_name,
        "table_reference": {"table": table},
        "columns": columns, "cached": False,
    }
    if pk_col:
        model["primary_key"] = pk_col
    model.update(extra)

    d = os.path.join(MODELS_DIR, model_name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "metadata.yml"), "w", encoding="utf-8") as f:
        f.write(f"# {table} table\n")
        yaml.dump(model, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"  {model_name}/ ({len(columns)} cols, PK={pk_col})")


# ── SQLite ──────────────────────────────────────────
if ds in ("sqlite", "duckdb"):
    import sqlite3
    db_path = cfg.get("url") or cfg.get("database_path") or cfg.get("database")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in c.fetchall()]

    for table in tables:
        c.execute(f"PRAGMA table_info('{table}')")
        cols_raw = c.fetchall()
        pk_col = None
        columns = []
        for cid, name, col_type, notnull, default, pk in cols_raw:
            if pk: pk_col = name
            columns.append({
                "name": name, "type": TYPE_MAP.get((col_type or "").upper(), "VARCHAR"),
                "is_calculated": False, "not_null": bool(notnull), "is_primary_key": bool(pk),
            })
        write_model(table, columns, pk_col)
    conn.close()

# ── MySQL / PostgreSQL ──────────────────────────────
elif ds in ("mysql", "postgres"):
    from sqlalchemy import create_engine, inspect, text

    if ds == "mysql":
        url = (
            f"mysql+pymysql://{cfg['user']}:{cfg['password']}"
            f"@{cfg['host']}:{cfg.get('port', 3306)}/{cfg['database']}?charset=utf8mb4"
        )
    else:
        url = (
            f"postgresql+psycopg2://{cfg['user']}:{cfg['password']}"
            f"@{cfg['host']}:{cfg.get('port', 5432)}/{cfg['database']}"
        )

    engine = create_engine(url)
    insp = inspect(engine)

    # 批量获取表注释和列注释
    table_comments = {}
    col_comments = {}
    try:
        with engine.connect() as conn:
            if ds == "mysql":
                rows = conn.execute(text(
                    "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = :db"
                ), {"db": cfg["database"]})
                table_comments = {r[0]: r[1] for r in rows if r[1]}
                rows = conn.execute(text(
                    "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND COLUMN_COMMENT != ''"
                ), {"db": cfg["database"]})
                for r in rows:
                    col_comments.setdefault(r[0], {})[r[1]] = r[2]
            elif ds == "postgres":
                rows = conn.execute(text(
                    "SELECT c.relname, pg_catalog.obj_description(c.oid) "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind = 'r' AND n.nspname = 'public'"
                ))
                table_comments = {r[0]: r[1] for r in rows if r[1]}
                rows = conn.execute(text(
                    "SELECT c.relname, a.attname, pg_catalog.col_description(a.attrelid, a.attnum) "
                    "FROM pg_catalog.pg_attribute a "
                    "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind = 'r' AND n.nspname = 'public' "
                    "AND pg_catalog.col_description(a.attrelid, a.attnum) IS NOT NULL"
                ))
                for r in rows:
                    col_comments.setdefault(r[0], {})[r[1]] = r[2]
    except Exception as e:
        print(f"  (comments unavailable: {e})")

    for table in insp.get_table_names():
        cols = insp.get_columns(table)
        pk_cols = insp.get_pk_constraint(table).get("constrained_columns", [])
        pk_col = pk_cols[0] if len(pk_cols) == 1 else None
        columns = []
        for col in cols:
            raw_type = str(col["type"]).upper().split("(")[0]
            col_entry = {
                "name": col["name"], "type": TYPE_MAP.get(raw_type, "VARCHAR"),
                "is_calculated": False,
                "not_null": not col.get("nullable", True),
                "is_primary_key": col["name"] in pk_cols,
            }
            # 添加列注释
            desc = col_comments.get(table, {}).get(col["name"], "")
            if desc:
                col_entry["properties"] = {"description": desc}
            columns.append(col_entry)

        # 添加表注释
        extra = {}
        if table in table_comments:
            extra["properties"] = {"description": table_comments[table]}
        write_model(table, columns, pk_col, **extra)
    engine.dispose()

else:
    print(f"Unsupported: {ds}. Supported: sqlite, duckdb, mysql, postgres")
    sys.exit(1)

print(f"\nDone. Next: wren context build")
