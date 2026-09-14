# Langfuse v4 部署与运维说明（192.168.25.64）

> **部署目录**：`/home/weint/apps/nl2sql/langfuse`（本目录，含 compose / .env / 验证脚本）
> **部署时间**：2026-08-27 | **版本**：Langfuse **v4.21.0** | **模式**：`LANGFUSE_MIGRATION_V4_WRITE_MODE=dual`（v3 读接口 + v4 事件双写）
> **适用服务器**：192.168.25.64（用户 `weint`，工作目录 `/home/weint/apps/nl2sql`）

## ⚠️ 操作约束（务必遵守）

1. **只允许操作 `/home/weint/apps/nl2sql` 目录下的文件**——不得编辑该目录以外的任何文件。
2. **只允许操作 `langfuse_*` 容器与 `langfuse_langfuse_*` 卷**——不得启动/停止/删除其它项目的容器与卷。
3. 所有下列命令都先 `cd /home/weint/apps/nl2sql/langfuse`，避免误操作。
4. `docker-compose` 为 **v1.29.2**（无 `docker compose` v2 插件），重建容器需 **stop → rm -f → up -d** 三步（直接 `up -d` 改配置会报 `ContainerConfig` KeyError）。

---

## 一、容器与端口

| 容器名 | 镜像 | 宿主端口 | 作用 |
|---|---|---|---|
| `langfuse_langfuse-web_1` | langfuse/langfuse:4 | **3010** → 3000 | Web UI + API |
| `langfuse_langfuse-worker_1` | langfuse/langfuse-worker:4 | 仅内网 3030 | 异步消费队列、写 ClickHouse/S3 |
| `langfuse_postgres_1` | postgres:14 | **5433** → 5432 | 元数据（用户/组织/项目/API Key/数据集/评分） |
| `langfuse_clickhouse_1` | clickhouse-server:25.12 | **18123**(HTTP) / **19000**(native) | 追踪数据（traces/observations/scores/events） |
| `langfuse_redis_1` | redis:7 | **6381** → 6379 | 缓存 + BullMQ 队列 |
| `langfuse_minio_1` | minio | **9090**(S3) / **9091**(控制台) | 事件与媒体对象存储 |

## 二、数据存储与宿主机目录 ★

**5 个命名卷**（compose project 名 = `langfuse`，故卷名前缀为 `langfuse_langfuse_`）。
Docker 命名卷在宿主机的真实目录固定为 `/var/lib/docker/volumes/<卷名>/_data`：

| 卷名 | 容器内挂载点 | **宿主机目录** | 存什么 | 清空后果 |
|---|---|---|---|---|
| `langfuse_langfuse_postgres_data` | `/var/lib/postgresql/data` | `/var/lib/docker/volumes/langfuse_langfuse_postgres_data/_data` | 元数据：用户、组织、项目、**API Key**、数据集、评分配置、模型价格 | ⚠️ 丢失登录账号与 API Key（需重新初始化） |
| `langfuse_langfuse_clickhouse_data` | `/var/lib/clickhouse` | `/var/lib/docker/volumes/langfuse_langfuse_clickhouse_data/_data` | 追踪数据：`traces` / `observations` / `scores` / `events_core` / `events_full` 等 | 丢失全部 trace（可接受，重跑即重建表结构） |
| `langfuse_langfuse_clickhouse_logs` | `/var/log/clickhouse-server` | `/var/lib/docker/volumes/langfuse_langfuse_clickhouse_logs/_data` | ClickHouse 日志 | 无影响 |
| `langfuse_langfuse_redis_data` | `/data` | `/var/lib/docker/volumes/langfuse_langfuse_redis_data/_data` | 缓存 + BullMQ 队列（key 前缀 `langfuse:` / `bull:`） | 无影响（队列内待处理任务会丢） |
| `langfuse_langfuse_minio_data` | `/data` | `/var/lib/docker/volumes/langfuse_langfuse_minio_data/_data` | S3 对象：事件归档与媒体文件（bucket `langfuse`） | 丢失历史事件归档与上传的图片/音频 |

> 📌 `langfuse_langfuse_clickhouse_data` 卷内部按库分目录（容器内 `/var/lib/clickhouse/data/<库名>`）。
> Langfuse 使用的库名由 `.env` 的 `CLICKHOUSE_DB` 决定（本部署为 `nl2sql`），即
> `/var/lib/docker/volumes/langfuse_langfuse_clickhouse_data/_data/data/nl2sql`。

**查看当前占用与路径**：

```bash
cd /home/weint/apps/nl2sql/langfuse
# 列出 langfuse 相关卷
docker volume ls | grep langfuse

# 查看每个卷的宿主机路径
for v in $(docker volume ls --format '{{.Name}}' | grep langfuse); do
  echo "$v -> $(docker volume inspect "$v" --format '{{.Mountpoint}}')"
done

# 查看占用大小（需 sudo，只读操作）
sudo du -sh /var/lib/docker/volumes/langfuse_langfuse_*/_data
```

