# Langfuse Experiment 离线实施方案（四维 A/B：前端 + API + 双落点）

- 日期：2026-09-01（历史：2026-08-30 方案 B CLI / 08-31 前端 + API 上线 / 09-01 Expected Output 修复）
- 状态：**已实施 + 前端已上线**（`/experiment` 页为主入口，CLI 保留为底层引擎）
- 关联：`run_experiment.py`（离线引擎）、`src/api/experiment.py`（HTTP API）、`harness-deep-agents-ui/src/app/experiment/`（前端）、Langfuse Dataset:badcase/goodcase、Wren 语义库 git 版本化、skill git 版本化

## 1. 目标

1. **离线跑 Langfuse Experiment**：模型生成 + 评分都在本地 worker 子进程跑，不依赖 Langfuse 云端 judge；
2. **选定数据集**：前端下拉多选 **任意 Langfuse Datasets**（不限于 badcase/goodcase），带条数上限、badcase 状态过滤；
3. **四维 A/B 可切**：数据集 × **prompt label** × **skill 版本**（git ref）× **wrenai 语义库版本**（git ref），每臂独立指定，缺省维度走默认；
4. **结果双落点进 Langfuse**：同一 trace 同时出现在 **Experiment 页**（`langfuse.experiment.*` 属性，含 Expected Output 金标）与 **Datasets→Runs**（Dataset Run），UI 可对比多轮；
5. **策略分层统计 + 双门槛门禁**：按查询实际走的策略 A/B/C 分层聚合，防策略混合摊薄信号；
6. **历史管理**：前端可查看运行中/已完成 run、删除历史实验、逐条下钻 Langfuse trace。

## 2. 现状与机制（已核实）

- **离线引擎** `run_experiment.py`：纯 CLI + 可被 API 进程内调用。`--dataset` 装载查询集 → 逐 arm spawn worker 子进程 → worker 顶层 import `nl2sql_agent`（MCP 工具在 asyncio 外加载）→ `g.ainvoke` 逐条跑 → 抽 `run_sql`+结果 → 五维确定性分（+可选 `--judge`）→ 聚合对比 + 阈值门禁（exit 1）。
- **HTTP API** `src/api/experiment.py`：把引擎包装为后台任务 + 状态轮询（8 路由，`custom_app.py` 一行注册），前端只走 API。
- **Langfuse SDK v4.14.4** 具备全部原语：
  - `api.dataset_items.list(dataset_name=..., limit≤100)` 读数据集（`meta.total_items` 拿条数，`datasets.list` 元数据不带 item 数）；
  - `api.dataset_run_items.create(run_name, dataset_item_id, trace_id, observation_id, metadata)` —— run 由首个 run_item 隐式创建；
  - `api.experiments.list/list_items`（**只读**）：Experiment 页实体与 item 查询；
  - 官方高阶 `run_experiment()` 会包自己的 OTEL span，与本项目自定义 trace 层级（M-T1~T6）冲突，**不采用**；采用低层手动装配（路径 B），保留 worker + trace 层级。
- **wrenai 语义库** = Wren 项目目录（`models/` + `cubes/` + `target/mdl.json`），wrenai MCP server 是 worker 进程自起的 stdio 子进程（`wren serve mcp --project <路径> --profile <连接>`），`--project` 指向哪就是哪套语义库。
- **skill 版本** = git 物化（`SKILLS_REF` env + `git_archive_materialize`，与语义库同模式），两个 agent 的 `SkillsMiddleware` sources 读物化目录。
- **trace 层级**（M-T1~T6b）：一次查询 = 单条 chat-turn trace；子 agent NonRecordingSpan 注入；根链 `on_chain_start` 补丁补 `root_observation_id`。

## 3. 总览架构

