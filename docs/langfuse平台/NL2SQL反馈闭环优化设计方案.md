# NL2SQL 反馈闭环优化设计方案

> **状态**：已实施（P0~P2 全部上线，2026-09-02 补标注页按状态操作流程） · **最后更新**：2026-09-02
> **定位**：面向「nl2sql 闭环」的三项优化——① 用户反馈类型区分（查询/闲聊）② 反馈看板指标 ③ 待标注队列 + 标注页面 + 人工正确 SQL + BadCase 生成（含 bad_type 错误类型）。
> **与现网关系**：全部为**增量**改造，不改变现有反馈写入/打分链路的数据语义；兼容存量数据。
> **配套手册**：[NL2SQL-Langfuse全流程实现手册.md](NL2SQL-Langfuse全流程实现手册.md)（§3 用户反馈 / §5 采集 / §7 门禁 / §6 迭代状态机）。

---

## 0. 背景与现状

**现有反馈闭环**（已上线，见全流程手册 §3）：

```
前端 👍/👎/评论 ──► PUT /api/threads/{tid}/messages/{mid}/feedback
  ├─► 本地 FeedbackStore（SQLite：rating/note/question/sql/context，CAS，唯一真相）
  └─► 后台线程写 Langfuse user-feedback score（好评1/差评0，撤销-1 哨兵分，按 message_id 精确归属 trace）
下游：collect_badcase（user-feedback=0 → Dataset:badcase） · feedback_gate（按 prompt_label 比好评率）
```

**三个痛点**（即本次优化动机）：

| # | 痛点 | 后果 |
|---|---|---|
| ① | user-feedback 不区分「查询问题」还是「闲聊」 | 闲聊差评（如"回复太啰嗦"）被当成 NL2SQL 质量信号 → 误采 badcase、拉低好评率门禁 |
| ② | 反馈只有散点数据，无看板 | 没有「反馈总数 / 信噪比 / 正负比例」总览，无法快速评估质量走势 |
| ③ | 复审只能 CLI（`badcase_status review`），无 UI、无人工补正确 SQL 能力、badcase 无错误类型 | 无法沉淀"金标 SQL"，回归只能跑分数、不能按错误类型归因 |

---

## 1. 总体设计

三项优化在闭环中的位置与新增组件：

```
优化① 反馈类型判别 优化② 看板          优化③ 标注闭环
用户👍/👎/评论 ──► FeedbackStore ──► /api/feedback/stats ──► 前端「反馈看板」页
   │                │  ▲feedback_type(查询/闲聊)
   │                ▼
   ├─► Langfuse user-feedback score（metadata 带 feedback_type）
   └─► 待标注队列 feedback_annotation ──► 前端「标注页面」（判断有效 → 补正确SQL → 执行 → 定 bad_type）
                                          └─► confirm → Dataset:badcase + badcase_status(bad_type/gold_sql)
```

| 新增/改动 | 层 | 说明 |
|---|---|---|
| `feedback.feedback_type` 列 + 两级判定 | 后端 store + v4 读封装 | 优化① |
| `GET /api/feedback/stats` | 后端新模块 `api/feedback_stats.py` | 优化② |
| 前端「反馈看板」页 | 前端 | 优化② |
| `feedback_annotation` 表 + 5 个 API | 后端新模块 `api/feedback_annotation.py` | 优化③ |
| 前端「标注页面」 | 前端 | 优化③ |
| `badcase_status` 扩展 `bad_type/gold_sql` | 后端 eval | 优化③ |
| `collect_badcase` / `feedback_gate` 按 `feedback_type` 收口 | 后端 eval | 优化①消费端 |

---

## 2. 优化①：用户反馈类型区分（NL2SQL 查询 vs 闲聊）

### 2.1 目标

每条反馈自动标 `query`（针对真实数据查询）或 `chat`（闲聊/无关对话），使「差评→badcase」「好评率→门禁」只统计查询类信号，闲聊反馈仅计入总数与信噪比看板。

### 2.2 判定信号（按可靠性排序）