## 三、清空数据库的方法 ★

> **两种方式**：
> - **A. 卷级重置**（彻底，删掉整个卷）——适合"推倒重来"，表结构由 Langfuse 启动时自动迁移重建；
> - **B. 库内清空**（保留容器与卷，只删数据）——适合日常清理，速度更快、无需重启全套服务。
>
> 通用前提：先载入 `.env` 变量，避免手敲密码
> ```bash
> cd /home/weint/apps/nl2sql/langfuse
> set -a; . ./.env; set +a      # 之后可用 $PG_PW / $REDIS_AUTH / $CLICKHOUSE_PASSWORD / $MINIO_ROOT_USER ...
> ```

### 3.1 ClickHouse（追踪数据）—— 最常清

**当前数据布局**：`CLICKHOUSE_DB=nl2sql` 已同时传给 `langfuse-web`、`langfuse-worker` 与 `clickhouse`，
Langfuse 的**建表迁移与运行时读写全部落在 `nl2sql` 库**，共 13 张：
`traces`、`observations`、`scores`、`events_core`、`events_full`、`blob_storage_file_log`、`dataset_run_items_rmt`、`observations_batch_staging`、`schema_migrations`，以及 3 个 `analytics_*` 视图与 1 个 `events_core_mv` 物化视图。

> 历史遗留：2026-09-12 之前 `CLICKHOUSE_DB` 只写在 `clickhouse` 服务上（web/worker 未传），
> 那批表建在 `default` 库。切换库后 `default` 里的旧表（当时均为 0 行）不再被使用，
> 可按需删除：`CH "DROP TABLE default.<表名>"`（附录见 §八）。

**方式 A：卷级彻底清空（推荐，最干净）**
```bash
cd /home/weint/apps/nl2sql/langfuse
docker-compose down
docker volume rm langfuse_langfuse_clickhouse_data
docker volume rm langfuse_langfuse_clickhouse_logs
docker-compose up -d
# langfuse-web/worker 启动时会自动跑 ClickHouse migration 重建全部表结构
```
> 卷被容器占用时无法删除，**必须先 `down`**。
> ⚠️ 卷名是 `langfuse_langfuse_clickhouse_logs`（**带 s**）。

**方式 B：容器内 TRUNCATE（保留表结构与容器，不用重启）**
```bash
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"; }
CHDB="${CLICKHOUSE_DB:-nl2sql}"        # 目标库跟着 .env 走，不要写死 default

# 清空数据表（保留 schema_migrations，避免重复迁移）
for t in traces observations scores events_core events_full \
         blob_storage_file_log dataset_run_items_rmt observations_batch_staging; do
  CH "TRUNCATE TABLE $CHDB.$t"
  echo "truncated $CHDB.$t"
done

# 校验
CH "SELECT table, sum(rows) FROM system.parts WHERE active AND database='$CHDB' GROUP BY table ORDER BY 2 DESC"
```
> - 不要 TRUNCATE `schema_migrations`（记录迁移版本）。
> - 物化视图 `events_core_mv` / 视图 `analytics_*` 无需处理（视图无数据，物化视图随基表自动清空）。

### 3.2 PostgreSQL（元数据：账号 / 项目 / API Key）

**当前内容**：库 `langfuse`（另有默认 `postgres` 库），共 **72 张表**；主要表 `trace_sessions`、`dataset_runs`、`pending_deletions`、`audit_logs`、`api_keys`、`models`、`prices`、`Account`、`Session` 等。

**方式 A：卷级彻底清空（会丢账号与 API Key，需重新初始化）**
```bash
cd /home/weint/apps/nl2sql/langfuse
docker-compose down
docker volume rm langfuse_langfuse_postgres_data
docker-compose up -d
# 启动时自动执行 postgres initdb + prisma migration；
# 若 .env 中 LANGFUSE_INIT_* 仍配置，会按 headless 初始化重建管理员/组织/项目/API Key
```
> 删 PG 卷后：管理员账号、组织、项目、**API Key 全部重建**，`.env` 里的
> `LANGFUSE_INIT_PROJECT_PUBLIC_KEY / SECRET_KEY` 会重新生效；
> 若这两个 key 想保持不变，删卷前先确认 `.env` 中仍填写原值（否则会生成新 key，需同步更新应用侧配置）。