```
前端 /experiment（harness-deep-agents-ui，Next.js）
  └─ src/lib/experiment.ts（request<T> + apiBase()）
       └─ 后端 API src/api/experiment.py（8 路由，custom_app.py 注册）
            ├─ POST /api/experiment/runs → stamp + {stamp}.status.json → asyncio.create_task(_execute_run)
            │     └─ _prepare_queries（数据集多选装载 / 去重 / db_name 归一化 / expected_output）
            │          └─ _run_orchestrator（逐 arm spawn worker 子进程，on_progress 写 status）
            │               └─ worker: env 注入(LANGFUSE_PROMPT_LABEL/SKILLS_REF/WREN_SEMANTIC_OVERRIDE)
            │                    → _experiment_attrs 包 g.ainvoke（langfuse.experiment.* + expected_output）
            │                    → _report_run_item（Dataset Run）+ trace 级五维分
            ├─ GET  /runs / GET /runs/{stamp}（状态轮询 + manifest/gate/逐条明细/trace_url）
            └─ DELETE /runs/{stamp}（删除历史，running 409）
       本地落盘（工作区 eval/experiment_runs/）：
            run_{stamp}.json（manifest，完成才落盘） + {stamp}.status.json（进度，running 阶段即写）
            + {stamp}/out/exp_{arm}.jsonl（逐条明细）
       Langfuse 双落点：trace(langfuse.experiment.*) → Experiment 页；dataset_run_items → Datasets→Runs
```

arm = `{name, prompt_label?, skill_ref?, semantic_ref?}`。未指定维度走默认：生产 prompt（production）/ 当前磁盘 skill / 当前语义库 HEAD。

## 4. API 层（src/api/experiment.py）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/experiment/datasets` | GET | 全部 Langfuse Datasets 及 item 数（每数据集一次 `dataset_items.list(page=1,limit=1)` 读 `meta.total_items`；失败标 -1），按名排序 |
| `/api/experiment/prompt-labels` | GET | 参与实验的 prompt（`_PROMPT_NAMES`）labels 聚合（`prompts.list` 去重） |
| `/api/experiment/skill-refs` | GET | skill git refs（tags/branches/HEAD），**`ls-tree` 过滤「树里真含 skills 子树」的 ref**（skill 在主仓库内，未过滤会把语义库 v1~v6 tags 误当 skill 版本；staged 未 commit 的 ref 也过滤掉） |
| `/api/experiment/semantic-refs?db=X` | GET | 指定库语义库 git refs（`project_path_for` 未建模 → 404） |
| `/api/experiment/runs` | POST | 提交 `{datasets[], dataset_limit?, arms[], judge?, threshold?}` → 建 stamp + 初始 status → 后台执行 → 返回 `{ok, stamp, status_url}` |
| `/api/experiment/runs` | GET | 历史列表（倒序）—— **manifest 与 `*.status.json` 合并扫**（见 §7.2） |
| `/api/experiment/runs/{stamp}` | GET | 状态 + manifest + gate + 逐条明细（读 `exp_{arm}.jsonl`）+ `trace_url`；running 超时 → interrupted |
| `/api/experiment/runs/{stamp}` | DELETE | 删除 manifest / status / run 明细目录；running（含僵尸 run）→ 409 |

后台执行 `_execute_run(stamp, body)`：`asyncio.create_task` + `asyncio.to_thread` 包阻塞的 `_prepare_queries`/`_run_orchestrator`（不卡事件循环）；`on_progress` 回调写 `{stamp}.status.json` 的 `stage/progress`。cancel 不在 MVP（subprocess 树取消复杂），超时由 GET 侧标记 interrupted 兜底。

## 5. 分模块方案

### 5.1 数据集选择（_load_dataset_queries + _prepare_queries）

- `_load_dataset_queries(dataset_name, limit, status_filter)`：
  - `api.dataset_items.list(page=1.., limit=100)` 翻页装载；
  - 每条产出 `{question, db_name, source_trace_id, dataset_item_id, dataset_name}` + **`expected_output`（金标，非 None 才带，供 Experiment 页 Expected Output 列）**；
  - badcase 套 `badcase_status` 过滤（fixed/invalid 默认跳过）；goodcase 不套；
  - `(question, db_name)` 去重（`_dedupe_queries`）。
