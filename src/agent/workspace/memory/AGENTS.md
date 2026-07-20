# NL2SQL Agent 参考手册

本手册供 SQL-of-Thought 编排器在需要时查阅，包含详细的操作参考、速查表和示例。

---

## 1. 技能清单与加载时机

### 1.1 技能清单

| 技能名称 | 对应流程步骤 | 加载时机 | 推荐模型类型 |
|----------|-------------|----------|-------------|
| `sql-of-thought` | 编排器 | NL2SQL 问题被识别时首先加载 | 任意 |
| `wrenai` | Phase 0 / Step 5 | Schema 预处理或 SQL 验证时按需加载 | 非推理模型 |
| `nl2sql-schema-linking` | Step 1 | Phase 2 开始时加载 | 推理模型 |
| `nl2sql-subproblem` | Step 2 | Step 1 完成后加载 | 非推理模型 |
| `nl2sql-query-plan` | Step 3 | Step 2 完成后加载 | 推理模型 |
| `nl2sql-sql-generation` | Step 4 | Step 3 完成后加载 | 非推理模型 |
| `nl2sql-correction` | Step 7-8 | 仅当 SQL 执行失败时加载 | 推理（Plan）+ 非推理（SQL） |

### 1.2 引用文件加载策略

编排器技能和纠错技能各自有引用文件（位于 `references/` 目录）：

| 引用文件 | 所属技能 | 加载时机 |
|----------|---------|----------|
| `error-taxonomy.md`（编排器版） | `sql-of-thought` | 需要了解完整错误分类法时 |
| `pipeline-flow.md` | `sql-of-thought` | 需要查阅完整流程决策逻辑时 |
| `design-principles.md` | `sql-of-thought` | 需要回顾设计原则和失败消融教训时 |
| `hybrid-model-strategy.md` | `sql-of-thought` | 需要决定模型分配策略时 |
| `error-taxonomy.md`（纠错版） | `nl2sql-correction` | 进入纠错循环时自动加载 |

---

## 2. 错误分类法速查

### 2.1 完整分类表

| 类别 | 编码前缀 | 包含子类 | 诊断优先顺序 |
|------|---------|---------|-------------|
| 语法错误 | `syntax` | `sql_syntax_error`, `invalid_alias` | **1st**（最容易检测） |
| Schema 链接 | `schema_link` | `table_missing`, `col_missing`, `ambiguous_col`, `incorrect_foreign_key` | **2nd** |
| Join 错误 | `join` | `join_missing`, `join_wrong_type`, `extra_table`, `incorrect_col` | **3rd** |
| 过滤错误 | `filter` | `where_missing`, `condition_wrong_col`, `condition_type_mismatch` | **4th** |
| 聚合错误 | `aggregation` | `agg_no_groupby`, `groupby_missing_col`, `having_without_groupby`, `having_incorrect`, `having_vs_where` | **5th** |
| 值错误 | `value` | `hardcoded_value`, `value_format_wrong` | **6th** |
| 子查询错误 | `subquery` | `unused_subquery`, `subquery_missing`, `subquery_correlation_error` | **7th** |
| 集合操作错误 | `set_ops` | `union_missing`, `intersect_missing`, `except_missing` | **8th** |
| 其他问题 | `other` | `order_by_missing`, `limit_missing`, `duplicate_select`, `unsupported_function`, `extra_values_selected` | **9th** |

### 2.2 诊断流程

按上表优先级从 1 到 9 扫描，为每个发现的错误分配精简编码，识别主根因和次生错误。

---

## 3. MCP 工具手册（`wren context show-compiler`）

### 3.1 MCP 工具速查

