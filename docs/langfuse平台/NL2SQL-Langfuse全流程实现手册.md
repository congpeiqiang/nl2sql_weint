# NL2SQL · Langfuse 全流程实现手册

> **用途**：NL2SQL 部署到生产后，基于 Langfuse 的「监控 → 用户反馈 → 评估 → 数据集 → 迭代 → A/B 测试 → 发版」完整闭环的操作手册。
> **适用读者**：后端开发 / 运维 / 负责 NL2SQL 质量与发版的工程师。
> **适用范围**：生产 = 服务器 Linux Docker 容器（weint 192.168.25.64）；开发机 = Windows 本机（`.venv` 开发环境）。
> **最后更新**：2026-08-30。

---

## 0. 闭环全景

```
                         ┌──────────────────────────────────────────────────────────┐
   用户提问 ──→ NL2SQL 主 agent ──→ 五维评分写 trace（M3 评估）
                     │                      │
                     │  LLM Judge(采样)      │ 差评/低分/异常
                     ▼                      ▼
           Langfuse trace + scores    collect_badcase ──→ Dataset:badcase（数据集）
                     │                      │
                     │ user-feedback score   │ 注册 pending
               前端 👍/👎/评论 ─┐              ▼
                     │        └──→ feedback_gate   badcase_status 状态机（迭代）
                     │              （在线真实反馈门禁）    │
                     │                    │            review / mark fixed|invalid
                     │                    │            ▼
                     │             放量判断/回滚 ←─ run_experiment --from-badcase
                     │                    │            （离线回归 + A/B 门禁）
                     │                    └──→ 决定可否发版
                     ▼
                sync_prompts ──→ Langfuse prompt 版本管理（发版：staging→canary→production）
```

**三道闸门**（①②是发版门禁，①是运行时安全闸门）：

| 闸门 | 时机 | 工具 | 判据 | 作用 |
|---|---|---|---|---|
| ① SQL 只读硬拦截 | 每条查询**执行前** | `sql_approval`（`classify_sql`） | 写/DDL（INSERT/UPDATE/DELETE/DROP/ALTER…）→ 直接拒绝执行，不弹人工审批 | **运行时安全**：杜绝写操作 |
| ② 离线 A/B 回归门禁 | **发版前预检** | `run_experiment` | 核心维均值 candidate < reference − 阈值 → exit 1 | **离线质量**：固定查询集可重复验证 |
| ③ 在线真实反馈门禁 | **放量后** / canary 对比 | `feedback_gate` | 好评率 candidate < reference − 阈值 → exit 1 | **在线口碑**：真实用户 👍/👎 |

②与③互补，数据源不同：

| | ② 离线门禁（M5） | ③ 在线门禁（M6） |
|---|---|---|
| 数据源 | 固定查询集 / Dataset:badcase 回灌 | Langfuse `user-feedback` score（真实用户 👍/👎） |
| 问的问题 | 「模型现在做得好不好」（可重复、离线） | 「线上用户觉得好不好」（权威、在线） |
| 落点 | 发版前预检 | 放量后持续监控 / canary 对比 |

**一次查询 = 一条 chat-turn trace**（root `chat_agent` + 子 agent/工具 span 嵌套），
连问时续跑链按 task_id 归属回原 trace。层级细节见
[Langfuse标准Trace层级方案.md](Langfuse标准Trace层级方案.md)。

### 0.1 实例走读：一个差评 → 一次发版

> 完整示例：把 ① SQL 硬拦截 → ② 离线回归 → ③ 在线反馈三道闸门和整个闭环串起来。
> 命令前缀见 §1.1：生产用 `DC ...`（容器封装），cron 里是完整 `docker exec ...`。
> 时间轴上的「自动」= 系统/cron 无需人工，「人工」= 需要人操作（本示例的操作者 = 工程师）。

**一句话主线**：用户一个「查询结果不对」的差评 → 次日被自动采集进 BadCase 数据集 →
人工复审确认真问题 → 改 prompt 打 staging → 离线回归门禁②通过 → canary 灰度 →
在线真实反馈门禁③通过 → 全量发版。**一个差评，最终变成一次受控发版。**

**时间线总览**（8-29 提问 → 9-2 全量，建议先扫这一表，再逐步骤细看）：

| # | 时间 | 触发方 | 动作 | 对应章节 |
|---|---|---|---|---|
| 1 | 8-29 15:12 | 自动 | 门禁① SQL 只读硬拦截 → 放行执行 | §0 闸门① |
| 2 | 8-29 15:13 | 自动 | 五维评分写 trace（judge 判 `sql_biz_correct=0.25`） | §4 |
| 3 | 8-29 15:14 | 用户 | 点 👎 + 评论 → Langfuse 写 `user-feedback=0` | §3（用户反馈） |
| 4 | 8-30 02:13 | cron 自动 | `collect_badcase` → `Dataset:badcase` + 状态注册 `pending` | §5 |
| 5 | 8-30 09:00 | 人工 | `badcase_status review` 复审 → 确认真问题（按 `r`） | §6.5 |
| 6 | 8-30 09:20 | 人工 | 改 prompt → `sync_prompts --label staging` | §8.2 |
| 7 | 8-30 09:30 | 人工 | 门禁② `run_experiment` 离线回归 → **exit 0** → 标 `fixed` | §7.2 |
| 8 | 8-30 14:00 | 人工 | canary 灰度（1 容器 `RATIO=0.1`） | §7.3 / §8.3 |
| 9 | 9-2 09:00 | cron 自动 | 门禁③ `feedback_gate` 在线反馈 → **exit 0** | §7.3 |
| 10 | 9-2 09:10 | 人工 | `production` 标签移到 staging 版本 → 全量 | §8.3 |
| 11 | 9-3 02:13 | cron 自动 | 次日采集确认同类问题不再复现 → 闭环 | §6.8 |

---

**第 1 步 · 门禁① SQL 只读硬拦截（8-29 15:12，自动，§0 闸门①）**

用户在 imdb 库提问：**「查 1990 年后上映、按评分倒序的前 10 部电影」**。

nl2sql 子 agent 生成候选 SQL → 在 `wrenai_imdb_run_sql` 工具**执行前**，
`SqlReadOnlyMiddleware` 调 `classify_sql` 做确定性分类：

```sql
SELECT title, rating, year FROM movie ORDER BY rating DESC LIMIT 10
```

```python
("read", "")   # classify_sql 实际返回：首词 SELECT + 含 LIMIT → 只读查询，放行
```

**✅ 放行分支** → SQL 正常执行，返回 10 行数据。

> ⚠️ **对比·拦截分支**：若子 agent 跑偏生成 `DELETE FROM movie WHERE rating < 5` →
> 首词 DELETE 命中 `_WRITE_LEADING` → `classify_sql` 返回 `("write","DELETE")` →
> **直接拒绝执行**，工具返回 error ToolMessage（`sql_approval.py` 原文）：
>
> ```
> Error: 系统为只读查询系统，仅允许执行 SELECT（含 WITH...SELECT）只读查询。
> 检测到 DELETE 操作，已禁止执行。请改用 SELECT 查询，或向用户说明无法执行数据修改。
> ```
>
> 不执行、不弹人工审批、不落库；该查询 `sql_valid_score=0`（§4.1），
> 之后会被 §5 条件 2 自动采为 BadCase。

