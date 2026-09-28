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
  --exclude=src/agent/shared/model_config.json --exclude=auth_secret `
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

脚本位置（本机；2026-09-23 起统一放在运维文档目录，`scripts/` 下的旧位置已移除）：
```
D:\code_work_space\llm\nl2sql\docs\weint环境\发布脚本\
├── release-backend.ps1     # 后端：打包→上传→打回滚tag→清空解压+重建→同步运行期shared→排空后重启→验证
├── release-frontend.ps1    # 前端：yarn build→打包产物→上传→重建→重启→验证
└── rollback-backend.ps1    # 后端回滚（按 rollback-* tag）
```

用法（本机 PowerShell，**已配置 SSH 密钥免密，无需输密码**）：
```powershell
# 后端发布
powershell -ExecutionPolicy Bypass -File "D:\code_work_space\llm\nl2sql\docs\weint环境\发布脚本\release-backend.ps1"
# 应急：跳过第 5 步（同步运行期 shared/skills + shared/memory）
powershell -ExecutionPolicy Bypass -File "D:\code_work_space\llm\nl2sql\docs\weint环境\发布脚本\release-backend.ps1" -SkipSharedSync

# 前端发布（DLP 致 build 失败时：手动 yarn build 后加 -SkipBuild）下面两条命令2选1
 # 情况 1：直接跑（脚本自己 build）
powershell -ExecutionPolicy Bypass -File "D:\code_work_space\llm\nl2sql\docs\weint环境\发布脚本\release-frontend.ps1"
 # 情况 2：build 失败/已手动 build 过 → 跳过 build
cd D:\code_work_space\llm\huice\008\harness-deep-agents-ui

yarn build
powershell -ExecutionPolicy Bypass -File "D:\code_work_space\llm\nl2sql\docs\weint环境\发布脚本\release-frontend.ps1" -SkipBuild
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
  --exclude=src/agent/shared/model_config.json --exclude=auth_secret `
  -C D:\code_work_space\llm\nl2sql .
  
scp D:\code_work_space\llm\deepseek-workspace\backend_update.tar weint@192.168.25.64:/home/weint/apps/nl2sql/nl2sql-app/
```

**② 服务器解压 + 重建 + 排空后重启 + 验证**（bash）：
```bash
cd /home/weint/apps/nl2sql/nl2sql-app
# ⚠️ 必须先清空 backend/ 再解压：`tar -xf` 只覆盖不删除，残留的旧文件会被 Dockerfile 的
#    `COPY . .` 打进镜像（仓库里删掉的 src/api/workspace.py、改名前的旧 skill 目录会"复活"）。
cp backend_update.tar backend_update.tar.prev      # 上一版留一份，构建失败可重解压（可选）
rm -rf backend && mkdir backend
tar -xf backend_update.tar -C backend && rm backend_update.tar
docker-compose build langgraph-api
# 同步运行期 skills + memory（等价于脚本第 5 步；跑脚本的请跳过，见 §二.1.2）
# P2-1 优雅停机：先排空（在跑的 run 跑完；新提交拿明确 503），再停
docker exec nl2sql-app_langgraph-api_1 python /app/scripts/ops_drain.py --budget 210
docker-compose stop -t 240 langgraph-api && docker-compose rm -f langgraph-api
docker-compose up -d langgraph-api
sleep 60 && curl http://127.0.0.1:2026/ok
docker logs nl2sql-app_langgraph-api_1 2>&1 | grep -E "MCP 工具加载完成|预检通过|error|\[drain\]"
# 期望：✅ MCP [mcp-server-echarts]: 正常 / ✅ [sub] dbmcp: N tools loaded / ✅ 预检通过
#      且日志里有 [drain] 排空完成：…（见 §二.1.1）
```
> 依赖没变时构建约 1-2 分钟（Docker 层缓存）；改了 pyproject/uv.lock 则 5-15 分钟。
> ✅ 本地仓库已含全部补丁（env 继承 + Dockerfile mcp-echarts），无需服务器重打。

<a id="drain"></a>
#### 1.1 优雅停机（P2-1）：停之前会发生什么

**背景**：`docker stop` 默认 10 秒就 SIGKILL，而 langgraph inmem 运行时自己的排空窗口是
**硬编码 5 秒**——所以在跑的 run（一次问数约 3 个后台任务，可能跑几分钟）被腰斩，症状是
前端"半轮对话/转圈"，没人会把它当成发版故障。

**两步合起来才是完整的优雅停机**：

| 步 | 谁触发 | 管什么 | 需要口令吗 |
|----|--------|--------|-----------|
| ① 协作式排空 | `ops_drain.py`（容器内跑）→ `POST /api/admin/drain` | 排空期间**新提交**立刻拿到 503 + 中文提示；并把"还剩几个后台任务"打给运维 | 不需要（容器内用现有管理员账号签 token） |
| ② 信号式排空 | `docker-compose stop -t 240` | SIGTERM → uvicorn 停机 → `custom_app` lifespan 等清零 → 之后才是 langgraph 的 5 秒窗口 | 不需要 |

