# NL2SQL 评估精准化设计方案

> 状态：**评审中（v0.1）——2026-09-02 已拍板 Q2/Q3/Q6（见 §11.1），Q6 已执行清理**
> 日期：2026-09-02
> 范围：在线五维评估（schema / sql_valid / exec_success / sql_biz_correct / report_table / analysis_report）从「借工具 span 现场打分」升级为「评估单元 + rubric 工程化 + 校准回路」的可信评估体系。
> 关联文档：[[Langfuse接入实现方案]](../Langfuse接入实现方案.md) §3、[[NL2SQL反馈闭环优化设计方案]](../NL2SQL反馈闭环优化设计方案.md)、[[LangfuseExperiment离线实施方案]](../LangfuseExperiment离线实施方案.md)

---

## 1. 背景与问题

### 1.1 现状

在线评估在 `LangfuseSpanMiddleware._maybe_score`（`src/agent/middlewares/langfuse_span.py`）里随工具 span 收尾触发：

- **确定性维**（`sql_valid_score` / `schema_match_score` / `sql_exec_success`）：span 结束时同步写分，零 LLM、全量、每查询必现。
- **LLM-judge 维**（`sql_biz_correct_score` / `report_table_score` / `analysis_report_score`）：按 `NL2SQL_EVAL_JUDGE_SAMPLE`（默认 0.3）采样，在**后端 daemon 线程**里 fire-and-forget 跑（`src/agent/eval/evaluators.py::schedule_judge`），评审 prompt 硬编码在 Python 源码。

### 1.2 为什么现在评不准（失真源清单）

| # | 失真源 | 后果 |
|---|--------|------|
| F1 | judge 在**工具 span** 上打分：一次查询多次 `run_sql`（探路/核查/最终），span 无「final」标记 | judge 拿探路 SQL 当最终答案，对错颠倒 |
| F2 | 结果 >8KB 时 span output 只留 VFS 占位符（`/workspace/large_tool_results/…`） | 服务端/程序化 judge 读不到数据，无法判断结果 |
| F3 | 报告正文 `write_file` input 被 `_compact(args, 2000)` 压到 2KB | 报告类评审（含幻觉检测）只看得到报告开头 |
| F4 | 问题文本只在 trace metadata `user_question` 属性；judge 若从 input 消息解析 | 深 agents/续跑场景取到系统通知或上一个问题 |
| F5 | rubric 是一句笼统话，无分档锚点 / 无 few-shot / 硬编码在源码 | judge 靠猜，改口径要发版 |
| F6 | 采样现场判定、失败即丢、无补评通道 | 「该评没评」与「评了=差」无法区分，无法事后回评 |
| F7 | 进程被杀前 SDK 分数未 flush、daemon 线程被掐 | 最近窗口的分数/评估丢失（无停机 flush 钩子，仅 `run_experiment.py` 一处手动 `flush()`） |

### 1.3 PoC 实证结论（2026-09-02，自托管 25.64 项目 `cmtcfnowb0008nu071sqx1muv`）

- Langfuse 侧 LLM-judge 机制**可用**：worker 用项目默认 judge 模型（bailianyun / qwen3.7-max）真实执行，目标事件到达 ~2s 自动触发，score+reasoning 落 trace，每次执行有独立可观测 trace。
- 项目内已存在 2 个 LLM_AS_JUDGE evaluator（08-29 建）：`nl2sql-evaluator`（手写 rubric：schema/question/标准SQL/预测SQL，rule INACTIVE）与 `SQL Semantic Equivalence`（官方模板，rule 曾 ACTIVE sampling=3.9% filter env=production，**映射坏的**——judge 收到整段对话 JSON，同查询评出 0/0.9/15/90 噪声分；rule 已于 2026-09-02 置 INACTIVE）。
- **结论**：跑在哪一边不是准不准的瓶颈；瓶颈是 judge 的**输入完整性**（F1~F4）与**评分标准**（F5），以及缺少**对 judge 本身的标定**。

---

## 2. 目标与范围

### 2.1 目标

1. 让每条被评估查询的 **judge 输入干净、完整、无歧义**（评估单元）。
2. 让每个语义维度的 **评分标准可读、可分档、可改不发版**（rubric 托管）。
3. 让「准」**可测量、可回归、可持续**（校准回路 + 门禁）。
4. 解决评估的**交付可靠性**：进程挂掉不丢、未评可补评、存量可回评。
5. 确定性规则保持**全量、零成本**，语义层 **可控成本采样**。