**第 2 步 · 五维评分（8-29 15:13，自动，§4）**

SQL 执行成功后，评估旁路把分写进本条 chat-turn trace `9e0ad442...`：

| score 名 | 值 | 方式 | 说明 |
|---|---|---|---|
| `schema_match_score` | 1.0 | 同步 code 规则 | 表/字段都解析到了 |
| `sql_valid_score` | 1.0 | 同步确定性 | 只读 + 有 LIMIT |
| `sql_exec_success` | 1.0 | 同步 code 规则 | 执行成功 |
| `sql_biz_correct_score` | **0.25** | LLM-judge（采样命中） | ⚠️ 发现真问题，见下 |
| `report_table_score` / `analysis_report_score` | — | 采样未命中 | 本查询无报表，不评 |

后台 judge 线程拿到 `(问题, SQL, 结果)` 评审后写 `sql_biz_correct_score=0.25`——
**发现真问题**：SQL 没带 `WHERE year>=1990`，结果混进了 80 年代电影，答非所问。

> 采样率 `NL2SQL_EVAL_JUDGE_SAMPLE=0.3`：确定性维（前三行）100% 出分，judge 维按 30%
> 概率出分。所以「有分」不代表一定被 judge 看过；用户差评（第 3 步）才是 100% 权威信号。

此时 Langfuse **Traces** 页这条 trace 长这样（一次查询 = 一条 chat-turn trace，层级见
[Langfuse标准Trace层级方案.md](Langfuse标准Trace层级方案.md)）：

```
9e0ad442 · chat-turn                    ← trace 名（= 一次查询）
├── skill: nl2sql           [span]
│   └── chat_agent          [span]
│       └── wrenai_imdb_run_sql [span] ← 工具调用（带 SQL 输入 / 结果输出）
scores 挂在 trace 级（不占树节点）：
    schema_match_score=1.0 / sql_valid_score=1.0 / sql_exec_success=1.0 / sql_biz_correct_score=0.25
```

**第 3 步 · 用户差评（8-29 15:14，用户操作）**

用户看到结果里混进 80 年代电影 → 前端点 **👎** + 评论：

> 「评分最高的应该是 1990 年后，这里混进了 80 年代的」

前端 `PUT /api/threads/{tid}/messages/{mid}/feedback` → 后端写 Langfuse
`user-feedback=0`（comment = 评语）。Langfuse **Scores** 页这时：

| trace | score 名 | value | comment |
|---|---|---|---|
| 9e0ad442 | `user-feedback` | 0 | 评分最高的应该是 1990 年后… |

至此「问题证据」齐了：**judge 低分 0.25 + 用户差评 0**，两条都满足 §5 采集条件 2/3。
第 1~3 步全自动、无需人工干预；接下来交给采集流水线。

**第 4 步 · 自动采集（8-30 02:13，cron 自动，§5）**

生产宿主机 cron（`13 2 * * *`）跑 `daily_collect_badcase.sh` 第一步（完整命令，日志
`/home/weint/apps/nl2sql/logs/nl2sql_collect_badcase.log`）：

```bash
docker exec nl2sql-app_langgraph-api_1 bash -c "cd /app && \
  PYTHONPATH=/app/src /app/.venv/bin/python -m agent.eval.collect_badcase --days 1"
```

扫最近 1 天 trace：`sql_biz_correct_score=0.25 < 0.6` 且 `user-feedback=0` 命中 →
写入 `Dataset:badcase` 一个 item：

```json
{
  "datasetName": "badcase",
  "input": "查 1990 年后上映、按评分倒序的前 10 部电影",
  "expectedOutput": "",
  "metadata": {
    "source_trace_id": "9e0ad442-c830-11e7-a1b2-c3d4e5f60718",
    "reasons": ["sql_biz_correct_score=0.25", "user_feedback=0"],
    "db_name": "imdb",
    "question": "查 1990 年后上映、按评分倒序的前 10 部电影",
    "sql": "SELECT title, rating, year FROM movie ORDER BY rating DESC LIMIT 10"
  }
}
```

同时 `register_batch` 把这条注册进本地状态机（§6.4，已有状态不覆盖）：

```json
{
  "9e0ad442c83011e7a1b2c3d4e5f60718": {
    "status": "pending",
    "question": "查 1990 年后上映、按评分倒序的前 10 部电影",
    "db_name": "imdb",
    "reasons": ["sql_biz_correct_score=0.25", "user_feedback=0"],
    "collected_at": "2026-08-30",
    "note": ""
  }
}
```

日志：`采集到 1 条新 badcase，状态跟踪：新注册 1 条 pending`。
**关键**：状态 = `pending` → 该条自动进入回归集（pending + reviewed = 开放），
从这一刻起它就是门禁②③的评估对象之一。

**第 5 步 · 人工复审（8-30 09:00，人工，§6.5）**

工程师晨检跑交互式复审，命令**自动打开**对应 Langfuse trace 页面：

```bash
DC -m agent.eval.badcase_status review
# 自动打开 https://192.168.25.64:3010/project/<PROJECT_ID>/traces/9e0ad442c83011e7a1b2c3d4e5f60718
```

逐条展示：

```
[1/1] pending
  问题   : 查 1990 年后上映、按评分倒序的前 10 部电影
  命中原因: sql_biz_correct_score=0.25, user_feedback=0
  数据库 : imdb
  > 按 f/i/r/s/q 操作：
```

对照 trace 里子 agent 生成的 SQL 与结果，确认**确实缺 `WHERE year>=1990`**——真问题、
根因已定位。此时按 **`r`** 标 `reviewed`（确认真问题、待修），保持它在回归集里。

> 复审 SOP（§6.5）：打开 trace → 看「问题 + 命中原因」→ 回看 SQL 与结果是否真错 →
> 真错且已定位根因 → 修复后门禁复验；确属测试/误报 → `i`（invalid，离开回归集）。

**第 6 步 · 修复根因并打 staging（8-30 09:20，人工，§8.2）**

工程师在 Langfuse UI 编辑 `nl2sql_system_prompt`：要求「年份 + TOP N」类提问必须生成
对应 WHERE/LIMIT。然后同步到 staging 标签：

```bash
DC -m agent.prompt.sync_prompts --all --label staging --commit "修复缺年份过滤"
```

Langfuse **Prompts** 页出现 `nl2sql_system_prompt` 的 **staging** 版本
（`latest` 同时指向它；`production` 仍指着旧版）。此后走 staging 分流的进程吃新 prompt。

**第 7 步 · 门禁② 离线 A/B 回归预检（8-30 09:30，人工，§7.2）**

发版前必须证明「新 prompt 在回归集上不更差」。跑离线回归：

```bash
DC -m agent.eval.run_experiment --from-badcase --labels production staging
```