**方式 B：库内清空（保留容器、账号体系随数据一起重建）**
```bash
# 清空业务表（保留结构）—— 用 CASCADE 处理外键依赖
docker exec langfuse_postgres_1 psql -U langfuse -d langfuse -c "
DO \$\$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE 'TRUNCATE TABLE public.' || quote_ident(r.tablename) || ' CASCADE';
  END LOOP;
END \$\$;"

# 校验
docker exec langfuse_postgres_1 psql -U langfuse -d langfuse -c \
  "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;"
```
> 更彻底的库内重置（重建整个 schema，Langfuse 启动时会重新 migrate）：
> ```bash
> docker exec langfuse_postgres_1 psql -U langfuse -d langfuse -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
> docker-compose restart langfuse-web langfuse-worker
> ```

### 3.3 Redis（缓存 + 队列）

**当前内容**：约 591 个 key（`langfuse:` 前缀 ≈373 个，`bull:` 前缀 ≈217 个队列 key），内存约 8MB。

**方式 A：清空数据（推荐，Redis 是缓存/队列，清空无副作用）**
```bash
docker exec langfuse_redis_1 redis-cli -a "$REDIS_AUTH" --no-auth-warning FLUSHALL
# 校验
docker exec langfuse_redis_1 redis-cli -a "$REDIS_AUTH" --no-auth-warning DBSIZE
```

**方式 B：卷级彻底清空**
```bash
cd /home/weint/apps/nl2sql/langfuse
docker-compose down
docker volume rm langfuse_langfuse_redis_data
docker-compose up -d
```
> 清空后：缓存失效（首次访问略慢）；`bull:` 队列中**未消费的任务会丢失**，正在进行的 worker 任务可能中断，建议在无写入时操作。

### 3.4 MinIO（S3 事件 / 媒体文件）

**当前内容**：bucket `langfuse`，约 **3.8 GiB / 9750 个对象**。

**方式 A：清空 bucket 内容（保留 bucket 与容器）**
```bash
docker exec langfuse_minio_1 sh -c '
  mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null &&
  mc rm --recursive --force local/langfuse &&
  mc du local/langfuse'
```

**方式 B：卷级彻底清空**
```bash
cd /home/weint/apps/nl2sql/langfuse
docker-compose down
docker volume rm langfuse_langfuse_minio_data
docker-compose up -d
# 容器 entrypoint 会自动重建 /data/langfuse 目录；bucket 由 Langfuse 首次写入时创建
```

### 3.5 全量重置（清空全部 5 个卷）

```bash
cd /home/weint/apps/nl2sql/langfuse
docker-compose down
docker volume rm \
  langfuse_langfuse_postgres_data \
  langfuse_langfuse_clickhouse_data \
  langfuse_langfuse_clickhouse_logs \
  langfuse_langfuse_minio_data \
  langfuse_langfuse_redis_data
docker-compose up -d

# 等待初始化（约 1~2 分钟），然后校验
docker-compose ps
curl -s http://127.0.0.1:3010/api/public/health
```
> 全量重置后：账号/项目/API Key 按 `.env` 的 `LANGFUSE_INIT_*` 重建；CH 表结构自动迁移重建。
> **务必把 `.env` 中新的 pk/sk 同步到使用方（如 nl2sql 的 `.env.prod`），否则上报会 401。**

## 四、常用运维命令

```bash
cd /home/weint/apps/nl2sql/langfuse
set -a; . ./.env; set +a                       # 载入变量（后续命令可用 $PG_PW 等）

docker-compose ps                              # 状态
docker-compose logs -f langfuse-langfuse-web_1     # Web 日志
docker-compose logs -f langfuse-langfuse-worker_1  # Worker 日志
docker-compose restart                         # 重启全部
docker-compose stop langfuse-web && docker-compose rm -f langfuse-web && docker-compose up -d langfuse-web   # 单服务重建（v1 三步法）
# ⚠️ 改过 compose 的 environment 后再 `up -d`：Docker 29 + compose v1 重建容器会报 KeyError: 'ContainerConfig'，
#    必须先 `stop` + `rm -f` 目标服务再 `up -d`；若出现 `<id>_langfuse_xxx_1` 僵尸容器，先 `docker rm -f` 它。

# 健康检查
curl -s http://127.0.0.1:3010/api/public/health

# 数据量速查（库名跟 .env 的 CLICKHOUSE_DB 一致，本部署为 nl2sql）
docker exec langfuse_clickhouse_1 clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q \
  "SELECT table, sum(rows) FROM system.parts WHERE active AND database='${CLICKHOUSE_DB:-nl2sql}' GROUP BY table ORDER BY 2 DESC"
docker exec langfuse_postgres_1 psql -U langfuse -d langfuse -c \
  "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;"
docker exec langfuse_redis_1 redis-cli -a "$REDIS_AUTH" --no-auth-warning DBSIZE
docker exec langfuse_minio_1 sh -c 'mc du local/langfuse'

# 备份（导出到本目录，注意勿导出到 nl2sql 以外路径）
docker exec langfuse_postgres_1 pg_dump -U langfuse langfuse | gzip > backup_pg_$(date +%Y%m%d).sql.gz
```