- 预算：后端 `NL2SQL_DRAIN_SECS`（默认 180，`0` = 只拒绝新请求不等待）；`stop -t` **必须大于它**，
  否则 docker 的 SIGKILL 会比等待先到、白做。
- 运维可中途反悔：`docker exec nl2sql-app_langgraph-api_1 python /app/scripts/ops_drain.py --undrain`。
- 预算用尽仍有任务在跑 → 日志 `[drain] **排空超时**：预算 180.0s 用尽，仍有 N 个后台任务在跑…`
  （这条日志就是事后归因那批"半轮对话"的唯一线索）。
- 症状对照：`backend_ok:200` 且日志里**没有** `[drain]` 行 = 排空没跑（多半是旧镜像或
  `stop` 没带 `-t`）。
- 不重建镜像的场景（`restart` / 容器 OOM / docker daemon 重启）走 compose 的
  `stop_grace_period`，默认 10s。**服务器侧已于 2026-09-24 加好**（备份见下），加到
  `$AppDir/docker-compose.yml` 的 `langgraph-api` 服务里：
  ```yaml
  langgraph-api:
    stop_grace_period: 240s    # P2-1：须 > NL2SQL_DRAIN_SECS(180)
  ```
  加了之后 `docker-compose restart langgraph-api`（§二.4/5）也会等排空。
  核实：`docker inspect -f '{{.Config.StopTimeout}}' nl2sql-app_langgraph-api_1` → `240s`
  （**改完必须重建容器才生效**，正好被发版的 stop/rm/up 覆盖）。
  该补丁不会被发版覆盖：发布包虽然**含** `docker-compose.yml`，但解压落点是
  `backend/docker-compose.yml`，而 compose 实际读的是 `$AppDir/docker-compose.yml`（服务器侧）。

#### 1.2 改了 skill / 记忆：发版第 5 步会自动同步（但要懂它同步的是什么）

**机制（以前为什么要手工同步）**：deepagents 实际读的是外置**运行期副本**
`<AGENT_DATA_ROOT>/shared/skills`（记忆同理，`shared/memory`），它只在**目录缺失时**从镜像内
`src/agent/shared/{skills,memory}` 播种一次 —— **发版（哪怕 `src` 整包全等）不会覆盖它**。
所以"改了 `SKILL.md` + 发版"≠"线上生效"（`src/agent/skills` 那份是死副本，改它等于没改）。

**2026-09-25 起 `release-backend.ps1` 第 5/7 步自动做这件事**：把宿主机刚解压的
`${AppDir}/backend/src/agent/shared/{skills,memory}` 整目录**替换**到运行期（`docker cp` 进容器 →
容器内 `mv` 换目录），备份落 `/app/data/shared.bak-<ts>.tgz`，并打印 SKILL.md / memory 计数。
**放在重启之前是必须的**：`memory/ORCHESTRATOR.md` 是 import 期读入（`main_agent.py:254` 的
`create_deep_agent` 在模块级）⇒ 记忆要重启才生效；skills 不需要重启（`SkillsMiddleware.before_agent`
每次读盘）。应急时加 `-SkipSharedSync` 跳过。

**三个前提（改动这一段前先读）**：
1. **只换 `skills` 与 `memory` 两个子目录**。`shared/` 下还住着 `checkpoint/`、`trace/`、`feedback/`、
   `model_config.json` —— 整树镜像会把运行期数据删掉。安全性依据：agent 的写权限只有
   `workspace/{report,tmp,nl2sql_process_data}`，这两个子树**没有运行期写入者**，所以可以整目录换。
2. **必须是"替换"而不是 `cp -a` 叠加**：技能改名/删除时 `cp -a` 只加不删，会把新旧两套都留在运行期
   （旧 SOP 继续被模型读到）。本次 `nl2sql-*` → `wren-*` 正是这种情况。
3. **种子取自宿主机 `${AppDir}/backend/src`** ⇒ 第 4 步必须**先清空 `backend/` 再解压**
   （`tar -xf` 只覆盖不删除，不清会把仓库里已删的文件留在镜像里 ⇒ 旧技能"复活"）。脚本第 4 步已是
   `rm -rf backend && mkdir backend && tar -xf …`，上一棵树留在 `backend_release.tar.prev` 可回滚。

