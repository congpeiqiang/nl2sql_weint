"""Wren 语义库业务知识模板 — 供新建语义库流程使用。

每个模板函数返回模板内容字符串，消费方（API / 前端）按需组合。
"""

from __future__ import annotations


def knowledge_yml() -> str:
    return "schema_version: 1\n"


def glossary_template() -> str:
    return """# 术语表 — 定义业务术语到字段的映射
# 文件名用英文小写+下划线，如 device_status.yml
name: ""               # 术语名称
description: ""        # 术语描述
synonyms: []           # 同义词列表，如 ["设备", "机器"]
mappings:              # 字段映射
  - model: ""          # 模型名（对应 models/ 下的目录名）
    column: ""         # 列名
"""


def metrics_template() -> str:
    return """# 指标定义 — 业务指标的计算方法
name: ""               # 指标名称
description: ""        # 指标描述
model: ""              # 来源模型（对应 models/ 下的目录名）
expression: ""         # 表达式，如 SUM(column_name)
dimensions: []         # 关联维度列名列表
"""


def rules_general_md() -> str:
    return """# 业务规则

在此添加面向 LLM 查询生成的业务规则或指南。

## 示例
- 查询时间范围时，默认使用最近 30 天
- 金额字段单位为"元"，需注意精度
- 当用户说"设备"时，默认指 device_code 字段
"""


def sql_template() -> str:
    return """# SQL 示例 — 典型查询场景的参考 SQL
name: ""               # 示例名称
description: ""        # 适用场景说明
sql: ""                # SQL 语句
"""


def caveats_template() -> str:
    return """# 注意事项
- 某事需要注意...
"""


def view_metadata_template() -> str:
    return """name: ""               # 视图名称
properties:
  description: ""      # 视图描述
"""


def view_sql_template() -> str:
    return "statement: >\n  SELECT ... FROM ... WHERE ...\n"


def cube_metadata_template() -> str:
    return """# 多维分析 Cube — OLAP 分析维度与指标
name: ""               # Cube 名称
dimensions:            # 维度
  - name: ""
    type: ""           # 如 time, string, number
    sql: ""            # SQL 表达式
measures:              # 指标
  - name: ""
    type: ""           # 如 sum, count, avg
    sql: ""            # SQL 表达式
"""


# ── 模板文件映射（API 返回用）─────────────────────────────────
def all_templates() -> dict[str, str]:
    """返回所有模板内容的 key-value 映射，key 为项目内相对路径。"""
    return {
        "knowledge/knowledge.yml": knowledge_yml(),
        "knowledge/glossary/example.yml": glossary_template(),
        "knowledge/metrics/example.yml": metrics_template(),
        "knowledge/rules/general.md": rules_general_md(),
        "knowledge/sql/example.yml": sql_template(),
        "knowledge/caveats/example.md": caveats_template(),
        "views/example_view/metadata.yml": view_metadata_template(),
        "views/example_view/sql.yml": view_sql_template(),
        "cubes/example_cube/metadata.yml": cube_metadata_template(),
    }