- `--from-badcase`：从回归集（pending + reviewed）加载同一批查询，旧/新 prompt 各吃一遍；
- 两个 label 各起一个 **worker 子进程**（`LANGFUSE_PROMPT_LABEL` 进程级注入，§7.2）；
- 跑完按 trace 写分 → 汇总 manifest `{workspace}/eval/experiment_runs/run_{stamp}.json`。

结果摘要（核心维 = `sql_biz_correct_score` / `sql_valid_score` / `sql_exec_success`）：

```
label        sql_biz_correct   sql_valid   sql_exec_success
production   0.61              0.95        1.00
staging      0.82              0.96        1.00
```

门禁判定：`0.82 ≥ 0.61 − 0.05` → **exit 0 通过** → 可以放量。
顺手把这条 badcase 标为 `fixed`（修复已被回归证明，§6.6 前缀匹配）：

```bash
DC -m agent.eval.badcase_status mark 9e0ad442 fixed --note "SQL 生成要求带年份过滤"
```

该条从此**离开回归集**，门禁② 不再被它重复考核。

> 若候选 < 基准 − 阈值 → **exit 1 不通过**：回查 staging 是否改坏，修好重跑本步。

**第 8 步 · canary 灰度（8-30 14:00，人工，§7.3 / §8.3）**

把 1 台容器设成 canary（其余保持 production），该容器 `.env.prod` 追加后重启：

```bash
LANGFUSE_CANARY_LABEL=staging
LANGFUSE_CANARY_RATIO=0.1      # 该实例 import 时约 10% 概率走 staging
```

重启后启动日志：`[langfuse] prompt label=staging（A/B 分流）`。
该实例的查询按 10% 概率走 staging 新 prompt，trace metadata.prompt 记录
`prompt_label=staging` ——这就是后面门禁③分组的依据。**production 组不受影响**，
继续全量走旧版。

**第 9 步 · 门禁③ 在线真实反馈（9-2 09:00，cron 自动，§7.3）**

canary 生效约 3 天后，每日 cron 的 `feedback_gate` 聚合真实用户反馈（完整命令）：

```bash
docker exec nl2sql-app_langgraph-api_1 bash -c "cd /app && \
  PYTHONPATH=/app/src /app/.venv/bin/python -m agent.eval.feedback_gate \
  --days 7 --ref production --cand staging"
```

按 trace metadata.prompt 分组、按 trace 去重取最新、剔除 v4 撤销哨兵分（value<0）：

| 组 | rated（有效反馈数） | 好评率 |
|---|---|---|
| production（旧） | 30 | 0.60 |
| staging（新） | 12 | **0.85** |

门禁判定：`0.85 ≥ 0.60 − 0.05` 且每组 ≥5 条 → **exit 0 通过** →
新 prompt 在真实用户里确实更好 → 可以全量。

> 若 staging 只有 0.52（0.52 < 0.60 − 0.05）→ **exit 1** → 走下方「门禁③失败」分支。

**第 10 步 · 全量发版（9-2 09:10，人工，§8.3 ⑧）**

Langfuse UI 把 `production` 标签打到 `nl2sql_system_prompt` 的 **staging 最新版本**，
`production` 自动离开旧版本 → 全局（含非 canary 容器）从此走新 prompt。
旧版本仍在版本历史里，随时可回退（§8.4 手段③）。

**分支 · 门禁③失败（回滚，§8.4）**

若在线门禁 exit 1，按影响面从小到大选：

| 手段 | 操作 | 影响面 |
|---|---|---|
| ① 退 canary | 清 `LANGFUSE_CANARY_LABEL/RATIO` 重启容器 | 全部实例回 production |
| ② 显式 label | 置 `LANGFUSE_PROMPT_LABEL=production` 重启 | 该进程强制旧版 |
| ③ prompt 回退 | UI 把 `production` 标签移回旧版本 | 全量走旧版 prompt |

同时该 staging badcase **保留在回归集**（不回滚状态），等下次修复再重新过门禁②③。

**第 11 步 · 收尾闭环（9-3 02:13，cron 自动，§6.8）**

次日 cron 再跑 `collect_badcase`：

- 同类「缺年份过滤」的查询现在都带 `WHERE year>=1990` → judge 不再打低分 →
  **不再命中采集条件** → 问题关闭；
- 旧 badcase `9e0ad442...` 已 `fixed`（第 7 步标的）→ 离开回归集 →
  门禁②③ 不再被旧债拖累；
- 若同类问题再次出现，会以**新 trace_id** 重新采集为 `pending` → 自动回到回归集，
  再走一遍第 5~10 步。

**闭环成立**：差评 → 采集 → 复审 → 修复 → 离线回归 → 灰度 → 在线反馈 → 全量发版 →
确认不再复现。这就是「一个差评 → 一次发版」的完整链路。

---

## 1. 前置：环境与开关

### 1.1 运行前缀（所有管理命令都以此开头）

**生产（容器）**——容器 venv 是 `uv sync --no-install-project` 装的，**没有** `_nl2sql_src.pth`，
必须 `PYTHONPATH=/app/src` + venv python；`bash -c`（非登录）避免丢 venv PATH：

```bash
DC() { docker exec nl2sql-app_langgraph-api_1 bash -c "cd /app && PYTHONPATH=/app/src /app/.venv/bin/python $*"; }
DC -m agent.eval.collect_badcase --days 1
```

**开发机（Windows PowerShell）**——本地 venv 有 `_nl2sql_src.pth`，无需 PYTHONPATH：

```powershell
$py = 'D:\code_work_space\llm\nl2sql\.venv\Scripts\python.exe'
& $py -m agent.eval.collect_badcase --days 1
```

> 两份脚本 `scripts/daily_collect_badcase.sh`（容器）/ `.ps1`（开发机）已封装三步，日常不用手敲。

### 1.2 环境变量（`.env.prod` 键名）

| 变量 | 作用 | 缺省 |
|---|---|---|
| `LANGFUSE_BASE_URL` / `PUBLIC_KEY` / `SECRET_KEY` / `PROJECT_ID` | 生产 Langfuse 连接 | — |
| `LANGFUSE_ENABLE` | **总开关**：false 关一切运行时埋点/打分/prompt（`get_client` 仍可用，管理工具照跑） | `true` |
| `LANGFUSE_PROMPT_ENABLED` | prompt 走 Langfuse 还是强制本地（第二层回滚） | `1` |
| `LANGFUSE_PROMPT_LABEL` | 显式指定 prompt label（run_experiment / 演练直接用，最高优先） | — |
| `LANGFUSE_CANARY_LABEL` / `CANARY_RATIO` | 按比例掷骰走 canary（进程级，import 时掷一次） | — / `0` |
| `LANGFUSE_RELEASE` | 客户端 release → Langfuse Release 页分组 | — |
| `NL2SQL_EVAL_JUDGE_SAMPLE` | LLM-judge 采样率 0~1 | `0.3` |

**env 加载语义**（`src/agent/settings/env_loader.py`）：先 `.env`（dev 基线），再叠加 `.env.prod`
中**仅 `LANGFUSE_*`** 且「未预先存在」的键。容器靠 `env_file: .env.prod` 注入（恒优先）；
宿主机/本机手动跑会自动连**生产** Langfuse 项目，`AGENT_DATA_ROOT` 不被覆盖（保持本机路径，stamp 落开发工作区）。

