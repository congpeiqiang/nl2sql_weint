# NL2SQL Agent 存储与部署架构方案

> 覆盖：**方案 A（当前单机单实例）** 与 **方案 B/C（后续多实例·多机优化）**
> 项目：nl2sql_weint（LangGraph + WrenAI + deepagents）
> 更新时间：2026-08-23

---

## 0. 结论速览

| 决策 | 结论 |
|------|------|
| 当前单机单实例 | **本地目录 + Docker volume 持久化**，不需要共享目录 |
| 灰度发版 | **不要上下文延续** → 按版本分数据目录 + Nginx 权重分流，SQLite 架构即可支撑 |
| 什么时候切 PG | 出现"多实例共享会话"或"多机扩容"需求时再切 |
| 数据分型原则 | 结构化状态→PG / 向量→pgvector / 文件产物→MinIO或共享目录 / 配置→git |
| 业务级隔离 | 不靠物理文件，靠 **thread_id 前缀 + workspace_id 列** |
| 可观测 | Langfuse v4（trace + Datasets 回归 + Scores 门禁） |

---

## 1. 方案 A：当前单机单实例（可立即落地）

### 1.1 架构

```
单机 ECS 8.163.4.42
├── LangGraph API Server（uvicorn :2026）—— 单实例
│     ├── chat_agent / nl2sql_agent
│     ├── checkpointer（AsyncSqliteSaver，工作区隔离）
│     └── custom_app（workspace/db_config/wren_semantic 等 API）
├── WrenAI 语义层（wrenai_* MCP）
├── DB MCP Server（dbmcp_* 直连 10 种引擎）
├── PostgreSQL 14.24（系统级，langfuse/chinook 等库）
├── Langfuse v4（:3001，trace/回归/监控）
└── MinIO（:9090，备用对象存储）
```

### 1.2 数据目录规划（关键：容器持久化）

```text
宿主机 /opt/agent-data/          ← 本地磁盘，不是共享盘
├── {version}/checkpoints.sqlite ← 会话状态（按版本分目录，灰度友好）
├── memory/  skills/             ← 共享记忆与技能（git 同步 + 本地副本）
├── report/  tmp/  nl2sql_process_data/
├── feedback/  db_config.json  model_config.json
```

**Docker 部署时必须挂本地卷**（容器删除不丢数据）：

```yaml
services:
  agent:
    volumes:
      - /opt/agent-data:/app/workspace   # 本地卷 = 持久化，不是共享
```

### 1.3 灰度支持（不要上下文延续）

```yaml
# docker-compose：v1 旧模型 + v2 新模型 双容器
agent-v1: { image: your-agent:1.0, environment: { MODEL: gpt-4o },  volumes: ["/opt/agent-data/v1:/app/workspace"] }
agent-v2: { image: your-agent:1.1, environment: { MODEL: deepseek }, volumes: ["/opt/agent-data/v2:/app/workspace"] }
nginx:    { ports: ["80:80"], volumes: ["./nginx.conf:/etc/nginx/nginx.conf:ro"] }
```

```nginx
upstream agents { server agent-v1:8000 weight=90; server agent-v2:8000 weight=10; }
server { listen 80; location / { proxy_pass http://agents; } }
```

- **会话分配**：新 thread_id → 按权重进 v1/v2；进行中会话固定走 v1（哈希/一致性分配）
- **回滚**：权重改回 `weight=100` → `nginx -s reload`，秒级
- **数据隔离**：v1/v2 各用各的 checkpoint 目录，灰度期互不干扰；全量后旧目录归档

### 1.4 灰度/回归/监控闭环（Langfuse）

```
代码/Prompt/Skill 更新
  → CI 跑 Langfuse Datasets 回归（nl2sql-chinook 29 题，阈值可调）
  → ✅ 通过 → 构建镜像 → 起 v2 容器 → Nginx 10% 灰度
  → Langfuse 对比 v1/v2：sql-correctness、延迟 P50/P95、成本
  → 达标 → 权重 100% → 停 v1；不达标 → 切回 + 失败案例回流 Datasets
```

### 1.5 局限（方案 A 的天花板）

- SQLite 单写者：**多实例并发写会 `database is locked`**
- 文件在本地：**多机无法共享**
- 会话状态绑定实例：**跨实例无缝延续不可能**（本方案主动放弃，发版从新会话开始）

---

## 2. 方案 B：多实例（同机灰度，共享会话）

触发条件：需要**用户会话在 v1/v2 间无缝延续**（灰度期不丢上下文）。

### 2.1 如果明确"灰度不要上下文"——方案 A 已够，跳到方案 C 再动

### 2.2 需要共享会话时的形态

| 数据 | 方案 | 原因 |
|------|------|------|
| checkpoints | **PG**（AsyncPostgresSaver） | SQLite 多写者锁死，NFS 锁不可靠 |
| feedback/结构化记忆 | **PG** | 同上 |
| query_history 向量 | **pgvector**（PG 扩展） | LanceDB 跨进程独占锁，有损坏风险 |
| 图表/报告/中间产物 | **共享目录 或 MinIO** | 低频写高频读，两者皆可（MinIO 更稳） |
| skills / Wren MDL | **git 分发** | 只读配置，无需共享盘 |

> 共享目录（NFS）**能解决"文件可见性"，解决不了 SQLite 并发写与锁语义问题**。

### 2.3 业务级隔离（防上下文污染）

**隔离不靠物理文件，靠数据维度：**

