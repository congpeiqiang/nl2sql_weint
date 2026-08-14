"""
从 ClickHouse 数据库内省表结构，生成 WrenAI MDL 模型文件。

用法: python gen_models_mysql.py
需要: pip install requests pyyaml
"""

import os, yaml, json
from pathlib import Path
import requests
from requests.auth import HTTPBasicAuth


# ── 从 JSON 文件读取数据库配置 ──────────────────────────────────────
def _load_db_config():
    # 使用相对路径定位 config/connection_clickhouse.json
    config_path = Path(__file__).resolve().parent / "config" / "connection_clickhouse.json"

    if not config_path.exists():
        raise FileNotFoundError(f"数据库配置文件未找到: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 根据 JSON 实际结构，从 properties 中提取信息
    props = config.get("properties", {})
    db_config = {
        "host": props.get("host", ""),
        "port": int(props.get("port", 8123)),
        "database": props.get("database", ""),
        "user": props.get("user", ""),
        "password": props.get("password", ""),
        "protocol": props.get("protocol", "http")  # 新增协议选项
    }

    # 简单校验
    if not all([db_config["host"], db_config["database"], db_config["user"]]):
        raise ValueError("配置文件中缺少必要的数据库连接信息")

    return db_config


DB_CONFIG = _load_db_config()
print(
    f"Database: {DB_CONFIG['database']} ({DB_CONFIG['host']}:{DB_CONFIG['port']}) using {DB_CONFIG['protocol']} protocol")

MODELS_DIR = "models"
RELATIONSHIPS_FILE = "relationships.yml"

TYPE_MAP = {
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER",
    "TINYINT": "INTEGER@ClickHouse", "SMALLINT": "INTEGER", "MEDIUMINT": "INTEGER",
    "VARCHAR": "VARCHAR", "CHAR": "VARCHAR", "TEXT": "VARCHAR",
    "NVARCHAR": "VARCHAR", "LONGTEXT": "VARCHAR", "MEDIUMTEXT": "VARCHAR",
    "NUMERIC": "DOUBLE", "DECIMAL": "DOUBLE", "FLOAT": "DOUBLE", "DOUBLE": "DOUBLE",
    "DATETIME": "TIMESTAMP", "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
    "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN",
    "BLOB": "VARCHAR", "LONGBLOB": "VARCHAR",
    # 补充常见的 ClickHouse 类型映射
    "STRING": "VARCHAR", "FIXEDSTRING": "VARCHAR",
    "UINT8": "INTEGER", "UINT16": "INTEGER", "UINT32": "INTEGER", "UINT64": "INTEGER",
    "INT8": "INTEGER", "INT16": "INTEGER", "INT32": "INTEGER", "INT64": "INTEGER",
    "FLOAT32": "DOUBLE", "FLOAT64": "DOUBLE",
    "DATETIME64": "TIMESTAMP",
}


# ── 建立 ClickHouse 连接 ──────────────────────────────────────
def test_connection():
    if DB_CONFIG["protocol"] == "http":
        # HTTP 协议连接测试
        url = f"http://{DB_CONFIG['host']}:{DB_CONFIG['port']}/ping"
        try:
            response = requests.get(
                url,
                auth=HTTPBasicAuth(DB_CONFIG["user"], DB_CONFIG["password"]),
                timeout=5
            )
            if response.status_code == 200:
                return True
            return False
        except Exception as e:
            print(f"HTTP连接测试失败: {e}")
            return False
    else:
        # 原生协议连接测试（使用 clickhouse-driver）
        try:
            from clickhouse_driver import Client
            client = Client(
                host=DB_CONFIG["host"],
                port=DB_CONFIG["port"],
                user=DB_CONFIG["user"],
                password=DB_CONFIG["password"],
                database=DB_CONFIG["database"]
            )
            client.execute("SELECT 1")
            client.disconnect()
            return True
        except Exception as e:
            print(f"原生协议连接测试失败: {e}")
            return False


if not test_connection():
    raise Exception(f"无法连接到 ClickHouse 服务器 ({DB_CONFIG['host']}:{DB_CONFIG['port']})")


# ── 使用 HTTP 协议获取表和列信息 ──────────────────────────────────────
def get_tables_info_http():
    base_url = f"http://{DB_CONFIG['host']}:{DB_CONFIG['port']}"
    auth = HTTPBasicAuth(DB_CONFIG["user"], DB_CONFIG["password"])

    # 获取表列表
    try:
        response = requests.get(
            f"{base_url}/?query=SELECT+name+FROM+system.tables+WHERE+database='{DB_CONFIG['database']}'",
            auth=auth,
            timeout=10
        )
        print(f"获取表列表响应状态: {response.status_code}")
        print(f"响应内容: {response.text[:200]}...")

        if response.status_code != 200:
            raise Exception(f"获取表列表失败: {response.text}")

        # 直接处理文本响应
        tables = response.text.strip().split('\n')
        print(f"找到的表: {tables}")
    except Exception as e:
        raise Exception(f"获取表列表失败: {e}")

    # 获取表注释
    table_comments = {}
    try:
        response = requests.get(
            f"{base_url}/?query=SELECT+name,+comment+FROM+system.tables+WHERE+database='{DB_CONFIG['database']}'",
            auth=auth,
            timeout=10
        )
        if response.status_code == 200:
            print("表注释响应内容:", response.text[:200])
            # 处理制表符分隔的响应
            lines = response.text.strip().split('\n')
            if len(lines) > 1 and '\t' in lines[0]:
                headers = lines[0].split('\t')
                print(f"表注释列名: {headers}")
                for line in lines[1:]:
                    values = line.split('\t')
                    print(f"表注释行数据: {values}")
                    if len(values) >= 2:
                        name = values[0]
                        comment = values[1]
                        if comment:
                            table_comments[name] = comment
    except Exception as e:
        print(f"获取表注释失败: {e}")

    # 获取列信息和注释
    columns_info = {}
    col_comments = {}
    try:
        response = requests.get(
            f"{base_url}/?query=SELECT+database,+table,+name,+type,+comment+FROM+system.columns+WHERE+database='{DB_CONFIG['database']}'",
            auth=auth,
            timeout=10
        )
        print(f"获取列信息响应状态: {response.status_code}")
        print(f"列信息响应内容: {response.text[:200]}...")

        if response.status_code == 200:
            lines = response.text.strip().split('\n')
            print(f"列信息总行数: {len(lines)}")

            # 检查第一行是否是列名
            if len(lines) > 0:
                first_line = lines[0].split('\t')
                print(f"第一行数据: {first_line}")

                # 判断第一行是否是列名（通过检查是否包含已知的列名）
                is_header = False
                if len(first_line) >= 5:
                    # 检查是否包含我们期望的列名
                    expected_columns = ['database', 'table', 'name', 'type', 'comment']
                    is_header = all(col in first_line for col in expected_columns)

                print(f"第一行是否是列名: {is_header}")

                # 根据第一行是否是列名来决定从哪一行开始处理数据
                start_index = 1 if is_header else 0

                for i, line in enumerate(lines[start_index:], start_index):
                    values = line.split('\t')
                    print(f"第{i}行列数据: {values}")

                    # 根据第一行是否是列名来决定如何解析数据
                    if is_header:
                        # 第一行是列名，使用列名来映射
                        if len(values) >= 5:
                            database = values[0]
                            table_name = values[1]
                            column_name = values[2]
                            column_type = values[3]
                            column_comment = values[4] if len(values) > 4 else ""

                            if table_name:
                                if table_name not in columns_info:
                                    columns_info[table_name] = []
                                columns_info[table_name].append({
                                    "name": column_name,
                                    "type": column_type,
                                    "nullable": True
                                })
                                if column_comment:
                                    if table_name not in col_comments:
                                        col_comments[table_name] = {}
                                    col_comments[table_name][column_name] = column_comment
                    else:
                        # 第一行不是列名，直接按位置解析
                        if len(values) >= 5:
                            database = values[0]
                            table_name = values[1]
                            column_name = values[2]
                            column_type = values[3]
                            column_comment = values[4] if len(values) > 4 else ""

                            if table_name:
                                if table_name not in columns_info:
                                    columns_info[table_name] = []
                                columns_info[table_name].append({
                                    "name": column_name,
                                    "type": column_type,
                                    "nullable": True
                                })
                                if column_comment:
                                    if table_name not in col_comments:
                                        col_comments[table_name] = {}
                                    col_comments[table_name][column_name] = column_comment
    except Exception as e:
        print(f"获取列信息失败: {e}")
        print(f"错误详情: {str(e)}")

    return tables, table_comments, columns_info, col_comments


# ── 生成模型文件 ──────────────────────────────────────
def write_model(table_name, columns, pk_name, **extra):
    model_name = table_name.lower() + ""
    model = {
        "name": model_name,
        "table_reference": {"table": table_name},
        "columns": columns,
        "cached": False,
    }
    if pk_name:
        model["primary_key"] = pk_name
    model.update(extra)

    d = os.path.join(MODELS_DIR, model_name)
    os.makedirs(d, exist_ok=True)
    meta_path = os.path.join(d, "metadata.yml")
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(f"# {table_name} table\n")
        yaml.dump(model, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── 使用 HTTP 协议获取外键信息 ──────────────────────────────────────
def get_foreign_keys_http():
    relationships = []
    seen_pairs = set()

    base_url = f"http://{DB_CONFIG['host']}:{DB_CONFIG['port']}"
    auth = HTTPBasicAuth(DB_CONFIG["user"], DB_CONFIG["password"])

    try:
        response = requests.get(
            f"{base_url}/?query=SELECT+source_database,+source_table,+source_column,+"
            "destination_database,+destination_table,+destination_column+"
            "FROM+system.foreign_keys+WHERE+source_database='{DB_CONFIG['database']}'",
            auth=auth,
            timeout=10
        )

        if response.status_code == 200:
            lines = response.text.strip().split('\n')
            if len(lines) > 1 and '\t' in lines[0]:
                headers = lines[0].split('\t')
                for line in lines[1:]:
                    values = line.split('\t')
                    if len(values) >= 6:
                        row = {}
                        for i, value in enumerate(values):
                            if i < len(headers):
                                row[headers[i]] = value

                        source_db = row.get("source_database")
                        source_table = row.get("source_table")
                        source_col = row.get("source_column")
                        dest_db = row.get("destination_database")
                        dest_table = row.get("destination_table")
                        dest_col = row.get("destination_column")

                        if all([source_db, source_table, source_col, dest_db, dest_table, dest_col]):
                            source_model = source_table.lower()
                            target_model = dest_table.lower()

                            if source_model == target_model:
                                continue

                            key = tuple(sorted([source_model, target_model]))
                            if key not in seen_pairs:
                                seen_pairs.add(key)
                                relationships.append({
                                    "name": f"{source_model}_{target_model}",
                                    "models": [source_model, target_model],
                                    "join_type": "MANY_TO_ONE",
                                    "condition": f"{source_model}.{source_col} = {target_model}.{dest_col}",
                                })
    except Exception as e:
        print(f"获取外键信息失败: {e}")

    return relationships


# ── 主程序 ──────────────────────────────────────
tables, table_comments, columns_info, col_comments = get_tables_info_http()

# 处理每个表
for table in tables:
    columns = columns_info.get(table, [])

    # 获取主键信息
    pk_name = None
    try:
        response = requests.get(
            f"http://{DB_CONFIG['host']}:{DB_CONFIG['port']}/?query="
            "SELECT+name+FROM+system.keys+WHERE+database='{DB_CONFIG['database']}'"
            "+AND+table='{table}'+AND+type='PRIMARY'",
            auth=HTTPBasicAuth(DB_CONFIG["user"], DB_CONFIG["password"]),
            timeout=10
        )
        if response.status_code == 200:
            lines = response.text.strip().split('\n')
            if len(lines) > 1 and '\t' in lines[0]:
                headers = lines[0].split('\t')
                for line in lines[1:]:
                    values = line.split('\t')
                    if len(values) >= 1:
                        pk_name = values[0]
                        break
    except Exception as e:
        print(f"获取主键信息失败: {e}")

    # 转换列类型
    processed_columns = []
    for col in columns:
        raw_type = str(col["type"]).upper().split("(")[0]
        wren_type = TYPE_MAP.get(raw_type, "VARCHAR")
        processed_columns.append({
            "name": col["name"],
            "type": wren_type,
            "is_calculated": False,
            "not_null": False,  # ClickHouse 默认允许 NULL
            "is_primary_key": col["name"] == pk_name,
            "properties": {"description": col_comments.get(table, {}).get(col["name"], "")}
        })

    # 写入模型文件
    extra = {}
    if table in table_comments:
        extra["properties"] = {"description": table_comments[table]}
    write_model(table, processed_columns, pk_name, **extra)

    print(f"  {table.lower()}/ ({len(columns)} cols, PK={pk_name})")

# 生成关系文件
relationships = get_foreign_keys_http()

with open(RELATIONSHIPS_FILE, "w", encoding="utf-8") as f:
    f.write("# Auto-generated from database foreign keys + naming convention inference\n")
    yaml.dump({"relationships": relationships}, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

print(f"\nGenerated {len(relationships)} relationships in {RELATIONSHIPS_FILE}/")
for r in relationships:
    print(f"  {r['name']}: {r['condition']}")

print("\nNext: wren context build")