### 2.2 名词表

| 名词 | 含义 |
|------|------|
| 评估单元（Evaluation Subject） | 一次查询「该被评的内容」的结构化集合：question/final_sql/exec_status/result_summary/final_answer/report |
| 最终 SQL 标记（final flag） | run_sql span 上标识「这条 SQL 产出最终答案」的属性 |
| 回归集 | 带人工标注的查询样本（badcase/goodcase dataset），用于标定与门禁 |
| 一致率 | judge 判定与人工标注在二值（好/坏）上的相符比例，校准的量化指标 |
| rubric | 评分标准（分档锚点 + 坑 + few-shot + 输出 schema），以 Langfuse prompt 承载 |

### 2.3 非目标

- 不做「端到端答案与规范标准 SQL 的逐条等价校验」——线上无 ground-truth SQL，等价校验留给 Dataset/实验（有 `expected_output`）。
- 不做 SQL 执行正确性的 LLM 评审——由确定性规则承担。
- 本期不动 UI/看板消费侧（Score Analytics 用法另行设计）。

---

## 3. 总体架构

```
                         ┌─────────────────────────────────────────────┐
   每次查询完成            │  ① 评估单元组装（后端，查询收尾钩子）           │
   ────────────────────► │  question + final_sql + result_summary +     │
                         │  final_answer + report                        │
                         └───────────────┬─────────────────────────────┘
                                         │
              ┌──────────────────────────▼──────────────────────────┐
              │  ② 三明治评分                                        │
              │   · 确定性规则（全量、零成本）：sql_valid/schema/exec  │
              │   · LLM-judge（采样，评评估单元，rubric 拉 Langfuse） │
              └──────────────┬──────────────────────────┬───────────┘
                             │ Score                    │ 待评队列（sqlite 持久化）
                             ▼                          ▼
                     Langfuse Score             ④ 补评/回评 worker
                         │
              ┌──────────▼──────────────────────────────────────────┐
              │  ③ 校准回路（离线，周期性）                          │
              │   · 回归集（人工标注）重跑 judge → 一致率 / 阈值        │
              │   · rubric / 模型 / 埋点变更先过门禁再上线              │
              └─────────────────────────────────────────────────────┘
```

三明治评分 + 评估单元解决 F1~F5；待评队列/停机 flush 解决 F6~F7；校准回路让分数「有含义」。

---

## 4. 设计一：评估单元（Evaluation Subject）

### 4.1 数据模型

一次查询（含多轮、子 agent、异步任务）收尾后组装为一条结构化记录：

```jsonc
{
  "subject_id": "<chat-turn trace_id>",
  "ts": "2026-09-02T03:02:31Z",
  "question": "有多少员工",                  // ← metadata.user_question
  "db_name": "WIT运营管理平台数据库",
  "final_sql": "SELECT COUNT(...) ...",     // ← final 标记的 run_sql
  "exec_status": { "ok": true, "error": "" },
  "result_summary": {                        // ← 始终携带，即使超长
    "row_count": 1,
    "head_rows": [["employee_count", "190"]],
    "note": "head-10/truncated-at-8000"
  },
  "final_answer": "当前数据库中共有 190 名在职员工。…",  // ← 主 agent 回答
  "report": null | { "path": "/workspace/report/xxx.md", "text_head": "…" },
  "task_type": "sync" | "async-subagent",
  "skill_tags": ["nl2sql-sql-of-thought"]
}
```

- **线上无 ground-truth SQL**，故语义评审针对「final_answer / result_summary 是否满足 question 意图」，而非与规范 SQL 等价。
- **报告类**（report_table / analysis_report）额外要求 report 全文可达（见 4.2 埋点 C）。

### 4.2 埋点改造点（集中在 `src/agent/middlewares/langfuse_span.py` + MessageSlimmer 对齐）