| 信号 | 来源 | 可靠度 | 说明 |
|---|---|---|---|
| 确定性分 | trace 上存在 `schema_match_score`/`sql_valid_score`/`sql_exec_success`/`sql_biz_correct_score` | ★★★ | `LangfuseSpanMiddleware` 只在 sql-execution/schema-linking 工具写分；**闲聊 trace 恒无、查询 trace 恒有**（失败也写 `sql_exec_success=0`） |
| run_sql 族 observation | trace 存在 `skill:sql-execution:*` 或工具名以 `run_sql`/`dry_run`/`query_cube` 结尾 | ★★★ | 查询铁证 |
| SQL 快照 | 本地 `FeedbackStore.sql` 非空 | ★★☆ | 零成本；Cube 快速通道无 run_sql 时可能为空 |
| db_name/关键词 metadata | trace metadata | ★☆☆ | 闲聊轮也可能带 db_name，**不采用** |

### 2.3 实现方案

**a. 判定函数**（`src/agent/trace/langfuse_v4_reads.py` 新增）：

```python
QUERY_SCORE_DIMS = ("schema_match_score", "sql_valid_score",
                    "sql_biz_correct_score", "report_table_score",
                    "analysis_report_score", "sql_exec_success")
QUERY_TOOL_SUFFIXES = ("run_sql", "dry_run", "dry_plan", "query_cube")

def classify_feedback_type(thread_id: str, message_id: str,
                           sql_snapshot: str = "") -> str:
    """反馈类型：'query' | 'chat' | ''（未判定）。
    快路径：sql 快照非空 → query（本地零成本）。
    慢路径：find_message_trace_id 定位 trace → 有 QUERY_SCORE_DIMS 分
            或 run_sql 族 observation → query；否则 chat。
    失败/无 trace → 返回 ''（调用方按不统计处理，不阻塞）。
    """
```

- 快路径由反馈写入时填（见 c）；慢路径在**聚合（stats）与入队（annotation）时**按需调用，带 LRU 缓存（`functools.lru_cache(maxsize=2048)`，key=`{thread_id}::{message_id}`），避免看板反复打 Langfuse。

**b. 数据表变更**（`src/agent/feedback/store.py`，`_SCHEMA` 加一列，兼容存量用迁移 SQL）：

```sql
ALTER TABLE feedback ADD COLUMN feedback_type TEXT NOT NULL DEFAULT '';
-- 存量行：'' = 未判定（聚合时惰性回填，不回写历史）
```

`FeedbackRecord` 加字段；`upsert`/`to_mapping`/`from_mapping` 同步。新增 `set_feedback_type(thread_id, message_id, ftype)`（不 bump version，仿 `update_snapshot`）。

**c. 写入侧（快路径）**（`src/api/message_feedback.py`）：
- `_schedule_snapshot_backfill` 补齐 question/sql 后，若 `sql` 非空 → `store.set_feedback_type(..., "query")`。
- `_schedule_langfuse_score` 的 `create_score(...)` 在 `metadata` 里加 `"feedback_type": <快路径结果>`（v4 会字符串化，读端用 `_parse_str_dict` 还原）。

**d. 消费端收口**（默认只统计 query，保留开关）：
- `collect_badcase.py` 条件 3（L153 `if scores.get("user-feedback") == 0`）改为：
  ```python
  if scores.get("user-feedback") == 0 and fb_type == "query":
      reasons.append("user_feedback=0")
  ```
  `fb_type` 从 `get_trace_metadata(rep_id)` 的 score metadata 或 store 读；`chat` 差评跳过采集并打 DEBUG 日志（可加 `--include-chat` 观察开关）。
- `feedback_gate.py`：`_fetch_scores` 后按 metadata `feedback_type` 过滤，默认只保留 `query`；加 `--include-chat` 参数。好评率门禁口径更干净。

### 2.4 用户如何使用

- **零操作**：判定全自动，评估/发版人员无需干预。
- 看板页（§3）可切换「全部 / 查询类 / 闲聊类」视图，直接看出信噪比。
- `GET /api/feedback/export` 导出结果新增 `feedback_type` 列，供离线分析。
- 已产生的存量反馈 `feedback_type=''`：首次聚合时自动惰性判定，无需人工补标。