**体检（只读，第 7/7 步会自动跑；自动同步后 exit 0 是预期）**：
```bash
docker exec nl2sql-app_langgraph-api_1 python /app/scripts/check_skills_drift.py
# exit 0=一致 / 3=漂移（打印 only-seed / only-runtime / changed 三组）/ 1=出错
```
exit=3 的常见原因：本次用了 `-SkipSharedSync`、发版时容器没在跑、有人事后手工改了运行期那份。

**手工同步（只在自动步骤没生效时用；注意是整目录替换，不要 `cp -a`）**：
```bash
cd /home/weint/apps/nl2sql/nl2sql-app/backend/src/agent/shared
docker cp skills nl2sql-app_langgraph-api_1:/app/data/shared/skills.new
docker cp memory nl2sql-app_langgraph-api_1:/app/data/shared/memory.new
docker exec nl2sql-app_langgraph-api_1 bash -c 'set -e; cd /app/data/shared; tar -czf /app/data/shared.bak-$(date +%Y%m%d-%H%M%S).tgz skills memory; rm -rf skills.old memory.old; mv skills skills.old; mv memory memory.old; mv skills.new skills; mv memory.new memory; rm -rf skills.old memory.old; find skills -name SKILL.md | wc -l'
# 换 skills 不用重启；换 memory 要重启：
# docker-compose stop -t 240 langgraph-api && docker-compose rm -f langgraph-api && docker-compose up -d langgraph-api
```

> 2026-09-24 手工执行过一次 skills 同步（`chart-saver` 2 个文件漂移，备份 `skills.bak-20260924-130243.tgz`，
> 漂移 exit **3 → 0**）。
>
> ⚠️ **还有第三处根本不走发版**：system prompt 住在 **Langfuse**（`_build_system_prompt` /
> `get_prompt_text`，**import 期**求值），发版只把本地 `.md` 送进镜像当种子。技能改名这类迁移是
> **三件套**（prompt + memory + skills），只同步一半 = 提示词让模型去用不存在的技能。
> 推 prompt：`python -m agent.prompt.sync_prompts --all`（用本机 `.env.prod`），之后重启。
> 同一批还有代码侧的 `_TOOL_OWNER_SKILLS`（`middlewares/langfuse_span.py`）—— 注释写明
> owner 拼写必须 = 真实 skill 目录名，技能改名时必须一起改。

#### 1.3 后端日志在哪、怎么按一次请求串起来（P2-2）

**位置**（P2-2 起）：`<AGENT_DATA_ROOT>/logs/agent-server.log` = **`/app/data/logs/agent-server.log`**。
`/app/data` 就是持久卷 `nl2sql-app_agent_data` ⇒ **重建容器（每次发版）也不丢**，所以本项**无需**给服
务器 compose 加任何挂载。（P2-2 之前写的是 `/app/logs`，那在容器可写层里 → 重建即丢；升级那一次会把
旧 `/app/logs/agent-server.log` 一起扔掉，这是预期。目录解析顺序：`NL2SQL_LOG_DIR` > `<AGENT_DATA_ROOT>/logs`
> 仓库根 `logs/`，生产**不要**设 `NL2SQL_LOG_DIR`。）

```bash
# 看当前日志（宿主侧一条命令；轮转：每天 0 点一个文件，保留最近 7 个历史 + 当前）
docker exec nl2sql-app_langgraph-api_1 tail -n 100 /app/data/logs/agent-server.log
# 按一次请求串起来：浏览器里任一请求的响应头/报错信息里带 X-Request-ID（12 位），直接搜
docker exec nl2sql-app_langgraph-api_1 grep -F 'rid=1a2b3c4d5e6f' /app/data/logs/agent-server.log
```

每行格式 `... [rid=<id>] <消息>`：**请求上下文之外的后台日志是 `rid=-`**（不串味）。访问行形如
`[access] rid=… POST /runs/stream -> 200 latency=7.9ms total=8.1ms ip=… user=…`，5xx 记 `ERROR`，
`/ok`（healthcheck）不记；`no response: 客户端提前断开或异常` 表示响应没走完（status 为 `-`）。

⚠️ **nginx 侧补丁（属服务器侧、发布包不含 `docker/nginx.conf`）—— 2026-09-24 已应用并校验**
（备份 `docker/nginx.conf.bak-20260924-130220`；实测不带 id → 响应头 32 位 hex、带
`X-Request-ID: rid-probe-12345` → 原样沿用）。**每次动它都要重新走一遍下面这一套**。

**一键应用（推荐，本机 Git Bash，全程不弹口令）**：

```bash
bash docs/weint环境/发布脚本/apply-nginx-request-id-patch.sh --dry-run   # 只看将要写入的指令
bash docs/weint环境/发布脚本/apply-nginx-request-id-patch.sh             # 应用 + 自校验（失败自动回滚）
bash docs/weint环境/发布脚本/apply-nginx-request-id-patch.sh --rollback  # 回滚到最近一次备份
```

