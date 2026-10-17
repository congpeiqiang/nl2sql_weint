# current_db_context.py — CurrentDbContextMiddleware 代码解读

> 源文件：`src/agent/middlewares/current_db_context.py`
> 类别：**配置注入**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

**实证 bug**（2026-08-11）：前端切库到 clickhouse 后问"有多少表"，system prompt 已写明当前库是 clickhouse，但模型采信对话历史里自己上轮说的"当前数据库是 imdb"，仍按 imdb 委派子任务（`start_async_task` 的【数据库名称】参数写错）。

根因：system prompt 注入的库名在历史对话中被陈旧信息覆盖——模型优先采信最近的 assistant 消息而非 system prompt。

## 2. 核心机制

每次模型调用前，把 `configurable.db_name` 以 `【当前数据库：{db_name}】` 前缀**直接拼进最新一条用户消息头部**。用户消息是当轮最高优先级信号，无法被陈旧历史覆盖。

### 2.1 db_name 读取

必须走 `langgraph.config.get_config()`——langchain 官方 `ModelRequest.runtime.config` 恒为空 dict（2026-08-11 实证），代码保留 runtime 路径仅作旧框架兜底：

```python
def _resolve_db_name(self, request):
    try:
        from langgraph.config import get_config as _cfg
        if _cfg is not None:
            db_name = (_cfg().get("configurable", {}) or {}).get("db_name", "") or ""
            if db_name:
                return db_name
    except Exception:
        pass
    # 兜底（当前恒为空）
    try:
        runtime = getattr(request, "runtime", None)
        config = getattr(runtime, "config", None) or {}
        return (config.get("configurable") or {}).get("db_name", "") or ""
    except Exception:
        return ""
```

### 2.2 注入规则

| 条件 | 行为 |
|---|---|
| 最新消息是 `HumanMessage` | 注入前缀 |
| 最新消息非 `HumanMessage`（工具回环/续跑/auto-continue） | 不注入，避免污染 |
| content 已以前缀开头 | 跳过（幂等） |
| content 为 block 列表 | 在末尾追加文本块 |
| db_name 为空 | 不注入，原样返回 |

### 2.3 消息替换

注入时用 `id=last.id` 重建消息 → `add_messages` reducer 原位替换而非追加：

```python
new_msg = HumanMessage(
    content=new_content,
    additional_kwargs=dict(last.additional_kwargs or {}),
    id=last.id,  # 关键：原位替换
)
return request.override(messages=messages[:-1] + [new_msg])
```

## 3. 关键设计决策

### 3.1 为什么注入用户消息而非 system prompt

- system prompt 权威性在长对话中被稀释
- 用户消息是模型每轮必读的最新信号
- 与 `main_agent.dynamic_prompt` 的 system prompt 注入形成**双重保障**

### 3.2 为什么只注入最新一条消息

- 改历史消息会破坏 checkpoint 一致性
- 最新一条已足够覆盖模型当轮决策
- 多轮注入会导致历史中出现多条前缀（噪音）

### 3.3 为什么不在 auto-continue 阶段注入

auto-continue 时最新消息是系统通知（非 HumanMessage），此时模型在做续跑决策，注入库名前缀会污染续跑逻辑。

## 4. 错误处理

- `get_config()` 异常 → 静默兜底到 runtime 路径
- runtime 路径也失败 → 返回空字符串，不注入
- content 为非常规类型（非 str 非 list） → 跳过注入
