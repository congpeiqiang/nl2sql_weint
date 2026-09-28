# 中间产物审计与 SQL 来源标记方案

> 目标：一条问数跑完后，能**事后**回答「这次 SQL 是怎么来的、每个技能各自留下了什么」。
> 全部由服务端从工具调用轨迹**确定性**算出 —— 零模型往返、零提示词改动、零技能文本改动。
>
> 落地文件：`src/agent/utils/process_audit.py`（新建）；契约测试
> `scripts/verify_process_audit.py`（96 项，含 10 条负对照）。

---

## 0. 起因：三个问题，同一件事

用户提出的三件事，核实后是同一个缺口的不同侧面 —— **现在没有办法回答「这次 SQL 是怎么来的」**：

| 提出的问题 | 核实结论 |
|---|---|
| 每个 skill 都该留下中间产物供审计；`heuristic` 是不是该改成技能名 | `heuristic` 是**工具族**（8 值），同一份 JSON 的兄弟字段 `skill` **已经是技能名** ⇒ 字段名不用改。真正的缺陷是**目录轴有两套写法**（见 §2） |
| 前端进度条的名称要不要同步改 | **不动**。进度条逐字显示模型自己写的 `todos[].content`，与产物 JSON **零数据通路**（见 §6） |
| 要能跟踪 SQL 是 cube 指标编译的还是模型照 schema 手写的 | 此前只有 `sql_kind="cube"`（且「手写」靠键缺席表达）⇒ **混合路径与纯手写长得一模一样**，而它恰恰是最该区分的一态（见 §5） |

---

## 1. 三个轴，别混为一谈

审计里同时存在三个**互相独立**的维度，此前被当成一个，才导致改不对地方：

| 轴 | 取值 | 载体 | 用途 |
|---|---|---|---|
| **span 轴** | 展示技能名，**可能回退成族名**（`sql-generation`） | Langfuse span 名 / `_span_meta["skill"]` | 可观测性分组。回退行为被 `verify_skill_owner_table.py` 的负例钉住，**本次不动** |
| **目录轴** | 恒 ∈ `SKILL_DIR_NAMES`（真实技能目录名） | `nl2sql_process_data/{thread}/{技能名}/` | 产物落盘位置。**本次新增规范化保证它恒等于技能名** |
| **来源轴** | `cube_metric` / `cube_metric+llm_outer` / `llm_from_schema` / `unknown` | manifest + check 结果 + 报告 | 回答「这条 SQL 怎么来的」。**本次新增** |

两轴的关系写在 `langfuse_span._invoke` 的注释里：span 名仍用展示名（钉子断言），落盘目录用
`normalize_skill_dir(display, heuristic)` 规范化后的名字。`heuristic` 字段**保留原义**并与
`skill` 并列留在 payload 里（两个维度都留，不做二选一）。

---

## 2. 目录轴：此前会写出一套没人读的目录

**血案**：落盘目录取 `langfuse_span._resolve_display_skill` 的返回值。共享工具 `dry_run` 有
**5 个 owner**（`wren-sql-author` / `wren-orchestrator` / `wren-perf-optimize` /
`wren-metric-query` / `wren-execution`），归属不定时会回退成**启发式族名**
（`sql-generation`）；而读者 `check_progress._backfill_process_data_sql` 曾**写死**读
`.../sql-generation/`。两套目录只在「线程活动 skill 恰好过期」时才碰巧对上 ⇒ **回填常年
静默 0**：它本身 fail-open，既不报错也不打日志，所以长期无人察觉。

**修法（两处，缺一不可）**：

1. **写侧规范化**：`normalize_skill_dir(skill, heuristic)`，返回值**恒 ∈ `SKILL_DIR_NAMES`
   ∪ {""}**，绝不返回族名。优先级——
   ① `skill` 已是真实技能目录名 → 原样（绝大多数情况）；
   ② 否则按启发式族回退到该族的**规范 owner**（`("sql-generation","sql-generation")`
      → `wren-sql-author`）；
   ③ 都落空（未知新族名/空）→ `""` = **不落盘**。宁可不落盘，也不拿未知名去建目录。
   映射方向安全的前提：每个族在 `_TOOL_OWNER_SKILLS` 里的 owner 唯一（回退只是把兜底收敛
   回规范 owner，不张冠李戴）。
2. **读侧跨目录扫描**：`_backfill_process_data_sql` 删掉写死的 `_BACKFILL_SKILL`，改为遍历
   `{session_thread}/` 下**全部技能子目录**（跳过 `skill_sop` / `wren_plan` /
   `query_result` / `_manifest`），并新增守卫 `tool.endswith("dry_run")`。
   不选「改常量」是因为 `dry_run` 的 5 个 owner 会把文件落到不同目录 —— 那与现在的病**同型**。
   跨目录扫描对进程级 `_THREAD_ACTIVE_SKILL` 的**新鲜度零依赖**（任务隔离本来就由
   `dry_sqls` 集合承担），幂等性不变。

