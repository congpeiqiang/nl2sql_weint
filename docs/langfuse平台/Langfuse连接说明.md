# Langfuse 平台连接说明（自托管 · v4）

> 部署服务器：阿里云 ECS `8.163.4.42`（Ubuntu 22.04.5 LTS，3.4GB 内存 / 2 核）
> 部署方式：Docker Compose 自托管 · Langfuse **v4.16.0**（web + worker + ClickHouse + MinIO + Redis + PostgreSQL）
> 更新时间：2026-08-24（v4 平台升级完成，健康检查 version=4.16.0；读接口按 M8 迁移到 v4 原生 API）

---

## 1. 平台访问

| 项目 | 值 |
|------|-----|
| **Web 地址** | `http://8.163.4.42:3001`（安全组 TCP 3001 已放行 ✅） |
| 健康检查 | `http://8.163.4.42:3001/api/public/health` → `{"status":"OK","version":"4.16.0"}` |
| 版本 | Langfuse v4.16.0（v4 平台，写模式 dual，见 §9） |

## 2. 部署架构

```
   浏览器 / Agent SDK（NL2SQL Agent 等）
              │  HTTP 3001
              ▼
   ┌────────────────────────────────────────────┐
   │          Docker 网络 langfuse_default       │
   │                                            │
   │   ┌──────────────┐    ┌───────────┐        │
   │   │ langfuse-web │───▶│   redis   │  队列/缓存│
   │   │  (UI + API)  │    └───────────┘        │
   │   └───────┬──────┘                         │
   │           │ 内部                        ┌───────────┐
   │   ┌───────▼──────┐                      │   minio   │  S3 媒体/事件
   │   │worker(入库)  │─────────────────────▶│           │
   │   └───────┬──────┘                      └───────────┘
   │           │ 写入
   │   ┌───────▼──────┐
   │   │  clickhouse  │   trace/观测数据
   │   └──────────────┘
   └───────────┬────────────────────────────────┘
               │ 172.17.0.1:5432（docker 网关 → 宿主机）
   ┌───────────▼────────────────────────────────┐
   │  宿主机 PostgreSQL 14.24（systemd 服务）    │  账号/项目/元数据
   │  /var/lib/postgresql/14/main                │
   └────────────────────────────────────────────┘
```

**组件运行方式**（注意：PostgreSQL 不是容器）：

| 组件 | 运行方式 | 标识 | 数据位置 |
|------|----------|------|----------|
| langfuse-web | Docker 容器 | `langfuse-langfuse-web-1` | 无（应用代码） |
| langfuse-worker | Docker 容器 | `langfuse-langfuse-worker-1` | 无（应用代码） |
| ClickHouse | Docker 容器 | `langfuse-clickhouse-1` | volume `langfuse_langfuse_clickhouse_data` |
| Redis | Docker 容器 | `langfuse-redis-1` | volume `langfuse_langfuse_redis_data` |
| MinIO | Docker 容器 | `langfuse-minio-1` | volume `langfuse_langfuse_minio_data` |
| **PostgreSQL** | **宿主机 systemd 服务（apt 安装，非容器）** | `postgresql.service` | `/var/lib/postgresql/14/main` |

**数据流**：SDK/浏览器 → web（3001）→ Redis 队列 → worker 消费 → ClickHouse（trace/观测）+ PostgreSQL（账号/项目/元数据）；媒体与事件文件存 MinIO（S3）。

> 设计说明：PostgreSQL 复用宿主机已装的系统服务（最初为 Chinook 库安装），未按官方 compose 再起一个 postgres 容器——省内存（约 200–300MB）、避免 5432 端口冲突。`docker ps` 看不到 postgres 容器属正常现象。

## 3. 版本清单（2026-08-24 实测）

| 组件 | 版本 | 镜像 / 来源 |
|------|------|-------------|
| **Langfuse** | **4.16.0** | `langfuse/langfuse:4`（web）+ `langfuse/langfuse-worker:4`（worker） |
| PostgreSQL | **14.24**（apt 自动升级自 14.23） | 宿主机 apt 安装（非容器） |
| ClickHouse | 25.12.11.4 | `clickhouse/clickhouse-server:25.12` |
| Redis | 7.4.11（standalone） | `redis:7` |
| MinIO | RELEASE.2024-05-28T17-19-04Z | `minio/minio` |
| Docker | 29.1.3 | 宿主机 |
| Docker Compose | 2.40.3 | 宿主机（docker-compose-v2 包） |

## 4. 管理员账号（已自动创建，直接登录）

