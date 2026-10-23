# 定时任务「固化查询」实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让定时任务可以把「跑对过一次」的查询固化成一份参数化快照，之后每次触发直接按快照取数（跳过模型写 SQL），数据口径不再随模型重新规划而漂移。

**Architecture:** 固化 = 把物理 SQL / Cube 查询定义、参数模板、期望列、语义库版本存进 `scheduled_tasks.pin_spec`。触发时 `worker._fire` 先做预检：版本一致 → 服务端只读执行 → 合成一对 `AIMessage(tool_calls)` + `ToolMessage(check 结果)` 作为 `runs.create` 的入参，主 agent 只写图表和报告、不再委派 nl2sql；任何一环失败 ⇒ 任务标 `pin_status='invalid'`，**本次**回退到今天的正常 agent run，且不自动重新固化。

**Tech Stack:** Python 3.11 / FastAPI(Starlette Route) / SQLite(WAL) / langgraph-sdk / wren 语义层 / Next.js(React 19) 前端。

**Spec:** 本文档 §0（设计决策）。来源为用户 2026-10-23 口述需求：「先执行一遍查询，查询正确后，可以复用本次查询 sql 和口径等」。

## Global Constraints

- **只读**：固化执行沿用 `agent.middlewares.sql_approval.classify_sql`，`write` 一律拒绝（不是审批，是硬拒）。cube 通道只走 `query_cube` 的编译产物，不接受任意 SQL。
- **永不自动重新固化**：失效后只能由人点「重新固化」。回退 run 即便成功，也不恢复 `pin_status='active'`。
- **固化只影响「取数」，不影响「取到数之后」**：图表/报告仍由主 agent 正常生成，报告落盘路径、文件名契约、`report/` 归属全部不变。
- **`db_name` 变更即解绑**：`update_task` 改库时自动清 pin（`pin_invalid_reason='db_changed'`）；改 `prompt` / 调度表达式**不**解绑。
- **时间锚点一律按任务时区 `scheduled_tasks.tz` 计算**，不用服务器 UTC。
- **不动任何标注/反馈表**；不新建独立库，pin 全部落在既有 `scheduled_tasks` / `task_runs` 上。
- **前端 `src/lib/scheduledTasks.ts` 被 DLP 加密**（文件头 `Esafenet`），**禁止 Edit/Write**；新 API 封装写进**新建**的 `src/lib/scheduledPins.ts`。读该文件只能用 `git show HEAD:src/lib/scheduledTasks.ts`。
- **提示词改动不进 `MAIN_AGENT_PROMPT.md`**：该文件生效副本由 Langfuse 下发（memory: `live-prompt-version-authority`），改文件不生效。固化模式说明一律由 `main_agent.dynamic_prompt` 代码注入。
- 新增存储列必须**幂等迁移**（`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`），照 `src/agent/feedback/store.py:244-276` 的先例；`CREATE TABLE IF NOT EXISTS` 对老库不加列。
- 提交信息不带 `Co-Authored-By` / 🤖 署名。

## Review Focus

1. **跨月/跨年边界**：`month_start` 与 `today` 在每月 1 号取值相同 ⇒ 同一个日期字面量会同时匹配两个锚点。规则是**拒绝固化并报歧义**，不能靠"随便挑一个"通过（挑错的话下个月口径就错了）。
2. **语义库"版本相同"的判据**：必须比 `git rev-parse HEAD` 的 **commit sha**，不能比 tag/branch（tag 可以移动、branch 一直在动）。
3. **列比对过严导致误失效**：列名大小写、返回顺序由驱动决定，比对要**大小写不敏感 + 忽略顺序**；只看集合，不看顺序。
4. **截断行**：固化 SQL 结果超 `row_limit` 时不能判失效——按 `truncated: true` 回放，并在报告口径里保留截断说明（既有 `chart/report` 管线已能表达）。
5. **时区**：任务 `tz` 是 `Asia/Shanghai`，容器时钟是 UTC；锚点必须显式用 `zoneinfo.ZoneInfo(row["tz"])` 计算，否则每月 1 号 08:00 前的报表会取错区间。

---

## §0 设计决策

### 0.1 为什么不是「缓存上次 SQL 字符串直接重放」

物理 SQL 里的日期是**字面量**（`stat_date >= '2026-10-01'`）。原样重放第二个月还在查 10 月。所以快照必须是**模板 + 锚点**，触发时按当次时间渲染。这是本方案与「记住上次 SQL」的关键差别。

### 0.2 为什么在 `_fire` 里执行，而不是在 agent 图里

- `_fire` 已有完整的任务行（`db_name` / `tz` / `owner` / 权限复验都在手边），且是**唯一**的触发入口（`tick` 和 `run_now` 共用 `claim` + `submit_fire`）。
- 预检失败要在**起 run 之前**决定回退，图内做只能让 run 先跑起来再改，白烧一次全量 agent。
- 回退路径 = 今天的代码路径，零改动风险。

### 0.3 为什么要注入合成 `check_async_task` 结果

报告是主 agent 调 `build_report` 生成的，而 `_build_report_coro` 的数据源**只有**「最近一条 `status=success` 的 check ToolMessage」（`src/agent/tools/report_builder.py:637-671, 802-818`）。固化回放不跑子 agent，就不会有真的 check 结果 ⇒ **不注入就必然报「未找到可用的查询结果」**。

注入形如：

```python
input={"messages": [
    {"role": "user", "content": content},
    {"role": "assistant", "content": "",
     "tool_calls": [{"name": "check_async_task",
                     "args": {"task_id": PIN_TASK_ID},
                     "id": PIN_CALL_ID}]},
    {"role": "tool", "tool_call_id": PIN_CALL_ID,
     "name": "check_async_task", "content": <check 结果 JSON>},
]}
```

`_find_last_check_result` 按**内容签名**识别（具备 `status`/`thread_id`/`result` 三键且 `status=="success"`），不看工具名，所以能命中。`PIN_TASK_ID` 用 `pin-<task_id>` 这种不可能与真子任务撞车的值，并且 `check_async_task` 真被调用时查不到任务——这正是要靠提示词 + 工具裁剪堵住的行为（§0.4）。

### 0.4 怎么保证回放时模型不偷偷重查

两层，缺一不可：

1. **提示词**：`dynamic_prompt` 检测 `configurable.pinned_replay`，在最前面插「固化回放模式」段，声明唯一数据源与禁令。放在代码里而不是 `MAIN_AGENT_PROMPT.md`，是因为线上提示词由 Langfuse 下发（见 Global Constraints）。
2. **工具面**：`PinnedReplayMiddleware` 在同一个 configurable 为真时，从模型可见工具里摘掉 `start_async_task`。这是硬闸——提示词是软的，模型可能忽略。

子 agent 侧的 `ToolFilterMiddleware`（`src/agent/middlewares/tool_filter.py:199-215`）**不需要改**：回放根本不启子 agent。

### 0.5 通道选择

`check` 结果里 `sql_kind == "cube"` ⇒ cube 通道，固化 `cube_query`（5 个编译键）；否则 sql 通道，固化**物理 SQL**（`check["dialect_sql"]`，没有则现场 `plan_run_sql` 展开一次）。理由见 memory：报告存的是**真正下发给物理库的那条**，不是语义层那条。

---

## §A 表结构

`src/agent/scheduler/store.py` 的 `_SCHEMA`（新库）与 `_migrate_schema()`（老库）都要加：

```sql
-- scheduled_tasks（6 列）
pin_status            TEXT NOT NULL DEFAULT ''      -- '' 未固化 | 'active' | 'invalid'
pin_spec              TEXT NOT NULL DEFAULT ''      -- §B 的 JSON 原文；'' = 无
pin_semantic_version  TEXT NOT NULL DEFAULT ''      -- git commit sha；直连库为 ''
pin_saved_at          TEXT NOT NULL DEFAULT ''      -- UTC ISO
pin_invalid_reason    TEXT NOT NULL DEFAULT ''      -- 失效原因码，见 §C
pin_source_thread_id  TEXT NOT NULL DEFAULT ''      -- 从哪个会话固化来的（可追溯）

-- task_runs（2 列）
pin_mode              TEXT NOT NULL DEFAULT 'normal' -- 'normal' | 'replay' | 'fallback'
pin_reason            TEXT NOT NULL DEFAULT ''       -- 本次回放失败原因（mode=fallback 时）
```

`_COLUMNS` 追加以上全部 8 个列名（`insert_task` 只认白名单）；`insert_task` 的默认值字典追加：

```python
"pin_status": "", "pin_spec": "", "pin_semantic_version": "",
"pin_saved_at": "", "pin_invalid_reason": "", "pin_source_thread_id": "",
```

`_UPDATABLE` **追加排除**这 6 个 pin 列（pin 只能走 `set_pin` / `clear_pin`，避免被通用 PATCH 写坏）：

```python
_UPDATABLE = frozenset(_COLUMNS) - {
    "id", "owner", "created_at",
    "next_run_at", "running_thread_id", "repeat_completed",
    "last_status", "last_run_at",
    "pin_status", "pin_spec", "pin_semantic_version",
    "pin_saved_at", "pin_invalid_reason", "pin_source_thread_id",
}
```

`task_runs` 的新列由 `record_run` 写入，不参与 `_COLUMNS`（那是 `scheduled_tasks` 的）。

**不变量**（由 `set_pin` 保证，测试锁定）：

- `pin_status` 非空 ⇒ `pin_spec` 非空。
- `pin_status == 'invalid'` ⇒ `pin_invalid_reason` 非空。
- `pin_status == ''` ⇒ 其余 5 个 pin 列全为 `''`。

---

## §B 参数模板格式

### B.1 占位符语法