> `_resolve_display_skill` **不回改** —— 它的族名回退被 `verify_skill_owner_table.py:200-217`
> 的负例（`wren-clarify`）钉住，改它会连带 span 轴一起漂移。

---

## 3. 落点与四种子布局

真实路径 `<AGENT_DATA_ROOT>/workspace/nl2sql_process_data/{session_thread}/`
（生产 `/app/data/workspace/nl2sql_process_data/`），VFS 视角 `/workspace/nl2sql_process_data/…`。
`{session_thread}` = 会话线程 id（`langfuse_session_id`），**不是**子 agent 线程 id。

| 子布局 | 写入方 | 内容 |
|---|---|---|
| `{技能名}/*.json` | 服务端 `langfuse_span._dump_process_data` | 每次工具调用的入参/结果（`tool` / `skill` / `heuristic` / `display_skill`） |
| `skill_sop/{技能名}/*` | **模型**按各 `SKILL.md` 契约自写 | 澄清裁决、性能优化结论等纯推理产物 |
| `wren_plan/*.sql` | `wren_plan.write_plan_file` | 真正下发目标库的物理 SQL sidecar |
| `query_result/*.md` | `QueryResultOffload` | 大结果全量落盘（报告「完整数据表」节的来源） |
| `_manifest/*.json` | **本次新增** | 子任务级审计索引 |

**四个布局不合并**：写入方各不同，合并就得改 `SKILL.md` 里的落盘路径 —— 那是**第三个交付面**
（运行期生效的是 `<AGENT_DATA_ROOT>/shared/skills`，改仓库 ≠ 线上生效），且合并只把「一处写坏」
变成「两处都写坏」。manifest 的 `layouts` + `artifacts` + 每技能 `files`/`sop_files` 已把四个
布局全部索引到。

---

## 4. 每个技能都有产物（谁写、写什么）

**原则：能从工具轨迹确定性复算的，一律平台代写**（零模型往返，不可能「模型忘了写」）；
**只有真·纯推理的才保留模型自写**。

| 技能 | 产物 | 来源 |
|---|---|---|
| `wren-orchestrator` | `_routing-{qid8}-{sub8}.json` | **平台复算**（它的路由是零工具调用的纯推理，工具边界抓不到）。复用 §5 的同一份判据，**不写第二套**。含 `route` / `sql_origin_evidence` / `phases` / `tool_sequence` / 显式 `model_self_report: null`（声明这不是模型自述） |
| `wren-retrieve` | `{技能名}/*.json` | 工具 dump（`get_context` / `recall_queries` 唯一归属） |
| `wren-clarify` | `skill_sop/wren-clarify/*` | **模型自写**（裁决是推理，平台无从复算） |
| `wren-metric-query` | 工具 dump + `_cube_summary-{qid8}-{sub8}.json` | 前者天然落盘（`query_cube` 唯一归属）；后者**平台合成**：用了哪个 cube / 哪些 measure、dimension、取数还是仅预览。**只在真有 cube 调用时写** |
| `wren-sql-author` | 工具 dump（`dry_plan` 唯一归属；`dry_run` 落此或别的 owner 目录） | 工具 dump + §2 的跨目录回填 |
| `wren-perf-optimize` | `skill_sop/wren-perf-optimize/*` + 干跑 dump | 结论模型自写；干跑是共享工具，按实际归属落盘 |
| `wren-execution` | 工具 dump（`run_sql` 唯一归属） | 工具 dump |
| `wren-writeback` | **无会话期产物** | 循环外回写规范，由 FeedbackStore 桥接执行；manifest 的 skills 一节仍列出并注明 |

**「每个技能都有产物」的可断言形式**：manifest 的 `skills` 一节**七个键恒在**，缺产物也要
在场并写明 `reason`（如 `route=llm_from_schema` / `no_verdict_no_stop_marker`）。测试断言
`set(manifest["skills"]) == set(SESSION_SKILLS)` ⇒ **「静默没有」在结构上不可能发生**。

设计上**不建假目录**：纯手写路径不创建 `wren-metric-query/`，而是由 manifest 如实记录缺席。

**写入时机**：`check_progress._enhanced_build_check_result` 的 `status=="success"` 分支末尾
（紧邻 `_backfill_process_data_sql` 之后）。这里是**唯一**同时具备「完整子任务消息 + 已算好的
锚点 SQL + 用户最终会看到的那份 check 结果」的地方 —— `langfuse_span` 的工具边界一次只有一条
工具消息，看不到全貌。失败路径（`status=="error"`）写一份 `status="error"` 的 manifest 闭合
记录缺口（**不**写 routing / cube_summary，`sql_origin` 如实落 `unknown`）；running / cancel
不写。