脚本做：核挂载 → md5 判差异（已一致就退出 0）→ 备份 `docker/nginx.conf.bak-<ts>` → **就地**写入
（`cat > 同一 inode`）→ 比对 inode/md5 → `nginx -t`（失败自动回滚）→ `nginx -s reload` → 校验响应头
（不带 id 应返回 32 位 hex；带 `X-Request-ID: rid-probe-12345` 应原样沿用）与 nginx 日志行。

手工等价步骤（脚本跑不了时）：

```bash
d=/home/weint/apps/nl2sql/nl2sql-app            # 在 192.168.25.64 上
cp -p $d/docker/nginx.conf $d/docker/nginx.conf.bak-$(date +%Y%m%d-%H%M%S)
# 就地写入新内容（**必须 `cat > 同一个 inode`，不能用 mv 换文件**：compose 是
# `./docker/nginx.conf:/etc/nginx/nginx.conf:ro` 的**文件级** bind mount，换 inode 后
# 容器仍读旧内容）—— 内容 = 仓库 `docker/nginx.conf`（已含线上那段 P0-3 注释）
docker exec nl2sql-app_nginx_1 nginx -t && docker exec nl2sql-app_nginx_1 nginx -s reload
# 校验：响应头出现 X-Request-ID；nginx 日志行形如 … 200 0.123 s rid=<32hex>
curl -s -o /dev/null -D - http://127.0.0.1:8080/ok | grep -i x-request-id
docker logs --tail 3 nl2sql-app_nginx_1
```

补丁做了三件事：① `map $http_x_request_id $rid`（客户端自带就沿用，否则用 nginx 生成的
`$request_id`）+ `proxy_set_header X-Request-ID $rid` 到三个 location；② `log_format`/`access_log`
带 `$request_time` 与 rid；③ `add_header X-Request-ID $rid always` —— **nginx 自己就回显 id，
不依赖后端是否已发版**（后端中间件是"响应头没有才补"，不重复），且 `always` 让 nginx 自己产生的
502/504 也带 id（那类请求到不了后端）。`nginx -t` 失败就 `cat <备份> 回去`，**reload 前坏文件不影响
正在跑的 nginx**，不需要重建容器。

另注：nginx 官方镜像里 `access.log` 是指向 stdout 的软链 → 那份日志进 `docker logs nl2sql-app_nginx_1`，
**容器被 `rm` 即丢**（与后端的持久日志不同）。

#### 1.4 指标与告警：`/metrics` 怎么看、告警在哪（P2-3）

**一句话**：后端进程每次启动会自带一个采样任务（间隔 10s，`NL2SQL_METRICS_INTERVAL_SECS` 可调，
**改了要重启**），把队列深度/事件循环延迟/内存/锁等待/LLM 成败等写进 prometheus 指标；
`GET /metrics` 一次给出全部（`?format=json` 是队列与 worker 的快照）。

```bash
# 取指标（后端端口 2026 只在容器网络里，宿主机要用 docker exec；镜像里没 curl，用 python）
docker exec nl2sql-app_langgraph-api_1 python -c "
import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:2026/metrics').read().decode())" | grep '^nl2sql_'
# 只看队列深度与事件循环延迟（并发压测时最该盯的两条）
docker exec nl2sql-app_langgraph-api_1 python -c "
import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:2026/metrics?format=json').read().decode())"
# → {"queue":{"n_pending":0,"n_running":0,...},"workers":{"max":10,"active":0,"available":10}, ...}
```

**为什么外部取不到**（刻意如此，不是漏配）：`/metrics` **不在**鉴权白名单，带 Cookie 或带 XFF 的
请求一律 401；只有容器内网（内网 IP + 无 Cookie + 无 XFF）放行（`docker exec` 进去打 127.0.0.1 就是）。
生产 nginx 只把 `/api/` 与 `/threads|runs|…` 转后端，`/metrics` 落到**前端**（`location /`）→
浏览器打 `http://192.168.25.64:8080/metrics` 得到的是前端 404。**要接面板**就让抓取端（Prometheus/
Grafana/自建脚本）在**容器网络内或宿主机上**经 `docker exec`/`--network` 取。

**并发压测时该看什么**（对应 P2-3 的验收）：

| 指标 | 现象 |
| --- | --- |
| `nl2sql_run_queue_running` / `_pending` | 在跑 / 排队；`available`（见 `nl2sql_worker_slots{state="available"}`）归零＝并发到顶 |
| `nl2sql_event_loop_lag_seconds`（+ `_max_seconds`） | 健康是毫秒级；>0.5s 说明有同步代码堵住了事件循环（同进程所有请求一起慢） |
| `nl2sql_sqlite_lock_wait_seconds{store=…}` / `nl2sql_sqlite_lock_contention_total` | 谁在锁上排队（store=feedback/grants/eval_queue/trace_events/trace_bind/thread_search） |
| `nl2sql_llm_calls_total{outcome="timeout"|"error"}` / `nl2sql_llm_latency_seconds` | 模型侧失败率与耗时分布 |
| `nl2sql_process_rss_bytes`、`nl2sql_mcp_servers{status="failed"}`、`nl2sql_process_children` | 内存水位、MCP 是否有条目加载失败、此刻几个 MCP 子进程 |