- `_prepare_queries(args)`：`--queries` 文件 + `datasets` 列表（API 多选，任意数据集集合）或 `--dataset` 单值（CLI，限 badcase/goodcase/all）合并 → 去重 → `db_name` 归一化（`normalize_db_name`，大小写不敏感）→ 落盘 `queries_{stamp}.json`。
- 展示用 dataset 名：单数据集 = 原名；多数据集 = 逗号连接（manifest/history）。

### 5.2 编排器（_run_orchestrator）

- `_build_arms(args)`：有 `--arms` JSON 文件 → `[{name?, prompt_label?, skill_ref?, semantic_ref?}]`；无 → 由 `--labels`+`--semantic` 按位置构造（旧行为）。
- 逐 arm spawn worker 子进程：`env["PYTHONPATH"]=src`，cmd 带 `--worker --label <arm> --queries <qpath> --out <out> --run-name <name> --prompt-label <pl> --skill-ref <sr> --semantic <sem>`。
- `_default_run_name(arm, stamp)` = `<arm名>:<语义ref>[:<skill>]@<stamp>`（语义 ref 缺省 `head`）。
- 聚合 `_aggregate(records)` 两层：`overall` + `by_strategy`（A/B/C/none，不足 1 条该维 null）。
- **门禁（arms≥2，ref=arms[0], cand=arms[1]）**：双门槛——`overall` 核心维掉 > threshold、**A 类**核心维掉 > threshold，任一 → fail。`CORE_DIMS=(sql_biz_correct_score, sql_valid_score, sql_exec_success)`，`AUX_DIMS=(schema_match_score)` 不参与门禁。单 arm 不门禁（canary 预检）。
- manifest 落盘（工作区 `eval/experiment_runs/run_{stamp}.json`）：`{stamp, queries, dataset, arms, labels:{arm:{aggregate, run_name}}, gate?}`。
- `_write_run_scores`：`datasets.get_run(dataset_name, run_name)` 拿 run_id → `scores.create(name="experiment:<dim>", value, dataset_run_id=...)` 写 run 级分（badcase/goodcase 都试）。

### 5.3 worker（_run_worker）

- 入口 env 注入（import graph 前，进程级 A/B）：
  - `LANGFUSE_PROMPT_LABEL`（显式指定或沿 label；空串 → production 默认）；
  - `SKILLS_REF`（skill 版本，非空）；
  - `WREN_SEMANTIC_OVERRIDE`（语义库版本，非空）；
  - `LANGFUSE_TRACING_ENVIRONMENT=experiment`（UI Environment 列区分实验 trace）。
- 每条查询用 `_experiment_attrs`（`@contextmanager`）包 `g.ainvoke`：
  - 构建 `langfuse.experiment.*` 属性：`experiment_id=exp-<run_name>`（一 arm 一实体）/`experiment_name=<run_name>`/`experiment_dataset_id`（`api.datasets.get` 按名解析 UUID，进程级缓存）/`experiment_item_id`/`experiment_item_metadata`；
  - 根链 `on_chain_start` 补丁补 `langfuse.experiment.item.root_observation_id=<root span 自身 id>`；
  - **Expected Output（2026-09-01）**：goodcase 金标经 `item["expected_output"]` → 同一补丁内 `otel.set_attribute("langfuse.experiment.item.expected_output", <JSON 字符串>)` 直写 root span（见 §9.2 关键坑）；
  - 仅当 handler 可用 + item 带 `dataset_item_id` + dataset_id 可解析才启用，否则退化纯 Dataset Run；失败只告警不阻断查询。
- 逐条落库：`_report_run_item`（Dataset Run，仅当有 item_id）+ trace 级五维分 `create_score`（metadata 带 label/experiment/semantic_ref/skill_ref）。
- 记录字段：`{question, db_name, sql, result_head, scores, reasons, strategy, trace_id, dataset_item_id, dataset_name, semantic, skill_ref, run_name, index}`。

### 5.4 skill 版本化（skills_versioning.py + git_archive.py）