### 2.5 边界

- 无 trace / Langfuse 不可达 → 返回 `''`，按「不参与 query 统计」处理（不误判为 chat，避免闲聊被当成查询信号的反向污染）。
- `feedback_type` 是**附加元数据**，不改 `user-feedback` score 的 value 语义（v4 无 score 更新，规避）。

---

## 3. 优化②：反馈看板指标

### 3.1 指标口径（先行定义）

| 指标 | 口径 |
|---|---|
| 反馈总数 | 近 N 天 `feedback` 表有效记录数（rating ∈ positive/negative；撤销即本地删除，天然不含） |
| 正负反馈比例 | `positive / negative`，好评率 `positive / total` |
| 反馈信噪比 | **查询类反馈数 / 闲聊类反馈数**（signal=对真实查询、可行动；noise=闲聊）。chat=0 时显示 `∞` 或 `N/A` |
| 趋势 | 按天计数（总数 + 好评率双序列），供折线图 |

### 3.2 实现方案

**后端**（新模块 `src/api/feedback_stats.py`，注册进 [custom_app.py](src/api/custom_app.py) 的 `ROUTES`）：

```http
GET /api/feedback/stats?days=7
```

```json
{
  "days": 7,
  "total": 123, "positive": 80, "negative": 43, "positive_rate": 0.6504,
  "query":  { "count": 95, "positive": 70, "negative": 25 },
  "chat":   { "count": 28, "positive": 10, "negative": 18 },
  "signal_noise_ratio": 3.39,
  "trend": [
    {"date": "2026-08-24", "total": 18, "positive": 12},
    {"date": "2026-08-25", "total": 21, "positive": 14}
  ]
}
```

实现要点：
- 数据源 `FeedbackStore`：新增 `stats(days)` 聚合（单条 SQL `GROUP BY date(created_at), rating` + 分组统计），毫秒级。
- `feedback_type=''` 的行：聚合时对每条调 `classify_feedback_type` 惰性判定（LRU 缓存命中即零成本），不回写历史。
- Langfuse 不可达也不影响看板（本地 store 为唯一真相）。

**前端**（`harness-deep-agents-ui`）新增「反馈看板」页 `/feedback`：
- 卡片区：反馈总数 / 好评率 / 信噪比 / 查询类差评数。
- 图表区：每日趋势（折线，总数+好评率双轴）、正负占比（环形）、类型占比（query vs chat 堆叠条）。
- 下钻：点某天 → 该日反馈明细表（问题/评论/类型/评分/时间），支持跳转会话。

### 3.3 用户如何使用

1. 前端侧栏「反馈看板」→ 默认近 7 天。
2. 顶部切换日期范围（7/30/90 天）。
3. 读三卡：总数、好评率、**信噪比**——信噪比高说明差评大多是真查询问题（可行动）；低说明闲聊抱怨占比大。
4. 想看某天明细 → 点击趋势图该天 → 明细表 → 点「查看会话」跳回对话排查。

### 3.4 边界

- 趋势按本地时区取日切（与 `created_at` UTC 存值换算，实现时统一用服务器本地时区展示）。
- 前端图表用项目现有图表栈（不引入新依赖），DLS/加密文件改动走既有通道。

### 3.5 Langfuse UI 仪表板自建（2026-08-30 补充）

> 目标：把反馈指标做到 **Langfuse UI 的 Custom Dashboards** 里（Dashboards → Widgets → New Widget，数据源选 Evaluation scores），不用依赖 harness 前端页。本地 `/feedback` 页保留为**离线权威**（Langfuse 分数是尽力而为镜像，写分时不可达不落；v4 无删分 API、撤销用哨兵，看板为近似值）。

**数据模型（每条反馈在 cloud 项目里落两档分）**：

| Score name | data_type | value | 说明 |
|---|---|---|---|
| `user-feedback` | NUMERIC | `1` 好评 / `0` 差评 / `-1` 撤销哨兵 | 原有，`feedback_gate`/`collect_badcase` 依赖 |
| `feedback_type` | CATEGORICAL | `query` / `chat` / `revoked` | 新增（[message_feedback.py](src/api/message_feedback.py) `_schedule_langfuse_score`/`_schedule_langfuse_revoke`）；unknown 不写；受 `LANGFUSE_ENABLE` 总开关控制 |

