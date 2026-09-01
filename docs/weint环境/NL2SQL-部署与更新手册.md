# NL2SQL 部署与更新手册（weint 环境 192.168.25.64）

> 适用对象：NL2SQL 应用（后端 LangGraph API + 前端 Next.js）容器化部署与日常更新
> 部署目录（服务器）：`/home/weint/apps/nl2sql/nl2sql-app/`
> 环境：Ubuntu 24.04 · Docker 29 · docker-compose **v1.29**（注意 v1 限制）
> 更新时间：2026-08-28（含前后端打包上传完整流程）

---

## 〇、架构速览

```
浏览器 → nginx:8080 ── /api/*,/threads,/runs,/ok → langgraph-api:2026（后端）
                └── 其余 → frontend:3000（前端，宿主不发布端口）
后端另发布宿主 2026（直连/健康检查）
postgres（nl2sql_checkpoint）127.0.0.1:5435
```

| 容器 | 镜像 | 端口 | 数据卷 |
|------|------|------|--------|
| nl2sql-app_nginx_1 | nginx:alpine | 8080→80 | docker/nginx.conf（只读挂载） |
| nl2sql-app_frontend_1 | node:20-alpine（产物化） | 内部 3000 | 无 |
| nl2sql-app_langgraph-api_1 | python:3.13-slim | 2026 | workspace（db_config.json/语义库） |
| nl2sql-app_postgres_1 | postgres:14（复用已有镜像） | 127.0.0.1:5435 | nl2sql_pg_data |

> **docker-compose v1 铁律**：容器已有同名时 `up -d` 会报 `KeyError: 'ContainerConfig'`（v1 与新版 Docker API 不兼容）。**凡涉及重建/改配置，一律三步**：`docker-compose stop <svc>` → `docker-compose rm -f <svc>` → `docker-compose up -d <svc>`。

---

## 一、首次部署（从零）

### 1. 前置条件
- 服务器已装 Docker + docker-compose（v1 即可）
- 基础镜像已备（或用 daocloud 拉取并打标准 tag）：
  ```bash
  docker pull docker.m.daocloud.io/python:3.13-slim && docker tag docker.m.daocloud.io/python:3.13-slim python:3.13-slim
  docker pull docker.m.daocloud.io/node:20-alpine
  docker pull docker.m.daocloud.io/nginx:alpine
  # postgres:14 已在本机（docker.m.daocloud.io/postgres:14）
  ```
  > docker.io 直连在此网络不通（DNS 只回 IPv6），一律走 daocloud 镜像。

### 2. 打包并上传代码/产物（本机 PowerShell）

**后端**（本机 `D:\code_work_space\llm\nl2sql`）：
```powershell
# ① 打包（排除 .venv/.git/logs/docs/探针文件/环境变量/工作区）
tar -cf D:\code_work_space\llm\deepseek-workspace\backend_update.tar `
  --exclude=.venv --exclude=.git --exclude=logs --exclude=.langgraph_api `
  --exclude=.idea --exclude=docs --exclude=.tmp --exclude=__pycache__ `
  --exclude="*.bin" --exclude="*.log" `
  --exclude=.env --exclude=.env.prod --exclude=src/agent/workspace `
  -C D:\code_work_space\llm\nl2sql .

# ② 上传
scp D:\code_work_space\llm\deepseek-workspace\backend_update.tar weint@192.168.25.64:/home/weint/apps/nl2sql/nl2sql-app/
```
> ⚠️ 必须排除 `.env`/`.env.prod`（本地是占位符/开发配置，服务器的生产版不能被覆盖）。
> ✅ 本地仓库已内置全部补丁（mcp_tool.py env 继承 + Dockerfile mcp-echarts），上传后**无需在服务器重打**。

**前端**（本机 `D:\code_work_space\llm\huice\008\harness-deep-agents-ui`）：
```powershell
# ① 本地生产构建（DLP 解密必须在本地环境；注意是 build 不是 dev）
cd D:\code_work_space\llm\huice\008\harness-deep-agents-ui
yarn build

