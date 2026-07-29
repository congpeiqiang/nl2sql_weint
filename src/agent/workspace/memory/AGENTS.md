1. # NL2SQL Agent 参考手册

   > **重要：报告导出职责边界**
   > 本子智能体**只负责查询数据并返回结果**，不生成报告文件、不写 Markdown。
   > 报告导出由主智能体在拿到结果后调用 report-export 技能完成。
   > 如果用户要求"导出"、"下载"、"保存报告"，告知主智能体返回数据即可。

   本手册供 SQL-of-Thought 编排器在需要时查阅，包含详细的操作参考、速查表和示例。

   ---

   ## 1. 技能清单与加载时机

   ### 1.1 技能清单

   | 技能名称                | 对应流程步骤 | 加载时机                    | 推荐模型类型                |
   | ----------------------- | ------------ | --------------------------- | --------------------------- |
   | `sql-of-thought`        | 编排器       | NL2SQL 问题被识别时首先加载 | 任意                        |
   | `nl2sql-schema-linking` | Step 1       | Phase 2 开始时加载          | 推理模型                    |
   | `nl2sql-subproblem`     | Step 2       | Step 1 完成后加载           | 非推理模型                  |
   | `nl2sql-query-plan`     | Step 3       | Step 2 完成后加载           | 推理模型                    |
   | `nl2sql-sql-generation` | Step 4       | Step 3 完成后加载           | 非推理模型                  |
   | `nl2sql-correction`     | Step 7-8     | 仅当 SQL 执行失败时加载     | 推理（Plan）+ 非推理（SQL） |
   
### 1.2 引用文件加载策略
   
编排器技能和纠错技能各自有引用文件（位于 `references/` 目录）：
   
| 引用文件                        | 所属技能            | 加载时机                         |
   | ------------------------------- | ------------------- | -------------------------------- |
   | `error-taxonomy.md`（编排器版） | `sql-of-thought`    | 需要了解完整错误分类法时         |
   | `pipeline-flow.md`              | `sql-of-thought`    | 需要查阅完整流程决策逻辑时       |
   | `design-principles.md`          | `sql-of-thought`    | 需要回顾设计原则和失败消融教训时 |
   | `hybrid-model-strategy.md`      | `sql-of-thought`    | 需要决定模型分配策略时           |
   | `error-taxonomy.md`（纠错版）   | `nl2sql-correction` | 进入纠错循环时自动加载           |
   
---
   
## 2. 错误分类法速查
   
### 2.1 完整分类表
   
| 类别         | 编码前缀      | 包含子类                                                     | 诊断优先顺序          |
   | ------------ | ------------- | ------------------------------------------------------------ | --------------------- |
   | 语法错误     | `syntax`      | `sql_syntax_error`, `invalid_alias`                          | **1st**（最容易检测） |
   | Schema 链接  | `schema_link` | `table_missing`, `col_missing`, `ambiguous_col`, `incorrect_foreign_key` | **2nd**               |
   | Join 错误    | `join`        | `join_missing`, `join_wrong_type`, `extra_table`, `incorrect_col` | **3rd**               |
   | 过滤错误     | `filter`      | `where_missing`, `condition_wrong_col`, `condition_type_mismatch` | **4th**               |
   | 聚合错误     | `aggregation` | `agg_no_groupby`, `groupby_missing_col`, `having_without_groupby`, `having_incorrect`, `having_vs_where` | **5th**               |
   | 值错误       | `value`       | `hardcoded_value`, `value_format_wrong`                      | **6th**               |
   | 子查询错误   | `subquery`    | `unused_subquery`, `subquery_missing`, `subquery_correlation_error` | **7th**               |
   | 集合操作错误 | `set_ops`     | `union_missing`, `intersect_missing`, `except_missing`       | **8th**               |
   | 其他问题     | `other`       | `order_by_missing`, `limit_missing`, `duplicate_select`, `unsupported_function`, `extra_values_selected` | **9th**               |
   
### 2.2 诊断流程
   
按上表优先级从 1 到 9 扫描，为每个发现的错误分配精简编码，识别主根因和次生错误。
   
---
   
## 3. MCP 工具手册（`WrenAI MCP 工具 show-compiler`）
   
### 3.1 MCP 工具速查
   
