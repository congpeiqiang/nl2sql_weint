# NL2SQL 应用部署信息（weint 环境）

> 服务器：**192.168.25.64**（Ubuntu 24.04，内网）
> 部署目录：`/home/weint/apps/nl2sql/nl2sql-app/`
> 部署时间：2026-08-27 | 容器化部署（docker-compose v1.29）

## 1. 访问入口

| 服务 | 地址 | 说明 |
|------|------|------|
| **前端 UI** | http://192.168.25.64:8080 | nginx 统一入口（80 被占，用 8080） |
| **后端 API** | http://192.168.25.64:2026 | LangGraph API（健康检查 `/ok`） |
| 健康检查 | http://192.168.25.64:8080/ok | nginx 代理后端 |
| **报告目录** | http://192.168.25.64:8080/report/ | 查询生成的图表/报告（HTML/MD，autoindex 目录浏览） |
| Checkpoint PG | 127.0.0.1:5435（仅本机） | nl2sql_checkpoint 库 |

## 1.1 报告访问方式（2026-08-28 新增）

- 查询生成的图表/报告保存在服务器 workspace 卷的 `report/` 目录，nginx 直接对外静态服务：
  - 目录浏览：http://192.168.25.64:8080/report/
  - 单文件：http://192.168.25.64:8080/report/<文件名>（中文文件名浏览器自动编码）
- 聊天里 agent 回显的路径是 `D:/workspace1/report/xxx.html`（开发机路径，服务器上不存在），
  把前缀替换为 `http://192.168.25.64:8080/report/` 即可访问。
- 实现：nginx 挂载 workspace 卷（只读 `workspace:/workspace:ro`），`location /report/` alias 到 `/workspace/report/`（autoindex on）。
- ⚠️ HTML 报告引用外部 CDN（jsdelivr/unpkg）的 echarts 脚本，浏览器打开时需服务器可达外网；离线环境图表会空白。

## 2. 架构（4 容器 + 全新卷）

```
nginx:8080 ── /api/*,/threads,/runs → langgraph-api:2026（内部）
      └──── /report/* → workspace 卷 report/（静态文件）
      └──── 其余 → frontend:3000（内部，宿主不发布）
langgraph-api:2026（宿主直连）
postgres（nl2sql_checkpoint，127.0.0.1:5435）
```

| 容器 | 镜像 | 端口 | 卷 |
|------|------|------|-----|
| nl2sql-app_nginx_1 | nginx:alpine | 8080→80 | docker/nginx.conf + workspace（只读，报告静态服务） |
| nl2sql-app_frontend_1 | node:20-alpine（产物化构建） | 内部 3000 | — |
| nl2sql-app_langgraph-api_1 | python:3.13-slim（多阶段） | 2026 | workspace |
| nl2sql-app_postgres_1 | postgres:14（复用已有镜像） | 127.0.0.1:5435 | nl2sql_pg_data |

## 3. 数据库

### Checkpoint（应用自有）
- 库/用户：`nl2sql_checkpoint` / `nl2sql`，密码：`nl2sql_secret`（postgres 容器实际密码；.env.prod 的 PG_PASSWORD 同值）
- 连接串：`postgresql://nl2sql:nl2sql_secret@192.168.25.64:5435/nl2sql_checkpoint`
- db_config.json 已预置 3 个业务库（aix_report / Chinook_AutoIncrement / imdb，指向 mysql-master 占位）——**实际业务库待通过前端 UI 配置**（DB_CONFIG_SECRET 加密存储）

### Langfuse（链路追踪）
- 已指向 weint Langfuse：http://192.168.25.64:3010，项目 proj_6nMyvFmN（pk/sk 见 .env.prod）
- ⚠️ Langfuse 中的 Prompt（main_system_prompt / nl2sql_system_prompt 等）尚未创建，当前回退本地提示词——**如需云端管理提示词，请在 Langfuse UI 创建对应 Prompt**（label=production）

## 4. 环境变量（.env.prod，chmod 600）

- LLM：DeepSeek（真实 key 已配置，来自本地 .env）
- LANGFUSE_*：指向 weint 实例
- CHART_ENGINE=semiotic（已禁用图表 MCP，见下）
- NL2SQL_CHART_MCP_ENABLED=false（图表 MCP 关闭）
- DB_CONFIG_SECRET：b40629feb5e52e157b9065cb0626be7f

## 5. 部署过程中的代码补丁（服务器副本，本地仓库未动）

| 文件 | 改动 | 原因 |
|------|------|------|
| `backend/src/agent/tools/mcp_tool.py` | 4 处 `**_UTF8_ENV` → `**os.environ, **_UTF8_ENV`（子进程 env 继承父进程） | MCP 子进程环境被剥离，dbmcp 读不到 DB_CONFIG_SECRET/DB_* 导致"未配置任何数据库" |
| `backend/src/agent/tools/mcp_tool.py` | npx 加 `-y` | 非交互环境 npx 确认提示阻塞 |
| `backend/src/agent/tools/mcp_tool.py` | 新增 `NL2SQL_CHART_MCP_ENABLED` 开关 | 按需禁用图表 MCP |
| `frontend/next.config.ts` | 最小明文版（原文件 DLP 加密，仅含构建期设置） | 服务器构建/运行需要明文配置 |
| 备份 | `mcp_tool.py.bak-20260827` | 原文件备份 |

## 6. 运维命令

```bash
cd /home/weint/apps/nl2sql/nl2sql-app
docker-compose ps                                   # 状态
docker-compose logs -f langgraph-api                # 后端日志
docker-compose logs -f frontend                     # 前端日志
docker-compose restart langgraph-api                # 重启后端
# 注意：docker-compose v1 重建容器（up -d 改配置）会报 ContainerConfig KeyError，
# 需用：stop → rm -f → up -d 三步
curl http://127.0.0.1:2026/ok                       # 健康检查
docker exec nl2sql-app_postgres_1 psql -U nl2sql -d nl2sql_checkpoint -c "\dt"
```

## 7. 已知事项（2026-08-28 更新）

- **图表 MCP 已启用（echarts）**：本地代码已移除 semiotic、仅保留 echarts；镜像内全局安装 `mcp-echarts@0.7.1`；当前 **20 个 MCP 工具就绪**（18 echarts + 2 dbmcp）
- **业务数据库未配置**：dbmcp 工具已加载，但查询前需在 UI 配置真实库（当前为 mysql-master 占位，不可达）
- Langfuse Prompt 未创建（回退本地）
- 后端环境变量在子进程继承的补丁（mcp_tool.py）如后续合并到上游代码，可移除服务器补丁