# ② 打包构建产物（不含 node_modules；next.config.ts 本地常为 DLP 加密，单独处理）
tar -cf D:\code_work_space\llm\deepseek-workspace\frontend_artifacts.tar `
  --exclude=.next/cache -C D:\code_work_space\llm\huice\008\harness-deep-agents-ui `
  .next public package.json yarn.lock

# ③ 上传产物 + 明文 next.config.ts（用 nl2sql-app/frontend/next.config.ts 的明文最小版，
#    切勿用本地被 DLP 加密的文件，否则 next start 崩溃）
scp D:\code_work_space\llm\deepseek-workspace\frontend_artifacts.tar weint@192.168.25.64:/home/weint/apps/nl2sql/nl2sql-app/frontend/
scp D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\next.config.ts weint@192.168.25.64:/home/weint/apps/nl2sql/nl2sql-app/frontend/next.config.ts
```

**服务器解压**（后端和前端 tar 上传后）：
```bash
cd /home/weint/apps/nl2sql/nl2sql-app
tar -xf backend_update.tar -C backend && rm backend_update.tar
cd frontend && tar -xf frontend_artifacts.tar && rm frontend_artifacts.tar && cd ..
# 校验：cat .next/BUILD_ID 应为新构建 ID；head -3 next.config.ts 应为明文
```

**配置**：`.env.prod`（服务器版，含真实 DeepSeek key + langfuse 指向 192.168.25.64:3010）+ `docker-compose.yml` + `docker/nginx.conf` + `frontend/Dockerfile`（产物化运行时镜像）一并上传。

### 3. 构建
```bash
cd /home/weint/apps/nl2sql/nl2sql-app
docker-compose config --quiet          # 校验
docker-compose build langgraph-api     # 后端（uv sync，5-15 分钟）
docker-compose build frontend          # 前端（yarn install + 拷贝 .next）
```

### 4. 启动（三步法）
```bash
docker-compose up -d postgres nginx
docker-compose up -d langgraph-api frontend   # 若报 ContainerConfig → stop/rm -f 后 up
```

### 5. checkpoint 表（代码已自动建表，无需手动初始化）

`checkpointer_factory.py` 的 PostgreSQL 模式已在**首次使用时自动调用 `setup()` 建表**（checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations）——新部署空库开箱即用，不再需要手工步骤。

校验（可选）：
```bash
docker exec nl2sql-app_postgres_1 psql -U nl2sql -d nl2sql_checkpoint -c '\dt'
```

> 仅当使用**旧镜像**（未含自动建表修复）部署时，才需要手工执行一次：
> ```bash
> docker run --rm --network nl2sql-app_default nl2sql-app_langgraph-api:latest python -c "
> import asyncio
> from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
> URI = 'postgresql://nl2sql:nl2sql_secret@postgres:5432/nl2sql_checkpoint'
> async def main():
>     cm = AsyncPostgresSaver.from_conn_string(URI)
>     async with cm as saver:
>         await saver.setup()
>         print('SETUP_OK')
> asyncio.run(main())
> "
> ```

> ⚠️ **密码提醒**：docker-compose 的 `${PG_PASSWORD}` 只从 compose 目录的 `.env` 文件插值（**不是 `.env.prod`**）。本环境无 `.env` → 实际密码为 `nl2sql_secret`（已在卷初始化时定死）；`.env.prod` 的 `PG_PASSWORD` 已改为同值保持一致。

### 6. 初始化业务库配置（db_config.json）
后端就绪门控要求至少 1 个库配置。两种方式：
- **推荐**：前端 UI → 数据库配置（DB_CONFIG_SECRET 加密，持久化到 workspace 卷）
- 或一次性迁移：`docker run --rm --env-file .env.prod -e PYTHONPATH=/app/src -v nl2sql-app_workspace:/app/src/agent/workspace nl2sql-app_langgraph-api:latest python -c "from mcp_server.db_mcp_server.db.core.db_config_store import get_store; print(get_store().migrate_from_env())"`

### 7. 验证
```bash
curl http://127.0.0.1:2026/ok                          # {"ok":true}
curl http://127.0.0.1:8080/                            # 前端 200
docker logs nl2sql-app_langgraph-api_1 2>&1 | grep -E "MCP 工具加载完成|预检通过"
# 期望：✅ 子智能体 MCP 工具加载完成: N 个工具；✅ 预检通过
```

---

## 二、日常更新

### 0. 一键发布脚本（推荐，替代 1/2 的手动步骤）

脚本位置（本机，两处内容一致）：
```
主路径（项目内，推荐）：D:\code_work_space\llm\nl2sql\scripts\
├── release-backend.ps1     # 后端：打包→上传→重建→三步重启→验证
└── release-frontend.ps1    # 前端：yarn build→打包产物→上传→重建→重启→验证
副本（运维文档目录）：D:\code_work_space\llm\nl2sql\docs\weint环境\发布脚本\
```

用法（本机 PowerShell，**已配置 SSH 密钥免密，无需输密码**）：
```powershell
# 后端发布
powershell -ExecutionPolicy Bypass -File "D:\code_work_space\llm\nl2sql\scripts\release-backend.ps1"

