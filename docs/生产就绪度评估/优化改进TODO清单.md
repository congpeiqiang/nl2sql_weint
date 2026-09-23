# NL2SQL 多用户并发优化 —— TODO 清单

> 配套：[多用户并发生产就绪度评估报告.md](多用户并发生产就绪度评估报告.md)
> 生成日期：2026-09-23 ｜ 目标：生产环境支持多用户并发请求
> 用法：`[ ]` → 认领时改 `[~]` → 完成改 `[x]`。每项的「验收」列是可执行的判定，不要只看代码改完就勾。

## 0. 如果只做三件事

按「投入产出比」排序，先做这三项就能把「多人同时用会互相拖死」变成「慢但不会互相拖死」：

| 顺序 | 动作 | 工作量 | 收益 |
|---|---|---|---|
| **1** | **P0-1** 设 `BG_JOB_ISOLATED_LOOPS=true` | **10 分钟** | 消除「一个请求的阻塞调用拖死其他所有用户」——这是当前最大的单点 |
| **2** | **P0-2 + P0-3** 收回 2026 端口 + 关 nginx `/report/` | **15 分钟** | 堵掉两处免登录入口（一个可读全站会话，一个可下载全站报告） |
| **3** | **P0-5** 提交并发布会话归属隔离（当前未提交） | **半天** | 生产上「任何登录用户可读所有人会话」这个洞 |

> ⚠️ 前两项都要**重启容器**才生效，重启会打断当时所有在跑的会话（见 P2-1）。建议合并到一次维护窗口。

---

## P0 — 立即处理（小时级）

- [ ] **P0-1 设 `BG_JOB_ISOLATED_LOOPS=true`**
  - 位置：生产 compose 的 `langgraph-api` 环境变量
  - 现状：未设置 → 默认 `False` → 10 个 run 共用 1 个事件循环
  - 验收：启动日志出现隔离循环分支（不再是 `Starting queue with shared loop`）；并发 2 人跑长查询，A 的耗时不再随 B 的启动而跳变
  - 备注：与 **P1-14** 是互补关系，不是替代——隔离只保证各有各的 loop，同步阻塞仍会饿死自己那条

- [ ] **P0-2 收回 2026 端口**
  - 改法：`ports` 去掉，或改 `127.0.0.1:2026:2026`
  - 验收：从同网段另一台机器 `curl http://192.168.25.64:2026/ok` 不通；nginx 入口功能不受影响

- [ ] **P0-3 关闭 nginx `/report/` 的静态直出与 `autoindex`**
  - 位置：线上 `docker/nginx.conf` 的 `location /report/`（`alias /workspace/report/` + `autoindex on`）
  - 验收：未登录访问 `http://<host>:8080/report/` 不再列出目录、不再能取到文件
  - 备注：这层**完全绕过后端认证**，光改后端没用

- [ ] **P0-4 两处 bypass 判据换掉 + `internal` 不再短路归属校验 + dev 旁路硬编码关闭**
  - 判据换成不可由客户端伪造的凭据（共享密钥头 / Unix socket / 内部短期 token）
  - `internal` 身份给一个明确受限的授权集，而不是三个钩子全部 `return None`
  - `NL2SQL_AUTH_DISABLED=0` 写进生产 compose（防 `-e` 注入全站变 admin）
  - 验收：不带任何凭据直连 2026，`POST /threads/search` 返回 401 而非全量列表

- [ ] **P0-5 提交并发布会话归属隔离，并补齐三个洞**
  - 现状：`M src/agent/auth/backend.py`（+87/-1）、`?? src/agent/auth/ownership.py`，**均未提交，生产没有**
  - 必须同时补：
    - `update` 强制覆盖 `metadata["owner"]`（create 有，update 没有 → 客户端可把自己会话改成 `legacy` 全站可见，或改成他人的 user_id 注入他人侧边栏）
    - `admin` 语义统一（`on.threads` 放行 admin，`search` 不放行 —— 与文件自陈意图矛盾）
    - `POST /threads` + 已存在 `thread_id` + `if_exists` 分支查归属（现直接返回既有会话 = 泄露 + 存在性探测）
  - 验收：用户 A 登录后 search 只看到自己的；A 用 B 的 thread_id 做 read/delete/update 全部 404；A PATCH 自己会话设 `owner:"legacy"` 不生效
  - 备注：auth 模块启动时加载，**必须重启后端**