## 五、访问入口与账号

| 服务 | 地址 |
|---|---|
| Langfuse Web | http://192.168.25.64:3010 |
| 健康检查 | http://192.168.25.64:3010/api/public/health → `{"status":"OK","version":"4.21.0"}` |
| MinIO S3 / 控制台 | http://192.168.25.64:9090 / http://192.168.25.64:9091 |
| PostgreSQL | 192.168.25.64:5433（用户 `langfuse`，库 `langfuse`，密码 `$PG_PW`） |
| ClickHouse HTTP / native | 192.168.25.64:18123 / 19000（用户 `$CLICKHOUSE_USER`，密码 `$CLICKHOUSE_PASSWORD`） |
| Redis | 192.168.25.64:6381（密码 `$REDIS_AUTH`） |

| 账号项 | 值 |
|---|---|
| 管理员 | `admin@langfuse.local` / `Langfuse@GxzKAlruuC`（见 .env `LANGFUSE_INIT_USER_PASSWORD`） |
| 组织 | AgentLab（`org_mzZBzMZr`） |
| 项目 | `nlsql`（`proj_6nMyvFmN`）——名称来自 `.env` 的 `LANGFUSE_INIT_PROJECT_NAME`；**与 ClickHouse 库名 `nl2sql` 无关**，如需改名在 UI 的 Project Settings 里改（不影响任何已存数据与 API Key） |
| 公钥 pk | `pk-c64d8d357fa8e8b90e8aefb8183a2cea` |
| 密钥 sk | `sk-3791dd2504c88ca7502c987768ac35e907366db9260eec07` |

> ⚠️ 若重建了 PostgreSQL 卷，pk/sk 会按 `.env` 的 `LANGFUSE_INIT_PROJECT_*` 重新生成，请同步到调用方配置。

## 六、本目录文件说明

| 文件 | 用途 |
|---|---|
| `docker-compose.yml` | 6 服务 + 5 卷定义（v1.29.2 兼容，`version: "3.8"`） |
| `.env` | 全部密钥与初始化参数（权限 600，**含 pk/sk、PG/CH/Redis/MinIO 密码**；**已加入本目录 `.gitignore`，不入库**） |
| `.env.example` | `.env` 的脱敏模板（占位值 + 每项用途说明），用于在新环境重建 `.env` |
| `.gitignore` | 忽略 `.env`、`venv/`、`*.bak-*` |
| `README.md` | 本文件 |
| `pull_images.sh` / `pull_images2.sh` | 经 daocloud 镜像源拉取 langfuse/clickhouse 镜像并打标准 tag |
| `mirror_diag.sh` | 镜像源连通性诊断 |
| `ports_check.sh` | 端口占用检查（部署前确认 3010/5433/18123/19000/6381/9090/9091 空闲） |
| `verify_v4.sh` | v4 部署后验证脚本 |
| `verify_ch_db.sh` | **校验 CH 落库库名**：打印三容器 `CLICKHOUSE_DB`、`nl2sql` 库对象数、`schema_migrations` 行数、各表行数与 `default` 残留 |
| `sdk_e2e_new.sh` / `sdk_test.py` | v4 SDK 端到端上报验证（chain + generation；查询语句查的是客户端默认库） |
| `sdk_e2e_nl2sql.sh` | **v4 SDK 端到端 + 校验落到 `${CLICKHOUSE_DB}`**（按 `.env` 的 pk/sk 上报，再核对 `nl2sql` 与 `default` 的行数变化） |
| `cleanup_default_db.sh` | 删除 `default` 库中切换目标库之前遗留的 Langfuse 旧表（先列对象，`--yes` 跳过交互确认） |
| `create_bj_views.sh` | 生成/刷新 `langfuse_bj` 北京时间视图层（时间列按 Asia/Shanghai 渲染，不改原表） |
| `ch_time_probe.sh` / `ch_time_probe3.sh` | 时区语义排查：服务器/列时区、写入解析实验、`session_timezone` 读写对照 |
| `.env.example` | `.env` 的脱敏模板（占位值 + 每项用途说明） |
| `venv/` | 验证脚本用的 Python 虚拟环境 |

## 七、端到端验证记录

### 7.1 初始部署（2026-08-27）

- 健康检查 OK、UI 200、6 容器全部 `Up (healthy)`
- v4 SDK（langfuse 4.14.5）上报 chain + generation 成功，ClickHouse `traces`/`observations` 与
  `events_core`/`events_full` 均可见类型化事件（CHAIN / GENERATION）
- SDK 用法（v4 API，与 v3 不同）：

