# NL2SQL Docker 部署与发版手册

> 服务器：阿里云 ECS `8.163.4.42`（Ubuntu 22.04，3.4GB RAM / 2 核）
> 分支：`only_wrenai`

---

## 一、架构概览

### 方案 A：前端本地 + 服务器后端（推荐 ⭐）

服务器只跑后端，前端在笔记本本地启动，**省 ~320MB 服务器内存**。

```
笔记本（本地）                       服务器 8.163.4.42
├─ frontend:3000  ──── HTTP ────→  ├─ langgraph-api:2026
└─ 浏览器                           └─ 宿主机 PostgreSQL:5432
```

**优点：** 服务器省内存（无 frontend 256MB + nginx 64MB）；前端开发热更新方便；无需处理 DLP 加密。

### 方案 B：全部部署在服务器

```
服务器 8.163.4.42
├─ nginx:80                ← 反向代理（/ → frontend, /api → backend）
├─ langgraph-api:2026      ← 后端容器（LangGraph Server + custom_app.py + MCP）
├─ frontend:3000           ← 前端容器（Next.js）
└─ 宿主机 PostgreSQL:5432  ← 复用已有实例（nl2sql_checkpoint 库）
```

**适用：** 生产环境对外服务、演示环境。需要额外 ~320MB 内存。

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

### 2.5 构建并启动后端（两种方案通用）

```bash
cd /opt/nl2sql
sudo docker compose build langgraph-api
sudo docker compose up -d langgraph-api

# 验证
sleep 30
curl http://localhost:2026/ok
sudo docker compose logs langgraph-api | tail -20
```

### 2.6 开放端口（阿里云安全组）

#### 方案 A（前端本地）

| 端口 | 用途 | 建议 |
|------|------|------|
| **2026** | 后端 API（本地前端直连） | ✅ **必须放行** |

#### 方案 B（全部服务器）

| 端口 | 用途 | 建议 |
|------|------|------|
| **80** | nginx 统一入口 | ✅ 必须放行 |
| 2026 | 后端 API 直连 | 可选 |
| 3000 | 前端直连 | 可选 |

---

## 三、前端部署

### 3.1 方案 A：前端在笔记本本地启动（推荐）

服务器 **不需要** 启动 frontend 和 nginx 容器，节省 ~320MB 内存。

**笔记本操作：**

```bash
# 1. 进入前端仓库
cd D:\code_work_space\llm\harness-deep-agents-ui

# 2. 配置 API 地址指向服务器
#    在前端 .env 或 .env.local 中设置：
#    NEXT_PUBLIC_API_URL=http://8.163.4.42:2026
#    （具体变量名以前端项目文档为准）

# 3. 安装依赖（首次）
yarn install

# 4. 启动开发模式（支持热更新）
yarn dev
# 或生产模式
yarn build && yarn start
```

**访问：** `http://localhost:3000`

> **注意：** 笔记本需能访问 `8.163.4.42:2026`（安全组已放行 2026 端口）。

### 3.2 方案 B：前端部署在服务器

```bash
# 1. 克隆前端仓库到服务器
cd /opt
git clone <前端仓库地址> harness-deep-agents-ui

# 2. 复制 Dockerfile 到前端仓库根目录
cp /opt/nl2sql/docker/frontend/Dockerfile /opt/harness-deep-agents-ui/Dockerfile

# ⚠ 前端有 DLP 加密文件，构建前需解密：
#   next.config.ts、src/lib/config.ts

# 3. 构建并启动前端 + nginx
cd /opt/nl2sql
sudo docker compose up -d frontend nginx

# 4. 验证
curl http://localhost:3000          # 前端
curl http://localhost:80            # nginx 入口
```

---

## 四、日常发版

### 4.1 后端发版

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

### 4.2 前端发版

#### 方案 A（本地前端）

```bash
# 笔记本操作
cd D:\code_work_space\llm\harness-deep-agents-ui
git pull
# 开发模式会自动热更新；生产模式需重新 build
yarn build && yarn start
```

#### 方案 B（服务器前端）

```bash
# 服务器操作
cd /opt/harness-deep-agents-ui
git pull

# 如果 Dockerfile 被覆盖，重新复制
cp /opt/nl2sql/docker/frontend/Dockerfile ./Dockerfile

cd /opt/nl2sql
sudo docker compose up -d --build frontend
```

### 4.3 仅重启（不重建镜像）

适用于：修改了 `.env.prod`、`docker-compose.yml` 等配置文件。

```bash
cd /opt/nl2sql
sudo docker compose restart langgraph-api   # 重启后端
# 方案 B 还可以重启 nginx
sudo docker compose restart nginx
# 或全部重启
sudo docker compose restart
```

### 4.4 修改环境变量

```bash
cd /opt/nl2sql
vim .env.prod
# 重启生效（不需要重建镜像）
sudo docker compose up -d langgraph-api
```

> `docker compose up -d` 会检测配置变化并重启容器。

---

### 4.5 每日 BadCase 采集调度（宿主机 cron + docker exec）

容器内不跑 cron；差评/低分 → `Dataset:badcase` 的采集由宿主机每日触发。
> 完整部署说明见 `docs/weint环境/NL2SQL-部署与更新手册.md`（生产环境手册，
> 含服务器路径 /home/weint/apps/nl2sql/nl2sql-app/ 与容器名 nl2sql-app_langgraph-api_1）。