# 前端发布（DLP 致 build 失败时：手动 yarn build 后加 -SkipBuild）下面两条命令2选1
 # 情况 1：直接跑（脚本自己 build）
powershell -ExecutionPolicy Bypass -File "D:\code_work_space\llm\nl2sql\scripts\release-frontend.ps1"
 # 情况 2：build 失败/已手动 build 过 → 跳过 build
cd D:\code_work_space\llm\huice\008\harness-deep-agents-ui

yarn build
powershell -ExecutionPolicy Bypass -File "D:\code_work_space\llm\nl2sql\scripts\release-frontend.ps1" -SkipBuild
```

验证输出：后端 `backend_ok:200` + 预检通过；前端 `ui_8080:200`。

> 与下方手动流程完全等价；日常更新建议直接用脚本，手动命令保留用于排障/自定义场景。

### 1. 后端代码更新（改 Python）

**① 本机打包上传**（PowerShell）：
```powershell
tar -cf D:\code_work_space\llm\deepseek-workspace\backend_update.tar `
  --exclude=.venv --exclude=.git --exclude=logs --exclude=.langgraph_api `
  --exclude=.idea --exclude=docs --exclude=.tmp --exclude=__pycache__ `
  --exclude="*.bin" --exclude="*.log" `
  --exclude=.env --exclude=.env.prod --exclude=src/agent/workspace `
  -C D:\code_work_space\llm\nl2sql .
  
scp D:\code_work_space\llm\deepseek-workspace\backend_update.tar weint@192.168.25.64:/home/weint/apps/nl2sql/nl2sql-app/
```

**② 服务器解压 + 重建 + 三步重启 + 验证**（bash）：
```bash
cd /home/weint/apps/nl2sql/nl2sql-app
tar -xf backend_update.tar -C backend && rm backend_update.tar
docker-compose build langgraph-api
docker-compose stop langgraph-api && docker-compose rm -f langgraph-api
docker-compose up -d langgraph-api
sleep 60 && curl http://127.0.0.1:2026/ok
docker logs nl2sql-app_langgraph-api_1 2>&1 | grep -E "MCP 工具加载完成|预检通过|error"
# 期望：✅ MCP [mcp-server-echarts]: 正常 / ✅ [sub] dbmcp: N tools loaded / ✅ 预检通过
```
> 依赖没变时构建约 1-2 分钟（Docker 层缓存）；改了 pyproject/uv.lock 则 5-15 分钟。
> ✅ 本地仓库已含全部补丁（env 继承 + Dockerfile mcp-echarts），无需服务器重打。

### 2. 前端更新（改 UI）

