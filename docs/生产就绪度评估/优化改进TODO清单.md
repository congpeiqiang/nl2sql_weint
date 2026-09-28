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
| **3** | **P0-5** 提交并发布会话归属隔离 | **✅ 已完结** | 已于 09-23 发版（commit `f5cd2fc`，E2E 18/18）。⚠️ 但当时有两个**未关的旁路门**（见 §P0-4 与「另外两条门」） |

> ⚠️ 前两项都要**重启容器**才生效，重启会打断当时所有在跑的会话（见 P2-1）。建议合并到一次维护窗口。
>
> **2026-09-23 执行进展**：P0-1/2/3/4/6 的改动已全部落盘（代码 + 镜像 compose + nginx.conf + 发版脚本），
> **总计只需一次后端重启 + 一次 nginx 容器重建**。P0-5 已完结。P0-7 已完结（离线完成，无需重启）。
> 见本文件末尾「执行记录」。

---

## P0 — 立即处理（小时级）

- [~] **P0-1 设 `BG_JOB_ISOLATED_LOOPS=true`** —— ✅ 已写入镜像 compose，**待重启生效**
  - 位置：生产 compose 的 `langgraph-api` 环境变量
  - 现状：未设置 → 默认 `False` → 10 个 run 共用 1 个事件循环
  - 验收：启动日志出现隔离循环分支（不再是 `Starting queue with shared loop`）；并发 2 人跑长查询，A 的耗时不再随 B 的启动而跳变
  - 备注：与 **P1-14** 是互补关系，不是替代——隔离只保证各有各的 loop，同步阻塞仍会饿死自己那条

- [~] **P0-2 收回 2026 端口** —— ✅ 已改为 `127.0.0.1:2026:2026`，**待重启生效**（已核对发版脚本自检走 `127.0.0.1`，不受影响）
  - 改法：`ports` 去掉，或改 `127.0.0.1:2026:2026`
  - 验收：从同网段另一台机器 `curl http://192.168.25.64:2026/ok` 不通；nginx 入口功能不受影响

- [~] **P0-3 关闭 nginx `/report/` 的静态直出与 `autoindex`** —— ✅ **已确认零消费者，整段删除**（比原计划更彻底），**待部署 nginx**
  - 位置：线上 `docker/nginx.conf` 的 `location /report/`（`alias /workspace/report/` + `autoindex on`）
  - 验收：未登录访问 `http://<host>:8080/report/` 不再列出目录、不再能取到文件
  - 备注：这层**完全绕过后端认证**，光改后端没用

- [~] **P0-4 两处 bypass 判据换掉 + `internal` 不再短路归属校验 + dev 旁路硬编码关闭** —— ✅ 判据已换（16/16 + 反向对照）、✅ dev 旁路已硬编码 `"0"`；⚠️「`internal` 不再短路」**判定为不该改**（理由见执行记录）；共享密钥列为后续
  - 判据换成不可由客户端伪造的凭据（共享密钥头 / Unix socket / 内部短期 token）
  - `internal` 身份给一个明确受限的授权集，而不是三个钩子全部 `return None`
  - `NL2SQL_AUTH_DISABLED=0` 写进生产 compose（防 `-e` 注入全站变 admin）
  - 验收：不带任何凭据直连 2026，`POST /threads/search` 返回 401 而非全量列表

- [x] **P0-5 提交并发布会话归属隔离，并补齐三个洞** —— ✅ 已发版（`f5cd2fc`，E2E 18/18）。三个洞：`update` 强制覆盖 owner ✅ 已补；admin 语义 ✅ 09-23 拍板「有意如此」（列表隔离、单条直读放行）；`if_exists` 分支 ⚠️ 未复核
  - 现状：`M src/agent/auth/backend.py`（+87/-1）、`?? src/agent/auth/ownership.py`，**均未提交，生产没有**
  - 必须同时补：
    - `update` 强制覆盖 `metadata["owner"]`（create 有，update 没有 → 客户端可把自己会话改成 `legacy` 全站可见，或改成他人的 user_id 注入他人侧边栏）
    - `admin` 语义统一（`on.threads` 放行 admin，`search` 不放行 —— 与文件自陈意图矛盾）
    - `POST /threads` + 已存在 `thread_id` + `if_exists` 分支查归属（现直接返回既有会话 = 泄露 + 存在性探测）
  - 验收：用户 A 登录后 search 只看到自己的；A 用 B 的 thread_id 做 read/delete/update 全部 404；A PATCH 自己会话设 `owner:"legacy"` 不生效
  - 备注：auth 模块启动时加载，**必须重启后端**

- [~] **P0-6 给宿主机的 compose 补内存上限** —— ✅ 已写 `mem_limit: 8g`（兜底值，非容量规划），**待重启生效**
  - 现状：生产 `nl2sql-api` `Mem=0`（无上限），同机 40+ 容器、宿主 31GB
  - 验收：`docker inspect` 显示内存限额非 0；顺带修掉仓库 compose 里「服务器 3.4GB」的过时注释

- [x] **P0-7 建立镜像回滚 tag** —— ✅ 发版脚本已在**重建前**打 `nl2sql-api:rollback-<时间戳>`；新增 `rollback-backend.ps1`（列出 tag / 一键回滚）。离线完成，无需重启
  - 现状：发版脚本 `stop/rm -f/up`，旧容器删除后才验证，失败即 `throw`，无 tag 历史
  - 改法：发版前 `docker tag <当前> nl2sql-api:rollback-<日期>`
  - 验收：一次演练——tag 一个旧版本，走一次回滚，确认 5 分钟内能回到旧版本

---

## P1 — 并发正确性（天级）

- [x] **P1-1 `trace_recorder._cached_thread_id` 改为请求级读取** —— ✅ 2026-09-23 已实施（`scripts/verify_trace_thread_scope.py` 8/8；负对照在旧代码下复现错归因：B 拿到 A 的 2 个工具事件）
  - 现状：模块级单例上存实例属性，写入（模型调用）到读取（工具调用）窗口 = 整个 LLM 推理时长 → **并发下系统性错记**
  - 改法：与同文件 `_get_parent_thread_id()` 同法，用 `langgraph.config.get_config()`
  - 验收：并发跑 2 个会话各触发工具调用，`trace_events` 里两个 thread 的事件不交叉

- [x] **P1-2 `configurable.db_name` / `user_id` 运行期授权校验** —— ✅ 2026-09-23 已实施（`scripts/verify_config_authz.py` 32/32，负对照 3/3 检出）
  - 现状：工具裁剪只按客户端传的 `db_name`；`can_access_db`/`visible_dbs` 在 `src/agent/` 下无调用点；`thinking_toggle` 用客户端 `user_id` 加载**他人**模型配置（含 api_key）
  - 验收：伪造 `configurable.db_name=<未授权库>` 发起 run → 工具列表里没有该库的 wrenai 工具；伪造 `configurable.user_id=<他人>` → 不会加载他人模型凭据
  - **实施**：钳制点选在 `LangfuseMetadataMiddleware`（唯一同时看得见 AuthMiddleware 的权威身份 `scope["state"]["user"]` 与客户端 `configurable` 的收口）。外部真实用户 → ① `user_id`/`langgraph_auth_user_id` 强制改写为登录身份、`langgraph_auth_user` 删除；② `thread_id`/`checkpoint_id`/`checkpoint_ns` 丢弃（服务端按 URL 写入的运行状态，客户端传值只能是伪造）；③ `db_name` 走 `can_access_db`，**不通过就 403 拒掉整个 run**。内部调用（子 agent / sync，身份 `internal`）与 dev 旁路不钳制——它们的 configurable 是服务端自己镜像的
  - **三处关键取舍（都不显然）**：
    1. **拒绝而不是清空**：`tool_filter._filter_tools` 里 `if not db_name: return tools` —— 空库名 = 不裁剪 = 把**所有**库的 wrenai 工具绑给模型，比越权更糟。所以未授权库只能 403，不能「清空了事」
    2. **不挂在 `LANGFUSE_ENABLE` 上**：原 `__call__` 在监控开关关闭时**不读 body**（安全属性会随开关一起消失）。已重构为「先钳制（安全，出错即拒）→ 再注入（监控，失败不影响主流程）」
    3. **补拦 stateless 端点**：nginx 把 `^/(threads|runs|assistants|store|ok|docs|openapi)` 全量转发，外部可直接 `POST /runs/stream` 自建临时会话绕开只拦 `/threads/{tid}/runs*` 的正则。`/runs/batch` 载荷是 list，故钳制按逐个 dict 项做
  - **顺带修掉的既有缺陷**：`_inject` 返回原对象（`new_body is body`）时中间件转发的是**已被读空**的 `receive` 通道（下游拿到空 body）。现在无论是否改动都回放 body；非 JSON 体原样回放
  - **配套堵的绕过口**：未选库（`db_name` 为空）这条 — 前端 `selectedDb` 默认空串、localStorage 没选过就是空，越权者只要**省略** db_name 就能拿到全部库的语义层工具。`tool_filter` 新增 `_filter_unselected`：按调用者可见库裁掉无权 wrenai 工具（内部/dev 不裁，读授权失败 fail-open）
  - **已知残留（另立 P1-16）**：`dbmcp_run_sql` 把目标库当**工具参数**传，未选库时 dbmcp 仍 fail-open 保留 → 该通道不受本次库授权约束

- [x] **P1-3 报告/图表落盘按用户或 thread 隔离 + 建立归属映射** —— ✅ 2026-09-23 已实施（`scripts/verify_report_ownership.py` 40/40；对抗性负对照：拆掉 `_may_read` 守卫后 7 条断言立刻失败、而"放行"类断言仍通过 ⇒ 不是恒真断言也**不是一刀切 404**）
  - 现状：全站共享 `active_workspace/report/`，文件名 `{标题}_{秒级时间戳}`，无用户维度 → 同秒同名互相覆盖；chart-saver 更严重（**无时间戳，同名 100% 覆盖**）
  - **落盘路径三条全改（同秒同名各存各的）**：
    1. `report_builder.build_report`：`{标题}_{ts}_{thread前8位}.md` + 存在性 `_{n}` 兜底（不同会话 tag 不同 ⇒ 用户维度天然隔离；同一会话重复出报告由 `_{n}` 兜住）
    2. `path_resolver`（generate_echarts **自动落盘**的 .html/.svg/.png）：新增 `_reserve_report_file()` = 时间戳 + 4 位随机 + **`O_EXCL` 独占创建**（check-then-act 在并发下必然有窗口，光查 `exists()` 不够）
    3. `chart-saver/scripts/save_chart.py`：同上（新增 `_reserve_dest()`），并同步改 SKILL.md 的命名规则表与"相对路径引用"示例——**原先教模型用基名拼路径**，加了后缀后那么写必然是死链
  - **归属账本**：`auth.grants.report_owner(filename PK, user_id, thread_id, created_at)` + `record_report_owner`（INSERT OR IGNORE，**先到者胜**——改写归属 = 后来者能把别人的报告认领成自己的）/ `can_read_report`
  - **强制读侧**：`GET`/`HEAD`/`?download=1`/`GET /api/reports`（列表）四处同口径。**列表必须一起管**：目录列表本身就是文件名的来源，只挡单文件下载等于没挡
  - **越权响应 = 404 而非 403**：与"文件不存在"**同状态码同文案**，否则 403 等于告诉越权者"这份报告确实存在，只是不是你的"
  - **无记录放行**：存量文件与内部/dev 产物走放行口径（与 `owned_thread` 一致）——不能因为加了账本让人打不开自己的历史报告；代价是**未登记文件对任何登录用户可读**（见残留）
  - **图表归属靠 `configurable`**：`path_resolver._record_chart_owner()` 读 `configurable.user_id` 登记。这里能成立的前提是 P1-2 已把该值钳制为登录身份（客户端伪造会被覆盖）——**两条改动是耦合的**，P1-2 若回退，这条账本就不可信
  - 验收：两个用户同秒对同名主题出报告/出图，两份都存在；跨用户读 404，本人读 200
  - **已知残留**：
    - chart-saver **skill 进程**（`execute` 调 `save_chart.py`）拿不到请求身份（shell backend 的 env 是 import 时快照）→ 这类 .svg/.png **无归属**，任何登录用户知道文件名即可读。**已补**：结果侧中间件读脚本 stdout 的 `✅ 图表已保存: <路径>` 登记归属（**P1-17**）
    - 生产 `<AGENT_DATA_ROOT>/shared/skills/` 是**外置持久目录**，不随发版 tar 更新 → `save_chart.py`/SKILL.md 的改动**必须单独同步到生产 data_root**（与 `model_config.json` 同一类坑）
    - 仓库里另有一份**过期副本** `src/agent/skills/main/chart-saver/scripts/save_chart.py`（写死 `<项目根>/workspace/report`，与运行期无关）——别改错文件，运行期用的是 `<data_root>/shared/skills/`（仓库种子在 `src/agent/shared/skills/`）

- [x] **P1-4 报告端点补 `require_user`** —— ✅ 2026-09-23 已实施（含 HEAD；`scripts/verify_endpoint_guards.py` 覆盖）
  - 现状：`get_report_file`（详情/下载/HEAD）**连 `require_user` 都没有**
  - 验收：未登录 `GET /api/reports/<存在的文件名>` 返回 401

- [x] **P1-5 `owned_thread` 从 fail-open 改 fail-closed** —— ✅ 2026-09-23 已实施（`scripts/verify_thread_ownership.py` 29/29；负对照：旧口径对同一请求放行、新口径 403，翻转可检）
  - 现状：未登记会话一律放行 → 削弱了所有挂了 `require_thread` 的端点
  - **为什么这条不能只改一行**：登记是**惰性**的（只在建 run 时写），所以"未登记"是**常态而非异常** —— 直接翻转会把两类合法访问打成 403：
    1. **建了但还没跑过 run** 的会话（`POST /threads` 原先只写 `metadata.owner`，grants 行要等第一个 run）
    2. **09-22 之前**建的老会话（当时还没有这段代码）
    所以「翻转」必须与「堵写侧缺口 + 回填存量」同时发生，三件事一件都不能少：
  - **缺口 1（向前）**：`@auth.on.threads.create` 钩子在写 `metadata.owner` 的同一处 `claim_thread()` 写 grants 行 —— 两套账本**同一个入口一起写**，否则迟早出现「侧边栏看得见、点进去 403」
  - **缺口 2（存量）**：新增 `scripts/backfill_thread_owner.py`（默认 dry-run）按 `metadata.owner` 回填，分桶 `claim/legacy/ok/conflict/orphan`；`INSERT OR IGNORE` **绝不改写已有行**（改写=能把别人的会话认领走）；无归属（缺失/空/internal/dev）只报告不写，删它们用已有的 `cleanup_unowned_threads.py`
  - **认领路径**：`backfill_thread_owner.py --claim <tid> --user <uid>`（人工单条迁移）；批量迁移见上。⚠️ **部署顺序硬要求：先 `--apply` 回填，再发 fail-closed 的代码**（顺序反了 = 窗口期内老会话全员 403）。回滚 = 把 `row is None` 分支改回 `return True`，无需清理数据（本脚本只增不删）
  - **与 ops 层口径对齐**：`owned_thread` 对 `legacy` 哨兵行放行，对齐 `ownership.owner_filter` 的 `$or: [owner=身份, owner=legacy]`；`legacy`/`internal` 常量统一从 `auth/ownership.py` 取，不在 grants 里另写字面量
  - 验收：未登记会话被拒 + 迁移/认领路径明确（脚本内 docstring 写了容器内跑法）
  - 顺带：不采纳"未登记时回读 thread metadata.owner"的运行时方案 —— 那是每个 thread 访问一次内部 HTTP（`run-status` 被前端 1~2s 轮询），延迟与失败模式都不可接受；**写穿透 + 回填**才是正解

- [x] **P1-6 其余无校验端点补齐** —— ✅ 2026-09-23 已实施（`scripts/verify_endpoint_guards.py`；负对照 `NL2SQL_AUTH_DISABLED=1`；**2026-09-25 删掉 6 条 `/api/workspaces*` 断言后实测 23/23**）。`git-ssh-key` 定为 `require_user`（只回公钥，非管理员合法可用）
  - 清单：`thread_export`、`thread_run_status`、`message_feedback`、`feedback_stats`、`trace_routes`、~~`workspace`（含 `activate`）~~（**2026-09-25 端点已删除**）、`mcp/reload`、`db-configs/{name}/test`（工作区改动把它从 admin 降到了 user → SSRF 探活）、`git-ssh-key`
  - 验收：逐端点用「非 owner 已登录用户」打一遍，全部 401/403/404

- [x] **P1-7 `auth/grants.py` 与 `auth/users.py` 补锁** —— ✅ 2026-09-23 已实施（`scripts/verify_auth_store_concurrency.py` 8/8；负对照 2/2 检出：无锁 `os.replace` 撞 WinError 5、无锁双检建 20 条连接）。**追加**：读路径也必须持锁（见 P1-9）
  - `grants`：模块级单连接 + `check_same_thread=False` + 无锁 + **每个请求都写它**（`register_user`）+ 每个 run 创建再写两次（`claim_thread`/`record_thread_db`）→ 并发写热点
  - `users`：`_users_cache` 无锁 RMW（会丢用户、会复活已删用户）+ `_save_users` 用**固定 tmp 名** `path.with_suffix(".tmp")` → 改 mkstemp
  - 验收：并发 20 个请求各建一个用户 + 并发注册 100 次，用户数与授权记录数完全正确

- [x] **P1-8 全仓 SQLite 补 `PRAGMA busy_timeout`；`fts.sqlite` 开 WAL 且改按需更新；`eval_queue.claim` 改原子 CAS** —— ✅ 2026-09-23 已实施（`scripts/verify_store_concurrency.py` D/E/F/G 项）。**顺带发现并修掉一个更严重的**：fts 搜索的归属过滤原先在 `LIMIT` **之后**做 → 前 N 条全是别人的会话时，用户搜自己的历史得到「无结果」（E 项就是这条的判别性用例）
  - 现状：全仓无一处 `busy_timeout`；`fts.sqlite` 每次操作新建连接、每次检索**先写** + 跑 DDL → 并发搜索直接 5xx；`eval_queue.claim` 是 SELECT→UPDATE 非原子 → 同一 judge 任务跑两遍（重复烧 token）
  - 验收：并发 10 个搜索请求无 `database is locked`；两个进程同时 drain 队列，同一任务只被领一次

- [x] **P1-9 `traces.sqlite` 收敛连接；`last_insert_rowid()` 移入锁内** —— ✅ 2026-09-23 已实施（`scripts/verify_store_concurrency.py` A/B/C/J 项）。**新发现（原清单没有、比 seq 撞号更急）**：共享连接上的**并发读**会互踩 —— sqlite3 模块按 SQL 文本缓存 prepared statement 并跨游标复用，两个线程跑同一条 SELECT 会互相 reset（实测 8 线程稳定复现 11 次异常 + 2 次空结果）。受影响的读路径：`grants.owned_thread`（每次会话访问）、`EventStore.query_events` 等（前端按会话 1~2s 轮询）→ 修法=一条连接一把锁、**读写都进锁**
  - 现状：多实例各自 `_seq_counters` → 同 thread 事件 **seq 重复**（索引非 UNIQUE，不报错 → 前端排序错乱）；sync watcher 每个子任务新建连接且**不关** → fd 泄漏
  - 验收：并发写同一 thread，seq 严格单调无重复；长跑后 `lsof` 无连接堆积

- [x] **P1-10 `_TOOL_EXECUTOR` 加上限保护 + 超时任务真取消** —— ✅ 2026-09-23 已实施（`scripts/verify_tool_pool.py` 31/31）。**"真取消"改成如实说不**：Python 杀不掉线程（`future.cancel()` 只对尚未开始的任务有效），所以做的是"满载快速失败 + 阈值后换池"，把「不可取消」明确记在注释与验收里，不假装做到
  - 现状（原）：全局 `ThreadPoolExecutor(max_workers=4)`，其**工作队列无界**；超时任务不取消 → 4 个卡住的工具让所有用户的同步工具调用一起排队，而调用方要等满**自己的** timeout（数据工具 300s）才拿到超时消息 = **每个工具都"假超时"**（明明一秒没跑），且槽位永不回收 → 全局性故障
  - 实现：`_SyncToolPool`（`path_resolver.py`）——① 自记 `_inuse`，**在取槽阶段**判满，满则等待 5s（吸收正常抖动）后返回**「繁忙」**，与"超时"是**两条不同消息**（LLM 才能分辨"没跑"与"跑超时了"）；② `_inuse` 只在工作函数**真正返回**时归还（超时但仍在跑 → 继续占槽，如实反映"没有可用并发"）；③ 满载**持续** 300s → 换新池（旧池 `shutdown(wait=False)`、旧线程随各自调用结束退出，泄漏上限 = 每次回收 ≤ 4 线程），后续调用立刻恢复可用。`timeout=None` 的工具仍走原内联路径
  - 验收（31/31）：4 个卡死后第 5 个**快速返回繁忙**（0.30s « 30s）且函数**一次都没执行**；破坏式负对照——把取槽上限拆掉（复刻旧写法）同一局面立刻变回 0.51s 的"假超时"，证明断言不是恒真；裸池对照证明旧写法的第 3 个调用"根本没开始跑"
  - 未做：**异步路径**（`wrapped_arun` 本来就 `asyncio.wait` 立即返回、不排队）未加同样守卫 —— 它不阻塞调用方，属另一类风险（无并发上限），未列入本项

- [x] **P1-11 切工作区时失效所有进程级缓存** —— 🗑️ **已随 T3 删除（2026-09-25）**：没有"切工作区"这个动作了 ⇒ `cache_reset.py` 与 `verify_workspace_cache_reset.py` 一并删除。**但其中两项缓存的失效必须留在别处**（否则是静默口径降级）：`_db_name_norm_cache` / `_semantic_override_cache` 重挂到 `semantic_db.invalidate_db_discovery_caches()`，由 db_config（3 处）+ 语义库（1 处）写路径调用；`_wrenai_display_cache` 也在这条链上。其余三组（prompt / skills 物化缓存键含版本号，MCP `_sub_entries` 有独立对账）本就不需要切换钩子。新验收 `scripts/verify_workspace_pinned.py` §⑤。以下为 2026-09-23 的原实现，保留作依据。
  - ✅ 2026-09-23 已实施（`scripts/verify_workspace_cache_reset.py` 30/30，脚本已随 T3 删除）
  - 现状风险（原）：工作区是**全局单值**，但一批缓存是"第一次用到时按当时的工作区算出来就一直复用" → 切完后 `mcp_tool._sub_entries` 里**旧工作区的库工具仍可调用**（跨工作区读数）；`_db_name_norm_cache` 让新工作区建模的库判「未建模」→ 静默从语义层掉到 dbmcp 直连（口径变且不报错）；`detector`/`_wrenai_display_cache`（**原先永不失效**）/语义库·prompt·skill 的物化目录缓存（值都是**旧工作区内的绝对路径**）同样残留
  - 实现：新增 `agent/workspace_manager/cache_reset.py`（清单 + 理由 + 顺序）——`reset_process_caches()` 同步清全部（best-effort、单项失败不连累其它，失败项进报告与 warning）+ **摘掉**旧工具条目；`reload_tools_for_active_workspace()` 第 2 段装载。挂载点：`WorkspaceManager.activate_workspace`（**注册表锁之外**，见下）与 `unregister_workspace`（活跃工作区被取消 → 回退 default 也是"变了"）；新增的 5 个失效入口分散在各模块（`reset_db_name_norm_cache`/`reset_semantic_override_cache`/`reset_wrenai_display_cache`/`reset_prompt_cache`/`reset_skills_cache`）
  - **两段式**（安全属性必须落在响应之前）：① 同步摘掉旧工具 → 旧库工具当场不可见、也不可执行（执行侧对已发出的调用回错误 ToolMessage）；② `PUT /api/workspaces/{name}/activate` 里 `await asyncio.to_thread(refresh_sub_entries)` 装载新工作区的工具，**不能丢后台**——`reload_sub_entries_in_background()` 遇到"已有对账在跑"直接返回 False，那样新工作区的工具就**永远**没人加载（静默退化）。响应里带 `tools.loaded`，哪个库没起来当场可见
  - **刻意不清**（写进代码注释与验证脚本，免得下次无脑全清）：`_user_stores`/`thinking_toggle._model_cache`（模型配置**按用户**，与工作区无关）、`eval_flags_store._path_cache`（锚 shared）、`skills_versioning._remote_cache`（按 git 远端缓存，清了只是白打一次网络）
  - 验收（30/30，含判别性负对照）：① 五组缓存清得掉 + `_remote_cache` **不**被清（证明清单有选择）；② **负对照=把清理变 no-op 复刻修复前**：切到 ws-b 后仍认为 alpha 已建模（`discover()=={'alpha'}`）、ws-b 自己的库反倒判未建模、旧工具仍在注册表；③ 真实切换 A→B→A：切换响应返回时清单里**已无** alpha 工具、加载后**只含** beta 的工具，切回对称；④ 端到端只调 `activate_workspace` 即完成"清缓存+摘旧+待装"，第 1 段结束时新条目 `tools=0`（只摘不装）、第 2 段后 `tools=1`
  - 未做：工作区**目录内**的产物（report/tmp）无需失效（按路径实时解析）；跨进程（worker 子进程）的缓存各自进程内，切工作区不影响已在跑的 run

- [x] **P1-12 凭据加固** —— ✅ 2026-09-24 已实施（`scripts/verify_auth_hardening.py` 76/76；**2026-09-24 已发版**）
  - 现状风险（原）：默认口令 `admin/admin123` 明文写死且无改密入口；口令哈希是**单轮无盐 SHA-256**（撞库即破，且相同口令哈希相同 = 一眼看出谁跟谁同口令）；token 是**无状态 HMAC**，签名只证明"是我签的"、不证明"它还该有效" —— 改密/删号/降权后 24h 内照用；Cookie 无 `Secure`（生产是被 nginx 转发的明文 HTTP）
  - **实现（六件事，都在后端，前端一行没改）**：
    1. **哈希 → PBKDF2-HMAC-SHA256**（`users.hash_password`）：260k 轮 + 16 字节随机盐，自描述格式 `pbkdf2_sha256$轮数$盐$摘要`。校验用 `hmac.compare_digest`，解析失败一律 `False`（不抛）。**存量无盐哈希仍能登录**，且登录时自动**就地升级**（`needs_rehash` → `_upgrade_hash`）——升级只重写 `password_hash` 字段，**绝不动 `token_version`**（否则"一登录就把自己踢下线"）
    2. **吊销 = 版本号**（`token.py` + `users.token_version_of`）：token 里带 `pv`，与账号记录的 `token_version` 比对；**记录不存在 = 失效**；`display_name` / `is_admin` 也**从记录读**，不采信 token 里的旧值 → 降权当场生效。改密 / 删号 / 管理员 `POST /api/auth/users/{uid}/revoke` 三处都 +1
    3. **向后兼容 = 不造成全站重登**：老 token 无 `pv`（按 0）、老记录无 `token_version`（按 0）→ 对齐放行。发版当天没人被踢出去
    4. **首登强制改密**：`must_change_password` 字段 + 默认管理员打上标记；端点 `POST /api/auth/change-password`（自助改密，改完**重发 cookie**，旧的当场失效）
    5. **Cookie `Secure`** 按 scheme / `X-Forwarded-Proto` 判定，另有 `NL2SQL_COOKIE_SECURE` 显式覆盖
    6. 登录/改密的口令校验走 `asyncio.to_thread`（PBKDF2 260k 轮是 CPU 活，同步做会卡住事件循环 —— 这条顺带把 P1-14 里"每请求都走"的两处之一先灭了）
  - **决策与偏离（都与条目原文不同，逐条记）**：
    - **选 PBKDF2 而不是条目写的 bcrypt**：bcrypt/argon2 需要装包，而生产发版是**镜像离线搬运**（`docker save | load`，目标机无外网）→ 不能加依赖。PBKDF2 是 stdlib（`hashlib.pbkdf2_hmac`）、也是 Django 的默认。自描述格式的意义：将来真要换算法，是**逐条升级**而不是"全体改密"
    - **"登录即失效"改成"登录不吊销"**：条目原文写"登录/改密/删用户即失效"。**登录不吊销** —— 会把同一个人其它设备一起踢掉，而登录是"取得凭据"、不是"凭据失效"事件。改密 / 删号 / 显式吊销三处生效
    - **"首登必须改密"退成"只标记 + 一个默认关的开关"**：前端 `src/lib/authApi.ts` 等 ~20/96 个 `.ts*` 文件是 DLP 密文、`next build` 被挡 → 前端加不了改密页面。后端**拦了也没有自助入口 = 把人锁在门外**，所以做的是：默认管理员打标记 + `/api/auth/me` 与登录响应里带上该字段（前端将来直接用）+ 强制拦截逻辑写好但由 `NL2SQL_FORCE_PASSWORD_CHANGE` 控制（**默认 off**）。真打开时白名单放行 `/api/auth/me`、`/api/auth/change-password`、`/api/auth/logout`（留自助脱困路径），403 body 里直接给出可复制的 curl 命令。**前端一落地就把开关打开**
    - `Secure` 的**真修法**是 nginx 终止 TLS（明文 HTTP 上无条件加 `Secure` = 浏览器直接丢弃 cookie = 登录"成功"但每个请求都 401），当前的探测只是让明文环境可用
  - 验收（76/76，六段）：① 哈希（同口令两次哈希不同 / 格式 / 轮数 ≥200k / 坏哈希 → False 不抛 / 存量哈希可校验 / 升级不动版本号）；② 吊销（多设备共存 / 老 token 穿越发版存活 / 改密后所有设备失效 / **登录不吊销** / 显式吊销不动口令 / 删号即失效 / 降权立刻生效）；③ 标记 + 自助改密 + 400 分支；④ Cookie `Secure` 矩阵 + 重发 cookie + 旧 cookie 401 + 管理员吊销端点；⑤ 强制开关默认关、打开后白名单放行的**顺序陷阱**（logout 会清 cookie，所以它不能排在改密检查之前）；⑥ **破坏性负对照三连**：`token_version_of` 打成恒返 0 → 已吊销的 token **复活**；`verify_password_hash` 打成恒 True → **任何口令都能登**；`hash_password` 换回 `_legacy_sha256` → 盐断言失守（证明断言不是恒真）
  - **回归代价（最值得记的一条）**：`verify_token` 改成"以账号记录为唯一真源"之后，**三个既有套件当场打红** —— `verify_endpoint_guards` 30→7、`verify_report_ownership` 40→25、`verify_thread_ownership` 29→27，表现全是 **401**（另有 `verify_auth_middleware` 16→13，首轮扫描漏看，第二轮才发现）。原因不是生产代码错，而是这些测试习惯"**凭空签一个 token 冒充某用户**"：`sign_token("alice", …)` → `verify_token` → `find_user("alice")` → `None` → 401。这正是我们想要的线上行为（记录没了/改过密码/被吊销 → 立即失效），**在生产里"只有登录能发 token"是对的**，是测试脚手架过时了。修法：新增 `scripts/_auth_test_support.py`（先登记身份、再按记录里的真实 `token_version` 签发 = 等价于"管理员建号 → 用户登录"）。**教训：改了鉴权的"真源"，要全仓搜一遍所有造 token 的地方，不能只跑一遍单测**（P1-13 的版本坑是同一类：改动落在哪一层，验证就得跟到那一层）
  - 未做（如实记）：**登出不吊销**（无状态 token 的代价 —— 服务端不留状态就无从"记住要吊销谁"，要真做得上黑名单/会话表）；`Secure` 生效仍待 nginx TLS；强制改密的**前端 UI** 未做（后端已就绪）