### 1.3 每日自动调度

| 环境 | 机制 | 触发时间 | 日志 |
|---|---|---|---|
| 生产（容器） | 宿主机 cron → `daily_collect_badcase.sh` → docker exec 三步 | `13 2 * * *` | `/home/weint/apps/nl2sql/logs/nl2sql_collect_badcase.log` |
| 开发机 | Windows 任务计划 → `daily_collect_badcase.ps1`（仅兜底） | 02:13 | 项目根 `server_collect_badcase.log` |

三步固定为：① `collect_badcase --days 1`（采集）→ ② `feedback_gate --days 7`（在线门禁）→
③ `badcase_status summary`（待复审提醒）。注册/启停见
[../weint环境/NL2SQL-部署与更新手册.md](../weint环境/NL2SQL-部署与更新手册.md) §2.6。

---

## 2. 监控（可观测）

### 2.1 怎么看

| Langfuse 页面 | 看什么 |
|---|---|
| **Sessions** | 按会话分组；一次用户对话的完整链路、每轮花费 |
| **Traces** | 单 trace 明细：子 agent 执行、工具调用、token 消耗、延迟 |
| **LLM Chain** | 每一跳 LLM 的输入/输出/耗时 |
| **Scores** | 五维评分 + user-feedback 的分布 |
| **Release** | 按 `LANGFUSE_RELEASE` 分组的发版对比 |

### 2.2 关键事实（避免误判）

- **会话页行名 = observation 名**（含嵌套 span），**详情标题 = trace 名**（= chat-turn）。
  同一 `trace_id` 在列表/详情显示不同名是**常态，不是数据问题**；判据用
  `GET /api/public/traces/{id}`。
- **主 agent trace 名 = chat-turn**，不会被子 agent/skill 名污染（M-T6 系列）。
- 连问场景：任务 N 的完成通知即使落在任务 N+1 的 run 活跃期，续跑链仍归属 N 的 trace（M-T5 任务级路由）。
- 实验 trace 带 `tags=["nl2sql","experiment"]` + session `exp:{label}:{run}`，用 tag 过滤即与生产隔离。
- 生效证据看启动日志：`[langfuse] prompt label=...（A/B 分流）`、`[langfuse] release=...`。

### 2.3 监控要点

- 每轮看：`sql_valid` / `sql_exec_success`（确定性，100% 出分）；`schema_match`（code 规则）。
- LLM-judge 维（`sql_biz_correct` / `report_table` / `analysis_report`）按采样率出分，样本少时**别当 100% 指标**。
- 差评线索优先看 `user-feedback=0` 的 comment（存于 score comment）。

---

## 3. 用户反馈（真实用户反馈闭环）

用户反馈是**最权威的质量信号**（优先级高于五维评分——见 §4.2「确定性 vs judge —— 怎么读分」：
judge 低分 + 用户差评同现最可疑）。本章讲清「前端 👍/👎/评论 → 本地库 + Langfuse score 双写
→ 下游消费（badcase 采集 / 发版门禁）」的完整实现，是 §5.1 条件 3 与 §7.3 门禁的数据来源。

### 3.1 链路总览

```
用户 点👍/👎 + 写评论
  ──► PUT /api/threads/{tid}/messages/{mid}/feedback（毫秒级返回）
        ├─► ① 本地 FeedbackStore（SQLite，唯一真相）
        │      └─ 后台补齐 question/sql 快照（读线程 state 1~9s，不阻塞保存）
        └─► ② 后台线程写 Langfuse user-feedback score（旁路，失败仅告警）
              ├─ 好评=1 / 差评=0，comment=用户真实评语
              ├─ 归属：按 message_id 精确找产生它的 chat-turn trace
              └─ 撤销 → 哨兵分 value=-1（v4 无 score 删除 API，软删除）
下游消费：
  ├─ collect_badcase 条件 3（§5.1）：user-feedback=0 → Dataset:badcase
  └─ feedback_gate（§7.3）：按 prompt_label 分组比好评率 → 发版门禁
```

### 3.2 数据模型与双写约定

**本地 store**（`src/agent/feedback/store.py`，SQLite `{shared}/feedback/message_feedback.db`，
WAL + 进程锁，本地反馈的唯一真相）：

| 字段 | 说明 |
|---|---|
| `(thread_id, message_id)` | 联合主键，一条反馈 = 对某条回答的点赞/差评 |
| `rating` | `positive` / `negative` |
| `note` | 用户评论（≤2KB UTF-8） |
| `version` | 乐观并发（CAS），并发编辑冲突返回 409 |
| `question` / `sql` | 首次写入后**后台补齐**的问题/SQL 快照（评测归因：问题→SQL→反馈） |
| `context` | 首次写入快照（如 `db_name`），更新不覆盖 |

**Langfuse `user-feedback` score 约定**（写入：`src/api/message_feedback.py`）：

| 事件 | value | comment | 语义 |
|---|---|---|---|
| 点赞 | `1` | 用户 note | 好评 |
| 差评 | `0` | 用户 note | 差评（触发 badcase 采集） |
| 撤销 | `-1`（`USER_FEEDBACK_REVOKED` 哨兵） | 已撤销 | v4 events 表无 score 删除 API，软删除 |

> **「最新一条即当前状态」**：同一 trace 可能多次打分（点赞后改评/补评论/撤销各写一条）。
> 读取端（`feedback_gate._dedupe_scores` / `collect_badcase`）按 trace 取 **timestamp 最新**一条，
> `value<0` 视为无反馈——撤销后残留的旧点赞分不会被误计好评率。

### 3.3 落库与打分行为

- **接口**：`PUT/DELETE /api/threads/{tid}/messages/{mid}/feedback`（body：`rating`、`note`、
  `if_version`(CAS)、`context`）；`GET /api/threads/{tid}/feedback` 回显；`GET /api/feedback/export` 全量导出。
- **写入毫秒级**：点赞/评论立即落库返回；question/sql 快照与 Langfuse 打分都是**后台旁路**，
  不阻塞前端交互（读线程 state 实测 1~9s，同步等会卡住批注）。
- **打分触发**：仅「首次写入 / 评分变化 / note 变化」才写 Langfuse score——**note 变化也打分**，
  否则用户点赞后再补的评论永远不反映到 Scores 界面。
- **trace 精确归属**：`find_message_trace_id` 按 message_id 升序扫 chat_agent 根 output，定位
  「创建这条消息」的那次 chat-turn trace（**不能取会话最新**，否则同会话多条反馈全落最后一个问题）。
- **撤销**：DELETE → 本地删除 + Langfuse 写 `-1` 哨兵分（软删除语义；`_find_trace_with_retry`
  3 次退避，防代理间歇超时吞掉打分/撤销）。

### 3.4 读取与验证

```bash
# 会话内反馈（前端回显图标态）
curl http://localhost:2026/api/threads/{tid}/feedback

# 全量导出（评测回流；含 👍 正例）
curl http://localhost:2026/api/feedback/export
```