| 改造 | 内容 | 对应失真 |
|------|------|---------|
| A. final 标记 | 对产出最终答案的 run_sql span 在 metadata 加 `"final": true`（识别信号：该 run_sql 后无后续 run_sql 且其输出进入最终回答 / 或按现行 `_extract_last_sql` 语义在 span 级落标）。`probe`/`dry_run`/核查型 SQL 显式排除 | F1 |
| B. 结果摘要常驻 | run_sql span 的 output 在超长截断时**仍保留** `{row_count, head_rows(≤10), truncated}` 摘要，而非只有 VFS 占位符（对齐 MessageSlimmer 落盘阈值，两处同源） | F2 |
| C. 报告正文可达 | write_file(报告) span：放宽 input 上限或把全文写盘并在 metadata 落 `report_text_path` + 头部摘要；judge 需要全文时后端进程直读 | F3 |
| D. question 属性化 | 评审只消费 trace metadata `user_question`；不再从 input 消息解析 | F4 |
| E. 查询收尾钩子 | 组装评估单元：挂主 agent run 收尾（现有 `_THREAD_TRACE_MAP` / auto-continue 归并之后），写盘 + 提供给评分层 | 总装 |

> 说明：A~C 同时让评估单元**未来可作为 observation 落进 trace**，使 Langfuse observation 级 evaluator 可直接过滤命中（为第 8 节「可全迁」留路）。

### 4.3 单元落点

- 运行期：后端内存 dict 直供评分层（code-judge 需要运行时全量）。
- 持久化：写 `{active_workspace}/nl2sql_process_data/{thread_id}/eval-subject/{trace_id8}.json`（与现有 process_data 同构），失败仅 debug 不影响主流程。
- 视需要再同步为 chat-turn trace 下的一条结构化 span（`name="eval-subject"`，output=单元 JSON），供 Langfuse 侧评估器消费。

---

## 5. 设计二：三明治评分

| 层 | 维度 | 方式 | 覆盖 | 成本 |
|----|------|------|------|------|
| 确定性 | `sql_valid_score` | `classify_sql` 规则 | 全量 | 0 |
| 确定性 | `schema_match_score` | 发现成功/失败基础分 | 全量 | 0 |
| 确定性 | `sql_exec_success` | 执行成功 + `looks_like_exec_error` | 全量 | 0 |
| LLM-judge | `sql_biz_correct_score` | 评**评估单元**：question×final_answer×result_summary | 采样 | LLM |
| LLM-judge | `report_table_score` | 评 report 数字/表格 vs final_sql+result_summary | 采样（报告类可提权） | LLM |
| LLM-judge | `analysis_report_score` | 幻觉检测：report 每个断言需可由 final_sql+结果支持 | 采样（报告类可提权） | LLM |

要点：
- 确定性维维持现状（规则能确定的绝不上 LLM），纳入第 8.2 停机 flush。
- LLM-judge **只评评估单元**，不评原始 span；打分失败/超时进待评队列（8.3），不再「现场失败即丢」。
- 采样率默认保持 0.3；**报告类建议提权到 1.0**（高价值、幻觉风险高、量小）。采样参数仍 `NL2SQL_EVAL_JUDGE_SAMPLE`，报告类单独 `NL2SQL_EVAL_REPORT_SAMPLE`。
- 执行失败/被拦截的查询（确定性低分命中）**不跑语义 judge**（现状保留，省成本；它们是 BadCase 采集的确定依据）。

---

## 6. 设计三：Rubric 工程化与 Langfuse 托管

### 6.1 rubric 结构（统一模板，逐维度一份）

```markdown
# Role
你是 NL2SQL 语义评审员，评审对象是一次查询的「问题 × 最终答案 × 依据（SQL+结果）」。
# 输出
严格 JSON：{"score": 0~1, "reason": "一句话"}
# 分档锚点
1.0  结果正确回答意图，无缺漏
0.6~0.8  口径偏：如该去重未去重 / 漏在职筛选 / 汇总口径不一致
0.3  答非所问 / 结果空或报错但流程走通
0    编造数据 / 明显错误
# 已知坑（不得据此判错）
- NL 表名单复数归一化（「artists」→ artist）不算错
- 异步子任务：答案在 check_async_task 结果里，属正常链路
- 摘要对但缺明细，不因"没给明细"降 0
# Few-shot（真实标注样本，每个分档 1~2 例）
...
```

