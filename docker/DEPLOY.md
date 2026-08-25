# NL2SQL Docker 部署与发版手册

> 服务器：阿里云 ECS `8.163.4.42`（Ubuntu 22.04，3.4GB RAM / 2 核）
> 分支：`only_wrenai`
> 架构：nginx:80 → frontend:3000 + langgraph-api:2026 → 宿主机 PostgreSQL:5432

---

## 一、架构概览

```
8.163.4.42
├─ nginx:80                ← 反向代理（/ → frontend, /api → backend）
├─ langgraph-api:2026      ← 后端容器（LangGraph Server + custom_app.py + MCP）
├─ frontend:3000           ← 前端容器（Next.js）
└─ 宿主机 PostgreSQL:5432  ← 复用已有实例（nl2sql_checkpoint 库）
```

**不新建 postgres 容器** — 复用宿主机已有的 PostgreSQL 14.24 服务，省内存 + 避免端口冲突。

---

## 二、首次部署

### 2.1 服务器前提

- Docker 29+ & Compose V2 ✅（已安装）
- PostgreSQL 14+（宿主机 systemd 服务）✅（已安装）
- GitHub Deploy Key ✅（已配置）

### 2.2 准备宿主机 PostgreSQL

```bash
# 创建用户和数据库（仅首次）
sudo -u postgres psql -c "CREATE USER nl2sql WITH PASSWORD 'nl2sql_secret';"
sudo -u postgres psql -c "CREATE DATABASE nl2sql_checkpoint OWNER nl2sql;"

# 添加 pg_hba.conf 规则（允许 Docker 容器访问）
echo "host    all             nl2sql          172.17.0.0/16         scram-sha-256" | sudo tee -a /etc/postgresql/14/main/pg_hba.conf
sudo -u postgres psql -c "SELECT pg_reload_conf();"

# 验证
psql postgresql://nl2sql:nl2sql_secret@127.0.0.1:5432/nl2sql_checkpoint -c "SELECT 1;"
```

### 2.3 克隆代码

```bash
cd /opt
sudo mkdir -p nl2sql && sudo chown ecs-user:ecs-user nl2sql
git clone -b only_wrenai git@github.com:congpeiqiang/nl2sql_weint.git nl2sql
```

### 2.4 配置环境变量

```bash
cd /opt/nl2sql
vim .env.prod
```

**必须修改的项：**

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | DeepSeek API 密钥 |
| `DB_CONFIG_SECRET` | 数据库配置加密密钥 |
| `PG_PASSWORD`（docker-compose.yml）| PostgreSQL 密码（默认 `nl2sql_secret`）|

### 2.5 构建并启动后端

```bash
cd /opt/nl2sql
sudo docker compose build langgraph-api
sudo docker compose up -d langgraph-api

# 验证
sleep 30
curl http://localhost:2026/ok
sudo docker compose logs langgraph-api | tail -20
```

### 2.6 部署前端（可选）

```bash
# 克隆前端仓库
cd /opt
git clone <前端仓库地址> harness-deep-agents-ui

# 复制 Dockerfile 到前端仓库根目录
cp /opt/nl2sql/docker/frontend/Dockerfile /opt/harness-deep-agents-ui/Dockerfile

# ⚠ 前端有 DLP 加密文件，构建前需解密：
#   next.config.ts、src/lib/config.ts

# 构建并启动
cd /opt/nl2sql
sudo docker compose up -d frontend nginx
```

### 2.7 开放端口（阿里云安全组）

| 端口 | 用途 | 建议 |
|------|------|------|
| **80** | nginx 统一入口 | ✅ 必须放行 |
| 2026 | 后端 API 直连 | 可选 |
| 3000 | 前端直连 | 可选 |

### 2.8 验证清单

```bash
curl http://localhost:2026/ok          # 后端健康检查
curl http://localhost:2026/api/db-configs  # 自定义 API
curl http://localhost:80/ok            # nginx → 后端
curl http://localhost:3000             # 前端页面
sudo docker compose ps                 # 全部容器状态
sudo docker stats --no-stream          # 资源占用
```

---

## 三、日常发版（更新代码后重新部署）

### 3.1 后端发版

```bash
# 1. 本地提交推送
git add .
git commit -m "feat: xxx"
git push origin only_wrenai

# 2. SSH 到服务器
ssh ecs-user@8.163.4.42

# 3. 拉取 + 重建 + 重启（一条命令搞定）
cd /opt/nl2sql
git pull origin only_wrenai
sudo docker compose up -d --build langgraph-api
```