**最小告警 = 日志里的 `[alert]` 行**（不是告警系统，不做推送）：每条规则**连续 N 轮**成立才记一条
WARNING，**只在状态翻转时记**（不刷屏），恢复记 `[alert-clear]`。

```bash
docker exec nl2sql-app_langgraph-api_1 grep -E '\[alert' /app/data/logs/agent-server.log | tail -20
# → [alert] run_slots_saturated | 在跑 run 8 个 >= 8（槽位上限 10）：并发已到天花板…（阈值 NL2SQL_ALERT_RUNNING_RUNS=8）
```

阈值都在环境变量里（`.env.prod` / compose，**每轮现读，不用重启**；`0` = 关闭该条；填了非数字会
退回默认并记一条 warning）：`NL2SQL_ALERT_LOOP_LAG_SECS`(0.5) / `NL2SQL_ALERT_RUNNING_RUNS`(8) /
`NL2SQL_ALERT_PENDING_RUNS`(20) / `NL2SQL_ALERT_RSS_MB`(4096) / `NL2SQL_ALERT_LLM_FAILURES`(3) /
`NL2SQL_ALERT_LOCK_CONTENTIONS`(3) / `NL2SQL_ALERT_MCP_FAILED_SERVERS`(1)。面板上还能看
`nl2sql_alert_active{alert=…}`（1=正在响）。

**两个排查要点**：① `/metrics` 里一个 `nl2sql_*` 都没有＝**采样任务没起**（多半是只 `docker cp` 了代码
而没重启，采样任务在 lifespan 里启动）；上游的 `python_gc_*` 还在不代表我们的在。② 输出首行若是
`# nl2sql: … 队列指标缺失`，说明 langgraph 的 handler 没解析到（上游挪模块了）——自采指标照常，但
队列深度没了，跑 `scripts/verify_metrics.py` 第 ⑤ 段确认。

#### 1.5 「进度卡一直执行中 / 图表报告没出来」怎么查（P2-4）

**现象与根因**：子任务跑完后，watcher 要把终态写回主线程 state（`async_tasks[task].status` 是前端
**自动续跑的唯一依据**）。但 LangGraph 对 `threads.update_state` 有硬闸：**主线程只要还有 pending/running
的 run 就返回 409**（`Thread is busy with a running job`），而并发一起来主线程就是忙的。watcher 重试到
天花板（300s）还写不进就只能放手 —— 老版本放手即**永久丢失**：进度卡停在「执行中」，**自动续跑不触发
⇒ 用户看不到图表/报告**（不只是显示问题）。P2-4 之后，放手的终态会**先落 SQLite 再补写**，所以：

| 你看到的 | 实际含义 | 该做什么 |
| --- | --- | --- |
| 进度卡停在执行中，且 **`--status` 的 pending 长期 >0** | 终态还在待补写（主线程一直忙） | 看 `/metrics` 的 `nl2sql_run_queue_running/_pending` 与 `[alert] run_backlog`：多半是 run 槽饱和或有 run 卡死 |
| 卡片停在执行中，但 `pending` = 0 | 不是 P2-4 这条路（终态已写或从没登记） | 按前端/`async_tasks` 自身查（P1-9 的「终态写了、清零没写」是另一半） |
| 日志出现 `[pending-terminal] 放弃补写 sub=…`（ERROR） | 超过 6h（`NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS`）仍写不进，**有意不假装成功** | 查僵尸 run（`.langgraph_ops.pckl`）；重启会清掉它，残留行下次启动立刻重放 |

```bash
# 看还剩几行待补写 / 几行已放弃（只读；--list 列出明细，--replay 立刻补一轮）
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.subagents.pending_terminal --status'
# → {"path":"/app/data/pending_terminal/pending_terminal.sqlite","pending":0,"abandoned":0}

# 只判定不写（看它打算怎么处理每一行，不发写请求）
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.subagents.pending_terminal --replay --dry-run'

# 进度卡相关的日志（登记/丢行/补写都记在这两处）
docker exec nl2sql-app_langgraph-api_1 grep -E "pending-terminal|写入最终 subagent_steps_map|写 async_tasks" /app/data/logs/agent-server.log | tail -20
```

