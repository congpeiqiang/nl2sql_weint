# current_db_context.py 代码解读

> 文件路径：`src/agent/middlewares/current_db_context.py`（103 行）
> 解读日期：2026-09-18

## 一句话概括

`CurrentDbContextMiddleware`：每次模型调用前，把当前数据库名（`configurable.db_name`）以 `【当前数据库：{db_name}】` 前缀注入**最新一条用户消息**头部，防止模型采信对话历史中的陈旧库名。

## 解决的问题（实证 bug）

2026-08-11 实证：前端切库到 clickhouse 后发"有多少表"，system prompt 已注入"当前数据库是 clickhouse"，但模型采信对话历史/总结里自己上轮说过的"当前应用数据库是 imdb"，仍按 imdb 委派（`start_async_task` 的【数据库名称】写错）。

**解法思路**：system prompt 是"远程信号"，容易被历史里的自我陈述压过；而**当轮用户消息是最高优先级信号**——直接把库名拼进用户消息头部，历史无法覆盖。（system prompt 前置权威化见 `main_agent.dynamic_prompt`，两者配合。）

## 核心实现

### `_resolve_db_name`（第 31–49 行）
- **必须走 `langgraph.config.get_config()`** 读 `configurable.db_name`：langchain 官方明确 Runtime 不含 config——`ModelRequest.runtime.config` 恒为空 dict（实证 2026-08-11，db_name=''）。
- 保留 `request.runtime.config` 路径仅作旧框架兼容兜底（当前版本恒为空）。

### `_inject`（第 51–80 行）
1. db_name 为空 → 原样返回。
2. **仅当最新消息是 HumanMessage 时注入**：工具回环/续跑/auto-continue 阶段（最新消息非用户消息）不注入，避免污染中间步骤。
3. 幂等：str 内容已以前缀开头 → 跳过。
4. content 为 block 列表（content_and_artifact 形态）→ 在末尾追加 `{"type":"text"}` 块。
5. 用 `id=last.id` 重建 HumanMessage → add_messages reducer **原位替换**而非追加新消息。

### 挂点
`wrap_model_call` / `awrap_model_call`：注入后调用 handler，只改出站请求，不改 checkpoint 里的原始用户消息。

## 设计要点

- **同路径模式复用**：与 `query_keywords.py`、`thinking_toggle.py`、`tool_filter.py` 共用"get_config 读 configurable + runtime 兜底"的固定写法——这是本项目的框架级共识坑。
- 注入动作有 info 日志（`[dbctx]`），便于排查委派库名来源。
- 全链路 try/except 降级：读不到配置就完全不注入，行为退化为原状，不报错。

## 关联文件

- `main_agent.py`：dynamic_prompt 在 system prompt 侧的权威化（前置配合）
- `query_keywords.py` / `thinking_toggle.py`：同一 configurable 通道、同一读取模式
