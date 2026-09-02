# Langfuse v4 部署说明（192.168.25.64）

> 部署目录：`/home/weint/apps/nl2sql/langfuse`（本目录）
> 部署时间：2026-08-27 | 版本：**Langfuse v4.21.0** | 模式：`WRITE_MODE=dual`（v3 读接口 + v4 事件双写）

## 访问入口

| 服务 | 地址 | 说明 |
|------|------|------|
| Langfuse Web | http://192.168.25.64:3010 | 管理后台（LAN 可达） |
| 健康检查 | http://192.168.25.64:3010/api/public/health | `{"status":"OK","version":"4.21.0"}` |
| MinIO S3 | http://192.168.25.64:9090 | 媒体上传（浏览器可达） |
| MinIO 控制台 | http://192.168.25.64:9091 | 已开放远程（LAN 可达） |
| PostgreSQL | 192.168.25.64:5433 | 已开放远程（scram 认证，密码见 .env） |
| ClickHouse HTTP | 192.168.25.64:18123 | 已开放远程（用户/密码，见 .env） |
| Redis | 192.168.25.64:6381 | 已开放远程（requirepass，见 .env；宿主 6379 被占故用 6381） |

## 账号

| 项目 | 值 |
|------|-----|
| 管理员 | `admin@langfuse.local` / `Langfuse@GxzKAlruuC`（见 .env LANGFUSE_INIT_USER_PASSWORD） |
| 组织 | AgentLab（org_mzZBzMZr） |
| 项目 | default（proj_6nMyvFmN） |
| 公钥 pk | `pk-c64d8d357fa8e8b90e8aefb8183a2cea` |
| 密钥 sk | `sk-3791dd2504c88ca7502c987768ac35e907366db9260eec07` |
| 数据库角色 | langfuse（容器 postgres，见 .env） |

## 架构（6 容器 + 5 卷，全部独立新建，未触碰服务器既有资源）

```
langfuse-web (3010) ─┬─ postgres:14   (容器, 127.0.0.1:5433)
langfuse-worker      ├─ clickhouse:25.12 (容器, 127.0.0.1:18123)
                     ├─ redis:7       (容器, 仅内网, 带密码)
                     └─ minio         (容器, 9090/9091)
```

- 镜像来源：postgres/redis/minio 复用服务器已有镜像；clickhouse/langfuse/worker 经 daocloud 镜像源拉取并打标准 tag
- `.env` 权限 600，内含全部密钥

## 常用运维命令

```bash
cd /home/weint/apps/nl2sql/langfuse
docker-compose ps                      # 状态
docker-compose logs -f langfuse-langfuse-web_1
docker-compose logs -f langfuse-langfuse-worker_1
docker-compose restart                 # 重启全部
docker-compose down && docker-compose up -d   # 重建（数据在 volume 中保留）
docker exec langfuse_clickhouse_1 clickhouse-client -q "SELECT type, count() FROM events_core GROUP BY type"
```

## 端到端验证结果（2026-08-27）

- 健康检查 OK，UI 200
- v4 SDK（langfuse 4.14.5，venv 于本目录）上报 chain+generation 成功：
  - ClickHouse `traces`/`observations` 可见
  - `events_core`/`events_full` 已写入类型化事件（CHAIN / GENERATION）
- SDK 用法（v4 API，注意与 v3 不同）：

```python
from langfuse import Langfuse
langfuse = Langfuse(public_key="pk-...", secret_key="sk-...", host="http://192.168.25.64:3010")
with langfuse.start_as_current_observation(name="chain-name", as_type="chain", input=..., output=...):
    with langfuse.start_as_current_observation(name="gen", as_type="generation", model="gpt-4o", input=..., output=...):
        pass
langfuse.flush()
```

## 备注

- 部署严格遵守约束：仅写本目录；未删除/修改服务器既有镜像、容器、卷、compose 项目
- `verify_v4.sh`、`sdk_e2e_new.sh`、`pull_images2.sh` 等脚本保留在本目录可复用
