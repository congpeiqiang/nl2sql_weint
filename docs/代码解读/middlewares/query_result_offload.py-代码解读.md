# query_result_offload.py — QueryResultOffloadMiddleware 代码解读

> 源文件：`src/agent/middlewares/query_result_offload.py`
> 类别：**上下文管理**
> 挂载点：`wrap_tool_call` / `awrap_tool_call`

## 1. 解决的问题

**生产故障**（会话 `01a064e1`「有多少部门，每个部门人数」）：nl2sql 子 agent 拿到 431 行 / 42.5KB 的 run_sql 结果后，在最终回复里**重新生成整张 markdown 表**，单次超长生成撞 60s 超时 ×3 = 228s 静默。

**关键认知**：让模型自己 `write_file` 全表行不通（仍要输出 431 行 tokens）——**正确治本 = 代码在工具边界代写**。

## 2. 核心机制

### 2.1 触发条件

仅针对返回 `{columns, rows, row_count}` 的**数据表型**工具（`is_data_tool` 覆盖 `run_sql` 与 `query_cube`）：

- `row_count > 50`（`QUERY_RESULT_OFFLOAD_ROWS`）
- 或文本 > 8000（`LARGE_RESULT_TRUNCATE_CHARS`）

阈值以下行为不变。

### 2.2 落盘

全量 rows 转 markdown 写入进程数据目录：

```
{workspace}/nl2sql_process_data/{thread}/query_result/{qid}_result-{seq}.md
```

- 目录口径与 `langfuse_span._dump_process_data` 同源
- 行数超 10000（`QUERY_RESULT_OFFLOAD_FILE_MAX_ROWS`）只存前 N 行并注明
- 落盘失败 → fail-open 保留原结果

### 2.3 消息瘦身

进 state 的消息替换为瘦身 JSON：

```json
{
    "columns": ["name", "count"],
    "row_count": 431,
    "rows": [{"name": "HR", "count": 15}, ...],
    "rows_truncated": true,
    "full_result_file": "/workspace/nl2sql_process_data/xxx/query_result/abc_result-1.md",
    "note": "该结果表过大（共 431 行），全量数据已由系统写入文件...禁止全表重打..."
}
```

- `rows` 只保留前 20 行样例（`QUERY_RESULT_OFFLOAD_PREVIEW`）
- 样例单元格超 300 字符截断（防行数少但单格超大撑爆预览）
- `note` 内嵌行为指引：只给结论 + 样例 + 文件链接，禁止全表重打 / 禁止 `read_file` 照抄

### 2.4 markdown 转换

`_records_to_markdown(columns, rows, cap)` 把 rows（records dict 或 list[list]）转成全量 markdown 表：

- 单元格安全化：转义管道符 `|`、折叠换行
- 行数超 cap 只保留前 cap 行并注明
- 返回 `(md_text, was_truncated)`

## 3. 注册顺序

必须注册在 `LangfuseSpan` **之前（外层）**——外层后处理在 handler 返回后执行，span/score/process_data 仍吃原始全量 payload，只有进 state 的消息被瘦身。

## 4. 适用工具演进

| 时间 | 覆盖范围 | 问题 |
|---|---|---|
| 2026-09-14 前 | 只认 `run_sql` | Cube 报告缺全量表 |
| 2026-09-14 后 | `run_sql` + `query_cube`（`DATA_TOOL_SUFFIXES`） | Cube 报告也包含全量明细 |

工具判定统一走 `agent.utils.query_tools.is_data_tool`——三处共用（本中间件、check_progress 指针收集、path_resolver 工具超时）。

## 5. 关键设计决策

### 5.1 为什么落盘到 process_data 而非 large_tool_results

- `large_tool_results/` 是 `message_slimmer` 的通用截断目录
- `process_data/{thread}/query_result/` 是结构化查询结果目录
- 前端和 langfuse_span 的产物收集逻辑扫描 `process_data`，能自动发现全量文件
- 路径带 thread_id → 天然隔离不同会话

### 5.2 为什么保留原始 JSON 结构的其他字段

`slim = dict(parsed)` 保留 `statement_count`、`statements` 等元信息字段——模型可能需要知道 SQL 语句数量等上下文。

### 5.3 为什么 note 里写禁止指令

note 随 JSON 一起进上下文，模型会读到。显式的"禁止全表重打"指令比隐式的"rows_truncated=true"更有效——实测模型看到 `true` 仍会尝试 `read_file` 后照抄。

## 6. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `QUERY_RESULT_OFFLOAD_ROWS` | `"50"` | 行数触发阈值 |
| `LARGE_RESULT_TRUNCATE_CHARS` | `"8000"` | 字符数触发阈值 |
| `QUERY_RESULT_OFFLOAD_PREVIEW` | `"20"` | 进 state 保留的样例行数 |
| `QUERY_RESULT_OFFLOAD_FILE_MAX_ROWS` | `"10000"` | 落盘文件最大行数 |
