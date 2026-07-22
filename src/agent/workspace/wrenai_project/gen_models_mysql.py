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
# 策略：优先用数据库外键，没有外键则按列名约定推断
engine2 = create_engine(url)
inspector2 = inspect(engine2)

all_tables = inspector2.get_table_names()
all_columns = {}
for t in all_tables:
    all_columns[t] = {c["name"] for c in inspector2.get_columns(t)}

relationships = []
seen_pairs = set()

for table in all_tables:
    source_model = table.lower() + "_t"

    # 策略 1：数据库外键
    fks = inspector2.get_foreign_keys(table)
    for fk in fks:
        target_table = fk["referred_table"]
        target_model = target_table.lower() + "_t"
        if source_model == target_model:
            continue
        col = fk["constrained_columns"][0]
        referred_col = fk["referred_columns"][0]
        key = tuple(sorted([source_model, target_model]))
        if key not in seen_pairs:
            seen_pairs.add(key)
            relationships.append({
                "name": f"{source_model}_{target_model}",
                "models": [source_model, target_model],
                "join_type": "MANY_TO_ONE",
                "condition": f"{source_model}.{col} = {target_model}.{referred_col}",
            })

    # 策略 2：按命名约定推断（仅当该表没有外键时）
    if not fks:
        cols = inspector2.get_columns(table)
        for col in cols:
            col_name = col["name"]
            # 匹配模式：xxx_id / xxx_code / xxx_no → 找对应表
            for suffix in ["_id", "_code", "_no"]:
                if col_name.endswith(suffix) and col_name != "id":
                    prefix = col_name[: -len(suffix)]
                    # 尝试匹配表名（精确匹配或包含匹配）
                    candidates = [t for t in all_tables if t.lower() == prefix.lower()
                                  or t.lower().startswith(prefix.lower())
                                  or prefix.lower().startswith(t.lower())]
                    # 排除自身
                    candidates = [c for c in candidates if c.lower() != table.lower()]
                    if candidates:
                        target_table = candidates[0]
                        target_model = target_table.lower() + "_t"
                        # 找目标表的主键列
                        target_pk = inspector2.get_pk_constraint(target_table).get("constrained_columns", [])
                        referred_col = target_pk[0] if target_pk else "id"
                        key = tuple(sorted([source_model, target_model]))
                        if key not in seen_pairs:
                            seen_pairs.add(key)
                            relationships.append({
                                "name": f"{source_model}_{target_model}",
                                "models": [source_model, target_model],
                                "join_type": "MANY_TO_ONE",
                                "condition": f"{source_model}.{col_name} = {target_model}.{referred_col}",
                            })
                            break  # 一个表只推断一条关系
            if relationships and relationships[-1]["models"][0] == source_model:
                break  # 已为此表找到关系，跳过剩余列

engine2.dispose()

with open(RELATIONSHIPS_FILE, "w", encoding="utf-8") as f:
    f.write("# Auto-generated from database foreign keys + naming convention inference\n")
    yaml.dump({"relationships": relationships}, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

print(f"\nGenerated {len(relationships)} relationships in {RELATIONSHIPS_FILE}/")
for r in relationships:
    print(f"  {r['name']}: {r['condition']}")

print("\nNext: wren context build")
