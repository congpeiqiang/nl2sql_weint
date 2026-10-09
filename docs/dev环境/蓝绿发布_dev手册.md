# 蓝绿发布 · dev 手册（192.168.25.34 · 后端链路 · 真零中断）

> 适用对象：`192.168.25.34`（dev）上的 nl2sql 后端。
> 一键入口：`docs/dev环境/dev-release-backend-bluegreen.ps1`。本手册是它的**说明书 + 验收清单 + 出事了怎么办**。
> 命令速查在 [发布脚本.md](发布脚本.md)；生产那套在 [../prod环境/蓝绿发布_阶段2发版手册.md](../prod环境/蓝绿发布_阶段2发版手册.md)。
> 静态闸：`scripts/verify_dev_blue_green.py`（156/156）。

---

## 0. 一句话说清它解决什么

dev 原来的后端发版 = 把 `/mydata/nl2sql/src` 换装 + `docker compose restart` ⇒ **一次冷启中断**
（而且它直接改正在接流的 `src`，新代码没有任何验证窗口）。

蓝绿把它换成：**把新代码暂存到 `src-next` → 起一个不接流量的新色 B（挂 `src-next`）→ 探测 →
提升 → 改 nginx 一行 + reload → 排空并停旧色 A → **目录轮换**（`src-next` → `src`）→ 用 base compose
把 A 起回来 → 再改一行 + reload 切回 A → 排空并停 B**。用户侧目标是 **0 个 5xx**
（注意：是 5xx，不只是 502 —— 见下面「为什么切流必须在排空之前」）。

### 与 prod 的三处结构性差异（读脚本前必须先知道）

| | prod（64） | **dev（34）** |
|---|---|---|
| 新代码在哪 | 打进**镜像** | bind mount 的**目录**（`/mydata/nl2sql/src`） |
| 第二色怎么拿到新代码 | 用**新镜像**起 B（overlay 里 sed 成实测 image ID） | 把 **`src-next`** 挂进 B（`COLOR_B_SRC`） |
| 发版末段 | 用**新镜像**把 A 起回来 | **目录轮换**（`src` → `src-prev-<ts>`，`src-next` → `src`） |
| 回退靠什么 | 旧**镜像 ID** | 旧**目录** `src-prev-<ts>` |
| compose | v1（下划线容器名 `nl2sql-app_*_1`） | **v2 插件**（连字符 `nl2sql-app-*-1`、`docker compose`） |
| env 文件 | `.env.prod` | **`.env.dev`**（`DEPLOY_ENV=dev` 在它里面） |
| **不变量** | | dev **全程不换镜像、不构建**（34 无外网） |

三条铁律在 dev 照旧：① 每个 `up` 都带 `--no-deps`；② overlay **只新增服务名**；
③ 改配置用 `stop && rm -f && up -d`。对旧色的**停止**用裸 `docker stop/rm <容器名>`。

### 为什么切流必须在排空之前（这是整套设计里最关键的一条）

老路是「**先排空再停**」。排空期间新提交会被 `DrainGateMiddleware` 明确拒掉（**503**）。
503 也是 5xx，用户照样看得见 ⇒ 旧顺序只是**把 502 换成了 503**。
蓝绿**先切流、再排空**：切流之后旧色再也收不到用户请求，它的排空（以及排空期的 503）
**没有任何人看得见** ⇒ 这才是「零 5xx」。

### 本轮边界（诚实声明）

* 零中断**只覆盖后端/API 链路**。**前端发版仍是 `dev-release-frontend.ps1`**，`location /` 有几秒 502。
* 稳态**内存不变**：平时只跑 1 个色，第二色只在发版窗口里存在，收尾时被删掉。
* **不迁移** dev 存量的 inmem pckl 会话（那批数据已归档，需求方明确不搬）。

---

## 1. 前置条件

### 1.1 一次性地基（现已就位；下面是核验方法）

