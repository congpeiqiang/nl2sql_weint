# Langfuse 存储表清单 —— PostgreSQL `langfuse` + ClickHouse `nl2sql`

> 面向：需要直连数据库排查 / 取数 / 做 BI 接入的人。
> 部署事实来源：`docs/langfuse平台/langfuse部署脚本/README.md`（192.168.25.64 自托管 Langfuse v4）。
> 建立日期：2026-09-17。
>
> **核实状态标注**（全文沿用）：
> - ✅ **已核实** —— 本仓库文档或代码里有明确记载，可直接采信。
> - ⚠️ **推断** —— 由表名与本项目实际用法推断，**未逐表核验**；用之前跑一次本文 §4 的 `DESCRIBE` 确认。

---

## 0. TL;DR —— 数据存在哪个库

| 存储 | 装什么 | 库 / 卷 | 能不能丢 |
|---|---|---|---|
| **ClickHouse** `nl2sql` | trace / observation / score 的**本体载荷**（含 session 归属），即全部可观测数据 | 库 `nl2sql`，容器 `langfuse_clickhouse_1`，卷 `langfuse_langfuse_clickhouse_data` | **不能**——丢了等于观测数据全没了 |
| **PostgreSQL** `langfuse` | **元数据与配置**：账号、组织、项目、API Key、数据集、评估器配置、模型价格、删除队列；以及 **session 索引表** `trace_sessions` | 库 `langfuse`，容器 `langfuse_postgres_1`，卷 `langfuse_langfuse_postgres_data` | **不能**——丢了要重新初始化管理员/项目/API Key |
| **Redis** | 缓存 + 任务队列（`langfuse:` ≈373 key、`bull:` ≈217 key，约 8 MB） | 容器 `langfuse_redis_1` | **可以**，清空无副作用 |
| **MinIO** | S3 事件归档与媒体文件（图片/音频），bucket `langfuse` | 容器 `langfuse_minio_1` | 取决于是否开了 blob 导出；本项目当前不是主存储 |

**一句话记牢**：**「业务内容」在 ClickHouse，「账号与配置」在 PostgreSQL。** 备份必须两边成对做——只备份 ClickHouse 会丢掉 session 索引和全部 API Key。

### 关于「Session 存在哪」

`Session` 在 Langfuse 里**不是一张存业务数据的表**，而是从 trace/observation 的 `sessionId` 列**聚合出来的视图**：

- 会话内容（每轮问答、工具调用、耗时）→ ClickHouse `nl2sql` 的 `events_*` / `traces` / `observations`，靠 `sessionId` 归属。
- 会话索引（会话 id 列表、首末时间等）→ PostgreSQL `langfuse`.`trace_sessions`。
- **本项目的 `sessionId` 就是 LangGraph 的 `thread_id`**（前端会话 URL 里的 id 直接可用）。

---

## 1. ClickHouse —— 库 `nl2sql`（共 13 个对象）

> 由 `CLICKHOUSE_DB=nl2sql` 决定（已同时传给 `langfuse-web` / `langfuse-worker` / `clickhouse` 三个服务）。改这个变量 = 换一个库：新库会自动迁移建表，但**旧库数据不搬**，UI 里历史追踪会「消失」。✅

### 1.1 数据表（9 张）

