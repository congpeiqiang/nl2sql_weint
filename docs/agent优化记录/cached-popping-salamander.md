# 方案 D 实施计划：后端状态注入（零前端改动）

## Context

当前子智能体 (nl2sql_agent) 运行在独立线程中，其进度数据虽然已通过 `ProgressTrackerMiddleware` + `write_todos` 生成，但只有主智能体主动调用 `check_async_task` 时才会展示。方案 D 通过后台同步进程，将子智能体的 todos 自动合并写入主智能体的 state，前端任务进度条（`TasksFilesSidebar`）自动展示，无需改动前端。

## 需要创建/修改的文件

### 1. 新建：`src/agent/subagents/sync_subagent_todos.py`

后台同步进程核心逻辑：

- `_run_sync_loop(main_thread_id, sub_thread_id, agent_name)` — 守护线程入口
- `_snapshot_main_todos(client, main_thread_id)` — 首次快照主智能体原始 todos（排除残留的子智能体标记项）
- `_extract_subagent_todos(client, sub_thread_id)` — 从子智能体线程提取最新 `write_todos` 结果
- `_merge_todos(main_todos, sub_todos, agent_name)` — 合并算法：主 todos + 🔍分隔符 + 子 todos
- `launch_sync(main_thread_id, sub_thread_id, agent_name)` — 公开入口，在守护线程中启动同步

关键细节：
- 使用 `threading.Thread(daemon=True)` 运行，不阻塞主进程
- 每 2 秒轮询子智能体线程状态
- 通过 `langgraph_sdk.get_client(url=None)` 使用 ASGI 内部调用（与 `check_progress.py` 一致）
- 子智能体完成后，清除子智能体 todos，只保留主智能体原始 todos

### 2. 新建：`src/agent/subagents/sync_launcher.py`

Monkey-patch `deepagents.middleware.async_subagents._build_start_tool`：

- 关键发现：`_build_start_tool(agent_map, clients, tool_description)` 返回 `StructuredTool`，Command 是在内部 `start_async_task()` 函数中**内联构建**的（没有独立的 `_build_start_command` 函数）
- 因此 patch 方式是**包装 `tool.func` 和 `tool.coroutine`**（与 `check_progress.py` 对 `_build_check_tool` 的 patch 模式一致）
- 包装函数在 `start_async_task` 返回 Command 后，从 Command 的 `async_tasks` 中提取 `thread_id` 和 `agent_name`，从 `runtime.config["configurable"]["thread_id"]` 提取主智能体的 thread_id，然后调用 `launch_sync()`

获取 thread_id 的方式：
```python
# 主智能体 thread_id
main_thread_id = runtime.config["configurable"]["thread_id"]
# 子智能体 thread_id = task_id（从 Command.update["async_tasks"] 中提取）
```

### 3. 修改：`src/agent/main_agent.py`

在已有的 `import agent.subagents.check_progress` 旁边，添加：
```python
import agent.subagents.sync_launcher  # 触发 monkey-patch
```

仅此一行改动。

## 关键参考文件

| 文件 | 作用 |
|------|------|
| `.venv/.../deepagents/middleware/async_subagents.py` | `_build_start_tool` (line 279) — patch 目标 |
| `src/agent/subagents/check_progress.py` | 现有 monkey-patch 模式参考（`apply_patch()` + 幂等 + 自动执行） |
| `src/agent/main_agent.py` | 添加 import 触发 patch |
| `src/agent/subagents/track_progress.py` | `ProgressTrackerMiddleware` — 子智能体进度数据源 |

## 验证方式

1. 启动后端：`python start_server.py`
2. 在前端发送数据查询（触发 `start_async_task`）
3. 观察前端**输入框上方的任务进度条**：
   - 应自动出现子智能体的步骤（带 🔍 前缀）
   - 每完成一步，进度条应更新
   - 子智能体完成后，子智能体步骤应消失，只保留主智能体的 todos
4. 检查后端日志：`[sync]` 前缀的日志应显示同步进度