**① 本机构建 + 打包上传**（PowerShell）：
```powershell
cd D:\code_work_space\llm\huice\008\harness-deep-agents-ui
yarn build                                   # 生产构建（DLP 解密在本地）
tar -cf D:\code_work_space\llm\deepseek-workspace\frontend_artifacts.tar `
  --exclude=.next/cache -C D:\code_work_space\llm\huice\008\harness-deep-agents-ui `
  .next public package.json yarn.lock
scp D:\code_work_space\llm\deepseek-workspace\frontend_artifacts.tar weint@192.168.25.64:/home/weint/apps/nl2sql/nl2sql-app/frontend/
scp D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\next.config.ts weint@192.168.25.64:/home/weint/apps/nl2sql/nl2sql-app/frontend/next.config.ts
```

**② 服务器解压 + 重建 + 三步重启 + 验证**（bash）：
```bash
cd /home/weint/apps/nl2sql/nl2sql-app/frontend
tar -xf frontend_artifacts.tar && rm frontend_artifacts.tar
cat .next/BUILD_ID          # 确认是新构建 ID
cd .. && docker-compose build frontend
docker-compose stop frontend && docker-compose rm -f frontend && docker-compose up -d frontend
curl http://127.0.0.1:8080/   # 200 即更新完成
```

### 3. 环境变量更新（.env.prod）
```bash
cd /home/weint/apps/nl2sql/nl2sql-app
vi .env.prod   # 改配置
docker-compose stop langgraph-api && docker-compose rm -f langgraph-api && docker-compose up -d langgraph-api
# 无需重建镜像；前端若用到 NEXT_PUBLIC_* 则需重新构建前端
```
> **AGENT_DATA_ROOT（外部数据根）**：容器内固定 `/app/data`，须与 `docker-compose.yml` 的 `agent_data:/app/data` 挂载一致。
> 首启自动从镜像内 `src/agent/{shared,workspace}` 拷贝种子到 `/app/data`（仅目录缺失时）。
> 存量升级的旧卷迁移见「四、数据与持久化」。

### 4. 业务数据库配置更新
前端 UI → 数据库配置（增删改库）→ 保存后 **重启后端** 生效（MCP 工具按 db_config 构建）：
```bash
cd /home/weint/apps/nl2sql/nl2sql-app
docker-compose restart langgraph-api
```

### 5. 仅重启（不重建镜像）
```bash
docker-compose restart langgraph-api   # 或 frontend / nginx
```

### 6. 每日 BadCase 采集调度（宿主机 cron + docker exec）

**背景**：差评/低分 → `Dataset:badcase` 的采集（`collect_badcase` + `feedback_gate`
门禁 + `badcase_status` 汇总）由生产后端每天自动执行。容器内不跑 cron，由**宿主机**
cron 每日 `docker exec` 进容器触发——容器 `env_file: .env.prod` 已注入生产
LANGFUSE_*（指向 192.168.25.64:3010）与 `AGENT_DATA_ROOT=/app/data`，无需在宿主机
重复配置密钥。

**脚本**：`scripts/daily_collect_badcase.sh`（随后端发布包传到 `backend/scripts/`），
内部依次跑三步——**采集 → 门禁 → 状态汇总**（数据源是 Langfuse API，不是日志）：
```bash
# ① 采集 BadCase → Langfuse Dataset:badcase
#    扫近 1 天 trace，筛 user-feedback=0 / 五维分<0.6 / ERROR / sql_exec_success=0
#    → create_dataset_item 写入 Dataset:badcase（本地 stamp 去重，同 trace 不重复采）
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.eval.collect_badcase --days 1'

# ② 真实反馈门禁（M6）：聚合近 7 天好评率，按 prompt_label 分组对比，
#    candidate 好评率低于 reference−阈值 → exit 1（触发回滚/不放量）；只判断不写数据
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.eval.feedback_gate --days 7'

# ③ 状态汇总（P0）：输出 badcase_status.json 各状态计数，提示人工复审待处理数量
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.eval.badcase_status summary'
```

**注册**（宿主机，只需一次）：
```bash
# 每日 02:13（采集后紧跟门禁与状态汇总）
crontab -e
13 2 * * * /home/weint/apps/nl2sql/nl2sql-app/backend/scripts/daily_collect_badcase.sh >> /home/weint/apps/nl2sql/logs/nl2sql_collect_badcase.log 2>&1
```