| 表名 | 存什么 | 本项目用法 | 核实 |
|---|---|---|---|
| `traces` | **旧版（v3）trace 主表**。一行 = 一条 trace：id、时间戳、name、session 归属、user、tags、metadata、input/output。含 `timestamp` 列（DateTime64）。 | 不直读；`LANGFUSE_MIGRATION_V4_WRITE_MODE=dual` 保证新数据仍双写一份进来 | ✅ 表存在 / ⚠️ 列结构 |
| `observations` | **旧版（v3）observation 表**。一行 = 一次 span / generation / event，挂 `trace_id`：起止时间、输入输出、模型名、token usage。 | 不直读 | ✅ 表存在 / ⚠️ 列结构 |
| `scores` | **评分**。一行 = 一个分：挂 trace 或 observation，`name`（维度名）、`value`、`source`（API / EVAL / ANNOTATION）、`comment`、`data_type`。 | 本项目打分最终都落这里；删除噪声分也走这里（`analytics_scores` 无副本，删一次即全清） | ✅ |
| `events_core` | **v4 事件表（核心列）**——v4 起 trace/observation/score 的**新写入路径**，只存用于过滤/聚合/列表的轻量列（含 `sessionId`、`type`、`isRootObservation` 等，camelCase）。 | 本项目的 v4 读接口（`/api/public/v2/observations`）实际读的就是它 | ✅ api / ⚠️ 列结构 |
| `events_full` | **v4 事件表（全量载荷）**——按事件 id 关联 `events_core`，存 input/output/metadata 这类大字段。 | 同上（`get_trace_input` / `session_question` 走的取数路径） | ✅ api / ⚠️ 列结构 |
| `observations_batch_staging` | **observation 批量写入暂存表**。worker 攒批写入时的中间落地，正常写完即被消费。 | 无 | ⚠️ 推断 |
| `blob_storage_file_log` | **blob/对象存储导出日志**。记录哪些事件范围已归档到 S3（MinIO），避免重复导出。 | 无 | ⚠️ 推断 |
| `dataset_run_items_rmt` | **Dataset Run 明细**（ReplacingMergeTree）。实验（Experiment / Dataset Run）每条 item 的执行记录。 | 离线实验落库、Experiment 页读它 | ✅ 用途 / ⚠️ 列结构 |
| `schema_migrations` | **迁移版本表**。CH 侧已应用的迁移记录（本部署 92 行）。 | **不要 TRUNCATE** | ✅ |

### 1.2 视图（3）与物化视图（1）

| 对象 | 说明 | 核实 |
|---|---|---|
| `events_core_mv` | 物化视图，把写入投递到 `events_core` / `events_full`。**无需单独清理**，随基表自动清空。 | ✅ |
| `analytics_scores` | **建在 `scores` 之上的 VIEW**（无数据副本，删基表即全清）。 | ✅ |
| `analytics_*`（另 2 个） | 同族视图，**本仓库文档未记录具体名字**。用 §4 命令一查即得。 | ❌ 待补 |

> 注意：清游标 `analytics_*` 时不要当表删——它们是视图，删了要重建。

### 1.3 读接口分两套（本项目踩过的坑）

| 读接口 | 实际读哪 | 状态 |
|---|---|---|
| **v4 原生**：`GET /api/public/v2/observations`、`GET /api/public/v3/scores` | **事件表**（`events_core` / `events_full`） | ✅ 本项目一律走这套 |
| **legacy**：`GET /api/public/traces`、`GET /api/public/scores` | events 写模式下**读不到本应用写入的数据**（`events_only` 直接 404；`dual` 返回 200 但为空） | ❌ 弃用 |

**v4 没有独立的 trace 资源**：主 trace 由 **root observation** 表达（本项目埋点是 name=`chat_agent` / `nl2sql_agent` 的 AGENT 根观测）；trace 级业务元数据（`prompt.label` / workspace / skills / db_name / thread_id）挂在该 root observation 的 `metadata` 上。

关键环境变量：`LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL=false` —— 意味着**历史 v3 数据不会回填进事件表**。所以「UI 里看不到早期追踪」有两种截然不同的可能：数据真没了（在 `traces` 表里查一下就知道），或只是没进事件表 / 时间筛选取窄了。**先查时间筛选器，再查表。**

### 1.4 时区

CH 的 `DateTime64` 存的是**瞬时**（epoch），读出来的字符串由**会话时区**决定；本部署服务器会话时区是 UTC，所以直读 `nl2sql` 出来是 UTC。