- **分档锚点 + 坑 + few-shot** 直接消灭 F5 的「靠猜」。few-shot 从已标注 goodcase/badcase 取（见 §7）。
- 每个语义维一份：`judge/sql-biz-correct`、`judge/report-table`、`judge/analysis-report`（后续可并入实验评估器 `judge/semantic-equivalence`，用 {{standard_sql}}）。
- **输出强制 JSON schema + temperature 0**；解析失败按「无法判定」进待评队列重试一次，再失败置 `unverifiable`（与 BadCase 的 invalid 状态区分）。

### 6.2 存储与版本

- 复用 M4 prompt 管理：`get_prompt_text(name="judge/sql-biz-correct", label=…)` 拉取，失败回退本地文件（本地放 `src/agent/shared/prompts/judge/*.md`）。
- label 与系统 prompt 同体系（`production` / canary A/B），UI 改 rubric 即时生效、可回滚、可灰度——**改评审口径不再发版**。
- rubric 变更走 §7.4 门禁。

### 6.3 维度特化要点（评审口径，待评审确认）

- `sql_biz_correct`：以「final_answer 是否满足 question 意图」为核心，result_summary 佐证；不看中间探路 SQL。
- `report_table`：报告内每个数字/表格单元格须能由 final_sql+result_summary 定位支持；口径（在职/去重）要与 question 一致。
- `analysis_report`：幻觉检测——报告中出现的**事实断言**逐条要求「能在依据里找到」，找不到即幻觉，按比例扣分并给出错句摘录进 reason。

---

## 7. 设计四：校准回路（让「准」可测量）

### 7.1 真值来源

- 人工标注存量已具备：前端 👍👎（goodcase/badcase dataset）+ 标注页 gold_sql/bad_type。
- 新增约定：人工标注过的 trace，同时记录「judge 当时给的分」→ 累积 (question, judge_score, human_label) 对齐样本。

### 7.2 回归集与一致率

- 回归集 = badcase/goodcase 两个 dataset 的已标注子集（分语义维）。
- 周期（或 rubric/模型变更前）在该集上重跑 judge，输出指标：
  - **一致率**：judge 二值判定（阈值切分）与人工标注相符比例；
  - **阈值曲线**：找使一致率最高的分值切点；
  - **分箱误差**：人工好/坏在 judge 0~0.4 / 0.4~0.8 / 0.8~1 各箱的分布（看系统性偏差）。
- 一致率低即说明 rubric/模型/输入有系统性问题，先修再上线。

### 7.3 阈值标定

- 由回归集决定「多少分以上算 BadCase 候选」「多少分以上算好」的切点，替代拍脑袋；切点随 rubric 版本记录。
- 校准结果回写为文档 + 一份可查询的对照（如 dataset: judge-calibration）。

### 7.4 变更门禁流程

rubric 修改 / judge 模型切换 / 埋点（final 标记等）改动：

1. 拉回归集 → 重跑 judge → 计算一致率；
2. 一致率 ≥ 门槛（初值 0.85，对人工好/坏二值；报告维单列）→ 放行；否则打回；
3. 放行后对**存量目标集回评一次**（§8.3），保证新口径下历史分数一致可读。

---

## 8. 设计五：归属地与执行可靠

### 8.1 运行归属（hybrid → 可全迁）

| 场景 | 归属 | 理由 |
|------|------|------|
| 在线单查询语义打分 | **后端**（code-judge 评评估单元） | 进程内有运行时全量（final_sql/结果/回答），第 4 节改造就地受益；rubric 已迁 Langfuse prompt |
| 实验 / 批量 / 回归回评 | **Langfuse LLM-judge evaluator**（数据集有 ground truth） | `nl2sql-evaluator` rubric 形态匹配；worker/自动触发已验证 |
| 未来的在线 observation 级 | 评估单元落 trace 后**可整体迁 Langfuse** | §4.2 A~C 已铺路 |

决策依据（PoC）：机制两边都通，**先保输入与标准，再谈搬**。

### 8.2 优雅停机 flush

- 在 app 生命周期挂显式 `flush()`：uvicorn `lifespan`（shutdown 段）+ `atexit` 兜底（`src/api/…` 启动入口处 `get_client().flush(timeout=…)` 一次）。
- 覆盖：确定性分、judge 分、feedback score 等一切 `create_score`/trace 批量上送窗口；修复 F7。

### 8.3 落盘待评队列（幂等 / 补评）