**手动触发/验证**：
```bash
# 注意：容器 venv 是 uv sync --no-install-project 装的，无 _nl2sql_src.pth，
#       必须 PYTHONPATH=/app/src + venv python 才能 import agent 模块
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.eval.collect_badcase --days 1'
# 期望日志：扫描近 1 天用户 AGENT roots: N 条 ... 完成：本次新采集 N 条 BadCase
# 校验数据集：curl -u pk:sk "http://192.168.25.64:3010/api/public/dataset-items?datasetName=badcase"
```

#### 启停操作

| 操作 | 命令 | 说明 |
|---|---|---|
| **启动（注册 cron）** | `crontab -e` → 加 `13 2 * * * /home/weint/apps/nl2sql/nl2sql-app/backend/scripts/daily_collect_badcase.sh >> /home/weint/apps/nl2sql/logs/nl2sql_collect_badcase.log 2>&1` | 只需执行一次；保存后立即生效，无需重启服务 |
| **确认已注册** | `crontab -l \| grep daily_collect` | 能看到该行即注册成功 |
| **立即手动跑一次** | `bash /home/weint/apps/nl2sql/nl2sql-app/backend/scripts/daily_collect_badcase.sh` | 不等 02:13，验证脚本/容器/网络是否 OK（等价于手动触发） |
| **查最近运行日志** | `tail -30 /home/weint/apps/nl2sql/logs/nl2sql_collect_badcase.log` | 看每次运行的 `collect_badcase exit=` / `feedback_gate exit=` 等 |
| **暂停（临时停）** | `crontab -e` → 行首加 `#` 注释掉 | 不再自动执行；脚本文件保留，随时可取消注释恢复 |
| **恢复** | `crontab -e` → 去掉 `#` 注释 | 从下一个 02:13 起恢复每日采集 |
| **停止（彻底移除）** | `crontab -e` → 删除该行 | 永久停止自动采集；需重新手动注册才能恢复 |

> **停不停采数据？** 停止 cron 只停「自动采集」这一步——用户反馈/五维分仍照常写入
> Langfuse（那是运行时埋点，与 cron 无关）。停采只是 Dataset:badcase 不再自动新增，
> 后续可手动跑 `collect_badcase --days N` 补采，数据不丢。

> **开发机兜底**：Windows 下 `scripts/daily_collect_badcase.ps1`（任务计划
> `nl2sql-collect-badcase`，02:13）仍可用；采集脚本经 `agent.settings.env_loader`
> 叠加 `.env.prod` 的 LANGFUSE_*（生产项目），本机跑同样连生产 Langfuse，但 stamp
> 落开发工作区——生产采集以容器内为准，开发机仅兜底。

---

## 三、回滚

| 场景 | 方法 |
|------|------|
| 后端 | 用 git 版本重新构建：服务器 backend/ 若为 git 仓库 `git checkout <commit>` 后走「后端更新」流程；否则保留旧源码备份重传 |
| 前端 | 保留旧 `frontend_artifacts.tar`（或 .next 备份），回传后重建前端镜像 |
| 配置 | 恢复 .env.prod 备份 → 三步重建 langgraph-api |
| 数据 | 数据库/语义库在卷中（见下），重建容器不丢 |

---

## 四、数据与持久化

| 卷 | 内容 | 说明 |
|----|------|------|
| `nl2sql-app_agent_data`（挂载 `/app/data`） | **外部数据根**：`/app/data/shared`（memory/skills/checkpoint 回退/trace/feedback）+ `/app/data/workspace`（db_config.json、语义库、报告） | 由 `.env.prod` 的 `AGENT_DATA_ROOT=/app/data` 指定；首启自动从镜像内 `src/agent/{shared,workspace}` 拷贝种子（仅缺失时）；**重建容器不丢** |
| `nl2sql-app_nl2sql_pg_data` | nl2sql_checkpoint 数据库（PostgreSQL 14） | 独立 postgres 容器 |