```python
from langfuse import Langfuse
langfuse = Langfuse(public_key="pk-...", secret_key="sk-...", host="http://192.168.25.64:3010")
with langfuse.start_as_current_observation(name="chain-name", as_type="chain", input=..., output=...):
    with langfuse.start_as_current_observation(name="gen", as_type="generation", model="gpt-4o", input=..., output=...):
        pass
langfuse.flush()
```

### 7.2 切换到 `nl2sql` 库（2026-09-14 11:30）

改动：`.env` 新增 `CLICKHOUSE_DB=nl2sql`；compose 中 `langfuse-web`、`langfuse-worker`、`clickhouse` 三处
`environment` 均引用 `CLICKHOUSE_DB: ${CLICKHOUSE_DB:-nl2sql}`。

执行过程与结果（`bash verify_ch_db.sh` + `bash sdk_e2e_nl2sql.sh`）：

| 检查项 | 结果 |
|---|---|
| 三容器 `printenv CLICKHOUSE_DB` | `langfuse_clickhouse_1` / `langfuse_langfuse-web_1` / `langfuse_langfuse-worker_1` 均为 `nl2sql` |
| `nl2sql` 库对象 | 13 个（`traces`/`observations`/`scores`/`events_core`/`events_full`/`blob_storage_file_log`/`dataset_run_items_rmt`/`observations_batch_staging`/`schema_migrations` + 3 视图 + 1 物化视图） |
| `nl2sql.schema_migrations` | 92（全部迁移已在目标库执行） |
| SDK 上报 `v4-sdk-0914-113012` | `nl2sql.traces` 1 行、`nl2sql.observations` 2 行（CHAIN + GENERATION）、`nl2sql.events_core`/`events_full` 各 2 行 |
| `default` 库 | 行数**未增加**（仍是切换前的 2 traces / 9 observations），证明写入已完全走 `nl2sql` |
| `default` 库清理 | 已用 `cleanup_default_db.sh --yes` 删除全部 14 个遗留对象（含 3 视图 + 1 物化视图），`default` 现为 0 对象；清理后再上报一次（`v4-sdk-0914-113224`）仍正常落入 `nl2sql` |

> ⚠️ **执行顺序坑**：Docker 29.1.3 + docker-compose v1.29.2 下，`up -d` 去「重建已存在的容器」会抛
> `KeyError: 'ContainerConfig'`，并留下一个形如 `<容器ID前12位>_langfuse_clickhouse_1` 的僵尸容器。
> 正确姿势：`docker rm -f <僵尸容器名>` → `docker-compose stop <服务>` → `docker-compose rm -f <服务>` → `docker-compose up -d`。

## 八、注意事项

- **部署隔离**：本栈 6 容器 + 5 卷全部独立新建，未复用/未触碰服务器既有容器、镜像、卷、compose 项目。
- **删除卷的顺序**：必须 `docker-compose down` → `docker volume rm` → `docker-compose up -d`；容器运行中卷无法删除。
- **重建容器的顺序（Docker 29 + compose v1 特有）**：只要 compose 的 `environment:` 有改动，`up -d` 会尝试重建容器并抛
  `KeyError: 'ContainerConfig'`（compose v1 读取新版 Docker 镜像元数据失败），同时留下 `<id>_<服务名>_1` 僵尸容器。
  处理：`docker rm -f <僵尸容器>` → `docker-compose stop <服务>` → `docker-compose rm -f <服务>` → `docker-compose up -d`。
- **迁移自愈**：pg 与 clickhouse 的表结构由 langfuse-web/worker 启动时的 migration 自动创建，删卷后 `up -d` 即自动重建，无需手工建表。
- **v4 双写**：`LANGFUSE_MIGRATION_V4_WRITE_MODE=dual` 同时保留 v3 读接口与 v4 事件写入；如需只保留 v4，改 `.env` 后重启（本文档不涉及）。
- **Langfuse「项目」≠ ClickHouse「库」**：Project 是 PostgreSQL 里的逻辑租户，所有项目**共用同一套 CH 表**，靠行内 `project_id` 列区分；
  UI 里新建/改名项目**不会**创建或切换 CH 库。CH 库名只由 `.env` 的 `CLICKHOUSE_DB` 决定。
