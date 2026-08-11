# llm-wiki-compiler MCP 工具使用手册

> Wiki 项目：`chinook_autoIncrement`（Chinook 数据库 Schema 知识库）
> 运行环境：Python 3.11+, Node.js 20+, npx

---

## 快速开始

```bash
cd D:\code_work_space\llm\nl2sql\src\test\llm_wiki_compiler_exec
python run_all_tools.py
```

> 注意：`compile_wiki` 和 `ingest_source` 已注释，按需取消注释。

---

## 目录结构对照

llm-wiki-compiler 在指定 root 目录下管理以下结构：

```
{root}/                          ← WIKI_ROOT
├── .llmwiki/
│   └── state.json               ← 编译状态（hash、时间戳）
├── sources/                     ← ingest_source 写入的原始文件
│   └── chinook-ddl.md
├── wiki/
│   ├── index.md                 ← compile_wiki 生成的页面索引
│   ├── log.md                   ← 操作日志
│   ├── MOC.md                   ← compile_wiki 生成的主题地图
│   ├── concepts/                ← 概念页面（compile_wiki 生成）
│   ├── entities/                ← 实体页面（compile_wiki 生成）
│   └── queries/                 ← 查询存档（query_wiki save=true 写入）
├── schema.md                    ← Wiki Schema 配置
└── purpose.md                   ← 项目目的描述
```

---

## 工具详解

每个工具按以下维度说明：
- **作用**：工具的核心功能
- **用途**：在 NL2SQL 或其他场景中的实际应用
- **输入参数**：调用时需要传的参数
- **输出参数**：返回结果中各字段的含义
- **llm-wiki 目录对照**：工具操作对应的文件目录

---

### 1. wiki_status — Wiki 状态概览

**作用：** 扫描整个 Wiki 工作区，汇总页面数量、来源数量、编译状态和健康信息。

**用途：**
- NL2SQL Phase 1 前置检查：判断 Schema Wiki 是否已编译就绪
- 日常维护：发现陈旧页面（来源变更后未更新）、孤立页面（源已删除）
- 健康监控：检查 `state.json` 是否损坏、Journal 是否完整



**输入参数：** 无

**llm-wiki 对应操作：** 扫描 `wiki/concepts/`、`wiki/queries/`、`sources/` 目录，读取 `.llmwiki/state.json`

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `pages.concepts` | int | concepts/ 下页面数 |
| `pages.queries` | int | queries/ 下页面数 |
| `pages.total` | int | 总页面数 |
| `sources` | int | sources/ 下文件数 |
| `lastCompiledAt` | string\|null | 最后编译时间 (ISO 8601) |
| `stalePages` | string[] | 来源变更但页面未更新的概念（上限 100） |
| `staleCount` | int | 陈旧页面真实总数 |
| `orphanedPages` | string[] | 所有源被删除的页面（上限 100） |
| `orphanedCount` | int | 孤立页面真实总数 |
| `stateStatus` | string | `ok` / `missing` / `corrupt` |
| `pendingChanges` | array | 待编译的来源变更列表 |
| `warnings` | array\|undefined | Journal 或 Embedding 健康警告 |

**调用示例：**
```python
await call_tool(session, "wiki_status")
```

---

### 2. read_page — 读取单个页面

**作用：** 按 slug 读取指定页面，返回 frontmatter 元数据和正文。

**用途：**
- 快速查阅某个已知概念页面的详细内容
- 调试：验证某个页面是否存在、内容是否正确

**限制：** 只搜索 `concepts/` 和 `queries/`，**不搜索** `entities/`

**输入参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `slug` | string | 是 | 页面标识（不含 .md 扩展名） |

**llm-wiki 对应操作：** 按顺序搜索 `wiki/concepts/{slug}.md` → `wiki/queries/{slug}.md`，解析 YAML frontmatter

> **重要限制：** 只搜索 `concepts/` 和 `queries/` 两个目录。`entities/` 下的页面（如 `chinook-database`、`track-表`）无法通过 `read_page` 读取。

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `slug` | string | 页面标识 |
| `title` | string | 页面标题（来自 frontmatter 或 slug） |
| `summary` | string | 页面摘要（来自 frontmatter.summary） |
| `body` | string | 页面正文（Markdown 格式，不含 frontmatter） |