> 全量备份建议：`pg_dump` checkpoint 库 + 复制 `agent_data` 卷中的 `workspace/db_config.json` 与 `shared/model_config.json`。

### ⚠️ 存量升级迁移（旧 `nl2sql-app_workspace` 卷 → 新 `agent_data`）

旧版把 workspace 单独挂 `workspace:/app/src/agent/workspace`；升级后统一切到 `agent_data:/app/data`。
**旧卷里的业务数据（db_config.json / 语义库）不会自动出现**，需一次性拷贝：

```bash
docker-compose stop langgraph-api && docker-compose rm -f langgraph-api
# 旧 workspace 卷内容 → 新 agent_data 卷的 workspace/ 子目录
docker run --rm -v nl2sql-app_workspace:/old -v nl2sql-app_agent_data:/new \
  alpine sh -c "mkdir -p /new/workspace && cp -a /old/. /new/workspace/"
docker-compose up -d langgraph-api
```
> 新部署（空卷）无需此步：`/app/data/workspace` 首启从镜像内 `src/agent/workspace` 种子初始化（若镜像内没有该目录则跳过，由 UI/语义库配置另行初始化）。

---

## 五、排障速查

| 症状 | 原因 | 处理 |
|------|------|------|
| 容器无限重启 | 后端：MCP 工具为空（未配置库）/ 前端：next.config.ts 加密 | 看日志；配 db_config.json；换明文 next.config.ts |
| `未配置任何数据库` | dbmcp 子进程缺 env 或 store 为空 | 确认 mcp_tool.py 有 `**os.environ` 补丁；用 UI/store 配库 |
| `relation "checkpoints" does not exist` | checkpoint 表未初始化（新部署空库） | 跑一次 `AsyncPostgresSaver.setup()` 建表（见首次部署第 5 节） |
| `ContainerConfig KeyError` | docker-compose v1 重建 bug | stop → rm -f → up -d 三步 |
| `mcp-server-chart FAILED` / `mcp-echarts: No such file` | 图表 MCP 不可用 | 镜像需全局安装 mcp-echarts（Dockerfile 已加 `npm install -g mcp-echarts`）；代码仅保留 echarts（semiotic 已移除） |
| Langfuse 404 prompt | Prompt 未在 Langfuse 创建 | UI 创建（label=production）或忽略（回退本地） |
| 容器内 `ls` 中文文件名显示 `$'\345\277\220'` octal 转义 | 容器 shell 无 UTF-8 locale（镜像未带 LANG/LC_ALL=C.UTF-8，或容器未重建） | Dockerfile 已 `ENV LANG/LC_ALL=C.UTF-8`（2026-08-31）；重新发布后端（`release-backend.ps1` 重建镜像 + 三步重启）生效。验证：`docker exec nl2sql-app_langgraph-api_1 bash -c 'locale | grep LANG; ls /app/data/workspace/'`。**注意：只改 compose 的 `environment` 也必须重建容器（restart 不重新读 env）** |
| 前端页面打不开 | .next 过期 / nginx 未代理 | 重新 yarn build + 上传；检查 nginx 日志 |

---

## 六、关键文件清单

| 文件（服务器） | 说明 |
|---------------|------|
| `docker-compose.yml` | 4 服务编排（nginx/frontend/langgraph-api/postgres） |
| `.env.prod`（600） | 生产环境变量（LLM key、langfuse、PG 密码等） |
| `backend/` | 后端源码 + Dockerfile（env 继承补丁已合入本地仓库 mcp_tool.py；Dockerfile 含 mcp-echarts 全局安装） |
| `frontend/` | 前端产物（.next/public）+ 运行时 Dockerfile + 明文 next.config.ts |
| `docker/nginx.conf` | 反向代理 + SSE 支持 |

**服务器补丁备份**：`backend/src/agent/tools/mcp_tool.py.bak-20260827`（env 继承补丁前的原版）。

---

## 七、新服务器生产环境——容器服务与镜像总清单（2026-08-28）