- `git_archive.py`：从 `semantic_db._git_archive_materialize` 提取为共享 `git_archive_materialize(base, ref, dest_root)`（`git archive <ref> <relpath> -o tmp.tar` → 解压；`--project` 指向物化目录即换语义库/skill 版本）。
- `SKILLS_REF` 取值形态：`<ref>`（仓库内置目录 src/agent/shared/skills）或 `<path>@<ref>`（显式源）；未设/物化失败 → 原样返回默认 sources（磁盘 skill），生产默认行为不变。
- 物化到 `<data_root>/skill_refs/<safe_ref>/`（须在 data_root 内，否则 `vfs_root_backend` 无法解析）；`git archive` 子树带 `src/agent/shared/skills/` 前缀 → 提升后 VFS 路径 `/skill_refs/<safe_ref>/{main,nl2sql}/` 直接命中。
- 接线：`nl2sql_agent.py` / `main_agent.py` 的 `SkillsMiddleware` sources 用 `effective_skills_sources(default, group)`。

### 5.5 语义库 A/B（semantic_db.py，沿用 §旧文档 4.4，机制未变）

- `WREN_SEMANTIC_OVERRIDE=<db>=<ref>` 解析 + `semantic_project_path(db)` 物化（git archive + 进程级缓存，按 `(db, source, ref)`）。
- 失败 → WARNING + 回退 base（静默退化当前版本）；大小写不敏感匹配；`normalize_db_name` 自动归一化（dataset 小写变体 `chinook_aliyun` → 配置名 `Chinook_Aliyun`，否则判未建模强制直连 B）。

### 5.6 状态机与历史

- `{stamp}.status.json`：`{stamp, status(running|done|error|interrupted), stage, progress{total,done,current}, arms, request, error, started_at, finished_at}`。POST 即写初始 running（`stage=queued`），`on_progress` 逐步更新，完成写 done/error + finished_at。
- **list_runs 双扫**（2026-08-31 修复）：manifest 是完成才落盘，running 阶段只有 status.json → 必须同时扫 `*.status.json` 补条目，否则「运行中」的 run 从历史消失、无法点回看。
- 超时：`_RUN_TIMEOUT=7200s`，GET 侧 running 且超时 → 标 interrupted。
- 删除：`DELETE /runs/{stamp}` 删 manifest + status + run 目录（`shutil.rmtree`）；running（含重启后的僵尸 run）→ 409；不存在 → 404 幂等。

### 5.7 时间戳

- `_stamp()` 用**北京时间**：`datetime.now(timezone(timedelta(hours=8)))`（固定 UTC+8，无 DST，不依赖 tzdata）→ 历史 run 标题 `run 20260831T133203` 不再 0 时区观感。`started_at`/`finished_at` 保持 UTC ISO，前端 `toLocaleString` 转本地。

### 5.8 前端（harness-deep-agents-ui）

- `src/app/experiment/page.tsx`：主页面（use client），左栏表单 + 历史，右栏进度/对比/明细。
- `src/app/components/experiment/`：
  - `ExperimentForm.tsx`：数据集多选下拉（自绘，枚举全部 Datasets + 条数）+ 条数上限 + judge 开关 + 门禁阈值 + 动态 arm 列表（每臂 prompt Select / skill Select / semantic 输入）；**「数据集与参数」「对比臂」卡片默认折叠**（交互参考「语义库 refs 速查」）；「添加臂」自动展开对比臂区；
  - `RunHistory.tsx`：历史列表（状态徽章：运行中/完成/失败/中断 + PASS/FAIL），hover 显示删除按钮（confirm 后 `deleteRun`），刷新按钮；
  - `RunProgressCard.tsx`：2s 轮询 `fetchRun`，Loader/进度条/stage/error；
  - `ResultCompareTable.tsx` / `StrategyBreakdown.tsx` / `GateBadge.tsx` / `ItemDetailDrawer.tsx`（逐条下钻，`trace_url` 跳 Langfuse）。
- `src/lib/experiment.ts`：`fetchDatasets/fetchPromptLabels/fetchSkillRefs/fetchSemanticRefs/submitRun/listRuns/fetchRun/deleteRun`。
- 首页 header 加 `/experiment` 导航入口。