**别做的三件事**：① **别手工删 `pending_terminal.sqlite` 里的行**（补写器在跑，删了等于永久丢掉那个终态，
要用就 `--replay` 或等它自己滚；确有坏行用 `--list` 看清再定）；② **别把补写器当成"重试更久"** —— 它补写的
前提是主线程**真的空下来**，主线程永久卡死时它只会到期记 ERROR（这是有意的降级）；③ 发版**只 `docker cp`
不重启 ⇒ 补写器没起**，表里会积行却没人补（`--status` 的 pending 只涨不掉就是这个特征）。

**表与保留**：`<AGENT_DATA_ROOT>/pending_terminal/pending_terminal.sqlite`
（**一库一目录**，2026-09-25 起从数据根目录归位；持久卷，随 `AGENT_DATA_ROOT`；首次使用自动建），
`pending` 行补写成功即删，`abandoned` 行留痕 **7 天**后由补写器自己清（`purge_old`）。转发**不需要**
改 compose/nginx、也没有新依赖。

#### 1.6 磁盘占用与保留策略（P2-5）

**它自动做什么**：后端进程启动时会拉起一个维护线程，**默认每小时**跑一轮按龄清理，并在 `/metrics`
里报各区域占用与数据卷水位。**启动即跑一轮**（不等你问），所以重启本身就是一次回收时机。

| 区域 | 默认保留 | 说明 |
| --- | --- | --- |
| `trace_events`（trace 事件库） | 30 天 | 机器产生的埋点 |
| `eval_queue`（待评队列） | 30 天 | 只删**终态**行，在跑的/待评的不动 |
| `large_tool_results/` | 30 天 | 大工具结果落盘 |
| `conversation_history/` | 30 天 | 历史会话快照 |
| `<工作区>/tmp/` | 7 天 | 中间产物，目录本身留着 |
| **`report/`（报告交付物）** | **0 = 默认不清** | 用户下载过的东西 |
| **`message_feedback.db`（反馈/金标）** | **0 = 默认不清** | 含人工标注（标注行**永远不删**） |

> ⚠️ **默认配置下不会删掉任何用户能看见的东西**。`report/` 与反馈库要清只能显式给 env 天数
> （见下表），且**不可逆**（没有回收站）—— 打开前先跑一次 `--run --dry-run` 看清单。

**怎么查 / 怎么手动清**（全部只读或显式触发，`--status` 不写任何东西）：

```bash
# ① 状态：七个目标的天数（0 = 关闭）、上次跑的时间与删了多少、各区域占用、数据卷水位
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.utils.retention --status'
# → {"enabled":true,"interval_secs":3600.0,"days":{...},"last_run":{"at":"…","removed":7,"freed_bytes":…},"areas":{...},"disk":{...}}

# ② 手动跑一轮：先 --dry-run 看清单（**零副作用**），确认无误再真跑
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.utils.retention --run --dry-run'
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.utils.retention --run'

# ③ 真回收磁盘（见下方「为什么盘的数不降」）：DELETE 只标空闲页，文件不缩，必须 vacuum
docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app && PYTHONPATH=/app/src /app/.venv/bin/python -m agent.utils.retention --vacuum trace'
```

**环境变量**（改完**要重建容器**才进 env，临时验可用 `docker exec -e`）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `NL2SQL_RETENTION_ENABLED` | 开 | 总开关，`0` = 整个线程不干活 |
| `NL2SQL_RETENTION_INTERVAL_SECS` | `3600` | 每轮间隔；**启动时读一次**，改了要重启 |
| `NL2SQL_RETENTION_DAYS_<目标>` | 见上表 | 目标名大写：`TRACE_EVENTS` / `EVAL_QUEUE` / `LARGE_TOOL_RESULTS` / `CONVERSATION_HISTORY` / `WORKSPACE_TMP` / `REPORT` / `FEEDBACK`；**`0` 或负数 = 关闭该目标** |
| `NL2SQL_ALERT_DISK_FREE_PCT` | `10` | 数据卷**剩余**空间低于该百分比即 `[alert] disk_low`，**连续 3 轮**才响；`0` = 关闭这条 |

**为什么盘的数不降（最容易被误读的一条）**：清理用的是 `DELETE`，SQLite **只把页标成空闲、文件不缩**。
所以删了几万行后 `ls -lh` 可能一模一样 —— 这不代表没干活，`--status` 里的 `removed` / `freed_bytes`
才是真凭据（文件类目标如 `tmp/` 会立刻体现在 `freed_bytes` 上）。要真缩文件得单独跑 `--vacuum`，
它会**重写整个库**（期间占额外空间、需独占），所以**故意不放进自动轮**里。

**告警信噪比**：磁盘水位是**拉取式**、只在 `[alert] disk_low` 落日志，`grep -E '\[alert'` 见 §二.1.4。
⚠️ 量的是 `AGENT_DATA_ROOT` 那个卷（`/app/data`），不是容器根 `/` —— 根是镜像层、几乎不动，
盯错卷的告警**永远不会响**。