---

## 5. 来源轴：四态判据

**锚点定义（全部争议所在）**：来源 = 「**产出最终结果表的那一次执行**」。`producing_sql` 由
调用方传入（`check_progress._extract_last_sql`，也正是前端与报告逐字展示给用户的那条），
不在判据内部重算 —— 一是避免重复扫描，二是避免 `process_audit` 反向 import `check_progress`
成环。**不以 `_extract_last_cube_call` 为锚**：它取「最后一次**成功**调用」，可能只是一次探值/预览。

| 条件 | `sql_origin` |
|---|---|
| `producing_sql` 非空，且锚点**之前**有成功的 cube 调用（取数，或 `sql_only` 预览） | `cube_metric+llm_outer` |
| `producing_sql` 非空，其余（含 cube 调用都在锚点之后） | `llm_from_schema` |
| `producing_sql` 为空，有带数据的 cube 调用 | `cube_metric` |
| `producing_sql` 为空，只有成功的 `sql_only` 预览 | `unknown`（`preview_only_no_execution`） |
| 都没有（澄清中断、抽取失败、结果来自缓存） | `unknown`（`no_sql_evidence`） |

**硬约束**：`list_cubes` / `describe_cube` / `get_mdl` / `list_models` / `describe_model` /
`describe_schema` / `get_db_info` / `get_data_source` / `get_context` / `get_instructions` /
`list_knowledge` / `recall_queries` / `dry_plan` / `dry_run` **一律不是来源证据**，只进
`evidence.metadata_only_tools` 备查。`list_cubes` 尤其重要 —— 它说明「看过有哪些 cube」，
不说明「用 cube 取了数」。

⚠️ **刻意不复制** `run_experiment._extract_strategy` 的两个缺陷：它把 `list_cubes`（纯检索）
算作 Cube 证据，且把混合路径归入 `"C"`。新代码里这两条各有一条**负对照**测试钉住。

⚠️ **已知的定义性取舍**：模型若先用 `query_cube`（取数）拿表、再用 `run_sql` 探值或补算，
按上表判为 mixed。这是「最终数据来源」这一定义的自然结果，**不是误判**；逐条明细留在
`evidence` 里供事后复核。`confidence` 在锚点定位失败时从 `strong` 降为 `weak`（次序不可证）。

`evidence.cube_sql_in_final` 只作**次要**置信标注（从 `sql_only` 那次结果里抠编译 SQL，
判断是否被最终 SQL 包含；抠不到 = `null`），**不参与枚举** —— 宁可拿不到，也不去猜一段
可能是别的东西的文本。

### 三处落点

1. **manifest**：`_manifest/manifest-{qid8}-{sub8}.json`。按**子任务**分文件（复杂问题拆 2~3 个
   子问题各自 check），文件名稳定（不用序号）⇒ 反复 check 同名覆盖 = 幂等。`tool_trace` 只放
   摘要与指纹（`sql_digest` / `cube_spec` / `result_kind`），正文在 per-call dump 与
   `wren_plan/*.sql` 里，整份控在几十 KB（实测 4 KB 量级）。
2. **check 结果透传**：`result["sql_origin"]` / `result["sql_origin_evidence"]` 写在
   `if sql:` **分支之前**，让「有 `run_sql`」与「纯 Cube 快速通道」两条路都带上；另加小指针
   `result["process_manifest"]`（~100 字节）。`sql_kind` **原样保留**（全仓无活读者，删了只制造
   兼容风险），注释里注明 `sql_kind=="cube"` ⇔ `sql_origin=="cube_metric"`。
3. **报告**：`report_builder._sql_origin_line` 渲染一行 `> SQL 生成来源：…`。落点在
   `if sql:` 分支末尾（**含**「结果里已内嵌 SQL」的跳节路径 —— 那正是用户看到 SQL 的情形）
   与 `elif cube_query:` 分支末尾。节标题**只在** `cube_metric+llm_outer` 时演进为
   `## N. 执行 SQL（Cube 指标主体 + 模型手写外层，实际下发）`，其余标题一字不动。
   **`unknown` / 字段缺席 → 不输出该行**：老 check 结果 ⇒ 报告与改动前逐字一致，不给读不出来源
   的报告硬安一个来源。

---

## 6. 边界：前端进度条与本方案**没有任何数据通路**

进度条逐字显示模型自己写的 `todos[].content`（`write_todos` 协议），前端全仓**无
`process_data` 引用**，本方案的模块也不写 todos。**改这里不会影响进度条，改进度条也不会影响
这里** —— 这条界线写进代码注释与文档，避免下次再被当成联动关系（历史方案见
`并发查询进度条分组显示方案.md`）。