已建好北京时间视图层 **`langfuse_bj`**（由 `create_bj_views.sh` 生成，对每个含 DateTime 列的对象 `CREATE OR REPLACE VIEW … SELECT * REPLACE (toTimeZone(col,'Asia/Shanghai') …)`）：**同一份数据、只改时间列渲染**，供 BI / NL2SQL 直接查。表结构变动后重跑脚本刷新即可；撤销 = `DROP DATABASE langfuse_bj`。

**给 BI / NL2SQL 接数据源**：host `192.168.25.64`、port **`18123`**（HTTP 协议，不是 9000）、database 选 `nl2sql`（UTC）或 `langfuse_bj`（北京时间）。

---

## 2. PostgreSQL —— 库 `langfuse`（共 72 张表）

> 另有默认 `postgres` 库（Langfuse 不用）。表由容器启动时的 prisma migration 自动创建。
> ⚠️ **本节只核实了 14/72。** 其余 58 张：本机无 Langfuse 源码、无外网、无服务器访问权限，**不臆造**——跑 §2.2 的命令把表名贴回来，即可补齐（含 `DESCRIBE` 式的列级信息）。

### 2.1 已核实（14 张）

| 表名 | 存什么 | 核实来源 |
|---|---|---|
| `Account` | **登录账号**（OAuth/密码登录凭据）。 | README §3.2 |
| `Session` | **登录会话**（浏览器登录态，≠ 观测里的 session，别混）。 | README §3.2 |
| `api_keys` | **项目 API Key**（`pk-lf-…` / `sk-lf-…`），应用侧接入凭据。 | README §3.2 |
| `trace_sessions` | **观测会话索引**——前端 Sessions 页的会话列表来源（会话 id、首末时间等）。 | README §3.2 |
| `dataset_runs` | **Dataset Run 元数据**——一次实验运行（run name、描述、时间、关联 dataset）。 | README §3.2 |
| `dataset_items` | **数据集条目**——badcase / goodcase 数据集里的每条样本（question、期望值、metadata）。 | ⚠️ 仅 SDK 调用面证据，表名待确认 |
| `pending_deletions` | **异步删除队列**——删 project / dataset / traces 是异步的，这里记录待删对象与进度。 | README §3.2（本项目在 Langfuse 数据清理手册 §2 有使用） |
| `audit_logs` | **审计日志**——谁在什么时候改了项目配置 / 成员 / 凭据。 | README §3.2 |
| `models` | **模型定义**——项目里可用模型条目（名称、匹配规则）。 | README §3.2 |
| `prices` | **模型价格表**——按模型/token 类型定价，用于算成本。 | README §3.2 |
| `llm_api_keys` | **LLM provider 凭据**——供 evaluator / playground 调模型用（本项目：`ollama-weint`、`bailianyun` 连接就存这里）。 | 评估精准化 §12.1 |
| `default_llm_models` | **项目默认模型**——哪个连接+模型做默认 judge（本项目当前 = `ollama-weint/qwen3.8:27b`）。 | 评估精准化 §12.1、§11.1 Q3 |
| `evaluator_versions` | **评估器版本**——LLM-judge / code evaluator 的配置快照（本项目有 `nl2sql-evaluator`、`SQL Semantic Equivalence`）。 | 评估精准化 §12.1 |
| `evaluation_rules` | **评估触发规则**——什么时候自动跑哪个 evaluator（可置 INACTIVE 停用）。 | 评估精准化 §12.1 |
| `job_executions` | **任务执行记录**——worker 跑评估 / 迁移等后台任务的执行状态。 | 评估精准化 §12.1 |

（上表实际 15 行，其中 `dataset_items` 标了 ⚠️，故称「核实 14 张」。）

### 2.2 待补 58 张 —— 跑这两条命令即可补齐

```bash
# ① 全部表名 + 行数（一眼看出哪些是空表，空表可以少花笔墨）
docker exec langfuse_postgres_1 psql -U langfuse -d langfuse -c "
SELECT c.relname AS table_name,
       COALESCE(s.n_live_tup, 0) AS rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY rows DESC, c.relname;"

# ② 只看名字（更短）
docker exec langfuse_postgres_1 psql -U langfuse -d langfuse -c "\dt public.*"
```