Langfuse UI 验证：**Scores** 页过滤 `user-feedback`，看 value 分布与 comment；打开 trace 应落在
产生该回答的那条 chat-turn 上（不是会话最新一条）。

### 3.5 下游消费

| 消费者 | 位置 | 用法 |
|---|---|---|
| badcase 采集 | §5.1 条件 3 | `user-feedback=0` → 入 `Dataset:badcase`，差评自动进复审集 |
| 在线门禁 | §7.3 | 按 `prompt_label` 分组好评率对比，`value<0` 已剔除 |
| 监控 | §2.3 | 差评线索优先看 `user-feedback=0` 的 comment |

### 3.6 标注闭环（待标注队列 + 标注页操作流程 · 实施现状，2026-09-02）

**入队**（`put_feedback` → `store.enqueue_annotation`，幂等 `INSERT OR IGNORE`）：**所有评分（👍 和 👎）都进待标注队列**（`feedback_annotation` 表，status=`queued`）；`feedback_type`（query/chat）自动判定。入队时 question/sql 为空串，由后台快照补齐**同步回填标注记录**（列表/详情即取即得，不会显示「无问题摘要」）。

**状态机**：

```
queued 待判断 ──① 有效 + 点赞 ──► good（直接入 Good Set，跳过改 SQL）
        │──② 有效 ──────────► annotating ──「执行验证」──► validated
        │                                              ├──③ 正确 ──► good
        │                                              └──④ 错误 ──► badcase
        └──⑤ 无效 / 误报 / 闲聊 ──► rejected
```

**标注页操作**（`/feedback/annotate`，入口=聊天主页右上「待标注」；顶栏可选填**标注人**，存 localStorage 追溯）：

1. **待判断 queued** — 判断这条反馈是否有效：
   - 「**有效查询，直接入 Good Set**」（仅点赞反馈）→ 一步入集，跳过改 SQL
   - 「**有效反馈，进入标注**」→ 进标注中
   - 「**无效 / 误报，驳回**」→ rejected
2. **标注中 / 已验证** — 修正并验证 SQL：
   - SQL 编辑器（预填模型 SQL，后端只读护栏，写/DDL 直接拒绝）→ 填库名 → 「**执行验证**」→ 结果表格预览 → 状态自动变 **validated**
   - 「**确认入 Good Set**」→ 写 `Dataset:goodcase`（正向样本），终态 good
   - BadCase 区：选**错误类型**（下拉带说明）+ 填**金标 SQL**（正确写法，须能执行通过）+ 备注 → 「**确定入 BadCase**」→ 写 `Dataset:badcase` + 回归集，终态 badcase
3. **终态**（BadCase / Good / 已驳回）— 只读展示（错误类型、金标 SQL、金标结果、确认时间、标注人）

**规则**：点赞反馈**不能入 BadCase**（按钮置灰，只能 Good Set 或驳回）——差评才走 badcase；入 BadCase 必须「错误类型 + 金标 SQL（后端先执行校验）」。

**产物去向**：BadCase → Langfuse `Dataset:badcase` + `badcase_status.json`（status=reviewed，进回归集）→ `run_experiment --from-badcase` 回归；Good → `Dataset:goodcase` → 正向样本。标注页 BadCase/GoodCase 两个 Tab **直读 Langfuse Dataset**（来源 `auto-collect` 自动采集 / `user-annotation` 人工确认），与 Langfuse UI 一致。

### 3.7 关键坑

- **v4 无 score 删除 API**：撤销只能写 `-1` 哨兵分（软删除），读取端统一「按 trace 取最新」+ `value<0` 过滤。
- **反馈归属必须精确到 message**：不能按会话最新 trace，必须 `find_message_trace_id` 升序扫。
- **读 state 慢**：快照必须后台补齐，绝不阻塞反馈保存接口。
- **comment 只用用户真实评语**：不伪造「有帮助/有问题」文案，评分语义由 value 表达。

---

## 4. 评估（五维评分）

### 4.1 维度定义

| 维度 score 名 | 方式 | 规则 | 采样 |
|---|---|---|---|
| `schema_match_score` | code evaluator（同步） | Schema 发现成功=1.0 / 失败=0.3；语义层由 judge 补充 | 否 |
| `sql_valid_score` | 确定性（复用 `sql_approval.classify_sql`） | read=1.0 / full_dump=0.4 / write·DDL·不可识别=0.0 | 否 |
| `sql_biz_correct_score` | LLM-as-a-Judge（后台线程） | SQL 结果是否满足问题意图 | `NL2SQL_EVAL_JUDGE_SAMPLE` |
| `report_table_score` | LLM-as-a-Judge（后台线程） | 表格是否正确回答 | 同上 |
| `analysis_report_score` | LLM-as-a-Judge（后台线程） | 报告质量 + **幻觉检测** | 同上 |
| `sql_exec_success` | code evaluator（同步） | 执行成功 1 / 失败 0（用 `looks_like_exec_error` 识别错误文本返回值） | 否 |

实现：`src/agent/eval/evaluators.py`；写入：`create_score(name, value, trace_id, comment)`（trace 级，旁路不阻塞主流程）。

### 4.2 各维评估对象（防什么）

六个 score 按职责逐个说明判据与典型失败场景：

**① schema_match_score —— Schema 选对了吗**

防「连表/字段都没解析到」。schema 发现成功 = 1.0；抛异常 = 0.3。
只证明「**能解析**」，不证明「**选对了表**」——语义层由 ③ 的 judge 补充。

**② sql_valid_score —— SQL 安不安全**

直接复用门禁① 的 `classify_sql`，与运行时**同一套判据**：

| `classify_sql` 判定 | score | 场景 |
|---|---|---|
| `read`（SELECT，含 WHERE/LIMIT/聚合） | 1.0 | 正常查询 |
| `full_dump`（无 WHERE/LIMIT/聚合） | 0.4 | 疑似全表拉取，高成本有风险 |
| `write` / DDL / 不可识别 | 0.0 | DELETE/DROP/UPDATE…——根本过不了门禁①，执行都执行不了 |

**③ sql_biz_correct_score —— 答非所问了吗（结果错了却还能跑）**

**最核心**、也最容易被 judge 判错的一维。LLM-judge 拿 `(用户问题, SQL, 结果)` 评审：
结果数量/明细/汇总是否符合**问题意图**；结果为空、报错、答非所问 → 低分。
示例：用户问「1990 年后上映的电影」，SQL 没带 `WHERE year>=1990` 结果混进 80 年代 → 0.25。
**已知坑**：用户说「artists 表」物理表是 `artist`（单复数归一化）属正常，
rubric 已声明不能仅因表名/字段名与问题措辞不一致判 0 分（见 §4.3）。

**④ report_table_score —— 表格对吗**

有报表/表格产出时才评，采样。judge 看表格是否准确回答用户问题（数据算错 / 列错 / 对不上）。

**⑤ analysis_report_score —— 报告真实吗（有没有幻觉）**

分析报告质量 + **幻觉检测**：报告结论是模型编的、数据里不存在 → 低分。
与 ④ 由同一个 judge 一起出分（`judge_report` 一次返回两个分）。