> `--build` 会利用 Docker 缓存，只重建有改动的层。
> 如果只改了 Python 源码（没改 Dockerfile / pyproject.toml），构建通常 1-2 分钟。
> 如果改了 pyproject.toml（依赖变动），构建需 5-10 分钟。

### 3.2 前端发版

```bash
cd /opt/harness-deep-agents-ui
git pull

# 如果 Dockerfile 被覆盖，重新复制
cp /opt/nl2sql/docker/frontend/Dockerfile ./Dockerfile

cd /opt/nl2sql
sudo docker compose up -d --build frontend
```

### 3.3 仅重启（不重建镜像）

适用于：修改了 `.env.prod`、`docker-compose.yml`、`docker/nginx.conf` 等配置文件。

```bash
cd /opt/nl2sql
sudo docker compose restart langgraph-api   # 重启后端
sudo docker compose restart nginx            # 重启 nginx
# 或全部重启
sudo docker compose restart
```

### 3.4 修改环境变量

```bash
cd /opt/nl2sql
vim .env.prod
# 重启生效（不需要重建镜像）
sudo docker compose up -d langgraph-api
```

> `docker compose up -d` 会检测配置变化并重启容器。

---

## 四、回滚

```bash
cd /opt/nl2sql

# 查看提交历史
git log --oneline -10

# 回滚到指定版本
git checkout <commit-hash>

# 重建并启动
sudo docker compose up -d --build langgraph-api
```

---

## 五、监控与排障

### 5.1 日志

```bash
# 实时查看后端日志
sudo docker compose logs -f langgraph-api

# 查看最近 100 行
sudo docker compose logs --tail=100 langgraph-api

# nginx 日志
sudo docker compose logs -f nginx

# 宿主机 PostgreSQL 日志
sudo journalctl -u postgresql -f
```

### 5.2 资源监控

```bash
# 容器资源占用
sudo docker stats --no-stream

# 磁盘空间
df -h /

# 内存
free -h
```

### 5.3 数据库

```bash
# 连接 checkpoint 数据库
psql postgresql://nl2sql:nl2sql_secret@127.0.0.1:5432/nl2sql_checkpoint

# 查看表
\dt

# 查看 checkpoint 数量
SELECT count(*) FROM checkpoints;
```

### 5.4 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 构建卡住（apt 下载慢） | Debian 源国内慢 | Dockerfile 已配阿里云镜像 |
| 构建卡住（torch 下载） | wrenai[memory] 拖入 PyTorch | Dockerfile 已去掉 memory extra |
| 容器 OOM Killed | 内存不足（3.4GB 总量） | 升配服务器 或 减少 Langfuse 内存 |
| `/ok` 返回 502 | 后端未启动完成 | `docker compose logs` 查看错误 |
| checkpoint 连不上 PG | pg_hba.conf 规则缺失 | 添加 `172.17.0.0/16` 规则 |
| nginx 504 | 后端处理超时 | 检查 `proxy_read_timeout` |

---

## 六、文件清单

| 文件 | 位置 | 说明 |
|------|------|------|
| `Dockerfile` | 项目根 | 后端多阶段构建 |
| `.dockerignore` | 项目根 | 排除 .venv/.env/.git 等 |
| `docker-compose.yml` | 项目根 | 服务编排（后端+前端+nginx） |
| `.env.prod` | 项目根 | 生产环境变量 |
| `docker/nginx.conf` | docker/ | 反向代理 + SSE 支持 |
| `docker/frontend/Dockerfile` | docker/frontend/ | 前端多阶段构建模板 |
| `docker/DEPLOY.md` | docker/ | 本文档 |

---

## 七、关键配置说明

### Dockerfile 优化（国内服务器）

- **apt 源**：`mirrors.aliyun.com`（阿里云 Debian 镜像）
- **pip 源**：`mirrors.aliyun.com/pypi/simple/`（阿里云 PyPI）
- **Node.js**：`npmmirror.com`（淘宝 npm 镜像）
- **wrenai**：去掉 `[memory]` extra（省 1.3GB PyTorch+CUDA，grep 模式不需要）

### docker-compose.yml 内存限制

```yaml
langgraph-api: 1200m    # 后端
frontend:       256m    # 前端
nginx:          64m     # 反代
```

### checkpointer 双模式

```python
# 有 CHECKPOINT_DB_URI → PostgreSQL（生产）
# 无 → SQLite（本地开发）
```

### start_server.py setdefault

```python
# 不覆盖 Docker 传入的环境变量（DATABASE_URI 等）
for k, v in env_updates.items():
    os.environ.setdefault(k, v)
```
