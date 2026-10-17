# model_required.py — ModelRequiredMiddleware 代码解读

> 源文件：`src/agent/middlewares/model_required.py`
> 类别：**安全护栏 / 模型配置守卫**
> 挂载点：`wrap_model_call` / `awrap_model_call`（图最外层）

## 1. 解决的问题

新账号（含新建的管理员账号）不再继承共享 `model_config.json`。但只删播种不够：图节点的默认模型是模块级 `deepseek_model`，`ThinkingToggleMiddleware._maybe_swap` 在 `create_model` 返回 None 时保留那个共享模型 → "没配模型的账号"会**静默地拿别人的 key 打模型**。

前端 composer 虽有门禁，但子任务完成的自动续跑、离线实验等入口绕得过它，所以必须在服务端 fail-closed。

## 2. 核心机制

### 2.1 拦截方式

图的最外层，有登录身份但该账号没有可用模型时：

1. **直接返回友好中文 AIMessage**，连 handler 都不调用（不构造模型实例、不发任何请求）
2. 消息同时**盖失败戳**（`failure_signal.mark_failed`）—— "没配模型"确实是失败（零执行），盖戳后前端给的是「上一轮执行失败 + 重试」

### 2.2 判据

唯一判据：`agent.llms.model.has_usable_model(user_id)`

- 与 `create_model` 同一条解析链
- 判据函数内部已吞掉一切读取异常（→ 视为「无模型」= fail-closed）

### 2.3 非真实用户不拦

无「真实用户」身份时维持旧行为：

| 场景 | 身份 | 行为 |
|---|---|---|
| AUTH_DISABLED 本地开发 | `dev` 哨兵 | 不拦 |
| 内部自调用 | `internal` | 不拦 |
| 离线实验 | 无 user_id | 不拦 |
| Langfuse evaluator | 无身份 | 不拦 |

判定用 `auth/ownership.is_real_owner`（同一份口径，**不是**自写的 `if not user_id` — `dev`/`internal` 是非空串，那样写会误拦）。

## 3. 友好提示文案

```
当前账号尚未配置可用的大模型，无法执行。模型配置按账号独立（新账号不继承其他账号的配置），
请点击右上角「设置 → 模型」新增一个接入点：填好 base_url、自己的 API Key，
并至少添加一个模型，然后重新发送。
（若已添加过，请检查它还缺 API Key / base_url，或模型列表为空。）
```

## 4. user_id 读取

```python
def current_user_id():
    try:
        from langgraph.config import get_config
        cfg = get_config()
        configurable = (cfg.get("configurable") or {}) if cfg else {}
    except Exception:
        return ""
    uid = configurable.get("user_id") or configurable.get("langgraph_auth_user_id")
    return str(uid) if uid else ""
```

- 必须走 `langgraph.config.get_config()`（`request.runtime.config` 恒为空）
- `langgraph_auth_user_id` 是同一身份的另一个别名（sync 循环补写、子 agent 读取）

## 5. 子 agent 覆盖

本中间件依赖 `configurable.user_id` **就是登录身份**：

- `api/langfuse_metadata.LangfuseMetadataMiddleware` 强制改写该键（P1-2，伪造无效）
- `deepagents_async_config_patch` 与 `sync_subagent_todos` 都把它透传下来
- → 子 agent 同样拦得住

## 6. 关键设计决策

### 6.1 为什么不回落共享模型

- 额度记在别人名下 → 安全/计费问题
- 新账号应该明确配置自己的模型
- fail-closed 比 fail-open 更安全

### 6.2 为什么与 QuotaErrorMiddleware 同构

- 不抛异常 → agent 正常结束、消息进 checkpoint、界面不卡
- AIMessage 是最可靠的"把信息传递给用户"的方式
- 盖失败戳 → 前端展示"执行失败"而非"正常回答"

### 6.3 为什么在最外层

- 不构造模型实例 → 零资源消耗
- 不调用 handler → 不触发任何下游逻辑
- 第一时间拦截，不浪费任何处理时间