> 适用：在**全新服务器**上部署完整生产环境（NL2SQL 应用 + Langfuse v4 栈）。
> **Chinook 为业务数据库，不列入本清单**（随业务变动，另行部署）。
> 镜像源：`docker.io` 直连在此网络不通（DNS 只回 IPv6），一律走 **daocloud**（`docker.m.daocloud.io`）。

### 7.1 前端（frontend + nginx）

**镜像**

| 镜像 | 用途 | 来源 |
|------|------|------|
| `docker.m.daocloud.io/node:20-alpine` | 前端运行时基础 | 拉取 |
| `docker.m.daocloud.io/nginx:alpine` | 反向代理 / 统一入口 | 拉取 |
| `nl2sql-app_frontend`（自构建） | 前端应用镜像（.next 产物 + yarn install + next start） | `frontend/Dockerfile` 构建 |

**容器**

| 容器 | 镜像 | 宿主端口 | 数据卷 |
|------|------|---------|--------|
| frontend | nl2sql-app_frontend | 内部 3000（宿主不发布） | — |
| nginx | nginx:alpine | 8080→80 | — |

### 7.2 后端（langgraph-api + checkpoint PG）

**镜像**

| 镜像 | 用途 | 来源 |
|------|------|------|
| `python:3.13-slim` | 后端构建基础 | daocloud 拉取 + `docker tag` |
| `docker.m.daocloud.io/postgres:14` | checkpoint 数据库 | 拉取 |
| `nl2sql-app_langgraph-api`（自构建） | 后端应用镜像（Node 20 + mcp-echarts 已内置） | 后端 `Dockerfile` 构建 |

**容器**

| 容器 | 镜像 | 宿主端口 | 数据卷 |
|------|------|---------|--------|
| langgraph-api | nl2sql-app_langgraph-api | 2026 | workspace |
| nl2sql-postgres | postgres:14 | 127.0.0.1:5435 | nl2sql_pg_data |

### 7.3 Langfuse 平台（v4 栈，6 容器）

**镜像**

| 镜像 | 用途 | 来源 |
|------|------|------|
| `langfuse/langfuse:4` | Langfuse Web | daocloud 拉取 + `docker tag` |
| `langfuse/langfuse-worker:4` | Langfuse Worker | daocloud 拉取 + `docker tag` |
| `clickhouse/clickhouse-server:25.12` | v4 事件/分析存储 | daocloud 拉取 + `docker tag` |
| `docker.m.daocloud.io/redis:7` | 任务队列 | 拉取 |
| `docker.m.daocloud.io/minio/minio:RELEASE.2025-07-23T15-54-02Z` | 媒体存储（S3） | 拉取 |
| `docker.m.daocloud.io/postgres:14` | langfuse 元数据库 | 拉取 |

**容器**

| 容器 | 镜像 | 宿主端口 | 数据卷 |
|------|------|---------|--------|
| langfuse-web | langfuse/langfuse:4 | 3010→3000 | — |
| langfuse-worker | langfuse/langfuse-worker:4 | 内部 3030 | — |
| langfuse-clickhouse | clickhouse/clickhouse-server:25.12 | 18123→8123（HTTP）；19000→9000（原生） | langfuse_clickhouse_data / _logs |
| langfuse-redis | redis:7 | 6381→6379 | langfuse_redis_data |
| langfuse-minio | minio/minio:RELEASE.2025-07-23 | 9090（S3）；9091（控制台） | langfuse_minio_data |
| langfuse-postgres | postgres:14 | 127.0.0.1:5433 | langfuse_postgres_data |

**合计**：前端 2 容器 + 后端 2 容器 + Langfuse 6 容器 = **10 容器**；镜像含 2 个自构建 + 9 个基础（postgres:14 跨前后端/Langfuse 复用，实际拉取 8 个）。

