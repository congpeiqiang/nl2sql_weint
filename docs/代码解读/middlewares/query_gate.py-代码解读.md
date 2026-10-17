# query_gate.py — QueryGateMiddleware 代码解读

> 源文件：`src/agent/middlewares/query_gate.py`
> 类别：**执行监督**
> 挂载点：`wrap_tool_call` / `awrap_tool_call`

## 1. 解决的问题

nl2sql 子 agent 查询执行前的**双规则确定性闸门**：

1. **通道硬闸**：已建模库禁止走 dbmcp 通道（生产 trace 实证：WIT 库上模型直连 `dbmcp_run_sql` ×21 次）
2. **顺序软提醒**：确保模型先获取语义上下文再执行 SQL

## 2. 规则一：通道硬闸（每次必拦）

### 2.1 判定逻辑

当前查询库**已在 Wren 语义层建模**时，`dbmcp_*`（`run_sql` 和 `get_db_info` 都算）一律不执行：

```python
# 目标库解析优先级
1. 工具参数 db_name（显式指定）
2. 从 state 系统提示里读 dynamic_prompt 注入的标记：
   "当前数据库: `X` —— 已在/未在 Wren 语义层建模"
```

### 2.2 行为

| 条件 | 行为 |
|---|---|
| 已建模库 + dbmcp 工具 | 拦截，返回指到 `wrenai_<库>_run_sql` 的 error 消息 |
| 已建模库 + wrenai 工具 | 放行 |
| 未建模库 + dbmcp 工具 | 放行（dbmcp 是唯一查询通道） |
| 未建模库 + wrenai 工具 | 不存在（工具已被 tool_filter 过滤） |

### 2.3 豁免

Cube 通道三工具（`query_cube`、`cube_*`）完全豁免——Cube 有独立路由逻辑（`cube_route_gate`）。

## 3. 规则二：顺序软提醒（每线程至多一次）

### 3.1 触发条件

`run_sql` / `dry_run` / `dry_plan` 执行前，若历史所有 assistant tool_calls 中**从未出现**任何"获取工具"（`_ever_fetched`）：

```python
_FETCH_TOOLS = {
    "get_context", "get_all_knowledge", "get_instructions",
    "recall_queries", "describe_schema", "get_mdl",
    "list_models", "get_db_info",
}
```

含同批次并行的 `get_context`——天然放行同批并行调用。

### 3.2 行为

拦下并给出 nl2sql-understand 顺序指导：

```
建议：在执行 SQL 之前，先调用 get_context 或 get_all_knowledge 获取业务口径，
再调用 describe_schema 获取表结构信息。这样可以生成更准确的 SQL。
```

### 3.3 一次性提醒

`_REMINDED` set（带锁 + 4000 上限）记录 thread_id，提醒一次后放行，不无限弹回、不 interrupt。

## 4. 状态管理

| 进程级缓存 | 键 | 值 | 上限 |
|---|---|---|---|
| `_REMINDED` | thread_id | True | 4000 条后全清 |

## 5. 关键设计决策

### 5.1 硬闸 vs 软提醒

- **通道硬闸**：每次必拦，确定性规则，模型无法绕过
- **顺序软提醒**：每线程一次，引导性建议，提醒后放行

设计哲学：安全规则必须硬（不能走错通道），质量规则可以软（模型有时确实不需要获取上下文）。

### 5.2 为什么 db_name 解析有两层

- 工具参数 `db_name` 是显式的、最权威的
- 系统提示标记是兜底的（工具可能没传 db_name）
- 两层都取不到 → fail-open 放行

### 5.3 fail-open 的边界

- thread_id 取不到 → 不弹软提醒（不影响硬闸）
- 库标记取不到 → 放行（不拦截，让 query_result 自然报错）
- 硬闸的库标记解析失败 → 放行（宁可漏拦也不误拦正确的查询）

## 6. deny 消息设计

```
Error: 当前数据库 `{db_name}` 已在 Wren 语义层建模，
请使用 `wrenai_{db_name}_run_sql` 工具执行 SQL，不要使用 dbmcp_run_sql。
```

消息用中文、指明替代工具名——让模型一次修正到位。`status="error"` + `Error:` 前缀让 `langfuse_span` 把它记为 exec 失败而非成功。