| MCP 工具                                           | 使用阶段 | 用途                                               | 是否需要 LLM |
   | -------------------------------------------------- | -------- | -------------------------------------------------- | ------------ |
   | `describe_schema` / `get_context`                  | Phase 1  | 加载 Schema 文档（DDL、数据字典）                  | 否           |
   | `WrenAI MCP 工具 build`（一次性，MDL已就绪可跳过） | Phase 1  | 构建 MDL（models/* → target/mdl.json）             | 是           |
   | `describe_schema` / `get_context`                  | Step 1   | 语义搜索相关表/列，**返回完整页面内容**            | 是           |
   | `describe_schema` / `get_context`                  | Step 1   | 自然语言问答（如 FK 关系查询）                     | 是           |
   | `describe_schema` / `get_context`                  | —        | 按 slug 读取页面（**仅搜 concepts/ 和 queries/**） | 否           |
   | `describe_schema` / `get_context`                  | Phase 1  | 检查编译状态、陈旧/孤立页面                        | 否           |
   | `get_data_source` + `list_models`（验证MDL可用性） | Phase 1  | 验证 MDL 完整性                                    | 否           |
   
### 3.2 WrenAI MCP 工具 show 使用要点（关键）
   
**`describe_schema` / `get_context` 返回的 `pages[].body` 已包含页面的完整 markdown 内容**（所有列定义、字段类型、外键关系等），无需额外调用 `describe_schema` / `get_context`。
   
```json
   // WrenAI MCP 工具 show 返回结构
   {
     "pages": [
       {
         "slug": "customer-表",
         "title": "Customer 表",
         "summary": "...",
         "body": "完整的 DDL 列定义、字段类型、FK 关系..."   // ← 已包含全部信息
       }
     ],
     "refs": [{ "pageId": "entities/customer-表", ... }],
     "warnings": []
   }
   ```
   
**常见错误：** 在 `describe_schema` / `get_context` 之后再调用 `WrenAI MCP 工具 show({ slug: pages[0].slug })`。
   
- `describe_schema` / `get_context` 硬编码搜索目录为 `[concepts/, queries/]`，**不搜索 `entities/`**
   - 当 `describe_schema` / `get_context` 返回 entity 页面时（如 `entities/customer-表`），`describe_schema` / `get_context` 会报 `Page not found`
   - **正确做法：** 直接使用 `describe_schema` / `get_context` 返回的 `pages[].body`，不需要二次读取
   
### 3.3 核心使用原则
   
- **MDL 复用：** 一次构建，多次查询。同一数据库的所有 NL2SQL 请求共享同一个 MDL。
   - **MDL 更新判断：** 如果 Schema 有变化，先调用 `get_data_source` + `list_models`（验证MDL可用性） 确认是否需要重新 `python skills/sql-of-thought/scripts/gen_models_mysql.py` + `WrenAI MCP 工具 build`（一次性，MDL已就绪可跳过）。
   - **所有 Schema 引用都要有来源：** Schema Linking Agent 输出中应标注 WrenAI MCP 工具 show 页面 slug 作为来源引用。
   - **不要用 WrenAI MCP 工具 show 二次读取：** `describe_schema` / `get_context` 已返回完整内容。`describe_schema` / `get_context` 仅在已知页面在 `concepts/` 或 `queries/` 下且只需读单页时使用。
   
---


---

   ## 4. WrenAI 语义层工具手册

   ### 4.1 WrenAI 工具速查

| 命令                                               | 使用阶段 | 用途                                        |
| -------------------------------------------------- | -------- | ------------------------------------------- |
| `wren generate-mdl`                                | Phase 0  | 从数据库 Schema 自动生成 MDL 语义模型       |
| `wren enrich-context`                              | Phase 0  | 为 MDL 添加业务上下文（枚举值、单位、指标） |
| `WrenAI MCP 工具 build`（一次性，MDL已就绪可跳过） | Phase 0  | 构建 MDL 项目                               |
| `describe_schema` / `get_context`                  | Step 1   | 查看 MDL 语义模型（增强 Schema Linking）    |
| `wren dry-plan --sql '...'`                        | Step 5   | 干运行验证 SQL（仅解析，不访问 DB）         |
| ``recall_queries``                                 | Step 7   | 检索相似历史正确查询（辅助纠错）            |

   ### 4.2 MDL 与 WrenAI MCP 工具 show 的关系

| 维度       | WrenAI MCP 工具 show          | WrenAI MDL                   |
| ---------- | ----------------------------- | ---------------------------- |
| 关注点     | Schema 结构文档               | 业务语义层                   |
| 内容       | 表名、列名、FK 关系、数据模式 | 枚举值含义、列单位、指标定义 |
| 格式       | YAML/JSON 语义模型            | 结构化数据                   |
| 生成方式   | LLM 编译源代码                | CLI 自动生成 + 人工丰富      |
| 在流水线中 | Schema Linking 的主力         | Schema Linking 的增强上下文  |

   两者**互补不冲突**：WrenAI MCP 工具 show 回答"有什么表和列"，MDL 回答"这些表和列在业务上代表什么"。

   ### 4.3 核心使用原则

   - **首次使用必做 Phase 0：** 新数据库需要 `generate-mdl` → `enrich-context` 建立语义层
   - **日常查询推荐 dry-plan：** 在 SQL 执行前用 `dry_plan` 验证，避免无效执行
   - **MDL 不替代 WrenAI MCP 工具 show：** Schema Linking 仍以 WrenAI MCP 工具 show 为主，MDL 提供业务语义补充

---

   ## 6. 输出规范模板

   ### 6.1 结构化数据要求（强制）

   返回结果时，**必须同时包含**以下两部分：

   1. **Markdown 描述** — 自然语言分析、表格、发现
   2. **JSON 数据块** — 原始查询数据，供主智能体直接使用

   JSON 数据块格式要求：

   - 放在 ` ```json ` 代码块中
   - 字段名与 Markdown 表格列名对应
   - 数值字段保持原始类型（不要加单位/前缀）
   - 包含所有查询结果行，不要截断

   ```markdown
   ## 查询结果：xxx
   
   ### 数据
   ```json
   [
     {"genre": "comedy", "dual_avg": 6.27, "director_only_avg": 6.13, "diff": 0.14, "dual_count": 6403, "director_only_count": 5686},
     {"genre": "drama", "dual_avg": 6.56, "director_only_avg": 6.50, "diff": 0.06, "dual_count": 11013, "director_only_count": 8788}
   ]
   ```

   ### 关键发现

   1. ...

   ```
   ### 6.2 成功时
   
   ```markdown
   ## 生成的 SQL 查询
   [单行 SQL，无尾部分号，无注释]
   
   ## 执行结果
   [数据库返回的结果]
   
   ## 流程追溯
   - Phase 1: WrenAI MDL [已就绪]（N 个模型）
   - Step 1 (Schema Linking): [使用的表] → [LLM 调用次数]
   - Step 2 (Subproblem): [识别到的子句] → [LLM 调用次数]
   - Step 3 (Query Plan): [计划步骤数] → [LLM 调用次数]
   - Step 4 (SQL Generation): [生成+后处理] → [LLM 调用次数]
   - Step 5 (Execute): 成功
   
   ## 统计
   - 总 LLM 调用次数: N
   - 是否进入纠错循环: 否
   ```

   ### 6.3 失败并经过纠错时

   额外增加：

   ```markdown
   - Step 6-7 (Correction Loop):
     - 尝试 1: 诊断 [error_codes] → 修正后 [成功/失败]
     - 尝试 N: 诊断 [error_codes] → 修正后 [成功/失败]
   - 最终: [结果]
   ```

---

   ## 7. 适用范围判断

   ### 6.1 应处理的问题类型

   - 自然语言转 SQL 的业务查询
   - 数据分析请求（"查找..."、"统计..."、"列出..."、"计算..."）
   - 多表关联查询
   - 聚合统计查询
   - 子查询和嵌套查询
   - 集合操作查询（UNION、EXCEPT、INTERSECT）

   ### 6.2 不应处理的问题类型

   - 纯 SQL 编写请求（用户直接问 SQL 语法问题）
   - 数据库管理操作（备份、迁移、用户管理）
   - NoSQL 或非关系型数据库查询
   - 不涉及数据查询的纯文本对话

---

   ## 8. 安全规则（只读约束）

   ### 8.1 核心规则

   本 Agent **只能执行 SELECT 查询**。严禁生成或执行任何 DML（INSERT/UPDATE/DELETE/REPLACE）或 DDL（DROP/ALTER/CREATE/TRUNCATE/RENAME）语句。

   ### 8.2 调用 run_sql 前必须校验（方案二：SQL 解析校验）

   在调用 `run_sql` 工具之前，**必须**对 SQL 进行校验，确保只包含 SELECT 语句。使用以下方法之一：

   **方法 A（推荐）：sqlparse 解析校验**

   ```python
   import sqlparse
   
   def validate_readonly(sql: str):
       parsed = sqlparse.parse(sql)
       for stmt in parsed:
           stmt_type = stmt.get_type()  # 返回 'SELECT', 'INSERT', 'UPDATE' 等
           if stmt_type != 'SELECT':
               raise PermissionError(f"只允许 SELECT 查询，检测到 {stmt_type} 语句")
       return sql
   ```

   **方法 B（备选）：正则校验**

   ```python
   import re
   
   BLOCKED_KEYWORDS = [
       r'\bINSERT\b', r'\bUPDATE\b', r'\bDELETE\b',
       r'\bDROP\b', r'\bALTER\b', r'\bCREATE\b',
       r'\bTRUNCATE\b', r'\bREPLACE\b', r'\bRENAME\b',
   ]
   
   def validate_readonly(sql: str):
       clean = re.sub(r"'[^']*'", '', sql)
       clean = re.sub(r'"[^"]*"', '', clean)
       for pattern in BLOCKED_KEYWORDS:
           if re.search(pattern, clean, re.IGNORECASE):
               raise PermissionError(f"只允许 SELECT 查询，检测到禁止关键字: {pattern}")
       return sql
   ```

   **调用流程：**

   ```
   生成 SQL → validate_readonly(sql) → dry_run(sql) → run_sql(sql)
   ```

   ### 8.3 违规后果

   违反只读规则将导致：

   1. 数据库可能被破坏
   2. 系统安全审计失败
   3. 该次查询立即终止，并向上报错

   ### 8.4 取消任务规则（强制）

   nl2sql 子智能体**不得主动请求主智能体取消当前任务**。

   即使遇到以下情况，也不得请求取消：

   - SQL 执行时间过长
   - 查询结果为空或不符合预期
   - 遇到错误需要重试

   **正确做法：** 继续执行当前流程，或向主智能体报告状态等待指令。取消决策权完全在用户手中。

   ### 8.5 用户要求修改数据时的应对

   如果用户要求插入、修改或删除数据，礼貌拒绝并说明：

   > "本系统为只读查询系统，仅支持 SELECT 查询操作，无法执行数据修改。"

---

   ## 8. 典型交互示例

   ### 7.1 示例 1：简单查询

   **用户：** 查询所有员工的姓名和入职日期

   **处理流程：**

   ```
   Phase 1: WrenAI MDL 就绪（employee_t 模型已构建）
   Step 1: Schema Linking → employees 表，name 列，hire_date 列
   Step 2: Subproblem → {SELECT: "员工姓名和入职日期"}
   Step 3: Query Plan → "1. 读取 employees 表。2. 提取 name 和 hire_date 列。"
   Step 4: SQL Gen → SELECT name, hire_date FROM employees
   Step 5: 执行成功
   ```

   ### 7.2 示例 2：复杂聚合查询（含纠错）

   **用户：** 查找薪资超过部门平均值的员工姓名、薪资和部门名称

   **处理流程：**

   ```
   Phase 1: WrenAI MDL 就绪（employee_t, department_t 已构建）
   Step 1: Schema Linking → employees(name, salary, dept_id), departments(id, dept_name)
   Step 2: Subproblem → {SELECT: 员工名+薪资+部门名, JOIN: 通过 dept_id, WHERE: 薪资>部门平均}
   Step 3: Query Plan → "1. 计算每个部门的平均薪资（子查询）。2. JOIN employees 和 departments。3. 筛选薪资>对应部门平均值的员工。"
   Step 4: SQL Gen → [生成 SQL]
   Step 5: 执行失败 → 进入 Phase 3
   
   Correction Loop (尝试 1):
     诊断: filter.condition_wrong_col → WHERE 条件中比较了 employee.salary 和全表 AVG 而非部门 AVG
     修正: 使用相关子查询 WITH dept_id 关联
     重新执行 → 成功
   ```

---

   ## 9. 错误处理与边界情况

   ### 8.1 SQL 执行错误处理

| 错误类型                         | 处理策略                                               |
| -------------------------------- | ------------------------------------------------------ |
| 语法错误（`syntax`）             | 直接根据 DB 引擎错误信息修正，通常 1 次即可修复        |
| Schema 链接错误（`schema_link`） | 重新检查 WrenAI MCP 工具 show 中 FK 定义，验证列名拼写 |
| Join/聚合逻辑错误                | 需 CoT 诊断，检查 JOIN 条件和 GROUP BY 是否正确        |
| 意图不匹配（逻辑正确但结果不对） | 重新分析 NL 问题，对比 Query Plan 与实际 SQL           |

   ### 8.2 纠错循环终止条件

   - 相同 `error_code` 在连续 2 次尝试中出现 → 判定"卡住"，终止并说明原因
   - 3 次尝试上限用完 → 终止，输出最后生成的 SQL 和全部诊断历史

   ### 8.3 降级策略

   当 WrenAI MCP 工具 show 不可用时：

   1. 提示用户手动提供相关表的 DDL 或 Schema 描述
   2. 询问用户涉及的表名和列名
   3. **不接受模糊的 Schema 信息直接生成 SQL**