`{{p:<anchor>}}`，纯文本替换。SQL 里引号由模板自己带：

```sql
SELECT dept, SUM(hours) FROM t WHERE stat_date BETWEEN '{{p:month_start}}' AND '{{p:yesterday}}'
```

cube 通道同理，占位符出现在 `filters` 的 value 段和 `time_dimension` 的起止段里：

```json
{"filters": ["workhour.stat_date:gte:{{p:month_start}}"],
 "time_dimension": "workhour.stat_date:day:{{p:month_start}},{{p:tomorrow}}"}
```

`time_dimension` 的 end 是**开区间**（引擎语义），所以"含今天"要写 `{{p:tomorrow}}`——这一点必须在固化预览里原样显示，不能让用户以为是闭区间。

### B.2 锚点集合（闭集，按任务 `tz` 计算）

| anchor | 取值 |
|---|---|
| `today` | 今天 |
| `yesterday` | 昨天 |
| `tomorrow` | 明天 |
| `month_start` | 本月 1 日 |
| `last_month_start` | 上月 1 日 |
| `last_month_end` | 上月最后一天 |
| `year_start` | 本年 1 月 1 日 |
| `week_start` | 本周一 |

渲染格式固定 `%Y-%m-%d`。**时间部分不进锚点**：`'2026-10-23 00:00:00'` 只把日期段模板化 ⇒ `'{{p:today}} 00:00:00'`。

不做「近 7 天」「上季度」这类区间锚点（YAGNI；真需要时用户可以用两个锚点拼）。

### B.3 固化 spec JSON（`pin_spec` 列的完整形状）

sql 通道：

```json
{
  "v": 1,
  "channel": "sql",
  "db_name": "witops",
  "tz": "Asia/Shanghai",
  "semantic_version": "b5208d7c1e...",
  "source_thread_id": "01a1d8c2-...",
  "captured_at": "2026-10-23T01:30:12+00:00",
  "sql_template": "SELECT dept, SUM(hours) FROM ... WHERE stat_date BETWEEN '{{p:month_start}}' AND '{{p:yesterday}}'",
  "params": ["month_start", "yesterday"],
  "expect_columns": ["dept", "sum_hours"],
  "row_limit": 5000,
  "warnings": []
}
```

cube 通道：把 `sql_template` 换成 `cube_template`（只允许 5 个编译键）：

```json
{
  "v": 1, "channel": "cube", "db_name": "witops", "tz": "Asia/Shanghai",
  "semantic_version": "b5208d7c1e...", "source_thread_id": "01a1d8c2-...",
  "captured_at": "2026-10-23T01:30:12+00:00",
  "cube_template": {
    "cube": "workhour",
    "measures": ["workhour.total_hours"],
    "dimensions": ["dept.name"],
    "filters": ["workhour.stat_date:gte:{{p:month_start}}"],
    "time_dimension": "workhour.stat_date:day:{{p:month_start}},{{p:tomorrow}}"
  },
  "params": ["month_start", "tomorrow"],
  "expect_columns": ["dept_name", "total_hours"],
  "row_limit": 5000,
  "warnings": []
}
```

字段约束：

- `v` 必须 `== 1`；未知 `v` ⇒ 失效码 `spec_version_unknown`（未来的兼容闸）。
- `params` 必须等于模板里出现过的占位符集合（顺序按首次出现），用来做**渲染期兜底**：模板里有、`params` 里没有 ⇒ `render_failed`（防止手工改坏 spec）。
- `expect_columns` 来自**固化时的验证执行**（不是 check 结果里的文字摘要——那是 Markdown）。
- `row_limit` 固化时定值，触发时取 `min(spec.row_limit, env NL2SQL_SCHED_PIN_ROW_LIMIT)`。
- `warnings` 记录"字面量未匹配任何锚点、按常量保留"的清单，前端展示，不阻断。

### B.4 字面量 → 模板的判定规则（固化时）

扫描 `sql_template` 源文本（或 cube 的 `filters`/`time_dimension` 字符串）里所有 `\d{4}-\d{2}-\d{2}`：

| 该字面量在锚点表中命中 | 动作 |
|---|---|
| 恰好 1 个锚点 | 替换成 `{{p:<anchor>}}` |
| 0 个锚点 | **默认拒绝**；调用方传 `accept_constants=true` 时原样保留并写 `warnings` |
| ≥ 2 个锚点 | **拒绝**，返回歧义清单 + `date_bindings` 用法 |

`date_bindings` 是唯一的人为消歧通道：`{"2026-10-01": "month_start"}`。服务端校验"绑定锚点在固化当天的取值 == 该字面量"，不等则 400。

---

## §C 失效与回退语义

### C.1 触发时序（`worker._fire`）

```
recheck_db_access 失败 ──► 既有 _fail_fire（不动 pin）
       │
pin_status != 'active' ──► 正常 agent run（pin_mode='normal'）
       │
pin_status == 'active'
       ├─ 版本闸：semantic_version 非空且 != 当前 commit ──► 标 invalid，回退
       ├─ 渲染 ──► 失败 ⇒ invalid(render_failed)，回退
       ├─ 执行 ──► 失败 ⇒ invalid(<对应码>)，回退
       ├─ 列比对 ──► 不匹配 ⇒ invalid(columns_changed)，回退
       └─ 全部通过 ──► 起回放 run（pin_mode='replay'）
```

`run_now` 与定时触发共用 `claim` + `submit_fire` ⇒ **手动"立即运行"同样走回放**。这是有意的：否则用户点"立即运行"看到的结果和定时跑出来的不一样，更难排查。

### C.2 失效码（`pin_invalid_reason`，码 + 冒号 + 细节）

| 码 | 触发条件 | 细节内容 |
|---|---|---|
| `semantic_version_changed` | 语义库 commit 变了 | `<旧 sha7> -> <新 sha7>` |
| `spec_version_unknown` | `spec.v` 不是 1 | 值原文 |
| `render_failed` | 未知占位符 / `params` 对不上 | 占位符名 |
| `cube_compile_failed` | `plan_cube_sql_checked` 抛 `CubePlanError` | 引擎原话 |
| `not_semantic_db` | cube 通道但该库已无 wren 项目 | db_name |
| `sql_failed` | 只读执行抛错，或 `classify_sql` 判 write | 驱动错误前 200 字 |
| `exec_timeout` | 超过 `NL2SQL_SCHED_PIN_TIMEOUT_S`（默认 120） | 秒数 |
| `columns_changed` | 列集合不等 | `missing=[..] extra=[..]` |
| `db_changed` | `update_task` 改了 `db_name` | `<旧> -> <新>` |

### C.3 回退语义

1. **只回退这一次**：`pin_status` 置 `invalid` 并**持久失效**；下一次触发继续走正常 agent run，不会每轮重试固化（也就不会每轮多花一次执行）。
2. **不自动重新固化**：回退 run 产出的新 SQL 只是普通运行结果，不会写回 `pin_spec`。
3. **可见**：`task_runs` 写 `pin_mode='fallback'` + `pin_reason=<码>`；任务卡片显示「固化已失效：<中文原因>」徽标 + 「重新固化」按钮。
4. **不吞错误**：失效只影响取数路径，本次 run 的正常/失败仍由 `_reconcile_task` 按 run 终态收口——回退成功 = `last_status=success`，固化失效这件事单独在 `pin_status` / `task_runs.pin_reason` 里表达，两者不互相覆盖。
5. **固化期间的其它操作**：`pause` / `resume` / `delete` 不动 pin；`delete` 连带丢弃（行没了）；`update_task` 改 `db_name` ⇒ 调 `clear_pin`（不是 invalid，因为 spec 里的 SQL 指向另一个库，留着没有意义），`pin_invalid_reason='db_changed'`。**注意**：`clear_pin` 要保留 `pin_invalid_reason` 供 UI 提示，因此它把 `pin_status`、`pin_spec`、`pin_semantic_version`、`pin_saved_at`、`pin_source_thread_id` 清空，只留 reason。

---

## §D 测试点总表

> 仓库 `pyproject.toml:35` 设了 `asyncio_mode = "auto"`：异步测试直接写 `async def test_...`，**不要**加 `@pytest.mark.asyncio`。跑测统一定式 `uv run --no-sync python -m pytest <file> -q`。

单元（纯函数，`src/test/unit/test_scheduler_query_spec.py`）：

| # | 测点 |
|---|---|
| T1 | 锚点在跨月、跨年、闰年 2 月末的取值正确 |
| T2 | 锚点按 `tz='Asia/Shanghai'` 算，服务器 UTC 03:00（= 北京 11:00）时 `today` 仍是北京日期 |
| T3 | 字面量命中 1 锚点 ⇒ 模板化；同一字面量出现多次 ⇒ 全部替换 |
| T4 | 字面量命中 0 锚点 ⇒ 抛歧义/未匹配；`accept_constants=True` ⇒ 保留 + warning |
| T5 | 字面量命中 2 锚点（每月 1 日）⇒ 抛 `SpecError('ambiguous')`，带候选 |
| T6 | `date_bindings` 消歧成功；绑定值当天不等 ⇒ 抛错 |
| T7 | `'2026-10-23 00:00:00'` ⇒ `'{{p:today}} 00:00:00'`（只模板化日期段） |
| T8 | `render_spec` 渲染结果与固化当天原值逐字相等（roundtrip） |
| T9 | sql 通道：`check.dialect_sql` 优先；无则取 `sql` |

存储（`src/test/unit/test_scheduler_pin_store.py`）：