**别做的三件事**：① **别手工 `rm` `report/` 里的文件**（`report_owner` 账本会对不上，读侧按归属判权，
要么走本模块、要么连账本一起处理；报告名不含用户，跨工作区可能同名）；② **别手工删
`message_feedback.db` 里的标注行**（那是人工劳动，本模块刻意不碰；要清只能人工确认后清）；
③ **发版只 `docker cp` 不重启 ⇒ 维护线程没起**（`--status` 里的 `last_run.at` 会停在很久以前、
`/metrics` 里没有 `nl2sql_disk_free_bytes`，而 `/metrics` 不会告诉你"这项被关了"）。

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
> 首启自动从镜像内 `src/agent/shared` 拷贝种子到 `/app/data/shared`（仅目录缺失时）；`/app/data/workspace` 的骨架由启动代码补齐，详见「四、数据与持久化」。
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
| `nl2sql-app_agent_data`（挂载 `/app/data`） | **外部数据根**：`/app/data/shared`（memory/skills/checkpoint 回退/trace/feedback）+ `/app/data/workspace`（**唯一工作区**：db_config.json、语义库、报告）+ `/app/data/logs`（后端日志，**P2-2**）+ 三个运行时库目录 `/app/data/{eval_queue,trace_bind,pending_terminal}/`（**一库一目录**，2026-09-25 起；每目录内是库 + `-wal` + `-shm`） | 由 `.env.prod` 的 `AGENT_DATA_ROOT=/app/data` 指定；`shared` 首启自动从镜像内 `src/agent/shared` 拷贝种子（仅缺失时），`workspace` 由后端启动时的 `WorkspaceManager.__init__` 补齐骨架（**2026-09-25 起路径钉死为 `<AGENT_DATA_ROOT>/workspace`，不再有「切换工作区」**）；**重建容器不丢**（含日志） |
| `nl2sql-app_nl2sql_pg_data` | nl2sql_checkpoint 数据库（PostgreSQL 14） | 独立 postgres 容器 |

> 全量备份建议：`pg_dump` checkpoint 库 + 复制 `agent_data` 卷中的 `workspace/db_config.json` 与 `shared/model_config.json`。
> 日志也在卷里（`/app/data/logs`，P2-2），**不受发版影响**；`/app/logs` 这个旧路径在容器可写层里（P2-2 前的位置），
> 里面若还有历史文件，重建容器即丢 —— 需要留档就在重建前 `docker cp` 出来。

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
> 新部署（空卷）无需此步：`/app/data/workspace` 由后端**启动时的 `WorkspaceManager.__init__`** 建出骨架
> （`report/ tmp/ nl2sql_process_data/ large_tool_results/ checkpoint/ feedback/` + 空 `db_config.json`）。
> ⚠️ 镜像里**没有** `src/agent/workspace` 种子（发版 tar 一直排除它），所以 `_seed_data_root_once` 那条路是空转
> —— 全新部署的初始化**只靠 `__init__` 这一个点**，别把它删了。库/语义库仍按老办法从 UI 配。

**运行时库「一库一目录」的自动归位**（2026-09-25 起，无需任何运维动作）：`eval_queue` / `trace_bind` /
`pending_terminal` 三个库原先连同 `-wal`/`-shm` 散在 `/app/data` **根目录**上，现在各自归到同名子目录
（`/app/data/pending_terminal/pending_terminal.sqlite` 这种）。升级后**首次启动**会把根上的老三件套
`rename` 过去 —— 前提是「没人打开过那份老库」，所以这一步排在 `_lifespan` 最前面（早于补写器与保留线程）。
不会丢数据：`pending_terminal` 里**没补写成功的终态行**、`trace_bind` 的绑定镜像都跟着走（`-wal` 一起搬，
否则已提交未 checkpoint 的事务会丢）。查证：

