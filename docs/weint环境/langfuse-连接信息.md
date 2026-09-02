# Langfuse 连接信息（weint 环境）

> 服务器：**192.168.25.64**（Ubuntu 24.04，内网）
> 部署目录：`/home/weint/apps/nl2sql/langfuse`（docker-compose.yml + .env 所在）
> 版本：**Langfuse v4.21.0**（`LANGFUSE_MIGRATION_V4_WRITE_MODE=dual`）
> 记录时间：2026-08-27 | **全部组件已开放远程连接**（2026-08-27 更新）

---

## 1. Langfuse Web / API

| 项目 | 值 |
|------|-----|
| Web 控制台 | http://192.168.25.64:3010（0.0.0.0:3010，LAN 可达 ✅） |
| 健康检查 | http://192.168.25.64:3010/api/public/health |
| 管理员账号 | `admin@langfuse.local` / `Langfuse@GxzKAlruuC` |
| 组织 | AgentLab（org_mzZBzMZr） |
| 项目 | default（proj_6nMyvFmN） |
| Public Key (pk) | `pk-c64d8d357fa8e8b90e8aefb8183a2cea` |
| Secret Key (sk) | `sk-3791dd2504c88ca7502c987768ac35e907366db9260eec07` |
| SDK host | `http://192.168.25.64:3010` |

**SDK 用法（v4 API）**：
```python
from langfuse import Langfuse
langfuse = Langfuse(public_key="pk-c64d8d357fa8e8b90e8aefb8183a2cea",
                    secret_key="sk-3791dd2504c88ca7502c987768ac35e907366db9260eec07",
                    host="http://192.168.25.64:3010")
with langfuse.start_as_current_observation(name="chain", as_type="chain", input=..., output=...):
    with langfuse.start_as_current_observation(name="gen", as_type="generation", model="gpt-4o", input=..., output=...):
        pass
langfuse.flush()
```

---

## 2. PostgreSQL（Langfuse 元数据库，独立容器）— 远程已开放

| 项目 | 值 |
|------|-----|
| 容器 | `langfuse_postgres_1`（镜像 docker.m.daocloud.io/postgres:14，全新卷） |
| 数据库 / 用户 | `langfuse` / `langfuse` |
| 密码 | `Lf_Pg_TEj1SCQS` |
| 监听地址 | `0.0.0.0:5433`（LAN 可达 ✅，scram-sha-256 认证） |
| 远程连接串 | `postgresql://langfuse:Lf_Pg_TEj1SCQS@192.168.25.64:5433/langfuse` |
| 容器内连接串 | `postgresql://langfuse:Lf_Pg_TEj1SCQS@postgres:5432/langfuse` |
| 端口映射 | 0.0.0.0:5433 → 5432 |

---

## 3. ClickHouse（v4 事件/分析存储，独立容器）— 远程已开放

| 项目 | 值 |
|------|-----|
| 容器 | `langfuse_clickhouse_1`（clickhouse/clickhouse-server:25.12） |
| 用户 / 密码 | `clickhouse` / `71012e0d184400ef32f8ad23eab1b033` |
| 监听地址 | `0.0.0.0:18123`（HTTP，LAN 可达 ✅） |
| 远程 HTTP | `http://192.168.25.64:18123`（认证：`-u clickhouse:<密码>`） |
| **内置 Web 界面** | **http://192.168.25.64:18123/play**（浏览器直接登录 `clickhouse`，免驱动 ✅） |
| 原生 TCP | `0.0.0.0:19000`（clickhouse-client 用；JDBC 走原生需驱动属性 `transport=native`） |
| 容器内 HTTP / native | `http://clickhouse:8123` / `clickhouse://clickhouse:9000` |
| 关键表 | `events_core` / `events_full` / `traces` / `observations` / `scores` |

---

## 4. Redis（任务队列，独立容器）— 远程已开放