- judge 请求先入 **sqlite 待评队列**（`{data_root}/eval_queue.sqlite`：subject_id+kind 唯一，state=pending/running/done/unverifiable），后台 worker 拉取执行；
- 幂等：`subject_id+kind` 已 done 则跳过 → 进程重启/挂掉后 pending 自动续跑（补评）；daemon 线程不再承载「唯一一次机会」；
- 运维工具：`eval_worker.py --replay --subject <id>` 对指定/全部存量 subject 回评（改 rubric 后一键回评，§7.4 第 3 步）。

---

## 9. 变更清单（文件级）

| 文件 | 变更 |
|------|------|
| `src/agent/middlewares/langfuse_span.py` | final 标记、结果摘要常驻、报告正文可达、query 收尾组装评估单元 |
| `src/agent/middlewares/message_slimmer.py`（若独立） | 截断阈值与结果摘要同源 |
| `src/agent/eval/evaluators.py` | judge 改为消费评估单元 + 读 Langfuse rubric（`get_prompt_text`），失败入队 |
| `src/agent/eval/eval_queue.py`（新） | 落盘待评队列 + worker（幂等/回评） |
| `src/agent/eval/calibrate.py`（新） | 回归集一致率/阈值标定脚本 |
| `src/agent/eval/run_experiment.py` | 复用 rubric；支持一致性门禁报告 |
| `src/agent/shared/prompts/judge/*.md`（新） | rubric 本地兜底文件（同 Langfuse prompt 内容） |
| `src/agent/trace/langfuse_client.py` | （无改，复用 `get_prompt_text`/`flush`） |
| 后端启动入口（`start_server.py` / custom app lifespan） | 优雅停机 flush 注册 |
| `docs/langfuse平台/*` | 校准阈值、门禁结果记录 |

> **P0 已实施（2026-09-02，本地验证通过）**：上表为 P1~P4 的规划变更。P0 实际落地文件：
> `src/agent/eval/eval_queue.py`（新，落盘队列 + 单例守护 worker，key 现为 `trace_id+kind`，subject 模型待 P1）+ `evaluators.py`（`schedule_judge` 改入队、抽 `_execute_judge_task`，签名不变；`fetch_question` 加 `trust_env=False` 防代理劫持）+ `langfuse_client.py`（`flush_langfuse` + `atexit` 兜底）+ `src/api/custom_app.py`（Starlette lifespan 停机 flush）。

> 前端无改动；Langfuse UI 侧改动见 §8.1 表（建 rubric prompt / 实验评估器），评审通过后再配置。

---

## 10. 分期实施

| 期 | 内容 | 验收 |
|----|------|------|
| **P0 可靠交付** | 优雅停机 flush + judge 落盘待评队列（幂等） | 进程重启后 pending 自动续跑不丢分；模拟 kill 验证补评 → ✅ **2026-09-02 已实施**（本地模拟 6 场景全过；fetch_question 代理劫持隐患一并修复；生产待发版+重启验证） |
| **P1 评估单元收口** | final 标记、结果摘要、报告正文、question 属性、收尾组装单元 | 抽样 N 条真实查询人工核对单元字段齐全/正确；探值 run_sql 均无 final → **片1（评估单元脊柱）已实施（2026-09-02，本地验证 39/39 过）**：新增 `src/agent/eval/eval_subject.py`（纯逻辑组装/落盘）+ `langfuse_span.py` 工具边界证据 sidecar（run_sql 完整数字载荷/report 正文头部）+ 主 agent `after_agent` 收尾组装写 `{workspace}/nl2sql_process_data/{session}/eval-subject/{subject8}.json`；final_sql 复用 check 内嵌 `sql`（=`_extract_last_sql`，天然排除探值/核查）；Q1 抽样核对与 E2E 待后端发版+重启后在 192.168.25.64 真跑 |
| **P2 rubric 工程化** | rubric 迁 Langfuse prompt（3 维）+ 本地兜底；code-judge 改读 | UI 改 rubric 不发版生效；回滚 label 生效 |
| **P3 校准回路** | 回归集标定脚本 + 一致率/阈值报告 + 门禁流程 | 对 badcase/goodcase 出一期一致率报告，给出阈值切点 |
| **P4 报告维提权 + 回评工具** | 报告采样 1.0、`eval_worker --replay` | 报告维一致率达标；rubric 改动能回评存量 |