**Widget 配置清单**：

| 指标 | 数据源 | 指标(Metric) | 筛选(Filter) | 维度 | 图表 |
|---|---|---|---|---|---|
| 反馈总数 | Evaluation scores（numeric） | Count | `name = 'user-feedback'` 且 `value >= 0`（排除 -1 撤销） | 时间按天 | Big number / 时间柱状 |
| 好评率 | Evaluation scores（numeric） | **Average of `value`**（0~1 → 即好评率）；或建 value=1 / value=0 两个 count widget 手工比例 | `name = 'user-feedback'` 且 `value >= 0` | — | Big number / Gauge |
| 信噪比 | Evaluation scores（categorical） | Count，**建两个 widget**：一个 filter `value='query'`、一个 `value='chat'`；信噪比 = query 数 ÷ chat 数（chat=0 → ∞） | `name = 'feedback_type'` | 可按天看趋势 | Big number / Pie（query vs chat 占比） |
| 每日趋势 | Evaluation scores（numeric） | Count（总数）+ Average（好评率）双序列 | `name = 'user-feedback'` 且 `value >= 0` | 时间按天 | Time-series 折线/柱状 |

**口径提醒**：
- **总数/好评率**：filter 必须带 `value >= 0`，否则 `-1` 撤销哨兵会被计入。
- **信噪比**：`feedback_type` 只对判定成功（query/chat）写分；unknown 反馈不出现在该 widget（本地页单独归组）。撤销后 query/chat 旧行仍在（v4 无删分 API），对「撤销后重算」是近似值。
- **开关**：`LANGFUSE_ENABLE=false` 时不写任何分（含 `feedback_type`），看板数据暂停刷新。
- 三档指标用同一 `name` 过滤即可在**同一 Dashboard** 上并排，widget 间无跨查询公式（信噪比需手动除或各建一个）。

---

## 4. 优化③：待标注队列 + 标注页面 + 人工 SQL + BadCase

### 4.1 业务流程与状态机

```
用户 差评/写评论 ──入队──► queued（待人工判断）
                          │ 标注页：判断是否有效反馈
             有效 ──► annotating ──► validated     ← 人工填正确 SQL + 点「执行」看结果（gold_sql/gold_result 就绪）
             无效 ──► rejected（误报/闲聊，丢弃）
validated ──选 bad_type + 点「确定」──► badcase（写 Dataset:badcase + badcase_status.json + 回写队列状态）
```

- **入队规则**：`note` 非空（有评论）**必入队**；无评论差评**默认入队**（差评本身是复审信号）；好评无评论不入队。入队 `INSERT OR IGNORE`（按 PK 幂等，已处理的不重复入队）。

### 4.2 数据表设计（与 feedback 同库，[store.py](src/agent/feedback/store.py) 模式：RLock + WAL）

```sql
CREATE TABLE IF NOT EXISTS feedback_annotation (
    thread_id   TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    feedback_type TEXT NOT NULL DEFAULT '',     -- 优化①
    question    TEXT NOT NULL DEFAULT '',       -- 用户问题（store 快照）
    bad_sql     TEXT NOT NULL DEFAULT '',       -- 模型生成 SQL（快照，可能空）
    exec_error  TEXT NOT NULL DEFAULT '',       -- 执行错误（可空）
    note        TEXT NOT NULL DEFAULT '',       -- 用户反馈评论
    status      TEXT NOT NULL DEFAULT 'queued', -- queued/annotating/validated/rejected/badcase
    is_valid    INTEGER,                        -- 人工判断 1/0
    gold_sql    TEXT NOT NULL DEFAULT '',       -- 人工正确 SQL
    gold_result TEXT NOT NULL DEFAULT '',       -- 执行结果（JSON 或截断文本）
    bad_type    TEXT NOT NULL DEFAULT '',       -- 6 大类枚举（§4.7）
    annotator   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    annotated_at TEXT NOT NULL DEFAULT '',
    badcase_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_fa_status ON feedback_annotation(status);
```

