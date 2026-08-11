# llm-wiki-compiler MCP 工具深度分析

> 基于源码 [atomicstrata/llm-wiki-compiler](https://github.com/atomicstrata/llm-wiki-compiler)，结合实际案例 `chinook_autoIncrement` 项目进行说明。

---

## 目录

1. [项目概述](#1-项目概述)
2. [架构总览](#2-架构总览)
3. [工具详解](#3-工具详解)
   - [3.1 ingest_source — 来源摄入](#31-ingest_source--来源摄入)
   - [3.2 compile_wiki — 编译 Wiki](#32-compile_wiki--编译-wiki)
   - [3.3 query_wiki — 查询 Wiki](#33-query_wiki--查询-wiki)
   - [3.4 search_pages — 搜索页面](#34-search_pages--搜索页面)
   - [3.5 read_page — 读取页面](#35-read_page--读取页面)
   - [3.6 lint_wiki — 质量检查](#36-lint_wiki--质量检查)
   - [3.7 wiki_status — 状态概览](#37-wiki_status--状态概览)
4. [附加工具](#4-附加工具)
5. [实战案例：Chinook 数据库知识 Wiki](#5-实战案例chinook-数据库知识-wiki)
6. [工具选型指南](#6-工具选型指南)

---

## 1. 项目概述

**llm-wiki-compiler**（CLI 命令 `llmwiki`）是一个知识编译器，遵循 Andrej Karpathy 提出的 [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 模式：

> 将原始资料**编译一次**，生成持久的、可积累的、带引用的交叉链接 Markdown Wiki，而不是每次查询时从原始文件中重新发现知识。

通过 `llmwiki serve` 启动 MCP Server，对外暴露 10 个工具，供 MCP 兼容的 AI Agent 调用。


## 2. 架构总览

```
                          MCP Server (llmwiki serve)
                                  │
          ┌───────────────────────┼───────────────────────┐
          │                       │                       │
      只读工具                 需要 LLM                 写入工具
      ─────────               ──────────              ──────────
      read_page              compile_wiki            ingest_source
      wiki_status            query_wiki              (写入 sources/)
      lint_wiki              search_pages            compile_wiki
      get_context_pack       run_eval (full)         (写入 concepts/、
      run_eval (fast)                                 queries/、
      verify_artifact                                 index.md)
```

**数据流：**

```
sources/                    wiki/                      导出
  │                          │                          │
  │  ingest_source           │  compile_wiki             │
  ▼                          ▼                          ▼
原始资料 ──────────────► 概念页面 (concepts/)    OKF / JSON /
                        实体页面 (entities/)     JSON-LD /
                        查询存档 (queries/)     GraphML /
                        索引 (index.md)          llms.txt
                        日志 (log.md)
                        MOC (MOC.md)
```

**工作区目录结构（以 chinook_autoIncrement 为例）：**

```
chinook_autoIncrement/
├── .llmwiki/
│   └── state.json          ← 编译状态（hash 记录、时间戳）
├── sources/
│   └── chinook-ddl.md      ← 原始 DDL 来源
├── wiki/
│   ├── index.md            ← 页面目录
│   ├── log.md              ← 操作日志
│   ├── MOC.md              ← 主题地图 (Map of Content)
│   ├── overview.md         ← 项目概览
│   ├── concepts/           ← 6 个概念页面
│   │   ├── 作曲家署名模式.md
│   │   ├── 发票定价模式.md
│   │   ├── 员工层级结构.md
│   │   ├── 客户地理分布.md
│   │   ├── 播放列表曲目分布模式.md
│   │   └── 艺术家内部流派不一致.md
│   ├── entities/           ← 14 个实体页面
│   │   ├── chinook-database.md
│   │   ├── artist-表.md
│   │   ├── track-表.md
│   │   └── ... (各数据表)
│   └── sources/            ← 来源元信息
│       └── Chinook_Sqlite_Sql.md
├── schema.md               ← Wiki Schema（领域约定）
└── purpose.md              ← 项目目的
```

---

## 3. 工具详解

### 3.1 ingest_source — 来源摄入

**功能：** 将外部来源拉入 `sources/` 目录。

**实现链路：** `tools.ts → commands/ingest.ts → ingest/web.ts | ingest/file.ts | ingest/pdf.ts | ingest/image.ts | ingest/transcript.ts`

**参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `source` | string | URL (http/https) 或本地的 `.md`/`.txt` 文件的绝对路径 |

**处理流程：**

1. 检测输入类型：
   - `http://` 或 `https://` → Web 抓取
   - `.md` / `.txt` 文件 → 启发式内容嗅探（前 2048 字节）
   - PDF → 文本提取
   - 图片 → Vision API 描述
   - YouTube URL → 自动识别为 transcript

2. 对 `.txt` 文件的启发式检测：
   - 检测 `Speaker: dialogue` 对话模式（至少 2 个不同说话人，至少 1 人出现 ≥2 次）
   - 检测行首时间戳模式 `HH:MM`（至少 3 个匹配）
   - 满足任一条件则标记为 `transcript` 类型

3. 保存时生成 YAML frontmatter：
   ```yaml
   ---
   source_url: https://example.com/article
   ingested: 2026-07-18
   sha256: <hex digest>
   sourceType: web | file | pdf | image | transcript
   ---
   ```
   `sha256` 用于后续变更检测——重复摄入同一 URL 时对比哈希，不变则跳过。

4. 大小限制：`MAX_SOURCE_CHARS` 和 `MIN_SOURCE_CHARS`，超出会被截断。

**返回结果：** `{ filename, charCount, truncated }`

**Chinook 案例：**
```
# 将 Chinook DDL 文件摄入为来源
ingest_source({ source: "D:\\学习资料\\...\\chinook_ddl.md" })

→ 结果:
{
  filename: "chinook-ddl.md",
  charCount: 3207,
  truncated: false
}
→ 文件保存至: sources/chinook-ddl.md
```

---

### 3.2 compile_wiki — 编译 Wiki

**功能：** 运行增量编译流水线——从新的/变更的来源中提取概念、生成 Wiki 页面、解析交叉链接、重建索引。

**实现链路：** `tools.ts → compiler/index.ts` 中的 `compileAndReport()`

**参数：** 无（从工作区自动检测变更）

**处理流程（8 个阶段）：**

| 阶段 | 模块 | 说明 |
|------|------|------|
| 1. 获取锁 | `lock.js` | 获取 `.llmwiki/lock`，防止并发编译 |
| 2. 恢复日志 | `journal-recovery.js` | 从上次崩溃中恢复 |
| 3. 变更检测 | `hasher.js` | 对每个 source 计算 hash，与 `state.json` 对比 |
| 4. 概念提取 | `extraction-phase.js` | 两阶段 LLM pipeline：提取概念 → 合并去重 |
| 5. 页面生成 | `review-pipeline.js` | 为每个概念生成 Wiki 页面 |
| 6. 孤立标记 | `orphan.js` | 标记来源已删除的页面 |
| 7. 链接解析 | `resolver.js` | 解析 `[[wikilinks]]`，确保一致性 |
| 8. 索引重建 | `indexgen.js` | 生成 `index.md` 和 `MOC.md` |

**返回结果（CompileResult）：**
```json
{
  "compiled": 6,
  "skipped": 0,
  "deleted": 0,
  "concepts": [
    "作曲家署名模式",
    "发票定价模式",
    "员工层级结构",
    "客户地理分布",
    "播放列表曲目分布模式",
    "艺术家内部流派不一致"
  ],
  "pages": ["作曲家署名模式", "发票定价模式", "..."],
  "errors": []
}
```

**Review Policy（审核策略）：** 支持将生成的页面自动进入审核队列，需人工审批后才发布。

**Chinook 案例：**

执行 `compile_wiki` 后，从 `chinook-ddl.md` 中提取出 6 个概念，对应生成了 `wiki/concepts/` 下的 6 个中文页面：

| 概念 | 页面 | 核心内容 |
|------|------|----------|
| 作曲家署名模式 | `作曲家署名模式.md` | 14 种署名格式分类（全名、缩写、集体署名、NULL、斜杠分隔等） |
| 发票定价模式 | `发票定价模式.md` | 0.99/1.99 定价、混合定价、非标准总计 |
| 员工层级结构 | `员工层级结构.md` | 2 级组织架构（General Manager → Managers → Staff） |
| 客户地理分布 | `客户地理分布.md` | 59 位客户、21 个国家/地区、公司关联 |
| 播放列表曲目分布 | `播放列表曲目分布模式.md` | 块状分布、曲目范围、重叠分析 |
| 艺术家流派不一致 | `艺术家内部流派不一致.md` | Iron Maiden 等同一艺术家的流派冲突 |

同时生成了 14 个实体页面（11 张表 + chinook-database + luis-rocha + sqlite）。

---

### 3.3 query_wiki — 查询 Wiki

**功能：** 自然语言问题 → 语义检索 → 页面选择 → LLM 生成带引用的回答。

**实现链路：** `tools.ts → commands/query.ts` 中的 `generateAnswer()`

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 自然语言问题 |
| `save` | boolean | 否 | 是否将回答持久化为 `queries/` 页面 |
| `debug` | boolean | 否 | 是否返回检索 debug 信息（chunks、分数） |

**处理流程：**

```
问题输入
    │
    ▼
Step 1: 页面选择（三阶段 fallback）
    ├── V3 Embedding 语义搜索（chunk 级 → page 级聚合）
    ├── BM25 重排序
    └── LLM fallback（当 embedding 不可用时，对 pageId 索引选择）
    │
    ▼
Step 2: 答案生成
    ├── 加载选中页面完整内容
    └── LLM 流式生成带 [[引用的回答]]
    │
    ▼
Step 3: 可选保存（save=true）
    └── 写入 wiki/queries/<slug>.md
```

**Chinook 案例：**

```
query_wiki({
  question: "Chinook 数据库中有哪些数据质量问题？",
  save: false
})

→ Step 1: 语义检索选中了 3 个页面：
  - 艺术家内部流派不一致.md（相关性最高）
  - 作曲家署名模式.md
  - track-表.md（异常曲目信息）

→ Step 2: LLM 生成回答：
  Chinook 数据库存在以下数据质量问题：
  
  1. **艺术家内部流派不一致**（[[艺术家内部流派不一致]]）：同一艺术家的曲目被分配了
     不同的 GenreId。例如 Iron Maiden 同时使用 GenreId 1 (Rock) 和 GenreId 3 (Metal)...
  
  2. **作曲家署名格式不统一**（[[作曲家署名模式]]）：Track 表的 Composer 字段存在
     14 种不同的署名格式，包括全名、缩写、斜杠分隔、NULL 值等...
  
  3. **异常曲目数据**（[[track-表]]）：TrackId 2461 时长仅 1071 毫秒，
     TrackId 3304 为商业广告片段...
```

---

### 3.4 search_pages — 搜索页面

**功能：** 选择与问题相关的 Wiki 页面并返回其完整内容（不生成回答）。

**实现链路：** `tools.ts → search/retrieval.ts` 中的 `pickSearchRefs()` + `loadSelectedRefs()`

**参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `question` | string | 用于排序页面的查询语句 |

**处理流程：**

```
问题输入
    │
    ▼
pickSearchRefs(root, question)
    ├── 加载 Embedding Store
    ├── Chunk 级语义检索 → Page 级聚合
    ├── 去重（按 pageId）
    └── 返回 { refs: SelectedPageRef[], warnings }
    │
    ▼
loadSelectedRefs(root, refs)
    ├── 通过 page-registry 从 LIVE 文件读取
    ├── 不使用存储缓存（S12 要求）
    └── 返回完整页面内容
```

**返回结构：**
```json
{
  "pages": [
    {
      "slug": "艺术家内部流派不一致",
      "title": "艺术家内部流派不一致",
      "summary": "同一艺术家的曲目可能被分配不同的流派ID...",
      "body": "# 艺术家内部流派不一致\n\n..."
    }
  ],
  "refs": [
    {
      "pageId": "concepts/艺术家内部流派不一致",
      "slug": "艺术家内部流派不一致",
      "title": "艺术家内部流派不一致",
      "kind": "chunk"
    }
  ],
  "warnings": []
}
```

**与 query_wiki 的区别：**

| 维度 | search_pages | query_wiki |
|------|-------------|------------|
| 输出 | 原始页面内容 | 自然语言回答 |
| LLM 调用 | 仅页面选择 | 页面选择 + 答案生成 |
| 用途 | Agent 自行处理 | 直接获取答案 |

**Chinook 案例：**

```
search_pages({ question: "员工层级结构" })

→ 返回 pages:
  [
    {
      slug: "员工层级结构",
      title: "员工层级结构",
      body: "# 员工层级结构\n\n## 组织架构\n- Andrew Adams — General Manager\n  - Nancy Edwards — Sales Manager\n    - Jane Peacock\n    - Margaret Park\n    - Steve Johnson\n  - Michael Mitchell — IT Manager\n    - Robert King\n    - Laura Callahan"
    },
    {
      slug: "employee-表",
      title: "Employee 表",
      body: "..."
    }
  ]
```

---

### 3.4.1 重要提示：search_pages 已返回完整 body

`search_pages` 返回的 `pages[].body` 字段包含页面的**完整 markdown 内容**（包括全部列定义、字段类型、外键关系等），与 `read_page` 返回的 `body` 完全相同。

**常见误区：** 在 NL2SQL Schema Linking 流程中，误以为 `search_pages` 只返回摘要，需要额外调用 `read_page` 获取详细列定义。实际上 `search_pages` 内部已通过 `loadSelectedRefs` → `loadPageRecordPairsByPageId` 加载了完整页面内容。

**正确用法：**
```
result = search_pages({ question: "哪些表包含客户信息？" })
// result.pages[0].body  ← 已包含 Customer 表的完整 DDL、所有列、类型、外键
// 无需再调用 read_page({ slug: result.pages[0].slug })
```

**对比 `read_page` 的局限性：**
- `read_page` 只搜索 `concepts/` 和 `queries/`（硬编码），无法读取 `entities/` 下的页面
- `search_pages` 通过 `page-registry` 扫描所有 namespace 目录，能返回任意目录下的页面

---

### 3.5 read_page — 读取页面

**功能：** 按 slug 读取单个 Wiki 页面。纯文件读取，无 LLM 调用。

**实现链路：** `tools.ts → pages/read.ts` 中的 `readPageRecord()`

**参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `slug` | string | 页面标识（不含 `.md` 扩展名） |

**处理流程：**

1. 按优先级顺序搜索：`concepts/` → `queries/`
2. 读取 `.md` 文件，解析 YAML frontmatter
3. 跳过 `orphaned: true` 的页面
4. 返回结构化 `PageRecord`

**返回结构：**
```json
{
  "slug": "发票定价模式",
  "title": "发票定价模式",
  "summary": "Chinook 数据库的发票系统展示了特定的定价模式。",
  "body": "# 发票定价模式\n\n## 单价结构\n- 标准音乐曲目：UnitPrice = 0.99\n..."
}
```

**查找失败：** 如果页面不存在或已被标记为孤立，抛出 `Page not found: {slug}` 错误。

**Chinook 案例：**

```
read_page({ slug: "发票定价模式" })
→ concepts/发票定价模式.md 存在 → 返回内容

read_page({ slug: "不存在的页面" })
→ concepts/ + queries/ 均无此文件 → Error: Page not found
```

---

### 3.6 lint_wiki — 质量检查

**功能：** 运行基于规则的 Wiki 质量检查，返回结构化诊断。无 LLM 调用。

**实现链路：** `tools.ts → linter/index.ts` 中的 `lint()`

**参数：** 无

**检查规则（14 条）：**

| 分类 | 规则 | 检查内容 |
|------|------|----------|
| **结构** | `checkBrokenWikilinks` | 断开的 `[[内部链接]]` |
| | `checkOrphanedPages` | 无入站链接的孤立页面 |
| | `checkMissingSummaries` | 缺少 `summary` 的页面 |
| | `checkDuplicateConcepts` | slug 相同的重复概念 |
| | `checkEmptyPages` | 空内容的页面 |
| **引用** | `checkBrokenCitations` | 断开的 source 引用 |
| | `checkMalformedClaimCitations` | 格式错误的 claim 级引用 |
| | `checkInferredWithoutCitations` | 推断性声明缺少引用 |
| **质量** | `checkLowConfidencePages` | `confidence: low` 的页面 |
| | `checkContradictedPages` | 标记为矛盾的页面 |
| **健康** | `checkJournalHealth` | Journal 是否 pending/unavailable |
| | `checkPendingEmbeddings` | Embedding 刷新是否 pending |
| | `checkWorkflowRunHealth` | Workflow 运行健康状态 |
| **Schema** | `checkSchemaCrossLinks` | 按 Schema 检查交叉链接最低数 |
| **时效** | `checkStalePages` | 来源已变更但页面未更新 |

**返回结构：**
```json
{
  "errors": 0,
  "warnings": 2,
  "info": 0,
  "results": [
    {
      "severity": "warning",
      "rule": "checkStalePages",
      "file": "concepts/发票定价模式.md",
      "message": "Source 'chinook-ddl.md' updated after page generation"
    }
  ]
}
```

**严重级别：** `error` > `warning` > `info`

**Chinook 案例（预期输出）：**

```
lint_wiki()

→ 可能发现的问题：
  - warning: concepts/作曲家署名模式.md — summary 字段为空
  - warning: concepts/客户地理分布.md — summary 字段为空
  - info: wiki/overview.md — 内容过少，可能未完善
```

---

### 3.7 wiki_status — 状态概览

**功能：** 只读的 Wiki 状态概览。无 LLM 调用。

**实现链路：** `tools.ts → status/collect.ts` 中的 `collectStatus()`

**参数：** 无

**返回结构：**
```json
{
  "pages": {
    "concepts": 6,
    "queries": 0,
    "total": 6
  },
  "sources": 1,
  "lastCompiledAt": "2026-07-18T01:53:25.345Z",
  "stalePages": [],
  "staleCount": 0,
  "orphanedPages": [],
  "orphanedCount": 0,
  "stateStatus": "ok",
  "pendingCandidates": 0,
  "pendingChanges": [],
  "pendingChangesCount": 0,
  "warnings": []
}
```

**关键字段说明：**

| 字段 | 说明 |
|------|------|
| `stateStatus` | `ok` / `missing` / `corrupt` — 损坏的状态永远不会被静默忽略 |
| `stalePages` | 来源已变更但页面未重新编译的概念（上限 100 条） |
| `staleCount` | 真实陈旧页面总数（可能超过列表长度） |
| `orphanedPages` | 所有源已被删除的页面（上限 100 条） |
| `pendingChanges` | 自上次编译以来来源文件的变更（new/changed/deleted） |
| `warnings` | Journal 不完整或 Embedding 刷新 pending 时出现 |

**只读保证：** 使用 `readStateClassified`（而非 `readState`），损坏的 `state.json` 永远不会产生 `.bak` 副作用。

**Chinook 案例：**

```
wiki_status()

→ 返回:
{
  pages: { concepts: 6, queries: 0, total: 6 },
  sources: 1,
  lastCompiledAt: "2026-07-18T01:53:25.345Z",
  stalePages: [],
  orphanedPages: [],
  stateStatus: "ok",
  pendingCandidates: 0,
  pendingChanges: []
}
```

---

## 4. 附加工具

除上述 7 个工具外，MCP Server 还注册了 3 个附加工具：

| 工具 | 说明 | LLM |
|------|------|-----|
| `get_context_pack` | 为 prompt 构建 Agent 就绪的 evidence pack（主页面、语义 chunks、图邻居、引用、警告） | 否* |
| `run_eval` | 运行 eval 套件：`fast`（健康 + 引用覆盖）或 `full`（含 LLM 评判引用支持度） | full 需要 |
| `verify_artifact` | 验证工作流产出的 artifact | 否 |

> *`get_context_pack` 内部 semantic retrieval 是机会性的，缺少 credentials 时回退到词法检索。

---

## 5. 实战案例：Chinook 数据库知识 Wiki

### 5.1 项目概览

`chinook_autoIncrement` 是一个对 Chinook 示例数据库（SQLite，数字媒体商店）进行知识编译的 Wiki 项目。

**信息来源：** 一个 Chinook DDL 文件（`chinook-ddl.md`，3207 字节），包含完整的数据库 schema 定义。

**编译结果：**
- 6 个概念页面（数据模式发现）
- 14 个实体页面（数据库对象）
- 1 个来源页面（元信息归档）

### 5.2 典型工作流

```
Step 1: ingest_source
  摄入 chinook-ddl.md → sources/
  
Step 2: compile_wiki
  从 DDL 中提取概念 → 生成 6 个概念页面 + 14 个实体页面
  
Step 3: wiki_status
  确认编译完成，无陈旧/孤立页面
  
Step 4: query_wiki
  "Chinook 数据库中有哪些数据质量问题？"
  → 返回带引用的分析报告
  
Step 5: read_page
  按需读取特定页面：read_page({ slug: "发票定价模式" })

Step 6: lint_wiki
  质量检查，发现 summary 为空等问题
  
Step 7 (修复): compile_wiki
  修正来源后重新编译，lint 通过
```

### 5.3 生成的概念页面示例

**发票定价模式**（`wiki/concepts/发票定价模式.md`）：

```markdown
---
type: concept
title: 发票定价模式
tags: [chinook, invoice, pricing, data-analysis]
related: [invoice-table, invoiceline-table, track-table]
created: 2026-07-18
updated: 2026-07-18
sources: ["Chinook_Sqlite_Sql.md"]
---

# 发票定价模式

Chinook 数据库的发票系统展示了特定的定价模式。

## 单价结构
- 标准音乐曲目：UnitPrice = 0.99
- 电视剧集曲目：UnitPrice = 1.99
- 所有行项目的 Quantity 始终为 1

## 发票总计模式
发票总计主要遵循 0.99 的倍数，但存在例外：
- 标准总计：0.99、1.98、3.96、5.94、8.91、13.86
- 非标准总计：6.94、17.91、18.86、21.86、25.86

## 混合定价
部分发票包含混合定价（0.99 和 1.99 行项目），导致非标准总计。

## 相关页面
- [[invoice-table]] — 发票记录
- [[invoiceline-table]] — 发票行项目
- [[track-table]] — 曲目定价
- [[Chinook_Sqlite_Sql.md]] — 源文件
```

### 5.4 生成的概念页面全览

| 概念 | 发现内容 | 标签 |
|------|----------|------|
| **作曲家署名模式** | 14 种署名格式（全名、缩写、NULL、斜杠分隔、逗号分隔等） | composer, attribution, data-quality |
| **发票定价模式** | 0.99/1.99 定价、混合定价、非标准总计 | invoice, pricing, data-analysis |
| **员工层级结构** | General Manager → Sales/IT Manager → Staff | employee, hierarchy, organization |
| **客户地理分布** | 59 客户、21 国家、公司关联（Google/Microsoft/Apple） | customer, geography, distribution |
| **播放列表曲目分布** | 块状分布（按专辑分组）、曲目重叠 | playlist, distribution, data-patterns |
| **艺术家内部流派不一致** | Iron Maiden 同时为 Rock 和 Metal、U2 被标记为 R&B/Soul | genre, inconsistency, data-quality |

---

## 6. 工具选型指南

| 场景 | 推荐工具 | 原因 |
|------|----------|------|
| 添加新来源 | `ingest_source` | 将 URL/文件拉入 sources/ |
| 生成 Wiki 内容 | `compile_wiki` | 增量编译，提取概念 → 生成页面 |
| 直接获取答案 | `query_wiki` | 自然语言 → 检索 + 生成回答 |
| Agent 自行分析 | `search_pages` | 返回原始页面内容，不生成回答 |
| 快速查页面 | `read_page` | 按 slug 读取，零 LLM 开销 |
| 质量审计 | `lint_wiki` | 14 条规则，结构化诊断 |
| 健康检查 | `wiki_status` | 页面数、陈旧/孤立页面、状态健康 |
| 为 Agent 准备上下文 | `get_context_pack` | 主页面 + chunks + 图邻居 + 引用 |
| 评估 Wiki 质量 | `run_eval` | fast（无 LLM）/ full（含引用评判） |

**依赖 LLM 的工具：** `compile_wiki`, `query_wiki`, `search_pages`, `run_eval (full)`

**无需 LLM 的工具：** `ingest_source`, `read_page`, `wiki_status`, `lint_wiki`, `get_context_pack`, `run_eval (fast)`, `verify_artifact`

---

> 源码地址：https://github.com/atomicstrata/llm-wiki-compiler
> 分析日期：2026-07-18
> 案例项目：`D:\code_work_space\llm\LLM-Wiki-Project\chinook_autoIncrement`
