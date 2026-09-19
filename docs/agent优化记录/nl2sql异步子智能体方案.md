# NL2SQL 异步子智能体方案分析

## 一、当前架构 vs 异步架构

| 维度 | 当前（同步 subagents） | 异步（AsyncSubAgent） |
|------|----------------------|----------------------|
| 执行模型 | 主智能体阻塞等待 | 主智能体立即返回，后台运行 |
| 并发 | 可并行但阻塞 | 并行且非阻塞 |
| 中途控制 | 不支持 | 支持 update/cancel |
| 状态 | 无状态（每次独立） | 有状态（保持线程上下文） |
| 前端实时性 | 不可见中间步骤 | 可轮询进度 |
| 部署要求 | 单一 graph | 每个子智能体独立 graph |

## 二、AsyncSubAgent 核心要求

AsyncSubAgent 需要子智能体是一个独立部署的 LangGraph：
- 在 graph.json 中有独立的 graph_id
- 通过 Agent Protocol 通信（ASGI 同进程 / HTTP 远程）
- 工具名变为: launch_async_task / get_task_status / update_async_task / cancel_async_task

## 三、改造步骤

### Step 1: graph.json 注册两个图

```json
{
  "graphs": {
    "chat_agent": { "path": "./src/agent/main_agent.py:agent" },
    "nl2sql_agent": { "path": "./src/agent/nl2sql_agent.py:agent" }
  }
}
```

### Step 2: 创建 nl2sql_agent.py（子智能体独立图）

```python
# src/agent/nl2sql_agent.py
from deepagents import create_deep_agent
from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import tools as mcp_tools

agent = create_deep_agent(
    model=deepseek_model,
    tools=mcp_tools,
    system_prompt=NL2SQL_PROMPT,
)
```

### Step 3: 主智能体使用 AsyncSubAgent

```python
# main_agent.py
from deepagents import create_deep_agent, AsyncSubAgent

nl2sql_async = AsyncSubAgent(
    name="nl2sql",
    description="NL2SQL查询专家",
    graph_id="nl2sql_agent",
    # url=None → ASGI同进程
)

agent = create_deep_agent(
    model=deepseek_model,
    tools=mcp_tools,
    async_subagents=[nl2sql_async],   # 注意: async_subagents 非 subagents
)
```

### Step 4: 前端感知异步进度

- launch_async_task 立即返回 job_id
- 前端显示 "nl2sql 后台执行中..."
- 主智能体 get_task_status(job_id) 轮询
- 完成后展示结果

## 四、新交互流程

```
用户: 查询2024年评分最高的10部电影
  |
主智能体: launch_async_task(subagent='nl2sql', prompt='...')
  | → {job_id: 'xxx', status: 'queued'}
  |
主智能体: "已提交查询，后台执行中..."
  |
  ├── [后台] nl2sql 执行:
  │     ├─ get_context + recall_queries + get_instructions (并行)
  │     ├─ describe_model * N (并行)
  │     ├─ SQL生成 + dry_run + run_sql
  │     └─ 完成
  |
主智能体: get_task_status('xxx') → completed
  |
主智能体: 展示查询结果
```

## 五、优劣对比

| 优势 | 劣势 |
|------|------|
| 主智能体不阻塞，用户可继续对话 | 部署复杂度增加（多 graph） |
| 可取消长查询（IMDb 大数据量） | 需要额外状态管理 |
| 可轮询/推送进度 | 工具名变为 launch_async_task |
| 子智能体有状态，可连续对话 | async_subagents 是预览功能 |
| 适合 IMDb 大数据分析场景 | 需要更新 SYSTEM PROMPT |

## 六、建议

- 当前阶段：保持同步 subagents，先完成核心功能
- 需要异步时：长查询 >30s 或需要取消/进度时切换
- 优先级：前端子智能体步骤可见 > 异步改造
- 触发条件：当IMDb查询超时影响体验时启用异步


---

## 实施记录 (2026-07-22)

### 已实施改动

| 文件 | 改动 |
|------|------|
| `nl2sql_agent.py` | **新建** — 从 `nl2sql.yaml` 读取配置构建子智能体独立 graph |
| `graph.json` | 注册 `nl2sql_agent` graph_id |
| `langgraph.json` | 同步注册 |
| `main_agent.py` | `AsyncSubAgent(name="nl2sql", graph_id="nl2sql_agent")`。tools=[], skills=[main/] |
| `MAIN_AGENT_PROMPT.md` | `task` → `start_async_task` / `check_async_task` / `cancel_async_task` / `update_async_task` / `list_async_tasks` |
| `start_server.py` | `N_JOBS_PER_WORKER: 1→3` |
| `nl2sql.yaml` | 保留作为配置源（由 `nl2sql_agent.py` 读取） |

### 当前架构

```
主智能体 (chat_agent)
  tools: []                       ← 无 MCP 工具，纯协调者
  skills: [main/]                 ← 仅主技能
  AsyncSubAgent: [nl2sql]         ← 异步子智能体

子智能体 (nl2sql_agent)            ← 独立 graph
  tools: [MCP 35+ 工具]           ← 从 nl2sql.yaml 匹配
  skills: [nl2sql/]               ← 独立 SkillsMiddleware
  system_prompt: NL2SQL_SYSTEM_PROMPT.md
```

### 添加新子智能体

```
1. subagents/configs/新名称.yaml      ← YAML 配置
2. 新名称_agent.py                    ← 复制 nl2sql_agent.py 改路径
3. graph.json 注册                    ← 加 graph_id
4. main_agent.py 加 AsyncSubAgent     ← 注册子智能体
```

### 异步工具速查

| 工具 | 用途 |
|------|------|
| `start_async_task(subagent, prompt)` | 启动后台任务 |
| `check_async_task(task_id)` | 查询状态/结果 |
| `update_async_task(task_id, instructions)` | 追加指令 |
| `cancel_async_task(task_id)` | 取消任务 |
| `list_async_tasks()` | 列出活跃任务 |