| # | 测点 |
|---|---|
| T10 | 新库建表后 6 个 pin 列存在 |
| T11 | 老库（手工建无 pin 列的 `scheduled_tasks`）启动后迁移补列，且已有行取默认值 |
| T12 | `set_pin` / `clear_pin` / `invalidate_pin` 三条不变量 |
| T13 | `update_task` 不能写 pin 列（被 `_UPDATABLE` 拒绝） |
| T14 | `record_run(pin_mode=...)` 落库，`list_runs` 读得回 |

回放（`src/test/unit/test_scheduler_pin_replay.py`）：

| # | 测点 |
|---|---|
| T15 | 回放输入三条消息成对：`tool_calls[0].id == tool_call_id`，且 `role=tool` 内容能被 `_find_last_check_result(msgs, "")` 命中 |
| T16 | cube 通道的 check 载荷带 `sql_kind='cube'` / `cube_query` / `cube_args` |
| T17 | 版本闸：commit 不同 ⇒ `pin_status='invalid'` 且本次走正常 run（`pin_mode='fallback'`） |
| T18 | 执行失败 ⇒ invalid + fallback，且**不**影响 `last_status` 的收口路径 |
| T19 | 列不匹配：大小写差异、顺序不同 **不** 失效；少列/多列 失效 |
| T20 | `truncated=True` 仍然 success（不失效），check 载荷里 `truncated` 可见 |
| T21 | `pinned_replay=True` 写进 run 的 configurable |

主 agent 模式（`src/test/unit/test_pinned_replay_mode.py`）：

| # | 测点 |
|---|---|
| T22 | `PinnedReplayMiddleware` 在 `pinned_replay=True` 时摘掉 `start_async_task`，其余工具原样保留 |
| T23 | `pinned_replay` 缺失/False ⇒ 工具面完全不变（不创建新 request） |
| T24 | `dynamic_prompt` 在回放模式注入段落，正常模式不注入 |

API（`src/test/unit/test_scheduler_pin_api.py`）：

| # | 测点 |
|---|---|
| T25 | `POST /pin` 成功返回 spec 摘要 + 渲染后预览 SQL |
| T26 | 非 owner ⇒ 404（不是 403，沿用既有 owner 隔离口径） |
| T27 | 固化失败（歧义/执行失败）⇒ 400 + 中文原因，且**不**写库 |
| T28 | `DELETE /pin` 清空 5 列、保留 reason |
| T29 | `semantic_version_of` 取 commit，不取 tag/branch；直连库返回 `""` |
| T30 | `GET /pin` 未固化时 `pin` 为 `null` |

前端：`npx tsc --noEmit` 无新增错误（仓库既有 37 个错误为基线）、`eslint` 改动文件干净、手工验证徽标与按钮。

---

## 文件结构

**新建**

| 文件 | 职责 |
|---|---|
| `src/agent/scheduler/query_spec.py` | 纯函数：锚点、模板化、spec 提取、渲染、列比对、check 载荷构造 |
| `src/agent/scheduler/pin_exec.py` | 只读执行渲染后的 spec（sql / cube） |
| `src/agent/scheduler/pin_service.py` | 异步编排：从会话读结果 → 固化 → 验证 → 落库；失效/解绑 |
| `src/agent/middlewares/pinned_replay.py` | 主 agent 工具面裁剪 |
| 前端 `src/lib/scheduledPins.ts` | pin API 封装（**新建**，绕开 DLP 加密的 `scheduledTasks.ts`） |
| `src/test/unit/test_scheduler_query_spec.py` 等 5 个测试文件 | §D |

**修改**

| 文件 | 改动 |
|---|---|
| `src/agent/scheduler/store.py` | `_SCHEMA` + `_migrate_schema` + `_COLUMNS` + 默认值 + `_UPDATABLE` + `set_pin/clear_pin/invalidate_pin` + `record_run(pin_mode)` |
| `src/api/scheduled_tasks.py` | 3 条 pin 路由 |
| `src/agent/scheduler/worker.py` | `_fire` 分叉 + `_build_replay_input` |
| `src/agent/main_agent.py` | `dynamic_prompt` 注入段；`PinnedReplayMiddleware` 入链 |
| `src/agent/tools/manage_scheduled_task.py` | `_brief` 透出 `pin_status` / `pin_invalid_reason`（只读展示，**不加** pin 动作） |
| 前端 `src/app/components/ScheduledTasksPanel.tsx` | 徽标 + 固化/取消固化按钮 + 重新固化 |

---

### Task 1: 锚点与模板化（纯函数）

**Files:**
- Create: `src/agent/scheduler/query_spec.py`
- Test: `src/test/unit/test_scheduler_query_spec.py`

**Interfaces:**
- Produces:
  - `class SpecError(Exception)` —— 带属性 `code: str`、`detail: str`
  - `ANCHOR_KINDS: tuple[str, ...]` = `("today","yesterday","tomorrow","month_start","last_month_start","last_month_end","year_start","week_start")`
  - `resolve_anchors(now: datetime, tz: str) -> dict[str, str]`（值形如 `"2026-10-23"`）
  - `templatize(text: str, anchors: dict[str, str], bindings: dict[str, str] | None = None, accept_constants: bool = False) -> tuple[str, list[str], list[str]]` → `(模板文本, 用到的锚点列表, 常量告警列表)`
  - `render_text(template: str, anchors: dict[str, str]) -> str`
  - `DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")`

- [ ] **Step 1: 写失败测试**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler.query_spec import SpecError, render_text, resolve_anchors, templatize

TZ = "Asia/Shanghai"


def _anchors(y=2026, m=10, d=23):
    return resolve_anchors(datetime(y, m, d, 11, 0, tzinfo=ZoneInfo(TZ)), TZ)


def test_anchors_basic():
    a = _anchors()
    assert a["today"] == "2026-10-23"
    assert a["yesterday"] == "2026-10-22"
    assert a["tomorrow"] == "2026-10-24"
    assert a["month_start"] == "2026-10-01"
    assert a["last_month_start"] == "2026-09-01"
    assert a["last_month_end"] == "2026-09-30"
    assert a["year_start"] == "2026-01-01"
    assert a["week_start"] == "2026-10-19"   # 2026-10-23 是周五


def test_anchors_cross_year_and_leap():
    assert _anchors(2026, 1, 5)["last_month_start"] == "2025-12-01"
    assert _anchors(2026, 1, 5)["last_month_end"] == "2025-12-31"
    assert _anchors(2028, 3, 3)["last_month_end"] == "2028-02-29"


def test_templatize_single_match():
    out, used, warns = templatize(
        "SELECT 1 WHERE dt >= '2026-10-01' AND dt <= '2026-10-22'", _anchors()
    )
    assert out == "SELECT 1 WHERE dt >= '{{p:month_start}}' AND dt <= '{{p:yesterday}}'"
    assert used == ["month_start", "yesterday"]
    assert warns == []


def test_templatize_keeps_time_part():
    out, used, _ = templatize("dt > '2026-10-23 00:00:00'", _anchors())
    assert out == "dt > '{{p:today}} 00:00:00'"
    assert used == ["today"]


def test_templatize_ambiguous_on_first_of_month():
    # 2026-11-01 同时是 today 与 month_start
    a = resolve_anchors(datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo(TZ)), TZ)
    with pytest.raises(SpecError) as e:
        templatize("dt = '2026-11-01'", a)
    assert e.value.code == "ambiguous_date"
    assert "month_start" in e.value.detail and "today" in e.value.detail


def test_templatize_binding_resolves_ambiguity():
    a = resolve_anchors(datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo(TZ)), TZ)
    out, used, _ = templatize("dt >= '2026-11-01'", a, bindings={"2026-11-01": "month_start"})
    assert out == "dt >= '{{p:month_start}}'"
    assert used == ["month_start"]


def test_binding_must_match_that_day():
    a = _anchors()
    with pytest.raises(SpecError) as e:
        templatize("dt = '2026-10-09'", a, bindings={"2026-10-09": "month_start"})
    assert e.value.code == "bad_binding"


def test_zero_match_needs_accept_constants():
    with pytest.raises(SpecError) as e:
        templatize("dt >= '1990-01-01'", _anchors())
    assert e.value.code == "unmatched_date"


def test_zero_match_kept_as_constant_when_accepted():
    out, used, warns = templatize("dt >= '1990-01-01'", _anchors(), accept_constants=True)
    assert out == "dt >= '1990-01-01'"
    assert used == []
    assert warns == ["常量日期保留: 1990-01-01"]


def test_render_roundtrip():
    a = _anchors()
    out, _, _ = templatize("dt = '2026-10-01'", a)
    assert render_text(out, a) == "dt = '2026-10-01'"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_query_spec.py -q`
Expected: FAIL（`ModuleNotFoundError: agent.scheduler.query_spec`）

- [ ] **Step 3: 实现 `query_spec.py` 的锚点与模板化部分**

要点：
- `resolve_anchors` 用 `now.astimezone(ZoneInfo(tz)).date()` 起算，全部用 `datetime.date` 运算，避免 DST 陷阱。
- `templatize(text, anchors, bindings=None, accept_constants=False)`：对 `DATE_RE` 的**每个不同字面量**先算候选 `[k for k, v in anchors.items() if v == literal]`；
  - `len==1` → 记录 `literal -> anchor`；
  - `len==0` 且 `accept_constants` 为假 → `SpecError("unmatched_date", literal)`；为真 → warning `f"常量日期保留: {literal}"`；
  - `len>1` 且该字面量在 `bindings` 里 → 校验 `anchors[bindings[lit]] == lit`，不等抛 `SpecError("bad_binding", ...)`；不在 `bindings` → `SpecError("ambiguous_date", f"{lit}: {candidates}")`。
- 替换用 `DATE_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)` **一次完成**（不要边扫边替换，否则二次替换会污染）。
- `used` 按首次出现顺序去重返回；`warns` 返回常量清单。
- `accept_constants` 是 `templatize` 的形参（默认 `False`），`True` 时零命中字面量原样保留并进 `warns`，不抛错。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_query_spec.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent/scheduler/query_spec.py src/test/unit/test_scheduler_query_spec.py
git commit -m "feat(scheduler): 固化查询的日期锚点与模板化"
```