- [x] **P1-13 加单测锁死「过滤器仅限 `$eq`/`$or`」+ 核对 legacy 存量** —— ✅ 2026-09-23 已实施（`scripts/verify_authz_filter_contract.py` 47/47；**必须在项目 `.venv` 里跑**，脚本 ⓪ 会自证版本）
  - 原因：inmem 对未知操作符**静默忽略 = 放行**；全仓无代码写 `legacy`，需确认它不是靠手工改库来的
  - 验收：测试里写一个 `$ne` 过滤器，断言它不会退化成「放行」
  - **实测到的运行期真相（这是"只能写 `$eq`/`$or`"的理由，不是风格偏好）**：
    - 生效：`$eq` / `$and` / `$or` / `$contains` / 简写等值（直接给字符串）
    - **静默放行**：dict 值里的未知操作符 —— `{"owner": {"$ne": "alice"}}` 对 **任何人**都判可见；`$in`/`$nin`/`$gt`/`$lt`/`$exists`/`$regex` 同样（7 个全试过，全部 True）。**不报错、不告警、日志里什么都没有**
    - 同一 dict 里写第二个操作符被丢弃（`next(iter(value))` 只取第一个）→ 多写不报错、只是无效
    - 顶层未知操作符（值不是 dict）走"直接等值"比一个不存在的键 → **全拒**（另一种故障：所有人都看不到自己的会话，同样无报错）
    - `$or` 元素 **< 2 个** → 运行时直接 `HTTPException 500`（不是"当 1 个用"）
  - **两道锁**：① 运行期矩阵实测上述行为（版本一变就红）；② AST 扫 `src/**.py` 里每个 `$` 开头的字符串字面量，白名单 `{"$or"}` —— 全仓唯一的构造点是 `auth/ownership.py:36`。将来谁"顺手加个 `$ne`"，这个脚本立刻红，而不是等生产上越权
  - **⑤ 负对照是破坏性的**：把 `owner_filter` 临时换成 `{"owner": {"$ne": identity}}` → 断言**别人的会话对 alice 变成可见**（复现"静默放行"这条路真的存在，而不是只有我口头说）
  - **⑥ 存量核对**：`backfill_thread_owner.py` 只经 HTTP 列线程、需跑在目标环境（**实盘 dry-run 待授权**）；这里离线钉死它的 `plan()` 分桶 —— 真实 owner→`claim`、`legacy`→哨兵、缺失/空/None/`internal`/`dev`→`orphan`（**不写**）、账本≠metadata→`conflict`（**不改写**，改写可能夺走别人的会话）；并断言两份账目的身份集合同口径。判错一个桶是静默的（把 orphan 当 legacy 浇出去 = 全员可见），所以值得单测
  - **版本坑（最该记住的一条）**：本机有**两个** `langgraph-runtime-inmem` —— 项目 `.venv` 是 **0.31.1**（＝生产同版本，支持 `$or`），系统 site-packages 是 **0.14.1**（`inspect.getsource` 里**没有 `$or` 分支**）。旧版本下我们的过滤器**不是越权，而是另一个方向的故障**：所有人（非管理员）都看不到自己的会话。所以这个脚本**不能**用 `uv run --no-project python` 跑（那是 ambient 0.14.1）；用错解释器它会 0/2 并打印正确命令，不会给假绿

- [x] **P1-14 把 §3.3 清单里的同步阻塞逐个包 `asyncio.to_thread`** —— ✅ 2026-09-24 已实施（`scripts/verify_event_loop_liveness.py` 32/32；**2026-09-24 已发版**；**破坏性负对照**：把被测模块的 `offload` 换成就地调用 → 4 个真实 handler 的滞后断言全部变红）
  - **改法**：新增 `agent/utils/offload.py`，**两个池**：`offload()` = `asyncio.to_thread`（默认执行器，**传播 contextvars**）、`offload_long()` = 独立有界池（`NL2SQL_LONG_OFFLOAD_WORKERS`，默认 4，**不传播 contextvars**）。判据不是"函数看起来 async 不 async"，而是"这段代码里有没有**真的** `await`" —— 本仓大量驱动是「async 外壳 + 同步实心」（10 个 `db/engine/*/sql_runner.py` 全是 `async def run_sql`，函数体里一个 `await` 都没有），`await runner.run_sql(...)` 并不会让出控制权
  - **为什么必须分两个池**（脚本 ① 有对照实验）：共用一个池时，一条 600s 的 wren 构建占满默认池后，**每个请求都要走的 auth 写库会排在它后面** —— 那只是把「卡事件循环」换成「卡线程池」，而且故障形态更难查（请求全都"慢"但日志无异常）。实测：长池占满 4 个槽时短调用等 **2.3ms**；同一个池占满时等 **550ms**
  - 覆盖面：**15 个文件、116 个搬运点**（`wren_semantic` 37 / `feedback_annotation` 30 / `message_feedback` 11 / `auth_admin` 10 / `experiment` 7 / `trace_routes` 5 / `thread_search` 4 / `workspace` 3 / `model_config` 2 / `eval_flags` 2 / `thread_fork`·`db_config`·`git_repo`·`check_progress`·`auth.backend` 各 1）
  - **`check_progress._build_check_result` 必须走 `offload`（短池）而不是 `offload_long`**：它内部靠 `get_config()` 读本请求的 db_name/thread_id，而 contextvars 只有 `to_thread` 会传播（`run_in_executor` 不会）。脚本 ⑤ 把这条做成了**静态回归**：规则从代码里推导（直读 / 传递调用 / 别名赋值三条识别路径），并用两条合成负对照钉住，防止后人"优化"成 `offload_long`
  - **故意不搬的**（附测量，不是偷懒）：`verify_token`（每个受保护请求都走）实测 **15µs/次**，而一次线程切换 **430~470µs（29~33×）** —— 它读的是进程内缓存（`_users_cache` 记忆化 + 线性扫几个 dict），包线程只会让每个请求更慢。同理留在主线程的还有 `db_config_store.list_configs/get`（小 JSON + AES，已被自己的 `_LOCK` 串住）、`_read_project_name`（一个小 yml）、`_scan_wren_projects`、`experiment` 的 status 读写（小 JSON）
  - **踩过的两个坑**（都写进了脚本注释）：
    1. 给 `offload_long(...)` 搬线程时**漏了 import** → `py_compile`/`ast.parse` 全绿，端点一碰就 NameError。且 `symtable` **不检查**「`await` 写在**同步**函数里」（实测 3.13：symtable 放行、`compile` 报 `'await' outside async function`）—— 我给同步的 `withdraw_auto_good` 里插了 `await`，正是靠显式 `compile()` 才逮到（那处会让 `feedback_annotation` 整个模块 import 失败）。扫描器 `check_undefined_names.py` 已补这两步，并加了「**扫到 0 个文件即失败**」：`Path(文件).rglob("*.py")` 静默返回空，曾让我把"扫描 0 个文件"当成通过
    2. **同步 Langfuse SDK 是隐藏的阻塞类别**：`langfuse.get_client()` 返回的是**同步**客户端（异步那个叫 `AsyncLangfuse`），`client.api.datasets.list` 这类是**阻塞 HTTP**。按「接收者名字像不像 async」做启发式扫描一个都查不出来 —— `langgraph_sdk.get_client()` 返回的才是协程（那批是假阳性）、`httpx.AsyncClient` 那几个是真 await。这类只能逐调用点看 SDK 类型
  - 验收：4 个真实 handler（`validate_project` / `read_knowledge` / `put_feedback` / `_stamp_thread_owner`）各配 500ms 慢依赖 → 并发 5ms 探针**最大滞后 11~12ms**（基线噪声 11ms）、慢活在非主线程、慢依赖确实被执行（耗时 ≥ 阻塞时长，不是被跳过）
  - 残余：**首次请求的模块导入成本仍在事件循环上**（单个 handler 的模块首导入实测 400~800ms，`auth.backend` 要拉起整个 langgraph_sdk）。生产里不发生 —— `src/api/custom_app.py` 启动时逐个 import 所有 api 模块 —— 但**新加的自定义 API 模块若只在 handler 内 import，第一个请求就要付这笔钱**（脚本 ② 第一版正是被这个坑到：同一用例预热前 456ms、预热后 11ms）

- [x] **P1-15 统一 `thread_owner` 表与 `metadata.owner` 两套账本** —— ✅ 2026-09-23 已实施（与 P1-5 同一批改动）
  - **统一方式 = 写穿透 + 常量共用 + 回填，不是"运行时读对方"**：两套账本各有各的读者（REST 层读表、ops 层读 metadata），谁也替不了谁，所以让**所有写点同时写两边**：
    `@auth.on.threads.create`（新建）/ `langfuse_metadata._apply_ownership`（建 run，覆盖子 agent 线程）/ `thread_fork`（fork）
  - 哨兵值（`legacy` / `internal`）统一从 `auth/ownership.py` 取，`grants.py` 不再各写一份字面量
  - `grants.py` 文件头补了「两套账本 + 写侧入口清单」的说明，避免下次又只改一边
  - 残余：`thread_db`（会话↔库）与 `metadata` 里的图/库信息仍是两处，但那条不在隔离路径上，未动

- [x] **P1-16 `dbmcp_run_sql(db_name=…)` 的参数级库授权校验**（P1-2 的残留口，2026-09-23 新记）—— ✅ 2026-09-23 已实施（`scripts/verify_db_exec_authz.py` 32/32；破坏性负对照：把 `_target_db` 打成恒返空 → 同一越权调用立刻执行）
  - 现状：wrenai 通道已按库授权（工具名带库名，P1-2 已收紧），但 **dbmcp 直连通道把目标库当工具参数传** —— 未选库时 dbmcp 仍 fail-open 保留（见 `_filter_tools` 注释：未建模库上 dbmcp 是唯一查询通道），且工具出站前无法知道参数里会填哪个库 → `dbmcp_run_sql(db_name="<无授权的库>")` 依旧可达
  - 改法：在 `QueryGateMiddleware.wrap_tool_call`（已有 `_active_db` 解析逻辑，参数优先）加一道 `can_access_db(user, db_name)`；用户身份取 `langgraph.config.get_config()["configurable"]["user_id"]`（P1-2 已保证该值对客户端伪造免疫）
  - **实施（三道要点，比条目原文多一条）**：
    1. **规则零放在通道硬闸之前**：`_gate` 第一判就是库授权。反过来的话，无权者发 `dbmcp` 到**已建模**库会拿到"该库已建模，请走语义层"的指路文案 —— 等于从错误信息里告诉越权者"这个库存在且已建模"（存在性预言机，与 P1-3 越权响应=404 同一条原则）
    2. **连带堵住 wrenai 通道的同款口子**（条目原文没写，读代码时发现）：`wrenai_<slug>_*` 的库名编码在**工具名**里，而执行侧注册表 `mcp_tool.lookup_sub_tool` **只按名字取实例、不看身份** —— 模型幻觉出的工具名、以及**历史消息里授权被撤销前存下的旧工具名**照样执行。只补 dbmcp 等于补了半条：已建模库恰恰只走 wrenai 通道。故规则零对 `_is_query_tool()`（两条通道的所有工具）统一判，并为此新增 `semantic_db.db_name_from_wrenai_tool()`（**正算前缀匹配，不"反解 slug"**——`_server_slug` 是有损折叠，`WIT运营管理平台数据库`→`WIT` 反解必错）
    3. **判权口径与 `tool_filter` 收成一处** `agent/auth/runtime.py`（`caller_identity` / `resolve_caller` / `caller_can_access_db`）：两处各写一份必然漂移（P1-2 的 `tool_filter` 已改为调用它，32/32 未回归）。fail-open 边界写死：内部调用 / dev 旁路 / 读用户记录失败 → **不启用**判权；身份真实但账号已删 → 可见库为空 → **拒**（fail-closed）
  - **顺带修掉的既有缺陷**：`_ACTIVE_DB_RE`（规则一与规则零共用的 state 兜底）要求 `——` 后**紧跟**"已在 Wren 语义层建模"，而 `dynamic_prompt` 注入的真实文本是 `—— **已在 Wren 语义层建模**。`（**带 Markdown 加粗**）→ **一条都匹配不上**，这条兜底长期是死代码。加 `\*{0,2}` 后两种形态都认（`query_gate` 文件头已注明）
  - 验收（32/32，真图非合成对象）：未授权用户 `dbmcp_run_sql(db_name=<未授权库>)` → **不执行** + 模型收到含库名与**可用库清单**的原因（模型能自己换库重试）；`get_db_info` 同样判；wrenai 通道（工具名级）与非查询类语义工具（`get_data_source`）同样判；非库工具（图表）不受影响
  - **测试拓扑的坑（写下来免得下次白试）**：`dbmcp` 在**已建模**库上会被规则一已经拦掉，所以"直连通道能不能执行"**只能在未建模库上观察** —— 首版用已建模库当靶子，断言全被规则一顺带拦下（假绿）。最终拓扑：`db_alpha`(已建模/alice) · `db_beta`(已建模/仅 bob) · `db_gamma`(**未建模**/alice) · `db_delta`(**未建模**/无人) —— 未授权的靶子必须是**未建模**库，⑥ 的负对照才复现得出越权

- [x] **P1-17 chart-saver skill 产物的归属登记**（P1-3 的残留口，2026-09-23 新记）—— ✅ 2026-09-24 已实施（`scripts/verify_chart_artifact_owner.py` 47/47，连跑 3 次稳定 exit 0；**后端 2026-09-24 已发版**；**破坏性负对照**：同一条产出链不挂中间件 → BOB 下载 ALICE 的图 **200**，挂上后同一张图 **404**）
  - 现状：`execute` 调 `save_chart.py` 落盘的 `.svg/.png` **无归属**（子进程拿不到请求身份）→ 任何登录用户知道文件名即可读；而 P1-3 已让这类文件名不可猜（时间戳+随机），所以只剩"目录列表把名字送出去"这一条路
  - 改法：在**中间件**里做（那里一定有身份，且不依赖子进程 env）——`execute` 工具返回的文本里有 `✅ 图表已保存: <宿主绝对路径>`，新结果侧中间件 `ChartArtifactOwnerMiddleware`（`src/agent/middlewares/chart_artifact_owner.py`）正则取出文件名 → `record_report_owner(name, configurable.user_id, thread_id)`。**不要**试图给 shell 子进程注入身份：`DynamicLocalShellBackend` 的 `_env` 是 import 时的 `os.environ.copy()`
  - ⚠️ **条目原文写的验收是恒真的，已改判据**：原文"两个用户各出一个同名图表，彼此在 `GET /api/reports` 列表里看不到对方那份"—— 但 `list_reports` 在**判权之前**就把 `.svg/.png` 过滤掉了（`report_file.py:147` 只列 `.md/.html/.csv/.json`），所以这条断言**修与不修都通过**，它测的是后缀过滤、不是归属。改用能区分的那条：**直接 `GET /api/reports/<图名>?download=1`**（修复前他人 200，修复后 404）；脚本里两条都留着，并显式标注哪条恒真（`他人列表里看不到这张图（此条恒真…）`）
  - **实施要点（三处）**：
    1. **只认显式标记，不扫"结果里出现的所有 report/ 路径"**：shell 结果里出现某路径 ≠ 刚由我写出（`cat`/`ls` 别人的文件也会出现），而 `record_report_owner` 是 INSERT OR IGNORE（先到者胜）—— 对**无记录的存量文件**，一次 `cat` 就等于认领，反过来把原主挡在外面。**宁可漏登记（漏了＝维持放行），不可错登记**
    2. **登记前两道收敛**：路径父目录必须**等于当前活跃工作区的 `report_dir`**（`save_chart.py --dir` 能落别处，而账本只认基名，别处同名文件会"认领"report/ 里别人的那份）；文件必须**真实存在**（排除路径被截断/被 shell 加料后凑出的假名字）。解析不了/超范围 → 不登记且不抛异常
    3. **身份必须过 `ownership.is_real_owner`，不能只判非空**：内部调用与 dev 旁路的身份是 `"internal"` / `"dev"` 这两个**非空**哨兵（`caller_identity()` 返回它们，不是空串 —— 与条目原先的假设相反）。照字面登记会把图记到它们名下，而 `can_read_report` 对"有记录且非本人"是**拒绝** → 等于把真实用户锁在自己刚出的图外面，比原来的洞更坏
  - 验收（47/47）：真 `save_chart.py` **子进程**落盘并按其 stdout 契约回喂（不是手写标记）→ 基名正确；正/反斜杠形态、无空格冒号、反引号/引号/中文标点包裹都能剥；无标记 → 不登记；一个结果多张图按序去重；范围收敛 5 条（不存在/不在 report/ 里/在 report/ 子目录/工作区解析失败/对照）；身份 4 条（真用户登记、`internal`/`dev`/空不登记、无 config 上下文不登记不炸）；工具名非 `execute` 不登记；content blocks 形态认；重复登记不改写归属；端到端（真 `AuthMiddleware` + 真 `report_file` handler）本人 200/他人 404/管理员 200/未登录 401/HEAD 同样拦/越权 404 与"不存在" 404 **同正文**；存量无记录文件仍放行（老图不会因本次修复突然打不开）
  - ⚠️ **本脚本的坑（写下来免得下次白试）**：`agent.tools` **在 import 期**就去连 MCP 子进程（`load_*_tools` 的模块级副作用），若这个 import 发生在事件循环里，`_load_mcp_servers` 的 `asyncio.new_event_loop()` 会对每个 server 抛「Cannot run the event loop while another loop is running」再各退避重试 3s（实测输出被淹 + 拖慢 40s）。解法：在 `asyncio.run` **之外**先 `import agent.tools.mcp_tool` 预热
  - **残留**：`--dir` 落别处产出的图不登记（与"漏了＝维持放行"同口径）；`.svg/.png` 仍不在列表接口里（另一件事，归属之外的可用性问题）；存量无归属的图表文件保持放行（沿用 `can_read_report` 的兼容口径，与 P1-5 的 fail-closed 不同调，见该条）；本中间件**依赖运行期那份 `save_chart.py` 的 stdout 契约** —— 生产 skills 在外置 data_root、不随 tar 更新（见 P1-3 残留第二条），同步 skills 时**必须核对该行打印**，否则中间件静默不登记（表现 = 修复没生效，且没有任何报错）

---

## P2 — 可靠性（周到双周）

- [x] **P2-1 发版改滚动/优雅（drain）** —— ✅ 2026-09-24 已实施（后端 `src/api/drain.py` + `custom_app` 接线 + 容器内运维脚本 `scripts/ops_drain.py` + 两个发布脚本 + 手册 §二.1.1；`scripts/verify_graceful_drain.py` 76/76，连跑 3 次稳定 exit 0；**2026-09-24 已发版**；**破坏性负对照**：预算置 0 → flush 时仍有 7 个任务在跑，证明"等到了"不是恒真）
  - 现状：`stop`(默认 10s→SIGKILL) → `rm -f` → `up`，固定 `sleep 75` 后再验证；**无 drain、无自动回滚**，在跑 run 全断
  - 验收：一次发版期间已提交的 run 能跑完（或明确提示用户），而不是静默中断
  - **读源码得到的机制（决定了方案形状，条目原文没写）**：
    1. **langgraph 自己的排空是硬编码 5 秒**：`langgraph_runtime_inmem/queue.py` 的 `SHUTDOWN_GRACE_PERIOD_SECS = 5`（`asyncio.wait_for` 到点即弃）；`langgraph_api.config.BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS`（默认 180）在本版本**没有任何读取点**（只在 config 里定义，属 postgres/Go 核心那条路径）→ "改个配置就好"是行不通的
    2. **我们只有一个插入点**：`langgraph_api/timing/timer.py` 的 `combine_lifespans(base, user)` 用 `AsyncExitStack`（逆序退出）→ 自定义 app 的 lifespan 在基础 runtime teardown **之前**退出。所以排空只能写在 `custom_app._lifespan` 里
    3. **自定义 app 的中间件是全局的**：`langgraph_api/server.py` 默认分支 `app.user_middleware = custom_middleware + global_middleware + [EnsureStoreAccessible]` → 挂在 `custom_app` 上的 gate 连原生 `/threads/{tid}/runs/stream`（前端真正提交 run 的路径）一起覆盖。⚠️ 若有人把 `HTTP_CONFIG.middleware_order` 配成 `"auth_first"`，自定义中间件会退化成只挂自定义路由的 Mount 级、**静默**漏掉原生 run 路径（脚本 ⑤ 对这条做了静态回归；本仓未配该键）
    4. **uvicorn 先关监听、再跑 lifespan** → 信号路径上不会再有新请求进来，gate 在那一刻形同虚设（不影响正确性）；它保证的是"即使有人直接 `docker restart`，在跑的 run 也有预算跑完"
  - **两个入口，各管一段**：① 协作式（`POST /api/admin/drain`，容器内脚本驱动）——排空期间**新提交**拿到 503 + 中文提示 + `Retry-After`，并把"还剩几个后台任务"打给运维；② 信号式（`docker-compose stop -t 240`，也是主路径）——SIGTERM → lifespan 排空 → 之后才是 langgraph 的 5s 窗口
  - **计数真源**：`langgraph_runtime_inmem.queue.get_num_workers()`（在跑的后台任务数，**含子 agent run 与 sync 循环** —— 一次问数约 3 个，所以清零点比"run 数"更保守）。读不到（非 inmem 运行时/包改名）→ 视为 0 且**不等待**：宁可照旧硬停，也不要凭空把发版拖住（脚本 ② 用"把该模块从 `sys.modules` 里设成 None"来真触发这条兜底，而不是打桩上层函数 —— 打桩上层测的是"抛异常"，兜底不在那条路径上）
  - **拦截面必须白名单式**：只拦**新建 run** 的 7 条路径（`/runs{,/stream,/wait,/batch}` × 有无 `/threads/{tid}` 前缀），`/runs/cancel`、`/threads/{tid}/runs/{rid}/cancel`、`/runs/crons*`、列表/查询/`join` 一律放行 —— 排空期间用户最需要能用的恰恰是"取消我这个跑不完的查询"。按 `/runs` 前缀一刀切会把取消也拒掉（脚本 ① 逐条钉死，且断言**拒绝发生在入口**：下游 app 一次都没被调用）
  - **`_effective_budget` 里不许 `int()` 取整**（实测踩到）：`int(1.0 - ε) == 0` 会把整份预算归零、等待直接跳过（表现是"排空瞬完"，看起来一切正常）。预算按 `begin_drain` 的截止时刻算**剩余**浮点秒，脚本 ② 用 `NL2SQL_DRAIN_SECS=1` 断言耗时落在 `[1.0, 1.5)` 把它钉住
  - **运维入口不引新后门**：`ops_drain.py` 在**容器内**运行，用现有管理员账号的**当前 `token_version`** 签一枚 token 来调端点（与 `e2e_thread_isolation.py` 同一套路）——不需要任何口令，也**没有**新增"loopback 免鉴权"（那会是一个新的可用性开关：`127.0.0.1` 已在 `_INTERNAL_PREFIXES` 里，免 Cookie 时身份是 `internal`/`is_admin=False`，照样过不了 `require_admin`）。挑管理员时优先挑**不需要改密**的（开了 `NL2SQL_FORCE_PASSWORD_CHANGE` 时未改初始密码的账号会被中间件 403）
  - 验收（76/76，六段）：① 拦截面（未排空放行、排空时 7 条创建路径 503 + Retry-After + JSON 正文 + 下游未被调用、11 组放行调用逐条带方法（`POST /threads/{tid}/runs` 该拒 vs `GET` 该放行——**只看路径会把两者混为一谈**）、非 http scope 透传、撤销后恢复）；② 等待语义（真等清零、**超时不装成功**且耗时贴着预算、期间被 DELETE 撤销立即返回、预算 0 只拒不等、**计数不可得则不等**、预算解析与回退）；③ 真 `custom_app._lifespan`（启动不排空；退出先排空后 flush 且 **flush 时刻晚于清零时刻**；负对照：预算 0 → flush 时仍有 7 个在跑；排空抛异常仍 flush）；④ 真 `AuthMiddleware` + 真路由（未登录 401 / 非管理员 403 / 管理员 200 / GET 查 / 排空态与 gate 共享同一状态 / DELETE 恢复）；⑤ 接线（真 app 的 `user_middleware` 顺序 = CORS → DrainGate → Auth → LangfuseMetadata、端点已挂进真路由表、`_lifespan` 里 `wait_for_idle` 在 `flush_langfuse` 之前、7 条路径与正则对账、langgraph 的全局中间件前提与"本仓未配 `middleware_order`"）；⑥ 运维入口（签出的 token 能过 `verify_token` 且 `is_admin=True`；端点不可达 → 子进程退出码 1 且提示"直接停容器仍有信号式排空"）
  - **本脚本的坑**：`_lifespan` 的顺序断言**必须按 `lineno` 排**——`ast.walk` 是广度优先，嵌套的 `await` 会比同级的 `flush_langfuse()` 后出，直接按遍历次序比会得出**相反**的结论（第一版就这么红了）
  - **部署侧两条硬约束**（写进手册 §二.1.1，属服务器侧）：① `stop -t` / `stop_grace_period` **必须 > `NL2SQL_DRAIN_SECS`（默认 180）**，否则 SIGKILL 比预算先到、等待白做；② compose 补丁不会被发版覆盖（发布包虽含 `docker-compose.yml`，但解压落点是 `backend/docker-compose.yml`，compose 实际读 `$AppDir/docker-compose.yml`）
  - 残余（如实记）：**排空期间前端仍会看到连接层失败**（若走信号路径：uvicorn 已关监听）——友好 503 只在协作式路径窗口内生效；**没有自动回滚**（条目原文提了"无自动回滚"，本项只做 drain，回滚仍是 `rollback-backend.ps1` 手动）；**同步阻塞型 run 无法靠排空救**（若某 run 卡在同步阻塞里，`get_num_workers` 不清零，只能等预算耗尽——与 P1-14 互补，不是替代）；`restart`/OOM/daemon 重启走 compose 的 `stop_grace_period`，**默认 10s，需在服务器补 `240s`**（未补则只有脚本路径优雅）—— **2026-09-24 已补**：线上 `docker-compose.yml` 的 `langgraph-api` 加 `stop_grace_period: 240s`，重建容器后 `docker inspect -f '{{.Config.StopTimeout}}'` 核实为 **240s**（备份 `docker-compose.yml.bak-20260924-120838`）