同理**不改** `write_todos` 协议、不改 `_resolve_display_skill` / span 名 /
`_span_meta["skill"]`。

---

## 7. 交付面与发版（务必读完再动手）

本方案**只走代码发版一个面**：不改任何 `SKILL.md`、不改 `NL2SQL_SYSTEM_PROMPT.md` ⇒ 无需推
Langfuse 提示词、无需同步运行期技能副本、无需因本次改动重启。

⚠️ **若将来为了让模型配合而改了 `SKILL.md` 或提示词**，必须同时：

1. `cp -a` 到运行期生效副本 `<AGENT_DATA_ROOT>/shared/skills`（用
   `scripts/check_skills_drift.py` 体检）——**改仓库 ≠ 线上生效**；
2. **重启后端**：提示词是**导入期求值**的，不重启则 Langfuse 上推完也不生效。

这是历史事故高发点（`runtime-skills-not-refreshed-by-release.md`）；
`heuristic` 与「已发版」标记都**判不了**线上跑什么，判据只有 trace 的
`metadata.prompt.prompt_versions` / 启动日志 `[langfuse] prompt … v<N> 生效` /
`docker exec md5sum`。

---

## 8. 验证

```
PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_process_audit.py   # 96/96
```

七组（含负对照）：

| 组 | 关键负对照 |
|---|---|
| A 目录轴 | 族名永远不会成为目录名（`HEURISTIC_TO_SKILL` 的值 ∩ 族名 == ∅）；盘上目录 == 常量表 == frontmatter `name`；未知值 → `""` 不落盘 |
| B 来源四态 | **负对照 A**：只 `list_cubes`+`describe_cube`+手写 → 必须 `llm_from_schema`；**负对照 B**：cube 调用在锚点之后 → `llm_from_schema`；仅预览 → `unknown`；垃圾输入不抛 |
| C manifest | 顶层键齐全；`set(skills) == set(SESSION_SKILLS)`；`artifacts` 每路径真实存在；重复写幂等（文件数不变）；体积 < 256KB |
| D 每技能有产物 | 跑满主循环 → 五个技能目录 + `_routing` + `_cube_summary` 全在；**负对照**：纯手写 → 不建 `wren-metric-query/`，但该节恒在且 `observed=false` |
| E fail-open | 落盘目标不可用 → 仍返回结构完整的空指针；`None` 消息不抛 |
| F 回填跨目录 | 只改 `tool` 以 `dry_run` 结尾且命中 `dry_sqls` 的文件，其余**逐字节未动**；二次调用改写 0；**负对照**：旧实现（写死 `sql-generation/`）在本轨迹下选中 0 份 |
| G 报告一行 | 三态各自出现对应来源行；mixed 时标题演进；**负对照**：无 `sql_origin` → 无该行且标题回到 `## N. 执行 SQL` |

回归（必须全绿）：`verify_report_caliber.py`(85) · `verify_slimmer_exempt.py`(42) ·
`verify_skill_owner_table.py`(41) · `verify_retention.py`(76) ·
`verify_workspace_pinned.py`(56) · `verify_wren_offload.py`(28)。

**生产 E2E**：跑一条走 cube 的与一条手写的问数，`docker exec` 核对
`/app/data/workspace/nl2sql_process_data/` 下 manifest 的 `sql_origin` 与实际路径一致、
七个技能目录齐、报告里出现「SQL 生成来源」行且与实际相符。

---

## 9. 已知残余与不做

- **`_THREAD_ACTIVE_SKILL` 过期**：目录不会再变成族名（规范化兜底），残余影响面只剩共享工具
  `dry_run` 的落盘归属可能标错技能目录 —— 回填对新实现**免疫**（跨目录扫描），manifest 的
  `tool_trace[].skill` 如实记录当时判定（`skill_owner: derived`）。
- **进程级状态跨重启丢失**（`_THREAD_ACTIVE_SKILL` / 计数）：与 `query_gate` /
  `progress_boundary` 同款债，不影响正确性。
- **保留期**：`nl2sql_process_data` 走 30 天按龄清理 ⇒ **审计窗口 = 30 天**。要更长需另改保留
  策略（`p2-5-retention-disk-watermark`）。
- **不做**：不做 LLM 判来源（零模型往返是硬要求）；不给 `wren-writeback` 造会话期产物；
  不合并四个子布局；不回溯历史目录（老的 `sql-generation/` 目录不改名不搬迁）；
  不做跨子任务的合并索引（并发读-改-写风险 > 收益）。
- **回退**：新增一个模块 + 四处调用点 + 一个渲染函数，`git revert` 即净；老报告、老 thread
  目录不受影响。