**⑥ sql_exec_success —— 执行成功没有**

确定性 code 规则：成功 = 1 / 失败 = 0。SQL 报错时错误文本常作为工具**正常返回值**
（非异常），须先用 `looks_like_exec_error` 识别再判 0（§4.1 表格备注）。

**确定性 vs judge —— 怎么读分**

| 维度 | 成本 | 出分 | 读法 |
|---|---|---|---|
| ①②⑥（规则） | 零 | **每条必出** | 看「基础健康」 |
| ③④⑤（LLM-judge） | LLM 调用（采样 0.3） | 概率出 | 样本少时**别当 100% 指标** |
| `user-feedback`（用户 👍/👎） | 用户真实操作 | 有反馈才有 | **比 judge 更权威**：judge 低分 + 差评同现最可疑 |

### 4.3 评分口径要点

- **确定性维零成本、每查询必现**；LLM-judge 维采样降本，后台 daemon 线程 fire-and-forget 写分。
- 评分跨主/子 trace 统一按 `sessionId` 汇总（v4 events_only 下走 `langfuse_v4_reads`）。
- **已知坑（judge 假阴性）**：用户说「artists 表」物理表是 `artist`（单复数归一化），
  judge rubric 已声明「不能仅因表名/字段名与问题措辞不一致判错」。若人工看 judge 分明显偏低，
  先按此排查，再怀疑真实回归。

---

## 5. 数据集（BadCase 采集）

### 5.1 采集条件（命中任一即入 `Dataset:badcase`）

1. 系统异常：trace status = ERROR
2. 五维分 < 阈值（默认 0.6）：`schema_match` / `sql_valid` / `sql_biz_correct` / `report_table` / `analysis_report`
3. 用户差评：`user-feedback = 0`
4. SQL 执行失败：`sql_exec_success = 0`

### 5.2 命令

```bash
# 生产（容器）
DC -m agent.eval.collect_badcase --days 1
# 开发机
& $py -m agent.eval.collect_badcase --days 30 --force   # 重扫历史并重放

# 参数
#   --days 1          扫描最近 N 天
#   --threshold 0.6   低分阈值
#   --limit 200       trace 扫描上限
#   --force           忽略已采集 stamp，全部重处理
```

- 数据源是 **Langfuse API**（v2/observations 列用户 AGENT root + scores_v3 汇总），不是日志。
- 每条 item 带 `source_trace_id` 链回 trace、`metadata.reasons`（命中原因）、`metadata.db_name`（回灌定位同库）。
- **stamp 去重**：`{workspace}/eval/badcase_collected.json`，同 trace 不重复采；`--force` 才重放。
- 新采集自动注册 `badcase_status` 为 `pending`。

### 5.3 验证数据集

```bash
curl -u "pk-lf-xxx:sk-lf-xxx" \
  "http://192.168.25.64:3010/api/public/dataset-items?datasetName=badcase&limit=10"
# 返回 {"data":[...]}（注意是 data 不是 items）
```

---

## 6. 迭代（BadCase 复审 → 修复 → 回归）

> 本节是日常质量运营的核心动作：把自动采集的 BadCase 变成「已修 / 无效」的判定，
> 再通过**回归集**把「线上暴露的问题」转成「发版门禁」，形成闭环。

### 6.1 为什么需要本地状态机

- Langfuse Dataset API 是 **append-only**：`create_dataset_item` 只有新增，没有
  update/delete → **无法在 item 上标记「已修复 / 已确认无效」**。
- 因此状态追踪放在本地 JSON：`{workspace}/eval/badcase_status.json`，
  **以 `source_trace_id` 为主键**，与 Dataset:badcase 每个 item 的 `source_trace_id` 一一对应。
- 与采集去重文件分工不同：

| 文件 | 主键 | 回答的问题 |
|---|---|---|
| `badcase_collected.json`（stamp） | trace_id | 「采过没有」（去重） |
| `badcase_status.json`（状态） | source_trace_id | 「这条现在什么状态」（生命周期） |

### 6.2 状态机与流转

```
                          ┌──→ fixed（根因已修复）┐
pending（新采集）─复审─→ reviewed（确认真问题，待修）├→ 已关闭（不进回归集）
                          └──→ invalid（误报/非真问题）┘
```

| 状态 | 含义 | 采集自动写入 | 回归集 |
|---|---|---|---|
| `pending` | 新采集、未人工复审 | ✅ register_batch | ✅ 开放 |
| `reviewed` | 已复审，确认真问题、待修 | — | ✅ 开放 |
| `fixed` | 根因已修复 | — | ❌ 关闭 |
| `invalid` | 误报 / 非真问题（如用户测试差评） | — | ❌ 关闭 |

- **回归集（open）= pending + reviewed**；fixed/invalid = 已关闭，不回归。
- **兼容旧数据**：状态文件里没有的 trace_id（stamp 有但 status 无）默认按**开放**
  处理（`is_open()` 未注册 → True），避免旧采集项被静默踢出回归集。

### 6.3 数据形态（badcase_status.json）

```json
{
  "9e0ad442c83011e7a1b2c3d4e5f60718": {
    "status": "pending",
    "question": "查 1990 年后上映、评分最高的电影",
    "db_name": "imdb",
    "reasons": ["sql_biz_correct_score=0.20", "user_feedback=0"],
    "collected_at": "2026-08-29",
    "updated_at": "2026-08-29T06:38:45.913Z",
    "note": ""
  }
}
```

字段：`status`（状态）/ `question`（用户问题，截断 200 字符）/ `db_name`（回灌定位同库）/
`reasons`（命中原因）/ `collected_at`（采集日）/ `updated_at`（最后更新）/ `note`（人工备注）。

### 6.4 采集自动注册（register_batch）

- `collect_badcase` 每轮循环结束后**批量**注册新条目为 `pending`（单次 IO：load + save，
  比逐条 register 高效）。
- 关键语义：**已有状态的条目不覆盖**——即使同一 trace 再次被采到，也不会把已 `fixed`
  的盖回 `pending`，人工判断永远保留。
- 返回本次新注册条数（日志：`状态跟踪：新注册 N 条 pending`）。

### 6.5 日常复审（review，交互式）

```bash
DC -m agent.eval.badcase_status review          # 生产（容器）
& $py -m agent.eval.badcase_status review       # 开发机
```

逐条展示 pending + reviewed（按 updated_at 倒序），**自动打开 Langfuse trace 页面**定位原始链路：

- 打开地址：`{LANGFUSE_BASE_URL}/project/{LANGFUSE_PROJECT_ID}/traces/{trace_id}`
- 每条展示：问题 / 命中原因 / 数据库

**操作键**：

| 键 | 写入状态 | 场景 |
|---|---|---|
| `f` | `fixed` | 根因已修复（会提示输入说明） |
| `i` | `invalid` | 误报 / 非真问题（会提示输入说明） |
| `r` | `reviewed` | 确认是真问题，但还没修 |
| `s` | — | 跳过，下次再看 |
| `q` | — | 退出 |