- [ ] **P0-6 给宿主机的 compose 补内存上限**
  - 现状：生产 `nl2sql-api` `Mem=0`（无上限），同机 40+ 容器、宿主 31GB
  - 验收：`docker inspect` 显示内存限额非 0；顺带修掉仓库 compose 里「服务器 3.4GB」的过时注释

- [ ] **P0-7 建立镜像回滚 tag**
  - 现状：发版脚本 `stop/rm -f/up`，旧容器删除后才验证，失败即 `throw`，无 tag 历史
  - 改法：发版前 `docker tag <当前> nl2sql-api:rollback-<日期>`
  - 验收：一次演练——tag 一个旧版本，走一次回滚，确认 5 分钟内能回到旧版本

---

## P1 — 并发正确性（天级）

- [ ] **P1-1 `trace_recorder._cached_thread_id` 改为请求级读取**
  - 现状：模块级单例上存实例属性，写入（模型调用）到读取（工具调用）窗口 = 整个 LLM 推理时长 → **并发下系统性错记**
  - 改法：与同文件 `_get_parent_thread_id()` 同法，用 `langgraph.config.get_config()`
  - 验收：并发跑 2 个会话各触发工具调用，`trace_events` 里两个 thread 的事件不交叉

- [ ] **P1-2 `configurable.db_name` / `user_id` 运行期授权校验**
  - 现状：工具裁剪只按客户端传的 `db_name`；`can_access_db`/`visible_dbs` 在 `src/agent/` 下无调用点；`thinking_toggle` 用客户端 `user_id` 加载**他人**模型配置（含 api_key）
  - 验收：伪造 `configurable.db_name=<未授权库>` 发起 run → 工具列表里没有该库的 wrenai 工具；伪造 `configurable.user_id=<他人>` → 不会加载他人模型凭据

- [ ] **P1-3 报告/图表落盘按用户或 thread 隔离 + 建立归属映射**
  - 现状：全站共享 `active_workspace/report/`，文件名 `{标题}_{秒级时间戳}`，无用户维度 → 同秒同名互相覆盖；chart-saver 更严重（**无时间戳，同名 100% 覆盖**）
  - 验收：两个用户同秒对同名主题出报告，两份都存在且各自可见

- [ ] **P1-4 报告端点补 `require_user`**
  - 现状：`get_report_file`（详情/下载/HEAD）**连 `require_user` 都没有**
  - 验收：未登录 `GET /api/reports/<存在的文件名>` 返回 401

- [ ] **P1-5 `owned_thread` 从 fail-open 改 fail-closed**
  - 现状：未登记会话一律放行 → 削弱了所有挂了 `require_thread` 的端点；且归属可被抢占（`claim_thread` 是 `INSERT OR IGNORE`）
  - 验收：未登记会话被拒绝，并给出明确的迁移/认领路径

- [ ] **P1-6 其余无校验端点补齐**
  - 清单：`thread_export`、`thread_run_status`、`message_feedback`、`feedback_stats`、`trace_routes`、`workspace`（含 `activate`）、`mcp/reload`、`db-configs/{name}/test`（工作区改动把它从 admin 降到了 user → SSRF 探活）、`git-ssh-key`
  - 验收：逐端点用「非 owner 已登录用户」打一遍，全部 401/403/404