dev 原来**不具备**蓝绿的前提：后端是**每容器一份内存 pckl**（`DATABASE_URI: ":memory:"`），
两色各持一份状态 ⇒ 切色 = 用户会话「消失」；而且 nginx 还是**老形态**（启动期解析的
`upstream backend {}`），**根本没有可切的那一行**。一次停机窗口里做了这七件事：

| # | 做了什么 | 核验命令（只读） |
|---|---|---|
| 1 | 从 64 搬来**带 postgres runtime** 的后端镜像（34 无外网，只能 `docker save \| docker load`） | `docker run --rm nl2sql-app_langgraph-api:latest sh -c 'ls /storage/langgraph_runtime_postgres >/dev/null && ls /storage/migrations \| wc -l && ls -l /usr/bin/core-api-grpc'` → 三样齐全（迁移 **64** 条）。⚠️ **别去 site-packages 找 `langgraph_runtime_postgres`**：postgres 版在 `/storage`（镜像里 `PYTHONPATH=/storage`），site-packages 里只有 `langgraph_runtime_inmem`；旧镜像则这三样**全无** |
| 2 | `CREATE DATABASE nl2sql_platform`（`POSTGRES_DB` 只建第一个库） | `docker exec nl2sql-app-postgres-1 psql -U nl2sql -lqt \| cut -d\| -f1 \| grep nl2sql_platform` |
| 3 | 服务器 `docker-compose.yml` 加 `redis` 服务 | `docker ps \| grep nl2sql-app-redis-1` |
| 4 | 平台存储切 postgres 版型（`LANGGRAPH_RUNTIME_EDITION=postgres` + `DATABASE_URI`） | `docker exec nl2sql-app-langgraph-api-1 env \| grep LANGGRAPH_RUNTIME_EDITION` → `postgres` |
| 5 | 整份换仓库 `docker/nginx.conf`（**同时**拿到请求期解析、`worker_shutdown_timeout`、P0-3 删除、P2-2 的 rid） | `docker exec nl2sql-app-nginx-1 grep -n 'backend_up\|report' /etc/nginx/nginx.conf` |
| 6 | 端口收回环（**P0-2**）+ nginx 去掉 `agent_data:ro`（**P0-3**） | `docker ps --format '{{.Ports}}' \| grep 2026` → 应为 `127.0.0.1:2026->2026` |
| 7 | 归档旧 pckl（**不删**）：`mv data/api_meta data/api_meta.inmem-<ts>` | `ls -d /mydata/nl2sql/data/api_meta*` |

仓库侧对应物：`docker/compose.dev.yml`（34 上 compose 的**参照副本**）、`docker/nginx.conf`（**两边同一份**）、
`docker/compose.colorB.dev.yml`（第二色 overlay）。**线上真身仍是 `/mydata/nl2sql/nl2sql-app/docker-compose.yml`，手工维护。**

> **P0-2**：原写 `"2026:2026"` 发布在 `0.0.0.0` ⇒ LAN 上不带 token、不带 XFF 直连即被认成 internal
> 身份，四个 `@auth.on` 全放行，整套用户隔离归零。
> **P0-3**：nginx 原挂 `agent_data:ro` 喂 `location /report/`（`alias … + autoindex on`）⇒
> 未登录即可列出并下载全站报告。

### 1.2 每次发版前的只读核对

| # | 检查 | 命令 / 判据 |
|---|---|---|
| 1 | 平台存储是 postgres 版型 | 见 1.1 #4（**不满足脚本第 0 步直接拒绝发版**） |
| 2 | redis 在跑 | `docker ps \| grep nl2sql-app-redis-1`（**不满足直接拒绝**） |
| 3 | 线上 nginx.conf 与**仓库模板**一致 | 脚本第 0 步自动比对（遮掉 `$backend_up` 那一行）。不一致会**中止**并打印 diff，确认无误才加 `-Force` |
| 4 | 磁盘有余量 | `df -h /mydata` 剩余 ≥ 10G |
| 5 | postgres 在跑 | `docker ps \| grep nl2sql-app-postgres-1` |
| 6 | **上一次的残留第二色不存在** | `docker ps -a \| grep langgraph-api-b` 应为空 |
| 7 | **当前流量在 A** | `docker exec nl2sql-app-nginx-1 grep backend_up /etc/nginx/nginx.conf` → `"langgraph-api:2026"` |
| 8 | 迁移必须是 **add-only** | 见 §6 |