`FeedbackStore` 新增 `enqueue/ list_annotations(status)/ get_annotation/ update_annotation` 等方法（复用现有锁/连接，同库事务原子）。

### 4.3 API 设计（新模块 `src/api/feedback_annotation.py`）

| 端点 | 方法 | 请求体 | 作用 |
|---|---|---|---|
| `/api/feedback/annotations` | GET | `?status=queued&limit=50` | 队列列表（问题/反馈/库/时间/状态） |
| `/api/feedback/annotations/{tid}/{mid}` | GET | — | 详情：问题、模型 SQL、执行错误、评论、db、trace 链接（字段为空时惰性从 trace 补齐） |
| `/api/feedback/annotations/{tid}/{mid}/judge` | POST | `{is_valid: bool}` | 有效 → `annotating`；无效 → `rejected` |
| `/api/feedback/annotations/{tid}/{mid}/execute` | POST | `{sql, db_name?}` | **执行人工 SQL 并返回预览结果**（§4.5） |
| `/api/feedback/annotations/{tid}/{mid}/confirm` | POST | `{gold_sql, bad_type, note?}` | 确定 → 生成 badcase（§4.6），状态 → `badcase` |

注册进 [custom_app.py](src/api/custom_app.py) 的 `ROUTES`。

### 4.4 标注页面（前端，用户核心操作）

新增路由 `/feedback/annotate`（`harness-deep-agents-ui`；复用 `MessageFeedbackActions` 的数据流，新页面组件走非 DLP 加密路径）。

**布局**：左列队列表 + 右详情区。

**标注员操作步骤**：

1. 打开「标注页面」→ 左列显示待处理队列（`queued`），按时间倒序。
2. 点一条 → 右区展示：**用户问题** / **模型生成 SQL**（只读）/ **执行错误**（如有）/ **用户评论** / **数据库** / **Langfuse trace 链接**。
3. 判断「是否有效反馈」：
   - 点 **「无效（误报/闲聊）」** → 该条转 `rejected`，移出队列。→ 结束。
   - 点 **「有效」** → 进入标注编辑区。
4. 编辑区：**SQL 编辑器**（预填模型 SQL，可修改）→ 点 **「执行」** → 右侧出现**结果表格**（列名+行数+预览），确认人工 SQL 正确、结果符合问题意图。
5. 下拉选择 **bad_type 错误类型**（§4.7，6 类，可搜索）+ 备注（可选）→ 点 **「确定入 BadCase」** → 后端生成 badcase（§4.6），该条在队列中状态变 `badcase`（可保留在「已处理」过滤下复查）。
6. 队列顶部有状态筛选（全部/待处理/已处理/无效），可统计当天处理量与 bad_type 分布。

**权限**：标注页限管理员/标注角色（前端路由守卫；后端 judge/execute/confirm 端点加简单鉴权或 `?key=` 校验，首版可用现有前端 auth）。

### 4.4.1 标注页操作流程（按状态 · 实施现状，2026-09-02）

> 与 4.4 设计稿的差异：实际 UI 已上线 Good Set 正向样本、点赞「直接入 Good Set」、标注人字段、状态 Tab 全量（含 validated/rejected/badcase/good）、BadCase/GoodCase Tab 直读 Langfuse Dataset。下面按**当前页面实际操作**描述。

**状态机**：

```
queued 待判断 ──① 有效 + 点赞 ──► good（直接入 Good Set，跳过改 SQL）
        │──② 有效 ──────────► annotating ──「执行验证」──► validated
        │                                              ├──③ 正确 ──► good
        │                                              └──④ 错误 ──► badcase
        └──⑤ 无效 / 误报 / 闲聊 ──► rejected
```

**页面结构**（`/feedback/annotate`，入口=聊天主页右上「待标注」链接）：
- 顶栏：返回聊天 + 标题 + 「刷新」+ **标注人**输入框（可选，存 localStorage，用于追溯「谁标注的」）
- 左侧：状态 Tab（全部 / 待判断 / 标注中 / 已验证 / 已驳回 / BadCase / GoodCase）+ 队列列表（每条显示状态、反馈类型、问题摘要、👍/👎、会话/消息 id）
- 右侧：选中记录的详情 + 操作面板