```
方案（推荐）：thread_id 加工作区前缀 + 表带 workspace_id 列

threads/checkpoints/messages 表：
| thread_id          | workspace_id | 数据          |
| aix_project/t123   | aix_project  | A 库会话历史    |
| imdb/t123          | imdb         | B 库会话历史（全新）|

查询永远带 WHERE workspace_id = 当前 —— 切库不串味
```

- 语义记忆（query_history）同样加 workspace_id 列 → 向量检索不跨库污染
- 切工作区 = 新会话（thread_id 前缀变化）；如需"摘要延续"，把结论写入新会话 memory 而非全量上下文

---

## 3. 方案 C：多机 / 高并发（目标架构）

触发条件：QPS 持续升高、要求高可用、多团队多环境。

### 3.1 目标架构

```
SLB（阿里云，可选）
 ├── ECS-1：agent 实例（docker compose）      ECS-2：agent 实例
 └── 都连同一套存储

存储层（独立于实例）：
├── PostgreSQL（可主从）→ agent_checks / agent_memory（含 pgvector）
├── MinIO / OSS → 产物对象存储（report/chart/中间结果）
└── Redis（可选）→ 分布式锁/队列
```

### 3.2 关键点

- **checkpointer**：`AsyncPostgresSaver`（LangGraph 官方支持），多实例同库并发
- **LangGraph Store**：memory 用 Postgres 后端，跨实例一致
- **产物**：一律写 MinIO/OSS（本地磁盘只放代码），PG 存 `artifact_meta`（path/type/thread_id/trace_id）
- **配置下发**：db_config / model_config 可放 PG 或配置中心，实例热加载
- **幂等与锁**：跨实例任务（如数据回流）用 PG 行锁或 Redis

### 3.3 迁移三步走（风险递减）

```
Step 1: checkpoints SQLite → PG（AsyncPostgresSaver + workspace_id 列）
        改动：checkpointer_factory.py（保留动态工作区逻辑）

Step 2: 产物本地文件 → MinIO + artifact_meta 表
        改动：report/tmp 写路径换 SDK；前端读 URL

Step 3: 语义记忆 LanceDB → pgvector（保留兼容层，逐步迁移）
        改动：semantic/query_history 读写抽象
```

每步可独立灰度，互不阻塞。

---

## 4. 数据分型对照表（贯穿三个方案）

| 数据类型 | 现状 | 单机单实例 | 多实例/多机 | 最佳载体 |
|----------|------|-----------|-------------|----------|
| checkpoints 会话状态 | SQLite 文件 | 本地目录 ✅ | PG | PostgreSQL |
| feedback / 结构化记忆 | SQLite | 本地 ✅ | PG | PostgreSQL |
| query_history 语义向量 | LanceDB 文件 | 本地 ✅（单写） | pgvector | PostgreSQL + vector 扩展 |
| 图表/报告/中间产物 | workspace 文件 | 本地 ✅ | 共享目录/MinIO | MinIO |
| skills / Wren MDL | git | git ✅ | git 分发 | git + 挂载 |
| db_config / model_config | 文件 | 本地 ✅ | 共享配置 | PG/配置中心 |
| trace / 打分 / 回归 | — | Langfuse ✅ | Langfuse ✅ | Langfuse |

---

## 5. 灰度发版标准流程（A/B/C 通用）

```
① 更新（代码/Skill → git；Prompt → Langfuse Prompts 版本管理）
② CI 回归：Langfuse Datasets 29 题 + 阈值判定 → 拦截/放行
③ 构建镜像 → 服务器拉取
④ Nginx 权重灰度：v1(90)/v2(10)；新会话按权重进 v2
⑤ Langfuse 观察：正确率/延迟/成本对比
⑥ 达标 → 100%；不达标 → 切回 + 失败案例回流 Datasets
```

---

## 6. 基础设施现状（服务器 8.163.4.42）

| 组件 | 状态 | 用途 |
|------|------|------|
| PostgreSQL 14.24（:5432，已对外放行） | ✅ 运行中 | langfuse / chinook / 未来 agent_checks |
| Langfuse v4.16.0（:3001） | ✅ 运行中 | 监控/回归/评估 |
| MinIO（:9090，已放行） | ✅ 运行中 | 产物对象存储（langfuse bucket；agent 可加 bucket） |
| Milvus + etcd + Attu | ⏸ 已停止（数据保留） | 向量库备选（规模大时启用） |
| Neo4j | ⏸ 已停止（可恢复） | — |
| Nginx | ❌ 未安装 | 灰度分流需要时安装 |
| pgvector 扩展 | ❌ 未安装（apt: postgresql-14-pgvector） | 方案 B/C 需要 |

---

## 7. 待办事项（安全与合规）

- ⚠️ **`.env` 已被 git 跟踪且未 ignore**（含 LLM_API_KEY 等密钥）→ 合入 main 前：`.gitignore` 加 `.env` + `git rm --cached .env` + **轮换已泄露密钥**
- LangSmith 与 Langfuse 双上报 → 确认以哪个为准，避免双写
- CI 门禁落地：GitHub Actions + Langfuse 回归脚本（`regression_nl2sql.py` 模板已就绪）

---

## 8. 决策建议

> **现在**：按方案 A 落地 —— 本地目录 + Docker volume、Nginx 灰度（不要上下文）、Langfuse 回归与监控。**零新增基础设施，直接可用。**
> **近期**（需要会话无缝延续）：只做 Step 1（checkpoints → PG），其余不动。
> **远期**（多机/高并发）：按方案 C 完整迁移（PG + pgvector + MinIO）。