脚本第 0 步（`preflight`）会把这些打出来。**第 1、2 条不满足直接拒绝**；第 6、7 条不满足也拒绝
（=` 上一次发版/回退没走完），要用 `-Rollback` 或 `rm_b` 先回到稳态。

---

## 2. 发版时序（脚本逐步做什么）

> 「步骤号」与脚本里的 `== N/12 ==` 对齐。服务器侧每一步都抽在 `/tmp/nl2sql_bg_steps_dev.sh`
> （脚本上传，可单独调用做手工排查）。

**稳态契约：`A`（`langgraph-api`）是常驻色，`B`（`langgraph-api-b`）只在发版窗口里存在。**
发版以「A 跑旧代码」开始、以「A 跑新代码」结束。

| 步 | 动作 | 关键理由 |
|---|---|---|
| 0 | 预检（只读）：版型、库名、redis、**当前流量在哪一色**、**有没有残留 B**、nginx 防呆比对、磁盘 | 有一样不对就**在动手之前**停（§2.1） |
| 1 | 本地打包 `src/`（排除清单与老路 `dev-release-backend.ps1` 逐字相同） | 推的是**工作树**（含未提交改动）—— 这正是 dev 的用途 |
| 2 | 分片上传（0.5MB/片，每片重试 8 次） | 链路单连接寿命短，整包必断 |
| 3 | 服务器拼接 + `dev_swap_src.sh stage` → **暂存到 `src-next`** | 用 `stage` 而**不是** `swap`：`swap` 会清空 `src`，而那正是**正在接流的 A** 挂着的目录 |
| 4 | 上传 overlay + `bg_steps_dev.sh`；`docker cp` 新的 `ops_drain.py` 进 A | `docker/` 不进发版包；镜像里那份 `ops_drain.py` 是**旧逻辑**（判据不该靠镜像版本） |
| 5 | `COLOR_B_JOBS=0` 先 `stop && rm -f` 再 `up -d --no-deps langgraph-api-b`（挂 `src-next`）；探测 B 并**与 A 对照预检结论** | 先起**空闲色**：新代码就绪但绝不先领真实用户的 run（`N_JOBS_PER_WORKER` 只在 import 期读一次）。先清再起＝铁律③；预检判据见 §2.2 |
| 6 | 提升 B：`stop && rm -f && COLOR_B_JOBS=10 up -d --no-deps` | 提升只能靠**重建**。此刻 B 还没接流，重建免费 |
| 7 | **切流到 B**：起 1s `/ok` 轮询 → 渲染 nginx.conf（只改 `set $backend_up` 一行）→ 备份 → `cat >` 就地写 → `nginx -t` → `nginx -s reload` | reload 是**优雅**的：旧 worker 会服完已建立的连接才退，在跑的 SSE 不被掐。`nginx -t` 失败自动就地回滚 |
| 8 | **排空 A** → 连续 3 次 idle 确认 → 同一 ssh 会话内 `docker stop -t 240` + `rm -f` | A 已收不到用户请求，它的排空对任何人不可见 |
| 9 | **目录轮换**：`src` → `src-prev-<stamp>`，`src-next` → `src` | 见 §2.3。**必须在停 A 之后、起 A 之前** |
| 10 | **用 base compose 把 A 起回来**（`dc up -d --no-deps --no-build`，**不带 overlay**）→ 探测 `/ok` → **核对 `DATABASE_URI` 与正在接流的 B 逐字相同** | B 在接流，A 的冷启对用户不可见。A 起来就领 run 是**安全**的：它跑的代码与正在接流的 B **逐字节相同**。`DATABASE_URI` 那道硬闸在验「服务器 compose 是否已是 postgres 版型」，不一致就**当场把 A 停掉**再喊停 |
| 11 | **切流回 A**（同一套就地覆盖 + `nginx -t` + reload） | 稳态契约恢复：流量回到常驻色 |
| 12 | **先 `docker cp` 新 `ops_drain.py` 进 B**，再 **排空并停临时色 B**；停轮询、断言 0 个非 200、两色日志查 `database is locked`、写状态文件 | `docker cp` 是 **per-container**，第 4 步只给 A 就位过（§2.4）。B 与 A 是同一份代码，就算它在切流那一瞬又领到 run 也无害 |

### 2.1 为什么结尾要绕回 A（而不是让 B 常驻）

留 B 常驻能少一次切换，但代价是：**下一次发版就没有空闲的第二色可用了**。
而且运维习惯、日志 grep、老路 `dev-release-backend.ps1` 全都认 `nl2sql-app-langgraph-api-1` 这个名字。

所以第 0 步有两条**拒绝发版**的前置检查（不满足时脚本直接停，不往前走）：

1. **当前流量必须在 A**（从线上 `docker/nginx.conf` 的 `set $backend_up` 读出）。
   不满足 ⇒ 上一次发版/回退没走完 ⇒ 先 `-Rollback` 或 `rm_b`，别硬来。
2. **不能有残留的 B 容器**。

> 这两条把「脚本跑第二遍」这种输入变成**响亮的拒绝**，而不是让它走到一半才炸。
> 硬来（`-Force`）能做，但要知道自己在破坏哪条假设。

### 2.2 第 5 步的预检判据：**B 不比 A 差**（不是「B 有没有预检失败」）

dev 的 `.env.dev` 设了 `MCP_ALLOW_DEGRADED=0`，但语义库连不上等情况仍会让容器出现
「预检失败 + 降级启动」——**A 自己就可能长期处于降级态**。按绝对判据写（见到「预检失败」就停）
会把 **A 的常态**当成 B 的失败 ⇒ **这台机器永远发不了版**。

正确判据是**子集比较**（`comm -13`）：只有「A 好 B 坏」或「B 新增了 A 没有的缺失 server」才拒流。
真正致命的那一档已被 `wait_ok` 盖住 —— 关键 server 缺失时 `start_server.py` 会 `sys.exit(1)`，
`/ok` 永远不会 200。

### 2.3 第 9 步的目录轮换：为什么必须在停 A 之后、起 A 之前

`/mydata/nl2sql/src` 是 A 容器挂着的目录。轮换只 `mv` **目录本身**（改个名，不动里面的文件）：

* **A 还在跑的时候轮换会怎样**：容器已经打开了那个 inode，改名字对它**毫无影响**（内容一模一样）——
  所以技术上"看着没事"，但这会让 `src` 这个名字指向的代码和 A 实际跑的不一致，下一个踩到的人会判断错。
* **等 A 停了再轮换**：`src` 空出来了，`src-next`（B 正在挂的那个 inode）改名成 `src` 完全无缝，
  第 10 步用 base compose 起回来的 A 才会挂到**新代码**。**顺序反了就是「A 起回来仍然跑旧代码」**。
* ⚠️ 目录是**改名**不是删。`src` 里全是 root 属主的 `__pycache__`（容器以 root 写），
  `weint` 根本删不掉 —— 删必半残（2026-09-21 实测：278 个 `.py` 全没、只剩 123 个 root 属主的 `__pycache__`）。
  所以 `rotate` 只用 `mv`，且**做任何改动之前先改名备份**。

### 2.4 排空探针必须**跑得起来**，否则拒绝停容器（fail-closed）

`cutover` 的第一步是 `docker exec "$ct" test -s /tmp/nl2sql_ops_drain.py || die …`。
没有这道闸，「探针跑不起来」会被当成「已排空」然后照停 —— 那正是这套设计要根除的 fail-open。

`docker cp` 是 **per-container** 的：第 4 步只给 A 就位过，所以第 12 步必须**先给 B 也 cp 一份**。
（prod 在 2026-10-08 的 E2E 上实撞过：整段排空被静默跳过，只因当时零 run 才没出事。）

### 2.5 为什么第 8 步要「同一 ssh 会话内」做完

`N_JOBS_PER_WORKER` 只在进程 import 期读一次（`langgraph_api/config/__init__.py:380`），
没有运行期开关 ⇒ 切流之后旧色**仍持有** 10 个 run 槽，仍可能从**共享**队列里领到刚提交给新色的 run。
缓解手段就是「连续 3 次 idle 判定 → 立刻 `docker stop`」**全在服务器侧一次 ssh 往返里**做完，
把窗口压到毫秒。见 §5.1。

---

## 3. 验收断言（发完必须逐条核）

```bash
# ① 脚本结尾那几行
#    POLL_NON200=0        ← 1s 轮询 /ok 全程；原始采样 /tmp/nl2sql_bg_dev_poll.log
#    排空确认：A=yes       ← A=stale 见 §5.1
#    回退用：上一版代码目录 = src-prev-<stamp>