| MCP 工具 | 使用阶段 | 用途 | 是否需要 LLM |
|----------|---------|------|-------------|
| `wren context show` | Phase 1 | 加载 Schema 文档（DDL、数据字典） | 否 |
| `wren context build` | Phase 1 | 构建 MDL（models/* → target/mdl.json） | 是 |
| `wren context show` | Step 1 | 语义搜索相关表/列，**返回完整页面内容** | 是 |
| `wren context show` | Step 1 | 自然语言问答（如 FK 关系查询） | 是 |
| `wren context show` | — | 按 slug 读取页面（**仅搜 concepts/ 和 queries/**） | 否 |
| `wren context show` | Phase 1 | 检查编译状态、陈旧/孤立页面 | 否 |
| `wren context validate` | Phase 1 | 验证 MDL 完整性 | 否 |

### 3.2 wren context show 使用要点（关键）

**`wren context show` 返回的 `pages[].body` 已包含页面的完整 markdown 内容**（所有列定义、字段类型、外键关系等），无需额外调用 `wren context show`。

```json
// wren context show 返回结构
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

**常见错误：** 在 `wren context show` 之后再调用 `wren context show({ slug: pages[0].slug })`。
- `wren context show` 硬编码搜索目录为 `[concepts/, queries/]`，**不搜索 `entities/`**
- 当 `wren context show` 返回 entity 页面时（如 `entities/customer-表`），`wren context show` 会报 `Page not found`
- **正确做法：** 直接使用 `wren context show` 返回的 `pages[].body`，不需要二次读取

### 3.3 核心使用原则

- **MDL 复用：** 一次构建，多次查询。同一数据库的所有 NL2SQL 请求共享同一个 MDL。
- **MDL 更新判断：** 如果 Schema 有变化，先调用 `wren context validate` 确认是否需要重新 `python skills/sql-of-thought/scripts/gen_models_mysql.py` + `wren context build`。
- **所有 Schema 引用都要有来源：** Schema Linking Agent 输出中应标注 wren context show 页面 slug 作为来源引用。
- **不要用 wren context show 二次读取：** `wren context show` 已返回完整内容。`wren context show` 仅在已知页面在 `concepts/` 或 `queries/` 下且只需读单页时使用。

---


---

## 4. WrenAI 语义层工具手册

### 4.1 WrenAI 工具速查

| 命令 | 使用阶段 | 用途 |
|------|---------|------|
| `wren generate-mdl` | Phase 0 | 从数据库 Schema 自动生成 MDL 语义模型 |
| `wren enrich-context` | Phase 0 | 为 MDL 添加业务上下文（枚举值、单位、指标） |
| `wren context build` | Phase 0 | 构建 MDL 项目 |
| `wren context show` | Step 1 | 查看 MDL 语义模型（增强 Schema Linking） |
| `wren dry-plan --sql '...'` | Step 5 | 干运行验证 SQL（仅解析，不访问 DB） |
| `wren memory recall` | Step 7 | 检索相似历史正确查询（辅助纠错） |

### 4.2 MDL 与 wren context show 的关系

| 维度 | wren context show | WrenAI MDL |
|------|---------|-----------|
| 关注点 | Schema 结构文档 | 业务语义层 |
| 内容 | 表名、列名、FK 关系、数据模式 | 枚举值含义、列单位、指标定义 |
| 格式 | YAML/JSON 语义模型 | 结构化数据 |
| 生成方式 | LLM 编译源代码 | CLI 自动生成 + 人工丰富 |
| 在流水线中 | Schema Linking 的主力 | Schema Linking 的增强上下文 |

两者**互补不冲突**：wren context show 回答"有什么表和列"，MDL 回答"这些表和列在业务上代表什么"。

### 4.3 核心使用原则

- **首次使用必做 Phase 0：** 新数据库需要 `generate-mdl` → `enrich-context` 建立语义层
- **日常查询推荐 dry-plan：** 在 SQL 执行前用 `wren dry-plan` 验证，避免无效执行
- **MDL 不替代 wren context show：** Schema Linking 仍以 wren context show 为主，MDL 提供业务语义补充

## 5. 混合模型策略

### 4.1 模型分配表

| Agent | 推理需求 | 推荐模型 | 备选模型 |
|-------|---------|---------|---------|
| Schema Linking | 高 | Claude Opus / GPT-5 | Claude Sonnet |
| Query Plan | 高 | Claude Opus / GPT-5 | Claude Sonnet |
| Correction Plan | 高 | Claude Opus / GPT-5 | Claude Sonnet |
| Subproblem | 低 | GPT-4o | Claude Haiku |
| SQL Generation | 低 | GPT-4o | Claude Haiku |
| Correction SQL | 低 | GPT-4o | Claude Haiku |

### 4.2 策略配置速查

| 配置 | 推理 Agent | 生成 Agent | 预估 EA | 适用场景 |
|------|-----------|-----------|---------|---------|
| 最高精度 | Claude Opus | Claude Opus | ~95% | 对准确率要求极高的场景 |
| 建议混合 | Claude Opus | GPT-4o | ~85% | 成本与精度平衡（**推荐**） |
| 预算优先 | GPT-4o-mini | GPT-4o-mini | ~87% | 预算有限但可接受略低精度 |
| 不推荐 | GPT-3.5 | GPT-3.5 | ~67% | 精度过低 |
| 不推荐 | Llama 3.1 8B | Llama 3.1 8B | ~45% | 严重幻觉 |

---

## 6. 输出规范模板

### 5.1 成功时

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

### 5.2 失败并经过纠错时

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

| 错误类型 | 处理策略 |
|---------|---------|
| 语法错误（`syntax`） | 直接根据 DB 引擎错误信息修正，通常 1 次即可修复 |
| Schema 链接错误（`schema_link`） | 重新检查 wren context show 中 FK 定义，验证列名拼写 |
| Join/聚合逻辑错误 | 需 CoT 诊断，检查 JOIN 条件和 GROUP BY 是否正确 |
| 意图不匹配（逻辑正确但结果不对） | 重新分析 NL 问题，对比 Query Plan 与实际 SQL |

### 8.2 纠错循环终止条件

- 相同 `error_code` 在连续 2 次尝试中出现 → 判定"卡住"，终止并说明原因
- 3 次尝试上限用完 → 终止，输出最后生成的 SQL 和全部诊断历史

### 8.3 降级策略

当 wren context show 不可用时：

1. 提示用户手动提供相关表的 DDL 或 Schema 描述
2. 询问用户涉及的表名和列名
3. **不接受模糊的 Schema 信息直接生成 SQL**