```bash
# 拉取 + 打 tag 示例（daocloud → 标准名）
docker pull docker.m.daocloud.io/python:3.13-slim && docker tag docker.m.daocloud.io/python:3.13-slim python:3.13-slim
docker pull docker.m.daocloud.io/langfuse/langfuse:4 && docker tag docker.m.daocloud.io/langfuse/langfuse:4 langfuse/langfuse:4
# ... 其余同理
```

### 7.4 端口占用一览

| 端口 | 服务 |
|------|------|
| 3010 | Langfuse Web |
| 18123 / 19000 | ClickHouse HTTP / 原生 |
| 6381 | Redis |
| 9090 / 9091 | MinIO S3 / 控制台 |
| 5433 | langfuse PG |
| 8080 | nginx（NL2SQL 统一入口） |
| 2026 | NL2SQL 后端 |
| 5435 | nl2sql checkpoint PG |

### 7.5 部署顺序建议

1. **Langfuse 栈**：postgres → clickhouse → redis → minio → web + worker（`WRITE_MODE=dual`，镜像 v4）
2. **NL2SQL 应用**：postgres → langgraph-api（先配好 db_config.json 或 UI 配库）→ frontend → nginx
3. 验证：`/ok` 200、UI 200、`docker logs` 看 MCP 工具加载

### 7.6 注意

- `postgres:14` **一个镜像被 2 个容器复用**（langfuse / nl2sql），各自独立卷，互不影响——**不合并实例**（故障域隔离）
- 端口若冲突，按惯例调整（redis 用 6381 即因宿主 6379 被其他项目占用）
- 业务数据库（如 Chinook）**不在本清单**，由业务侧另行部署

### 7.7 镜像复用与离线迁移规范

**原则**：新服务器部署时，先查本地/源服务器已有镜像，能复用则不复拉；应用类镜像（带代码的 `nl2sql-app_*`）始终重新构建，不复用。

**① 直接复用（192.168.25.64 已有，零下载）**
```bash
docker images   # 确认存在后，compose/docker run 直接引用镜像名
```
可复用：`postgres:14`、`redis:7`、`minio RELEASE.2025-07-23`、`nginx:alpine`、`node:20-alpine`、`python:3.13-slim`、`clickhouse:25.12`、`langfuse:4`、`langfuse-worker:4`
> ⚠️ 镜像可复用 ≠ 数据可复用：**必须新建容器/卷/端口**，绝不复用现有容器的数据卷。

**② 直接拉取（新服务器有 daocloud 网络）**
```bash
docker pull docker.m.daocloud.io/<镜像>:<tag>   # docker.io 直连不通，一律走 daocloud
# 需要标准名的（FROM 引用），拉取后打 tag
docker tag docker.m.daocloud.io/python:3.13-slim python:3.13-slim
```

**③ 离线迁移（源服务器 save → 传输 → 新服务器 load）**

适用于新服务器无外网/隔离网络。**只添加镜像，不影响现有容器与卷**。

```bash
# 源服务器（192.168.25.64）打包
docker save -o /tmp/base_images.tar \
  docker.m.daocloud.io/postgres:14 docker.m.daocloud.io/redis:7 \
  docker.m.daocloud.io/minio/minio:RELEASE.2025-07-23T15-54-02Z \
  docker.m.daocloud.io/nginx:alpine docker.m.daocloud.io/node:20-alpine \
  python:3.13-slim clickhouse/clickhouse-server:25.12 \
  langfuse/langfuse:4 langfuse/langfuse-worker:4

# 传输（两服务器互通：直接 scp；隔离网络：经本机中转 scp 两段）
scp /tmp/base_images.tar weint@<新服务器>:/tmp/

# 新服务器加载
docker load -i /tmp/base_images.tar
docker images    # 确认镜像与 tag 就位
```

**④ 注意**

- `docker save` 的 tar 通常比镜像显示尺寸小（层已压缩）；全部基础镜像合计约 3GB，全部镜像（含自构建）约 9GB
- 复制 = 固定当前版本，不随上游更新；需要升级时重新拉取/重新导出
- 若两服务器能直接互通且目标有外网，**直接拉取优于离线复制**（官方 CDN 更快、可获最新版）