# ② 稳态：只剩一个色、流量在 A
ssh weint@192.168.25.34 'docker ps -a | grep langgraph-api; \
  docker exec nl2sql-app-nginx-1 grep -n backend_up /etc/nginx/nginx.conf'

# ③ 平台库计数（发版前后应只增不减）
ssh weint@192.168.25.34 "docker exec nl2sql-app-postgres-1 psql -U nl2sql -d nl2sql_platform \
  -tAc \"select 'thread=' || count(*) from thread;\""

# ④ 两色日志里的 'database is locked' 各 0 条
ssh weint@192.168.25.34 'for f in /tmp/nl2sql_dev_last_*.log; do echo "$f: $(grep -c "database is locked" $f)"; done'

# ⑤ src 现状：src=新代码、src-prev-<stamp>=上一版（`src-next` 可能残留，见下）
ssh weint@192.168.25.34 'ls -1d /mydata/nl2sql/src /mydata/nl2sql/src-* 2>/dev/null'

# ⑥ 对外链路（经 nginx；401 也算「通」⇒ 说明请求到得了应用）
ssh weint@192.168.25.34 'for p in / /ok /api/db-configs; do printf "%-18s -> " "$p"; \
  curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8080$p"; done'
```

**「切流真的生效」的证据**：第 8 步停掉 A **不产生 502**。如果切流没生效，停 A 那一刻
轮询会立刻出现成片的 502 而不是 0。

### 残余竞态怎么量

看第 8 步的 `DRAIN_READY`：

* `yes` —— 排空确认干净（连续 3 次 idle）。
* `stale` —— 切流之后旧色**又领到了 run**。**记下这个数**：它是「残余竞态」被观测到的次数。
  查 A 的日志（`/tmp/nl2sql_dev_last_langgraph-api.log`）定位是哪一条。
* `n/a` —— 该色本来就不存在（回退的第二遍常见）。

---

## 4. 回退

### 4.1 切流之前出问题（B 起不来 / 探测不过 / `nginx -t` 失败）

**线上零影响**（B 未接流、nginx 未切）。清掉 B 重跑：

```bash
ssh weint@192.168.25.34 'bash /tmp/nl2sql_bg_steps_dev.sh rm_b'
```

`nginx_apply` 若 `nginx -t` 失败会**就地回滚到备份且不 reload**，线上仍是旧配置。

### 4.2 要回退（发完发现新版有问题）

```powershell
.\dev-release-backend-bluegreen.ps1 -Rollback
.\dev-release-backend-bluegreen.ps1 -Rollback -PrevSrc src-prev-20261008-153012
```

**回退本身也是蓝绿**：把 B 挂到 `src-prev-<stamp>` 起临时色（`COLOR_B_SRC`）→ 提升 → **切流到 B**
→ 停 A → 目录轮换回来（那一版成为 `src`，被换下去的留档成 `src-rolledback-<stamp>`）→
用 base compose 起 A → 切回 A → 停 B。

**为什么不能「直接把 A 重启成旧版」**：回退最常见的场景是「发完发现新版有问题」，
而那时**窗口已经关了**（B 已删、流量在 A）。直接 stop/rm/up 就是拿真用户等一次冷启 ——
与「零中断」自相矛盾。所以回退必须**先有第二色接流**，再动 A。这条在静态闸里是硬断言。

* 上一版目录来自上次发版的 `SRC_PREV`（或 `-PrevSrc`）。**不给就拒绝猜**。
* 回退的终点 = 发版的终点（A 常驻、B 已删）⇒ 回退完可以**立刻再次发版**；
  `-Rollback` 连跑两次 = 退回去再退回来。
* 回退后 A 跑的是上一版代码，而平台库可能已被新版迁移改过（迁移是 add-only，一般仍能跑）。

---

## 5. 残余风险

### 5.1 不可消除的那一条：切流后旧色仍可能领到一个 run

`N_JOBS_PER_WORKER` 是 import 期常量（`langgraph_api/config/__init__.py:380`），**没有运行期开关**。
切流之后旧色进程仍然活着、仍然持有 10 个槽，仍可能从共享队列里领到**刚提交给新色**的 run。
唯一的缓解是「连续 idle 判定 → 立刻 stop」全在服务器侧一次 ssh 往返里（§2.5），把窗口压到毫秒。

* ⚠️ **只有第 8 步这一次有害**（旧色 A 跑的是**旧代码**）。第 12 步停 B 时两色是**同一份代码**。
* 观测点：第 8 步的 `DRAIN_READY=stale`（§3）。

### 5.2 第二色是「原样复制」的隐式契约

overlay 里那批环境变量必须与 base **逐字相同**（库、runtime、迁移目录、redis）——
`scripts/verify_dev_blue_green.py` ① 把这条做成了机械断言。改 base 而忘了改 overlay，
表现是「第二色起来看着正常，但连的是另一个库」这种**静默**分裂。

### 5.3 其它已知但不在本轮范围的

* **前端不是蓝绿**（`location /` 有几秒 502）。
* **没有单例闸门**（prod 也没做）：理论上两个色可以同时跑同一件事的定时任务。
* **`src-prev-*` 会越攒越多**（每次发版一个目录；回退还会再加一个 `src-rolledback-*`）。
  没有自动清理 —— 要手工 `rm -rf`，但**至少留最近一个**（回退要用）。

### 5.4 发版结束后 `src-next` 可能「复活」成一个空目录（无害）

E2E 两次（正向 + 回退）都观测到：收尾时 `/mydata/nl2sql/src-next` **又出现**，是一个
**空的、`root:root` 属主的目录**（轮换明明已经把它改名成 `src` 了）。

* 机理（**未单独验证**，但与证据一致）：第 9 步轮换之后还会对 B 做 `docker exec`
  （第 12 步的 `cp_drain` / `cutover`），而 B 的挂载源路径此时已被改名走 —— Docker 为新
  exec 进程准备挂载时，会把**缺失的宿主路径重建为空目录**（daemon 是 root，所以属主是 root）。
  mtime 正好落在那一刻，两次都复现。
* **无副作用**：脚本第 0 步会明确提示「上一次发版留下了 src-next」，第 3 步的 `stage`
  会把它**整个清掉重放**（只影响这一个目录）。
* 推论（**同样未单独验证**）：切流之后**不要靠 `docker exec` 进 B 读 `/app/src` 里的代码** ——
  新起的 exec 进程看到的可能是那个空目录。判定排空只看 `/tmp/nl2sql_ops_drain.py`，不受影响。

---

## 6. expand-contract 约束（加了迁移就要看这一节）

第二色**开机即跑迁移**，而它跑在 `DATABASE_URI` 指定的**共享**平台库上 ⇒
**新代码带的迁移会在「起空闲色 B」那一刻就打到共享库，而旧色 A 还在跑**。

所以蓝绿只允许 **add-only** 迁移：

* langgraph 官方迁移与本仓自建表迁移都只能加表/加列（本仓惯例 `IF NOT EXISTS` + 逐列 try 已满足）；
* **删列 / 改类型 / 加 NOT NULL 无默认** 这类破坏性变更**走不了蓝绿**，
  用老路 `dev-release-backend.ps1` 的停机窗口。

---

## 7. 收尾清理

```bash
# 中途失败留下残留 B（脚本第 0 步会因此拒绝发版）
ssh weint@192.168.25.34 'bash /tmp/nl2sql_bg_steps_dev.sh rm_b'

