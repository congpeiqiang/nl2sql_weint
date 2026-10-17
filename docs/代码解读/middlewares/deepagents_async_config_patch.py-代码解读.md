# deepagents_async_config_patch.py — Monkey-Patch 代码解读

> 源文件：`src/agent/middlewares/deepagents_async_config_patch.py`
> 类别：**Monkey-Patch**
> 挂载点：模块导入时自动生效

## 1. 解决的问题

deepagents 的异步子 agent（通过 `start_async_task` 创建）在独立的线程/协程中运行，**不继承**父 run 的 `configurable`（如 `db_name`、`user_id`）和 Langfuse metadata（`parent_trace_id`、`parent_obs_id`）。

导致：

- 子 agent 不知道当前数据库 → 工具选择错误
- 子 agent 的 Langfuse trace 无法嵌套到主 trace → 观测链断裂
- 子 agent 拿不到 `user_id` → 权限隔离失效

## 2. 核心机制

### 2.1 Monkey-Patch 目标

替换 deepagents 内部的 `_ClientCache.get_sync` 和 `_ClientCache.get_async` 方法：

```python
# 原始方法
original_get_sync = _ClientCache.get_sync
original_get_async = _ClientCache.get_async

# 替换为注入版本
_ClientCache.get_sync = patched_get_sync
_ClientCache.get_async = patched_get_async
```

### 2.2 注入内容

在子 agent 创建时，从父 run 的 context 中提取并注入：

**Configurable 透传**：

| 字段 | 说明 |
|---|---|
| `db_name` | 当前数据库名 |
| `user_id` | 当前用户 ID |
| `thread_id` | 父线程 ID |
| `query_keywords` | 查询关键词 |
| `enable_thinking` | 思考开关 |
| `llm_route` / `llm_model` | 模型路由 |

**Langfuse metadata 透传**：

| 字段 | 说明 |
|---|---|
| `langfuse_parent_trace_id` | 主 trace ID |
| `langfuse_parent_obs_id` | 父 observation ID |
| `langfuse_session_id` | 会话级 session ID |

### 2.3 注入时机

`_ClientCache.get_sync/async` 是 deepagents 创建子 agent client 时的必经路径。Patch 在此处：

1. 读取当前线程/协程的 `langgraph.config.get_config()` 获取父 configurable
2. 读取当前 OTel context 获取 Langfuse metadata
3. 合并进子 agent 的 configurable 和 metadata
4. 调用原始方法创建 client

### 2.4 自动生效

```python
# 模块末尾
_apply_patch()
```

只要在 `create_deep_agent` 之前导入本模块，patch 就自动生效。不需要显式调用。

## 3. 关键设计决策

### 3.1 为什么用 Monkey-Patch 而非继承

- `_ClientCache` 是 deepagents 内部类，不在公开 API 中
- 继承需要替换整个 client 创建流程，侵入性太大
- Monkey-Patch 只改一个方法，最小侵入

### 3.2 为什么必须在 create_deep_agent 之前导入

- `create_deep_agent` 内部立即调用 `_ClientCache.get_sync/async`
- 如果 patch 还没生效，子 agent 就拿不到父 context
- 通常在应用启动时导入（`import agent.middlewares`）

### 3.3 为什么 OTel context 可能丢失

- 异步子 agent 在新的 `asyncio.Task` 中运行
- Python `contextvars` 默认不跨 Task 传播
- 某些执行器（`ThreadPoolExecutor`）不传播 OTel context
- 所以必须显式注入，不能依赖自动传播

## 4. 与其他中间件的协作

| 中间件 | 本 patch 提供的数据 |
|---|---|
| `current_db_context` | `db_name`（子 agent 也能读到） |
| `tool_filter` | `db_name`（子 agent 也能过滤工具） |
| `langfuse_span` | `parent_trace_id`（子 agent trace 嵌套到主 trace） |
| `filesystem_thread_guard` | `thread_id`（子 agent 的线程隔离正确） |
| `thinking_toggle` | `enable_thinking`/`llm_route`（子 agent 用同模型） |

## 5. 风险与限制

### 5.1 deepagents 版本升级风险

- Patch 依赖 `_ClientCache` 的内部实现
- deepagents 升级可能改变方法签名或移动类
- 需要在升级时验证 patch 仍然生效

### 5.2 多重 patch 冲突

- 如果其他模块也 patch `_ClientCache`，可能互相覆盖
- 当前系统中只有本模块 patch 该类

### 5.3 异步 context 传播的边界

- `asyncio.Task` 内的 contextvars 可以传播（Python 3.7+）
- 但 `concurrent.futures.ThreadPoolExecutor` 不传播
- 本 patch 只处理 `_ClientCache` 层面的注入，不解决底层 context 传播问题
