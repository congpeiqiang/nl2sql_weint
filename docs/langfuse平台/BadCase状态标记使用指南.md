# BadCase 状态标记使用指南

> 配套文件：`src/agent/eval/badcase_status.py`
> 状态存储：`{workspace}/eval/badcase_status.json`（本地 JSON，不动 Langfuse 源码）

---

## 一句话说明

每天 cron 自动采集差评/低分会话为 badcase → 你 **review 看一眼** → 标记 fixed/invalid → 回归测试自动跳过已关闭的 → 闭环完成。

## 完整流程（每天只需 1 条命令）

```
每天 02:13 cron 自动跑 collect_badcase + feedback_gate + badcase_status summary
                         ↓
        server_collect_badcase.log 末尾显示「pending: N 条」
                         ↓
        N > 0 → 跑 review 命令，逐条看 + 标记
        N = 0 → 什么都不用做
```

---

## 真实样例

### 样例 A：真问题（需要修）

```
trace_id:   9e0ad442c83044bc9504ed16c3e61356
用户问题:   参演了最多不同类型电影的演员 Top 10，显示演员名和涉及的类型数
数据库:     imdb
触发原因:   sql_biz_correct_score = 0.00
```

**发生了什么：**

用户提了一个合理的 SQL 查询需求。Agent 生成了 SQL 并执行了，但 SQL 的业务语义不正确（比如 JOIN 关系搞错、聚合逻辑不对、排序字段选错），导致 `sql_biz_correct_score` 判 0 分。

**你看到后做什么：**

1. 打开 Langfuse trace（`review` 命令自动打开），看 Agent 生成的 SQL 和实际结果
2. 判断：SQL 确实有错，是 prompt 或 skill 的问题
3. 修代码/prompt
4. 标记：

```bash
python -m agent.eval.badcase_status mark 9e0ad442 fixed --note "修复了 JOIN 逻辑，name 应连 person 表"
```

**闭环验证：**

下次 `run_experiment --from-badcase` 自动跳过这条。下次 `collect_badcase --days 1` 重跑，如果该问题不再出现 → 说明确实修好了。

---

### 样例 B：非真问题（误报，标记 invalid）

```
trace_id:   707b335d8f0ea18d19a52398b8fc8b18
用户问题:   你好
数据库:     imdb
触发原因:   user_feedback = 0（用户点了👎）
```

**发生了什么：**

用户在对话里说了「你好」，然后点了👎差评。系统按规则（user_feedback=0）自动采集为 badcase。但这不是模型的问题——「你好」不是数据查询，Agent 正常回复了问候语，用户差评可能是测试行为。

**你看到后做什么：**

1. `review` 命令自动打开 trace，看到用户只是说了「你好」
2. 判断：这不是 SQL/Agent 的质量问题，是误报
3. 标记：

```bash
python -m agent.eval.badcase_status mark 707b335d invalid --note "用户测试差评，非模型问题"
```

**效果：**

这条从回归集中移除，后续不再浪费评测资源。

---

### 样例 C：另一种误报（非查询意图）

```
trace_id:   fdd0e697799df79c195d69bfb81557fa
用户问题:   停止任务
触发原因:   report_table_score=0.00, analysis_report_score=0.00
```

用户说了「停止任务」，这不是数据查询也不是报表需求，所以报表分数为 0 是正常的。标记 `invalid`。

---

## 命令速查

```bash
# 交互式逐条复审（最常用，自动打开 Langfuse trace）
python -m agent.eval.badcase_status review

# 列出待处理的
python -m agent.eval.badcase_status list --status pending

# 标记状态（支持前缀匹配，12 字符即可）
python -m agent.eval.badcase_status mark <trace前缀> fixed --note "原因"
python -m agent.eval.badcase_status mark <trace前缀> invalid --note "原因"
python -m agent.eval.badcase_status mark <trace前缀> reviewed

# 看整体分布
python -m agent.eval.badcase_status summary
```

### review 交互键位

| 按键 | 含义 | 标记为 |
|---|---|---|
| `f` | 已修复 | fixed |
| `i` | 误报/非真问题 | invalid |
| `r` | 确认是问题，待修 | reviewed |
| `s` | 跳过，下次再看 | （不变） |
| `q` | 退出 | — |

## 状态流转图

```
                  pending（新采集，自动注册）
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
    reviewed     fixed       invalid
   (确认待修)   (已修复)    (非真问题)
        │
        ├──→ fixed    (修好了)
        └──→ invalid  (发现不是问题)

fixed / invalid = 已关闭 → run_experiment 回归集自动跳过
```

## 关键设计

| 项目 | 说明 |
|---|---|
| **不改 Langfuse** | 状态追踪全在本地 JSON，只用 Langfuse 公开 API 读写 |
| **自动注册** | `collect_badcase` 采集新 item 时自动标记 pending，已有状态不覆盖 |
| **回归过滤** | `run_experiment --from-badcase` 默认只加载 pending + reviewed |
| **前缀匹配** | `mark` 命令只需输入 trace_id 前 12 位（够唯一定位即可） |
| **每日日志** | cron 末尾自动输出 summary，扫一眼就知道有没有待处理 |