# 服务器侧的中间文件（发版脚本自己会清大部分）
ssh weint@192.168.25.34 'ls -la /mydata/nl2sql/.tmp/ | head -20'
```

**别删** `src-prev-<stamp>`（回退依据）、`data/api_meta.inmem-<ts>`（旧 pckl 归档）。

---

## 8. 与老脚本、与仓库文件的关系

| 文件 | 作用 |
|---|---|
| `docker/compose.dev.yml` | 34 上 `docker-compose.yml` 的**仓库参照副本**（线上真身手工维护，改这里不等于改线上） |
| `docker/compose.colorB.dev.yml` | 第二色 overlay（**只在发版窗口里用**，不常驻） |
| `docker/nginx.conf` | **prod 与 dev 共用同一份**（这就是「一致」最强的形态） |
| `docs/dev环境/dev-swap-src.sh` | 服务器侧换装助手：`stage` / `rotate`（老路也用它） |
| `docs/dev环境/dev-apply-nginx-conf.sh` | 把仓库 `docker/nginx.conf` 整份同步到 34（一行壳） |
| `docs/dev环境/dev-release-backend.ps1` | **老路**（停机窗口；破坏性迁移的唯一通路） |
| `scripts/verify_dev_blue_green.py` | 本手册全部断言的离线闸 |

### 附录：让 dev 追平生产版本（搬镜像）

日常发版**不需要**这个（dev 换的是目录）。只有在「改了依赖」或「想验证生产那版镜像」时才用。

```bash
#!/bin/bash
# 在 34 上跑：从 64 搬镜像。用法：bash pull_from_64.sh [镜像名...]
set -u
BASE=/mydata/nl2sql
printf '#!/bin/sh\necho "<64密码>"\n' > "$BASE/.tmp/.ask64.sh"; chmod 700 "$BASE/.tmp/.ask64.sh"
export SSH_ASKPASS="$BASE/.tmp/.ask64.sh" SSH_ASKPASS_REQUIRE=force
SSHOPTS="-o StrictHostKeyChecking=no -o NumberOfPasswordPrompts=1 -o PreferredAuthentications=password -o ConnectTimeout=10 -o ServerAliveInterval=20"
IMGS=("$@")
[ ${#IMGS[@]} -eq 0 ] && IMGS=("nl2sql-app_langgraph-api:latest" "nl2sql-app_frontend:latest")
fail=0
for img in "${IMGS[@]}"; do
  echo "[$(date +%H:%M:%S)] START $img"
  if ssh $SSHOPTS weint@192.168.25.64 "docker save '$img'" | docker load; then
    echo "[$(date +%H:%M:%S)] OK    $img"
  else echo "[$(date +%H:%M:%S)] FAIL  $img"; fail=1; fi
done
rm -f "$BASE/.tmp/.ask64.sh"; echo "ALL_DONE fail=$fail"
```

搬完**必须重建对应容器**（`:latest` 被顶掉了，容器不会自动换）：

```bash
cd /mydata/nl2sql/nl2sql-app
docker tag nl2sql-app_langgraph-api:latest nl2sql-app_langgraph-api:rollback-<ts>   # 存旧，只新增 tag
docker compose stop langgraph-api nginx && docker compose rm -f langgraph-api nginx
docker compose up -d --no-deps langgraph-api nginx
```