**按状态的操作**：

1. **待判断 queued** — 人工判断这条反馈是否有效：
   - 「**有效查询，直接入 Good Set**」（仅点赞反馈）→ 一步入集，跳过改 SQL
   - 「**有效反馈，进入标注**」→ 进标注中
   - 「**无效 / 误报，驳回**」→ rejected
2. **标注中 annotating / 已验证 validated** — 修正并验证 SQL：
   - SQL 编辑器（预填模型 SQL，后端只读护栏，写/DDL 直接拒绝）→ 填库名（默认用反馈带的）→ 「**执行验证**」→ 结果表格预览（前 30 行）→ 状态自动变 **validated**
   - 「**确认入 Good Set**」→ 写 `Dataset:goodcase`（正向样本），终态 good
   - **BadCase 区**：选**错误类型**（下拉带说明）+ 填**金标 SQL**（正确写法，须能执行通过）+ 备注 → 「**确定入 BadCase**」→ 写 `Dataset:badcase` + 回归集，终态 badcase
3. **终态**（BadCase / Good / 已驳回）— 只读展示（错误类型、金标 SQL、金标结果、确认时间、标注人）

**规则与提示**：
- **点赞反馈不能入 BadCase**（按钮置灰，只能走 Good Set 或驳回）——差评才走 badcase
- 入 BadCase 必须：错误类型 + 金标 SQL（后端会先执行校验，失败则拦）
- 标注人字段可选，不填也能操作；填了落库并显示在详情里
- BadCase / GoodCase 两个 Tab **直读 Langfuse Dataset**（含来源：`auto-collect` 自动采集 / `user-annotation` 人工确认），与 Langfuse UI 一致

**产物去向（闭环终点）**：
- BadCase → Langfuse `Dataset:badcase` + `badcase_status.json`（status=reviewed，进回归集）→ `run_experiment --from-badcase` 回归评测（默认跳过 fixed/invalid）
- Good Set → Langfuse `Dataset:goodcase` → 正向样本评测
- 每日 02:13 cron `collect_badcase` 自动采集（feedback_gate 门禁，`feedback_type=query` 收口）

### 4.5 「执行」复用 dbmcp 引擎（不新写执行器）

`db_server.py` 已有四引擎执行链（`_RUNNER_REGISTRY` mysql/clickhouse/postgres/sqlite + `_load_runner_class` + 服务端默认 LIMIT + `split_sql_statements`/`combine_multi_results`），标注页直接复用：

```python
# api/feedback_annotation.py:execute 伪代码
from agent.middlewares.sql_approval import classify_sql          # 只读护栏
from mcp_server.db_mcp_server.db.db_server import _load_runner_class
from mcp_server.db_mcp_server.db.core.db_config_store import DbConfigStore

def _run_preview(db_name: str, sql: str) -> dict:
    kind, _ = classify_sql(sql)                                   # ① 只读校验
    if kind != "read":
        return {"error": f"标注执行仅允许只读查询：{kind}"}
    cfg = DbConfigStore().get_config(db_name)                     # ② 取连接配置
    runner = _load_runner_class(cfg.db_type)(cfg)                 # ③ 实例化引擎
    df = runner.run(sql)                                          # ④ 执行（默认 LIMIT 预览）
    return {"columns": list(df.columns), "rows": df.head(100).to_dict("records"),
            "row_count": len(df)}
```

要点：
- **执行前必须 `classify_sql` 只读校验**（与运行时门禁同一判据），杜绝人工误跑写/DDL；
- 复用 `DbConfigStore`（db_config.json + AES-GCM 解密），`db_name` 来自反馈快照 `context.db_name` 或标注页选择；
- 返回前截断（≤100 行 + 长文本截断），前端表格渲染。

### 4.6 「确定」→ 生成 BadCase（三处写入）