**调用示例：**
```python
# 成功：concepts/ 下的页面
await call_tool(session, "read_page", {"slug": "发票定价模式"})

# 失败：entities/ 下的页面（read_page 搜不到）
await call_tool(session, "read_page", {"slug": "chinook-database"})
```

---

### 3. search_pages — 语义搜索页面

**作用：** 根据自然语言问题，语义搜索相关的 Wiki 页面，返回完整内容。

**用途：**
- NL2SQL Step 1 Schema Linking：根据用户问题找到相关的表/列定义
- **替代 read_page**：返回的 `pages[].body` 已含完整页面内容，无需二次调用 `read_page`
- 跨目录搜索：能搜到 `entities/` 下的页面（read_page 搜不到）

**在 NL2SQL 中的角色：** Schema Linking Agent 的第一步——用于表/列发现

**输入参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 自然语言查询，用于语义搜索排序 |

**llm-wiki 对应操作：**
1. 加载 Embedding Store → chunk 级语义检索 → page 级聚合
2. 通过 `page-registry` 从 LIVE 文件读取完整页面内容
3. 搜索范围包括所有 namespace 目录（concepts、entities、queries 等）

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `pages` | PageRecord[] | 匹配页面列表 |
| `pages[].slug` | string | 页面标识 |
| `pages[].title` | string | 页面标题 |
| `pages[].summary` | string | 页面摘要 |
| `pages[].body` | string | **完整页面内容**（含所有列定义、FK 关系） |
| `refs` | SelectedPageRef[] | 引用的页面引用 |
| `refs[].pageId` | string | 完整页面 ID（如 `entities/track-表`） |
| `refs[].kind` | string | 来源：`chunk` / `page` / `index` |
| `warnings` | array | 降级/健康警告（空数组 = 正常） |

> **关键：** `pages[].body` 已包含完整内容，无需再调 `read_page`。`search_pages` 能搜到 entities/ 下的页面（`read_page` 不能）。

**调用示例：**
```python
await call_tool(session, "search_pages", {
    "question": "artist 表和 album 表的结构和列定义"
})
```

---

### 4. query_wiki — 自然语言问答

**作用：** 先语义检索相关页面，再让 LLM 阅读页面内容，生成带引用的自然语言回答。

**用途：**
- NL2SQL Step 1 补充：查询表间关系（如"employee 和 customer 的 FK 关系是什么？"）
- 知识问答：对 Schema 的深层问题（如"Chinook 数据库有哪些数据质量问题？"）
- `save=true` 时：回答持久化为 `queries/` 页面，纳入 Wiki 知识库

**与 search_pages 的区别：** search_pages 返回原始页面内容，query_wiki 返回 LLM 生成的回答

**输入参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 自然语言问题 |
| `save` | boolean | 否 | 是否将回答保存为 `queries/` 下的页面 |
| `debug` | boolean | 否 | 是否返回检索 debug 信息 |

**llm-wiki 对应操作：**
1. 语义检索选中相关页面
2. LLM 阅读页面内容，生成带引用的回答
3. 如果 `save=true`，回答写入 `wiki/queries/<slug>.md`

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `answer` | string | LLM 生成的带引用的自然语言回答 |
| `citations` | array | 引用来源列表（wiki 页面 slug） |
| `pages_used` | array | 检索到的页面 slug 列表 |
| `debug` | object\|undefined | 仅在 debug=true 时返回（chunks、分数） |

**调用示例：**
```python
# 普通查询
await call_tool(session, "query_wiki", {
    "question": "Chinook 数据库中 employee 表和其他表有什么关系？"
})

# 查询并保存结果
await call_tool(session, "query_wiki", {
    "question": "Chinook 数据库的整体架构是怎样的？",
    "save": True
})
```

---

### 5. lint_wiki — 质量检查

**作用：** 并发运行 14 条检查规则，扫描所有 Wiki 页面，输出结构化诊断。

**用途：**
- NL2SQL Phase 1 验证：确保 Schema Wiki 没有断链、空页、矛盾等问题
- 质量审计：发现孤立页面（影响 search_pages 召回率）、陈旧页面（信息过时）
- CI/CD 集成：作为 Wiki 质量门禁