- **ClickHouse 落库位置由 `.env` 的 `CLICKHOUSE_DB` 决定（本部署 = `nl2sql`）★**：
  Langfuse v4 的两条路径**都读取该变量**（已在镜像中核对，`langfuse/langfuse:4` = 4.21.0、`langfuse/langfuse-worker:4` = 4.22.0）：
  1. **建表迁移**：`langfuse-web` 的 entrypoint 执行 `packages/shared/clickhouse/scripts/up.sh`，
     golang-migrate 连接串为 `CLICKHOUSE_MIGRATION_URL?username=…&password=…&database=${CLICKHOUSE_DB}&x-multi-statement=true`；
     迁移 SQL 里用的是**不带库前缀的裸表名**（如 `CREATE TABLE traces (...)`），因此库名完全由 `CLICKHOUSE_DB` 决定。
  2. **运行时读写**：共享客户端 `createClient({ url: CLICKHOUSE_URL, username, password, database: env.CLICKHOUSE_DB, … })`
     （`@clickhouse/client` 中 `config.database ?? "default"`，即该变量缺省时才回落 `default`）。
  因此 `CLICKHOUSE_DB` **必须同时传给 `langfuse-web` 与 `langfuse-worker`**；`clickhouse` 服务上的同名变量只是给官方镜像做 `CREATE DATABASE`（保证库存在）。
  > **历史误判（2026-09-12 已修正）**：此前该变量只写在 `clickhouse` 服务上，web/worker 未传 → 两者回落到 `default`，
  > 表因此全建在 `default` 库。现 compose 三处 + `.env` 均统一为 `nl2sql`。
  校验与旧库清理：
  ```bash
  set -a; . ./.env; set +a
  CH() { docker exec langfuse_clickhouse_1 clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"; }

  CH "SHOW DATABASES"                                                   # 期望看到 nl2sql（+ 可能残留的 default）
  CH "SHOW TABLES FROM ${CLICKHOUSE_DB:-nl2sql}"                         # 期望 13 个对象
  CH "SELECT count() FROM ${CLICKHOUSE_DB:-nl2sql}.schema_migrations"    # 期望 92（迁移已跑）
  # 容器内实际生效值（三处应一致）
  docker exec langfuse_clickhouse_1 printenv CLICKHOUSE_DB
  docker exec langfuse_langfuse-web_1 printenv CLICKHOUSE_DB
  docker exec langfuse_langfuse-worker_1 printenv CLICKHOUSE_DB
  # 旧库残留表（切换前建的、当前为 0 行）可逐个删除：
  # CH "DROP TABLE default.<表名>"
  ```
  > 也可以改用 `CLICKHOUSE_URL=http://clickhouse:8123/nl2sql` 让 URL 路径指定库（`@clickhouse/client` 会把
  > pathname 解析成 `database`），但**迁移脚本仍以 `CLICKHOUSE_DB` 为准**，两处不一致会导致「建表在一个库、读写去另一个库」，
  > 故本部署统一只维护 `CLICKHOUSE_DB` 一处。
- **镜像版本**：web 4.21.0 / worker 4.22.0（两者都是 `:4` 浮动标签，不同时间拉取所致）。若要严格对齐，
  建议把 compose 中镜像改写为同一确定版本（如 `langfuse/langfuse:4.22.0` 与 `langfuse/langfuse-worker:4.22.0`）后重拉。
- **数据规模参考**（2026-09-12 实测）：MinIO ≈3.8 GiB / 9750 objects；Redis ≈591 keys；ClickHouse 数据表已清空（仅 `schema_migrations` 92 行）；PostgreSQL 元数据表行数均为个位数。

## 九、环境变量说明（`.env`）

### 9.1 修改风险分级

| 级别 | 变量 | 说明 |
|---|---|---|
| 🔴 **绝不能改** | `SALT` | **API Key 的哈希盐**（库中只存 `hash(key, SALT)`）。改动 → 现有 pk/sk **全部失效**，所有接入方上报 401，且不可恢复 |
| 🔴 **绝不能改** | `ENCRYPTION_KEY` | **对称加密密钥**，加密存储 LLM Connections 等敏感配置。改动 → 已加密数据无法解密 |
| 🟠 改则登录态失效 | `NEXTAUTH_SECRET` | 登录会话（JWT）签名密钥。改动 → 所有用户需重新登录（数据不丢） |
| 🟠 改要配套一致 | `DATABASE_URL` + `PG_PW` | 两者密码**必须一致**（前者给应用、后者供 compose 插值）。只改一处 → 应用连不上库 |
| 🟠 改要重建对应卷 | `CLICKHOUSE_PASSWORD`、`REDIS_AUTH`、`MINIO_ROOT_USER/PASSWORD` | 这些凭据在**卷首次初始化时**写入；改 `.env` 后不删对应卷**不会生效** |
| 🟡 仅首次初始化生效 | `LANGFUSE_INIT_*`（9 项） | 组织/项目/pk/sk/管理员账号，**仅在 PG 中无对应记录时**执行。想改 pk/sk 必须删 PG 卷重建，或在 UI 里新建 Key |
| 🟠 改则等于换库 | `CLICKHOUSE_DB` | **CH 目标库名**（迁移建表 + 运行时读写都用它）。改成一个**已存在的旧库**可能撞表结构；改成**新库**则自动迁移建表，但旧库里的历史 trace 不会跟过来（UI 看起来「历史全没了」）。要搬数据需手工 `INSERT INTO 新库.表 SELECT * FROM 旧库.表` |
| 🟢 可自由调整 | `NEXTAUTH_URL`、`CLICKHOUSE_URL`、`CLICKHOUSE_MIGRATION_URL`、`LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` | 按网络环境调整 |