```python
# api/feedback_annotation.py:confirm 伪代码
# ① Dataset:badcase（Langfuse，复用 collect_badcase 的 create_dataset_item 模式）
client.create_dataset_item(
    dataset_name="badcase",
    input={"question": question, "session_id": thread_id},
    expected_output={"sql": gold_sql},                    # ← 人工金标，供 exact-match 回归
    metadata={"source": "user-annotation", "bad_type": bad_type,
              "reasons": ["user_feedback=0", "manual_annotation"],
              "db_name": db_name, "source_trace_id": trace_id},
    source_trace_id=trace_id,
)
# ② badcase_status.json 扩展（人工确认=reviewed，自动进回归集）
badcase_status.annotate(trace_id, bad_type=bad_type, gold_sql=gold_sql, note=note)
# ③ 回写 annotation 状态 → badcase，记 badcase_at
store.update_annotation_status(tid, mid, "badcase", bad_type=bad_type, gold_sql=gold_sql)
```

**`badcase_status.py` 扩展**（[badcase_status.py](src/agent/eval/badcase_status.py)）：
- entry schema 增加 `bad_type` / `gold_sql` 字段；
- 新增 `annotate(trace_id, bad_type, gold_sql, note)`：`status="reviewed"`（人工确认有效，`DEFAULT_INCLUDE` 含 reviewed → 自动进回归集），补 bad_type/gold_sql；
- `summary` 增加按 `bad_type` 分布打印。

**顺带收益（gold 回归）**：`run_experiment --from-badcase`（§6.7）回归时，命中含 `gold_sql` 的 item 增加 **exact-match 对比**（生成 SQL 与金标做规范化比对：大小写/空白/别名归一化，可先用字符串归一化、后续可升级为 AST 对比），并按 `bad_type` 分组输出「修复后各错误类型收敛情况」。

### 4.7 bad_type 枚举（6 大类，可直接下拉/统计/打标签）

> ⚠️ 用户原列表在「多表关联错」处截断，第 5/6 类为**补全提案**，实施前请确认/替换。

| 枚举值 | 主类型 | 判定口径（示例） |
|---|---|---|
| `table_hallucination` | 表幻觉 | 使用不存在的表 / 表名写错 |
| `column_hallucination` | 字段幻觉 | 使用不存在的字段 / 字段归属错表 |
| `time_condition_error` | 时间条件错误 | 时间范围/粒度/时区/日期比较错（如"近3天"写成反向 BETWEEN） |
| `agg_logic_error` | 聚合与统计错误 | GROUP BY 缺漏 / 聚合函数错 / 去重错 / 比率口径错 |
| `join_error` | 多表关联错误 | JOIN 条件错 / 漏 ON / 笛卡尔积 / 表关系方向错 |
| `filter_condition_error` | 过滤条件错误 | WHERE 条件缺失 / 运算符错 / 取值错 |

落地：常量 `BAD_TYPE_CHOICES = [...]`（`src/agent/eval/badcase_status.py` 或独立 `bad_types.py`），前后端共享同一枚举（后端返回 `GET /api/feedback/annotations/bad-types` 或前端写死后与后端校验）。

### 4.8 与现有闭环的联动

| 现有能力 | 联动方式 |
|---|---|
| `badcase_status` 状态机（§6） | 标注产生的 badcase 直接为 `reviewed`（人工确认），进回归集；CLI `review` 保留兜底 |
| `run_experiment --from-badcase`（§7.2） | 有 gold_sql 的 item 增加 exact-match + 按 bad_type 分组 |
| `feedback_gate`（§7.3） | 沿用优化① 的 `feedback_type=query` 过滤，好评率更纯净 |
| `collect_badcase`（§5） | 标注入的 badcase 带 `source=user-annotation`，与自动采集（`source=auto-collect`）区分 |
| Langfuse Dataset | 同一 `Dataset:badcase`，标注/自动条目并存，用 metadata.source/bad_type 区分 |

---

## 5. 实施步骤（分期，各期可独立上线）

### P0：反馈类型区分 + 看板（收益立现、风险最低）
- [ ] `feedback_type` 列迁移 + `classify_feedback_type`（快/慢路径）+ LRU 缓存
- [ ] `message_feedback.py` 快路径落库 + score metadata
- [ ] `collect_badcase` / `feedback_gate` 按 query 收口（+ `--include-chat`）
- [ ] `GET /api/feedback/stats` + 前端「反馈看板」页
- **DoD**：闲聊差评不再进 Dataset；看板三卡+趋势图数据正确；`export` 带 feedback_type。

