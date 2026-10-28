# NL2SQL 平台 API 文档（总览）

> 代码位置：`src/api/`（25 个路由模块 + 组合根 `custom_app.py` + 共享工具 `_common.py`
> + 3 个纯 ASGI 中间件 `auth_middleware.py` / `request_log.py` / `langfuse_metadata.py`），
> 由 `src/api/custom_app.py` 的 `ROUTES` 聚合，经 `LANGGRAPH_HTTP.app`
> 钩子与 LangGraph 原生路由**合并进同一 Starlette app、同一进程/端口（2026）**。
> 整理日期：2026-10-28 ｜ 共 **30 个模块 / 110 个自定义路由**

## 一、阅读顺序

| 文档 | 覆盖模块 | 路由数 |
|---|---|---|
| [01-认证与配置.md](01-认证与配置.md) | auth_routes / auth_admin / db_config / model_config / deployment_info / eval_flags | 31 |
| [02-会话与查询交互.md](02-会话与查询交互.md) | auto_title / thread_* / task_cancel / sql_approval / report_file | 11 |
| [03-定时任务.md](03-定时任务.md) | scheduled_tasks（含固化回放 pin） | 10 |
| [04-反馈标注与数据集.md](04-反馈标注与数据集.md) | message_feedback / feedback_stats / feedback_annotation | 19 |
| [05-离线实验.md](05-离线实验.md) | experiment | 9 |
| [06-语义库管理.md](06-语义库管理.md) | wren_semantic | 21 |
| [07-运维与可观测.md](07-运维与可观测.md) | metrics / drain / trace_routes + 三个 ASGI 中间件 | 9 |

## 二、通用约定

### 2.1 鉴权

- **凭证**：Cookie `nl2sql_token`（HttpOnly / SameSite=Lax / 24h）或 `Authorization` 头；
  `AuthMiddleware` 校验通过后把用户写进 `scope["state"]["user"]`。
- **身份字段**：`{user_id, display_name, is_admin, must_change_password, token_version}`。
- **四档要求**：

  | 档位 | 实现 | 失败响应 |
  |---|---|---|
  | 登录 | `require_user(request)` | `401 {"detail":"未登录"}`（经 langgraph 处理器包装） |
  | 管理员 | `require_admin(request)` / `admin_guard(request, action)` | 403 |
  | 会话归属 | `require_thread(request, tid)` | 403 |
  | 库授权 | `can_access_db(user, db)` | 403 |

- **白名单（免鉴权）**：`/api/auth/login`、`/api/auth/logout`、`/ok`、`/_next/`、`/ui`、
  `/docs`、`/openapi.json`、`/redoc`。
- **内部请求**：来源 IP 属 Docker 网段（`172.` / `10.` / `127.0.0.1` / `::1`）**且**无 `Cookie`
  **且**无 `X-Forwarded-For` → 标记 `user_id="internal"`（子 agent / sync 循环自调用走这条）。
- **开发旁路**：`NL2SQL_AUTH_DISABLED=1` → 固定 `dev` 管理员身份。
- **强制改密**：`NL2SQL_FORCE_PASSWORD_CHANGE=1` 时，`must_change_password` 账号除
  `/api/auth/me`、`/api/auth/change-password`、`/api/auth/logout` 外一律 403（默认关闭）。

### 2.2 响应与错误

- **成功**：多数为 `{"ok": true, ...}`，或直接返回资源对象（如配置详情）。
- **错误**：自定义端点统一返回 **`{"error": "中文可读原因"}`**。

  > ⚠️ 这是硬约定：本 app 的路由并进 langgraph 主 app，**抛 `HTTPException` 会被折成
  > `{"detail": ...}`**，而前端只认 `.error` 键 —— 所以业务错误一律「返回」而非「抛」。
  > （例外：`require_user` / `require_admin` / `require_thread` 抛的 401/403 由上层包装。）

- **请求追踪**：所有响应回显 `X-Request-ID`（入站合法则透传，否则生成 12 位 hex）；
  访问日志格式 `[access] rid=... method path -> status latency=..ms total=..ms ip= user= [tid=]`。
- **排空期**：`POST /api/admin/drain` 之后，**新建 run 的 7 条路径**返回
  `503 {"error":"服务正在停机排空…","draining":true,"remaining_tasks":N}` + `Retry-After: 30`；
  取消/查询/会话操作不受影响。

