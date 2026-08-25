# NL2SQL Docker 部署指南

## 服务器前提

- Docker 29+ & Compose V2
- PostgreSQL 14+（宿主机 systemd 服务）
- 内存 ≥ 3.4GB（后端 1.2GB + 前端 256MB + nginx 64MB + 系统预留）

## 一、准备宿主机 PostgreSQL

```bash
sudo -u postgres psql -c "CREATE USER nl2sql WITH PASSWORD 'nl2sql_secret';"
sudo -u postgres psql -c "CREATE DATABASE nl2sql_checkpoint OWNER nl2sql;"
psql postgresql://nl2sql:nl2sql_secret@127.0.0.1:5432/nl2sql_checkpoint -c "SELECT 1;"
```

## 二、上传代码

```bash
cd /opt
git clone <nl2sql-repo-url> nl2sql
git clone <frontend-repo-url> harness-deep-agents-ui
```

## 三、前端 Dockerfile

将 `nl2sql/docker/frontend/Dockerfile` 复制到前端仓库根目录：

```bash
cp /opt/nl2sql/docker/frontend/Dockerfile /opt/harness-deep-agents-ui/Dockerfile
```

## 四、配置环境变量

```bash
cd /opt/nl2sql
vim .env.prod
# 修改 LLM_API_KEY、DB_CONFIG_SECRET 等实际密钥
```

## 五、启动

```bash
cd /opt/nl2sql
docker compose up -d --build
```

首次构建约 3-5 分钟。

## 六、验证

```bash
curl http://localhost:2026/ok        # 后端健康检查
curl http://localhost:3000           # 前端页面
curl http://localhost/ok             # nginx 反代
docker compose ps                    # 容器状态
docker compose logs -f langgraph-api # 查看后端日志
docker stats --no-stream             # 资源占用
```

## 七、开放端口（阿里云安全组）

| 端口 | 用途 |
|------|------|
| 80 | nginx 统一入口（推荐） |
| 2026 | 后端直连（可选） |
| 3000 | 前端直连（可选） |

## 运维

```bash
# 重启后端
docker compose restart langgraph-api

# 更新代码
cd /opt/nl2sql && git pull
cd /opt/harness-deep-agents-ui && git pull
docker compose up -d --build

# 查看资源
docker stats --no-stream
```
