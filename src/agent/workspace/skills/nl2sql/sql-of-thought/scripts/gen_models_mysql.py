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
    "database": "aix_report",
    "user": "aoi-dev",
    "password": "aoi8dev.1234",
}

MODELS_DIR = "models"
RELATIONSHIPS_FILE = "relationships.yml"

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

# ── 生成 relationships.yml ──────────────────────────────────────
# 重新连接，收集所有外键关系
engine2 = create_engine(url)
inspector2 = inspect(engine2)

relationships = []
for table in inspector2.get_table_names():
    fks = inspector2.get_foreign_keys(table)
    for fk in fks:
        source_model = table.lower() + "_t"
        target_table = fk["referred_table"]
        target_model = target_table.lower() + "_t"

        # 跳过自引用外键
        if source_model == target_model:
            continue

        # 取第一个约束列（Wren 的 condition 只支持单列等值）
        col = fk["constrained_columns"][0]
        referred_col = fk["referred_columns"][0]

        rel = {
            "name": f"{source_model}_{target_model}",
            "models": [source_model, target_model],
            "join_type": "MANY_TO_ONE",
            "condition": f"{source_model}.{col} = {target_model}.{referred_col}",
        }
        relationships.append(rel)

engine2.dispose()

# 去重（同一对表可能有多条 FK，取第一条即可）
seen = set()
unique_rels = []
for r in relationships:
    key = tuple(sorted(r["models"]))
    if key not in seen:
        seen.add(key)
        unique_rels.append(r)

with open(RELATIONSHIPS_FILE, "w", encoding="utf-8") as f:
    f.write("# Auto-generated from database foreign keys\n")
    yaml.dump({"relationships": unique_rels}, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

print(f"\nGenerated {len(unique_rels)} relationships in {RELATIONSHIPS_FILE}/")
for r in unique_rels:
    print(f"  {r['name']}: {r['condition']}")

print("\nNext: wren context build")
