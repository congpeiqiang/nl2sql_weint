# thinking_toggle.py — ThinkingToggleMiddleware 代码解读

> 源文件：`src/agent/middlewares/thinking_toggle.py`
> 类别：**配置注入**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

两个需求合一：

1. **思考开关**：前端单开关控制模型是否启用深度思考（reasoning）模式
2. **模型路由（P1-9）**：前端切模型免重启后端——每次调用按 `llm_route`/`llm_model` 重建模型实例

## 2. 核心机制

### 2.1 思考开关

前端 `configurable.enable_thinking` → `wrap_model_call` 时 `create_model(enable_thinking=...)` 重建模型实例并 `request.override(model=...)`。

时序依据：langchain 在替换**之后**才执行 `bind_tools`，工具绑定不受影响。

### 2.2 模型路由

`configurable.llm_route` / `configurable.llm_model` 指向前端 CRUD 的 `model_config.json` 中的 provider/模型，每次调用按 route 重建模型 → **前端切模型免重启后端**。

### 2.3 进程级缓存

`ChatDeepSeek` 构造实测 ~8.2s，同 `(enable_thinking, route, model_name)` 三元组复用实例：

| 缓存 | 键 | 值 | 失效策略 |
|---|---|---|---|
| `_model_cache` | `(enable, route, model)` 三元组 | 模型实例 | `model_config.json` 的 **mtime 变更**即清 |
| `_last_thread_key` | `thread_id` | 最近 cache_key | 上限 200 条 FIFO |

缓存上限 8 条 FIFO 淘汰。

### 2.4 auto-continue 模型继承（P1-10）

同 thread 后续续跑 run 不携带配置（前端只发系统通知消息）→ 查 `_last_thread_key`（thread_id → 最近显式配置的 cache_key）复用上次的模型实例。

**为什么需要**：续跑若不继承，会回退到模块级默认模型（可能已欠费），导致续跑失败。保证整会话同模型。

## 3. 缓存失效机制

```python
def _check_config_mtime():
    config_path = os.path.join(get_agent_data_root(), "model_config.json")
    try:
        mtime = os.path.getmtime(config_path)
    except OSError:
        mtime = 0
    if mtime != _last_mtime:
        _model_cache.clear()
        _last_mtime = mtime
```

每次 `wrap_model_call` 调用时检查 mtime，配置变更即清缓存。

## 4. 关键设计决策

### 4.1 为什么每次调用都检查 configurable

- 前端可能在会话中途切模型
- 配置变更通过 mtime 自动感知
- 不需要重启后端或刷新页面

### 4.2 为什么缓存上限 8 条

- 实测同时活跃的模型配置不超过 3-4 个
- 8 条足够覆盖正常使用场景
- FIFO 淘汰确保不会内存泄漏

### 4.3 为什么 auto-continue 要继承模型

- 续跑是同一问题的后续步骤，应该用同一模型
- 不同模型的风格/能力差异会导致续跑结果不一致
- 用户在前端切模型后，续跑应该用新模型（`_last_thread_key` 已更新）

## 5. 配置来源

| 字段 | 来源 | 说明 |
|---|---|---|
| `enable_thinking` | `configurable` | 前端思考开关 |
| `llm_route` | `configurable` | 模型提供商路由 |
| `llm_model` | `configurable` | 具体模型名 |

全部通过 `langgraph.config.get_config()` 读取（runtime.config 恒空）。

## 6. 错误处理

- configurable 缺失 → 不替换模型，走默认
- `model_config.json` 不存在 → mtime=0，缓存不清
- 模型构造失败 → 异常上抛（模型不可用是致命错误）
- `_last_thread_key` 查不到 → 走默认模型