---

### Task 2: spec 提取、渲染与列比对（纯函数）

**Files:**
- Modify: `src/agent/scheduler/query_spec.py`
- Test: `src/test/unit/test_scheduler_query_spec.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `resolve_anchors` / `templatize` / `render_text` / `SpecError`
- Produces:
  - `SPEC_VERSION = 1`、`CUBE_KEYS = ("cube","measures","dimensions","filters","time_dimension")`
  - `extract_spec(check: dict, *, db_name: str, tz: str, semantic_version: str, source_thread_id: str, now: datetime, date_bindings: dict[str,str] | None = None, accept_constants: bool = False) -> tuple[dict, list[str]]`
  - `render_spec(spec: dict, now: datetime) -> dict` → `{"channel","sql","cube","params"}`；`sql` 为 `None` 时取 `cube`
  - `validate_columns(spec: dict, columns: list[str]) -> tuple[bool, str]` → `(ok, detail)`，detail 形如 `missing=[a] extra=[]`
  - `build_check_payload(spec: dict, *, thread_id: str, columns: list[str], rows: list, sql: str, cube_query: str, cube_args: dict, cube_tool: str, row_count: int, truncated: bool) -> dict`

- [ ] **Step 1: 写失败测试**

追加进同一个测试文件（沿用 Task 1 的 `datetime` / `ZoneInfo` / `TZ` 导入）：

```python
import pytest

from agent.scheduler.query_spec import (
    SpecError, build_check_payload, extract_spec, render_spec, validate_columns,
)


@pytest.fixture
def now():
    """固化参考时刻：2026-10-23 11:00 北京（UTC 03:00，见 Review Focus #5）。"""
    return datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo(TZ))


def _check_sql():
    return {
        "status": "success", "thread_id": "t-sub", "result": "| a | b |\n|---|---|\n| 1 | 2 |",
        "sql": "SELECT dept, SUM(hours) FROM t WHERE dt >= '2026-10-01'",
        "dialect_sql": "SELECT `dept`, SUM(`hours`) FROM `t` WHERE `dt` >= '2026-10-01'",
    }


def test_extract_sql_prefers_dialect_sql(now):
    spec, warns = extract_spec(
        _check_sql(), db_name="witops", tz="Asia/Shanghai",
        semantic_version="abc123", source_thread_id="t-main", now=now,
    )
    assert spec["channel"] == "sql"
    assert spec["sql_template"].startswith("SELECT `dept`")
    assert "{{p:month_start}}" in spec["sql_template"]
    assert spec["params"] == ["month_start"]
    assert spec["v"] == 1 and spec["semantic_version"] == "abc123"
    assert warns == []


def test_extract_cube(now):
    check = {
        "status": "success", "thread_id": "t-sub", "result": "x",
        "sql_kind": "cube",
        "cube_query": "x > '2026-10-01'",
        "cube_args": {"cube": "workhour", "measures": ["workhour.total_hours"],
                      "dimensions": ["dept.name"],
                      "filters": ["workhour.dt:gte:2026-10-01"],
                      "time_dimension": "workhour.dt:day:2026-10-01,2026-10-24"},
        "cube_tool": "query_cube",
    }
    spec, _ = extract_spec(check, db_name="witops", tz="Asia/Shanghai",
                           semantic_version="", source_thread_id="t", now=now)
    assert spec["channel"] == "cube"
    assert spec["cube_template"]["filters"] == ["workhour.dt:gte:{{p:month_start}}"]
    assert spec["cube_template"]["time_dimension"] == "workhour.dt:day:{{p:month_start}},{{p:tomorrow}}"
    assert set(spec["cube_template"]) <= {"cube", "measures", "dimensions", "filters", "time_dimension"}


def test_extract_refuses_without_sql_or_cube(now):
    with pytest.raises(SpecError) as e:
        extract_spec({"status": "success", "thread_id": "t", "result": "x"},
                     db_name="d", tz="Asia/Shanghai", semantic_version="",
                     source_thread_id="t", now=now)
    assert e.value.code == "no_query"


def test_extract_strips_cube_window_keys(now):
    check = {"status": "success", "thread_id": "t", "result": "x", "sql_kind": "cube",
             "cube_args": {"cube": "c", "measures": ["c.m"], "limit": 100, "offset": 0,
                           "granularity": "day"}}
    with pytest.raises(SpecError) as e:
        extract_spec(check, db_name="d", tz="Asia/Shanghai", semantic_version="",
                     source_thread_id="t", now=now)
    assert e.value.code == "cube_key_unsupported"


def test_render_moves_with_time(now):
    spec, _ = extract_spec(_check_sql(), db_name="d", tz="Asia/Shanghai",
                           semantic_version="", source_thread_id="t", now=now)
    later = now.replace(year=2026, month=11, day=15)
    out = render_spec(spec, later)
    assert "'2026-11-01'" in out["sql"]
    assert out["params"] == {"month_start": "2026-11-01"}


def test_validate_columns_case_and_order_insensitive():
    spec = {"expect_columns": ["dept", "Sum_Hours"]}
    assert validate_columns(spec, ["SUM_HOURS", "DEPT"])[0] is True


def test_validate_columns_detects_drift():
    spec = {"expect_columns": ["dept", "hours"]}
    ok, detail = validate_columns(spec, ["dept", "cost"])
    assert ok is False
    assert "missing=[hours]" in detail and "extra=[cost]" in detail