### P1：标注队列骨架（可先让标注员"看+判有效无效"）
- [ ] `feedback_annotation` 表 + 入队规则 + 列表/详情/judge API
- [ ] 前端标注页（队列 + 详情 + 有效/无效按钮）
- **DoD**：有评论差评自动入队；无效可移出；有效可进入编辑态。

### P2：gold 闭环（人工 SQL + bad_type + badcase）
- [ ] execute 端点（复用 dbmcp + classify_sql 护栏）
- [ ] confirm 端点（Dataset + badcase_status.annotate + 状态回写）
- [ ] `badcase_status` bad_type/gold_sql 扩展 + summary 按类型分布
- [ ] `run_experiment` exact-match + 按 bad_type 分组
- **DoD**：标注员走完「判断→改 SQL→执行→定类型→确定」全流程；badcase 带 bad_type 出现在回归集；回归报告按错误类型收敛。

---

## 6. 风险与注意

| 项 | 缓解 |
|---|---|
| 判定慢路径打 Langfuse | 只在聚合/入队时触发；LRU 缓存（key=`{tid}::{mid}`）；复用 `find_message_trace_id` 已有重试 |
| 存量 `feedback_type=''` | 聚合时惰性判定，不回写历史；`''` 不参与 query 统计（避免反向污染） |
| 标注执行安全 | `classify_sql` 只读硬校验 + 服务端默认 LIMIT + 后端鉴权（标注页限管理员） |
| v4 无 score 更新 | 类型只走 metadata + 本地库，不改 score value |
| 标注页并发 | 单条同一标注员操作；judge/confirm 用状态机校验（非当前状态拒绝，返回 409） |
| 前端 DLP 加密文件 | `lib/feedback.ts`/`types.ts` 等加密文件按既有通道改动；新页面/组件走非加密路径 |
| 枚举口径 | bad_type 第 5/6 类为补全提案，实施前与需求方确认（§4.7） |

---

## 7. 文件/改动清单

**后端（nl2sql 仓库）**：
| 文件 | 改动 |
|---|---|
| [src/agent/feedback/store.py](src/agent/feedback/store.py) | +`feedback_type` 列、+`feedback_annotation` 表、+入队/列表/聚合方法 |
| [src/api/message_feedback.py](src/api/message_feedback.py) | 入队触发、feedback_type 快路径、score metadata |
| [src/agent/trace/langfuse_v4_reads.py](src/agent/trace/langfuse_v4_reads.py) | +`classify_feedback_type`（快/慢路径） |
| [src/agent/eval/collect_badcase.py](src/agent/eval/collect_badcase.py) | 条件 3 只统计 query |
| [src/agent/eval/feedback_gate.py](src/agent/eval/feedback_gate.py) | 默认只统计 query（+`--include-chat`） |
| [src/agent/eval/badcase_status.py](src/agent/eval/badcase_status.py) | +`bad_type/gold_sql` 字段、+`annotate()`、summary 按类型 |
| [src/api/feedback_stats.py](src/api/feedback_stats.py) | **新增** stats 端点 |
| [src/api/feedback_annotation.py](src/api/feedback_annotation.py) | **新增** 5 个标注端点 |
| [src/api/custom_app.py](src/api/custom_app.py) | 注册上述 2 个模块 |
| [src/agent/eval/run_experiment.py](src/agent/eval/run_experiment.py) | P2：exact-match + bad_type 分组 |

**前端（harness-deep-agents-ui 仓库）**：
| 文件/路由 | 改动 |
|---|---|
| `/feedback` 反馈看板页 | **新增**（卡片+趋势+下钻） |
| `/feedback/annotate` 标注页 | **新增**（队列+详情+SQL 编辑执行+bad_type 下拉） |
| `src/lib/`（非加密）反馈 API 封装 | 新增 `feedbackStats`/`annotations` 请求 |
| 路由守卫 | 标注页限管理员 |