**复审 SOP**：打开 trace → 看「问题 + 命中原因」→ 回看子 agent 生成的 SQL 与结果是否真错 →
真错且已定位根因 → 修复后在发版门禁复验；未修 → `r`；确属测试/误报 → `i`。
`f` / `i` 时填一句说明（note），供后续追溯「当时为什么这么判」。

### 6.6 查看与手动标记（list / summary / mark）

```bash
# 状态分布 + 回归集规模（看整体：有多少待处理、回归集会多大）
DC -m agent.eval.badcase_status summary

# 列出（可过滤）
DC -m agent.eval.badcase_status list
DC -m agent.eval.badcase_status list --status pending,reviewed

# 手动标记（trace_id 支持前缀匹配，≥8 字符唯一即可）
DC -m agent.eval.badcase_status mark 9e0ad442 fixed --note "修复了 JOIN 逻辑"
DC -m agent.eval.badcase_status mark 707b335d invalid --note "用户测试差评"
DC -m agent.eval.badcase_status mark 707b335d reviewed          # 撤销误标，重新打开
```

`summary` 输出解读：

```text
BadCase 状态汇总（共 32 条）

  pending      8  ████████░░░░░░░░░░░░  开放（回归集）
  reviewed     5  █████░░░░░░░░░░░░░░░  开放（回归集）
  fixed       15  ███████████████░░░░░  已关闭
  invalid      4  ████░░░░░░░░░░░░░░░░  已关闭

  回归集规模: 13/32
```

`mark` 前缀匹配规则：完整 ID 精确命中 → 直接改；唯一前缀（≥8 字符）→ 命中该条；
多个 trace 同前缀 → 报错提示用更长前缀；完全不存在 → 自动创建一条并打上状态。
传非法状态（非 pending/reviewed/fixed/invalid）→ 拒绝并退出码 1。

### 6.7 回归集联动（run_experiment --from-badcase）

```bash
# 默认：只回归开放问题（pending,reviewed）
DC -m agent.eval.run_experiment --from-badcase --labels production prod-a

# 强制包含已关闭项（全量回归，用于复核 fixed 是否真的好了）
DC -m agent.eval.run_experiment --from-badcase --labels production prod-a --badcase-status all
```

- 语义：`--badcase-status` 未传时只加载 pending/reviewed；传 `all` 跳过状态过滤。
- **未注册 trace 默认包含**（兼容旧数据），与 `is_open()` 一致。
- 意义：`fixed`/`invalid` 的 badcase 不再拖累发版门禁——回归集只含「线上仍在暴露的问题」。

### 6.8 闭环判定与典型场景

**闭环定义**：问题被采集 → 人工复审判「真问题」→ 修复根因 → 同类问题不再被新采集 →
已修项离开回归集（fixed），发版门禁不再背旧债。

```text
采集(review) → 修根因(mark fixed) → 重跑 collect_badcase 观察：
  同类新查询不再命中 badcase 条件 → ✅ 闭环
  复现（新 trace_id 再被采）      → 重新 pending，自动回回归集
```

| 场景 | 处理 |
|---|---|
| `fixed` 后又复现 | 复现产生**新的 trace_id** → 重新采集为新 pending item → 自动回到回归集 |
| 误标 `fixed`/`invalid` | `mark <tid> reviewed` 重新打开，回到回归集 |
| `invalid` 的用户测试差评 | 从回归集移除，不再干扰发版门禁 |
| 批量误标 | `mark` 只支持单条；批量改需直接编辑 `badcase_status.json` 后重跑相关脚本 |
| 复核 fixed 是否真修好 | `run_experiment --from-badcase --badcase-status all` 全量回归 |

### 6.9 数据一致性注意

- 两套文件都在 `{active_workspace}/eval/`；**AGENT_DATA_ROOT 决定它们在哪**：
  - 生产容器：`AGENT_DATA_ROOT=/app/data` → `/app/data/workspace/eval/`
  - 开发机未配 AGENT_DATA_ROOT → 项目内 `src/agent/workspace/eval/`
- 换 AGENT_DATA_ROOT = 换一整套状态 / stamp（本机补采用隔离 AGENT_DATA_ROOT 时，
  与生产状态不共享——见 §1.2 env_loader 语义）。
- 状态文件用 `source_trace_id` 关联 Langfuse item；删了 Langfuse item 不会同步清本地
  状态，属预期（状态是独立生命周期，不是 item 的镜像）。

---

## 7. A/B 测试

### 7.1 prompt label 分流机制（`resolve_prompt_label`，进程级）

```
1. LANGFUSE_PROMPT_LABEL  显式指定（run_experiment A/B、灰度演练直接用，最高优先）
2. LANGFUSE_CANARY_LABEL + LANGFUSE_CANARY_RATIO  按比例掷骰，命中走 canary
3. 默认 production（全局旧版）
```

进程级、import 时掷一次；trace metadata.prompt 记录分流结果（`prompt_label`），
是 `feedback_gate` 分组与 UI 上 A→B 可见分组的依据。

### 7.2 离线 A/B + 回归门禁（`run_experiment`）

```bash
# 单 label（canary 预检）：只跑不对比，PASS
DC -m agent.eval.run_experiment --from-badcase --labels prod-a

# A/B 对比 + 门禁：首个 label 为 reference，核心维掉超阈值 → exit 1
DC -m agent.eval.run_experiment --from-badcase --labels production prod-a --threshold 0.05

# 自定义查询集（也可与 --from-badcase 合并）
DC -m agent.eval.run_experiment --queries /app/queries.json --labels production prod-a

# 追加 LLM-judge 打分（慢、控成本）
DC -m agent.eval.run_experiment --from-badcase --labels production prod-a --judge
```

关键参数：`--labels A B`（首个=reference）、`--threshold 0.05`、`--from-badcase-limit N`、
`--badcase-status pending,reviewed`（传 `all` 包含 fixed/invalid）、`--timeout 1800`。

- 每个 label 一个 **worker 子进程**（`LANGFUSE_PROMPT_LABEL` 在 import graph 前注入 → 进程级 A/B 隔离）；
- **顶层 import graph**（MCP 工具在 asyncio 之外加载，与生产启动同路径）；
- **门禁只看核心维** `sql_biz_correct_score` / `sql_valid_score` / `sql_exec_success`
  （+ 展示维 `schema_match_score`）：candidate 均值 < reference 均值 − 阈值 ⇒ exit 1；
- 实验 trace 打 `tags=["nl2sql","experiment"]`，不污染生产视图；
- 结果 manifest → `{workspace}/eval/experiment_runs/run_{stamp}.json`。

### 7.3 在线 A/B（canary）+ 真实反馈门禁（`feedback_gate`）

**canary 分流**（放量阶段，无需代码改动）：

```bash
# 在要成为 canary 的容器 .env.prod 加，重启该容器：
LANGFUSE_CANARY_LABEL=staging
LANGFUSE_CANARY_RATIO=0.1        # 该实例 import 时约 10% 概率走 staging
```

**在线门禁**（每日 cron 已含；手动跑）：