每期独立可上线、可回滚；P0/P1 优先级最高（它们决定后续一切可信度）。

---

## 11. 决策记录与仍开放问题（2026-09-02 评审）

### 11.1 已定决策（评审拍板）

| # | 决定 | 落地状态 |
|---|------|---------|
| Q2 | `schema_match_score` 的 LLM 语义补充分支**并入 `sql_biz_correct`**（SQL 对则 schema 基本对），不再单设 schema 语义 judge；确定性 `schema_match_score`（发现成功/失败基础分）保留 | 已写入 §5/§6 口径 |
| Q3 | 默认 judge 模型 = **Ollama（私有 qwen3.8:27b）**，数据不出内网 | ✅ **生产已生效**：`default_llm_models` = ollama-weint/qwen3.8:27b（llm_api_key `cmtjrvnb4…`）；bailianyun 连接保留，per-evaluator 可显式覆盖 |
| Q6 | 清理存量噪声分（`SQL Semantic Equivalence`，rule 已停） | ✅ **2026-09-02 已执行**：ClickHouse `scores` 删除 7 条（source=EVAL，含同 trace 四连发 1/15/90/0.9 的 335c89…）；备份 `sql_semantic_equiv_noise_backup.json`（907B）；评估执行 trace 保留作审计 |

> Q3 注意：qwen3.8:27b 弱于外网 qwen3.7-max，两模型口径差异仍需 P3 回归集标定验证（§7.4 一致率门禁不变）；报告维/复杂 SQL 若分数系统性偏低，对具体 evaluator 显式覆盖回 bailianyun。
> Q6 依据：删除目标 = `scores` 中 `name='SQL Semantic Equivalence' AND source='EVAL'`，`analytics_scores` 为建在其上的 VIEW（无副本），一次删除即全清。

### 11.2 仍开放问题

| # | 开放问题 | 影响 | 建议 |
|---|---------|------|------|
| Q1 | final 标记的判定信号（"该 run_sql 后无后续 run_sql 且进入最终回答"）在异步子任务下是否可靠 | 直接决定 F1 修复质量 | P1 落地时抽样核对；必要时在子 agent 收尾显式写 final |
| Q4 | 人工标注量不足以支撑回归集（尤其报告维） | 校准无米下锅 | 前期用 goodcase/badcase 存量 + 标注页增量；报告维单独攒 |
| Q5 | 采样默认 0.3 是否够支撑「系统级质量趋势」 | 看板统计噪声 | 趋势口径可整体 100% 确定性分 + 语义分层抽样 + 每周回评补齐 |

---

## 12. 附录

### 12.1 PoC 证据摘要（2026-09-02）

- 项目默认 judge 模型：bailianyun/qwen3.7-max（`llm_api_keys` + `default_llm_models` 确认；Ollama 连接 ollama-weint qwen3.8:27b @192.168.25.13 在列）。
- worker 执行实证：执行 trace `edf7956f…`（name=`Execute evaluator: SQL Semantic Equivalence`），内嵌 GENERATION `chat qwen3.7-max`；目标 trace `18e06a2d…` 到达 2s 后自动触发；score 带 reasoning 落 trace。
- 真实查询 trace 结构：`729e11d2…`（"有多少员工"）run_sql SPAN 名 `skill:…:*_run_sql`，input={sql}、output=结果，与主 chat-turn 同 trace → observation 级 evaluator 可过滤。
- 映射坏样本：judge 收到整段对话 JSON，同查询评出 0 / 0.9 / 15 / 90。
- 既有配置：evaluators `nl2sql-evaluator`（cmtdxf6i1…，rule INACTIVE）、`SQL Semantic Equivalence`（cmtdxhau5…，rule cmtdxhw8w 已置 INACTIVE）；evaluation_rules/evaluator_versions/job_executions 表结构已核实。

### 12.2 相关记忆与文档

- 代码评审 rubric：`NL2SQL反馈闭环优化设计方案.md`、`Langfuse接入实现方案.md` §3
- 离线实验：`LangfuseExperiment离线实施方案.md`；`src/agent/eval/run_experiment.py`
- 部署：`nl2sql 部署环境 weint`（192.168.25.64）
- judge 假阴性已知坑：NL 表名归一化、异步子任务 check_async_task 链路、trace 摘要 Bug B（_extract_last_sql 取错 run_sql）