### 9.2 关键变量职责

| 变量 | 作用 | 本部署取值要点 |
|---|---|---|
| `NEXTAUTH_URL` | 对外基址，用于登录回调与 Cookie | `http://192.168.25.64:3010`（须与实际访问地址一致，否则登不上） |
| `DATABASE_URL` | PG 连接串 | 主机名用 **compose 服务名 `postgres`**（容器内 DNS），非宿主 IP |
| `CLICKHOUSE_URL` | CH **HTTP** 端点（运行时查询/写入用） | `http://clickhouse:8123`（**URL 里不带库名**；库由 `CLICKHOUSE_DB` 指定） |
| `CLICKHOUSE_MIGRATION_URL` | CH **native** 端点（迁移用） | `clickhouse://clickhouse:9000`；两个 URL 端口不同（8123 HTTP / 9000 native），不可混用 |
| `CLICKHOUSE_DB` | **CH 目标库**：迁移建表库 + 运行时读写库 | `nl2sql`；**必须被 compose 注入 `langfuse-web` 与 `langfuse-worker`**（compose 默认值 `${CLICKHOUSE_DB:-nl2sql}`）。改库名 = 换一个库：新库会自动迁移建表，但**旧库数据不会自动搬迁**，UI 中历史追踪会「消失」 |
| `CLICKHOUSE_BJ_DB` | 北京时间视图层库名 | `langfuse_bj`；由 `create_bj_views.sh` 创建，供 BI / NL2SQL 应用读取北京时间（详见 §十） |
| `LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT` | 事件归档上传（服务端内部） | `http://minio:9000`（容器名走内网，快） |
| `LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` | 媒体文件对外访问地址 | `http://192.168.25.64:9090`（**必须浏览器可达**；若填容器名会导致页面图片全部裂开） |
| `LANGFUSE_MIGRATION_V4_WRITE_MODE` | v4 写入模式 | `dual`：同时写 v3 表与 v4 事件表（`events_core`/`events_full`），读接口仍走 v3 → 平滑过渡 |
| `LANGFUSE_LLM_CONNECTION_WHITELISTED_IPS` | LLM Connections 出网白名单（防 SSRF） | `192.168.25.13`；**须在 `langfuse-web` 与 `langfuse-worker` 两处都被 compose 引用才生效** |
| `LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN` | 是否允许 v4 预览功能 | `false` |
| `LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL` | 是否后台回填 v3 历史数据到 v4 事件表 | `false` |

### 9.3 ⚠️ 配置生效的前提：必须在 compose 中显式引用

**Docker 只把 `docker-compose.yml` 的 `environment:` 段中列出的变量注入容器**。写在 `.env` 里但 compose 未引用的变量**永远不会生效**（`.env` 仅用于 `${VAR}` 插值）。

