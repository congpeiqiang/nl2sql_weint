"""
从 SQLite Schema 自动生成 WrenAI MDL 模型文件。

用法: python gen_models.py
输出: models/ 目录（每表一个子目录 + metadata.yml）
"""

import sqlite3, os, yaml

DB_PATH = r"D:\code_work_space\llm\nl2sql\src\agent\data\Chinook_Sqlite.sqlite"
MODELS_DIR = "models"

TYPE_MAP = {
    "INTEGER": "INTEGER", "INT": "INTEGER",
    "VARCHAR": "VARCHAR", "NVARCHAR": "VARCHAR", "TEXT": "VARCHAR",
    "NUMERIC": "DOUBLE", "REAL": "DOUBLE",
    "DATETIME": "TIMESTAMP", "BOOLEAN": "BOOLEAN",
}

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cursor.fetchall()]

os.makedirs(MODELS_DIR, exist_ok=True)

for table in tables:
    cursor.execute(f"PRAGMA table_info('{table}')")
    cols = cursor.fetchall()
    cursor.execute(f"PRAGMA foreign_key_list('{table}')")
    fks = cursor.fetchall()

    columns = []
    pk_col = None
    for cid, name, col_type, notnull, default, pk in cols:
        if pk:
            pk_col = name
        columns.append({
            "name": name, "type": TYPE_MAP.get(col_type.upper(), "VARCHAR"),
            "is_calculated": False, "not_null": bool(notnull), "is_primary_key": bool(pk),
        })

    model_name = table.lower() + "_t"
    model = {
        "name": model_name,
        "table_reference": {"catalog": "", "schema": "", "table": table},
        "columns": columns, "cached": False,
    }
    if pk_col:
        model["primary_key"] = pk_col

    model_dir = os.path.join(MODELS_DIR, model_name)
    os.makedirs(model_dir, exist_ok=True)
    meta_path = os.path.join(model_dir, "metadata.yml")
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(f"# Chinook {table} table\n")
        yaml.dump(model, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"  {model_name}/ ({len(cols)} cols, PK={pk_col})")

conn.close()
print(f"\nGenerated {len(tables)} models in {MODELS_DIR}/")
print("Next: wren context build")
