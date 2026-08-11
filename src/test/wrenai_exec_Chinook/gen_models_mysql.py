"""
从 MySQL 数据库内省表结构，生成 WrenAI MDL 模型文件。

用法: python gen_models_mysql.py
需要: pip install sqlalchemy pymysql pyyaml
"""

import os, yaml
from sqlalchemy import create_engine, inspect, text

DB_CONFIG = {
    "host": "mysql-master",
    "port": 3306,
    "database": "Chinook_AutoIncrement",
    "user": "aoi-dev",
    "password": "aoi8dev.1234",
}

MODELS_DIR = "models"

TYPE_MAP = {
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER",
    "TINYINT": "INTEGER", "SMALLINT": "INTEGER", "MEDIUMINT": "INTEGER",
    "VARCHAR": "VARCHAR", "CHAR": "VARCHAR", "TEXT": "VARCHAR",
    "NVARCHAR": "VARCHAR", "LONGTEXT": "VARCHAR", "MEDIUMTEXT": "VARCHAR",
    "NUMERIC": "DOUBLE", "DECIMAL": "DOUBLE", "FLOAT": "DOUBLE", "DOUBLE": "DOUBLE",
    "DATETIME": "TIMESTAMP", "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
    "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN",
    "BLOB": "VARCHAR", "LONGBLOB": "VARCHAR",
}

url = f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset=utf8mb4"
engine = create_engine(url)
inspector = inspect(engine)

os.makedirs(MODELS_DIR, exist_ok=True)


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
    meta_path = os.path.join(d, "metadata.yml")
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(f"# {table} table\n")
        yaml.dump(model, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# 读取 MySQL 注释
table_comments = {}
col_comments = {}
try:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = :db"
        ), {"db": DB_CONFIG["database"]})
        table_comments = {r[0]: r[1] for r in rows if r[1]}
        rows = conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND COLUMN_COMMENT != ''"
        ), {"db": DB_CONFIG["database"]})
        for r in rows:
            col_comments.setdefault(r[0], {})[r[1]] = r[2]
except Exception as e:
    print(f"  (comments unavailable: {e})")


for table in inspector.get_table_names():
    cols = inspector.get_columns(table)
    pk_cols = inspector.get_pk_constraint(table).get("constrained_columns", [])
    fks = inspector.get_foreign_keys(table)

    columns = []
    pk_name = pk_cols[0] if len(pk_cols) == 1 else None

    for col in cols:
        raw_type = str(col["type"]).upper().split("(")[0]
        wren_type = TYPE_MAP.get(raw_type, "VARCHAR")
        is_pk = col["name"] in pk_cols
        col_entry = {
            "name": col["name"], "type": wren_type,
            "is_calculated": False,
            "not_null": not col.get("nullable", True),
            "is_primary_key": is_pk,
        }
        desc = col_comments.get(table, {}).get(col["name"], "")
        if desc:
            col_entry["properties"] = {"description": desc}
        columns.append(col_entry)

    extra = {}
    if table in table_comments:
        extra["properties"] = {"description": table_comments[table]}
    write_model(table, columns, pk_name, **extra)

    fk_info = f" (FKs: {len(fks)})" if fks else ""
    print(f"  {table.lower()}_t/ ({len(cols)} cols, PK={pk_name}{fk_info})")

engine.dispose()
print(f"\nGenerated models in {MODELS_DIR}/")
print("Next: wren context build")