| 项目 | 值 |
|------|-----|
| 容器 | `langfuse_redis_1`（redis:7，requirepass） |
| 密码 | `6dbafb5d669ebaa9e22fe7ecf908561f` |
| 监听地址 | `0.0.0.0:6381`（LAN 可达 ✅；宿主机 6379 被其他服务占用，故映射 6381） |
| 远程连接 | `redis-cli -h 192.168.25.64 -p 6381 -a 6dbafb5d669ebaa9e22fe7ecf908561f` |
| 容器内地址 | `redis:6379` |

---

## 5. MinIO（S3 媒体存储，独立容器）— 远程已开放

| 项目 | 值 |
|------|-----|
| 容器 | `langfuse_minio_1` |
| S3 API | http://192.168.25.64:9090（0.0.0.0，LAN 可达 ✅） |
| **控制台登录地址** | **http://192.168.25.64:9091**（0.0.0.0，LAN 可达 ✅；**注意是 9091，不是 9001**） |
| **登录 Access Key** | `minio` |
| **登录 Secret Key** | `ljMop8MeoN2DUXZX` |
| Bucket | `langfuse`（`LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT=http://192.168.25.64:9090`） |

> ⚠️ **本服务器上有 4 个 MinIO 实例，控制台端口不同、凭据各自独立，勿混淆：**
>
> | 实例 | 控制台端口 | 归属 |
> |------|-----------|------|
> | `langfuse_minio_1` | **9091** ← 本实例，用上面的凭据 | Langfuse 媒体存储 |
> | `minio` | 9001 | 其他项目 |
> | `ragflow-minio` | 9011 | RAGFlow |
> | `weintmind_minio` | 9201 | weintmind |
>
> 若登录报 `Expected element type <AssumeRoleResponse> but have <ErrorResponse>`，多半是**登录了错误的端口（9001/9011/9201）或凭据输错**。

---

## 6. 其他密钥（服务器 `.env`，chmod 600）

| 变量 | 值 |
|------|-----|
| NEXTAUTH_SECRET | `e8184a093fa82641f9675631bc4e468658fb5c308cfe8c9c5f8add84ec29febb` |
| SALT | `eadbbeecf0cce7d85b03afa36b70c428786db889a156f73d790ee8b503be7a27` |
| ENCRYPTION_KEY | `f4636ee0da8faf22604fa1b8dab2ebd28a37b2dc23ede5458e3bb6777f45efaf` |
| LANGFUSE_MIGRATION_V4_WRITE_MODE | `dual` |
| LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN | `false` |
| LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL | `false` |

> ⚠️ 以上为敏感凭据，请妥善保管。完整配置以服务器 `/home/weint/apps/nl2sql/langfuse/.env` 为准。

---

## 7. 常用运维命令

```bash
cd /home/weint/apps/nl2sql/langfuse
docker-compose ps                                   # 状态
docker-compose logs -f langfuse-langfuse-web_1      # web 日志
docker-compose logs -f langfuse-langfuse-worker_1   # worker 日志
docker-compose restart                              # 重启全部
docker exec langfuse_clickhouse_1 clickhouse-client -q "SELECT type, count() FROM events_core GROUP BY type"
docker exec langfuse_postgres_1 psql -U langfuse -d langfuse -c "\dt"
```

## 8. 端口一览（全部 0.0.0.0，LAN 实测可达）

| 端口 | 服务 | 认证 |
|------|------|------|
| 3010 | Langfuse Web | 登录账号 |
| 5433 | PostgreSQL | 密码（scram） |
| 18123 | ClickHouse HTTP | 用户/密码 |
| 6381 | Redis | requirepass |
| 9090 | MinIO S3 API | Access/Secret |
| 9091 | MinIO 控制台 | 登录账号 |

> 跨网段/公网访问需在防火墙/安全组放行对应端口。安全提醒：这些服务已暴露到局域网，请确保密码强度并限制可访问网段。