def test_check_payload_has_signature_keys():
    spec = {"channel": "sql", "sql_template": "SELECT 1", "expect_columns": ["a"]}
    p = build_check_payload(spec, thread_id="t-new", columns=["a"], rows=[(1,)],
                            sql="SELECT 1", cube_query="", cube_args={},
                            cube_tool="", row_count=1, truncated=False)
    assert {"status", "thread_id", "result", "sql"} <= p.keys()
    assert p["status"] == "success" and p["thread_id"] == "t-new"
    assert "固化回放" in p["result"] and "| a |" in p["result"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_query_spec.py -q -k "extract or render or validate or payload"`
Expected: FAIL（`ImportError: cannot import name 'extract_spec'`）

- [ ] **Step 3: 实现**

- `extract_spec`：`sql_kind == "cube"` 分支取 `check["cube_args"]`，非法键（不在 `CUBE_KEYS` 之内，含 `limit/offset/granularity/segments/order_by`）抛 `SpecError("cube_key_unsupported", ...)`——**不要**用 `_parse_cube_spec` 的宽容集合，固化必须严；对 `filters` 列表与 `time_dimension` 各自调 `templatize`。sql 分支取 `dialect_sql or sql`。
- `render_spec`：`spec["v"] != 1` 抛 `SpecError("spec_version_unknown")`；先校验模板里的占位符集合 ⊆ `spec["params"]`，否则 `SpecError("render_failed")`；再 `render_text` / 默认值替换（cube 用 dict 拷贝后逐字段渲染）。
- `validate_columns`：`{c.lower() for c in columns} == {c.lower() for c in expect}`。
- `build_check_payload`：`result` 用 Markdown 表格（`| col | ... |`）+ 首行 `> 固化回放：按已固化查询取数，共 N 行{truncated ? "（已截断）" : ""}`；`sql_kind` 仅在 cube 时给 `"cube"`；同时带 `pinned_replay: True`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_query_spec.py -q`
Expected: PASS（全文件）

- [ ] **Step 5: 提交**

```bash
git add src/agent/scheduler/query_spec.py src/test/unit/test_scheduler_query_spec.py
git commit -m "feat(scheduler): 固化 spec 的提取、渲染与列漂移检测"
```

---

### Task 3: 只读执行器

**Files:**
- Create: `src/agent/scheduler/pin_exec.py`
- Test: `src/test/unit/test_scheduler_pin_exec.py`

**Interfaces:**
- Consumes: Task 2 的 `render_spec`；既有 `agent.middlewares.sql_approval.classify_sql`、`agent.utils.wren_call_extract.resolve_wren_ctx_by_db`、`agent.utils.wren_plan.plan_cube_sql_checked`
- Produces:
  - `class PinExecError(Exception)` —— `__init__(self, code: str, detail: str = "")`，属性 `code` / `detail`（取值见 §C.2）
  - `@dataclass PinResult`: `channel, sql, cube_query, cube_args, cube_tool, columns, rows, row_count, truncated, params`
  - `async def run_pinned_spec(spec: dict, *, now: datetime, limit: int | None = None, timeout_s: int = 120) -> PinResult`

- [ ] **Step 1: 写失败测试**

```python
import pytest

from agent.scheduler import pin_exec


async def test_sql_channel_executes_rendered_sql(monkeypatch, now):
    called = {}

    async def fake_run(db_name, sql, limit=1000):
        called.update(db_name=db_name, sql=sql, limit=limit)
        return {"columns": ["a"], "rows": [[1]], "truncated": False}

    monkeypatch.setattr(pin_exec, "run_readonly_sql", fake_run)
    spec = {"v": 1, "channel": "sql", "db_name": "d", "tz": "Asia/Shanghai",
            "sql_template": "SELECT 1 WHERE dt >= '{{p:yesterday}}'",
            "params": ["yesterday"], "expect_columns": ["a"], "row_limit": 5000}
    res = await pin_exec.run_pinned_spec(spec, now=now)
    assert res.channel == "sql"
    assert "2026-10-22" in called["sql"]
    assert res.sql == called["sql"]
    assert res.columns == ["a"] and res.row_count == 1


async def test_cube_channel_compiles_then_executes(monkeypatch, now):
    monkeypatch.setattr(pin_exec, "resolve_wren_ctx_by_db", lambda db: ("/p", object()))
    monkeypatch.setattr(pin_exec, "plan_cube_sql_checked",
                        lambda p, c, a: {"dialect_sql": "SELECT '2026-10-01'", "cube_sql": "x"})

    async def fake_run(db_name, sql, limit=1000):
        return {"columns": ["m"], "rows": [[7]], "truncated": True}

    monkeypatch.setattr(pin_exec, "run_readonly_sql", fake_run)
    spec = {"v": 1, "channel": "cube", "db_name": "d", "tz": "Asia/Shanghai",
            "cube_template": {"cube": "c", "measures": ["c.m"],
                              "filters": ["c.dt:gte:{{p:month_start}}"]},
            "params": ["month_start"], "expect_columns": ["m"], "row_limit": 5000}
    res = await pin_exec.run_pinned_spec(spec, now=now)
    assert res.sql == "SELECT '2026-10-01'"
    assert res.truncated is True
    assert res.cube_args["filters"] == ["c.dt:gte:2026-10-01"]


async def test_not_semantic_db(monkeypatch, now):
    monkeypatch.setattr(pin_exec, "resolve_wren_ctx_by_db", lambda db: (None, None))
    spec = {"v": 1, "channel": "cube", "db_name": "d", "tz": "Asia/Shanghai",
            "cube_template": {"cube": "c", "measures": ["c.m"]},
            "params": [], "expect_columns": [], "row_limit": 5000}
    with pytest.raises(pin_exec.PinExecError) as e:
        await pin_exec.run_pinned_spec(spec, now=now)
    assert e.value.code == "not_semantic_db"


async def test_write_sql_rejected(monkeypatch, now):
    async def fake_run(db_name, sql, limit=1000):
        raise ValueError("仅允许只读查询（检测到 DELETE）")

    monkeypatch.setattr(pin_exec, "run_readonly_sql", fake_run)
    spec = {"v": 1, "channel": "sql", "db_name": "d", "tz": "Asia/Shanghai",
            "sql_template": "DELETE FROM t", "params": [],
            "expect_columns": [], "row_limit": 5000}
    with pytest.raises(pin_exec.PinExecError) as e:
        await pin_exec.run_pinned_spec(spec, now=now)
    assert e.value.code == "sql_failed"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_exec.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `run_readonly_sql(db_name, sql, limit) -> dict` 照抄 `src/api/feedback_annotation.py:242-281` `_run_preview` 的形状（`classify_sql` 只读闸 → `McpSqlConfig.from_env` → `asyncio.to_thread` 建 runner → `split_sql_statements` + `_apply_default_limit` → `combine_multi_results`）。**不要 import `_run_preview`**：私有下划线 API、且它不回传实际执行的语句。返回 `{"columns","rows","truncated","executed_sql"}`。
- `run_pinned_spec` 用 `asyncio.wait_for(..., timeout=timeout_s)` 包住执行；`TimeoutError` → `PinExecError("exec_timeout")`。
- cube 链路：`resolve_wren_ctx_by_db(db)` → 空则 `not_semantic_db`；`plan_cube_sql_checked` 的 `CubePlanError` → `cube_compile_failed`（detail 截 200）；拿 `dialect_sql` 走 `run_readonly_sql`。执行前对该 SQL 也过一次 `classify_sql`（引擎不该产出写语句，但闸门不嫌多）。
- `sql` 字段回填**实际执行的那条**（含 `_apply_default_limit` 追加的 limit），这样报告「执行 SQL」节与真实下发逐字一致。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_exec.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent/scheduler/pin_exec.py src/test/unit/test_scheduler_pin_exec.py
git commit -m "feat(scheduler): 固化查询的只读执行器"
```

---

### Task 4: 存储层（建列 + 幂等迁移 + pin 写方法）

**Files:**
- Modify: `src/agent/scheduler/store.py:33-96`（`_SCHEMA` / `_COLUMNS` / `_UPDATABLE`）、`:110-150`（`__init__` / `insert_task`）、`:319-346`（`record_run`）
- Test: `src/test/unit/test_scheduler_pin_store.py`

**Interfaces:**
- Produces:
  - `ScheduledTaskStore._migrate_schema(self) -> None`
  - `set_pin(self, task_id: str, *, spec_json: str, semantic_version: str, source_thread_id: str, now_iso: str) -> bool`
  - `invalidate_pin(self, task_id: str, reason: str, now_iso: str) -> bool`
  - `clear_pin(self, task_id: str, reason: str, now_iso: str) -> bool`
  - `record_run(self, task_id, thread_id, run_id, fired_at, pin_mode: str = "normal", pin_reason: str = "") -> int`（向后兼容旧四参调用）

- [ ] **Step 1: 写失败测试**

```python
import sqlite3

from agent.scheduler.store import ScheduledTaskStore


def _legacy_db(path):
    """模拟线上老库：只有 2026-10 之前的列。"""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE scheduled_tasks (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
        prompt TEXT NOT NULL, db_name TEXT NOT NULL DEFAULT '',
        sched_kind TEXT NOT NULL, sched_expr TEXT NOT NULL,
        next_run_at TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    c.execute("INSERT INTO scheduled_tasks (id,owner,name,prompt,sched_kind,sched_expr,created_at,updated_at)"
              " VALUES ('t1','u','n','p','interval','5','x','x')")
    c.commit()
    c.close()


def test_new_db_has_pin_columns(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(scheduled_tasks)")}
    assert {"pin_status", "pin_spec", "pin_semantic_version",
            "pin_saved_at", "pin_invalid_reason", "pin_source_thread_id"} <= cols
    run_cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(task_runs)")}
    assert {"pin_mode", "pin_reason"} <= run_cols


def test_legacy_db_is_migrated(tmp_path):
    p = tmp_path / "legacy.sqlite"
    _legacy_db(p)
    s = ScheduledTaskStore(db_path=p)
    row = s.get_task("t1")
    assert row["pin_status"] == "" and row["pin_spec"] == ""


def test_set_invalidate_clear(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    s.insert_task({"id": "t1", "owner": "u", "name": "n", "prompt": "p",
                   "sched_kind": "interval", "sched_expr": "5",
                   "created_at": "x", "updated_at": "x"})
    s.set_pin("t1", spec_json='{"v":1}', semantic_version="abc",
              source_thread_id="th", now_iso="2026-10-23T00:00:00Z")
    r = s.get_task("t1")
    assert r["pin_status"] == "active" and r["pin_invalid_reason"] == ""

    s.invalidate_pin("t1", "semantic_version_changed: a -> b", "2026-10-24T00:00:00Z")
    r = s.get_task("t1")
    assert r["pin_status"] == "invalid"
    assert r["pin_invalid_reason"].startswith("semantic_version_changed")
    assert r["pin_spec"] == '{"v":1}'    # invalid 不清 spec，便于人工排查

    s.clear_pin("t1", "db_changed: a -> b", "2026-10-25T00:00:00Z")
    r = s.get_task("t1")
    assert r["pin_status"] == "" and r["pin_spec"] == ""
    assert r["pin_invalid_reason"].startswith("db_changed")


def test_update_task_cannot_write_pin(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    s.insert_task({"id": "t1", "owner": "u", "name": "n", "prompt": "p",
                   "sched_kind": "interval", "sched_expr": "5",
                   "created_at": "x", "updated_at": "x"})
    assert s.update_task("t1", {"pin_status": "active"}) is False
    assert s.get_task("t1")["pin_status"] == ""


def test_record_run_pin_mode(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    s.insert_task({"id": "t1", "owner": "u", "name": "n", "prompt": "p",
                   "sched_kind": "interval", "sched_expr": "5",
                   "created_at": "x", "updated_at": "x"})
    s.record_run("t1", "th", "r1", "2026-10-23T00:00:00Z", pin_mode="fallback",
                 pin_reason="columns_changed: missing=[b]")
    run = s.list_runs("t1")[0]
    assert run["pin_mode"] == "fallback"
    assert "columns_changed" in run["pin_reason"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_store.py -q`
Expected: FAIL（`KeyError: 'pin_status'` / `TypeError: record_run() got an unexpected keyword argument 'pin_mode'`）

- [ ] **Step 3: 实现**

- `_SCHEMA` 里 `scheduled_tasks` / `task_runs` 按 §A 加列；`__init__` 在 `executescript(_SCHEMA)` 之后调 `self._migrate_schema()`。
- `_migrate_schema` 照 `src/agent/feedback/store.py:244-276`：`pending = ((table, col, decl), ...)`，`PRAGMA table_info` 判存在，逐列 `try/except` 不中断启动，最后 `commit`。
- `_COLUMNS` / `insert_task` 默认值 / `_UPDATABLE` 按 §A 改。
- `update_task` 已有"未知/不可更新字段 → False"的行为，测试 T13 依赖它；若实现是**过滤未知字段后照写**，改成"含不可更新字段即返回 False"，否则 T13 会写成假绿。
- `record_run` 加两个可选参数，INSERT 补两列。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_store.py src/test/unit/test_scheduled_worker.py -q`
Expected: PASS（worker 既有测试不被签名变更打破）

- [ ] **Step 5: 提交**

```bash
git add src/agent/scheduler/store.py src/test/unit/test_scheduler_pin_store.py
git commit -m "feat(scheduler): 固化查询的存储列与幂等迁移"
```

---

### Task 5: 固化编排（读结果 → 固化 → 验证 → 落库）

**Files:**
- Create: `src/agent/scheduler/pin_service.py`
- Test: `src/test/unit/test_scheduler_pin_service.py`

**Interfaces:**
- Consumes: `extract_spec` / `render_spec` / `validate_columns`（Task 2）、`run_pinned_spec`（Task 3）、store 的 `set_pin` / `invalidate_pin` / `clear_pin`（Task 4）、`service.get_task_for_user`、`agent.utils.git_repo.repo_info`、`agent.utils.semantic_db` 的 `project_path_for`
- Produces:
  - `async def semantic_version_of(db_name: str) -> str` —— 已建模库返回 40 位 commit sha，直连库返回 `""`
  - `async def capture_spec(task: dict, *, thread_id: str | None, now: datetime, date_bindings=None, accept_constants=False, client=None) -> tuple[dict, list[str]]`
  - `async def pin_task(task: dict, *, thread_id=None, date_bindings=None, accept_constants=False, now=None, store=None, client=None) -> dict`
  - `async def invalidate(task_id: str, reason: str, now=None, store=None) -> None`
  - `async def clear(task_id: str, reason: str, now=None, store=None) -> None`

- [ ] **Step 1: 写失败测试**

```python
import pytest

from agent.scheduler import pin_service


async def test_semantic_version_uses_commit_not_tag(monkeypatch):
    monkeypatch.setattr(pin_service, "project_path_for", lambda db: "/proj")
    monkeypatch.setattr(pin_service, "repo_info",
                        lambda p: {"commit": "deadbeef" * 5, "tag": "v1.10.2",
                                   "branch": "main"})
    assert await pin_service.semantic_version_of("d") == "deadbeef" * 5


async def test_direct_db_has_empty_version(monkeypatch):
    monkeypatch.setattr(pin_service, "project_path_for", lambda db: None)
    assert await pin_service.semantic_version_of("d") == ""


def _seed(tmp_path):
    """一个已建好的任务行 + store。"""
    from agent.scheduler.store import ScheduledTaskStore

    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    s.insert_task({"id": "t1", "owner": "u", "name": "n", "prompt": "p",
                   "db_name": "d", "sched_kind": "interval", "sched_expr": "5",
                   "tz": "Asia/Shanghai", "created_at": "x", "updated_at": "x"})
    return s, s.get_task("t1")


def _wire(monkeypatch, *, columns=("a",), exec_error=None):
    """把 capture/执行/版本查三处都打成桩，只测编排分支。"""
    async def fake_capture(task, *, thread_id, now, date_bindings=None,
                           accept_constants=False, client=None):
        return ({"v": 1, "channel": "sql", "db_name": "d", "tz": "Asia/Shanghai",
                 "sql_template": "SELECT 1", "params": [], "expect_columns": ["a"],
                 "row_limit": 5000, "semantic_version": "", "source_thread_id": "th",
                 "captured_at": "x", "warnings": []}, [])

    class _Res:
        sql, cube_query, cube_args, cube_tool, channel = "SELECT 1", "", {}, "", "sql"
        rows, truncated, row_count = [[1]], False, 1

    async def fake_run(spec, **kw):
        if exec_error is not None:
            raise exec_error
        res = _Res()
        res.columns = list(columns)
        return res

    monkeypatch.setattr(pin_service, "capture_spec", fake_capture)
    monkeypatch.setattr(pin_service, "run_pinned_spec", fake_run)
    monkeypatch.setattr(pin_service, "project_path_for", lambda db: None)  # 直连库


async def test_pin_task_happy_path(monkeypatch, tmp_path, now):
    s, task = _seed(tmp_path)
    _wire(monkeypatch)
    out = await pin_service.pin_task(task, now=now, store=s)
    assert out["pin_status"] == "active"
    row = s.get_task("t1")
    assert row["pin_status"] == "active" and row["pin_source_thread_id"] == "th"


async def test_pin_task_column_drift_rejected(monkeypatch, tmp_path, now):
    s, task = _seed(tmp_path)
    _wire(monkeypatch, columns=("b",))
    with pytest.raises(pin_service.PinError) as e:
        await pin_service.pin_task(task, now=now, store=s)
    assert e.value.code == "columns_changed"
    assert s.get_task("t1")["pin_status"] == ""   # 失败不落库


async def test_pin_task_never_pins_on_exec_failure(monkeypatch, tmp_path, now):
    from agent.scheduler.pin_exec import PinExecError

    s, task = _seed(tmp_path)
    _wire(monkeypatch, exec_error=PinExecError("sql_failed", "Table 't' doesn't exist"))
    with pytest.raises(pin_service.PinError) as e:
        await pin_service.pin_task(task, now=now, store=s)
    assert e.value.code == "sql_failed"
    assert s.get_task("t1")["pin_spec"] == ""
```

（`_wire` 把 `project_path_for` 钉成 `None` ⇒ `semantic_version_of` 走直连库分支返回 `""`，与假 spec 里的 `semantic_version: ""` 一致，版本闸不会拦。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_service.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `semantic_version_of`：`SemanticDbDetector().project_path_for(db_name)` 拿路径 → `git_repo.repo_info(path)["commit"]`；拿不到路径或 `commit` 为空返回 `""`（直连库）。**比 commit，不比 tag/branch**（Review Focus #2）。模块顶部 `from agent.utils.semantic_db import SemanticDbDetector`、`from agent.utils.git_repo import repo_info`，并把 `project_path_for` / `repo_info` 绑成模块级名字（`project_path_for = SemanticDbDetector().project_path_for` 这种写法不行——detector 有状态，改成 `def project_path_for(db): return SemanticDbDetector().project_path_for(db)`），以便测试 monkeypatch。
- `capture_spec`：`thread_id` 缺省取 `store.list_runs(task_id, limit=20)` 里最近一条 `status='success'` 且有 `thread_id` 的记录；仍无 → `PinError("no_source_run")`。用 `client.threads.get_state(thread_id)` 取 `values["messages"]`，调 `report_builder._find_last_check_result(messages, "")` 拿 check 结果（**这是唯一能复用现成解析的地方，别重写**）；拿不到 → `PinError("no_check_result")`。然后 `extract_spec(...)`。
- `pin_task`：`capture_spec` → `run_pinned_spec`（验证执行）→ `validate_columns`（不通过 → `PinError("columns_changed", detail)`，**不落库**）→ `store.set_pin(...)`。`pin_exec` 的 `PinExecError` 转成 `PinError(同 code)` 抛出，API 层转 400。
- `PinError(Exception)`：`code` + `detail`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_service.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent/scheduler/pin_service.py src/test/unit/test_scheduler_pin_service.py
git commit -m "feat(scheduler): 固化编排——取结果、验证执行、落库"
```

---

### Task 6: API 端点

**Files:**
- Modify: `src/api/scheduled_tasks.py:168-176`（路由表）、新增三个 handler
- Test: `src/test/unit/test_scheduler_pin_api.py`

**Interfaces:**
- Consumes: Task 5 的 `pin_task` / `clear` / `PinError`；`service.get_task_for_user`
- Produces（HTTP）:
  - `POST /api/scheduled-tasks/{task_id}/pin` → `{"ok": true, "task": {...}, "pin": {"channel","preview_sql","params","warnings"}}`
  - `DELETE /api/scheduled-tasks/{task_id}/pin` → `{"ok": true, "task": {...}}`
  - `GET /api/scheduled-tasks/{task_id}/pin` → `{"ok": true, "pin": {...} | null}`

- [ ] **Step 1: 写失败测试**

```python
def test_pin_ok(client, seeded_task, monkeypatch):
    async def fake_pin(task, **kw):
        return {"pin_status": "active", "pin_spec": "{}",
                "pin": {"channel": "sql", "preview_sql": "SELECT 1",
                        "params": {"month_start": "2026-10-01"}, "warnings": []}}
    monkeypatch.setattr(api_module.pin_service, "pin_task", fake_pin)
    r = client.post(f"/api/scheduled-tasks/{seeded_task}/pin", json={})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_pin_other_owner_404(client, other_user_task):
    r = client.post(f"/api/scheduled-tasks/{other_user_task}/pin", json={})
    assert r.status_code == 404


def test_pin_ambiguous_returns_400(client, seeded_task, monkeypatch):
    from agent.scheduler.pin_service import PinError

    async def fake_pin(task, **kw):
        raise PinError("ambiguous_date", "2026-11-01: ['month_start','today']")

    monkeypatch.setattr(api_module.pin_service, "pin_task", fake_pin)
    r = client.post(f"/api/scheduled-tasks/{seeded_task}/pin", json={})
    assert r.status_code == 400
    assert "歧义" in r.json()["error"] or "ambiguous" in r.json()["error"]


def test_unpin_clears(client, seeded_task, monkeypatch):
    called = {}

    async def fake_clear(task_id, reason, **kw):
        called.update(task_id=task_id, reason=reason)

    monkeypatch.setattr(api_module.pin_service, "clear", fake_clear)
    r = client.delete(f"/api/scheduled-tasks/{seeded_task}/pin")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called["task_id"] == seeded_task
    assert called["reason"].startswith("user_unpin")


def test_get_pin_returns_null_when_absent(client, seeded_task):
    r = client.get(f"/api/scheduled-tasks/{seeded_task}/pin")
    assert r.status_code == 200 and r.json()["pin"] is None
```

夹具与本文件其它 API 测试同款（`client` 用 `TestClient(Starlette(routes=scheduled_tasks.routes))`，`api_module` 即 `api.scheduled_tasks` 模块对象，`seeded_task` / `other_user_task` 是直接 `store.insert_task` 造出的两个任务的 id）。前两个用例里 `pin_task` 返回的是**伪造的整行**——只为验证路由与权限，不验证业务。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_api.py -q`
Expected: FAIL 404（路由不存在）

- [ ] **Step 3: 实现**

- 文件顶部加 `from agent.scheduler import pin_service`（模块级，测试用 `monkeypatch.setattr(api_module.pin_service, ...)` 打桩）。
- 路由表按上面三条追加（`{task_id}` 已在既有路由里，注意 `pin` 段不与 `/runs` 冲突）。
- handler 全部遵循本文件既有契约：错误返回 `{"ok": false, "error": ...}`，不抛异常；未登录 401；`service.get_task_for_user` 返回 `None` → 404（**不是 403**，沿用 owner 隔离口径）。
- 响应体里 `task` 用 service 返回的整行（REST 层无序列化白名单，新列自动透出）。
- 权限：pin 会触发一次真实查询，必须在 handler 里再走一次 `service.recheck_db_access(task["owner"], task["db_name"])`，失败 403 并提示"无权访问数据库"。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/api/scheduled_tasks.py src/test/unit/test_scheduler_pin_api.py
git commit -m "feat(api): 定时任务固化的 pin/unpin/pin 预览端点"
```

---

### Task 7: 触发分叉与回放注入

**Files:**
- Modify: `src/agent/scheduler/worker.py:340-422`（`_fire`）、新增 `_build_replay_input`
- Test: `src/test/unit/test_scheduler_pin_replay.py`

**Interfaces:**
- Consumes: Task 2 的 `build_check_payload`；Task 3 的 `run_pinned_spec` / `PinExecError`；Task 4 的 `record_run(pin_mode=...)` / `invalidate_pin`
- Produces:
  - `PIN_TASK_ID = "pin-replay"`、`PIN_CALL_ID = "pin-replay-call"`
  - `def _build_replay_input(content: str, payload: dict) -> dict` → `runs.create` 的 `input`
  - `async def _try_pinned_fire(row, store, now, client) -> tuple[bool, str, str, dict | None]` → `(是否回放成功, pin_mode, pin_reason, replay_input)`

- [ ] **Step 1: 写失败测试**

绑定 T15–T18、T21：

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler import worker
from agent.scheduler.store import ScheduledTaskStore
from agent.scheduler.worker import _build_replay_input


@pytest.fixture
def now():
    return datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _async(v):
    async def f(*a, **k):
        return v
    return f


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    s.insert_task({"id": "t1", "owner": "u", "name": "n", "prompt": "p",
                   "db_name": "d", "sched_kind": "interval", "sched_expr": "5",
                   "tz": "Asia/Shanghai", "created_at": "x", "updated_at": "x"})
    return s


@pytest.fixture
def seeded(store):
    return store.get_task("t1")


@pytest.fixture
def client():
    return object()   # 版本闸分支用不到；回放分支的 client 在 run_pinned_spec 里不参与



def test_replay_input_pairs_tool_calls():
    payload = {"status": "success", "thread_id": "pin-replay", "result": "x",
               "sql": "SELECT 1", "pinned_replay": True}
    inp = _build_replay_input("做报表", payload)
    msgs = inp["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["id"] == msgs[2]["tool_call_id"]
    assert msgs[1]["tool_calls"][0]["name"] == "check_async_task"


def test_replay_input_is_findable_by_report_builder():
    from agent.tools.report_builder import _find_last_check_result
    from langchain_core.messages import AIMessage, ToolMessage

    payload = {"status": "success", "thread_id": "pin-replay", "result": "表格",
               "sql": "SELECT 1", "pinned_replay": True}
    inp = _build_replay_input("x", payload)
    a, t = inp["messages"][1], inp["messages"][2]
    msgs = [AIMessage(content=a["content"], tool_calls=a["tool_calls"]),
            ToolMessage(content=t["content"], tool_call_id=t["tool_call_id"],
                        name="check_async_task")]
    hit = _find_last_check_result(msgs, "")
    assert hit is not None and hit[0]["sql"] == "SELECT 1"


async def test_version_mismatch_invalidates_and_falls_back(monkeypatch, seeded, now):
    row = {**seeded, "pin_status": "active",
           "pin_spec": '{"v":1,"channel":"sql","semantic_version":"old","db_name":"d"}',
           "pin_semantic_version": "old"}
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("new"))
    ok, mode, reason, replay_input = await worker._try_pinned_fire(row, store, now, client)
    assert ok is False and mode == "fallback" and replay_input is None
    assert reason.startswith("semantic_version_changed")
    assert store.get_task(row["id"])["pin_status"] == "invalid"


async def test_replay_success_sets_mode(monkeypatch, seeded, store, client, now):
    row = {**seeded, "pin_status": "active", "pin_semantic_version": "abc",
           "pin_spec": '{"v":1,"channel":"sql","db_name":"d","tz":"Asia/Shanghai",'
                       '"sql_template":"SELECT 1","params":[],"expect_columns":["a"],'
                       '"row_limit":5000,"semantic_version":"abc","warnings":[]}'}
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    class _Res:
        sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
        columns, rows, row_count, truncated = ["a"], [[1]], 1, False

    monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))
    ok, mode, reason, replay_input = await worker._try_pinned_fire(
        row, store, now, client)
    assert ok is True and mode == "replay" and reason == ""
    assert replay_input["messages"][2]["role"] == "tool"
    assert store.get_task("t1")["pin_status"] == "active"    # 成功不失效