### 2.3 文档标注约定

本套文档的**每个端点**都固定标注四行：

- **功能**：该接口对外承诺什么（一句话）。
- **实现逻辑**：服务端实际怎么做——handler → 服务层函数 → 读写/副作用的调用链。
- **入参**：位置（path / query / body）+ 字段 + 类型 + 必填 + 说明；无参数写「无」。
- **出参**：成功响应的 JSON 形状（或文件流 / 状态码说明）。

失败响应统一记在 **错误** 行（状态码 + 实际 `error` 文案）。少数非端点小节（如固化回放机制、
排空期 503 形状、中间件说明、lifespan 顺序）已显式标注「无入参/出参」。

### 2.4 领域约定

- **数据库授权**：非管理员只能看到 `visible_dbs(user)` 里的库；越权访问按 403 / 404 处理。
- **会话归属**：thread 只能本人或管理员访问；他人会话统一按 403（部分端点按 404 隐去存在性）。
- **分页上限**：`threads/fts` limit ≤ 50；`traces/*` limit ≤ 500；
  `scheduled-tasks/{id}/runs` limit ≤ 200；`feedback/stats` days ≤ 365。
- **同步阻塞纪律**：SQLite / 文件遍历 / git 子进程 / 同步 HTTP 一律经
  `offload()` / `offload_long()` 进线程池，不占事件循环。

## 三、全量路由索引

> 鉴权列：**公开**=白名单；**登录**=任意登录用户；**管理员**=is_admin；
> **归属**=仅会话本人/管理员；**库权**=需该库授权。

### 3.1 认证与用户

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | 登录，签发 Cookie |
| POST | `/api/auth/logout` | 公开 | 清除 Cookie（不吊销 token） |
| GET | `/api/auth/me` | 登录 | 当前用户信息 |
| POST | `/api/auth/change-password` | 登录 | 自助改密并换发 Cookie |
| GET | `/api/auth/users/auth-list` | 管理员 | auth_users.json 用户清单 |
| POST | `/api/auth/users` | 管理员 | 新增用户 |
| PUT | `/api/auth/users/{uid}` | 管理员 | 修改用户 |
| DELETE | `/api/auth/users/{uid}` | 管理员 | 删除用户 |
| POST | `/api/auth/users/{uid}/revoke` | 管理员 | 吊销该账号全部 token |
| POST | `/api/auth/users/reload` | 管理员 | 重载 auth_users.json |
| GET | `/api/auth/users` | 管理员 | grants 表用户清单 |
| GET | `/api/auth/users/{uid}/grants` | 管理员 | 某用户库授权 |
| POST | `/api/auth/users/{uid}/grants` | 管理员 | 授权 |
| DELETE | `/api/auth/users/{uid}/grants/{db}` | 管理员 | 撤销授权 |

### 3.2 配置类

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| GET | `/api/db-configs` | 登录+库权 | 数据库连接列表（脱敏） |
| POST | `/api/db-configs` | 管理员 | 新增/更新连接（含 MCP 热加载） |
| GET | `/api/db-configs/{name}` | 登录+库权 | 单条连接（脱敏） |
| DELETE | `/api/db-configs/{name}` | 管理员 | 删除连接（工具下线） |
| POST | `/api/db-configs/{name}/test` | 管理员 | 连通性测试 |
| GET | `/api/mcp/status` | 管理员 | MCP 运行期注册表状态 |
| POST | `/api/mcp/reload` | 管理员 | 全量对账 MCP 工具 |
| GET | `/healthz` | 登录/内网 | 存活检查（handler 不校验；但不在白名单 ⇒ 外部需登录、容器内网直通） |
| GET | `/api/model-configs` | 登录 | 模型 provider 列表 |
| POST | `/api/model-configs` | 登录 | 新增/更新 provider |
| GET | `/api/model-configs/{name}` | 登录 | provider 详情 |
| DELETE | `/api/model-configs/{name}` | 登录 | 删除 provider |
| POST | `/api/model-configs/{name}/activate` | 登录 | 设为激活 |
| POST | `/api/model-configs/test` | 登录 | 探活 + 拉模型列表 |
| POST | `/api/model-configs/probe-capabilities` | 登录 | 探测上下文/输出容量 |
| GET | `/api/deployment-info` | 登录 | 默认助手 ID + 图名 |
| GET/PUT/DELETE | `/api/eval-flags` | 管理员 | 在线评估开关 |