```bash
# 宿主机 crontab -e（root 或 docker 组用户；日志目录需存在 mkdir -p /home/weint/apps/nl2sql/logs）
13 2 * * * /home/weint/apps/nl2sql/nl2sql-app/backend/scripts/daily_collect_badcase.sh >> /home/weint/apps/nl2sql/logs/nl2sql_collect_badcase.log 2>&1
```

脚本用 `docker exec nl2sql-app_langgraph-api_1` 在容器内依次跑 `collect_badcase`、
`feedback_gate`、`badcase_status summary`。容器 `env_file: .env.prod` 已注入生产
LANGFUSE_* 与 `AGENT_DATA_ROOT=/app/data`，无需宿主机重复配置。

验证调度是否生效（容器 venv 是 `uv sync --no-install-project` 装的、无 `_nl2sql_src.pth`，
必须 `PYTHONPATH=/app/src` + venv python 才能 import `agent`）：
```bash
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.eval.collect_badcase --days 1'
```

---

## 五、回滚

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

## 六、监控与排障

### 6.1 日志

```bash
# 实时查看后端日志
sudo docker compose logs -f langgraph-api

# 查看最近 100 行
sudo docker compose logs --tail=100 langgraph-api

# nginx 日志（方案 B）
sudo docker compose logs -f nginx

# 宿主机 PostgreSQL 日志
sudo journalctl -u postgresql -f
```

### 6.2 资源监控

```bash
# 容器资源占用
sudo docker stats --no-stream

# 磁盘空间
df -h /

# 内存
free -h
```

### 6.3 数据库

```bash
# 连接 checkpoint 数据库
psql postgresql://nl2sql:nl2sql_secret@127.0.0.1:5432/nl2sql_checkpoint

# 查看表
\dt

# 查看 checkpoint 数量
SELECT count(*) FROM checkpoints;
```

### 6.4 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 构建卡住（apt 下载慢） | Debian 源国内慢 | Dockerfile 已配阿里云镜像 |
| 构建卡住（torch 下载） | wrenai[memory] 拖入 PyTorch | Dockerfile 已去掉 memory extra |
| 容器 OOM Killed | 内存不足（3.4GB 总量） | 改用方案 A（前端本地）或升配服务器 |
| `/ok` 返回 502 | 后端未启动完成 | `docker compose logs` 查看错误 |
| checkpoint 连不上 PG | pg_hba.conf 规则缺失 | 添加 `172.17.0.0/16` 规则 |
| 本地前端 CORS 错误 | 后端未配跨域 | 后端 `ALLOW_PRIVATE_NETWORK=true` 已设置 |
| 本地前端连不上后端 | 安全组未放行 2026 | 阿里云安全组放行 TCP 2026 |
| nginx 504（方案 B） | 后端处理超时 | 检查 `proxy_read_timeout` |
| 容器内 `ls` 中文文件名显示 `$'\345\277\220'` octal 转义 | 容器 shell 无 UTF-8 locale（`LANG/LC_ALL` 未生效——Dockerfile `ENV` 或 compose `environment` 缺失，或容器未重建） | Dockerfile 已 `ENV LANG/LC_ALL=C.UTF-8`（2026-08-31）+ compose `environment` 同配；重新 `docker compose up -d --build langgraph-api` 重建容器。**注意：仅改 compose `environment` 需重建容器才生效（`docker compose restart` 不重新读 env）**。验证：`docker exec <容器> bash -c 'locale | grep LANG; ls /app/data/workspace/'` |

---

## 七、文件清单

| 文件 | 位置 | 说明 |
|------|------|------|
| `Dockerfile` | 项目根 | 后端多阶段构建 |
| `.dockerignore` | 项目根 | 排除 .venv/.env/.git 等 |
| `docker-compose.yml` | 项目根 | 服务编排（后端+前端+nginx） |
| `.env.prod` | 项目根 | 生产环境变量 |
| `docker/nginx.conf` | docker/ | 反向代理 + SSE 支持（方案 B 使用） |
| `docker/frontend/Dockerfile` | docker/frontend/ | 前端多阶段构建模板（方案 B 使用） |
| `docker/DEPLOY.md` | docker/ | 本文档 |

---

## 八、关键配置说明

### Dockerfile 优化（国内服务器）

- **apt 源**：`mirrors.aliyun.com`（阿里云 Debian 镜像）
- **pip 源**：`mirrors.aliyun.com/pypi/simple/`（阿里云 PyPI）
- **Node.js**：`npmmirror.com`（淘宝 npm 镜像）
- **wrenai**：去掉 `[memory]` extra（省 1.3GB PyTorch+CUDA，grep 模式不需要）

### docker-compose.yml 内存限制

```yaml
langgraph-api: 1200m    # 后端（两种方案都需要）
frontend:       256m    # 前端（仅方案 B）
nginx:          64m     # 反代（仅方案 B）
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

### 方案选择参考

| | 方案 A（前端本地） | 方案 B（全部服务器） |
|---|---|---|
| **服务器内存占用** | ~1.2GB（仅后端） | ~1.5GB（后端+前端+nginx） |
| **适合场景** | 开发/测试/个人使用 | 生产/演示/多人访问 |
| **前端热更新** | ✅ yarn dev 自动 | ❌ 需重建容器 |
| **DLP 加密问题** | 无（本地解密） | 需在服务器解密 |
| **外部可访问** | ❌ 仅本地 | ✅ 任意浏览器 |