async def test_truncated_result_still_replays(monkeypatch, seeded, store, client, now):
    """T20：结果被行上限截断 ≠ 失效——照常回放，截断事实由 check 载荷带出去。"""
    row = {**seeded, "pin_status": "active", "pin_semantic_version": "abc",
           "pin_spec": '{"v":1,"channel":"sql","db_name":"d","tz":"Asia/Shanghai",'
                       '"sql_template":"SELECT 1","params":[],"expect_columns":["a"],'
                       '"row_limit":5000,"semantic_version":"abc","warnings":[]}'}
    monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

    class _Res:
        sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
        columns, rows, row_count, truncated = ["a"], [[1]], 5000, True

    monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))
    ok, mode, _, replay_input = await worker._try_pinned_fire(row, store, now, client)
    assert ok is True and mode == "replay"
    assert store.get_task("t1")["pin_status"] == "active"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_replay.py -q`
Expected: FAIL（`_build_replay_input` 不存在）

- [ ] **Step 3: 实现**

`_fire` 改成：

```python
pin_mode, pin_reason = "normal", ""
replay_input = None
if row.get("pin_status") == "active":
    ok, pin_mode, pin_reason, replay_input = await _try_pinned_fire(...)
content = ...  # 既有内容不变
run = await client.runs.create(
    thread_id=thread_id, assistant_id="chat_agent",
    input=replay_input or {"messages": [{"role": "user", "content": content}]},
    config={"recursion_limit": 500,
            "configurable": {**configurable, **({"pinned_replay": True} if replay_input else {})}},
)
st.record_run(task_id, thread_id, run_id, now_iso, pin_mode=pin_mode, pin_reason=pin_reason)
```

要点：
- 文件顶部加 `from agent.scheduler import pin_service` 与 `from agent.scheduler.pin_exec import PinExecError, run_pinned_spec`（模块级，测试才能 monkeypatch 到）。
- **先建 thread 再判断固化**（回放 payload 要带这个新 thread_id），但预检失败时 thread 已建好——直接复用给正常 run，不要建两个、也不要删（删了会触发 `is_thread_gone` 分支）。
- `_try_pinned_fire` 内部捕获 `PinExecError` / `SpecError` / `json.JSONDecodeError`，统一调 `store.invalidate_pin(task_id, f"{code}: {detail}", now_iso)` 并返回 `(False, "fallback", reason, None)`；成功时返回 `(True, "replay", "", _build_replay_input(...))`；**绝不抛出**（抛了会走 `_fail_fire`，任务被错误地标记为 fire 失败）。
- `pinned_replay` 只能通过 `configurable` 传，且 `_fire` 显式传了 `config` ⇒ `_wrap_runs_create` 的自动注入分支不会补键（`deepagents_async_config_patch.py:190`），必须自己写。
- `run_now` 不需要改：它走同一条 `submit_fire` → `_fire`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduler_pin_replay.py src/test/unit/test_scheduled_worker.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent/scheduler/worker.py src/test/unit/test_scheduler_pin_replay.py
git commit -m "feat(scheduler): 触发时走固化回放，失效自动回退"
```