> 只要 ① 的输出贴回来就够写「每张表存什么」——**行数是最强的语义线索**（空表 = 没启用的功能；有数据的表 = 当前实际在用）。

### 2.3 本项目的读写路径

本项目**不直连任何 Langfuse 数据库**，全部走 REST API：

| 用途 | 走法 |
|---|---|
| 读 trace / observation | `GET /api/public/v2/observations`（按 `sessionId` + `isRootObservation=true` 过滤） |
| 读 score | `GET /api/public/v3/scores`（SDK `scores_v3.get_many_v3`） |
| 写 score | `create_score(...)`（trace 级，旁路，不阻塞主流程） |
| 提示词 | Prompt 管理接口（`get_prompt_text` / `create_prompt` / `update_prompt_labels`） |
| 数据集 | `api.datasets` / `api.dataset_items`（`dataset_items.list` 每页 ≤100，需翻页） |

代码位置：`src/agent/trace/langfuse_client.py`（写侧 + 回调）、`src/agent/trace/langfuse_v4_reads.py`（v4 读侧）。

---

## 3. 边界与坑（写这份文档时确认过的）

1. **`CLICKHOUSE_DB` 必须在 compose 的 `environment:` 里显式引用**——Docker 只注入 compose 列出的变量，仅写在 `.env` 里的变量永远不生效（`.env` 只用于 `${VAR}` 插值）。历史上就因为这个，表建到了 `default` 库（2026-09-14 已切到 `nl2sql` 并清理干净，`default` 现有 0 个对象）。
2. **切库 ≠ 搬数据**。新库自动迁移建表，旧库数据留在原地，UI 上表现为「历史追踪消失」。
3. **删 trace 会连带删 Session 视图条目**——Session 详情页由 trace 派生，没有独立的删除端点；删掉某会话的所有 trace，该会话就从列表里消失了。
4. **`events_only` vs `dual`** 决定了 legacy 读接口是否还有数据。本项目所有读路径都已迁到 v4 原生接口，不受影响。
5. **Data Retention 不在本项目启用**（Project Settings 里的保留天数，≥3 天，Langfuse 每晚自动清超窗数据）——排查「数据自动没了」时先确认这里是不是被设过，**再**怀疑数据库。

---

## 附录：本清单的核实来源

| 出处 | 提供的事实 |
|---|---|
| `docs/langfuse平台/langfuse部署脚本/README.md` §3.1 / §3.2 / §7.2 / §9.2 / §9.3 / §10 | CH 13 个对象、PG 72 张表与 9 个表名、切库记录、`langfuse_bj` 视图层、compose 变量注入规则 |
| `docs/langfuse平台/langfuse部署脚本/create_bj_views.sh` | `traces.timestamp` 列、视图层生成方式 |
| `docs/langfuse平台/langfuse部署脚本/verify_ch_db.sh` | CH 核对命令、`schema_migrations` 期望 92 行 |
| `docs/langfuse平台/NL2SQL-评估精准化设计方案.md` §11.1 / §12.1 | `llm_api_keys`、`default_llm_models`、`evaluator_versions`、`evaluation_rules`、`job_executions`、`analytics_scores` 是 `scores` 的视图 |
| `src/agent/trace/langfuse_v4_reads.py` 模块头注释 + `_session_root_obs_filter` / `find_session_main_trace_id` | v4 无独立 trace 资源、`sessionId=thread_id`、root observation 承载 trace 元数据、legacy 读接口失效 |
| `src/agent/trace/langfuse_client.py` | 写侧能力清单（score / prompt / 回调 / flush） |
| `docs/langfuse平台/Langfuse数据清理手册.md` §2 / §8.3 / §C | 删除模型、Data Retention、本系统数据形态对照 |

**未核实的部分已在上文逐条标注 ⚠️ / ❌，请勿当作事实引用。**