## 6. 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/agent/utils/git_archive.py` | **新增**：`git_archive_materialize` 共享实现（语义库/skill 共用） |
| `src/agent/utils/skills_versioning.py` | **新增**：`SKILLS_REF` 解析 + 物化 + `effective_skills_sources` |
| `src/agent/graphs/nl2sql_agent.py` / `src/agent/main_agent.py` | `SkillsMiddleware` sources 接 `effective_skills_sources` |
| `src/agent/utils/semantic_db.py` | `_git_archive_materialize` 改 import 共享实现（保留别名）；`WREN_SEMANTIC_OVERRIDE`/`normalize_db_name` 沿用 |
| `src/agent/eval/run_experiment.py` | `_load_dataset_queries`（含 expected_output 装载）、`_prepare_queries`（datasets 多选分支）、`_build_arms`/`_default_run_name`、worker `_experiment_attrs`（含 expected_output 注入）、门禁双门槛、manifest 含 gate |
| `src/api/experiment.py` | **新增**：8 路由 + 后台执行 + 状态轮询 + 删除 |
| `src/api/custom_app.py` | 注册 `experiment.routes` 一行 |
| `harness-deep-agents-ui/src/lib/experiment.ts` | **新增**：API 客户端 |
| `harness-deep-agents-ui/src/app/experiment/page.tsx` + `components/experiment/*` | **新增**：页面与 7 个组件 |

## 7. 关键坑与修复记录

### 7.1 数据集多选下拉卡死（前端 label 嵌套）

外层 `<label>` 包 button + 内层 checkbox `<label>` 是无效 HTML，浏览器 label 关联把点击分派给外层关联控件（trigger button），一次点击触发两次 `setDsOpen` 翻转 → 页面卡住。修复：外层/每行改 `<div>`，checkbox `readOnly` + `pointer-events-none`，整行 `onClick` toggle。

### 7.2 运行中 run 从历史消失

`list_runs` 原先只扫 manifest（完成才落盘），running 只有 status.json → 运行中 run 不显示。修复：双扫（manifest + `*.status.json`）。

### 7.3 时间戳 0 时区

历史标题 stamp 用 UTC 生成 → 显示差 8 小时。修复：`_stamp()` 改 `timezone(timedelta(hours=8))`（固定偏移无 DST）。

### 7.4 skill-refs 误列语义库 tags

skill 目录在 nl2sql 主仓库内，`git -C` 会向上找到主仓库，把语义库 v1~v6 tags 当 skill 版本。修复：`ls-tree` 逐个 ref 过滤「树里真含 skills 子树」。

### 7.5 Expected Output 为空（2026-09-01 修复，见 §9.2）

## 8. 验证与验收（DoD，已全部通过）

- ✅ 模拟：策略分类 / 数据集装载（badcase_status 过滤）/ 聚合分层 / 语义物化 46/46；Experiment 页属性注入 19/19；门禁双门槛模拟过；
- ✅ 数据集：`_load_dataset_queries` 返回含 `dataset_item_id` + `expected_output`（真库 badcase/goodcase）；
- ✅ 四维 A/B：prompt label + SKILLS_REF + WREN_SEMANTIC_OVERRIDE 正交生效（skill 物化缺 main/nl2sql SKILL.md 时退磁盘 skill）；
- ✅ db_name 归一化：54/54 模拟含 8 项归一化 + 真库 `chinook_aliyun → Chinook_Aliyun`（策略从 forced B 到可走语义通道）；
- ✅ 端到端：`--dataset badcase/goodcase` + 多臂跑通，`dataset_run_items.create` 成功、run 级分落库、manifest/gate 落盘；Experiment 页实体（`exp-<run_name>`）item 三字段全对（experiment_item_id/trace_id/environment=experiment）；
- ✅ **Expected Output 注入**：重跑 goodcase（`exp-prod-a:head@20260831T222202`）`api.experiments.list_items` 两条 item 的 `expected_output` 均非空（`{"sql": "..."}`）；
- ✅ API 全链路：datasets 枚举含 count、prompt-labels、skill-refs（未 commit → note）、semantic-refs、POST runs → 轮询 done、list（含 running）、DELETE（409/404/200）、北京时间 stamp；
- ✅ 前端：页面 200、提交/轮询/对比/分层/门禁/删除/折叠/运行中回看。

