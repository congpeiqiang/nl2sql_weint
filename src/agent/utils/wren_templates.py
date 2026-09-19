"""Wren 语义库业务知识模板 — 供新建语义库流程使用。

每个模板函数返回模板内容字符串，消费方（API / 前端）按需组合。

⚠ 格式约束（wren v5）：`knowledge/` 下**只有 `.md` 会被消费**，且各子目录用途固定：
  rules/*.md                    → 注入业务规则（按文件名排序拼接）
  sql/*.md                      → NL↔SQL 示例对，正文是 YAML front-matter（nl + sql）
  glossary|metrics|caveats/*.md → 供 get_all_knowledge 读取的说明性 Markdown
写成 .yml 或自由结构（如 `glossary.yml`）wren 读不到，等于没写。
"""

from __future__ import annotations


def knowledge_yml() -> str:
    # wren 只读 schema_version 这个键；description 仅作人类可读说明
    return "schema_version: 1\ndescription: 业务知识与规则（由平台「编辑知识」维护）\n"


def glossary_template() -> str:
    return """# 术语表

把业务术语映射到字段/表，供模型理解用户口径。

## 示例
- **设备**：指 `dim_device.device_code`，不是 `device_name`
- **在职**：`emp_status = 'A'`
"""


def metrics_template() -> str:
    return """# 指标定义

写清业务指标的算法与口径，避免模型自行猜测。

## 示例
- **人均产出**：`SUM(output_qty) / COUNT(DISTINCT emp_id)`
- **完成率**：完成量 / 计划量，分母为 0 时返回 NULL
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
    # 正文即 front-matter：nl 是自然语言问题，sql 是参考答案（块标量保留原格式）
    return """---
nl: 各迭代的需求工作量完成情况
sql: |
  SELECT iteration_name, story_workload, story_complete_workload
  FROM do_iteration_burndown
  ORDER BY story_workload DESC
  LIMIT 10
datasource: mysql
tags:
  - 迭代
source: user
---

# 各迭代的需求工作量完成情况
"""


def caveats_template() -> str:
    return """# 注意事项

记录容易踩坑的查询约束（数据延迟、口径陷阱、字段废弃等）。

## 示例
- 报工表 `do_worklog` 每日 03:00 同步，当天数据可能缺失
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
        "knowledge/glossary/example.md": glossary_template(),
        "knowledge/metrics/example.md": metrics_template(),
        "knowledge/rules/example.md": rules_general_md(),
        "knowledge/sql/example.md": sql_template(),
        "knowledge/caveats/example.md": caveats_template(),
        "views/example_view/metadata.yml": view_metadata_template(),
        "views/example_view/sql.yml": view_sql_template(),
        "cubes/example_cube/metadata.yml": cube_metadata_template(),
    }