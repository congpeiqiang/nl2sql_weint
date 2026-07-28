# 生产化改进 TODO 清单

> 基于 [生产就绪度评估报告](./生产就绪度评估报告.md)，按优先级排列。  
> 创建日期: 2026-07-28

---

## Phase 1 — 安全加固（目标: 1-2 周）

- [ ] **轮换所有泄露凭据**
  - DeepSeek API Key (`sk-97dc...`)
  - LangSmith API Key (`lsv2_pt_...`)
  - MySQL 密码 (`aoi8dev.1234`)
- [ ] **`.env` 加入 `.gitignore`**
  
  ```bash
  echo ".env" >> .gitignore
  git rm --cached .env
  ```
- [ ] **从 Git 历史清除 `.env`**
  
  ```bash
  # 使用 BFG Repo-Cleaner
  bfg --delete-files .env
  git reflog expire --expire=now --all && git gc --prune=now --aggressive
  ```
- [ ] **源码中硬编码密码替换为 `os.getenv()`**
  
  - `src/test/wrenai_exec_Chinook/gen_models_mysql.py:16`
  - `src/agent/skills/nl2sql/sql-of-thought/scripts/gen_models_mysql.py:16`
  - `src/agent/workspace/imdb_project/config/connection_mysql.json:8`
- [ ] **MCP 服务器绑定 127.0.0.1**
  
  - `.env`: `NL2SQL_MCP_HOST=127.0.0.1`
- [ ] **MCP 添加认证**
  
  - API Key 或 Bearer Token 中间件
- [ ] **SQL Runner 安全加固**
  
  - 数据库使用只读账户
  - 拦截 DROP / DELETE / ALTER / TRUNCATE / INSERT / UPDATE
  - 添加查询超时
  - 限制语句数量
- [ ] **删除备份目录**
  - `src/agent/skills备份/`
  - `src/agent/workspace/imdb_project备份/`

---

## Phase 2 — 可靠性提升（目标: 2-4 周）

- [ ] **硬编码路径替换**
  - `src/agent/main_agent.py:14` → `Path(__file__).parent`
  - `src/agent/tools/mcp_tool.py:48,51` → 环境变量 `WREN_BIN_PATH`, `WREN_PROJECT_PATH`
  - `src/agent/utils/path_resolver.py:10` → `Path(__file__).parent.parent / "workspace"`
  - `src/agent/checkpointer_factory.py:19` → 环境变量 `CHECKPOINT_DB_PATH`
  - `src/mcp_server/db_mcp_server/db/core/settings.py:57` → `Path(__file__).parents[4] / ".env"`
- [ ] **MySQL 连接池**
  - 引入 `DBUtils.PooledDB` 或 SQLAlchemy pool
  - 配置 `pool_size`, `max_overflow`, `pool_recycle`
- [ ] **LLM 超时 + 重试**
  - `ChatDeepSeek` 添加 `request_timeout=60`
  - 添加 `max_retries=3`
  - 使用 tenacity 装饰器实现指数退避
- [ ] **DB 查询超时**
  - `pymysql.connect()` 添加 `connect_timeout=10`
  - `cursor.execute()` 添加 `SET SESSION max_execution_time=30000`
- [ ] **MCP 工具调用超时**
  - `path_resolver.py` 中包裹 `asyncio.wait_for(timeout=120)`
- [ ] **健康检查端点**
  - 验证 MCP 服务器连通性
  - 验证 LLM API 可达性
  - 验证数据库连通性
- [ ] **就绪门控**
  - MCP 工具全部加载失败时，不启动 API 服务

---

## Phase 3 — 生产化（目标: 1-2 个月）

- [ ] **Checkpointer 切换 PostgreSQL**
  - 安装 `langgraph-checkpoint-postgres`
  - 更新 `checkpointer_factory.py` 使用 `AsyncPostgresSaver`
  - 更新 `graph.json` / `langgraph.json`
- [ ] **Docker 容器化**
  - 创建 `Dockerfile`（基于 `python:3.13-slim`）
  - 创建 `docker-compose.yml`（API + MCP + PostgreSQL）
- [ ] **CI/CD 流水线**
  - `.github/workflows/ci.yml`: lint → test → build
  - `.github/workflows/deploy.yml`: 构建镜像 → 推送 → 部署
- [ ] **自动化测试**
  - `tests/test_sql_pipeline.py`: SQL-of-Thought 流水线
  - `tests/test_mcp_tools.py`: MCP 工具调用
  - `tests/test_error_handling.py`: 纠错流程
  - `tests/test_security.py`: SQL 注入防护
  - pytest 配置 + 覆盖率报告
- [ ] **依赖版本锁定**
  - `pyproject.toml` 添加上界: `>=0.6.12,<1.0`
  - 生成 `uv.lock`
- [ ] **结构化日志**
  - 替换 `mcp_tool.py` 和 `start_server.py` 中的 `print()` 为 `logging`
  - `path_resolver.py:30` 降低日志级别，移除结果预览
  - SQL 日志脱敏
- [ ] **并发安全**
  - `_tools` / `_tools_loaded` 添加 `asyncio.Lock`
  - `_runner_cache` 添加锁保护
  - 进度文件写入添加文件锁
- [ ] **取消机制**
  - SQL 查询支持 `CANCEL` 信号
  - LLM 调用支持 `asyncio.CancelledError`
  - 沙箱命令支持优雅终止

---

## 验收标准

| 阶段 | 验收条件 |
|------|---------|
| Phase 1 | Git 历史无密钥泄露；SQL Runner 拦截 DROP/DELETE；MCP 不可外部匿名访问 |
| Phase 2 | 任何单点故障不导致服务崩溃；健康检查反映真实状态；所有路径可配置 |
| Phase 3 | CI 自动运行测试；Docker 一键部署；测试覆盖率 ≥ 60%；可水平扩展 |