## 9. 关键机制详述

### 9.1 Langfuse Experiment 页写路径（路径 B，2026-08-30/31 已实施）

v4 Experiment 页实体只认带 `langfuse.experiment.*` OTel 属性的 trace。`_experiment_attrs` 用官方私有 `_propagate_attributes(experiment={...})` 把属性注入 OTel context → 本次 trace 的 root span 及所有子 span 继承 → 后端聚合出 Experiment 实体。与 Datasets→Runs（`dataset_run_items`）并行：一条 trace 两个视图都有。

坑：
- `list_items` 过滤必须用 **`experiment_id`**（`experiment_name` 过滤返回空）；
- 复杂 trace 的 item 聚合有延迟（>5s）——只影响 Experiment 页，`dataset_run_items.create` 同步写不受影响；
- graph 必须真正顶层 import（否则 MCP 工具 0 个）。

### 9.2 Expected Output 注入（2026-09-01 修复）

**现象**：goodcase dataset item 有 `expected_output={"sql": "..."}`，但 Experiment 页 Expected Output 列恒空。

**根因**：Experiment 页 Expected Output 只读 root span 的 `langfuse.experiment.item.expected_output` OTel 属性，**不会回落 dataset item 的 expectedOutput**（实测：dataset item 有、experiment item 的 `expected_output=""`）。我们只注入了 `experiment_id/name/dataset_id/item_id/item_metadata`，从未注入 expected_output → 恒空。

**关键坑**：该 key **不在** `_propagate_attributes` 的 experiment 白名单（`PropagatedExperimentAttributes` 无此项），硬塞会被映射到错误的 `langfuse.metadata.experiment_item_expected_output`。必须像 `root_observation_id` 一样在 root span **直接 `otel.set_attribute("langfuse.experiment.item.expected_output", <JSON 字符串>)`**（官方 SDK `client.py:2981` 同款：`_serialize` = str/None 原样、其余 `json.dumps`）。

**修复**：
- `_load_dataset_queries`：读 dataset item `expected_output` 带进 rec（非 None）；
- `_experiment_attrs`：新增 `_serialize_experiment_expected`，在 `_root_backfill` 同一补丁内直写该属性；badcase（无金标）不注入。

**边界**：存量实验（摄入时固化）不回溯，需重新运行；badcase 无金标本就为空（符合预期）。API 路径改 `_prepare_queries` 需重启后端（server 进程内），worker 侧每次子进程新 import 自动生效。

### 9.3 Environment 属性（2026-08-30）

worker 注入 `LANGFUSE_TRACING_ENVIRONMENT=experiment` + `env_loader.py` 按 `DEPLOY_ENV` 推导（prod→production，其余→development）。显式注入恒优先，只作用于 worker 子进程 → 生产后端 trace 不受影响。

## 10. 风险与边界

- `git archive` 需 git 可用：生产 Docker 无仓库 → `SKILLS_REF`/`WREN_SEMANTIC_OVERRIDE` 不设即退化为当前磁盘/HEAD。
- v4 `dataset_items.list` limit≤100 需翻页（已实现）。
- `handler.last_trace_id` 顺序依赖：worker 逐条串行可靠，并发不可用（本实现串行）。
- 每 worker 自起 wrenai server → 内存/启动开销随 arm 数线性，离线可接受。
- cancel 端点不在 MVP（subprocess 树取消复杂），超时 interrupted 兜底。
- 实验跑数据集最新版本（SDK 暂不支持指定 dataset version）。

## 11. 后续项

- 取消运行中的实验（cancel 端点：kill worker 子进程树 + 回写状态）；
- 前端提示门禁阈值语义（双门槛：overall + A 类核心维）；
- 云端自动 evaluation rules（`unstable.evaluators`，非离线，独立课题）；
- skill 内容按 label 从 Langfuse 解析（`--scope prompt|prompt+skill`，M6 基础已具备）。