- [ ] **P1-7 `auth/grants.py` 与 `auth/users.py` 补锁**
  - `grants`：模块级单连接 + `check_same_thread=False` + 无锁 + **每个请求都写它**（`register_user`）+ 每个 run 创建再写两次（`claim_thread`/`record_thread_db`）→ 并发写热点
  - `users`：`_users_cache` 无锁 RMW（会丢用户、会复活已删用户）+ `_save_users` 用**固定 tmp 名** `path.with_suffix(".tmp")` → 改 mkstemp
  - 验收：并发 20 个请求各建一个用户 + 并发注册 100 次，用户数与授权记录数完全正确

- [ ] **P1-8 全仓 SQLite 补 `PRAGMA busy_timeout`；`fts.sqlite` 开 WAL 且改按需更新；`eval_queue.claim` 改原子 CAS**
  - 现状：全仓无一处 `busy_timeout`；`fts.sqlite` 每次操作新建连接、每次检索**先写** + 跑 DDL → 并发搜索直接 5xx；`eval_queue.claim` 是 SELECT→UPDATE 非原子 → 同一 judge 任务跑两遍（重复烧 token）
  - 验收：并发 10 个搜索请求无 `database is locked`；两个进程同时 drain 队列，同一任务只被领一次

- [ ] **P1-9 `traces.sqlite` 收敛连接；`last_insert_rowid()` 移入锁内**
  - 现状：多实例各自 `_seq_counters` → 同 thread 事件 **seq 重复**（索引非 UNIQUE，不报错 → 前端排序错乱）；sync watcher 每个子任务新建连接且**不关** → fd 泄漏
  - 验收：并发写同一 thread，seq 严格单调无重复；长跑后 `lsof` 无连接堆积

- [ ] **P1-10 `_TOOL_EXECUTOR` 加上限保护 + 超时任务真取消**
  - 现状：全局 `ThreadPoolExecutor(max_workers=4)`，超时任务**不取消**（注释自陈「后台继续跑」）→ **4 个卡住的工具就能让所有用户的同步工具调用永久排队**
  - 验收：4 个工具同时超时后，第 5 个同步工具仍能执行（或明确返回「繁忙」而非无限等待）

- [ ] **P1-11 切工作区时失效所有进程级缓存**
  - 待失效：semantic detector、MCP 工具注册表、模型缓存、`_wrenai_display_cache`（**永不失效**）
  - 现状风险：A 租户切到 B 租户工作区后，**A 配的敏感库工具仍可调用**
  - 验收：切工作区后立即请求工具列表，只含新工作区的库

- [ ] **P1-12 凭据加固**
  - 默认管理员 `admin/admin123` 强制首登改密；密码哈希加盐（单轮无盐 SHA-256 → bcrypt）；token 加吊销（登录/改密/删用户即失效，现 24h 内继续有效）；Cookie 补 `Secure`（生产是明文 HTTP）
  - 验收：改密后旧 token 立即 401；首登必须改密

- [ ] **P1-13 加单测锁死「过滤器仅限 `$eq`/`$or`」+ 核对 legacy 存量**
  - 原因：inmem 对未知操作符**静默忽略 = 放行**；全仓无代码写 `legacy`，需确认它不是靠手工改库来的
  - 验收：测试里写一个 `$ne` 过滤器，断言它不会退化成「放行」

- [ ] **P1-14 把 §3.3 清单里的同步阻塞逐个包 `asyncio.to_thread`**
  - 优先级：**先做「每个请求都走」的两处中间件写库**（`auth_middleware.py:156 register_user`、`langfuse_metadata.py:182/205 claim_thread/record_thread_db`）与 `feedback_annotation.py:251`（人工标注预览 = 真查询）
  - 验收：语义库 build / 模型探活 / 全文检索期间，其他用户的 SSE 不中断

- [ ] **P1-15 统一 `thread_owner` 表与 `metadata.owner` 两套账本**

---

## P2 — 可靠性（周到双周）

- [ ] **P2-1 发版改滚动/优雅（drain）**
  - 现状：`stop`(默认 10s→SIGKILL) → `rm -f` → `up`，固定 `sleep 75` 后再验证；**无 drain、无自动回滚**，在跑 run 全断
  - 验收：一次发版期间已提交的 run 能跑完（或明确提示用户），而不是静默中断