- [x] **P2-2 `/app/logs` 挂持久卷 + 打开访问日志 + 跨组件 request-id** —— ✅ 2026-09-24 已实施（`src/api/request_log.py` + `start_server.build_log_config` + `custom_app` 接线 + `_common.stamp_thread_owner` 透传 + `docker/nginx.conf`；`scripts/verify_request_logging.py` 65/65，连跑 3 次稳定 exit 0；**2026-09-24 已发版**）
  - **落点改判（与原条目不同，且更好）**：原方案是"给 `/app/logs` 挂个卷"。实测生产 `/app/logs` 在容器可写层、而 `/app/data` **本来就是持久卷**（`agent_data`，`AGENT_DATA_ROOT=/app/data`）→ 日志落 `<AGENT_DATA_ROOT>/logs` = `/app/data/logs` 就满足了验收（"重启后仍能查到上一次"），**不需要改服务器 compose、不需要维护窗口**。解析顺序 `NL2SQL_LOG_DIR` > `<AGENT_DATA_ROOT>/logs` > 仓库根 `logs/`（第三条 = 改动前的行为，dev/CLI 不受影响）；生产**不要**设 `NL2SQL_LOG_DIR`（设了就绕过持久卷）
  - 轮转：`TimedRotatingFileHandler`，每天 0 点、保留最近 7 个历史文件 + 当前，utf-8，`delay=True`（懒建文件 → 无日志时不产空文件）
  - **rid 注入挂在 handler 而不是 logger 上**（`RequestIdFilter` 挂 `default`/`file` 两个 handler）：langgraph 自己有 logger 树，只给"本仓的 logger"挂过滤器会漏掉它的输出；挂 handler 是"经过这两个 handler 的一切记录都有 rid"。无请求上下文 → `rid=-`（后台任务/启动日志不串味）
  - **访问日志不走 uvicorn 的 access logger**：`access_log=False` **保持不动**（`uvicorn.access` 也压到 WARNING）——uvicorn 自带格式既没耗时也没 rid，开了只是多一行无语义重复；记日志的是 `RequestContextMiddleware`，**纯 ASGI、不缓冲 body** → SSE/流式逐条透传不被破坏（脚本 ③ 断言三条 body 顺序不变）
  - 覆盖面：中间件顺序 **CORS → request_log → DrainGate → Auth → Langfuse** ⇒ 503（排空中）/401/403 **各有一行** access 日志（"被拒的请求也能查"）；`/ok` 不记（healthcheck 每天约 2880 行噪音，会挤掉有用日志）；5xx 记 `ERROR`；响应未走完记 `no response: 客户端提前断开或异常`（status `-`）；响应头回显 `X-Request-ID`（前端拿它去后端日志里搜）
  - **入站 rid 必须白名单净化**（`^[A-Za-z0-9._:-]{1,64}$`，非法则丢弃重生成）：header 是攻击者可控输入，原样进日志 = **日志伪造**（换行注入假行）。脚本用 6 个坏样本（含 `"x"*100`、`"a\nb"`、中文、空串）+ 一条"被拒的 id 没出现在日志里"钉死；重复 header 只认一枚（响应头也只有一枚）
  - 客户端 IP 取 XFF **第一跳**；用户名取 `scope["state"]["user"]`（AuthMiddleware 注入）→ 同一行里能对上"谁、从哪、调了什么、多久、什么结果"；会话 id 从路径抽（`/threads/{tid}`，取前 8 位）
  - 跨组件：只改了**唯一一处**内部自调用（`api/_common.stamp_thread_owner` → `PATCH /threads/{tid}`），带 `rid_headers()` ⇒ 这条自调用在后端日志里与触发它的用户请求**同一枚 id**（否则一次操作在日志里断成两截）。nginx 侧仓库副本已写好 `$request_id` 生成 + `log_format`（含 `$request_time`）+ 三个 location 透传
  - **安全断言（非装饰）**：日志里有别人的请求路径/用户名/会话 id ⇒ 必须保证 agent **读不到**。`agent/settings/file_permissions.py` 的读白名单只有 `/shared/**` 与 `/workspace/**`，`/logs/**` 被拒 —— 脚本用 deepagents 真实的 `_check_fs_permission` 对**主 agent 与 nl2sql 子 agent 两套权限集**各断言一次，并带一条 `/workspace/report/x.md` 的放行对照（证明断言不是恒真）。**别把日志挪进 shared/ 或 workspace/**
  - **本脚本的坑**：手工驱动 ASGI 时 `receive()` 若永远返回 `http.request`，Starlette 的 `StreamingResponse` 会**死循环**（它同时跑 `listen_for_disconnect(receive)`）；原始症状是脚本在 ④ 卡死到超时、看不到任何 FAIL。修法是照真实客户端给一发 `http.disconnect`（`_drive(..., disconnect_after=0.2)`）。另：`StreamingResponse` 末尾会补一个**空 body** 收尾消息，断言 body 条数时要去空
  - 残余（如实记）：① **nginx 侧补丁**：内容与一键脚本已备好（`docs/weint环境/发布脚本/apply-nginx-request-id-patch.sh`，含 `--dry-run`/`--rollback`），**2026-09-24 已应用到线上并校验**（见下）；② nginx 官方镜像的 `access.log` 是指向 stdout 的软链 → 那份日志进 `docker logs`，**容器 rm 即丢**（与后端的持久日志不同）；③ **MCP 子进程、后台 run、子 agent 的日志与 rid 无关联**（不是请求驱动的，拿不到 contextvar）——"跨组件"目前覆盖 nginx→后端→内部自调用；④ 前端是**另一个仓库**（`harness-deep-agents-ui`），未改；⑤ 日志**没有 retention**（只有轮转：7 天文件），磁盘水位归 P2-5
  - **nginx 补丁已应用（2026-09-24）**：`docker/nginx.conf.bak-20260924-130220` 备份 → 就地写入（**inode 13803193 未变**，文件级 bind mount 的关键）→ `nginx -t` 通过 → `nginx -s reload`；实测校验：不带入站 id → 响应头 `X-Request-ID: 8292dc92…`（32 hex，nginx 生成）；带 `X-Request-ID: rid-probe-12345` → **原样沿用**；nginx 日志行 = `… "GET /ok HTTP/1.1" 200 11 0.002 s rid=8292dc92… ua="curl/8.5.0"`（状态码 + `$request_time` + rid 三项齐）。**这一半不等后端发版就已生效**（`add_header … always` 由 nginx 自己回显）。滚回：`bash …/apply-nginx-request-id-patch.sh --rollback`
  - **nginx 补丁的三个细节（动手前必读）**：ⓐ 线上与仓库副本**已分叉**（线上多一段 P0-3 注释）⇒ 不能整包覆盖，仓库副本已补回那段并加了断言防再次丢失；ⓑ compose 是 `./docker/nginx.conf:/etc/nginx/nginx.conf:ro` 的**文件级** bind mount ⇒ **必须就地 `cat > 同一 inode`，`mv` 换文件后容器仍读旧内容**（经典坑）；ⓒ 补丁用 `map $http_x_request_id $rid`（客户端自带就沿用、否则 `$request_id` 兜底）+ **`add_header X-Request-ID $rid always`**：nginx 自己回显 id ⇒ **不等后端发版即生效**，且 nginx 自产的 502/504 也带 id（那类请求到不了后端；后端中间件是"缺才补"不会重复）。`nginx -t` 失败 → `cat <备份> >` 回滚；**reload 前坏文件不影响在跑的 nginx**，无需重建容器
  - 验收：重启后仍能查到上一次运行期间的请求日志 —— 落 `/app/data/logs`（持久卷）即满足；**升级那一次**会把旧的 `/app/logs/agent-server.log`（容器可写层）一起丢掉，属预期（需要留档就重建前 `docker cp` 出来）

- [x] **P2-3 加 `/metrics` + 最小告警** —— ✅ 2026-09-24 已实施（`src/api/metrics.py` + `src/agent/utils/prom_metrics.py` + `custom_app` 接线 + `ModelTimeoutMiddleware` 与 6 个存储的锁埋点；`scripts/verify_metrics.py` **84/84**，连跑 3 次稳定 exit 0；**2026-09-24 已发版**）
  - **不自己写 exposition，直接挂 langgraph 自带的 handler**：`langgraph_api/api/meta.py` 的 `meta_metrics` 早就做了两件我们要的事 —— `?format=prometheus` 就是 `generate_latest()`（读的是**同一个全局 REGISTRY**，我们注册的指标自动出现在同一份输出里）、`?format=json` 给「队列/worker/连接池」快照（`Runs.stats` → `n_pending`/`n_running`/等待时长）。它原本只在设了 `MOUNT_PREFIX` 时才挂（`langgraph_api/server.py`；本仓**没设**，实测全树 grep 无命中）→ 由我们在 `custom_app.ROUTES` 加一行挂上。收益：不重复实现、不 fork 上游、上游加字段我们白得。代价＝依赖内部模块 ⇒ ① 解析**放惰性**（`_resolve_meta_metrics()`）：`import langgraph_api.api.meta` 会连带 `langgraph_api.config`，**要求 `REDIS_URI` 等配置齐**（缺了直接 `KeyError: Config 'REDIS_URI' is missing`），放在 import 期就把自检脚本/单测全挡在门外；② 解析失败**降级但明说**（输出首行 `# nl2sql: … 队列指标缺失`，JSON 带 `degraded`），**不是静默空面板**；③ `verify_metrics.py` ⑤ 段断言解析必须成功，上游挪模块时是它先红
  - 指标全表（15 个家族，统一 `nl2sql_` 前缀，避免与 `python_*`/上游撞名）：进程 RSS / **事件循环延迟 + 历史峰值** / **队列深度 `n_running`+`n_pending`** / pending 等待时长(max,med) / worker 槽位(max,active,available) / LLM 调用次数×三态 + 耗时直方图 / **SQLite 等锁 4 条**（直方图+进入次数+竞争次数+排队线程数）/ MCP server 注册表(ok,failed) / 进程子进程数 / 告警触发次数 + 当前是否在响 / 采样轮次(ok,error)。**上游的 `python_gc_*` 等仍在同一份输出里**（同源 REGISTRY）
  - **事件循环延迟怎么量**（清单点名的验收项之一）：采样任务 `await asyncio.sleep(interval)` 前后取 `loop.time()`，**睡过头多少就是延迟多少** —— 同步代码堵住循环时心跳必然被推迟。健康值毫秒级（自检实测平静轮 **0.009s**），刻意堵 0.4s → 量到 **0.451s**。`_max_seconds` 只增不减（面板能看"曾经堵到多少"）。⚠️ 别用 `time.monotonic()` 量"任务实际耗时"：那会把"排队等其它协程"也算成阻塞
  - **最小告警**＝"翻日志能看得见"的那一层，不是告警系统：7 条规则（事件循环延迟 / 跑满槽位 / 队列积压 / 内存 / LLM 失败 / 锁竞争 / MCP 失败），**连续 N 轮**成立才 `[alert] …` WARNING，**只在状态翻转时记**（持续超标不刷屏，自检钉死"4 轮只有 1 条"），恢复 `[alert-clear] …（此前连续 N 轮超标）`，同时落 `nl2sql_alert_active` 供面板显示。阈值全走环境变量（`NL2SQL_ALERT_*`，见模块内 `_rules()`），**`0` = 关闭该条**，非数字 → **退回默认**（静默关掉告警比报错更危险）。查法：`grep '\[alert\]' /app/data/logs/agent-server.log`（走 P2-2 的日志落点，带 rid 的日志文件同一个）
  - **两个"不写假数据"的约定**（都是防"指标看着正常、实际瞎了"）：① 采样拿不到某一路（非 Linux 无 `/proc`、langgraph 运行时拿不到）→ **不产出该指标**、也 `continue` 不评估该规则，而不是写 0（写 0 会让内存/积压告警**永不触发**）；② 事件计数型规则按**两次采样的差值**判断，且**首轮不把历史累计当新增**（否则进程一启动就把开机以来的几百次失败报成告警）——自检对这两条各有一条断言
  - **SQLite 等锁**（清单点名的第二项）用**代理锁**，不在上百个 `with _LOCK:` 点上逐个包：`agent/utils/prom_metrics.metered_rlock(store)` 返回一个透明代理（只实现 `__enter__/__exit__/acquire/release` + `__getattr__` 透传），改的是**锁的定义那一行**：`feedback` / `grants` / `eval_queue` / `trace_bind` / `thread_search`（模块级）+ `event_store`（实例锁）共 6 处。语义细节都写进类注释了：量的是 **acquire 的耗时（=排队）**而不是临界区时长（后者会把"正常但慢的查询"误报成锁竞争）；同线程可重入进入等待≈0 但仍记一次（判断竞争看 `contention_total`）；`__exit__` 只释放不计时；异常原样透传且**锁一定释放**；**计量失败只降级、绝不改变取锁语义**（真锁恰好 acquire 一次）。自检的负对照：同样一把**裸 `threading.RLock`** 竞争后计数**不动**（证明断言非恒真）
  - **LLM 耗时/失败率**埋在 `ModelTimeoutMiddleware` —— 它是主 agent 与子 agent 图**共同注册的模型调用唯一收口点**，按 ok/timeout/error 三态记次数与耗时。⚠️ 计时必须覆盖**同步 + await 两段**：异步 handler 的真耗时在 await 里，只量 `handler(request)` 会量成 0（自检断言 ≥0.12s）。埋点函数自带 try/except（指标坏了不能让问答跟着挂），且如实记 **"非超时异常原样上抛但记为 error"**（不是静默吞掉）
  - **暴露面（安全，刻意如此）**：`/metrics` **不在** auth 白名单 → 过 `AuthMiddleware` 的内部判定。自检用**真中间件**打三种请求：带 Cookie / 带 XFF → **401**；容器内网直连（内网 IP + 无 Cookie + 无 XFF）→ **200**（运维能取、用户取不到）。生产 nginx 只把 `/api/` 与 `/threads|runs|…` 转后端，`/metrics` 落到**前端**（`location /`）→ 外网路径根本到不了（已核实前端 `next.config.ts` 无 rewrites，Next 对未知路由返回 404）。断言写在 ⑧ 段，**不依赖人工记得**
  - **自检抓到的一个真 bug（值得单独记）**：`custom_app._lifespan` 里写了 `api.metrics.start()`，而函数体后段有 `import api.drain` —— **import 也是赋值**，会让 `api` 变成**局部名** ⇒ `UnboundLocalError`，**进程启动即崩**（排空/flush 的 try 包不到它）。修法＝模块区别名 `_metrics = api.metrics` 供 lifespan 用。这类 bug 只有"真跑一遍 lifespan"才暴露（⑧ 段正是这么写的），纯文本断言 `"api.metrics.start()" in src` 会一路绿灯
  - 残余（如实记）：① **没有面板**：本项只做 exposition + 告警日志，"面板"要用现成的抓取端（Prometheus/Grafana 或直接 `curl`），仓内不起时序库；② 指标是**拉取式**（pull），进程挂了就没人抓 → 可用性告警仍得靠外部探活（`/ok`）；③ `/metrics` 只对**容器网络内部**开放，宿主机上用 `docker exec … python -c urllib…` 取（见手册）；④ `nl2sql_process_children` 数的是 OS 子进程，MCP 是按调用短命起停的 → 该值在 0/1 间跳，**"配了几个库"看 `nl2sql_mcp_servers`**；⑤ 告警只落日志、不推送（邮件/IM 归 P3 的运维项）；⑥ 采样间隔在任务启动时读一次，改 `NL2SQL_METRICS_INTERVAL_SECS` 要重启（阈值不用，每轮现读）
  - 验收对照：**并发压测时能在面板上看到队列深度与事件循环延迟** —— 队列深度＝`nl2sql_run_queue_running/_pending`（来自 `Runs.stats`，自检用确定值断言 gauge 落点 + `?format=json` 里确有 `n_running`/`n_pending`）+ 事件循环延迟＝`nl2sql_event_loop_lag_seconds`（真阻塞/不阻塞两轮对照）。压测期间该看的还有：`nl2sql_worker_slots{state="available"}` 归零、`nl2sql_sqlite_lock_contention_total` 上涨、`nl2sql_llm_latency_seconds` 的 P95 —— 这三条正好把 [[concurrency-ceiling-10-run-slots]] 的结论变成可观测的

- [x] **P2-4 修 `sync_subagent_todos` 的 300s 天花板导致的 `active_queries` 永久 true** —— ✅ 2026-09-24 已实施（新增 `src/agent/subagents/pending_terminal.py` + `sync_subagent_todos` 两个放弃点交接 + `custom_app._lifespan` 起停；`scripts/verify_pending_terminal.py` **69/69**，连跑 3 次稳定 exit 0；**2026-09-24 已发版**）
  - **先纠正一处低估**：清单原文写的是"进度卡永久执行中"（显示问题），实际后果更重 —— `async_tasks[task].status` 终态**压根没落地** ⇒ 前端的自动续跑**永不触发** ⇒ **用户的图表/报告直接丢掉**。前端 `ChatInterface.tsx` 的 `terminalTaskIds` 只在 `async_tasks.status` 已是终态时才压掉卡片，**写没进去就压不掉**（"显示"这一半只是症状，不是全部）
  - **根因写清楚（409 是硬闸，不是超时）**：`langgraph_api/grpc/ops/threads.py` 对 `threads.update_state` 的判据是 **`run_count > 0` → 409**，与"等多久"无关。而主线程忙**恰恰是常态**：另一子任务完成后的自动续跑 run、用户正在发的消息、以及**并发压满 10 个 run 槽时排在 pending 的 run**（[[concurrency-ceiling-10-run-slots]]）⇒ 老代码重试到 `COMPLETE_WRITE_MAX_SECONDS = 300` 就 `break`，正是"并发一起来就丢终态"
  - **为什么不改成"重试更久"**：① 僵尸 run（`.langgraph_ops.pckl` 里恒 running，见 [[langgraph-inmem-pckl-zombie-runs]]）会让线程**永久**写不进去 —— 重试多久都没用，**只有重启才能清掉**；② 线程里长等会白占一个 run 槽。⇒ 唯一正确的形态是**把终态持久化、稍后（甚至跨重启）补写**。本项管的是这条链里**最要命的一段：终态压根没写**（终态写了但 `active_queries` 没清零是同一 watcher 路径的另一半，见 `sync-async-tasks-write-lost` / `frontend-task-card-freezes-restart-sync` 两条记忆）
  - **实施形态**：`pending_terminal` 表（落 `<AGENT_DATA_ROOT>/pending_terminal/pending_terminal.sqlite`，**持久卷**；2026-09-25 起从数据根目录归位到同名子目录）+ 后台补写器（`start_reaper`/`stop_reaper`，随进程 lifespan 起停，默认 60s 一轮，登记后立刻唤醒一轮）。watcher 到 300s 天花板、或**回退守护超限**（`REGRESSION_GUARD_MAX_*`）这两个**放弃点**时调 `_handoff_terminal_write` 登记一行就退出 —— 两个放弃点都改到了（自检断言 `_handoff_terminal_write(` 出现 ≥3 次，含定义；最终步骤渲染抽成 `render_final_steps` **两边共用**，不留第二份逻辑）
  - **单写者规则**（防重复续跑/重复失败汇报）：watcher 一旦登记就**退出**，此后只有补写器写 ⇒ 同一终态不可能被通知两次
  - **三条安全规则（改代码时别退回去）**：补写前先看两处现状 —— ⓐ 子线程**最新 run 的 `run_id` 必须还是 watcher 盯的那个**（被 `update_async_task` 重派发过 ⇒ 新 watcher 在管，**丢行不写**，否则会把活的任务打成终态，正是 [[main-run-stale-snapshot-clobbers-sync-card]] 那一类 bug）；ⓑ 该 run 必须**已是终态**（还在跑 ⇒ 丢行）；ⓒ 主线程 `async_tasks[task].status` 已是终态（别处写过 ⇒ 幂等丢行）。三条各有一条"**零写入**"断言，不是"少写一点"
  - **写路径不做第二套**：复用 `sync_subagent_todos._sync_update_state`（同一把 `_SYNC_WRITE_LOCK` ⇒ 与 watcher 串行；client 同样由 `LANGGRAPH_API_URL` 内部回环构造 ⇒ 与 watcher 鉴权上下文一致），且经 `asyncio.to_thread`（P1-14 口径）
  - **降级要留痕、不静默**：超过 `NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS`（默认 **6h**）仍写不进去 ⇒ 置 `abandoned` + **ERROR 日志**（点明"进度卡会停在执行中"并指向 `/metrics` 的 `nl2sql_run_queue_*` 与 `[alert] run_backlog` 去查卡死的 run），不是删掉了事。阈值 `0` = 永不放弃（与 P2-3 的"0=关闭"约定一致），非数字 ⇒ **退回默认**（自检有负对照）
  - **自检抓到两个红灯的根因，值得单独记**（都不是产品代码的毛病，但都会被误读成"补写器没工作"）：① 负对照里先在外部挂新 store、而 `_run_real_watcher` 自己会再挂一条 ⇒ 上一轮（恒 409）留下的行被当成"负对照也登记了"；② "启动即补写一轮"那条，等的是**行消失**而不是**写入发生** —— 线程起跑有调度延迟，行还在 ⇒ 循环立刻超时退出 ⇒ 紧接着 `stop_reaper` 在补写线程**还没进循环体**时置 `_STOP` ⇒ `while not _STOP` 直接为假，一轮都没跑。**等待条件必须钉在"写发生了"上**，注释已写进自检里
  - 验收对照（清单 §5.2「进度卡永久执行中」）：跑真 `_async_sync_loop`、让 `_sync_update_state` 恒抛真 `ConflictError(409)`、天花板压到 0.3s ⇒ **恰好登记 1 行**且载荷齐（主/子线程 id、**watcher 盯的 run_id**、步骤快照、原 task 字典），且**放手时一次 `async_tasks` 都没写进去**；随后补写器把 `async_tasks` 终态 + `active_queries=false` + 最终步骤一次写进主线程并**触发续跑（正好一次）**；失败终态则走**失败汇报而不走续跑**。另有真 `custom_app._lifespan` 跑一遍确认起停顺序（"补写器在 yield **之前**启动" = 重启后残留行启动即落地）
  - 残余（如实记）：① ~~未发版~~ **2026-09-24 已发版并实测补写器已启动**（`[pending-terminal] 补写器已启动`；本项必须**重启**才生效，补写器挂在 lifespan 上，`docker cp` 不重启 = 补写器根本没起）；② `abandoned` 的行**会留着**（7 天后由 `purge_old` 清），运维查 `uv run python -m agent.subagents.pending_terminal --status|--list|--replay [--dry-run]`；③ 落进 `abandoned` 意味着**主线程真的长期写不进去**，进度卡仍是"执行中"（这是**有意**不假装成功：宁可留 ERROR 让人去查僵尸 run）；④ 轮询间隔 `NL2SQL_PENDING_TERMINAL_INTERVAL_SECS` 在补写线程启动时读一次，改它要重启（阈值 `MAX_AGE` 每轮现读，不用重启）；⑤ 补写器是**后台线程 + 独立事件循环**，与 langgraph 自己的停机路径无关，`stop_reaper` 有 2s join 上限（join 超时也只是"这轮没跑完"，行在库里，下次启动重放）

- [x] **P2-5 各存储补 retention + 磁盘水位告警** —— ✅ 2026-09-24 已实施（新增 `src/agent/utils/retention.py` + `api/metrics.py` 三个 gauge 与 `disk_low` 规则 + `custom_app._lifespan` 起停；`scripts/verify_retention.py` **73/73**，连跑稳定；**2026-09-24 已发版**）
  - 无保留策略的：`trace_events`、`eval_queue`、`message_feedback.db`、`report/`、`large_tool_results/`（现只有 `trace_bind_store` 有 3000 上限）
  - **先定一条分离原则（后面所有默认值都服从它）**：**机器产生的中间产物默认清，人产生的东西默认不清**。清：`trace_events` / `eval_queue` / `large_tool_results/` / `conversation_history/` / 工作区 `tmp/`（30/30/30/30/7 天）。**默认关（天数 = 0）**：`report/`（用户下载过的交付物）与 `message_feedback.db`（含人工标注＝金标劳动）。⇒ 默认配置下**永远不会删掉用户能看见的东西**，要清只能靠 env 显式打开（打开后也是成对删，见下）
  - 为什么按**龄**而不是按大小：容量上限要跟"当前有多少活跃会话/多少库"联动，算错就是**删掉刚需数据**；按龄的语义是"这东西超过 N 天没意义了"，与规模无关、可解释、运维能自己拍天数
  - **七个目标的删法各不相同（不是一刀切 `DELETE`）**：`trace_events` 有 `created_at`（**epoch 浮点**）→ 直接比时间戳；`eval_queue`/`message_feedback.db` 存的是 **ISO 字符串** → 必须比字符串；`large_tool_results/`/`conversation_history/`/`tmp/` 是**目录里的文件** → 按 `mtime` 删**文件**、**目录本身留着**（目录是运行期约定路径，删了会让当期会话在写的时候炸；顺手清掉被清空的子目录，不留一堆空壳）；`report/` 是**文件 + `report_owner` 账本**（见下）
  - **时间戳口径的坑（本项目已踩过一次，写死在自检里）**：`eval_queue._now()` 与 `feedback._now_iso()` 写的是 `datetime.now(timezone.utc).isoformat()` → `2026-09-24T13:32:09+00:00`（**T 分隔 + 偏移**）。若 cutoff 用 `strftime("%Y-%m-%d %H:%M:%S")`（空格分隔），**' '（0x20）< 'T'（0x54）** ⇒ 任何**当天**的时间戳都比 cutoff 大 ⇒ 一条都删不掉，而且**看着像"没有过期数据"**。⇒ cutoff 一律 `isoformat(timespec="seconds")`（同样以 `T` 开头，逐位可比、且秒精度对齐）。自检 ④ 段专门有一条**负对照**：`'2026-09-24 05:51:40' < '2026-09-24T05:51:40+00:00'` 断言为真 —— 证明"用空格会漏删"不是猜的。⚠️ 顺带：`trace_events` 是 epoch 浮点，**不能**拿它当"全仓都用 ISO"的证据，两边分开处理
  - **`report/` 必须文件与账本成对删**（否则读侧出鬼）：`report_owner` 账本管读侧授权（[[report-chart-ownership]]），只删文件会留下"有归属但文件不在"；反过来**按"文件不存在"去清账本行是错的** —— 别的用户/别的**工作区**可能有同名文件（报告名不含用户，靠唯一文件名区分，见 [[report-filename-charset-contract]]）。⇒ 实现是**只删这一轮真删掉的那批文件名**（新增 `grants.delete_report_owners(filenames)`，精确 IN 匹配 + 同锁提交）。自检 ⑥ 段三条负对照钉死：别人的账本行没被误删、另一工作区同名文件没被碰、1 天前的报告留着
  - **不碰的东西（有意）**：`annotations` 表（人工标注，只能人工清；⑦ 段断言"打开 feedback 清理也只删打分行"）、`trace_bind_store`（本来就有 3000 上限）、langgraph 自己的 checkpoint/pckl（改这些等于动运行时）
  - **磁盘水位告警**：`nl2sql_disk_free_bytes` / `nl2sql_disk_used_ratio`（label = 数据卷路径）+ `nl2sql_data_bytes`（label = 区域，**数据来自清理轮缓存、非实时**——见下条）。规则 `disk_low`：env `NL2SQL_ALERT_DISK_FREE_PCT`（默认 10，**0 = 关闭该条**），**连续 3 轮**才响（单次抖动不报），判据是**剩余**比例（越小越危险）而不是已用 —— P2-3 的 `_rules()` 老约定原样沿用
  - **量的是 `AGENT_DATA_ROOT` 那个卷，不是 `/`**：容器里数据卷是独立挂载（[[agent-data-root-external-base]]），看 `/` 的剩余量没有意义（`/` 是镜像层，几乎不动）；量错卷会得到一个**永远不响**的告警 —— 这类"配了但恒定不触发"的告警比没有更坏
  - **采样绝不遍历目录**（性能红线）：`/metrics` 是 10s 一轮的采样任务，遍历 `large_tool_results/` 这类目录在 P1-14 口径下属于**阻塞事件循环**。⇒ 区域占用由**清理轮**（默认 1h 一轮、跑在后台线程）算好放进 `_LAST_SIZES` 缓存，采样只读缓存；缓存为空时**不写 0**（写 0 = 面板显示"这个区域是空的"，而实际是"还没量过"），自检 ⑧ 段有这条断言
  - **维护线程与 P2-4 同一套形态**（照抄，不发明第二套）：后台 `threading.Thread` + 自己 `_STOP` 事件 + `start_maintenance()`/`stop_maintenance()` 挂在 `custom_app._lifespan`（**`yield` 之前起、停维护之后停**），`start` 幂等、停了能再起（lifespan 反复进入不炸）。⚠️ 老规矩：**挂着 lifespan 的东西 `docker cp` 不重启 = 根本没起**（[[p2-3-metrics-and-alerts]] 里同款坑）。**启动即跑一轮**（`last_run` 有记录，不等人来问）—— 重启是唯一可靠的回收时机
  - **整轮清理加一把计量锁** `metered_rlock("retention")`：防"运维手动 `--run`"与后台线程**同时删同一批文件**（同一批里删两次虽然幂等，但会让 `freed_bytes` 统计错乱、也会并发压 SQLite）
  - **同步实现、跑在后台线程**（不是 async）：文件遍历 + `DELETE` 绝不能落在事件循环上（P1-14）。⑨ 段有一条源码断言钉住"清理是同步实现"
  - **`_unsafe_root()` 安全闸**（这条是**防自伤**，比功能重要）：路径来自工作区根（2026-09-25 起 = `<AGENT_DATA_ROOT>/workspace`；此前来自可被写坏的 `workspaces.json` 注册表），若它被指到 `/` 或家目录（今天只可能因 **env 配错**），按龄递归删就是灾难。⇒ 拒绝文件系统根与家目录，**一个候选目录都不返回**。⚠️ 这条闸是被自检逼出来的：**本机工作区曾指向真实开发目录**（`D:\nl2sql_data\...`），第一版自检差点在开发机上真删东西 ⇒ 现在自检自带 `_assert_inside_work()` + 工作区桩（`_install_ws_stub`），**改这块代码时别把那层护住删了**；`scripts/verify_workspace_pinned.py` §④ 另有一条负对照（把 `get_workspace_manager` 打桩成家目录 ⇒ 断言返回空）
  - **CLI（运维入口）**：`uv run python -m agent.utils.retention --status | --run [--dry-run] | --vacuum <db>`。⚠️ `--vacuum` 是**唯一真正回收磁盘**的动作 —— `DELETE` 只把页标成空闲，**文件不缩**（自检对这点有断言，不假装"删了就省了盘"）。默认**不自动 vacuum**（它会重写整个库、期间占额外空间且要独占）
  - **自检抓到的一个真坑（跨平台，值得单独记）**：Windows 上 `subprocess.run(..., text=True)` 按 **GBK** 解码子进程 stdout ⇒ 本模块 CLI 输出的是**中文 JSON** ⇒ `UnicodeDecodeError` + `stdout=None` ⇒ 两条 CLI 断言假红（`the JSON object must be str, bytes or bytearray, not NoneType`）。⚠️ **不是产品问题**，修法是显式 `encoding="utf-8", errors="replace"`。凡自检里要读子进程输出的，都得带这一对参数
  - 验收对照（清单点名的"各存储补 retention + 磁盘水位告警"）：① 七个目标各有真删/不留不该删的对照，且 `dry_run` 一律**零副作用**（每种都有负对照断言"什么都没删"）；② `report/` 与 `feedback` 默认关闭有断言（防以后有人把默认改开）；③ `disk_low` 规则在 `REGISTRY` 里、阈值走 env、连续 3 轮才响、采不到不写假 0；④ 真跑一遍 `custom_app._lifespan` 确认起停顺序 `['start','inside','stop']`；⑤ 真 sqlite 文件上跑 `trace_events`/`eval_queue`/账本删除（非 mock）
  - 残余（如实记）：① ~~未发版~~ **2026-09-24 已发版并实测第一轮就清了东西**（`[retention] 维护线程已启动（间隔 3600.0s，总开关=True）` + `workspace_tmp：已清 9 项（7 天前）`；本项**必须重启**才生效，维护线程挂在 lifespan 上，`docker cp` 不重启 = 清理器根本没起，而 `/metrics` 上看起来"没配置"）；② **cleanup 一轮一轮删会留空洞**，磁盘只在 `--vacuum` 后才会缩 —— 长期跑建议 `NL2SQL_RETENTION_*` 之外**再排一条定时 `--vacuum`**（未做任务，同 P2-6 一起排）；③ **日志没有 retention**（P2-2 只做了 7 天轮转、不做删除）——日志现在是 `agent-server.log` + 轮转文件，**也占盘**，归本项之外的账；④ 区域占用是**缓存值**（最多滞后一个间隔），不是实时；⑤ 阈值/天数 env 每轮现读**不用重启**，但 `NL2SQL_RETENTION_INTERVAL_SECS` 在**线程启动时读一次**，改了要重启（与 P2-3/P2-4 同一约定）；⑥ `report`/`feedback` 一旦打开就是**不可逆删除**（没有回收站）—— 建议先跑 `--run --dry-run` 看清单再决定

- [ ] **P2-6 定时备份（含加密  暂不实现）**
  - 对象：`auth.sqlite`（授权与归属，**须加密**）、`db_config.json`/`model_config.json`、`pg_dump`
  - 现状：文档只给建议，**无自动任务**

- [x] **P2-7 补一次真实并发压测**（2026-09-24 已跑，生产 25.64；报告 `docs/生产就绪度评估/2026-09-24-P2-7并发压测报告.md`）
  - 目标曲线：N 用户 → 队列深度 / P95 延迟 / 内存 / MCP 子进程数
  - 这一步同时校验 §1 的「3~4 并发用户」推导值
  - **这一步也是 P3-1 / P3-2 的决策依据，建议早排**
  - 口径：生产容器内直连 `localhost:2026`（**绕过 nginx 与鉴权**），库 `witops`，固定 4 问，档位 1/2/4/6 **并发问数** × 2 轮 = 26 问数；生产**只有 2 个账号** ⇒ 并发轴是「并发问数」不是「并发用户」；`lag-abort=5.0s` 未触发
  - 实测曲线：1 档 2/2（p50 6.2s）· 2 档 4/4（p50 6.1s）· 4 档 8/8（p50 8.3s，max 50.8s）· **6 档 8/12（p50 7.3s，max 248.8s，峰值在跑 10.0、余槽 0.0）**；RSS 740→938MB，事件循环 lag ≤ 17ms，全程零排队
  - **关键归因（推翻「加槽位就好了」的直觉）**：6 档那 4 个失败的**直接原因不是排队/超时/内存/checkpointer/MCP**，而是 `openai.APIConnectionError`（模型调用连接失败，SDK 重试耗尽）×3 + 1 例 `settle_timeout`（主 run `success` 但 `next` 悬挂 248s 不推进）。**出站通道本身健康**：容器内并发打 DeepSeek `/v1/models`，2/6/12/20 并发全成功、0.08~0.23s、零异常 ⇒ 不能归因给「网络/限流」（限流会是 429 `RateLimitError`），也排除本服务侧（同期 500=0、`connection is closed`=0、自愈=0、lag 17ms）
  - ⇒ **P3-1 / P3-2 都不急**（打满 10 槽时服务端零排队、RSS 仅 +170MB、lag 17ms）；**应先做 P3-4（LLM 并发闸+退避）并补 P2-11 的日志**，否则加槽只会让更多并发模型调用去踩同一个坑。10 槽实测支撑 **3~6 个并发问数**，`concurrency-ceiling-10-run-slots` 的「3~4 并发用户」推导**成立**
  - 顺带：**P2-10 修复在生产真实并发下站住了**（重启后 `connection is closed` 0 次 / `status 500` 0 次 / 自愈 0 次，含峰值 10 并发 run）
  - 口径 caveat（别误用这些数）：① 采样 2s、档位 1/2 只有 7/6 个样本 ⇒ **低档峰值基本不可信**；② `nl2sql_process_children` 每 10s 才更新一次而 MCP 子进程随调用生灭 ⇒ **MCP 子进程峰值测不出来**（4 档采到 5、6 档采到 0），留给 P3-6 单独量；③ `peak_pending` 全程 0 ≠ 从不排队（本次从未超过 10 个并发 run）；④ JSON 的 `round` 字段是 `None`（工具缺陷）；⑤ 子 agent 线程清理仍需核对
  - 连带发现（与压测无关但更紧急）：前端「部署 URL」被配成 `http://192.168.25.64:2026`，而 `:2026` 只发布在**回环**上（09-23 关旁路门时改的）⇒ 浏览器 `Failed to fetch`、UI 报「尚未配置模型 / 加载对话列表失败」；修法=改填 nginx 入口 `http://192.168.25.64:8080`（记忆 `frontend-deployment-url-must-be-nginx-entry`）

- [x] **P2-8 目标库访问补三件：连接池 / statement timeout / 结果行数上限**（2026-09-24 完成，**其中「连接池」判定为不做**）
  - **落点全图（先用探查 agent 摸清再改）**：打目标库共 3 条通道 —— ① **dbmcp 直连**（`src/mcp_server/db_mcp_server/db/db_server.py:111` `run_sql`，引擎 mysql/postgres/sqlite/clickhouse 可达，mssql/duckdb/oracle/presto/snowflake/bigquery **不在 `_RUNNER_REGISTRY` 里、不可达**）② **wren 语义层**（代码在**第三方包** `.venv/.../wren/`，本仓只传连接字典）③ **HTTP API 复用 ① 的引擎**（`api/feedback_annotation.py:236` 预览试算 + `api/db_config.py` 连通性测试）
  - **③ 连接池：不做，且这不是"漏了"**。判据是硬事实：`langchain_mcp_adapters/tools.py:463-466` 在无 session 时**每次工具调用**都 `async with create_session(...)` ⇒ stdio 传输下**每调用起一个新子进程、调用完就关**。池的寿命 == 一次工具调用 ⇒ 在 runner 里加 `pool_size` 是**死代码**。真做池的前置是「MCP 会话复用」（改 `tools/mcp_tool.py:98` 的 `MultiServerMCPClient` 用法），而那正是「每请求 5~12 个子进程 / 2-4s 冷启动」那个成本中心 —— 属 P3 结构项，**且要先有 P3-6 的 MCP 子进程实测数据**。已记入 P3-6 的输入，不在这里硬做
  - **① statement timeout：已做**（新文件 `src/mcp_server/db_mcp_server/db/limits.py`）。默认 **240s**，env `NL2SQL_DB_STATEMENT_TIMEOUT_SECS`（`0`=关、非数字退回默认）。**240 不是随手取的**：工具超时是 300s（`agent/utils/path_resolver.py:_TOOL_TIMEOUTS`），必须让**数据库先中止并回明确错误**，否则本仓先超时、把一条还在烧目标库资源的查询留在服务端。落点：postgres `SET statement_timeout`（毫秒）、mysql **按序试两个候选**（MySQL `MAX_EXECUTION_TIME` 毫秒 / MariaDB `max_statement_time` 秒 —— 写死一个必然在另一边报 Unknown system variable，全失败只记 warning 不让查询失败）、clickhouse 走 client `settings.max_execution_time`（用户的 `extra_config.settings` 优先）、**sqlite 刻意不做**（无此机制）
  - **② 结果行数上限：已做，而且发现原来的判据是可以绕过的**。改造前唯一一处是 `_apply_default_limit`，判据 `"LIMIT" not in sql.upper()` ⇒ **模型自己写 `LIMIT 999999999` 就原样下发、全量进内存**（这正是「把结果拉进 1200m 容器」的路径）。现在两层：
    - **文本层**：结尾裸整数 LIMIT → **收敛**到 cap（只换数字、保留原大小写）；全文无 LIMIT 且是 SELECT/WITH → 追加；`LIMIT n OFFSET m` / 注释或字符串里出现 LIMIT → **原样放行**（改错会变语法错误，不冒险）。顺带修掉旧嗅探的两个假命中：`v_limited`、`LIMITED` 之类子串不再误判
    - **事实层（真正的兜底）**：`run_sql` 取数后按**行数硬截断**，并回 `truncated` + `truncated_note`（带真实原始行数）⇒ 文本判据绕得过去的形态（`LIMIT 4000 OFFSET 0`、子查询 LIMIT）也拦得住。**文本可以被绕过，行数绕不过去**
    - 入参 `limit` 当成**不可信输入**：`None/0/负数/非数字 → 1000`，`>硬上限 → 硬上限 10000`（env `NL2SQL_DB_MAX_ROW_LIMIT`，与语义层 `wren` 的 1000/10000 同值）。`get_db_info` 那条**唯一绕过注入**的入口也按行数封顶
    - ⚠️ 收敛默认**关**（`clamp_existing=False`），只有 dbmcp 通道开：`api/feedback_annotation._run_preview:634` 传进来的是**语义层已按连接器上限处理过的物理 SQL**，压小它会让「口径试算行数与线上工具不一致」——那是那个功能的立身之本（已加负对照断言钉死）
  - **顺带修掉一个真 bug**：postgres / sqlite runner 用 `args.sql.strip().upper().split()[0] == "SELECT"` 判类型 ⇒ **`WITH ... SELECT`（CTE）被判成非查询、走 commit 分支、查询结果整份丢掉**（返回 `rows_affected`）。改成以驱动给的 `cursor.description is not None` 为准（`WITH ... INSERT` 那种反向误判也一并修掉）
  - 验收：`scripts/verify_db_limits.py` **47/47**（含 e2e：临时 sqlite 库 → 真走 FastMCP `run_sql` 工具，5000 行表真取数真截断；负对照=`LIMIT 7` 不标截断、默认不收敛 OFFSET 形态）
  - **未验**（如实记）：postgres / mysql / clickhouse 的 statement timeout **只验证了"生成的语句对不对"，没有真下发到那三种库**（本机无这三个库在场）⇒ 需在生产或 dev 环境各打一条慢查询确认
  - 顺带记录：`db/config.py:112` 的 `config.update(cfg.extra_config or {})` 会把任意键变成**驱动的 connect kwargs**（`connect_timeout`/`sslmode` 现成可用），但也意味着**别把 `statement_timeout` 塞进 extra_config**（psycopg 会当无效连接参数直接报错）——这也是超时走 env 而不走那个通道的原因

- [x] **P2-9 `langfuse_span` 的 process_data dump 加上限或默认关**（2026-09-24 完成，**清单描述与生产实测不符，按实测改的**）
  - **纠正清单的前提**：清单写「每个工具调用一份 ≤8MB JSON、默认开启」——实测**两条都不准**：`NL2SQL_PROCESS_DATA_DUMP_OUTPUT` **默认就是关的**（只落 `input` + 一个行数标记，不落查询结果），而 8MB（`_DUMP_MAX_BYTES`）只作用在 `input` 一侧；生产 `nl2sql_process_data/` 共 **20MB / 3698 个文件 / 243 个线程目录、最大单文件 111KB** ⇒ **8MB 那个帽子从未生效**。所以「撑爆磁盘」不成立，也不该去调小那个上限
  - **真正的缺口**：`nl2sql_process_data/` **不在保留策略注册表里**（`retention.py` 的 `large_tool_results` / `conversation_history` / `tmp` 都在，它不在）⇒ **只涨不跌**，而且 `/metrics` 的区域占用里也看不见它（`measure_areas` 不量它）。对比：同目录 `large_tool_results` 46MB、`report` 2.7MB
  - 修法：登记成机器数据目标，默认 **30 天**（与 `large_tool_results` 同族；env `NL2SQL_RETENTION_DAYS_NL2SQL_PROCESS_DATA`，`0`=关），复用既有的 `_purge_workspace_dirs`（按 mtime 删文件 + 清空后子目录 `rmdir`）并纳入 `measure_areas` ⇒ 从此可见、可清
  - 验收：`scripts/verify_retention.py` **76/76**，新增 3 条断言（45 天前的删 / 负对照 1 天前的留 / 清空后 `thread_id` 子目录也删掉）
  - **未做**（判定为不需要）：调小 `_DUMP_MAX_BYTES`（帽子从未生效，改它只会让 debug 载荷变残）；把 dump 默认关掉（它本就只落 input，且是排查依据）

- [ ] **P2-10 checkpointer 单例 `_cm` 跨线程互关（2026-09-24 生产 P0，已热修并验证；待随发版固化）**
  - 症状：发版后**所有问数全挂**（每条 6s 内 `error`、`POST /threads/{id}/state` 瞬时 500），全是 `psycopg.OperationalError: the connection is closed`；**重启只能重置一次、第一波 run 就再中毒** —— 不是"连接死了"而是那个线程**永久报废**
  - 根因：`checkpointer_factory.py` 的 `checkpointer` 是模块级单例、老写法 `self._cm` 是**共享可变槽位**，而 langgraph_api 是**每线程各调一次** `get_checkpointer()`（`_adapter.py:65` threading.local + `:274` hasattr 守卫 ⇒ **只建不改**）。新线程 `__aenter__` 覆盖槽位 ⇒ 旧 `@asynccontextmanager` 失去最后引用被 **GC** ⇒ 终结器执行 `from_conn_string` 的 `finally: close()` ⇒ **关掉上一个线程仍在用的连接**（本地已最小复现；全程**无人**调用 `exit_checkpointer`，日志里也没有任何关停标记 ⇒ 与"优雅停机/postgres 重建"都无关）
  - 修法（两层，缺一不可）：① `_cm` 改 `threading.local()`（各线程各管各的，断掉互关；SQLite 的 `_DynamicCheckpointer` 同款）；② `_SelfHealingPostgresSaver` 在 `_cursor` 里查 `conn.closed`，死了就重连 + `setup()`（兜住**任何来源**的关闭；健康检查放 `self.lock` **之前**，对连接池跳过）
  - 验收：单条问数 6.5s 成功（修前 error 6s）；2 并发 × 2 轮 **4/4 成功**（p50 6.2s / max 8.1s）；重启后 `connection is closed` **0 次**；自愈日志 **0 次**（⇒ 病因断了，兜底未被用到）；能力检测与改前一致（`adelete_thread` 仍在 ⇒ `DELETE /threads` 不受影响）
  - 残余：① 目前是**容器内热修（`/app/src` 非 bind mount）+ 本地工作区改动，未提交、未随发版固化** —— 下次发版必须带上这份文件；② **同类模式排查已做（2026-09-24 只读审计 + 本人逐条复核，结论见下）**；③ 老注记「postgres 容器重建后重启 langgraph-api」仍成立（自愈现在也能兜住，但重启仍是首选）；④ `_adapter.py:274` 的"只建不改"是上游设计，**本文件两层修法必须在**否则同类关闭一律永久报废
  - ② **同类模式审计结论（2026-09-24）**：**Python 变量/连接层没有同型实例**（`event_store`/`trace_bind_store`/`grants`/`feedback.store` 是「单连接 + 一把读也进锁」且 `close()` 对 `_shared` 显式 no-op；`offload._long_pool`/`langfuse_client._client` 从不 close；`path_resolver._SyncToolPool._recycle_locked` 是**先摘槽后关**的正确形态；`wren_plan._ENGINE_CACHE` 从不 close）⇒ 原型的判据 4（存在「另一线程销毁活资源」的路径）在这些地方**不成立**。
    同型的实例全部落在**「拿磁盘目录当单槽」**的三处，且都是「目录名只含 ref / 键含 src」的错配：
    1. **`utils/semantic_db.py:264`（高）**：物化目录 `_cache_dir(db, ref)` 只由 (db,ref) 决定，而进程缓存键是 `(db, src, ref)`、marker 是 `f"{src}|{ref}"`（已复核代码属实）。`semantic_project_path`（override 命中，src 可非空）与 `materialize_semantic_ref`（`src=""` 硬编码，`semantic_db.py:342`，被 `api/experiment.py:522` 的 run 预检调用）**能指向同一个目录、却属于两个不同 key** ⇒ 后者会 `rmtree` 掉前者正在服务的那份语义库再换另一仓库的内容 ⇒ 问数报 `target/mdl.json` 缺失 / A/B 结论污染。**且 `rmtree` + git archive + move 全程在 `_semantic_override_lock` 之外**（该锁只保护缓存 dict），同 key 并发（API 预检 + worker 子进程，跨进程）也**没有串行化** ⇒ 这是**无条件成立**的一半风险。
       ✅ **① 已实施（2026-09-24，未发版）**：
       - **目录名含 src**：`_cache_dir(db_name, src, ref)` 的目录名里加了 `_source_tag(src)`（src 空 → `base`，非空 → `<basename[:24]>-<sha1(resolve)[:8]>`）⇒ 与缓存键 `(db, src, ref)`、marker `f"{src}|{ref}"` 三者一致，两个来源各用各的目录
       - **marker 增第二行 = 项目相对位置**：`git_archive_materialize` 对「仓库子目录形态」（`<repo>/semantic/<db>/`）返回的是 `dest_root/<rel>` —— 老的快路径判据只认「根下就是项目」，根本命中不了这种形态 ⇒ **每次调用都重 archive**（这是个既有的隐性 bug，本次一并修掉：现在 marker 记下 rel，快路径按它复查项目标记）
       - **per-key 物化锁**：`_materialize_key_lock(key)`（照抄 `skills_versioning._key_lock` 的形态，dict 用一把小 guard 锁保护），慢路径进锁后**复查**一次缓存/marker；同 key（含跨进程场景下同进程内的两个调用）只物化一次
       - **暂存 + 原子换入**：`_materialize_now` 全程在 `.stage-*`（mkdir 占位）里做，写完 marker 再 `_install_staged`（dest→trash、stage→dest，失败回滚），失败不留半成品、不动物化过的现场
       - **Windows 特有坑**：目标被打开时 `os.replace` 报 `WinError 5`（Linux 不会）⇒ `_rename_retry` 8 次线性退避
       - 验收：`scripts/verify_semantic_materialize.py` **50/50**（三连跑全绿；含真 git archive 的「仓库子目录 / 独立仓库」两形态、清进程缓存后的 marker 快路径、换 ref 不动旧目录、「两来源物化后先来的那份逐字节未动」核心回归、失败不动现场、并发同 key 只物化一次且轮询期间从未看到半成品缓存根）
    2. **`utils/skills_versioning.py:334`（中，条件成立）**：`skill_refs/<safe_ref(ref)>` 只含 ref，而 `key = f"{src}@{ref}"`、`_key_lock(key)`、marker 都含 src。同进程内 `main_agent.py:167`/`nl2sql_agent.py:83` 用 env 的 `SKILLS_REF=<path>@<ref>`（**src 非空**），`api/experiment.py:509` 是 `materialize_skills_ref(ref)`（**src=""**）⇒ 两把不同的锁 + 同一个目录 ⇒ 前者正在用的 skill 目录被后者删掉再覆写。**触发条件是生产 `SKILLS_REF` 用 `path@ref` 形态**（若一直是裸 `<ref>` 则两处 src 都是空、键一致，退化为良性）；本机读不到生产 env，故**条件未验**。参照物：同族的 `prompt_versioning.py` 把版本指纹写进**目录名**（键与目录名一致）⇒ 没这个坑。
       ✅ **② 已实施（2026-09-24，未发版）**：
       - **目录名含 src**：新增 `_ref_dir_name(ref, src)` = `<src标签>_<safe_ref(ref)>`，`_src_tag` = src 空 → `base`、非空 → `<basename[:24]>-<sha1(resolve)[:8]>`（与 `semantic_db._source_tag` 同语义，刻意重复——两处 `_safe_ref` 也各一份）。键含 src、`_key_lock` 含 src、**目录名现在也含 src** ⇒ 三个来源一致，两个来源各用各的目录，谁也不会删到对方正在服务的那份
       - **⚠️ 两侧必须同源**：`effective_skills_sources` 返回的那个串**不是** CompositeBackend 的挂载名（挂载键是前缀 `/offline_experiment/skill_refs/`，见 `main_agent.py:159`），而是**按目录名拼出来的物理路径** ⇒ 改目录名必须同步这一处，否则症状是「物化成功但技能读不到」。现在两侧都由 `_ref_dir_name` 生成，测试里用真物化目录 + 真读 `SKILL.md` 双向钉死
       - **顺带去掉「就地重建」**：原实现是 `rmtree` 掉活目录再原地 archive/copytree ⇒ 运行中的 run 读技能目录会读到残缺内容（同 ③ 的形态）。现在物化全部在 `.stage-<名>[-n]`（用 `mkdir` 占位保证唯一）里做完，再 `_install_staged` 换入（dest→trash、stage→dest，第二步失败把 trash 换回来）；任何失败都 rmtree 暂存并**返回 None**（不能把已被清掉的 stage 路径当成功结果返回）
       - **Windows 特有坑（同 ③）**：目标被打开时 rename 报 `WinError 5`（Linux 不会）⇒ `_rename_retry` 做 8 次线性退避重试（最坏 ~0.7s）
       - **触发条件仍未验（如实记）**：生产 `SKILLS_REF` 到底是不是 `path@ref` 形态，本机读不到那个 env —— 但**修好之后两种形态都不会互删**，所以这个未验项不再有风险含义
       - 验收：`scripts/verify_skills_ref_materialize.py` **55/55**（三连跑全绿；真 git 仓库两种形态 + 真 archive，含「两来源物化后先来的那份逐字节未动」这条核心回归、VFS 路径真能读到 `SKILL.md`、换入失败回滚、并发同 key 只物化一次且轮询期间从未看到半成品）
    3. **`api/wren_semantic.py:2076` + `:435`（高，疑似）**：`git_repo.pull_ref(活项目目录)` 与 `_run_wren(project_path, "context","build")` 都是**就地重写**正在服务的那份 wren 项目（该文件自己的注释写着「每次工具调用新起的子进程都重读 `target/mdl.json`」）⇒ checkout/build 窗口内并发问数读到缺 `target/mdl.json` 或半写；build 被打断（长池任务被停机打断 / 磁盘满）则**目录永久停在无 mdl.json 状态**，重启时 wren 直接拒绝启动。同文件的 `_adopt_git_into`（`:710-715`）用的是「`os.rename` 备份 + `os.rename` 换入」的正确形态，可作为改法参照。
       ⚠️ **与原清单的差异（必须纠正）**：原来把它写成 wit-mdl 事故（`wrenai_WIT` 因缺 `target/mdl.json` 拒绝启动）的**候选解释**，复核后**站不住**：① 事故发生在**发版重启**时，而发版不跑 build；② 该库的 `target/` 从未被提交、拉取路径本就不动它；③ 实测单次 build 只要 5.697s（151 个模型），"被停机打断"的时间窗与事故形态都不吻合。⇒ **该事故至今原因未知**，与本项解耦。本项的价值在于「消除就地重写的窗口 + 同库写操作串行化 + 失败绝不留下一个没构建的库」，**不是**解释那次事故。
       ✅ **③ 已实施（2026-09-24，未发版）**：
       - 「更新」（`git_pull`）→ `_pull_swapped`：整目录交换（**两次同盘 `os.rename`**）+ 每条入口按**解析后项目路径**取 `asyncio.Lock`；`prepare` 在副本里跑 `pull_ref`，失败**不换入**（线上目录一字节未动）；副本里若没有 `target/mdl.json` 就地补构建，补构建失败**不阻断更新**但把风险写进文案
       - 「重建」（`build_project`）→ `_stage_build_replace`：副本里 `context build`，成功后 **`os.replace` 单文件原子替换** `target/mdl.json` ⇒ **零窗口**（整目录交换做不到：两次 rename 之间目录名会短暂空缺）。拷贝时排除 `.git`/`target`（构建只读源文件、只写那一个产物），少拷几十~几百 MB
       - 「接管」（`_adopt_git_into`）→ 取锁后委托 `_adopt_git_into_locked`（顺带**把构建提到 `os.rename` 之前**：原来是先换目录再构建，换入的目录会有一段时间没有 mdl.json）；`generate_models` / `save_knowledge` 同样进同一把锁
       - **窗口实测（Windows 本机）**：整目录交换 = 紧循环 874 次轮询命中 8 次读不到、最长一次连续 **5.093ms**；单文件替换 = **1386/1289 次紧采样 0 次落空、0 次坏 JSON**（三次运行均如此）。
       - **Windows 特有坑（已处理）**：目标文件被别的进程打开时 Windows 的 rename 直接 `WinError 5`（紧循环读必现；Linux 无条件成功）。⇒ `_replace_target_mdl` 对 `PermissionError` 做 8 次线性退避重试（最坏 ~0.7s，Linux 上一次就成功）；`_swap_in` 的两次 rename **不重试**（重试会拉长那个本就存在的空缺窗口），代价是 Windows 上极不走运的并发读者会让一次「更新」报「换入失败，已回滚」（**线上无任何改动**，Linux 生产不受影响）
       - **仍存在的窗口（如实记）**：「更新」走整目录交换，两次 rename 之间目录名短暂空缺（Windows 实测 ~5ms；Linux 上只有目录项更新、远小于此，**未实测**）。窗口内并发问数会看到该库的 `target/mdl.json` 不存在 ⇒ wren 硬失败一次。要彻底消除得上「目录级 symlink 切换」或「MCP 会话复用」（P3-6），代价远超收益，**不做**
       - 验收：`scripts/verify_semantic_staging_swap.py` **86/86**（三连跑全绿；真实 git bare origin + 浅克隆，假 wren 二进制驱动真文件系统；含脏树被拒、stash 跨换入存活、补构建失败、并发两次构建区间不重叠、①号原语的 `mkdir` 占位与换入回滚）
  - **修法（①②③ 三项均已实施，2026-09-24，未发版）**：① `semantic_db` 加 **per-key 物化锁** + 把 `src` 纳入目录名（marker 补记项目相对位置，见上）；② `skills_versioning` 同样把 src 纳入目录名（并去掉「就地重建」）；③ ✅ wren 活项目目录改为「暂存副本里做完再安装」。三处共用同一套形态：**目录名/键/锁三者身份一致 + 暂存后原子换入 + Windows rename 退避重试**。验收脚本：`scripts/verify_semantic_staging_swap.py` **86/86**、`scripts/verify_semantic_materialize.py` **50/50**、`scripts/verify_skills_ref_materialize.py` **55/55**（均三连跑全绿）—— **改这三条链路后必须重跑对应脚本**
  - ④ **`src/agent/config.py` 死代码已清（2026-09-24）**：原 `MONGODB_URI`/`_mongodb_client`/`CHECKPOINTER`/`STORE` 四个模块级东西**零引用**，副作用是**导入即执行**，而且 ① `MONGODB_URI` 是**硬编码凭据指向外网主机**（`mongodb://root:123456@39.100.100.28/...`），`src/` 是整包发版内容 ⇒ **凭据随发行包发出去**；② `pymongo`/`langgraph.checkpoint.mongodb` **不在 `pyproject.toml`** ⇒ `import agent.config` 本来就**必然 ModuleNotFoundError**（叠加 `agent.env_utils` 也不存在、唯一 importer 是按设计文档「E3 未接线」的 `sandbox_setup.py`）⇒ 删掉是**纯改善、零行为变更**。checkpointer 的真实入口是 `checkpointer_factory.py`（`LANGGRAPH_CHECKPOINTER`）。**整文件删除留作可选清理**（需先确认没有字符串路径加载它；删文件在本仓有「幽灵模块」前科，见记忆 `release-rmrf-exposes-missing-files`）
  - 复盘：`docs/生产就绪度评估/2026-09-24-checkpointer单例跨线程互关事故复盘.md`

- [x] **P2-11 高并发下模型调用连接错误不可归因**（2026-09-24 完成，与 P3-4 合并做；**未发版**）
  - 问题：`openai.APIConnectionError` 是**壳**，真正的原因在 `__cause__`（`httpx.ConnectError` / `RemoteProtocolError` / `ReadError`…），而旧代码只把外层那一句 `Connection error.` 上抛、**连日志都不记** ⇒ 压测里那 3 次失败在服务端只留下 SDK 自己的 warning，「上游断连 / 代理抖动 / 被对端限流」三种完全不同的病长得一模一样，**无法进一步定位**（§四 的原话）
  - 修法：新增 `agent/utils/llm_gate.describe_exception_chain()` 摊平 `__cause__`/`__context__`（去环 + `max_depth=6`），失败时 `_logger.error` 记 `外层 ← 中层 ← 根因 | 根因 repr`（只摊平类名 + **最内层** repr —— 外层信息没有诊断价值）。归因与闸同一个模块；`is_timeout_error` 也从 `middlewares/model_timeout.py` 挪进 `llm_gate` 并**原样再导出**（模型调用的异常归类只此一处，对外名字不变）
  - ⚠️ **判定顺序是硬约束**：`openai.APITimeoutError` 是 `APIConnectionError` 的**子类** ⇒ 必须先判超时再判连接。判反了会把「对端慢」当「连接断」去重试，用户白等几个 60s（已加负对照断言钉死）
  - 验收：`scripts/verify_llm_gate.py` ① 段 8 条（三层链 / 根因 repr / 超长链截断 / 环不死循环 / 单层负对照）；⑤ 段**抓真日志**断言含 `APIConnectionError ← httpx.ConnectError` 与根因 `Connection refused`

- [x] **P2-12 主 run 成功后 `next` 悬挂不推进（settle 不收敛，P2-7 实测 1/12）**（2026-09-24 判明，**未发版**）
  - 症状：`main_run_status=success`（8.06s）、`last_message_is_final=True`，但 `GET /threads/{id}/state` 持续返回 `next=['PatchToolCallsMiddleware.before_agent']`，**悬挂 240s 无任何东西推进它**（只有 6 并发档出现，1/12）
  - **结论：不是丢步，是 `update_state` 的固有副作用 + 我们的消费侧口径过严。** 离线机制复现与判据回归见 `scripts/verify_phantom_next.py`（**23/23**）。三条链：
    1. **幽灵 `next` 怎么来的**：`update_state(values, as_node="__start__")`（= sync 的 `_sync_update_state`，并发写用 `_SYNC_WRITE_LOCK` 串行化）语义是「假装 `__start__` 刚跑完、产出了这些字段」，langgraph 顺带把 `next` 置成**它的后继** = 图入口节点 `PatchToolCallsMiddleware.before_agent` ⇒ **补丁写完 `next` 必然非空**；重复补丁不堆积（仍是一个待执行节点）；`as_node=<终态节点>` 则不产生（但改这条写路径会动到 P2-4/P1 的既有语义，只为消一个无害副作用，**不改**）
    2. **为什么没人推进**：LangGraph 对 `update_state` 有硬闸（主线程有 pending/running run → 409）⇒ 补丁**只可能落在主 run 终态之后**；此时该轮的续跑若早已通知过（`success_notified_local` 已置位 → 不会再起新 run），就没有任何东西去推进它。**并发相关性**：6 并发下续跑/别的子任务的 run 挤在一起，补丁更容易落在「终态之后、续跑已通知」的窗口里；1~2 并发时补丁落在 run 内或直接被下一轮消费（**与负对照一致**）
    3. **为什么无害**：答复已终稿、run=success，`api.thread_run_status.classify()` 的**判据 4**（最后一条是终稿 assistant 文本）明确把这种形态判成**不报「已中断」**——生产 trace 01a09ed5 就是这一形态（2723 字答复 + next 非空）；下一轮用户消息也会把它顺带消费掉。**全 `src/` 只有 `thread_run_status.classify` 一个 `state.next` 读者**（已 grep 核实），所以后端没有别处因此挂住
  - **修法（消费侧口径）**：`scripts/load_test_concurrency.py` 新增 `turn_settled(settle)` = 直接采用端点自己的结论字段（`not has_active_run and not awaiting_interrupt and not turn_failed and not turn_incomplete and last_message_is_final`），**不再要求 `next` 为空**；同时记录 `phantom_next` 计数（是否收敛时有 next）与逐档汇总，并因此**恢复记录答案**（旧口径下这类问数 `ans_len=0`，看起来像「答案丢了」）。`_sync_update_state` 与 `thread_run_status` 的 docstring 都写明该副作用，防止后续代码把 `next` 非空误读成丢步
  - 验收：`scripts/verify_phantom_next.py` **23/23**（① 真 langgraph 最小图复现幽灵 next + 幂等 + `as_node=<终态>` 对照；② `classify()` 对幽灵态判 `turn_incomplete=False` / `turn_failed=False`，负对照=真半轮 `tool_calls` 结尾仍报未完成、活跃 run 不报、终态失败仍 `turn_failed`；③ `turn_settled` 在幽灵态收敛、旧口径不收敛、四条负对照均不收敛）
  - **未验（如实记）**：**没有**在生产 6 并发档复现（那是生产写操作，需单独授权）；本项的「并发相关性」是由 409 硬闸 + `_SYNC_WRITE_LOCK` 争用推出的机制解释，**不是**实测的并发-发生率曲线。下次压测（若跑）直接读报告里的 `phantom_next` 计数即可证伪/证真

---

## P3 — 结构性（月级，**已有 P2-7 数据**：2026-09-24 生产压测，见 `2026-09-24-P2-7并发压测报告.md`）

> P2-7 的决策结论：**打满 10 个 run 槽（6 并发问数）时服务端零排队、零超时、RSS 仅 +170MB、事件循环 lag ≤ 17ms**；失败全部来自**出站模型调用连接错误**（P2-11）与 **`next` 悬挂不推进**（P2-12）。⇒ **加槽位（P3-1）与加副本（P3-2）都不是当前收益点，先做 P3-4 + P2-11。**

- [ ] **P3-1 提高 `N_JOBS_PER_WORKER`（现 10）** —— **实测暂不需要**：6 并发问数时峰值在跑恰好 10.0、余槽 0.0，但**没有产生排队**（`peak_pending=0`），且服务端各项健康 ⇒ 当前瓶颈不在槽位。10 槽实测支撑 **3~6 个并发问数**，与「3~4 并发用户」推导一致。**前置已具备**（P3-4 的 LLM 并发闸 + P2-11 的异常链归因 2026-09-24 已做）—— 但本项**仍维持「实测暂不需要」**：加槽的收益要等新一轮压测看到 `nl2sql_llm_gate_total` 的排队量再说
- [ ] **P3-2 多进程/多副本**（前置：搬走 inmem 运行时状态、MCP 改 HTTP/SSE、本地 SQLite 迁 Postgres、报告落共享存储）—— **实测暂不需要**：单副本 6 并发问数下 RSS 740→938MB、lag 17ms，资源远未打满；瓶颈是单次出站模型调用，不是副本数
- [x] **P3-3 工作区请求级隔离** —— 🔚 **已按 T1+T2+T3 终结（2026-09-25）**：不再是「请求级隔离」，也不是「加固单值语义」，而是**把机制本身删掉** —— 工作区路径钉死为 `<AGENT_DATA_ROOT>/workspace`（单值仍在，但**不可变**），`active_name` 恒 `"default"`，`api/workspace.py` 五个端点、注册表、CRUD、`WorkspaceBusyError`/`_guard_active_runs`/`?force=1` 全删，「有人在跑时切走工作区 ⇒ 在跑的 run 路径漂移」这个坑**不可能再发生**（没有切换动作）。验收 `scripts/verify_workspace_pinned.py` 56/56。以下为 2026-09-24 的加固设计与实测，保留作决策依据。
  - **审计结论（原清单前提要纠正）**：工作区**不是**按用户/按请求解析的 —— `WorkspaceManager` 的 active 值是**进程级单值**，29 处读者（`DynamicFilesystemBackend` 的 VFS 根、`DynamicLocalShellBackend` 的 cwd、`report/`、`nl2sql_process_data/`、`large_tool_results/`、checkpoint、`langfuse_span._active_workspace_path()`…）都**现取** `active_workspace`。但**三个改工作区的端点全都 `require_admin`**（`api/workspace.py` 的 activate/register/unregister，注释原话就是「全局唯一…切一次影响所有用户 → 管理员」）⇒ **「多用户各切各的工作区互相打乱」在当前产品形态下不成立**；`delete_workspace` 也早就有「禁删活跃工作区」护栏
  - **审计结论（原清单前提要纠正）**：工作区**不是**按用户/按请求解析的 —— `WorkspaceManager` 的 active 值是**进程级单值**，29 处读者（`DynamicFilesystemBackend` 的 VFS 根、`DynamicLocalShellBackend` 的 cwd、`report/`、`nl2sql_process_data/`、`large_tool_results/`、checkpoint、`langfuse_span._active_workspace_path()`…）都**现取** `active_workspace`。但**三个改工作区的端点全都 `require_admin`**（`api/workspace.py` 的 activate/register/unregister，注释原话就是「全局唯一…切一次影响所有用户 → 管理员」）⇒ **「多用户各切各的工作区互相打乱」在当前产品形态下不成立**；`delete_workspace` 也早就有「禁删活跃工作区」护栏
  - **因此真正缺的是最后一道 fail-closed**：管理员在**别人有 run 在跑**时切走工作区 → 那些 run 后续每一次路径解析都落到新工作区，而 run 自己不知道（产物/中间数据写错地方）。**原清单要求的「请求级隔离」= 让不同用户各用各的工作区**，那是产品能力（要动 29 个读点 + 前端把 workspace 塞进 run 的 `configurable` + 池线程显式传 context），半做比不做更糟（写侧按请求、读侧全局 = 静默混数据）⇒ 用户拍板先加固单值语义
  - **实施**：`manager._active_run_count()`（真源 = `langgraph_runtime_inmem.queue.WORKERS` 的长度，与 `api/drain.active_workers()` 同源；**惰性 import**，**读不到返回 `None` 而不是 0** —— 「数不出来」与「确实没人跑」必须能区分）+ `manager._guard_active_runs(action, force)`：
    - `activate_workspace(name)` 与 `unregister_workspace(name)` 都收 `force=False`，**默认拒绝**（抛 `WorkspaceBusyError`，带 `active_runs`）—— 拒绝时**注册表逐字节未改**，线上零改动
    - **只拦该拦的**：`unregister` 的守卫在「取消的正是活跃工作区」分支里（= 真会回退 default）；取消**非活跃**工作区即使 5 个 run 在跑也放行 —— 拦错了等于管理员永远清不掉废弃工作区（有负对照钉死）
    - **`force` 是给运维的出口**：僵尸 run（inmem `WORKERS` 里卡住的条目，[[langgraph-inmem-pckl-zombie-runs]]）会让计数永远不为 0，不留出口 = 再也切不了工作区；强制放行 **warning 留痕（含计数）**
    - **计数不可用（惰性 import 失败/包改名）→ 放行 + warning**：与 `api/drain` 同口径「没有计数就不阻塞运维」
    - **API**：`PUT /api/workspaces/{name}/activate` 与 `DELETE /api/workspaces/{name}` 支持 `?force=1`（口径同 `delete_files`），拒绝 → **409 + `active_runs`**，且**不触达**切完之后的工具重载（没切就没得装）
  - **如实记两条边界**：① 守卫是「别顺手切」的闸，**不是互斥**—— `activate` 的检查与写注册表之间，新 run 仍可能被提交（窗口极小，且真正受控的运维动作是 P2-1 的 drain：先 `POST /api/admin/drain` 拒绝新 run、再等排空、再切）；② `WORKERS` 含子 agent run 与 sync 循环（一次问数约 3 个），所以计数比「用户数」保守
  - 验收：~~`scripts/verify_workspace_switch_guard.py` **50/50**~~（三连跑全绿；真 Starlette Request 走真 handler，含「拒绝时注册表逐字节未改」「409 不触发工具重载」「非活跃取消注册的负对照」「计数不可用 → 放行 + 留言」「`is None` 分支必须在 `n <= 0` 之前」）。⚠️ **该脚本与被测代码已于 2026-09-25 随 T3 一起删除**（工作区不可切换 ⇒ 守卫无对象）；现存同类验收为 `scripts/verify_workspace_pinned.py` **56/56**。回归 `verify_chart_artifact_owner.py` 47/47。
- [x] **P3-4 LLM 侧并发闸 + 队列 + 退避 + 按用户配额**（2026-09-24 完成，与 P2-11 合并；**未发版**）
  - 背景（P2-7 升为 P3 第一优先的由来）：6 并发问数时唯一的真实失败就是出站模型调用的连接错误（`openai.APIConnectionError`，3/12 问数终态失败 + 1 个恢复的把 turn 从 8s 拉到 50.8s）
  - 落点：`ModelTimeoutMiddleware`（P2-3 起就是**模型调用的唯一收口点**，主/子 agent 的图都注册了它）负责接线；实现全在新文件 `src/agent/utils/llm_gate.py`
  - **四件套**：① 并发闸（全局 + 按用户；**计数式而不是 `Semaphore`** —— 容量要随 env 变、不该要求重启）；② 排队（背压轮询 0.02→0.2s，预算 30s）；③ 连接类错误的**全抖动**退避重试（默认额外 1 次）；④ 按用户配额（默认 4）
  - **为什么"SDK 已经重试了"不够**：SDK 的 `max_retries=3` 在生产上**已全部用尽**（同一 run 同一条错连续 4 行）⇒ 这层是**更慢的第二道**，目的不是等一轮长故障过去，而是把并发退化成**错峰重发**；也正因如此只给 1 次（多加会把单次模型调用的墙钟时间推向工具超时 300s）
  - **闸的三条硬约束**（都写在注释里，别改）：① 取槽失败一律 **fail-open 放行**（+计数 +warning）—— 收口点上任何死等或抛异常都会把**全站**问答弄挂；② 同步路径若在事件循环上**不排队**（P1-14 口径：宁可少一道闸，不可多一个全站阻塞点；单独记 `sync_on_loop` 标签）；③ `0`=关、非数字退回默认（与 `db/limits.py` 同口径）
  - 默认值全是**推导值不是实测最优**：全局 **6**（= 出错那一档之下、run 槽天花板 10 之下）、单用户 **4**、排队 **30s**（P2-7 里「恢复的那次」把 turn 从 8s 拉到 50.8s 已经很难看，再让用户静止等两分钟不如放行让上游去抖）
  - 可观测：`nl2sql_llm_gate_total{result=free|waited|bypassed|off|sync_on_loop}`、`nl2sql_llm_gate_wait_seconds`、`nl2sql_llm_retries_total{recovered|exhausted}`；新增告警 `llm_gate_bypass`（**连续 3 轮**窗口内 ≥5 次放行 = 闸长期不够用 ⇒ 要么调高、要么这就是那批连接错误的来源；`NL2SQL_ALERT_LLM_GATE_BYPASSES=0` 可关）。**注意计数口径**：每次真实尝试都记 `note_llm_call`，所以「重试后成功」也会在 `{outcome="error"}` 上留一笔，**不等于用户可见失败数**
  - 顺带修掉一个自造 bug：闸的 contextmanager 若把 `yield` 放进 `try/except Exception` 里，**body 抛的异常**会被当成「闸坏了」吞掉，`contextlib` 接着报 `generator didn't stop after throw()`（原始异常被换成一条没有诊断价值的 RuntimeError）⇒ 取槽与持槽必须**分段**，持槽那段只有 `finally`（已加异常穿透断言）
  - 验收：`scripts/verify_llm_gate.py` **70/70**（真 openai/httpx 异常对象 + 真并发 + 真中间件，含两条关键负对照：「闸关掉时峰值就是并发数」与「非连接类只调一次」）；回归 `verify_metrics.py` **87/87**（含新增 3 条告警断言）、`verify_event_loop_liveness.py` 32/32、`verify_graceful_drain.py` 76/76、`verify_tool_pool.py` 31/31、`verify_db_limits.py` 47/47、`verify_retention.py` 76/76
  - **未验（如实记）**：闸限值 6/4 是**推导值**，未在生产复测 6 并发档 ⇒ 下次压测必须把 `nl2sql_llm_gate_total{result=waited|bypassed}` 与失败率**一起**看：`waited` 大而失败率下降 = 闸起作用了；`bypassed` 一直涨 = 闸不够用（告警会叫）；两者都平静但失败照旧 = **瓶颈不在出站并发上**，那 3 次失败另有来源（需 `__cause__` 日志给出答案，这正是 P2-11 的意义）
- [ ] **P3-5 发版流水线化**（有版本、可回滚、可灰度）
  - **✅ 2026-09-25 已实施（用户拍板）**：`release-backend.ps1` 补 `--exclude=src/agent/workspace-temp`（脚本注释里写明了 tar 的斜杠边界匹配行为）。实测复核：旧口径打出的包里 `src/agent/workspace-temp/` **2847 个条目**，补上后 **0 个**；另发现 `src/agent/workspace` 在本机已不存在（工作区外置到 `AGENT_DATA_ROOT`）⇒ 旧那条排除项本来就是空转。**`.env.dev`（含 dev 库口令）与 `.claude/` 是否排除仍未定**，脚本注释原样保留待拍板。
- [ ] **P3-6 MCP 从 stdio 改 HTTP/SSE + 常驻进程池**（消掉每次调用新建子进程 + 2-4s 冷启动）
- [x] **P3-7 把共享 loop 上的 CPU 重活移出**（`check_progress.py:1158 plan_run_sql`、`wren_plan.py:222-241` 锁内建引擎）；wren 引擎缓存按库数扩容（2026-09-24 完成；**未发版**）
  - **前半（搬离 loop）：复核结论是「P1-14 已经搬完了」** —— 清单点名的 `check_progress.py:1158 plan_run_sql` 现在走 `await offload(_mod._build_check_result, …)`（`check_progress.py:1355` 附近，注释里就写着「`plan_run_sql` 建引擎约 0.9s，直接 await 就是把 CPU 挂在共用事件循环上」），Cube 快照 `cube_snapshot` 走 `await offload_long(...)`（`api/message_feedback.py:120`）、标注页试算走 `await asyncio.to_thread(plan_cube_sql_checked, …)`（`api/feedback_annotation.py:623`）。⇒ **本次不动运行代码，改为把这条规则固化成静态回归**：`scripts/verify_wren_offload.py`（**28/28**）
    - 判据按 **AST** 而不是 grep：重活名单（`plan_run_sql`/`plan_cube_sql`/`plan_cube_sql_checked`/`cube_snapshot`/`_build_check_result`/`_engine_for`/`build_report`）出现在 `async def` 体内、且**没有被** `offload|offload_long|to_thread|run_in_executor` 的实参包住 = 违规。grep 分不清「同步函数里裸调（正确：调用方整体搬走）」与「协程里裸调（坑）」；AST 能
    - 负对照 7 条：裸调/属性调用/协程内嵌**同步**函数裸调（这条是漏判防线 —— 嵌在协程里的 `def g()` 仍是就地调用）/同步函数内嵌协程裸调 → 全部命中；`offload`、`offload_long`、`to_thread`、同步函数裸调、同步函数被 offload → 全部不误报。另有「名单里的名字在树里真实存在」断言，防规则退化成恒真
    - 当前真实树 **0 处违规**（314 个 .py）
  - **后半（缓存按库数扩容）**：`wren_plan._CACHE_MAX = 4` 是单库时期的值，而缓存键含**连接指纹** ⇒ **键数 ≈ 同时活跃的语义库数**，库数 > 4 时用户在库间来回就会把引擎挤出去、每次重建。改为 `DEFAULT_CACHE_MAX = 16` + env `WREN_PLAN_CACHE_MAX` 覆盖（**非数字/≤0 退回默认**，与 `db/limits.py`/`llm_gate` 同口径）
  - **实测（本地 4 个语义库，如实记口径）**：冷建一次 **1241ms**（首个还含 wren 导入，单独量到 1672ms）、**热命中 0.33ms**；换连接指纹 → 新键。⚠️ 这些都是**本地小库**（mdl 16~119KB），生产 `witops` 那种 100+ 模型的库按代码注释约 0.9s ⇒ **16 这个默认值是按「≈4× 当前库数」取的推导值，未按生产内存实测**（每个引擎常驻内存，库很多时应按 RSS 调小）
  - **明确不做**：「按键加锁、锁外并行构建」能让不同库的首次查询并行（现在是持 `_CACHE_LOCK` 串行，本地 4 库并发到达实测墙钟 15ms ≈ 串行和），但 **wren `_build_engine` 是否线程安全没有证据**（可能碰进程级全局，如数据源注册）⇒ **不做**，除非先在生产量出这个串行是可观测瓶颈。已在代码注释里写明理由
- [x] **P3-8 `POST /api/model-configs/test`、`/probe-capabilities` 是任意登录用户可用的 SSRF 面**（2026-09-24 发现，**2026-09-25 拍板并已修，未发版**）
  - **事实**：这两个端点的 `base_url` 可以**只从请求体**给（`model_config.py` 的 `test_config` / `probe_capabilities`），服务端随后用 `urllib.request.urlopen` 去请求它（`_probe_models` 等三个 `_probe_*`，timeout 10s/6s，会依次试 `/v1/models`、`/models`）⇒ 任何登录用户都能让服务器代发 HTTP 到内网地址并**通过返回的 message/models 拿到回显**（端口开放与拓扑侦察）。
  - **为什么原方案的「限管理员」不成立**：模型配置自 P1 起是**按用户独立 store**（`get_user_store(user["user_id"])`），每个用户配自己的模型是**产品行为**，把整组端点限管理员会打断它。原文档 §5.8/§7 第 9、11 行写的「管理员」是**过期结论**（已同步修正）。
  - **修法（已实施）**：新增 `src/agent/utils/net_guard.py`，口径是**「只堵服务器能到、而配置者本人到不了的地址」**：拒 **环回 `127/8`、`::1`、`0.0.0.0`/`::`**（容器自身服务只有服务端到得了，这是唯一"新增"出来的探测能力）与 **链路本地/云元数据 `169.254/16`、`fe80::/10`**；**私网 `10/8`、`172.16/12`、`192.168/16` 一律放行**——本仓模型网关与数据库就在私网（`http://192.168.25.13:8100/v1`），**"拒绝私网"这条常见修法会把正常功能一起拒掉**。附带只允许 `http/https`（挡 `file://` 本地读取面）。
  - **两个实现要点**：① **解析后校验 IP**（`getaddrinfo` 拿全部地址逐个判），字符串校验挡不住 DNS 重绑定，且 `::ffff:127.0.0.1` 这类 IPv4-mapped 地址 `is_loopback` 为 False 要显式拆出来；② **守卫放在 handler 层**——`_probe_model_capabilities`/`_probe_single_model` 里那个 `except Exception: continue` 会把守卫的异常一起吞成"探活没命中、回退静态值"，用户看不出被拒；`_probe_models`（通用入口，最容易被复用）里另加一道纵深防御。
  - **不是只挡请求体**：`?name=` 走已存配置的路径同样要挡（否则"先存一个 `base_url=127.0.0.1` 的配置再点测试"就是两步绕过）——已在验收里作为独立用例。
  - **逃生门**：`NL2SQL_SSRF_ALLOW_LOOPBACK=1` 放行环回与 `0.0.0.0`（本机跑 ollama/网关的开发场景），**元数据地址仍拒**。
  - **如实记的残余**：守卫是「先解析、再交给 `urlopen`（它会再解析一次）」⇒ 理论上有 TOCTOU 窗口，要彻底关掉得把连接钉在已校验 IP 上（换 HTTP 客户端），代价与收益不匹配。另：`db-configs` 的连通性测试是**管理员端点**、且连的是数据库不是 HTTP，本次未动。
  - **验收**：`scripts/verify_net_guard.py` **70/70 ×3**（真 handler + 真 Starlette Request；含「环回被拒时**一次出站都没发**」「私网照常探活」这两条关键对照，以及「把 `urlopen` 换成炸弹仍返回 400」的反向断言）；回归 `verify_config_authz.py` 32/32。

---

## 附：不需要动的部分（避免误伤）

- `/api/feedback/annotations*`、`/api/feedback/datasets`、`/api/experiment/*`、`/api/auth/users*`、`/api/grants`、`/api/eval-flags` 都挂了 `require_admin` ✔
- `/api/db-configs` 的 `visible_dbs`/`can_access_db` 过滤 ✔
- 语义库那一面（`wren_semantic.py`）是全仓**最完整**的权限实现（读端点全挂项目级权限，对未关联库的项目 fail-closed）✔
- `/api/model-configs*` 有每用户独立 store ✔
- `mcp_tool.py:571` 等处的「起子进程必须放线程」约定 —— 这是 **P1-14 的整改模板**，照它抄 ✔

---

## 执行记录（2026-09-23）

### 一、会话归属隔离到底解决没有 —— 结论

**代码层面已解决（已提交、已发版、E2E 18/18），但当时仍有两条旁路门让它在生产上等于没生效。**

原清单写的「`M backend.py` / `?? ownership.py`，均未提交，生产没有」**是错的**：
`git log` 显示 `f5cd2fc feat(auth): enforce thread ownership at the LangGraph ops layer` 已提交且已发版。
复核后真实状态是：四个 `@auth.on` 钩子都在、也都写对了，但被两条**各自独立**的门抵消：

| # | 门 | 位置 | 怎么被利用 | 后果 |
|---|---|---|---|---|
| 1 | 端口直连 | compose `ports: "2026:2026"` → `backend.py:56` | 直连 `192.168.25.64:2026`，不带 token、不带 `X-Forwarded-For` → `authenticate` 返回 `internal` → 四个钩子对 internal **全部 `return None`** | 原生路由（`/threads/search` 等）全部敞开 |
| 2 | nginx 无 Cookie | `nginx:8080` → `auth_middleware.py:106` | 干净浏览器打开 8080 → 请求来自 nginx 容器（172.x）、不带 Cookie → 中间件注入 `internal` → `require_user` 不拒绝 internal | **63 个 `/api/*` 全部敞开，含登录门本身** |

**门 2 比原先描述的更严重**：`/api/auth/me` 对 `internal` 返回 200 + 用户信息 →
前端 `AuthGuard` 判定「已登录」、**不跳 `/login`** = 登录门被整体绕过。
零凭据可读走的包括 `/api/db-configs`（含连接串）、`/api/reports/*`（全站报告）、全部会话。

**两条门不是同一条——关端口只能关掉门 1。**

### 二、本次改动

| 项 | 文件 | 内容 | 生效方式 |
|---|---|---|---|
| P0-4 | `src/api/auth_middleware.py` | 内部旁路追加「**无** XFF」判据，与 `backend.py:56` 对称 | 发版 + 重启后端 |
| P0-4 | 镜像 compose | `NL2SQL_AUTH_DISABLED: "0"`（`environment` 优先于 `env_file`） | 重启 |
| P0-1 | 镜像 compose | `BG_JOB_ISOLATED_LOOPS: "true"` | 重启 |
| P0-2 | 镜像 compose | `ports: "127.0.0.1:2026:2026"` | 重启 |
| P0-6 | 镜像 compose | `mem_limit: 8g` | 重启 |
| P0-3 | 镜像 `docker/nginx.conf` | 整段删除 `location /report/`；compose 去掉 `workspace:/workspace:ro` | nginx 重建 |
| P0-7 | `docs/weint环境/发布脚本/` | 发版前打 rollback tag + 新增 `rollback-backend.ps1` | 无需重启 |
| — | `release-backend.ps1` | tar 追加 `--exclude=src/agent/shared/model_config.json` | 无需重启 |
| 验收 | `scripts/verify_auth_middleware.py` | 16/16；另做**反向对照**：换回修复前中间件 → 恰好那 4 个用例 `code=200` | 本地 |

**判据为什么可靠**：全仓**唯一**写 `X-Forwarded-For` 的地方是 `docker/nginx.conf`，
Python 侧无任何写入点 → 「带 XFF」⟺「经过外部入口」。客户端**伪造 XFF 也没用**
（nginx 用 `$proxy_add_x_forwarded_for` **追加**，头部必然非空 → 仍走 token 校验）；
想不带 XFF 只有直连 2026 一条路，而那正是 P0-2 关掉的门。两条改动互为补充，合起来才闭合。

**P0-4 第二小条「`internal` 不再短路归属校验」判定为不该改**：
`internal` 是子 agent / sync 循环的身份，**不携带是哪个真实用户**，钩子无法为它构造有意义的
`owner_filter`（返回过滤器会把它自己代用户创建的线程 404 掉）。正确解法是让 `internal`
从外部**不可达**（= P0-2 + P0-4），而不是改钩子的短路。
日后要更紧的升级路径：给内部调用发**短期内部 token**，让 `internal` 带上真实用户身份。

**共享密钥（原判据里的首选方案）本次未做**：XFF 判据 + 关端口已足以关闭两条门，而共享密钥
要改内部所有调用点（子 agent SDK、sync 循环、自定义 API 自调用），爆炸半径大、收益边际。列为后续硬化项。

### 三、生产落地（2026-09-23 16:41 已执行并验收）

用户授权「现在一次性全做」。**P0 全部落地生产。**

落地方式：`d:\tmp\patch_prod_p0.py`（断言式补丁，每处锚点必须恰好命中一次，否则整体不写；自动备份 `*.bak-p0-20260923`）
→ `release-backend.ps1`（含回滚 tag）→ nginx 容器重建。

**验收结果（全部通过）**：

| 项 | 验收方式 | 结果 |
|---|---|---|
| P0-1 | 容器启动日志 | `Starting queue with isolated loops` ✔（原为 shared loop） |
| P0-2 | `docker ps` + 从外部主机 `curl :2026` | `127.0.0.1:2026->2026/tcp`；外部 `http_code=000`（不通）✔ |
| P0-3 | 抽一个**真实存在的报告文件**请求 | `/report/<真实文件名>.md` → **404** ✔；nginx 的 agent_data 挂载已摘 |
| P0-4 | 无 Cookie / 带 Cookie 分别打 `/api/*` | 无 Cookie `/api/auth/me`、`/api/db-configs` → **401**；带 Cookie → **200** ✔ 正常登录不受影响 |
| P0-4 | 部署版 md5 与本地比对 | `2d3a68622572018c4b35945213bc4c27` **完全一致** ✔ |
| P0-6 | `docker inspect` | `Mem=8589934592`（8g）✔ |
| P0-7 | 发版输出 | 已建 `nl2sql-api:rollback-20260923-1641` ✔（真实演练） |
| 无回归 | 容器内跑 `scripts/e2e_thread_isolation.py` | **18/18 通过** ✔ |

**回滚方式**：`.\rollback-backend.ps1 -Tag rollback-20260923-1641`

### 四、执行中发现的两个环境陷阱（重要，已写入 memory）

1. **docker 29 + docker-compose v1：`up -d` 作用在「已存在的容器」上会崩，且是停掉容器之后才崩。**
   本次 `docker-compose up -d nginx` 报 `KeyError: 'ContainerConfig'`，nginx 先被 stop + 改名成
   `8464a7bba947_nl2sql-app_nginx_1` 然后 compose 崩溃 → **8080 直接中断（http_code=000）**。
   - 立即恢复：`docker start 8464a7bba947_nl2sql-app_nginx_1`
   - 正确姿势：**先 `stop` + `rm -f` 再 `up -d`**（发版脚本对 langgraph-api 正是这么做的，所以它没事）；
     或至少加 `--no-deps` 避免 compose 去 inspect 依赖容器。
   - **这条对发版脚本同样适用**：任何「对已存在容器直接 up -d」的步骤都会踩。

2. **从 Git Bash 里启动 PowerShell 跑发版脚本，`tar` 会解析到 Git 的 GNU tar**，
   报 `Cannot connect to D: resolve failed`（GNU tar 把 `D:` 当远程主机名）。
   解法：用净化过的 PATH 调 powershell（只留 System32 / OpenSSH），或在脚本里写死
   `$env:SystemRoot\System32\tar.exe`。

另修一处：`scripts/e2e_thread_isolation.py` 末尾的「运行方式」说明**没有加注释符**，
裸贴在代码后面 → 整个文件 `SyntaxError`、**无法运行**（该文件已提交且未修改，
说明记录的 18/18 来自加这段说明之前的版本）。已改为注释并补上真实可用的调用方式。

### 五、本次未做（留给后续）

- **P0-4 的共享密钥**（见上文理由）
- **postgres 的 5435 仍是 `0.0.0.0:5435` LAN 发布**（compose 注释写明「LAN 可达」，属有意为之）
  → 即 `postgresql://nl2sql:<默认口令>@192.168.25.64:5435` 从网段内可直连。
  同一台机器上还有多个容器（langfuse pg 5433、minio、clickhouse、redis…）也是 LAN 发布，
  属整机安全姿态问题，**未擅自改动**，建议单独评估。
- P0-5 遗留的 `if_exists` 分支复核
- **P1 全部补完**（P1-1~P1-17 共十五项；P1-17 于 2026-09-24 补完）；P2（9 项）/ P3（7 项）全部未动

---

### 六、P1 执行记录（2026-09-23，代码于 **2026-09-24 已发版**）

十五项已完成（P1-12 于 2026-09-24 补完、P1-14 与 P1-17 于 2026-09-24 补完），每项都配了可证伪的验证脚本（正向断言 + 负对照）。**十四个脚本在本地全绿**：

> ⚠️ **发版顺序约束（P1-5 引入，必须先做）**：fail-closed 的代码上线**之前**要在生产跑一次
> `scripts/backfill_thread_owner.py --apply`（先 dry-run 看分桶）。顺序反了 = 窗口期内所有老会话
> 的 trace/导出/反馈/run-status 全部 403（会话本身还能聊，因为 ops 层看的是另一套账本）。
>
> ✅ **P1-12 没有顺序约束、也不会踢人**：老 token 无 `pv`（按 0）、老账号记录无 `token_version`
> （按 0），两边对齐 → 发版当天不需要任何人重新登录、不需要回填。强制改密的开关默认 off。

| 项 | 脚本 | 结果 |
|---|---|---|
| P1-4 + P1-6 端点守卫 | `scripts/verify_endpoint_guards.py` | **23/23**（2026-09-25 实测；负对照 `NL2SQL_AUTH_DISABLED=1` → **6/23**）。⚠️ 原记录 30/30 / 7/30 已失效：T3 删掉 6 条 `/api/workspaces*` 断言、其余条目也重排过，数不上对不是回归 |
| 运行时库「一库一目录」+ 老文件接管（2026-09-25） | `scripts/verify_store_layout.py` | 36/36（负对照：根上同名老库**不被计入占用、也不被清理**；主库搬不动 ⇒ 只在原地等重试、**不半搬**；目标已存在 ⇒ 新库一字未变；两进程抢搬 ⇒ 恰好一个成功） |
| P1-1 trace 事件归属 | `scripts/verify_trace_thread_scope.py` | 8/8（负对照：旧代码下 B 拿到 A 的 2 个工具事件） |
| P1-7 auth 存储加锁 | `scripts/verify_auth_store_concurrency.py` | 8/8（负对照 2/2：WinError 5、20 条连接） |
| P1-8 + P1-9 存储并发 | `scripts/verify_store_concurrency.py` | 14/14（负对照 4/4：seq 撞号、id 取错行、并发读互踩、跨进程重复领取 195 次）<br>「并发读互踩」的窗口在 sqlite3 的 C 代码里（Python 侧插不进 sleep 放大）→ 单次运行实测约 1/5 假绿，2026-09-24 改成 `legacy_race_retry`「最多试 4 次、命中一次即算检出」（连跑 6 次全部第 1 次命中）。**判别力没削弱**：旧路径真被修好则 4 次都不命中，仍然红 |
| P1-2 run 入参授权 | `scripts/verify_config_authz.py` | 32/32（负对照 3/3：不钳制时伪造 user_id 存活、越权库放行、空库名暴露全部工具） |
| P1-3 报告/图表隔离 + 归属 | `scripts/verify_report_ownership.py` | 40/40（对抗性负对照：拆掉 `_may_read` → 7 条断言立即失败，同时"放行"类断言仍通过） |
| P1-5 + P1-15 归属 fail-closed + 两账本同写 | `scripts/verify_thread_ownership.py` | 29/29（负对照：旧口径对同一未登记会话放行 / 新口径 403，翻转可检；含回填分桶与幂等） |
| P1-10 同步工具池满载快速失败 + 换池 | `scripts/verify_tool_pool.py` | 31/31（破坏式负对照：拆掉取槽上限 → 同一局面从 0.30s「繁忙」变回 0.51s「假超时」） |
| ~~P1-11 切工作区清进程级缓存~~ **已随 T3 删除**（2026-09-25） | ~~`scripts/verify_workspace_cache_reset.py`~~ → `scripts/verify_workspace_pinned.py` §⑤ | 原 30/30（负对照：把清理变 no-op → 切完后旧库仍判已建模、旧工具仍可调用）；现由 §⑤ 钉「写路径仍能一次清掉三个发现类缓存」+ 静态调用点计数 |
| P1-16 执行侧库授权（两条通道） | `scripts/verify_db_exec_authz.py` | 32/32（**真 `create_agent` 图**，身份走真 configurable；负对照：`_target_db` 恒返空 → 越权查询被执行） |
| P1-13 过滤器操作符契约 + legacy 存量口径 | `scripts/verify_authz_filter_contract.py` | 49/49（负对照：`owner_filter` 换成 `{"owner": {"$ne": …}}` → 别人的会话变可见；⚠️ **必须跑 `.venv`**，用 ambient python 会 0/2 自曝版本不符） |
| P1-12 凭据加固（哈希 / 吊销 / 首登改密 / Cookie Secure） | `scripts/verify_auth_hardening.py` | 76/76（**破坏性负对照三连**：`token_version_of` 恒返 0 → 已吊销的 token 复活；`verify_password_hash` 恒 True → 任何口令都能登；`hash_password` 换回无盐 SHA-256 → 盐断言失守） |
| P1-14 同步阻塞搬离事件循环（两个池） | `scripts/verify_event_loop_liveness.py` | 32/32（**破坏性负对照**：`offload` 换成就地调用 → 4 个真实 handler 的滞后断言全部变红，滞后 495~526ms 且慢活回到 MainThread；① 另有「分池 vs 共池」对照实验 2.3ms vs 550ms） |
| P1-17 chart-saver 产物归属登记 | `scripts/verify_chart_artifact_owner.py` | 47/47（**破坏性负对照**：同一条产出链不挂中间件 → BOB 下载 ALICE 的图 **200**，挂上后同一张图 **404**。⚠️ 清单原文那条验收**恒真**（`.svg` 在判权前就被列表接口后缀过滤掉），已改判据为直接 `GET ...?download=1`；脚本里两条都留并标注） |
| ~~知识类取料结果免截断~~（§十二，生产 trace 驱动，非 P 清单项） | `scripts/verify_slimmer_exempt.py` | 42/42（**负对照**：同一份 21,039 字符载荷换个工具名（`read_file`/`get_context`/`recall_queries`/`execute`）就照旧落盘 ⇒ 判定差异只由工具名决定；含边界 60,000/60,001、async 与 sync 两条路径、`awrite` 缺失会静默 fail-open 的 fixture 坑） |
| ~~展示 skill 归属表更名 + 独立 wren-execution 技能~~（§十三） | `scripts/verify_skill_owner_table.py` | 41/41（**负对照**：把更名前那张表代回去，同一条 `wrenai_witops_list_knowledge` 立刻又解析出 `nl2sql-understand` ⇒ 断言非恒真；另锁 12 个取料/探查工具、唯一归属压过过期活动 skill、共享工具 `dry_run` 的继承/回退两侧、目录名==frontmatter name、模型可见字符串不点名不存在的 skill） |

运行方式统一为 `uv run --no-project python scripts/<脚本>.py`（脚本自己建临时 `AGENT_DATA_ROOT`，不碰真实数据）。
Windows 上中文输出建议加 `PYTHONIOENCODING=utf-8`，否则被控制台编码糊成乱码（不影响判定）。
**唯一例外：`verify_authz_filter_contract.py` 必须用项目 `.venv`**（`./.venv/Scripts/python.exe`）——
它测的是 `langgraph-runtime-inmem` 的 matcher 行为，ambient python 装的是另一个（更旧的）版本。

> `scripts/_auth_test_support.py` **不是套件**，是 P1-12 之后 4 个套件共用的夹具
> （`ensure_auth_user` / `mint_for`：先登记身份、再按记录里的真实 `token_version` 签发
> = 生产里"管理员建号 → 用户登录"）。批量跑 `scripts/verify_*.py` 不会带上它；
> 它靠 Python 把**脚本所在目录**放进 `sys.path` 才能被 `import`（别把它挪进 `src/`）。
> 两个解释器下都验过（ambient 与 `.venv` 结果一致）。

**P1-2 的关键点都写在清单条目里（拒绝而非清空 / 不挂监控开关 / 补拦 stateless 端点 / 未选库绕过口）**，
这里只补两条工程教训：

- 中间件里「读 body」和「转发 body」必须成对：一旦读过 `receive`，就只能自己回放一份，
  不能 `await self.app(scope, receive, ...)` 把读空的通道递下去。原实现在「body 无改动」分支上正好踩了这个坑。
- 安全属性的存放位置与**它所在的代码路径**同样重要：同一个中间件里混着「监控旁路（失败无所谓）」
  与「授权（失败必须拒）」两种语义时，别共用早退分支 —— 开关一关，授权就跟着消失了。

**P1-3 补两条教训**（改动文件名 / 加归属账本时通用）：

- **改了产物命名，就必须同步改 prompt**：`chart-saver/SKILL.md` 里原本教模型
  `![图](./{name}.svg)` 用**基名拼路径**，加了唯一后缀后照做必成死链。凡是"模型要回显/引用"
  的命名规则，代码与 prompt 是**同一处契约的两半**，改一半等于改坏。
- **归属校验要连着"名字的来源"一起管**：只挡 `GET /api/reports/{filename}` 而 `GET /api/reports`
  照旧全量返回，等于把文件名清单直接送给越权者（再拿 `?download=1` 就能读内容）。
  越权响应还必须与"不存在"**同状态码同文案**，否则 403 本身就是存在性预言机。

**本轮最重要的发现不是清单里写的那几条，而是「共享连接上的并发读会互踩」**：

- 机制：CPython 的 `sqlite3` 按 **SQL 文本** 缓存 prepared statement 并**跨游标复用**。两个线程并发跑同一条 SELECT 时，一个线程的 `sqlite3_reset`/重新绑定会把另一个线程正在取结果的语句重置 → 一个拿到 `None`、另一个抛 `sqlite3.InterfaceError: bad parameter or other API misuse`。
- 实测：8 线程 × 300 次读同一条 SQL + 8 线程写 → **11 次异常 + 2 次空结果**，稳定复现（`case_concurrent_reads` 负对照）。
- 生产里的等价场景：`grants.owned_thread`（**每次会话访问都要过**）、`EventStore.query_events`（前端按会话 1~2s 轮询，多用户同时轮询同一接口就是同一条 SQL 并发）。→ 会表现为偶发 500 / 偶发「没有事件」、「会话不可见」之类无法复现的怪现象。
- 修法：**一条共享连接 = 一把锁，读写都进锁**（`grants._lock`、`EventStore._lock`）。P1-7 当时只锁了写、且注释写着「WAL 下读不阻塞写」——那句话对**事务**成立，对**驱动层的语句复用**不成立，已改正。
- 代价：读也串行。单进程、毫秒级查询，可接受；真要并发读得换「每线程一条连接」或连接池，那是 P3 的活。

**P1-10 补一条教训**（"超时"与"繁忙"是两件事）：

- **排队型工具池的失败模式是"假超时"，不是"慢"**：`ThreadPoolExecutor` 的工作队列无界，
  槽位被占满后新调用**照收不误**，调用方分不清"我这次真的跑超时了"和"我根本没轮到"——
  两者都表现为"等了 N 秒后收到超时消息"。**判别办法**：给这两件事**两条不同的消息**，
  并且在池入口（取槽）而不是在超时到点判满。修复后第 5 个调用 0.30s 就拿到「繁忙」，
  旧写法同一局面是 0.51s 才拿到「超时」且那个函数一次都没执行。
- **"未取消"要如实记账，不要假装**：Python 杀不掉线程，`future.cancel()` 只对**尚未开始**
  的任务有效 → 超时任务的槽位**不能归还**（归还了就等于把"卡死"报成"可用"，第 5 个调用
  会再次撞上同一个假象）。所以 `_inuse` 只在工作函数**真正返回**时减一，另用"满载持续 N 秒
  → 换新池"兜底恢复。把做不到的事写进注释与验收口径，比给个"已取消"的假日志强。

**P1-16 补三条教训**（执行侧判权 / 中间件验证通用）：

- **执行侧判权要按「库名的来源」分头取**：同一个属性在两条通道里来源不同 —— `dbmcp_*` 的库在
  **工具参数**里，`wrenai_<slug>_*` 的库在**工具名**里（`semantic_db.db_name_from_wrenai_tool` 反查）。
  只按参数取会漏掉整个语义层；只按名字取会漏掉直连。另外"反查不到库名"必须 fail-open（判权不能
  因为查不出库名就误杀），前提是**反查方法本身不能是有损的**：`_server_slug` 把
  `WIT运营管理平台数据库` 折成 `WIT`，反解必然错 → 只能拿当前工作区已建模库**正算前缀**去匹配。
- **`import agent.tools.mcp_tool` 会加载全部 MCP server（起子进程）**：该模块末尾是
  `main_tools = lazy.main_tools` / `sub_tools = lazy.sub_tools` 这类**模块级属性**，import 即触发。
  离线脚本要在 `seed()` **之前**先 import（此时 db_config 还空）+ 打桩 `_load_entry`，否则会按刚写的
  假配置去真连 wrenai/dbmcp（P1-16 首版实测：拉起 dbmcp 子进程 + 两条 wrenai 失败日志）。
- **带加粗/装饰的注入文本会让正则静默失效**：`_ACTIVE_DB_RE` 要求 `——` 后紧跟文字，而真实注入是
  `—— **已在 Wren 语义层建模**。` → 兜底分支一年都匹配不上（`_active_db` 的 state 路径是死代码）。
  **判据**：凡是"从 prompt 文本反解"的正则，都要拿 `dynamic_prompt` 里的**真实 f-string**当测试输入，
  别手写一条"看起来一样"的（本项就是靠 `t0` 拿真实文本才照出来）。

**P1-13 补两条教训**（判权与过滤器）：

- **读代码得出的语义必须实测一遍，而且要用"当前装着的那个版本"实测**：本仓的越权最后一道闸是
  运行时（不是我们自己的代码）在判 —— `_check_filter_match` 对 dict 值里的未知操作符**静默忽略**，
  于是 `{"owner": {"$ne": "alice"}}` 看起来是"排除 alice"，实际**放行所有人**。这条不是读注释看来的，
  是把 7 个操作符逐个喂进去跑出来的。**判据：凡是把语义委托给第三方运行时的写法（filter / query DSL /
  driver 参数），都要为它写一份"运行期真相"断言，并且把版本号一起打进输出** —— 因为不同版本的
  matcher 支持的操作符集合不同，换版本就是换语义。
- **同一台机器上可能装着同一个包的两个版本，而差异恰好是安全语义**：项目 `.venv` 的
  `langgraph-runtime-inmem` 是 0.31.1，系统 site-packages 是 0.14.1，后者**根本不支持 `$or`** ——
  谁用 `uv run --no-project python` 跑这份测试，都会得到"我们的过滤器全错"的假象（实际旧版本下
  是另一套故障：所有人都看不到自己的会话）。**判据：测试脚本第一步先自证环境**（`inspect.getsource`
  找特征分支），不符就打印**可直接粘贴的命令**并停下，绝不在错环境下继续跑出一堆红（那会让人去改
  没错的生产代码）。

**P1-14 补三条教训**（"把阻塞搬走"类改动通用）：

- **「搬线程」不是搬到别处，是搬到另一条**排队链**上 —— 所以池必须按调用特征分开**：短调用（每请求都走）
  与长调用（wren 构建 600s / git push 300s）共用一个池时，长任务占满 `min(32, cpu+4)` 个线程后，
  每个请求的 auth 写库都得排在它后面。症状从"事件循环卡住"变成"所有请求都慢但日志无异常"，
  更难查。脚本里的对照实验把这件事钉成了数字（**2.3ms vs 550ms**）。判据：**新建线程池前先问
  "谁会和谁抢"**，抢不到一起的别放一个池。
- **判据要落在"有没有真 `await`"，不能落在"函数名/装饰器像不像 async"**：本仓 10 个 SQL 驱动
  全是 `async def run_sql` 而函数体中一个 `await` 都没有（`await` 上去只是换个地方同步执行）；
  反过来，同步的 Langfuse SDK（`langfuse.get_client()`，异步的才叫 `AsyncLangfuse`）从名字上
  完全看不出是阻塞 HTTP。**两个方向的假信号都会出**，只有逐调用点看实现才靠谱。
- **搬走之后必须验"搬走的副作用也没变"**：`offload_long` 走 `run_in_executor`，**不传播 contextvars** ——
  把读 `get_config()` 的函数搬过去，它会静默读到空值（表现为 db_name/thread_id 变空，不报错）。
  所以本项除了"循环还活着"，还静态钉死"哪些函数不许进长池"，并把规则从代码里推导（而不是手写名单）。

**P1-17 补三条教训**（给"共享目录里的产出物"补归属类改动通用）：

- **验收标准本身要先证伪一遍**：本项原文的验收（"彼此在 `GET /api/reports` 列表里看不到对方那份"）
  在**修复前后都通过** —— `list_reports` 只列 `.md/.html/.csv/.json`，`.svg` 在判权**之前**就被
  后缀过滤掉了。**一条恒真的验收比没有验收更坏**：它会在清单上留下"已验证"的印记，而洞还在。
  判据：写下验收时问一句"**把修复撤掉，这条还绿吗**"？（本项的负对照就是把撤掉后的 200 真切出来了）
- **写入型"认领"操作，宁可漏不可错**：`record_report_owner` 是 INSERT OR IGNORE（先到者胜），
  对**无记录的存量文件**，任何一次"看起来像"的触发都会变成认领，反过来把原主挡在门外。
  所以触发条件要收得极窄（只认脚本自己打印的那一个显式标记，不扫结果里出现的路径），
  且收窄的两道门（父目录必须等于活跃工作区的 `report/`、文件必须真实存在）只"剥"不"弃"。
- **"非空"不等于"是真人"**：内部调用与 dev 旁路的身份是 `"internal"` / `"dev"` 这两个**非空**哨兵。
  按"取到身份就登记"写，会把产物记到一个不存在的用户名下，而判权对"有记录且非本人"是拒绝 ——
  **等于把真实用户锁在自己刚产出的文件外面，比原来那个洞更坏**。判据：写归属/所有权之前一律过
  `ownership.is_real_owner`，别用 `if uid`。

**P1-12 补四条教训**（凭据/令牌类改动通用）：

- **"某个值变成唯一真源"要当成接口语义变更来处理，而不是当成一次内部重构**：`verify_token` 改成
  "以账号记录为准"后，三个既有套件当场红（30→7 / 40→25 / 29→27，全是 401）—— 因为它们的脚手架
  靠"凭空签 token 冒充用户"造身份。生产行为是对的（记录不存在就该失效），**是测试过时了**。
  判据：先 `grep` 一遍**所有构造点**（这里是 `sign_token(`），再跑验证；只跑一遍单测会漏掉
  "测试自己就是调用方"的那些。
- **全量扫描里"没有汇总行"本身就是可疑信号，不能当通过**：首轮修复后我扫了 13 个脚本，
  `verify_auth_middleware.py` 打印的是 `（无计数行）`、rc=0，我就当它没事放过去了 ——
  第二轮才发现它其实 16→13。**判据：批量跑验证脚本时，扫描器要为"输出里没有可识别的结论行"
  单独报一格**（本仓的 sweep 现在按 `通过|全绿|有失败` 三模式匹配，空匹配要显式标出来）。
- **迁移路径要"只动被迁移的那一个字段"**：存量口令在登录时就地升级哈希（无盐 → PBKDF2）时，
  顺手改 `token_version` 就等于"用户一登录就把自己踢下线"（且很难查：表现为"登录成功后第一个请求 401"）。
  判据：数据迁移函数写完先问一句"这次调用**除了目标字段**还碰了什么"。
- **写"吊销清单"时先把事件分成「凭据变化」与「凭据使用」**：条目原文把"登录/改密/删用户"并列为吊销点，
  但登录是**取得**凭据、不是凭据失效 → 跟着吊销会把同一个人其它设备一起踢掉（多设备体验直接崩）。
  另外"明文 HTTP 上无条件加 Cookie `Secure`"也属同一类想当然：浏览器会**直接丢弃**该 cookie，
  症状是"登录成功（响应 200 + Set-Cookie）但紧接着每个请求 401"。判据：安全属性的启用条件要跟
  **实际传输层**走，不跟着"生产本该有 TLS"的愿望走；拒绝用户某个动作时（如强制改密），
  必须同时列出**用户自救的确切命令**（这里放行 `/api/auth/me`、`/api/auth/change-password`、
  `/api/auth/logout`，并让 403 响应体直接带上 curl），否则等于把人锁在门外。



- 测试代码里**直接** `store._conn.execute(...)` 并发读同样会中招 —— 校验读必须 `with store._lock:`，否则断言本身就会抽风。
- 跨进程用例（`eval_queue` claim）必须让父进程与子进程**用同一条路径**：子进程走 `EvalQueueStore()` 默认路径（`eval_queue._db_path()`），父进程若自己造一个 `path=` 就变成各写一个文件，两边都领不到东西。
- 子进程 stderr 里会有工作区种子告警（`WinError 183`，不影响逻辑）→ 判定要看 `returncode`，不要看 stderr 非空。
- 复刻旧实现做负对照时，把竞态窗口用 `time.sleep` **放大**（真实窗口只有几条字节码宽，不放大就只能偶发，负对照会失去判别力）。注意归因：旧写法在 A/C 两项里**先崩在并发读**上，还没轮到「seq 撞号」显现 —— 两者同根因，标签写的是意图，别读成「seq 撞号已独立复现」。
- **测时间阈值时，别让"一次等待"跨过"另一条阈值"**：P1-10 的首版用例取 `wait_seconds=0.2 / recycle_after=0.3`，结果**同一个等待循环**里就把回收触发了 → 「未到阈值仍繁忙」这条断言必失败。这不是被测代码错，是测试自己的时间假设错（两个阈值量级太近）。把比值拉开（等待 0.1s / 阈值 0.5s）就稳。**判据：阈值类断言里，两个时间参数至少差 3~5 倍。**
- **被测函数返回的是 MCP 的 `(content, artifact)` 二元组，不是裸字符串**：直接 `assert out == _MSG` 会失败（`out` 是 `(_MSG, None)`），而"很快返回"这类断言仍然通过 → 表现成"一半通过一半失败"的迷惑现场。对工具返回值断言时先看契约。

---

### 七、P2 执行记录（2026-09-24，代码于 **2026-09-24 已发版**）

| 项 | 脚本 | 结果 |
|---|---|---|
| P2-1 优雅停机（drain） | `scripts/verify_graceful_drain.py` | 76/76（**破坏性负对照**：预算置 0 → flush 时仍有 7 个后台任务在跑，证明"等到了"不是恒真；另一组断言"下游 app 一次都没被调用"证明拒绝发生在入口） |

运行方式同上（`PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_graceful_drain.py`）。
另有运维脚本 `scripts/ops_drain.py`（**不是套件**：容器内跑、给发版脚本用，无口令驱动排空端点），
它的"签出的 token 真能过鉴权"与"端点不可达时优雅退出 1"两条由 ⑥ 段覆盖；
发版侧改动在 `docs/weint环境/`（`release-backend.ps1` / `rollback-backend.ps1` / 手册 §二.1.1）。

**P2-1 补三条教训**（改"进程生命周期/停机路径"类改动通用）：

- **先读第三方源码确认"能不能拦"，再定方案形状**：条目原文把这件事写成配置级改动，实际
  `langgraph_runtime_inmem` 的排空窗口是**硬编码 5 秒**、`BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS`
  在本版本**没有任何读取点**（只在 config 里定义），唯一插入点是自定义 app 的 lifespan
  （`combine_lifespans` + `AsyncExitStack` 逆序）。照原文"调个参数"改，会得到一个看起来对、
  实际不生效的改动 —— 而它的失败形态正是"发版时那批半轮对话"，没人会归因到发版。
- **"配了就算数"的开关，要连同"它依赖的配置"一起钉住**：自定义中间件能拦到原生 run 路径，
  靠的是 langgraph 走默认分支（`middleware_order != "auth_first"`）。这条前提既不在我们代码里、
  也不会报错 —— 只能静态回归（脚本 ⑤：既断言默认分支的组装表达式在，也断言本仓没配那个键）。
- **整型截断会"静默跳过"整段逻辑**：`int(预算 − ε)` 在预算 1 秒时得 **0** → 整个等待被跳过，
  而日志一切正常（表现为"排空瞬完"）。凡是"剩余时间"参与判断的地方一律用浮点，并用
  **"耗时落在某区间"**的断言钉住（只断言"没超时"抓不到这类 bug）。

---

### 八、发版执行记录（2026-09-24，一次维护窗口内两件事）

同一次窗口做了两件事（授权范围仅此两项）：① 服务器侧 compose 补 `stop_grace_period: 240s`；
② 后端完整发版（整包 `src`，回滚 tag `nl2sql-api:rollback-20260924-1211`，tar 35.6MB）。

**发版顺序约束已满足**：P1-5 上线**之前**先在生产跑了 `scripts/backfill_thread_owner.py --apply`
（计划 0 条 / 新增 0 条）。0 条不等于漏跑 —— 生产 `DATABASE_URI: ":memory:"`，ops 层线程注册表在内存中，
`/threads/search` 只看得见本进程启动后建过的线程（当时 2 条），而那**恰好就是 REST 层 fail-closed
的实际影响面**（老 thread_id 不在注册表里，`require_thread` 类端点也够不着）。⚠️ 哪天注册表换成
postgres 持久化，这次回填就从 no-op 变成**必需品**，届时要先 dry-run 看分桶再 `--apply`。

**发版后核对**（都只读）：容器内 `/app/src` 与本地工作区**逐字节全等**（内容不同 0 / 仅生产有 0；
剩 15 个"仅本地有"是 `src/test/**`，被 `.dockerignore` 排除、不构建进镜像）；`/ok` 200、37 个 MCP 工具；
`GET /api/admin/drain` 对匿名 **403**（对照不存在路径 404 → 路由确实挂上了）；
日志 0 ERROR/CRITICAL；排空端点可读驱动 `{draining:false, remaining:0, budget_secs:180}`。

**发版脚本自身两处改动**（本次一并上线）：

- 第 ⑤ 步加 `docker exec … test -f /app/scripts/ops_drain.py` 前置探测。原因：**首个带排空的版本里
  容器还是旧镜像、没有该脚本**，`python <missing>` 以 **exit=2** 退出，而 exit=2 恰好也是"预算用尽仍有
  run 在跑"的码 → 会把"文件不存在"误报成"排空超时"。实测本次打印了跳过提示并落到第 ② 步（信号式排空）。
- 第 ⑥ 步加 `scripts/check_skills_drift.py`（**非致命**，exit=3 只 WARN）。

**发版后追加：P2-2 已实施（未发版）** —— 见本清单 P2-2 条目。三件事值得记：① 日志持久化**绕开了服务器侧改动**
（落 `/app/data/logs`，本就是 `agent_data` 持久卷；原方案"给 `/app/logs` 挂卷"不需要了）；② 唯一还欠的服务器侧
动作是 **nginx 补丁**（发布包不含 `docker/nginx.conf`，与 compose 补丁同类，**需点名授权**）；③ 生产实测
`/app/logs/agent-server.log` 确实在容器可写层、`NL2SQL_LOG_DIR` 为空 ⇒ 发版后自动落到 `/app/data/logs`，
**旧的 `/app/logs/agent-server.log` 会随重建容器一起丢**（升级那一次的历史日志，需要就重建前 `docker cp`）。

**本次唯一的欠账：运行期 skills 未同步**（`check_skills_drift.py` 第 ⑥ 步报出）。
生效的 skills 是外置运行期副本 `<AGENT_DATA_ROOT>/shared/skills`，**只在目录缺失时从镜像播种、
发版永不刷新** → 生产仍跑旧版 `main/chart-saver/scripts/save_chart.py`（**缺 `_reserve_dest`**）与旧
`SKILL.md`，即 **P1-3/P1-17 的"同秒同名不覆盖"那条线上没生效**（中间件本身在线上）。
同步命令（先备份再 `cp -a`，**不需要重启**：`SkillsMiddleware.before_agent` 每次调用都读盘）：

```
docker exec nl2sql-app_langgraph-api_1 bash -c 'tar -czf /app/data/skills.bak-$(date +%Y%m%d-%H%M%S).tgz -C /app/data/shared skills && cp -a /app/src/agent/shared/skills/. /app/data/shared/skills/'
```

**未做（如实记）**：① 生产 `scripts/e2e_thread_isolation.py`（会写测试数据）；② **本次发版带上的是未提交
的工作区**（74 项改动，`release-backend.ps1` 打的是整个仓库根）—— 要留痕就得 commit + tag。

**欠账已清（2026-09-24 补做）**：
- **运行期 skills 同步**（原第 ① 项）：备份 `/app/data/skills.bak-20260924-130243.tgz`（103KB）→ `cp -a`
  覆盖 → `check_skills_drift.py` **exit 3（2 个文件漂移）→ exit 0（一致）**；运行期
  `main/chart-saver/scripts/save_chart.py` 已含 `_reserve_dest`（4 处）⇒ **P1-3/P1-17 的「同秒同名不覆盖」
  这条线上生效了**。同步不需要重启。
- **nginx 补丁应用**（P2-2 的服务器侧一半）：见 P2-2 条目。
- **P2-3 已实施（未发版）**：`/metrics` + 最小告警，见本清单 P2-3 条目。它**没有服务器侧动作**
  —— 端点由后端自己提供、生产 nginx 不需要改（`/metrics` 落到前端 = 对外的自然屏蔽），
  也没有新增依赖（`prometheus_client` 随 langgraph 的 OTel exporter 早已在环境里）。
  采样任务由 lifespan 在**进程启动时**拉起 ⇒ 发版（第 4 步 stop/rm/up 重建容器）本身就生效，
  **不需要额外动作**；反过来，若只是 `docker cp` 拷代码而**没重启**，`/metrics` 会只有上游那部分、
  一个 `nl2sql_*` 都没有（这正是"采样任务没起"的特征）。
- **P2-4 已实施（未发版）**：子任务终态"待补写"+ 后台补写器，见本清单 P2-4 条目。**有服务器侧动作吗？
  没有新依赖、没有 compose/nginx 改动**（表落持久卷 `<AGENT_DATA_ROOT>/pending_terminal/pending_terminal.sqlite`，
  首次使用自动建库建表；**2026-09-25 起从数据根目录归位到同名子目录**，老库由首启的
  `sqlite_paths.adopt_all_stores()` 自动搬过去，运维无需动作）；**但必须重启**——补写器在 lifespan 里起
  （`docker cp` 拷代码不重启 ⇒ 补写器没起、watcher 仍会放手、`pending_terminal.sqlite` 会出现行却没人补）。发版后体检：
  `ls /app/data/pending_terminal/pending_terminal.sqlite` 应存在，`--status` 的 `pending` 应长期为 0
  （非 0 = 有终态补不进去，按 P2-4 条目里的排查指向 `nl2sql_run_queue_*` / `[alert] run_backlog`）。
- **P2-5 已实施（未发版）**：保留策略 + 磁盘水位告警，见本清单 P2-5 条目。**没有服务器侧动作、
  没有新依赖、没有 compose/nginx 改动**（全走 `AGENT_DATA_ROOT` 既有路径与已注册的 prometheus REGISTRY）；
  **但必须重启**——维护线程在 lifespan 里起（`docker cp` 拷代码不重启 ⇒ 清理器没起，而 `/metrics` 上
  看起来就像"没配置这项"，因为它根本不报"我关了"）。发版后体检：`docker exec … python -m agent.utils.retention --status`
  应给出七个目标的天数（`report`/`feedback` 为 0 = 关，**这是默认值、不是坏了**），并确认响应里
  `last_run.at` 是**启动后**的时间（说明启动即跑了一轮）；`/metrics` 里应出现 `nl2sql_disk_free_bytes`
  与 `nl2sql_data_bytes`。⚠️ **默认不清用户可见数据**：`report/` 与 `message_feedback.db` 默认关闭，
  要清必须显式给 env 天数 —— 打开前先 `--run --dry-run` 看清单。
- 下一项：**P2-6**（定时备份，含 `auth.sqlite` 加密）。

### 第二次发版（2026-09-24 14:07，用户手动发的前后端）

把 P2-2 / P2-3 / P2-4 / P2-5 四项一次性推上生产（后端重建镜像 + 前端重建产物）。**发版后的独立核对（只读）**：

| 核对项 | 方法 | 结果 |
| --- | --- | --- |
| 后端代码 = 本地工作区 | 双向 md5 逐文件比对（本地 312 个 .py vs 容器 188 个） | **内容不同 0 / 仅生产有 0**；仅本地 124 个全部是照例不发版的 `agent/workspace-temp/**`（109）+ `src/test/**`（15） |
| 三个 lifespan 线程都起了 | `docker logs … grep` | `[pending-terminal] 补写器已启动`、`[retention] 维护线程已启动（间隔 3600.0s，总开关=True）`、`[metrics] 指标采样已启动：间隔 10.0s` |
| 保留策略真在干活 | 同上（第一轮日志） | `[retention] workspace_tmp：已清 9 项（7 天前）` —— **启动即清，不是只挂了个线程** |
| `/metrics` 有自采指标 | 容器内直连 `127.0.0.1:2026/metrics` | `nl2sql_disk_free_bytes{path="/app/data"}`、`nl2sql_disk_used_ratio`、`nl2sql_data_bytes{area=…}`（10 个区域）、`nl2sql_run_queue_running`、`nl2sql_event_loop_lag_seconds`（+`_max` 0.002）、`nl2sql_mcp_servers{status="ok"}=2`、锁计量 6+ 个 store 都在 |
| 前端产物 = 本地构建 | `docker exec … cat .next/BUILD_ID` + 产物内 grep | BUILD_ID **两边同为 `54kO5sqagdLbZ0_CMz_D-`**；产物里 `onDisconnect:"continue"` **2 处**、`joinStream` 11 个 chunk ⇒ 前端那两项（切会话不打死 run / 继续按钮）已在线上 |
| 运行期 skills 无漂移 | `check_skills_drift.py` | **exit 0**（种子 50 / 外置 50 一致）⇒ 08-24 那次同步还在位，本次无需再同步 |
| 优雅停机的服务器侧配置还在 | `grep stop_grace_period docker-compose.yml` | `240s` 在位 |

**发版后体检要看的两个指标**（都不是"没配好"，是正常值）：`nl2sql_alert_active` 与
`nl2sql_llm_calls_total` 这类**带标签的**指标在**第一次触发/第一次调用之前不会出现在 exposition 里**
（`prometheus_client` 不给"零标签组合"造行）——重启后没发过告警、没调过模型，就查不到它们。

**本次发版的两点如实记**：
1. **没有打新的回滚 tag**（手动发版没走 `release-backend.ps1` 的第 2 步）：现存最新回滚点是
   `nl2sql-api:rollback-20260924-1211`，其镜像 `5b19d465…` 的**构建时间是 2026-09-23 16:41**
   （tag 名是打 tag 的时刻），而当前在跑的镜像是 `bebff1ec…`（本次构建、**无 tag**）。⇒ **回滚只能退到
   09-23，退不回今天这一次之前**。下次发版前建议先补一个回滚 tag（或直接走脚本）。
2. **日志里 `Unable to parse docstring for route …` 是无害噪声**（本次新增的 `/metrics` 也在其中）：
   本仓与 **langgraph 自带路由**都有这条（`/threads/{thread_id}/commands`、`/deploy/{operation_id}/stream` …，
   共 13 个路由各报一次），原因是**路由 docstring 以反引号开头**，Starlette 的 OpenAPI 生成器拿它当 YAML
   解析失败后退回纯文本。不影响功能、不用改；**别把它当成新版本的报错**。

### 九、共享单槽 ①②③ + P3-3（2026-09-24 晚，**全部未发版**）

一次连续会话里做完的三件事，**都还没进生产**：P2-10② 的共享单槽三处（早前拍板的「修共享单槽三处」）、
P3-3 工作区切换守卫（拍板的「加固单值语义」；⚠️ **2026-09-25 该守卫代码已随 T3 整体删除** —— 工作区不再可切换，
下面这节对 P3-3 的描述只作历史依据）、以及顺手发现的 P3-8（新增待办，只记未修）。

**统一修法**（三处同型）：① 目录名 / 缓存键 / 锁**共用同一身份**（把物化源 `src` 编进目录名）；
② 所有写操作先在 `.stage-<名>` 里做（`mkdir` 占位保证唯一），再用**两次同盘 rename**换入
（`dest → trash`、`stage → dest`，失败把 trash 换回）⇒ 读者永远看到「旧目录」或「新目录」，不存在半成品；
③ rename 目标被占用时 Windows 会 `WinError 5`（Linux 不受影响）⇒ 有界重试（8 次、线性 20ms）；
④ 按键加锁（同步模块 `threading.Lock`、`wren_semantic` 按解析后路径 `asyncio.Lock`）。

| 落点 | 改了什么 | 验证 |
|---|---|---|
| `src/api/wren_semantic.py`（即上文 P2-10② 的 ③） | 活跃 wren 项目目录改「staging 构建 → 原子换入」（原来就地 `context build` 重写活目录） | `verify_semantic_staging_swap.py` **86/86** |
| `src/agent/utils/semantic_db.py`（上文 ①） | `_materialize_semantic` 按 key 加锁 + 目录名含 `src` 标签 | `verify_semantic_materialize.py` **50/50** |
| `src/agent/utils/skills_versioning.py`（上文 ②） | `_ref_dir_name(ref, src)`（`<src标签>_<safe_ref>`）+ 按键锁 + staging 原子换入 | `verify_skills_ref_materialize.py` **55/55 ×3**（新） |
| ~~**P3-3**：`src/agent/workspace_manager/manager.py`、`__init__.py`、`src/api/workspace.py`~~ **代码已全部删除（2026-09-25 T3）** | ~~`WorkspaceBusyError` + `_guard_active_runs`（读不到计数 = 放行并 warning，**刻意不等于 0**）+ `?force=1` 运维出口 + API 409~~ → 连端点与切换动作一起下线 | ~~`verify_workspace_switch_guard.py` 50/50 ×3~~ → `verify_workspace_pinned.py` **56/56**（钉死路径 + 首启骨架 + 机件已删 + 缓存重挂） |

**⚠️ 改 `skills_versioning`（skills 物化）时必须同步改 `effective_skills_sources`**：顶层物化的 VFS 字符串是由**目录名**拼出来的
（不是 CompositeBackend 的挂载键，挂载键是固定的 `/offline_experiment/skill_refs/`），
两边必须用同一个 `_ref_dir_name`，否则症状是「物化了但读不到」。文件注释里已写明这条约束。

**P3-3 的两条如实记（都不是 bug，是边界）** —— 记录于 2026-09-24；2026-09-25 T3 连守卫代码一起删除，
所以这两条今天只在「若将来又想引入工作区切换」时才是前提：
1. **守卫不是互斥锁**：检查通过后、写入前仍可能有人提交新 run（只缩窗口）。真正的排空手段是 P2-1 的
   `drain`（停止接单）。
2. `WORKERS` 计数**含子 agent 的 run 与 sync 循环**（一次问数约 3 个 run）⇒ 门槛偏保守、不会误拦少。

**回归（同会话内跑的）**：`verify_chart_artifact_owner.py` 47/47、
`verify_skills_ref_materialize.py` 连跑 3 次 55/55（防偶发）。**离线套件里 `WORKERS` 恒空 ⇒ 新守卫透明
通过**，不会给离线测试制造假阳性。

**本轮同样未发版的其它项**（各自条目里已写细节，此处只做发版时的一次性索引）：
P2-8/P2-9（目标库两道闸/保留缺口）、P2-11+P3-4（`llm_gate.py` + 归因）、P3-7（wren 缓存 + 重活守卫）、
`state.next` 幽灵 next（`verify_phantom_next.py`）。**发版时这些要与上面的 ①②③+P3-3 同一个包走**——
本仓没有「局部发版」，`release-backend.ps1` 打的是整棵 `src`（未提交改动一起上生产）。

**同一天拍板并实施的另外三件（都未发版）**：

1. **`release-backend.ps1` 补 `--exclude=src/agent/workspace-temp`**（用户拍板实施）。
   实测复核：旧口径（只有 `--exclude=src/agent/workspace`）打出的包里 `src/agent/workspace-temp/`
   **2847 个条目**；补上后 **0 个**。附带发现：`src/agent/workspace` 目录**在本机已不存在**
   （工作区外置到 `AGENT_DATA_ROOT`）⇒ 那条旧排除项如今本来就是空转。`.env.dev` 与 `.claude/`
   是否排除**仍未定**（脚本注释里原样留待拍板）。
2. **前端「部署 URL」默认改为 `window.location.origin`**（用户拍板实施，**前端仓库**，等 rebuild 发版）。
   新增唯一解析点 `src/lib/deploymentUrl.ts`，改到 `page.tsx`（探活 fetch + 导出下载）、
   `ReportFileActions.tsx`、`ContextRing.tsx`、`providers/ClientProvider.tsx`（SDK `apiUrl` 兜底），
   并把 `SettingsDialog`/`ConfigDialog` 的「部署 URL 必填」放开（助手 ID 仍必填）。
   ⚠️ **口径不是「拒绝私网」而是「跟随 origin」**——本仓模型网关就在私网（`192.168.25.13:8100`）。
   ⚠️ 前端 `src/` 下 **20 个文件是 DLP 密文**（`src/lib/*.ts` 几乎全部，含 `config.ts`）⇒ 只能新增文件、改不到密文文件。
3. **P3-8 出站 SSRF 守卫**（用户拍板「现在修：只堵环回+元数据」）——新增 `src/agent/utils/net_guard.py`，
   接到 `api/model_config.py` 的 `test_config`/`probe_capabilities` 出站前（另在 `_probe_models` 加一道纵深防御）。
   **私网必须放行**（本仓模型网关就在 `192.168.25.13:8100`，"拒绝私网"这条常见修法会把正常功能一起拒掉），
   只允许 `http/https`，**解析后校验 IP**，逃生门 `NL2SQL_SSRF_ALLOW_LOOPBACK=1`。
   验收 `scripts/verify_net_guard.py` **70/70 ×3**（含「被拒时一次出站都没发」「私网照常探活」两条对照），
   回归 `verify_config_authz.py` 32/32。细节见 P3-8 条目。

### 十、工作区概念的评估（2026-09-25，✅ **已终结：T1+T2+T3 交付**）

> **结论落地（2026-09-25）**：评估结论（下面）是「只修 UI」或「读法 B」。实际采取的路径更彻底 ——
> **把多工作区机制整个删掉，形态定为「一个工作区、路径钉死 `<AGENT_DATA_ROOT>/workspace`」**。
> 依据：生产实测 `/app/data/workspaces.json` 只有一条 `default` → `/app/data/workspace`，**钉的就是线上
> 当前生效的路径 ⇒ 零迁移、零行为变化**。
>
> - **T1（收掉 403）**：删端点（`api/workspace.py`）+ 删前端三处入口（工作区 tab / 输入框下拉 / 三个徽章）⇒
>   403 与两条静默失败入口一并消失，管理员与普通用户都没有这个 tab（比"仅管理员可见"更省事）。
> - **T2（钉死唯一路径）**：`active_workspace` 恒为 `_DEFAULT_WORKSPACE_DIR.resolve()`；删除 `workspaces.json`
>   注册表与 `WORKSPACE_PATH` env 两条分支；`active_name` 恒 `"default"`（run metadata 标签读者不变）；
>   `__init__` 补调 `_init_workspace_dirs()` —— **这是全新部署唯一的初始化点**（仓库里没有 `src/agent/workspace` 种子）。
> - **T3（删机件）**：注册/切换/删除 CRUD、P3-3 的 `_guard_active_runs` / `WorkspaceBusyError` / `?force=1`、
>   `cache_reset.py`、两个旧 verify 脚本、旧副本（`skills/`、`shared/skills_bak/`、`prompt_bak/`、`workspace-temp/`）全删。
> - **⚠️ 唯一的真实功能风险**：`cache_reset.py` 是 `_db_name_norm_cache` / `_semantic_override_cache` 的**唯一**
>   失效入口，删除后必须重挂 —— 新增 `semantic_db.invalidate_db_discovery_caches()`，由 db_config（3 处）与
>   语义库（1 处）的**写路径**调用。漏挂的症状是"新建模的库被判未建模 → 静默掉到 dbmcp 直连，口径变且不报错"。
> - **验收**：新脚本 `scripts/verify_workspace_pinned.py` **56/56**（含负对照：盘上放一个指向**真实存在**目录的
>   `workspaces.json` + 设 `WORKSPACE_PATH`，断言都没被带走），另加全量回归 + 前端 `tsc`/`next build`。
> - 前端改动在仓库 `harness-deep-agents-ui`；发布顺序**先前端后后端**。

**起因**：新用户登录 → 设置→工作区 → 前端提示 **HTTP 403**。

**根因（已核实）**：后端五个工作区端点全部 `require_admin`（`api/workspace.py:58/73/117/161/205`），
而前端「工作区」tab 在 `BASE_TABS` 里（前端 `SettingsDialog.tsx:73`；`isAdmin` 只用来插「评估/用户管理」，
`:77-86`）⇒ 非管理员点开面板 → `listWorkspaces()` 403 → 面板把 403 当错误文案显示。另两处入口是**静默失败**：
聊天输入框的工作区下拉（`ChatInterface.tsx:1632` → `WorkspaceSelector.tsx`：列表 403 被 `console.error` 吞，
手点切换会 `alert("切换失败: HTTP 403")`）、以及模型/数据库/语义库面板的徽章（`WorkspaceBadge.tsx` 的
`catch { /* ignore */ }` ⇒ 静默不显示）。**不是新引入的后端 bug，是前端没跟上 P3-3 的管理员门禁。**

**已核实的作用域矩阵**（以 `manager.py:340-348` 注释 + 实际代码为准）：

- **按工作区隔离**：`db_config.json`（`:398`）、**语义库根 = 工作区根**（`:436`，Wren 项目直接放在工作区目录里）、
  `report/`（`:415`）、`tmp/`、`nl2sql_process_data/`、`large_tool_results/`、`eval/`（badcase 状态 / feedback gates /
  experiment_runs）、agent 的 VFS 数据根。
- **全局共享**：checkpoint（`shared_checkpoint_dir`，`checkpointer_factory.py:79-80`）、trace、fts、feedback
  （`feedback/store.py:231`）、memory、skills、共享 `model_config.json`。注意 `checkpoint_dir` / `feedback_dir`
  这两个"工作区级"属性**只是展示用**（`:387-395` 注释自認）。
- **按用户**：`users/<uid>/model_config.json`（每个账号一份，**新账号从空开始、不继承共享配置**，
  2026-09-28 变更；见 memory `model-config-user-first-seed` 与
  [新账号模型隔离（不继承共享配置）方案](../agent优化记录/新账号模型隔离（不继承共享配置）方案.md)）。
- **用户维度不经工作区**：多人隔离是 `grants(db_name)` + `thread_owner` + `report_owner` 账本做的
  （`auth/grants.py:105-136`）⇒ **同一工作区里所有用户共享同一份 db_config.json / 报告目录 / 语义库**，
  靠账本过滤。

**结论**：工作区**不是**多用户隔离机制（隔离方案文档头部已自认那段矩阵过期），它是**部署级「一个部署跑几个
互不相干项目」的物理分区**。因为它是**进程级全局单值**（切一次影响所有人），才被迫限管理员 —— 也就才有了这个 403。

**用户提出的「每个用户固定一份工作区」评估**（先不实施）。要拆成两种读法：

- **读法 A = 每用户私有**一份工作区（用户之间零共享）。
- **读法 B = 每用户绑定**到某个**共享**工作区（工作区 = 项目组）。

**读法 A 的代价（逐项）**：

1. **DB 配置与凭据**：`db_config.json` 是工作区级单个文件、密码 AES-GCM 加密（`db_config_store.py:122`、`:1`）。
   每用户一份 ⇒ 要么从共享拷贝（= 把「新用户共用 admin 的 key」这个模式从 1 个模型 key **扩散到 N 个库密码**，
   且改密码要刷新 N 份），要么每人自配（`grants` 那套"管理员配一次 + 按库授权"作废，普通用户得自己知道
   库地址/账号）。两条路都不好。
2. **语义库与口径治理**：Wren 项目物理就在工作区根 ⇒ 每用户一套副本，**「统一口径」（S1~S5）立刻碎裂**：
   管理员改一次口径要同步 N 份，A/B 结论不再可比。物化缓存键里含**源路径**
   （`semantic_db.py:197-219`，落 `<data_root>/offline_experiment/semantic_refs/<db>/<src标签>_<ref>`）
   ⇒ 每人一份源 = 每人一套物化副本，**磁盘 × 用户数**。
3. **子进程与资源**：活跃工作区数从 1 变成「用户数」，而 MCP 条目/工具集是按工作区加载的。并发天花板本来
   就只有 10 个 run 槽（见 memory `concurrency-ceiling-10-run-slots`）⇒ 再叠 N 套工具集不值。
4. **协作损失**：报告/图表/中间产物今天是"共享目录 + 归属账本过滤"（`report_owner`、chart 归属账本，刚做完）
   ⇒ A 把它们变成物理隔离，同事互看报告要靠导出/导入。
5. **A 唯一真正的收益**：能把全部"共享目录 + 归属账本"的过滤逻辑删掉（隔离物理化）。但**账本已经做完了**
   ⇒ 省下的是维护成本，不是新增能力。

**读法 B 的形状**（推荐形态，仍未实施）：

- `users` 表加一列 `workspace`（管理员在用户管理里指派；**未指派 = 回落全局活跃工作区 ⇒ 零迁移**）。
- 每个 run 的活跃工作区 = 该用户的绑定，**取代进程单值**；不再有"运行时切换"。
- 可顺带把 `grants` 的"库级过滤"退化成"绑定哪个工作区"（二者并存也不冲突）。
- **保留**：一个项目组共享一套库配置/语义库/报告 ⇒ 口径统一不破。
- 待定的产品问题：一个人需要跨两个项目的数据怎么办（绑多个 ⇒ 又回到"选工作区"）。

**两种读法共同的技术前置**（跟选 A 还是 B 无关，都要做）：

- **拆进程级单值**：agent 侧约 20 处直接读 `wm.active_workspace`（`DynamicFilesystemBackend` 的 lambda、
  `path_resolver.py:23`、`report_builder.py:78/124/670`、`execute_guard.py:126`、`eval/*`、`vfs_path_resolver.py:43`…）
  ⇒ 改成"按运行上下文解析"（ContextVar，在 run 入口按 user_id 设置），全局值留作兜底。
- **全局缓存按工作区键控**：`_sub_entries`（MCP 条目）、wren 引擎缓存、语义库物化、skills 物化、
  db_config store 单例 —— 今天全靠"切换时清一遍"（`cache_reset.py`，**清单漏一项就是旧工作区工具仍可调 = 越权**）。
- **面比看着小**：`DynamicFilesystemBackend` 已经是 lambda 取根（换成读 ContextVar 即可）、`semantic_db`
  的缓存键已经含 source。工作量主体是"把清缓存式切换换成按工作区键控的多实例"，而这正好让上面 §九
  三处共享单槽的修复**退休**。

**判据与结论（2026-09-25 用户拍板：先不实施）**：

| 情况 | 走哪条 |
|---|---|
| "多项目"其实 = 多套部署 | 什么都不用做，只修 UI（非管理员隐藏工作区入口） |
| 一个部署 + 两个项目组 + **同时在用** | 走**读法 B**（用户绑定共享工作区），**不要走读法 A** |
| 用户之间本就不共享任何数据（≈ 多租户 SaaS） | 才是读法 A 成立的前提；届时共享账本/口径治理/审批都要拆 |

**未做**（原计划）：前端非管理员隐藏工作区三处入口（tab / 输入框下拉 / 徽章）。→ ✅ **已以更彻底的方式完成**
（2026-09-25 T1：三个入口连同整个工作区 UI 面一起删掉，不再区分管理员）；
`db_config.json` 是"一个文件装 N 个库"这一点也说明：**多项目/多库本身不需要多工作区** —— 这正是最终把多工作区
机制整个删掉、只留"一个工作区 + 钉死路径"的直接理由。


---

### 十一、运行时库「一库一目录」+ 老文件接管（2026-09-25，**未发版**）

**起因**：运维看 `/app/data` 根目录，`pending_terminal.sqlite`、`trace_bind.sqlite`（外加同处的
`eval_queue.sqlite`）连同 SQLite 自动生成的 `-wal`/`-shm` 一共 9 个文件散在数据根上，和
`shared/`、`workspace/`、`logs/` 这些目录混在一起，看不出谁是谁。

**做法**：三个库各自归到同名子目录 —— `<AGENT_DATA_ROOT>/<name>/<name>.sqlite`，三件套同处一层，
根上不再有散落的 `.sqlite*`（对齐既有先例 `auth/auth.sqlite`、`<shared>/checkpoint/`、`<shared>/trace/`）。

- **实现**：[utils/sqlite_paths.py](../../src/agent/utils/sqlite_paths.py)（`data_root()` / `store_db_path()` /
  `adopt_legacy_store()` / `resolve_store_db()` / `adopt_all_stores()`）。三个 store 的 `_db_path()`
  （`eval_queue` / `trace_bind_store` / `pending_terminal`）各一行改调 `resolve_store_db()`；
  `retention` 的 3 处路径（量占用 2 处 + `--run` 清理 + `vacuum`）改走 `store_db_path()`（**纯函数，
  量一下不能顺手搬文件**）。
- **老数据不会丢**（本次唯一有风险的一面，也是必须写代码而不是写手册的原因）：升级**首次启动**时，
  若新路径还没有库、根上还留着老三件套，就 `os.replace` 搬过去。规则钉死：
  **目标已存在绝不覆盖**；**先搬 `-wal`/`-shm`、最后搬主库**（反过来会让已提交未 checkpoint 的事务永久丢失，
  且下次启动"目标已存在"不会再补）；失败只 warning + 放弃本轮，**不半搬**（主库搬不动时 sidecar 可能已就位，
  主库留原地、下次启动收敛）。⚠️ **但接管只有一次机会**：同一次启动里补写器/保留线程立刻会在新路径建出空库，
  此后每次启动都因 `new.exists()` 直接跳过 ⇒ 老文件会**永远**留在根上，**重启不会自愈**。失败后的手工收尾
  （复制而非移动、核条数、最后才删）写在部署手册「四、数据与持久化」。
- **接线**：`custom_app._lifespan` **最前面**调 `adopt_all_stores()` —— 必须早于 `start_reaper` /
  `start_maintenance`（它俩会建连，搬运前提是"没人打开过那份老库"），也解决 `trace_bind`/`eval_queue`
  惰性建连导致的"没人用就一直躺在根上"。
- **验收**：`scripts/verify_store_layout.py` **36/36**；回归 `verify_retention` 76/76、
  `verify_pending_terminal` 69/69、`verify_store_concurrency` 14/14、`verify_workspace_pinned` 56/56、
  `verify_metrics` 87/87。
- **服务器侧动作：无**（无新依赖、无 compose/nginx 改动、无需手工 `mv`）；**但必须重启** ——
  归位发生在 lifespan 里，只 `docker cp` 不重启则老文件仍留在根上（新库也不会建）。
  查证命令与"没搬成怎么办"写在部署手册「四、数据与持久化」。

### 十二、知识类取料结果免截断 + 提示词读回约束（2026-09-25，**2026-09-26 md5 核实＝已发版**）

**起因**：生产 trace `2f98ed67b2f63e6ea1516cc722ae587a`（问「丛培强 本月工时分析」，
357.77s、140 个 observation、10 个 ERROR、零 SQL/零图表）—— 其中 9 个 ERROR 是
`read_file`/`grep`/`glob`/`ls` 去文件系统找 `knowledge/rules/报工与工时.md`、`knowledge/sql/月度工时统计.md`
被 `NL2SQL_FILE_PERMISSIONS` 拒，第 10 个是随后的 `ChatQwen` 243.455s 超时（60s×4）。

**根因（三段链，全部代码可验，**不是**"缺读权限"）**：

1. 规则轴 `wrenai_<库名>_get_instructions` 返回 `knowledge/rules/*.md` 的**无边界拼接**
   （实测 20,919 字符 = 业务域口径 + 报工与工时(R1~R8) + 通用规则，按文件名序）
   ⇒ 内容**本来就在模型上下文里**；
2. `MessageSlimmerMiddleware`（**子 agent 也挂**，[nl2sql_agent.py:256](../../src/agent/graphs/nl2sql_agent.py)，
   阈值 `LARGE_RESULT_TRUNCATE_CHARS`=8000）把它落盘成 head5/tail5 预览，**R1~R8 正文正好在被砍掉的中间**；
   而模型读到的头 5 行里恰好写着「工时专项见 `报工与工时.md`」；
3. 模型没照 stub 去 `read_file /workspace/large_tool_results/<id>`，而是猜 VFS 路径
   （`/workspace/knowledge/rules/…`，**少了一段不可推导的语义库目录名**，真路径
   = `/workspace/<db_config.wren_project 目录名>/knowledge/<5 分类>/<名>.md`），把
   `permission denied` 读成"路径写错了"继续找 ⇒ 9 次拒绝 + 超时。

**修法（A + A′ + A″，本次落地）**：

- **A″ 免截断白名单**（[message_slimmer.py](../../src/agent/middlewares/message_slimmer.py)）：
  新增 `_KNOWLEDGE_EXEMPT_MAX_CHARS`（默认 60000）+ `_is_knowledge_tool()`，知识类工具
  （`get_instructions` / `get_all_knowledge` / `list_knowledge`，**按后缀匹配**以兼容
  `wrenai_<库名>_` 前缀与裸名）结果在 60000 字符内不落盘。理由：知识料是本子任务**唯一**的
  业务口径来源、且按 wren-retrieve「唯一性铁律」一次取齐全程只读 ⇒ 用"模型找不到口径"换这点
  上下文完全不值。超上限仍落盘兜底（不设上限会把上下文撑爆），构造参数传 `None` 可关上限。
- **A 提示词**（[NL2SQL_SYSTEM_PROMPT.md](../../src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md)）：新增 **§9.1**，
  并删掉误导性的那句「`list_knowledge()` + **按需读取**」（**它正是这次事故的起点**）：
  要全文就 `read_file` 工具结果里给出的落盘路径；正文里的 `xxx.md` 是**来源标注**（内容已在同一份
  返回里）；🚫 禁止用 read_file/grep/glob/ls 找 `knowledge/**`（不在可读 VFS 通道内）；
  拿到 `permission denied` **不要换路径重试**。§十二 关键提醒里也留了一条（高可见位）。
- **A′ 技能**（[wren-retrieve/SKILL.md](../../src/agent/shared/skills/nl2sql/wren-retrieve/SKILL.md)）：
  新增「落盘读回」规则，并改掉原文里那句同样有害的「`list_knowledge()` + **按路径读**」。

**验收**：`scripts/verify_slimmer_exempt.py` **42/42**（8 组：工具名识别 6 命中/14 未命中、
20,919 同形态载荷不落盘、非知识类仍落盘、60,000/60,001 边界、async 真入口 `awrap_tool_call`、
fail-open、两处接线、提示词与技能文本断言含"旧文案必须消失"）。回归 `verify_chart_artifact_owner.py` 47/47
（图表免截断与新豁免的先后顺序未破坏图表链路）。

**发版动作（三件套，缺一不生效）**：① 后端整包发版（代码 A″）；② `python -m agent.prompt.sync_prompts --all`
（**system prompt 住在 Langfuse**，发版只把本地 `.md` 当种子送进镜像）；③ 运行期 skills 同步
（release-backend.ps1 第 5/7 步自动整目录替换）。**②③ 之后必须重启**。

**本次未做（另外两条通道，按需再定）**：
- **B 开读权限**：给子 agent 的 `NL2SQL_FILE_PERMISSIONS` 放行 `/workspace/*_semantic/knowledge/**`
  并注入真实语义库目录绝对路径（目录名不可推导，见上）。**与 P1 隔离口径属例外，需点名**。
- **C wren 侧**：① `get_instructions` 拼接加文件边界标记（`## 文件: knowledge/rules/xxx.md`）；
  ② **`recall_queries("丛培强 本月工时分析", limit=3)` 返回 `{"matches": []}`** —— 库里明明有
  `knowledge/sql/月度工时统计.md` ⇒ **范例轴这条通道等于没有**，根因在 wren 侧（索引/入库/嵌入），**未查**。
  这一条比 B 更值钱：范例召回能用，模型根本不需要读文件。

### 十三、展示 skill 归属表更名 + 独立 wren-execution 技能（2026-09-25，**2026-09-26 md5 核实＝已发版**）

**起因**：生产 Langfuse 里出现 span 名 `skill:nl2sql-understand:wrenai_witops_list_knowledge`，
但盘上**没有任何** `nl2sql-understand` 目录（运行期 shared 与镜像种子都只有 `wren-*`），
线上 `langfuse_span.py` 与本地逐字节相同，Langfuse 里的 system prompt 也全是 `wren-*`
⇒ 这个名字 **100% 由硬编码的 `_TOOL_OWNER_SKILLS` 表产生**，且走的是
`_resolve_display_skill` 优先级 ③「唯一归属 → 直接 `return owners[0]`」那条早退分支。

**危害不是名字难看，是同一个技能被拆成两个 tag**：`read_file` 命中 SKILL.md 走
`_skill_name_from_path`（返回**真实目录名**），其它工具走本表（返回旧名）⇒
`skill:wren-retrieve:read_file` 与 `skill:nl2sql-understand:list_knowledge` 在 Langfuse 里
是两个不同 skill，**按 skill 过滤/聚合、离线实验的 skill 维度分组全被拆开**。
（打分不受影响：`_maybe_score` 走的是另一张泛化名表 `TOOL_SKILL_MAP`；agent 行为也不受影响。）

**修法**：

- **归属表更名**（[langfuse_span.py](../../src/agent/middlewares/langfuse_span.py) `_TOOL_OWNER_SKILLS`）：
  owner 全部改为真实目录名。取料/探查族 12 个工具（`get_context` / `get_instructions` /
  `get_all_knowledge` / `list_knowledge` / `recall_queries` / `describe_schema` / `list_cubes` /
  `get_mdl` / `describe_model` / `get_data_source` / `get_db_info` / `describe_cube`）
  → **`wren-retrieve`**；`query_cube` → `wren-metric-query`；`dry_plan` → `wren-sql-author`；
  `run_sql` → **`wren-execution`**（见下）；共享工具 `dry_run` 的 owners 同步换成
  `wren-sql-author` / `wren-orchestrator` / `wren-perf-optimize` / `wren-metric-query` / `wren-execution`。
- **新增独立技能 `wren-execution`**（[SKILL.md](../../src/agent/shared/skills/nl2sql/wren-execution/SKILL.md)，
  旧名 `nl2sql-execution` 的对应物）：`run_sql` 从"编排器的一个动作"升为**第 (6) 步的独立技能**，
  明确「执行前检查三条」（dry_run 门已过 / 改过必复验 / 行数走参数且正文不写 LIMIT）、
  行数契约、只读铁律、**大结果落盘的呈现契约**（一句话结论引用 `row_count` + 前 20 行样例 +
  `full_result_file` 路径；禁止逐行重打、禁止 read_file 照抄、禁止把 20 当业务口径）、
  失败处理表（语法/口径类回 `wren-sql-author` ≤3 次；超时/权限/连接类**不重试执行**）。
  `wren-orchestrator` 的 skill 清单 / 六步流程 / 错误处理三处、系统提示词 §三(6) 与 **§四技能名称列表**
  同步登记。
- **清掉模型可见字符串里的旧技能名**（比归属表更容易漏，因为它们是**回给模型的文本**，
  点名一个不存在的 skill 会让模型去 `read_file` 它）：
  `query_gate.py` 的 `_HINT`（"请先按 nl2sql-understand skill 的顺序" → `wren-retrieve`）、
  `write_todos.py` 的 `WRITE_TODOS_PROTOCOL`（每轮注入 system prompt）、
  `wren-clarify` / `wren-perf-optimize` SKILL.md、「main-agent」SKILL.md 的子技能清单。
  顺带删掉 `main-agent/SKILL.md` 里指向**不存在文件**的
  `python skills/sql-of-thought/scripts/gen_models_mysql.py`。

**验收**：`scripts/verify_skill_owner_table.py` **41/41**（8 组：owner 全是真实目录名；
旧名词汇清空；生产实测那条调用解析为 `wren-retrieve` **且旧表作负对照**；
唯一归属覆盖过期活动 skill（用真机制 `read_file .../SKILL.md` 设活动 skill——直接往
thread_id 里塞 skill 名是无效的，第一版断言就栽在这里）；共享工具 `dry_run` 的继承与
回退两侧；目录名 == frontmatter `name`；owner 表 ⊂ `TOOL_SKILL_MAP`；提示词/各 SKILL.md
技能清单与盘上目录一致；模型可见字符串不点名不存在的 skill 且 `progress_boundary` 的阶段
别名仍覆盖协议里的步名）。回归 `verify_db_exec_authz.py` 32/32（`query_gate` 文件名与
`QueryGateMiddleware` 挂载点未动）、`verify_slimmer_exempt.py` 42/42。

**发版动作**：与 §十二 同一批（后端整包 + `sync_prompts --all` + 运行期 skills 整目录同步，
后两项之后必须重启）。归属表与 `query_gate`/`write_todos` 的字符串改动只需**发版+重启**；
`wren-execution` 技能与提示词 §四 需要那三件套齐全。

**顺带发现（未动，登记备查）**：`src/agent/memory/AGENTS.md`（366 行，10 处旧技能名）
是**死副本**——唯一引用 `config.py:84 LOCAL_AGENTS_MD` 自身零消费点；活文件是
`src/agent/shared/memory/AGENTS.md`（只有两处"已整体归档，不加载"的陈述，与新技能集不冲突）。
`src/agent/trace/skill_manifest.py` 的 docstring/注释里拿 `nl2sql-sql-generation` 举例
（仅示例文本，不影响 manifest 内容——它扫的是盘上真实目录与 frontmatter）。

> **⚠️ 2026-09-26 更正（推翻上一段与本节标题里的「未发版」）**：生产容器内
> `docker exec md5sum` 与本地逐字节比对 `message_slimmer.py` / `langfuse_span.py` /
> `report_builder.py` **完全相等** ⇒ §十二 与 §十三 的改动**随 09-23/24 的整包发版一起上线了**。
> 原因见 §八：发版是**整包 `src`**，未提交改动一并上生产，所以仓内任何「未发版」标记都不作数
> （与 [发版是整包 src] 同一结论）。判断线上跑什么**只有 md5 一条路**。

---

### 十四、报告「业务口径」节（2026-09-26，代码已在生产、契约已推并重启，**生产 E2E 未做**）

**起因**：生产 trace `294bbc0fd225de5a8def3faf4465c156`（2026-09-25 16:34，问「丛培强 本月
工时分析」）生成的报告**零业务口径**。用户诉求：报告必须包含业务口径。

**三条候选原因，只有一条为真**：

- ❌ **口径原文没进模型上下文** —— **不成立，且我一度据此下了错判**。生产日志实证
  MessageSlimmer 知识类免截断生效：`知识类结果 wrenai_witops_get_instructions 20049 chars 免截断`，
  `rules/报工与工时.md` 原文**确实进了子 agent 上下文**。Langfuse 里那个 92 字符 stub 是
  `langfuse_span._finish_span` 自己写的**展示用**指针（模型拿到的仍是原文）。
  已把该指针改为 `[large result truncated: {n} chars, see {vfs}]`——带上原长度，避免下次
  再被它误读成"模型没拿到内容"。
- ❌ **Cube 通道故障** —— 不成立，但**通道确实没走成**：`query_cube` 首调 `filters` 写成
  `user_name:=:丛培强`（wren 的 op 是**枚举名**不是比较符）⇒ `unknown variant` 直接失败、白丢
  一次调用；且全程 `sql_only: true`（只回编译 SQL、不回数据）⇒ 最终数据来自 `run_sql`。
- ✅ **报告装配层缺口径节** —— **真因**。

**根因（代码可验）**：口径节原先**只存在于 Cube 通道**（Layer A LLM 摘要 / Layer B 模板结构 /
Layer C 原始定义），而 [check_progress.py](../../src/agent/subagents/check_progress.py) 里
`cube_query` **只在没有 `run_sql` 的 else 分支**才回填（注释即写"两者互斥"），
[report_builder.py](../../src/agent/tools/report_builder.py) 又是 `if sql:` 优先
⇒ **SQL 通道的报告天然没有口径节**。口径只剩模型自己写进 `analysis` 的那点「查询口径」
（本次是统计范围，不是业务定义）。

**修法：三通道汇进同一个渲染器**（[report_builder.py](../../src/agent/tools/report_builder.py)）

1. **主路径 —— 从子 agent 结果里确定性抽取**。子 agent 最终回复**末尾**附 `## 业务口径` 块
   （逐条 `口径项 | 内容 | 出处`）；`_parse_caliber_block()` 抽（兼容 `- a | b | c` /
   `1. a | b | c` / `| a | b | c |` 表格三种写法，取**最后一个**同名标题，下一个标题即结束，
   上限 20 条），`_render_business_caliber()` 渲染成三列表。
   **为什么必须"抽"而不能只靠主 agent 转述**：口径原文（`knowledge/rules/*.md`）只有**子 agent**
   手里有，主 agent 只看得到 `check_async_task` **摘要过**的结果。
   解析不出三段的条目**降级为列表项**，绝不因格式问题丢内容。
2. **主 agent 显式传参** `business_caliber=[...]`（优先级最高，覆盖抽取）。
3. **兜底（env 开关，默认关）** `REPORT_CALIBER_APPENDIX_MAX_CHARS`（默认 `0`）：>0 时按当前
   db_name 经 `resolve_wren_ctx_by_db` 解析 wren 项目目录，把 `knowledge/rules/*.md` 原文按
   文件名序附到报告末尾 —— 唯一**不依赖模型自觉**的通道，带文件名小标题可追溯，任何一步失败
   fail-open（报告照出）。

**契约文本（缺一处就是静默回退）**：[NL2SQL_SYSTEM_PROMPT.md](../../src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md)
§十一 输出规范（三部分：Markdown 描述 / JSON 数据块 / **末尾**业务口径块）+ §十二 提醒
（"业务口径要交出去"）；[MAIN_AGENT_PROMPT.md](../../src/agent/prompt/MAIN_AGENT_PROMPT.md)
子任务成功后的固定协议 c；[wren-execution/SKILL.md](../../src/agent/shared/skills/nl2sql/wren-execution/SKILL.md)
结果呈现第 4 条（并加禁止项"凭印象补口径或编造出处"）。

**顺带修的两个真缺陷**：

- **Cube 分支的 Layer B 被 Layer A 的开关连坐**：`查询结构`（维度/过滤树，取自 cube 元数据、
  无 LLM 调用）原先和 Layer A 一起挂在 `not _caliber_md` 条件下 ⇒ 子 agent 一给结构化口径，
  报告就连查询结构一起丢（**静默缺口**）。已把 Layer B 移出该开关。
- **降级路径多余转义**：解析不出三段的条目走列表项，原先整行也过 `_cell()` 转义竖线 ⇒
  输出 `\|`。转义只对表格单元格必要，已只在表格路径转义。

**关键假设已离线钉死**：`check_async_task` 的 result 是**摘要过**的（`_MAX_RESULT_CHARS`=2000），
两条摘要路径（纯文本头尾各半；含表格把表后正文按剩余预算头尾保留）**都保留尾部**
⇒ 放在回复末尾的口径块活得过摘要。verify 脚本用**真** `_summarize_result` 断言（3345→2073 字符
后仍抽得到），不是靠推理。

**验收**：新增 `scripts/verify_report_caliber.py` **57/57**（10 组：抽取器四种写法与边界 /
渲染器降级与转义 / schema 默认值与描述约束 / **SQL 通道端到端**出节且各节序号连续无重复 /
显式传参优先 / 无口径则整节不出现且不虚报 / Cube 通道 Layer A 让位 **+ Layer B 负对照** /
兜底开关三态（默认关、开启、解析失败 fail-open）/ 摘要存活 / 提示词与技能契约在位）。
回归 **全 32 个 `verify_*.py` 全绿**（含 `verify_slimmer_exempt` 42/42、`verify_skill_owner_table`
41/41、`verify_report_ownership` 40/40、`verify_chart_artifact_owner` 47/47）。

> 另注：`filters` 的 op 枚举（`eq` 而非 `=`）与「要出数就摘掉 `sql_only`」两条坑已写进
> [wren-metric-query/SKILL.md](../../src/agent/shared/skills/nl2sql/wren-metric-query/SKILL.md)，
> 并带正反例与本次 trace 号。

#### 交付（2026-09-26）—— 真因是**交付渠道**，不是代码

发版后用户又给了一条 trace（`2bad1d86ac9a66f0e5baf365c39d03cd`，2026-09-26 10:17），报告**仍然零口径**。
核对下来代码侧完好：容器内 `report_builder.py` / `langfuse_span.py` / `message_slimmer.py`
与两个 prompt `.md` 的 md5 与本地**逐字全等**。问题出在**运行期实际读到的那两份文本**：

```json
"prompt": {"prompt_label": "production",
           "prompt_versions": {"main_system_prompt": 1, "nl2sql_system_prompt": 2},
           "source": "langfuse"}
```

运行期用的是 **Langfuse v1/v2（10096 / 3265 字符）**，而本节写的契约文本在 v3/v2（11488 / 3952）
⇒ §十一 的 `## 业务口径` 块、§十二、§9.1 读回、`wren-execution` 一节**一个都没生效**；
运行期 `/app/data/shared/skills` 也还是旧树（13 个 SKILL.md、无 `wren-execution`）。
**教训：判「代码上线没有」用 md5；判「提示词上线没有」md5 一点用都没有**——详见
[[live-prompt-version-authority]]。

已执行（用户点名）：

| 步骤 | 动作 | 结果 |
|---|---|---|
| 1 | 原子替换运行期 skills（先 `tar` 备份） | 13→**14** 个 SKILL.md，含 `wren-execution`；两文件 md5 与本地全等；备份 `/app/data/skills.bak-20260926-103502.tgz` |
| 2 | `python -m agent.prompt.sync_prompts --all` | `nl2sql_system_prompt` v2→**v3**、`main_system_prompt` v1→**v2**；7 个 marker 全命中 |
| 3 | 重启（`stop -t 240` → `rm -f` → `up -d --no-deps`） | 启动日志 `[langfuse] prompt nl2sql_system_prompt(label=production) v3 生效` / `main_system_prompt … v2 生效`，且无「回退本地」告警 |

⚠️ 步骤 2 **不能写成 `--all --skills`**：该脚本三个开关原先是 `if/elif` 链 ⇒
`--all --skills` **静默只跑 `sync_skills()`**，两个 system prompt 一个都没推，输出里却打着
`SYNC_DONE`，看着像成功。已在本地改成各自独立判断（可组合），负对照 `d:\tmp\test_sync_cli.py`
**6/6**；**该 CLI 修复未发版** ⇒ 镜像更新前，服务器上要分成两条命令跑。

**机制债（未处理）**：

- **`sync_prompts` 不在发版脚本里** ⇒ **任何提示词类修复发版后都不会生效**。
  用户 2026-09-26 明确「暂不加，先手工同步」；建议后续把 `--all --skills` 加进
  `release-backend.ps1`（位置同第 5 步，**必须在重启之前**）。
- 本次发版**第 5 步（同步运行期 shared）未生效**：宿主机树与镜像种子都已是新的 14 个
  （`md5=4ac68d7c…`），唯独运行期还是旧的 13 个；当时 drift 检查已 WARN
  「外置那份不会随发版更新，技能类修复此时不生效」，未被当回事。

**待办**：重启后的生产 E2E —— ✅ **2026-09-26 11:00 已过**，trace
`9c81d3181f2a72f27cf9d092d7185fab`（同一问题「丛培强 本月工时分析」、`db_name=witops`，重启后 19 分钟）：

| 判据 | 结果 |
|---|---|
| trace metadata 的提示词版本 | `{"prompt_versions":{"main_system_prompt":2,"nl2sql_system_prompt":3},"source":"langfuse"}` ✅ |
| 报告含 `## 业务口径` 节且每项带出处 | ✅ 第 2 节三列表 6 条，出处逐条标注（`workhour_analysis（Cube）`/`v_workhour`/`语义库字段字典`），末尾带"口径取自语义库知识库原文…未做推断" |
| `query_cube` 不再写 `:=` | ✅ 24 次调用、`:=` **0** 次，全部 `user_name:eq:丛培强`；且它已成为主力数据通道（上次只有 1 次因 `:=:` 失败的调用） |

**最有说服力的一点**：出报告的这份走的是 **SQL 通道**（有 `## 3. 执行 SQL` + `## 4. 执行 SQL（物理）`
两节）——正是"原先天然没有口径节"的那条通道，现在口径节照出。

> 旁注（非缺陷，待观察）：本次 run 里 `build_report` 被调 **2 次**，产出两份报告
> （`丛培强2026年9月工时分析_…11-01-28` 与 `丛培强本月工时分析_…11-01-11`）。同一问题出两份报告值得看一眼
> 是重派发还是主/子 agent 各建了一次。

**发版动作**：后端整包发版 + `sync_prompts --all`（prompt 住 Langfuse）+ 运行期 skills 整目录
同步；**后两项之后必须重启**。`report_builder.py` 与 `langfuse_span.py` 只需发版+重启。
`REPORT_CALIBER_APPENDIX_MAX_CHARS` 是否开启待定（开了会显著加长报告，属"宁可啰嗦也不漏"的保险）。

**未做**：B（放行 `/workspace/<语义库目录>/knowledge/**` 读取 + 注入不可推导的真目录名，属 P1
隔离例外，需点名）；`recall_queries` 返回 `{"matches": []}` 的根因（在 wren 侧，未查）。

#### 逐字证据核验（2026-09-26，**代码未发版；提示词 v4 / skill 未推；生产 E2E 未做**）

**起因**（用户原话）：「2. 业务口径 部分，有写下面，但是没具体口径来源的语义库知识库原文」。用户对
业务口径的用途定义是「主要用于**使用用户判断生成的答案是否准确**」——而上一轮 E2E 那份报告恰好
把这条用途废掉了：

- 表里 `内容` 是语义层**转述**、`出处` 写的是 `workhour_analysis（Cube）` / `v_workhour` /
  `语义库字段字典`，**一个知识库文件都没有**；
- 表格下面那句「口径取自语义库知识库原文（`rules/*.md` / `metrics/指标定义.md`），出处逐条标注；
  未做推断」是 `report_builder.py` **无条件写死**的 ⇒ **报告在撒谎**。

**契约文本已被证伪**：`NL2SQL_SYSTEM_PROMPT.md` §十一第 3 条与 `wren-execution/SKILL.md`
结果呈现第 4 条**本来就写着**「内容取原文」「出处必须是知识库里真实存在的文件名」，模型没守；
而且**原文明明在它手里**——该 trace 调过 `wrenai_witops_get_instructions`（20921 字符，含
`业务域口径`/`报工与工时`/`R1`/`R3`）⇒ 有料不引、改成自己的转述。所以本轮必须加**确定性闸**，
契约文本只做补强（补「逐字」措辞 + 这次的真实反例）。

**用户已拍板的四点**：① 不做系统侧「原文整段附在报告末尾」的兜底（`REPORT_CALIBER_APPENDIX_MAX_CHARS`
保持 `0`）；② **逼模型在表里逐条引原文**；③ 强度＝**打回重试 ≤2 次 + 仍不合规则显式标注**
（并删掉假脚注）；④ 严格度＝**出处 + 内容逐字比对**。另两点：语料基线＝**磁盘知识库全文**；
整节缺失＝**不追、只标注**（纯明细列举允许不写块）。

**修法**：

| 文件 | 动作 |
|---|---|
| [caliber_evidence.py](../../src/agent/utils/caliber_evidence.py) | **新增**：块解析 + 归一化 + 逐字/分片比对 + 出处白名单 + **磁盘语料加载**（`resolve_wren_ctx_by_db` → `knowledge/{glossary,metrics,rules,sql,caveats}/*.md`，60s TTL 缓存，失败一律 fail-open 空语料） |
| [caliber_gate.py](../../src/agent/middlewares/caliber_gate.py) | **新增**：`after_model` 终态判定，不合规 ⇒ 返回 `{"jump_to": "model", "messages": [纠正]}` 打回重写；重试计数**由 state 推导**（跨重启不吃掉额度） |
| [nl2sql_agent.py](../../src/agent/graphs/nl2sql_agent.py) | 挂载在 `ProgressBoundaryMiddleware` **之前**（index 更小 ⇒ after_model 跑得更晚，先推进完 todos 再打回） |
| [report_builder.py](../../src/agent/tools/report_builder.py) | **删掉无条件假脚注**；改按核验结论分**四态**脚注 + 未通过条目**单列第二张表**；解析/核验全部改为从 `caliber_evidence` 再导出（两侧共用一份实现） |

**为什么语料取自磁盘而不是子 agent state**：`wren/context.py:713-724` 的 `load_knowledge_rules()`
是 `"\n\n".join(parts)`，**拼接里没有文件名** ⇒ 拿工具返回来当语料**根本判不出「这段属于哪个文件」**，
也就验不出「引了 A 文件的话、署名 B 文件」。知识本体就在磁盘（0 成本、权威、可离线造 fixture），
且知识只有 MCP 一条投递通道（`knowledge/**` 的 read/grep 被权限拒）⇒ **逐字命中磁盘原文即反证
本次确实取过这份料**。

**重答机制（读依赖源码 + 实测，不是推断）**：`after_model` **每轮必跑**（`factory.py:1738`
无条件 `add_edge("model", …)`），但**只注入消息不会重答**（`:1867` 见 `tool_calls` 为空即退出循环），
重答的正规通道是 `jump_to`（`:1849-1855`；库注释原文写明是给 after_model 钩子用的，HITL 同款），
且它是 `EphemeralValue`（`middleware/types.py:351`）⇒ 每个 superstep 自动清空，**不会死循环**。

`jump_to` **只在「本节点出边是条件边」时才被读**，位置效应实测四组合（`create_agent` + tools，
复现见 `scripts/verify_caliber_evidence.py` t10）：

| 位置 | `@hook_config(can_jump_to=["model"])` | 结果 |
|---|---|---|
| `[1]`（前面还有 after_model 中间件）＝**我们的实际位置** | 有 | ✅ 模型被调 2 次（打回生效） |
| `[1]` | 无 | ❌ 只调 1 次，末条变成纠正提示（**尾部污染**） |
| `[0]` = `loop_exit_node` | 有 | ✅ 2 次 |
| `[0]` = `loop_exit_node` | 无 | ✅ 2 次（那条边是 `_make_model_to_tools_edge`，**无条件读** `jump_to`） |

⇒ deepagents 把 `TodoListMiddleware()` 恒定放在 middleware 栈首位（`deepagents/graph.py:773-775`），
我们**永远不是** `[0]` ⇒ 那条「不写装饰器也能跳」的便宜路与我们无关，**装饰器是硬要求**。
（另有一个静默坑：hook 第二参数**必须叫 `runtime`**，langchain 按参数名注入，改叫 `r` 直接抛
`missing 1 required positional argument`。）

**刻意不做（写进代码注释防回退）**：**不**摘取子 agent 块里的 `> …核验…` 行进报告——中间件只打回、
不写结论行，能摘到的只可能是**模型自己写的**自评（「本表已逐字核验通过」），把它印进报告就是
「报告撒谎」换个人称。核验结论**只由代码产生**。

**验收**：新增 `scripts/verify_caliber_evidence.py` **84/84**（11 组：归一化含「下划线**不**抹平」
负对照 / 逐字正例 / **负对照＝同义改写·只改数字·只换语序·张冠李戴（引 metrics 的话署 rules 出处）** /
短分片防放过（先证两个短词**确实**在原文里，再证仍被拒）/ 出处白名单（`v_workhour`/Cube 名/
字段字典/`report/x.md` 全拒；中文连接词粘连 ⇒ fail-closed）/ 子目录集合与 `api.wren_semantic`
对齐 / 语料加载与缓存 / **语料缺失 fail-open（闸门绝不打回）** / 闸门判定与跨轮预算 / 真图打回
（含去掉装饰器的负对照）/ 接线不变量）。`scripts/verify_report_caliber.py` 改为 **70/70**
（新增四态脚注与「模型自评不进报告」断言；解析断言一条未动）。回归
`verify_slimmer_exempt` **42/42**（证明**没动**免截断名单——那份名单有 42 条负例，
`get_context`/`recall_queries` 必须仍**不**命中）、`verify_db_exec_authz` 32/32。

**交付面（三处独立，缺一处就是静默回退）**：代码走发版；prompt 走 `sync_prompts --all`
（`nl2sql_system_prompt` v3→v4、`main_system_prompt` v2→v3）**且推完必须重启**；
`src/agent/shared/skills/nl2sql/wren-execution/SKILL.md` 要同步到运行期
`<AGENT_DATA_ROOT>/shared/skills`。⚠️ 服务器上 `sync_prompts` 还是旧版 `if/elif`，
`--all --skills` 会**静默丢 `--all`** ⇒ 必须**分两条命令**跑（见上文交付段）。

**未做**：生产 E2E（同一问题再问一次，看第 2 节 `内容` 列是否 `rules/*.md` 逐字片段、`出处` 是否
真实文件名，并核对 trace 里子 agent 是否发生打回）；`build_report` 同一 run 被调 2 次的旁支观察。

---

#### 生产 E2E 结果：整节省略（2026-09-26，trace `f222a8a5…`）—— 已修，**本轮未发版**

**交付状态核实（只读 `docker exec md5sum`，已获授权）**：`caliber_gate.py` / `caliber_evidence.py` /
`report_builder.py` / `NL2SQL_SYSTEM_PROMPT.md` / `MAIN_AGENT_PROMPT.md` 五个文件**与本地全等**
⇒ 代码与提示词**都已发版**（trace 里 `report_builder` 专属字符串 `逐条做出处`×17、`内容逐字核验`×34
出现在模型输入里，旧假脚注字符串 ×0，独立佐证）。⚠️ **但运行期技能漏了**：
`/app/data/shared/skills/nl2sql/wren-execution/SKILL.md` md5 `5164d5ed…` ≠ 本地 `5a68a32f…`，
新措辞命中 **0** ⇒ 第三交付面**没同步**（本轮子 agent 没读这个 SKILL.md，不是本次病因，但下次就是）。

**症状**：报告里**完全没有业务口径节**。全 trace（271 obs、1480 万字符）中 `## 业务口径` 出现 **0 次**；
子 agent 终稿写的是 `## 查询结果` + 一行粗体 `**统计口径说明**`；两次 `build_report` 都没传
`business_caliber`，自动抽取无块可抽。上一版的病（有节但出处造假）**没复发**——旧假脚注字符串 ×0。

**根因（两条，都是我上一轮改的措辞造成的）**：

1. 我加的「取不到原文依据的条目**直接不写**」+「纯明细列举…**整节不写**」给出了一个**合法逃生口**，
   模型取了它 —— 而这一轮 `get_instructions` 照样返回 **20921 字符**含 R1/R6/R7/R8（R8 恰好定义了
   它这次用的「本月总工时 = SUM(work_hour) + date BETWEEN + deleted=0」）⇒ **有料不引，改成省略**。
   主 agent 的推理原文是直接证据：「the subagent didn't provide a 「业务口径」block… I don't know
   the rules file names… I shouldn't fabricate… leave business_caliber empty」。
2. **我们要求了它做不到知道的事**：契约举例是 `rules/报工与工时.md R3`（带目录前缀），而
   `get_instructions` 返回里文件名**只裸着出现**（`报工与工时.md`）。核验器其实**认裸名**
   （`_resolve_source` 按 `f.name` 匹配，verify t5 里裸名就是正例）—— 但没人告诉模型，于是「不确定
   算不算真实文件名」⇒ 干脆不写。

⇒ 教训：**「不追、只标注」在缺块这一态等价于「报告永远可能没有口径」**，而报告用户正是拿这一节
判断答案准不准。省略是最省事的过关方式，纯契约拦不住。

**修法（三处，均已实施 + 断言钉住）**：

| 位置 | 改动 |
|---|---|
| `caliber_gate.py` | 新增 `_no_block()` 执法分支：**本轮取过知识料**却终稿无块 → 打回 1 次（`_MISSING_TAG` 独立标签，预算 `CALIBER_MISSING_MAX_RETRIES`=1，**与「不合规」的 2 次分开计**，否则「缺块→补块→出处不合格」这条正常升级路径会被误伤）；三条出口＝已声明「本次未依据知识库口径」/ 本轮没取过料 / 已打回满 1 次 |
| 三份契约 | ① 出处**照抄裸文件名**、不必也禁止自己拼 `rules/` 前缀；② 「整节不写」收紧为**仅纯明细列举**，并列出必写触发条件（统计窗口/有效记录/人员或口径池/表选择/比率分母）；③ 「删条目**不删节**」；④ 「确实没用到」的标准写法 `本次未依据知识库口径` |
| `verify_*` | `verify_caliber_evidence` 新增 t9b（17 条，含三条出口 + 两类预算互不占用 + 跨问题不串）→ **100/100**；`verify_report_caliber` t10 补 8 条契约断言 → **78/78** |

**刻意不做**：把 `本次未依据知识库口径` 渲染进报告。它是模型自述，且是免除打回的捷径、
最容易被滥用 —— 印出去等于「报告替模型背书」，正是上一轮删掉的那个坑换个形态。它只用来给循环终点。

**交付（2026-09-26 完成，三面均已只读核实）**：

| 面 | 动作 | 核实判据 |
|---|---|---|
| 代码 | `release-backend.ps1` 整包发版（回滚 tag `rollback-20260926-1352`） | `caliber_gate` / `caliber_evidence` / `report_builder` / `nl2sql_agent` md5 与本地全等；`backend_ok:200` + 预检 37 个 MCP 工具 |
| 提示词 | `sync_prompts --all`（本机 `.env.prod` 指向 64:3010） | 启动日志 `main_system_prompt v4 生效` / `nl2sql_system_prompt v5 生效` |
| 运行期技能 | 发版第 5 步整目录替换（`[shared-sync] SKILL.md=14 个, memory=3 个`） | `wren-execution/SKILL.md` md5 与 src 相等；`check_skills_drift.py` **exit 0**（37/37） |

⚠️ 两个坑（详见 memory）：

1. `sync_prompts --skills` 扫的是**本机 `AGENT_DATA_ROOT/shared/skills`**（运行期副本），**不是**
   `src/agent/shared/skills`。先用它推了一遍，把**旧内容**（`wren-execution` 缺本轮新措辞）推成 v2；
   改用 `--name skill/<g>/<d> --file src/agent/shared/skills/<g>/<d>/SKILL.md` 逐个点名重推为 **v3**
   （已打 production）。Langfuse 上因此留下 v2(旧)→v3(对) 两版，v3 生效。
2. `release-backend.ps1` **第 5 步自写下来起就不可解析**：PS 双引号串里 `\"` 不转义引号、会提前闭合字符串，
   后半行 `$(find … | wc -l)` 被当 PowerShell 代码执行 → `wc` 报错 → 顶层 `Stop` 中止**在重启之前**
   （线上零影响，但第 5 步从未成功跑过；09-25 那份运行期技能是**手工**同步的）。已改为 `` `" `` 并过
   `Parser::ParseFile` 解析校验。

**待办**：生产 E2E 复测（同一问题再问一次，核对报告口径节的出处是真实裸文件名 + `内容` 是逐字原文，
且 trace 里能看到一轮「缺块」打回）。
