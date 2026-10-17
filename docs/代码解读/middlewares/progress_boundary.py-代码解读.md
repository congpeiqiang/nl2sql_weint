# progress_boundary.py — ProgressBoundaryMiddleware 代码解读

> 源文件：`src/agent/middlewares/progress_boundary.py`
> 类别：**执行监督**
> 挂载点：`after_model`

## 1. 解决的问题

实测长查询模型只在开头初始化 + 结尾一次全勾 `write_todos`，中间 5-6 分钟不动 → 前端任务卡冻结最旧快照。提示词纪律已证伪（模型不遵守"中途更新"指引），必须**确定性兜底**。

## 2. 核心机制

### 2.1 触发时机

`after_model`——在本轮工具执行**之前**运行，看到模型刚发出的 `tool_calls`。

### 2.2 判定流程

1. 本轮 AI 消息已含 `write_todos` → **跳过**（尊重模型，不打架）
2. 按工具名分桶判阶段：

| 桶 | 工具 | 语义 |
|---|---|---|
| `KNOW` | `get_all_knowledge`, `get_context`, `recall_queries`, `get_instructions` | 知识检索 |
| `SCHEMA` | `describe_schema`, `get_mdl`, `list_models`, `get_db_info` | 结构探索 |
| `EXEC` | `run_sql`, `query_cube`, `dry_run`, `dry_plan` | 查询执行 |

注意：`get_context` / `recall_queries` 归 KNOW 不归 SCHEMA——否则 merged 理解建模第一步就误勾掉第一个 todo。

### 2.3 里程碑推进

`_seen_bucket`：按线程单调记录已见最高桶（跨轮保留）：

- **只有首次到达新里程碑才推进**（纠错循环重跑 schema/run_sql 不重复触发）
- 带锁 + 2000 上限清空防泄漏

### 2.4 advance_todos 逻辑（纯函数）

**SCHEMA 里程碑**：
- 当前步 → `completed`
- 下一未完成步 → `in_progress`
- 当前已在 schema 语义项 → 不动

**EXEC 里程碑**：
- 跳过纯推理步
- 把 `in_progress` 起到「查询执行」语义项之前全 `completed`
- 该项 → `in_progress`
- 找不到语义项 → 保守前进一步

### 2.5 保守单向

```
pending → in_progress → completed
```

绝不提前全勾最后阶段。状态异常（>1 个 `in_progress`）或匹配不中 → **不动**（退化现状不更糟）。

## 3. 进度文件同步

推进成功后同步调 `record_todos_progress` 写进度文件，供 sync 端耗时 / step_history 连续。前端轮询 `/run-status` 时读取此文件展示进度条。

## 4. 关键设计决策

### 4.1 为什么用 after_model 而非 wrap_tool_call

- `after_model` 看到模型**刚发出的所有 tool_calls**，可以一次性判断阶段
- `wrap_tool_call` 每次只看到一个 tool_call，无法全局判断
- `after_model` 在工具执行前运行，推进的 todo 状态能被后续工具节点读到

### 4.2 为什么 KNOW 和 SCHEMA 分桶

- 知识检索（KNOW）和结构探索（SCHEMA）是不同的进度阶段
- `get_context` 既是知识检索又涉及 schema linking → 归 KNOW
- 如果把 `get_context` 归 SCHEMA，merged 理解建模的第一步就误勾掉

### 4.3 为什么跨轮保留 _seen_bucket

- auto-compress 会裁剪消息 → 重扫消息列表会看不到之前的工具调用
- 跨轮保留确保里程碑不后退
- 2000 上限防止线程累积导致的内存泄漏

## 5. 状态管理

| 进程级缓存 | 键 | 值 | 上限 |
|---|---|---|---|
| `_seen_bucket` | thread_id | 已见最高桶（KNOW/SCHEMA/EXEC） | 2000 条后全清 |

共用 `threading.Lock` + 容量上限清空模式。

## 6. 错误处理

- 消息列表为空 → 不推进
- todos 状态异常（多个 in_progress） → 不动
- 语义项匹配不中 → 保守前进一步
- `record_todos_progress` 异常 → 记 warning，不影响主流程