---

### Task 8: 主 agent 回放模式（提示词 + 工具面）

**Files:**
- Create: `src/agent/middlewares/pinned_replay.py`
- Modify: `src/agent/main_agent.py:107-142`（`dynamic_prompt`）、`:326-348`（`MAIN_AGENT_MIDDLEWARE`）
- Test: `src/test/unit/test_pinned_replay_mode.py`

**Interfaces:**
- Consumes: 既有 `langgraph.config.get_config`（照 `tool_filter._resolve_db_name` 的写法）
- Produces:
  - `def _cfg() -> dict` —— 模块内读 `langgraph.config.get_config()["configurable"]`（照 `tool_filter._resolve_db_name` 的写法，异常返回 `{}`），单独成函数是为了测试 monkeypatch
  - `class PinnedReplayMiddleware(AgentMiddleware)`：
    - `def _strip(self, request) -> ModelRequest` —— `_cfg().get("pinned_replay")` 为真时返回 `request.override(tools=[t for t in request.tools if getattr(t, "name", "") != "start_async_task"])`；否则**原样返回同一个对象**（`is` 相等，供 T23 断言）
    - `wrap_model_call` / `awrap_model_call` 只做 `return self._strip(request)`
  - `PINNED_REPLAY_SECTION: str` —— 提示词段落原文（供 `dynamic_prompt` 拼接与测试断言）

- [ ] **Step 1: 写失败测试**