| 项目 | 值 |
|------|-----|
| 登录地址 | `http://8.163.4.42:3001` |
| 账号 | `admin@langfuse.local` |
| 密码 | `Langfuse@f3b933a41e9d` |
| 组织 / 项目 | AgentLab / `default`（proj_7qzwty81） |

## 5. SDK / 应用接入参数（项目已建好，密钥直接可用）

```text
LANGFUSE_PUBLIC_KEY   = pk-cd49050e51b62fdcf1a4784e54ccd1ab
LANGFUSE_SECRET_KEY   = sk-de66697585ac4f49915f0259b0ce9d54a59206821070dfc4b3fccf635a13d59f
LANGFUSE_HOST         = http://8.163.4.42:3001
```

Python 示例（`langfuse` 包）：

```python
from langfuse import Langfuse

langfuse = Langfuse(
    public_key="pk-cd49050e51b62fdcf1a4784e54ccd1ab",
    secret_key="sk-de66697585ac4f49915f0259b0ce9d54a59206821070dfc4b3fccf635a13d59f",
    host="http://8.163.4.42:3001",
)
```

## 6. 底层依赖数据库连接信息

### 6.1 PostgreSQL（Langfuse 主库：账号 / 项目 / 元数据）— 宿主机服务

| 项目 | 值 |
|------|-----|
| 类型 | PostgreSQL **14.24**（宿主机 systemd 服务，非容器） |
| 监听 | 0.0.0.0:5432（含 docker 内网可达） |
| 连接地址 | 容器内：`172.17.0.1:5432`；宿主机：`127.0.0.1:5432`；外部：`8.163.4.42:5432`（已放行 ✅） |
| 数据库 | `langfuse`（业务库，424 个 Prisma 迁移）；另有 `chinook`（NL2SQL 用）、`langfuse_v2`（旧 v2 遗留，可清理） |
| 用户 / 密码 | `langfuse` / `lf_Pg_8f3kQ2x` |
| 连接串 | `postgresql://langfuse:lf_Pg_8f3kQ2x@127.0.0.1:5432/langfuse` |

```bash
# 宿主机连接
psql -h 127.0.0.1 -U langfuse -d langfuse
# 容器内连接（在 langfuse-web 里）
psql postgresql://langfuse:lf_Pg_8f3kQ2x@172.17.0.1:5432/langfuse
```

### 6.2 ClickHouse（trace / 观测数据存储）— 容器

| 项目 | 值 |
|------|-----|
| 类型 | ClickHouse **25.12.11.4**（容器 `langfuse-clickhouse-1`） |
| 监听 | 仅 `127.0.0.1:8123`（HTTP）/ `127.0.0.1:9000`（native），外网不可达 |
| 容器内主机名 | `clickhouse` |
| 用户 / 密码 | `clickhouse` / `2d0171cb019a5ebd4779a2786a4b1cd1` |
| 数据库 | `default`（含 langfuse 表：`observations`、`event_log`、`schema_migrations`、`analytics_*` 等 74 个迁移） |
| 连接串 | HTTP：`http://clickhouse:8123`（容器内）；`clickhouse://clickhouse:9000`（native 迁移用） |

```bash
# 宿主机查询
clickhouse-client --host 127.0.0.1 --port 9000 --user clickhouse --password '2d0171cb019a5ebd4779a2786a4b1cd1'
# 容器内
sudo docker exec -it langfuse-clickhouse-1 clickhouse-client
```

### 6.3 Redis（队列 / 缓存，BullMQ 任务队列）— 容器

| 项目 | 值 |
|------|-----|
| 类型 | Redis **7.4.11**（standalone，容器 `langfuse-redis-1`） |
| 监听 | 仅 `127.0.0.1:6379`，外网不可达 |
| 容器内主机名 | `redis` |
| 密码 | `1695364eb649e182cf523f43832df02a`（默认用户，无用户名） |
| 用途 | 上报队列（ingestion）、任务队列（evaluation / data-retention / blobstorage 等）、缓存 |

```bash
redis-cli -h 127.0.0.1 -p 6379 -a '1695364eb649e182cf523f43832df02a' PING
```

### 6.4 MinIO（S3 对象存储：事件文件 / 媒体文件）— 容器

| 项目 | 值 |
|------|-----|
| 类型 | MinIO **RELEASE.2024-05-28T17-19-04Z**（容器 `langfuse-minio-1`） |
| S3 API | `0.0.0.0:9090`（已放行，外部可访问；健康检查 `/minio/health/live` = 200） |
| 控制台 | `127.0.0.1:9091`（仅本机） |
| 用户 / 密码 | `minio` / `HhZHmmHZn2ANtNDgR5N2X7KT` |
| Bucket | `langfuse`（前缀：`events/` 事件、`media/` 媒体） |
| 用途 | 媒体文件、事件日志的 S3 存储（web/worker 通过 `http://minio:9000` 内部访问；浏览器经 `http://8.163.4.42:9090` 取媒体） |