已知踩坑（2026-09-12 修复）：
- `LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN`、`LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL` 原先只在 `.env` 中，compose 未引用 → **从未生效**；现已补入 `langfuse-web` 与 `langfuse-worker` 两处。
- **`CLICKHOUSE_DB` 原先只写在 `clickhouse` 服务上**，`langfuse-web` / `langfuse-worker` 未引用 → Langfuse 回落到 `default`，追踪表全建在 `default` 库。现已补入两个应用服务（并保留 `clickhouse` 服务上的那一处用于建库），目标库统一为 `nl2sql`。详见 §八。
- 同类问题（社区已报）：[issue #16012「Official Docker Compose does not pass LLM connection whitelist environment variables」](https://github.com/langfuse/langfuse/issues/16012)、修复 [PR #16014](https://github.com/langfuse/langfuse/pull/16014)。

**自查方法**（确认某变量是否真正进入容器）：

```bash
cd /home/weint/apps/nl2sql/langfuse
# 方式一：看 compose 解析结果里是否出现（最直观）
docker-compose config 2>/dev/null | grep -E "WHITELISTED_IPS|PREVIEW_OPT_IN|HISTORIC_BACKFILL"
# 方式二：看运行中容器的实际环境变量
docker exec langfuse_langfuse-web_1 env | grep LANGFUSE_
```

**改动 compose 后的重建方式**（docker-compose v1.29.2 三步法）：

```bash
docker-compose stop langfuse-web langfuse-worker
docker-compose rm -f langfuse-web langfuse-worker
docker-compose up -d langfuse-web langfuse-worker
```

## 十、时间与时区（北京时间）★

### 10.1 事实：ClickHouse 存的是「瞬时」，读出来的字符串由会话时区决定

- Langfuse 写入的是 **UTC 朴素字符串**（`convertDateToClickhouseDateTime` 走 `Date.toISOString()`，
  形如 `2026-09-14 03:30:14.503`，**不带时区后缀**）。
- ClickHouse 的 `DateTime64(3)` 内部存的是 epoch（瞬时）；`nl2sql` 里所有时间列的 `type` 都**不带显式时区**
  → 用**会话时区**解析与渲染。本部署服务器时区是 `UTC`（`SELECT timezone()` → `UTC`），
  所以直接查出来是 UTC 字符串（即 `03:30`，而真实北京时间是 `11:30`）。
- **改服务器时区 / 列时区都改不了读出来的字符串**：解析与渲染用同一个时区，一进一出相互抵消；
  而让写入端按北京时区解析，会把 epoch 整体挪走 8 小时（数据失真）。实测证据：

  | 实验 | 结果 |
  |---|---|
  | 同一朴素串 `2026-09-14 03:30:14.503`，`session_timezone=UTC` 写入 | epoch `1789356614503` ✅ |
  | 同上，`session_timezone=Asia/Shanghai` 写入 | epoch `1789327814503`（**差 8h，错误**） |
  | 同一行已有数据，`session_timezone=UTC` 读 | `2026-09-14 03:30:14.503` |
  | 同上，`session_timezone=Asia/Shanghai` 读 | `2026-09-14 11:30:14.503`，**epoch 完全不变** ✅ |

- 所以正确做法是**读侧指定时区**；**不要物理改写存储值**（改写会让 Langfuse 的 UI 时间显示、
  时间范围过滤与数据保留策略整体错 8 小时）。

### 10.2 三种读侧做法

| 场景 | 做法 |
|---|---|
| 临时查询（clickhouse-client） | 先 `SET session_timezone='Asia/Shanghai';` 再 SELECT（同一会话生效） |
| BI / 应用连接 | 连接参数带 `session_timezone=Asia/Shanghai`（clickhouse_connect：`settings={'session_timezone':'Asia/Shanghai'}`；JDBC：`session_timezone=Asia/Shanghai`） |
| 不想改客户端（推荐） | 直接查 `langfuse_bj` 库的视图，见 10.3 |

> ⚠️ 不要把 `session_timezone` 设为服务器级/用户级默认：Langfuse 的写入端也会跟着变，落库 epoch 会整体 -8h。

### 10.3 北京时间视图层 `langfuse_bj`（已建好）

`create_bj_views.sh` 为 `nl2sql` 中每个含时间列的对象建同名视图，时间列用
`toTimeZone(col,'Asia/Shanghai')` 固定为北京时间；**原表与原数据零改动**。

```bash
cd /home/weint/apps/nl2sql/langfuse && bash create_bj_views.sh
# 撤销：DROP DATABASE langfuse_bj
```

实测对照（同一行、同一 epoch）：

| 查询 | 结果 |
|---|---|
| `SELECT timestamp FROM nl2sql.traces ORDER BY timestamp DESC LIMIT 1` | `2026-09-14 03:32:25.527`（UTC） |
| `SELECT timestamp FROM langfuse_bj.traces ORDER BY timestamp DESC LIMIT 1` | `2026-09-14 11:32:25.527`（北京） |
| 两者 `toUnixTimestamp64Milli(timestamp)` | 均为 `1789356745527`（同一瞬时 ✅） |

视图清单（12 个）：`traces`、`observations`、`scores`、`events_core`、`events_full`、
`blob_storage_file_log`、`dataset_run_items_rmt`、`observations_batch_staging`、`schema_migrations`
与 3 个 `analytics_*`。表结构变动（新增时间列）后**重跑一次脚本**即可刷新（`CREATE OR REPLACE VIEW`）。

给 NL2SQL / BI 接数据源：host `192.168.25.64`、port `18123`（HTTP 协议）、database `langfuse_bj`、
user `clickhouse`、password 见 `.env`（仅查询用途）。

> 为什么不建「北京时间只读账号」：本部署的 `clickhouse` 用户**没有 `CREATE USER` 权限**
> （实测 `ACCESS_DENIED`，且 `default` 账号已禁用）；要么在 compose 给 clickhouse 服务加
> `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1` 并重建容器，要么用视图层。视图层不需要任何权限变更，故采用它。

### 10.4 排查脚本

| 脚本 | 作用 |
|---|---|
| `ch_time_probe.sh` | 服务器/列时区现状、traces 时间样本、写入解析实验 |
| `ch_time_probe3.sh` | `session_timezone` 读/写语义对照（读平移、写错位） |
| `create_bj_views.sh` | 生成/刷新 `langfuse_bj` 北京时间视图层 |