测试文件顶部先放三个小工具（`T` 工具桩、`_fake_request`、`_fake_msg_request`），下面用例共用：

```python
class T:
    def __init__(self, name): self.name = name


def _fake_request(tools):
    class Req:
        def __init__(self, tools): self.tools = tools
        def override(self, *, tools): return Req(tools)
    return Req(tools)


def _fake_msg_request(text):
    class Msg:  # 只需要 .text，与 dynamic_prompt 的取法一致
        def __init__(self, t): self.text = t
    return type("R", (), {"system_message": Msg(text)})()


def test_middleware_strips_delegation(monkeypatch):
    from agent.middlewares.pinned_replay import PinnedReplayMiddleware

    tools = [T("build_report"), T("start_async_task"), T("generate_echarts")]
    req = _fake_request(tools)
    monkeypatch.setattr("agent.middlewares.pinned_replay._cfg",
                        lambda: {"pinned_replay": True})
    out = PinnedReplayMiddleware()._strip(req)
    assert [t.name for t in out.tools] == ["build_report", "generate_echarts"]


def test_middleware_untouched_without_flag(monkeypatch):
    monkeypatch.setattr("agent.middlewares.pinned_replay._cfg", lambda: {})
    req = _fake_request([T("build_report")])
    assert PinnedReplayMiddleware()._strip(req) is req


def test_dynamic_prompt_injects_section(monkeypatch):
    import agent.main_agent as m

    monkeypatch.setattr(m, "_get_configurable", lambda: {"pinned_replay": True})
    p = m.dynamic_prompt(_fake_msg_request("BASE"))
    assert "固化回放模式" in p and "禁止委派" in p and p.endswith("BASE")


def test_dynamic_prompt_plain_when_absent(monkeypatch):
    monkeypatch.setattr(m, "_get_configurable", lambda: {})
    assert "固化回放模式" not in m.dynamic_prompt(_fake_msg_request("BASE"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_pinned_replay_mode.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

提示词段落（逐字，测试按此断言）：

```
## 固化回放模式（本次运行）— 最高优先级
本轮数据已由系统按「已固化查询」取回，结果就在最近一条 check_async_task 的 ToolMessage 里
（其中 pinned_replay=true）。本次运行的规则：
1. 禁止委派 nl2sql、禁止调用任何查询工具重新取数——本任务的工具面已移除委派能力。
2. 禁止等待或轮询子任务；数据已完整。
3. 直接基于该结果生成图表与报告；报告的「执行 SQL」节使用结果里的 sql / cube_query 原文。
4. 结果为空或截断时如实说明「未查询到数据」/「结果已截断」，不得补充推测。
```

`dynamic_prompt` 里**在 `db_section` 之前**拼 `PINNED_REPLAY_SECTION`（回放模式优先级最高）。`main_agent._get_configurable()` 是对现有 `get_config()` 读取逻辑的提取（三行，与 `middlewares/pinned_replay.py` 里同名函数各自独立，不互相 import），方便测试 monkeypatch；不要为此引入全局变量。

`PinnedReplayMiddleware` 放进 `MAIN_AGENT_MIDDLEWARE`，紧跟 `scheduled_hitl` 之后（都在"拦截模型可见能力"这一层）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync python -m pytest src/test/unit/test_pinned_replay_mode.py src/test/unit/test_scheduled_hitl.py -q`
Expected: PASS（后者断言"恰好 1 个 HITL 中间件"不受影响）

- [ ] **Step 5: 提交**

```bash
git add src/agent/middlewares/pinned_replay.py src/agent/main_agent.py src/test/unit/test_pinned_replay_mode.py
git commit -m "feat(agent): 固化回放模式——摘掉委派工具并注入数据源说明"
```

---

### Task 9: 聊天工具透出 + 前端

**Files:**
- Modify: `src/agent/tools/manage_scheduled_task.py:71-89`（`_brief`）
- Create: 前端 `src/lib/scheduledPins.ts`
- Modify: 前端 `src/app/components/ScheduledTasksPanel.tsx:531-579`（卡片）、`:580-656`（操作行）
- Test: `src/test/unit/test_scheduled_task_brief.py`（新增 2 例）+ 前端 tsc/eslint

**Interfaces:**
- Consumes: 后端 `GET/POST/DELETE /api/scheduled-tasks/{id}/pin`
- Produces:
  - `_brief(row)` 输出新增 `pin_status`、`pin_invalid_reason`（截 200）
  - 前端 `type ScheduledPin = { channel: "sql"|"cube"; preview_sql: string; params: Record<string,string>; warnings: string[] }`
  - 前端 `type ScheduledTaskLike = { id: string; name: string; pin_status?: "" | "active" | "invalid"; pin_invalid_reason?: string }`（本地结构化类型，**不从** `scheduledTasks.ts` 引 `ScheduledTask`——那个文件禁止编辑、类型里也没有 pin 字段）
  - `getScheduledPin(id): Promise<Record<string, unknown> | null>`
  - `pinScheduledTask(id, opts?: { thread_id?: string; accept_constants?: boolean; date_bindings?: Record<string,string> }): Promise<{ ok: true; task: ScheduledTaskLike; pin: ScheduledPin }>`
  - `unpinScheduledTask(id): Promise<{ ok: true; task: ScheduledTaskLike }>`

- [ ] **Step 1: 写失败测试（后端）**

```python
def test_brief_exposes_pin_status():
    from agent.tools.manage_scheduled_task import _brief
    out = _brief({"id": "t", "name": "n", "pin_status": "invalid",
                  "pin_invalid_reason": "columns_changed: missing=[b]"})
    assert out["pin_status"] == "invalid"
    assert "columns_changed" in out["pin_invalid_reason"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync python -m pytest src/test/unit/test_scheduled_task_brief.py -q`
Expected: FAIL（`KeyError: 'pin_status'`）

- [ ] **Step 3: 实现后端 `_brief`**

只加两个键，不改其它字段；`pin_invalid_reason` 截断到 200 字（与 `last_error` 同口径）。

- [ ] **Step 4: 跑测试确认通过并提交后端**

```bash
uv run --no-sync python -m pytest src/test/unit/test_scheduled_task_brief.py -q
git add src/agent/tools/manage_scheduled_task.py src/test/unit/test_scheduled_task_brief.py
git commit -m "feat(tool): 定时任务简报透出固化状态"
```

- [ ] **Step 5: 前端 API 封装（新建文件，绕开 DLP）**

`src/lib/scheduledPins.ts` 里自己写一个最小的 `request`（照 `scheduledTasks.ts:78-99` 的形状：`credentials: "include"`、失败取 `data.error` 抛 `Error`），**不要 import `scheduledTasks.ts`**（能 import 但类型不全，且该文件禁止编辑；新增类型用本地结构化类型）。

- [ ] **Step 6: 前端卡片与按钮**

- 卡片名称行（`ScheduledTasksPanel.tsx:534-536` 附近）加徽标：
  - `pin_status === "active"` → 绿色「已固化」
  - `pin_status === "invalid"` → 琥珀色「固化失效」+ `title={pin_invalid_reason}`
- 操作行（`:630` 编辑按钮之后、删除之前）加一个同款 ghost 按钮：未固化显示「固化」，已固化显示「取消固化」，`invalid` 显示「重新固化」。
- 固化成功后用后端返回的 `pin.preview_sql` 弹一个只读确认框（`window.confirm` 不够看，用现成的 `Dialog`）展示：通道、渲染后的 SQL、参数取值、`warnings`。**这一步必须让用户看到渲染后的完整 SQL**——这是"先执行一遍、确认无误再固化"的落点。
- `busy` 复用 `act()` 的既有模式，失败用 `alert(e.message)`。

- [ ] **Step 7: 前端校验**

Run: `cd D:/code_work_space/llm/huice/008/harness-deep-agents-ui && npx tsc --noEmit 2>&1 | grep -c "error TS"`
Expected: 计数 ≤ 37（既有基线），且 `grep` 结果里不出现 `scheduledPins.ts` / `ScheduledTasksPanel.tsx`

Run: `npx eslint src/lib/scheduledPins.ts src/app/components/ScheduledTasksPanel.tsx`
Expected: 无输出

- [ ] **Step 8: 提交**

```bash
cd D:/code_work_space/llm/huice/008/harness-deep-agents-ui
git add src/lib/scheduledPins.ts src/app/components/ScheduledTasksPanel.tsx
git commit -m "feat(scheduler): 定时任务固化入口与失效提示"
```

---

## 端到端验收（人工，dev 34）

1. 建一个「每天 09:00」的任务，正常跑一次，确认 `task_runs.status='success'`。
2. 面板点「固化」→ 弹层里能看到**当天日期已填好**的完整 SQL → 确认 → 徽标变「已固化」。
3. 用 `POST /api/scheduled-tasks/{id}/run` 立即触发 → 会话里报告正常生成，`task_runs.pin_mode='replay'`，报告「执行 SQL」节与固化 SQL 一致（仅日期变化）。
4. 改语义库（随便改一条模型描述并提交构建）→ 再触发 → `pin_status='invalid'`、`pin_invalid_reason='semantic_version_changed: ...'`，卡片显示「固化失效」，本次 run 仍然正常出报告，`pin_mode='fallback'`。
5. 点「重新固化」→ 恢复 `active`。
6. 改动任务的 `db_name` → `pin_status=''`、`pin_invalid_reason='db_changed: ...'`。

## 非目标（明确不做）

- 不做「自动学习/自动重新固化」。
- 不做区间锚点（近 7 天、上季度）。
- 不在聊天里加 `pin` 动作（避免又一次 HITL 语义扩张）；固化只能从面板点。
- 不缓存查询结果、不复用上一次的行数据——每次触发都真实执行一次固化 SQL。