```bash
docker exec nl2sql-app_langgraph-api_1 bash -c 'ls -d /app/data/*/ ; ls /app/data/*.sqlite* 2>/dev/null || echo "根目录已无散落的 .sqlite*"'
docker exec nl2sql-app_langgraph-api_1 grep sqlite-paths /app/data/logs/agent-server.log | tail -5
# → 搬过的话有 [sqlite-paths] 老库已归位: /app/data/xxx.sqlite → /app/data/xxx/xxx.sqlite
```
> ⚠️ **接管只有一次机会，失败必须人工收尾（别指望重启自愈）**：没搬成（例如权限问题）会在日志里留一条
> warning，新库照常在子目录里建 —— 但**同一次启动里**补写器/保留线程立刻就把它建出来了，此后每次启动都因
> 「目标已存在」而不再尝试（刻意如此：绝不覆盖已有库）⇒ 老文件会**永远**留在根上。此时：
> ① **先别删老文件** —— 它是当时唯一的副本，`pending_terminal` 里没补写成功的终态行删了就永久丢；
> ② 停后端，`cp -a /app/data/<name>.sqlite* /app/data/<name>/` —— **复制，不是移动**；若目标库里已有内容，
>    停下来查清哪份才是真的，**不要覆盖**；
> ③ 起服务，用下方 §二.1.5 的 `--status` 核对条数一致后，才 `rm /app/data/*.sqlite{,-wal,-shm}`。

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
| 用户报「某次操作失败」但不知道是哪次 | 需要request-id | 让用户报响应里的 `X-Request-ID`（或任一报错文案），`grep rid=<id> /app/data/logs/agent-server.log`（见 §二.1.3）；nginx 侧同号补丁**已应用**（见同节末尾） |
| `/metrics` 里一个 `nl2sql_*` 都没有 | 采样任务没起（只 docker cp 代码没重启；它在 lifespan 里启动） | 重启后端（`logs/agent-server.log` 里搜 `metrics`）；见 §二.1.4 |
| 面板上看不到队列深度（`nl2sql_run_queue_*`） | 上游 handler 没解析到 / 输出首行有 `# nl2sql: … 队列指标缺失` | 自采指标仍可用；跑 `uv run python scripts/verify_metrics.py` 第 ⑤ 段确认上游模块是否挪位 |
| 某条告警一直响或一直不响 | 阈值在 env 里（`NL2SQL_ALERT_*`），本轮现读；`0` = 关闭 | 看 `[alert]` 行末尾打印的实测值与阈值；改 `.env.prod` 后**需重建容器**才进 env（或直接 `docker exec -e` 临时验） |
| 进度卡一直「执行中」/ 图表报告没出来 | 子任务终态写不进主线程（409 硬闸），老版本就此永久丢失；P2-4 后改为落库待补写 | 跑 `python -m agent.subagents.pending_terminal --status`：`pending`>0 = 还没补上（查 `/metrics` 的 run 队列与 `[alert] run_backlog`）；`pending`=0 = 不是这条路。**发版只 docker cp 不重启 ⇒ 补写器没起**（pending 只涨不掉）。见 §二.1.5 |
| 磁盘满了 / 数据卷快满 | 无保留策略（老版本），或清理器没起 | `python -m agent.utils.retention --status`：看 `disk.free_ratio` 与各 `areas` 谁最大 + `last_run.at` 是不是启动后的时间。**只 cp 不重启 = 清理器没起**。见 §二.1.6 |
| 清了数据但盘的数一点没降 | `DELETE` 只标空闲页、**文件不缩**（预期行为） | 看 `--status` 的 `removed`/`freed_bytes` 是不是真删了；要缩文件跑 `--vacuum <db>`（重写整库，故意不进自动轮） |
| 某个目标明明配了却没清 | 天数 `0`＝关闭；或该目标**默认就是 0**（`report`/`feedback`） | `--status` 的 `days` 里看是不是 0；env 名要写全 `NL2SQL_RETENTION_DAYS_<目标大写>`；改完**要重建容器**才进 env |
| `[alert] disk_low` 一直不触发 | 阈值/判据/卷搞错 | 阈值 `NL2SQL_ALERT_DISK_FREE_PCT`（**剩余**百分比，不是已用；`0`=关）；量的是 `/app/data` 那个卷；**连续 3 轮**才响。见 §二.1.6 |
| 启动日志里 `Unable to parse docstring for route …` + 一大段 yaml ScannerError | **无害噪声**：路由 docstring 以反引号开头，Starlette 生成 OpenAPI 时当 YAML 解析失败后退回纯文本 | 不用管、不用改：**langgraph 自带路由**（`/threads/{thread_id}/commands`、`/deploy/{operation_id}/stream`）也报同样的，本仓共 13 个路由各一次 |
| `/metrics` 里查不到 `nl2sql_alert_active` / `nl2sql_llm_calls_total` | **带标签的指标在第一个标签值出现前不会出现在 exposition 里** | 不是没配好：重启后没发过告警、没调过模型就没有这两行。发一次问答再查即可（`nl2sql_disk_*`/`nl2sql_run_queue_*` 是无标签或已赋值的，一直有） |
| 想回滚却发现没有可用的回滚镜像 tag | **手动发版不走 `release-backend.ps1`，不会自动打回滚 tag** | 发版前先手动打：`docker tag $(docker inspect -f '{{.Image}}' nl2sql-app_langgraph-api_1) nl2sql-api:rollback-$(date +%Y%m%d-%H%M)`；注意 tag 名是**打 tag 的时刻**、不等于镜像构建时间，核对用 `docker image inspect … -f '{{.Created}}'` |

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