- [ ] **P2-2 `/app/logs` 挂持久卷 + 打开访问日志 + 跨组件 request-id**
  - 现状：`access_log=False`（无每请求状态码/耗时）；日志写容器内、**没挂卷 → 重启即丢**；无统一 request-id
  - 验收：重启后仍能查到上一次运行期间的请求日志

- [ ] **P2-3 加 `/metrics` + 最小告警**
  - 建议指标：进程内存、**事件循环延迟**、队列深度（`n_running`/`n_pending`）、LLM 耗时/失败率、SQLite 锁等待、MCP 子进程数
  - 验收：并发压测时能在面板上看到队列深度与事件循环延迟

- [ ] **P2-4 修 `sync_subagent_todos` 的 300s 天花板导致的 `active_queries` 永久 true**

- [ ] **P2-5 各存储补 retention + 磁盘水位告警**
  - 无保留策略的：`trace_events`、`eval_queue`、`message_feedback.db`、`report/`、`large_tool_results/`（现只有 `trace_bind_store` 有 3000 上限）

- [ ] **P2-6 定时备份（含加密）**
  - 对象：`auth.sqlite`（授权与归属，**须加密**）、`db_config.json`/`model_config.json`、`pg_dump`
  - 现状：文档只给建议，**无自动任务**

- [ ] **P2-7 补一次真实并发压测**
  - 目标曲线：N 用户 → 队列深度 / P95 延迟 / 内存 / MCP 子进程数
  - 这一步同时校验 §1 的「3~4 并发用户」推导值
  - **这一步也是 P3-1 / P3-2 的决策依据，建议早排**

- [ ] **P2-8 目标库访问补三件：连接池 / statement timeout / 结果行数上限或分页**
  - 现状：每次 `run_sql` 新建连接、`finally: close()`、无 timeout、`fetchall()` **全量进内存无上限**
  - 风险：目标库 `max_connections` 会**先于本服务**报错；大表查询直接把结果拉进 1200m 容器

- [ ] **P2-9 `langfuse_span` 的 process_data dump 加上限或默认关**
  - 现状：每个工具调用一份 ≤8MB JSON、默认开启 → 单请求几十个文件

---

## P3 — 结构性（月级，需先有 P2-7 的数据）

- [ ] **P3-1 提高 `N_JOBS_PER_WORKER`（现 10）**
- [ ] **P3-2 多进程/多副本**（前置：搬走 inmem 运行时状态、MCP 改 HTTP/SSE、本地 SQLite 迁 Postgres、报告落共享存储）
- [ ] **P3-3 工作区请求级隔离**（ContextVar + configurable 透传，不再全局共享一个 active workspace）
- [ ] **P3-4 LLM 侧并发闸 + 队列 + 退避 + 按用户配额**
- [ ] **P3-5 发版流水线化**（有版本、可回滚、可灰度）
- [ ] **P3-6 MCP 从 stdio 改 HTTP/SSE + 常驻进程池**（消掉每次调用新建子进程 + 2-4s 冷启动）
- [ ] **P3-7 把共享 loop 上的 CPU 重活移出**（`check_progress.py:1158 plan_run_sql`、`wren_plan.py:222-241` 锁内建引擎）；wren 引擎缓存按库数扩容

---

## 附：不需要动的部分（避免误伤）

- `/api/feedback/annotations*`、`/api/feedback/datasets`、`/api/experiment/*`、`/api/auth/users*`、`/api/grants`、`/api/eval-flags` 都挂了 `require_admin` ✔
- `/api/db-configs` 的 `visible_dbs`/`can_access_db` 过滤 ✔
- 语义库那一面（`wren_semantic.py`）是全仓**最完整**的权限实现（读端点全挂项目级权限，对未关联库的项目 fail-closed）✔
- `/api/model-configs*` 有每用户独立 store ✔
- `mcp_tool.py:571` 等处的「起子进程必须放线程」约定 —— 这是 **P1-14 的整改模板**，照它抄 ✔