```bash
# 报表（不门禁，exit 恒 0）
DC -m agent.eval.feedback_gate --days 7 --report-only

# 门禁：production vs prod-a，好评率差 > 5% 且每组 ≥5 条反馈则判回归
DC -m agent.eval.feedback_gate --days 7 --ref production --cand prod-a \
   --threshold 0.05 --min-rated 5

# 数据不足也按失败处理（exit 2）
DC -m agent.eval.feedback_gate --days 7 --ref production --cand prod-a --fail-insufficient
```

| 退出码 | 含义 |
|---|---|
| 0 | 通过 / 数据不足跳过（默认） |
| 1 | **回归**：candidate 好评率 < reference − 阈值（不放量/触发回滚） |
| 2 | 数据不足 + `--fail-insufficient` |

- 数据源：`user-feedback` score（好评 1 / 差评 0，comment=评语）；
- **去重**：同一 trace 多次打分取最新；v4 撤销哨兵分（value<0）剔除，撤销后残留好评不误计；
- 分组：trace metadata.prompt 的 `prompt_label`（v4 下读 root observation metadata）；
- manifest → `{workspace}/eval/feedback_gates/feedback_gate_{stamp}.json`。

---

## 8. 发版（prompt 版本管理与灰度放量）

### 8.1 版本模型

- **prompt 名**：`main_system_prompt` / `nl2sql_system_prompt` / `skill/{group}/{skill_dir}`（M6）。
- **标签语义**：`latest` 恒指最新版本（Langfuse 托管）；`production` 指当前对外版本；
  `staging` / `prod-a` 等供灰度。A/B 分流按 label 取正文，切 label 即切版本。

### 8.2 同步命令（本地文件 → Langfuse，单向初始化）

```bash
# 同步主/子两个 system prompt
DC -m agent.prompt.sync_prompts --all

# 只同步某一个
DC -m agent.prompt.sync_prompts --name main_system_prompt

# 同步全部 skill SKILL.md（新增 skill 后必须跑一次，消 Prompt-not-found 警告）
DC -m agent.prompt.sync_prompts --skills

# 灰度：只打 staging+latest（production 不动）
DC -m agent.prompt.sync_prompts --all --label staging --commit "改进了 XX"

# 内容未变也强制新版本（mint 新 label 首次需此参数）
DC -m agent.prompt.sync_prompts --skills --label staging --force
```

> 之后改动**一律在 Langfuse UI 编辑**（版本管理的入口），打标签即发版；本地文件仅作基线。

### 8.3 一次标准灰度放量流程（check-list）

```text
① 看现状：collect_badcase summary → 最近差评集中在哪
② 复审：badcase_status review（mark fixed/invalid，缩小回归集）
③ 改 prompt → sync --label staging --force          # 打 staging
④ 离线预检：run_experiment --from-badcase --labels production staging
   → exit 0 才继续（核心维无回归）
⑤ canary：部分容器设 LANGFUSE_CANARY_LABEL=staging RATIO=0.1 重启
⑥ 观察：Langfuse 按 prompt_label 看 staging 组五维分 + user-feedback
⑦ 在线门禁：feedback_gate --ref production --cand staging（exit 0 才放量）
⑧ 全量：UI 把 production 标签打到 staging 的最新版本（production 自动离开旧版本）
⑨ 回看：次日 cron 自动采集 + 门禁，确认新版本不再产生同类 badcase
```

### 8.4 回滚手段（从局部到全局，按序升级）

| 手段 | 操作 | 影响面 |
|---|---|---|
| ① 退 canary | 清 `LANGFUSE_CANARY_LABEL/RATIO` 重启容器 | 全部实例回 production |
| ② 显式 label | 置 `LANGFUSE_PROMPT_LABEL=production` 重启 | 该进程强制旧版 |
| ③ prompt 回退 | UI 把 `production` 标签移到旧版本（`update_prompt_labels`） | 全量走旧版本 prompt |
| ④ 强制本地 | 置 `LANGFUSE_PROMPT_ENABLED=0` 重启 | 用本地 prompt 文件，绕过 Langfuse |
| ⑤ 总开关 | 置 `LANGFUSE_ENABLE=false` 重启 | 关一切运行时埋点/打分/prompt（监控旁路整体关停；管理工具仍可用） |

---

## 9. 常见坑与速查

### 9.1 已知坑

| 坑 | 说明 |
|---|---|
| 容器 exec 报 `No module named 'agent'` | 容器 venv 无 `_nl2sql_src.pth`，须 `PYTHONPATH=/app/src` + venv python（见 §1.1） |
| `bash -lc` 丢 PATH | 登录 shell 重设 PATH 会丢 venv → 系统 python 无依赖；用 `bash -c` |
| `trace.list` limit | 单页 ≤100，分页拉取 |
| `observations?sessionId=` | v4 下该参数 422，须用 filter |
| dataset-items 返回 | `{"data":[...]}`，不是 `{"items":[...]}` |
| v3 读接口 404 | v4 events_only 迁移完成（`langfuse_v4_reads`）；写入正常、读走 v4 |
| judge 假阴性 | NL 表名归一化（artists→artist），rubric 已声明不能据此判错 |
| 进程内 invoke 必须顶层 import | `agent.graphs.nl2sql_agent` 顶层导入（MCP 工具在 asyncio 外加载），否则撞 loop |
| `sync_prompts` 新 label | mint 新 label 首次需 `--force`（否则与 production 内容一致被跳过） |
| feedback_gate 同 trace 多次打分 | 按 trace 取最新去重；撤销哨兵 value<0 剔除 |
| `cmd | tail` 的 `$?` | 管道后 `$?` 是 tail 的退出码，不是命令的 |
| 观察 trace 生效证据 | 看启动日志版本号 / `prompt label=` / `release=`，deepagents 栈 system prompt 不进 trace |

### 9.2 命令速查

```
采集：        collect_badcase --days 1 [--force]
用户反馈：    GET /api/threads/{tid}/feedback | GET /api/feedback/export
状态：        badcase_status review | summary | list | mark <tid> <status> [--note]
离线 A/B：    run_experiment --from-badcase --labels A B [--judge]
在线门禁：    feedback_gate --days 7 --ref A --cand B [--report-only]
同步 prompt：  sync_prompts --all | --skills | --name X [--label staging --force]
验证 dataset： curl -u pk:sk ".../api/public/dataset-items?datasetName=badcase"
```

---

## 10. 相关文档

| 文档 | 内容 |
|---|---|
| [Langfuse接入实现方案.md](Langfuse接入实现方案.md) | M1~M8 接入设计（callbacks、评分、prompt、灰度、闭环） |
| [Langfuse标准Trace层级方案.md](Langfuse标准Trace层级方案.md) | M-T1~T6 trace 层级 / 归属规范 |
| [项目脚本命令手册.md](项目脚本命令手册.md) | 各 eval/prompt 命令参数全表 |
| [BadCase状态标记使用指南.md](BadCase状态标记使用指南.md) | badcase_status 使用细节 |
| [../weint环境/NL2SQL-部署与更新手册.md](../weint环境/NL2SQL-部署与更新手册.md) | 生产部署 / 容器 / 每日调度注册与启停 |
