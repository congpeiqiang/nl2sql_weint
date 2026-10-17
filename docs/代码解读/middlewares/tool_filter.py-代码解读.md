# tool_filter.py — ToolFilterMiddleware 代码解读

> 源文件：`src/agent/middlewares/tool_filter.py`
> 类别：**工具面控制**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

多 MCP server 全部加载后，模型看到所有库的工具（`wrenai_clickhouse_run_sql`、`wrenai_imdb_run_sql`、`dbmcp_run_sql`），容易选错工具。需要按当前库过滤，只暴露相关工具。

## 2. 核心机制

### 2.1 过滤规则

每次模型调用前，按 `configurable.db_name` 用 `request.override(tools=...)` 只暴露当前库工具：

| 工具类型 | 过滤规则 |
|---|---|
| `wrenai_<当前库>_*` | **保留** |
| `wrenai_<其他库>_*` | **过滤** |
| `dbmcp_*` | 条件保留（见下文） |
| 非查询工具（图表等） | **保留** |

### 2.2 语义层独占（2026-09-09）

当前库**实际存在** wrenai 工具时，直接不绑 `dbmcp_*`——模型看不见就不会试。

```python
has_wrenai_tools = any(
    name.startswith(wrenai_prefix) for name in all_tool_names
)
if has_wrenai_tools:
    # 语义层独占：移除所有 dbmcp 工具
    filtered = [t for t in tools if not t.name.startswith("dbmcp_")]
```

**为什么用实际工具名而非 `is_modeled()` 配置**：

2026-09-08 预检事故——wrenai server 加载失败时配置仍显示已建模，据此移除 dbmcp 让模型一个查询工具都没有。必须用**实际存在的工具名**判定（fail-open 保留 dbmcp）。

### 2.3 前缀推导

统一走 `semantic_db.wrenai_server_name`（唯一净化源）：

```python
wrenai_prefix = semantic_db.wrenai_server_name(db_name)
# 如 "wrenai_clickhouse" → 匹配 wrenai_clickhouse_run_sql
```

避免中文库名时 `\W+` 不折叠 CJK 导致前缀匹配不上。

## 3. 与 query_gate 的纵深防御

```
tool_filter（预防层）：
    模型看不见不该用的工具 → 不会尝试

query_gate（拦截层）：
    模型仍可能幻觉工具名 → 硬闸兜底
```

"看不见 + 拦得住"是纵深：

- `tool_filter` 减少无效尝试（省一轮 LLM 调用）
- `query_gate` 拦截漏网之鱼（存量历史调用/幻觉工具名）

## 4. 环境变量回退

`NL2SQL_SEMANTIC_EXCLUSIVE_TOOLS=0` 可秒回退旧行为（不启用语义层独占）：

```python
exclusive = os.getenv("NL2SQL_SEMANTIC_EXCLUSIVE_TOOLS", "1") != "0"
if exclusive and has_wrenai_tools:
    # 移除 dbmcp
```

## 5. 关键设计决策

### 5.1 为什么用 request.override 而非修改工具注册表

- `request.override(tools=...)` 只影响本次模型调用
- 注册表不变 → 其他会话/线程的工具不受影响
- 与 `dynamic_mcp_tools` 的注册表层解耦

### 5.2 为什么保留非查询工具

- 图表工具（`create_echarts_visualization`）与库无关
- 文件工具（`read_file`、`write_file`）与库无关
- 只过滤查询相关的工具（wrenai/dbmcp）

### 5.3 与 dynamic_mcp_tools 的协作

```
dynamic_mcp_tools（注册表层）：
    管"哪些工具存在" → 全量注册表

tool_filter（过滤层）：
    管"哪些工具暴露给模型" → 从注册表中按当前库筛选

执行顺序：dynamic_mcp_tools 先跑 → tool_filter 后跑
```

## 6. 错误处理

- db_name 为空 → 不过滤，暴露全部工具
- wrenai 前缀推导失败 → 不过滤，暴露全部工具（fail-open）
- 工具列表为空 → 不过滤（至少保留非查询工具）