**输入参数：** 无

**llm-wiki 对应操作：** 并发运行 14 条检查规则，扫描所有 wiki 页面

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `errors` | int | 错误数量 |
| `warnings` | int | 警告数量 |
| `info` | int | 提示数量 |
| `results` | LintResult[] | 诊断详情列表 |
| `results[].severity` | string | `error` / `warning` / `info` |
| `results[].rule` | string | 规则名称 |
| `results[].file` | string | 涉及的文件路径 |
| `results[].message` | string | 诊断信息 |

**检查规则对照：**

| 规则 | 检查内容 | 对应目录 |
|------|---------|---------|
| `checkBrokenWikilinks` | 断开的 `[[内部链接]]` | wiki/**/*.md |
| `checkOrphanedPages` | 无入站链接的页面 | wiki/concepts/, entities/ |
| `checkMissingSummaries` | 缺少 summary 字段 | wiki/**/*.md |
| `checkEmptyPages` | 空内容页面 | wiki/**/*.md |
| `checkDuplicateConcepts` | 同名概念 | wiki/concepts/ |
| `checkBrokenCitations` | 断开的 source 引用 | wiki/**/*.md |
| `checkLowConfidencePages` | confidence: low | wiki/**/*.md |
| `checkContradictedPages` | 标记为矛盾 | wiki/**/*.md |
| `checkStalePages` | 来源变更后未更新 | wiki/concepts/ |
| `checkJournalHealth` | Journal 完整性 | .llmwiki/ |
| `checkPendingEmbeddings` | Embedding 待刷新 | .llmwiki/ |

**调用示例：**
```python
await call_tool(session, "lint_wiki")
```

---

### 6. ingest_source — 摄入来源文件

**作用：** 将外部来源（URL 或本地文件）复制到 `sources/` 目录，生成带 SHA256 的 frontmatter。

**用途：**
- NL2SQL Phase 1 第一步：将数据库 DDL 文件、数据字典、ER 图等导入 Wiki
- 支持 URL 和本地文件（.md、.txt、PDF、图片、YouTube transcript）

**在 NL2SQL 中的角色：** Schema 知识的入口——所有表/列/FK 信息通过此工具进入 Wiki

**输入参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `source` | string | 是 | URL (http/https) 或本地 `.md`/`.txt` 文件的绝对路径 |

**llm-wiki 对应操作：**
1. 检测输入类型（URL → 网络抓取，本地文件 → 内容嗅探）
2. 对 `.txt` 文件检测 transcript 特征（Speaker 标签、时间戳）
3. 保存到 `sources/` 目录，附带 YAML frontmatter（source_url, ingested 日期, sha256）
4. sha256 用于后续去重和变更检测

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `filename` | string | 保存的文件名（位于 sources/ 下） |
| `charCount` | int | 文件字符数 |
| `truncated` | boolean | 是否因超过大小限制被截断 |

**调用示例：**
```python
ddl_path = r"D:\code_work_space\llm\nl2sql\docs\chinook数据库创建语句.md"
await call_tool(session, "ingest_source", {"source": ddl_path})
```

---

### 7. compile_wiki — 编译 Wiki

**作用：** 运行 8 阶段增量编译流水线，从 `sources/` 中提取概念、生成 Wiki 页面、解析交叉链接、重建索引。

**用途：**
- NL2SQL Phase 1 核心步骤：将原始 DDL 编译为结构化的 concepts/entities/ 页面
- 增量更新：只处理变更的来源文件（通过 SHA256 对比）
- 孤立标记：来源删除后自动标记相关页面

**在 NL2SQL 中的角色：** 将 Schema 文档转化为 Agent 可语义搜索的知识库——编译后的页面是 Schema Linking 的数据源

**输入参数：** 无

**llm-wiki 对应操作（8 阶段流水线）：**