### 3.3 会话

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| GET | `/api/threads/fts` | 登录 | 会话全文搜索 |
| DELETE | `/api/threads/{thread_id}` | 归属 | 删会话（先取消在飞 run） |
| GET | `/api/threads/{thread_id}/run-status` | 归属 | 轮次状态（中断/失败判定） |
| POST | `/api/threads/{thread_id}/compact` | 归属 | 手动压缩上下文 |
| GET | `/api/threads/{thread_id}/export` | 归属 | 导出 Markdown/JSON |
| POST | `/api/threads/{thread_id}/fork` | 归属 | 会话分叉 |
| POST | `/api/threads/{task_id}/cancel` | 归属 | 取消异步子任务（校验 body 里的 `main_thread_id`） |
| POST | `/api/threads/{thread_id}/sql-approval` | 归属/库权 | SQL 审批恢复 |
| POST | `/api/auto-title` | 登录 | 生成会话标题 |
| GET | `/api/reports` | 登录 | 报告文件列表 |
| GET/HEAD | `/api/reports/{filename}` | 登录+归属 | 报告预览/下载/存在性 |

### 3.4 定时任务

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| GET | `/api/scheduled-tasks` | 登录 | 任务列表（`?all=1` 管理员看全部） |
| POST | `/api/scheduled-tasks` | 登录 | 新建任务 |
| POST | `/api/scheduled-tasks/preview` | 登录 | 触发时间预览 |
| PATCH | `/api/scheduled-tasks/{id}` | 登录 | 改任务 / pause / resume |
| DELETE | `/api/scheduled-tasks/{id}` | 登录 | 删任务 |
| POST | `/api/scheduled-tasks/{id}/run` | 登录 | 立即运行一次 |
| GET | `/api/scheduled-tasks/{id}/runs` | 登录 | 运行历史 |
| GET | `/api/scheduled-tasks/{id}/pin` | 登录 | 固化状态 |
| POST | `/api/scheduled-tasks/{id}/pin` | 登录+库权 | 固化查询 |
| DELETE | `/api/scheduled-tasks/{id}/pin` | 登录 | 解绑固化 |

### 3.5 反馈与标注

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| PUT | `/api/threads/{tid}/messages/{mid}/feedback` | 归属 | 新建/更新反馈（CAS） |
| DELETE | `/api/threads/{tid}/messages/{mid}/feedback` | 归属 | 撤销反馈 |
| GET | `/api/threads/{tid}/feedback` | 归属 | 会话内反馈列表 |
| GET | `/api/feedback/export` | 管理员 | 全量导出 |
| GET | `/api/feedback/stats` | 管理员 | 反馈看板 |
| GET | `/api/feedback/annotations` | 管理员 | 待标注队列 |
| GET | `/api/feedback/annotations/bad-types` | 管理员 | 错误类型枚举 |
| GET | `/api/feedback/datasets` | 管理员 | 数据集条目 |
| GET | `/api/feedback/dataset-stats` | 管理员 | 数据集统计 |
| POST | `/api/feedback/annotations/clear` | 管理员 | 清空某状态队列 |
| GET | `/api/feedback/annotations/{tid}/{mid}` | 管理员 | 标注详情 |
| DELETE | `/api/feedback/annotations/{tid}/{mid}` | 管理员 | 删除队列条目 |
| POST | `…/{tid}/{mid}/judge` | 管理员 | 判定是否有效 |
| POST | `…/{tid}/{mid}/execute` | 管理员 | 执行校验 SQL |
| POST | `…/{tid}/{mid}/preview-cube` | 管理员 | Cube 口径试算 |
| POST | `…/{tid}/{mid}/confirm` | 管理员 | 确认 BadCase |
| POST | `…/{tid}/{mid}/confirm-good` | 管理员 | 确认入 Good Set |
| POST | `…/{tid}/{mid}/revoke-good` | 管理员 | 撤回入集 |
| POST | `/api/feedback/annotations/revoke-badcase` | 管理员 | 移出 BadCase |