```bash
# mc 客户端连接
mc alias set langfuse http://127.0.0.1:9090 minio 'HhZHmmHZn2ANtNDgR5N2X7KT'
mc ls langfuse/langfuse
```

### 6.5 端口与可达性汇总

| 服务 | 端口 | 绑定 | 外网安全组 |
|------|------|------|------------|
| langfuse-web (UI/API) | 3001 | 0.0.0.0 | ✅ 已放行 |
| MinIO S3（媒体访问） | 9090 | 0.0.0.0 | ✅ 已放行（2026-08-23，health/live=200） |
| PostgreSQL（NL2SQL Agent 接入） | 5432 | 0.0.0.0 | ✅ 已放行（2026-08-23，远程认证实测通过） |
| MinIO 控制台 | 9091 | 127.0.0.1 | ❌ 仅本机 |
| ClickHouse | 8123 / 9000 | 127.0.0.1 | ❌ 仅本机 |
| Redis | 6379 | 127.0.0.1 | ❌ 仅本机 |

> 安全边界：对外仅放行 UI（3001）、MinIO（9090）、PostgreSQL（5432，仅限 NL2SQL Agent 等可信来源），其余组件按官方推荐只在本机可达。

## 7. 服务器侧部署信息

| 项目 | 值 |
|------|-----|
| 部署目录 | `/opt/langfuse`（docker-compose.yml + .env 权限 600；v2 备份 `docker-compose.v2.yml.bak`） |
| 容器 | **5 个**：langfuse-web（3001→3000）、langfuse-worker、clickhouse、minio（9090）、redis（127.0.0.1:6379） |
| PostgreSQL | **宿主机 systemd 服务**（非容器），数据目录 `/var/lib/postgresql/14/main` |
| 镜像来源 | langfuse 系列来自 `docker.m.daocloud.io`（国内镜像源） |
| 内存配置 | web 上限 1600m（Node 堆 1024MB）、worker 512m、clickhouse 900m、redis 128m、minio 256m |

## 8. 运维命令（SSH 到 8.163.4.42 后执行）

```bash
cd /opt/langfuse
sudo docker compose ps                          # 容器状态（5 个，无 postgres 属正常）
sudo docker compose logs -f langfuse-web-1      # web 日志（监控）
sudo docker compose logs -f langfuse-worker-1   # worker 日志（入库）
sudo docker compose restart                     # 重启全部
sudo systemctl status postgresql                # 数据库服务（宿主机）
sudo systemctl restart postgresql               # 重启数据库
```

## 9. 注意事项

- **PostgreSQL 为何非容器**：复用宿主机已装系统服务（省内存、避免端口冲突），Langfuse 通过 `172.17.0.1:5432` 访问。
- **当前平台（v4 · 4.16.0，2026-08-24 确认）**：写模式 `LANGFUSE_MIGRATION_V4_WRITE_MODE=dual`（= events_only 时 legacy 读接口返回 404/空）；新 trace/score 落 ClickHouse `events_core`/`events_full`，**读取需走 v4 原生接口** `v2/observations` + `v3/scores`（应用侧 M8 已迁移，见 [Langfuse接入实现方案.md](Langfuse接入实现方案.md) §M8）。镜像 tag 跟随 major 版本（3→4），以服务器 `sudo docker compose ps` 实际拉取镜像为准。
- **端到端已验证**：已实际上报并查询一条 trace（id `fb3cec15-7178-4bd1-ae73-526c885bec3d`，项目 proj_7qzwty81），全链路（API→Redis→Worker→ClickHouse）正常。
- **可恢复的其他服务**（数据保留）：
  - Neo4j：`sudo systemctl enable --now neo4j`
  - Milvus 栈：`sudo docker start milvus-etcd milvus-minio milvus-standalone attu`

## 10. 相关文件备份位置

- 工作区：`D:\code_work_space\llm\deepseek-workspace\langfuse\`
  - `docker-compose.yml`（当前 v4 定制版，含内存配置与初始化参数）
  - `.env`（全部密钥与 LANGFUSE_INIT_* 初始化参数）
  - `README.md`（完整部署说明）
  - `start_v3.sh` / `retry_v3.sh` / `e2e_test.sh` 等运维/验证脚本