| 阶段 | 操作 | 影响目录 |
|------|------|---------|
| 1. 获取锁 | 获取 `.llmwiki/lock` | .llmwiki/ |
| 2. 恢复日志 | 从崩溃中恢复 | .llmwiki/ |
| 3. 变更检测 | 对比 source hash 与 state.json | sources/ → .llmwiki/state.json |
| 4. 概念提取 | LLM 两阶段 pipeline | （内存） |
| 5. 页面生成 | 生成 wiki 页面 | wiki/concepts/, wiki/entities/ |
| 6. 孤立标记 | 标记来源已删除的页面 | wiki/**/*.md |
| 7. 链接解析 | 解析 [[wikilinks]] | wiki/**/*.md |
| 8. 索引重建 | 生成 index.md, MOC.md | wiki/index.md, wiki/MOC.md |

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `compiled` | int | 新编译/更新的页面数 |
| `skipped` | int | 跳过的页面数（来源未变更） |
| `deleted` | int | 删除的页面数 |
| `concepts` | string[] | 提取的概念列表 |
| `pages` | string[] | 生成/更新的页面 slug 列表 |
| `errors` | array | 编译错误列表 |

**调用示例：**
```python
# ⚠️ 耗时操作，需要 LLM API，会修改 wiki 文件
await call_tool(session, "compile_wiki")
```

---

### 8. get_context_pack — Agent 上下文包

**作用：** 为指定任务构建 Agent 就绪的 evidence pack，包含主页面、语义 chunks、图邻居、引用和警告。

**用途：**
- 给 NL2SQL Agent 提供精准的 Schema 上下文（替代全量传 Schema）
- 控制 token 预算：通过 `budget` 和 `topPages` 限制上下文大小
- 包含 warnings：让 Agent 知道 Wiki 的健康状态（过期页面等）

**与 search_pages 的区别：** search_pages 返回匹配页面，context_pack 额外包含 chunks 和图邻居

**输入参数：**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `prompt` | string | 是 | — | 任务/主题描述 |
| `budget` | number | 否 | 8000 | 输出 token 预算 |
| `depth` | number | 否 | 1 | 图邻居深度（0=禁用，最大2） |
| `topPages` | number | 否 | 5 | 主页面数量上限 |
| `topChunks` | number | 否 | 8 | 语义 chunk 数量上限 |
| `omitRoot` | boolean | 否 | false | 是否隐藏 root 路径 |
| `includeSources` | boolean | 否 | false | 是否包含源文件内容 |

**输出参数：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `primary` | array | 主页面列表（含完整 body） |
| `chunks` | array | 语义 chunks |
| `neighbors` | array | 图邻居页面 |
| `warnings` | array | 降级/健康警告 |
| `project.root` | string\|null | Wiki 根目录 |

**调用示例：**
```python
await call_tool(session, "get_context_pack", {
    "prompt": "Chinook 数据库中有哪些表？它们之间的关系是什么？",
    "budget": 4000,
    "topPages": 5
})
```

---

## 工具分类速查

| 工具 | 是否需 LLM | 是否修改文件 | 搜索范围 |
|------|-----------|-------------|---------|
| `wiki_status` | 否 | 否 | 全目录统计 |
| `read_page` | 否 | 否 | **仅** concepts/ + queries/ |
| `search_pages` | 是 | 否 | 所有 namespace 目录 |
| `query_wiki` | 是 | 否（save=false）| 所有 namespace 目录 |
| `lint_wiki` | 否 | 否 | 全目录扫描 |
| `ingest_source` | 否 | 是 → sources/ | — |
| `compile_wiki` | 是 | 是 → wiki/ | sources/ → wiki/ |
| `get_context_pack` | 否* | 否 | 所有 namespace 目录 |

> *`get_context_pack` 的 semantic retrieval 是机会性的，无 credentials 时回退到词法检索。

---

## 典型工作流

### 新数据库首次编译

```
ingest_source  →  compile_wiki  →  wiki_status（验证）→  lint_wiki（检查质量）
```

### 日常 Schema 查询

```
search_pages  →  直接使用 pages[].body（无需 read_page）
```

### 知识问答

```
query_wiki    →  获取带引用的自然语言回答
```

### Wiki 健康维护

```
wiki_status   →  发现陈旧/孤立页面
lint_wiki     →  发现断链/空页/矛盾
compile_wiki  →  重新编译修复
```