### 3.6 离线实验 / 语义库 / 运维

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| GET | `/api/experiment/datasets` | 管理员 | 数据集及条数 |
| GET | `/api/experiment/prompt-labels` | 管理员 | prompt 可用 label |
| GET | `/api/experiment/skill-refs` | 管理员 | skill 仓库 refs |
| GET | `/api/experiment/semantic-refs` | 管理员 | 语义库 refs |
| POST | `/api/experiment/runs` | 管理员 | 提交实验 |
| GET | `/api/experiment/runs` | 管理员 | 历史 run |
| GET | `/api/experiment/runs/{stamp}` | 管理员 | run 状态/结果 |
| DELETE | `/api/experiment/runs/{stamp}` | 管理员 | 删除 run |
| POST | `/api/experiment/runs/{stamp}/cancel` | 管理员 | 停止 run |
| GET | `/api/wren-projects` | 登录 | 语义库列表 |
| POST | `/api/wren-projects/create` | 管理员 | 新建语义库 |
| POST | `/api/wren-projects/local` | 管理员 | 关联本地目录 |
| POST | `/api/wren-projects/from-git` | 管理员 | 从 Git 新建 |
| DELETE | `/api/wren-projects/{name}` | 管理员 | 删除语义库 |
| POST | `/api/wren-projects/{name}/introspect` | 管理员 | 内省表结构 |
| POST | `/api/wren-projects/{name}/generate-models` | 管理员 | 生成 models/yml |
| GET | `/api/wren-projects/{name}/knowledge/template` | 登录+归属 | 知识模板 |
| POST | `/api/wren-projects/{name}/knowledge/save` | 管理员 | 保存知识 |
| POST | `/api/wren-projects/{name}/open-directory` | 登录+归属 | 打开项目目录 |
| POST | `/api/wren-projects/{name}/push` | 管理员 | 推送 Git |
| POST | `/api/wren-projects/{name}/build` | 管理员 | 构建 |
| POST | `/api/wren-projects/{name}/validate` | 登录+归属 | 校验 |
| GET | `/api/wren-projects/{name}/summary` | 登录+归属 | 概览 |
| GET | `/api/wren-projects/{name}/knowledge/read` | 登录+归属 | 读知识 |
| POST | `/api/wren-projects/{name}/knowledge/ai-generate` | 管理员 | AI 生成知识草稿 |
| GET | `/api/wren-projects/{name}/git-status` | 登录+归属 | Git 状态 |
| GET | `/api/wren-projects/{name}/git-refs` | 登录+归属 | 远程 refs |
| POST | `/api/wren-projects/{name}/git-pull` | 管理员 | 更新到指定 ref |
| POST | `/api/wren-projects/{name}/git-adopt` | 管理员 | 接管远程仓库 |
| GET | `/api/git-ssh-key` | 登录 | 后端 SSH 公钥 |
| GET/HEAD | `/metrics` | 仅容器内网 | Prometheus 指标 |
| POST/GET/DELETE | `/api/admin/drain` | 管理员 | 排空：进入/查询/撤销 |
| GET | `/api/traces/threads/{tid}/events` | 归属 | trace 事件列表 |
| GET | `/api/traces/threads/{tid}/lineage` | 归属 | 会话谱系 |
| GET | `/api/traces/threads/{tid}/stats` | 归属 | 会话统计 |
| GET | `/api/traces/threads/{tid}/llm-calls` | 归属 | LLM 调用清单 |
| GET | `/api/traces/tasks/{task_id}/events` | 管理员 | 子任务事件 |

## 四、不属本套文档的原生路由

LangGraph 原生端点（`/threads*`、`/runs*`、`/assistants*`、`/store*`、`/ok`、`/ui`、`/docs`）
由 `langgraph_api` 提供，本文档不重复；但下列自定义中间件会影响它们的行为：

| 中间件 | 作用范围 |
|---|---|
| `AuthMiddleware` | 全部 http/websocket（除白名单） |
| `RequestContextMiddleware` | 全部 http（rid + access 日志） |
| `DrainGateMiddleware` | 仅 `POST /runs`、`/runs/stream|wait|batch`、`POST /threads/{tid}/runs[/stream|batch]` |
| `LangfuseMetadataMiddleware` | 仅上述 run 创建路径：注入 Langfuse metadata + **钳制 configurable**（外部请求以登录身份覆盖 `user_id`；`db_name` 未授权 → 403） |
