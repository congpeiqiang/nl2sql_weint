# write_todos.py — WriteTodosProtocolMiddleware 代码解读

> 源文件：`src/agent/middlewares/write_todos.py`
> 类别：**执行监督**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

deepagents 基础栈 `TodoListMiddleware` 默认指引写着"simple 请求不要用 write_todos"。为进度条强制所有查询执行时，简单查询（策略 B）多付 1 次初始化 + 1-2 次更新的 LLM 轮次延迟。前端已有 `deriveStepsFromSubMessages` 从工具序列推导步骤兜底。

需要在**必须维护 todos 的复杂查询**和**可跳过的简单查询**之间分级。

## 2. 核心机制

### 2.1 协议注入

把 `WRITE_TODOS_PROTOCOL` 追加到 system prompt（与默认指引**兼容共存**而非覆盖）：

| 策略 | 步骤数 | todos 要求 |
|---|---|---|
| 策略 A / C | 7-8 步流水线 | **必须**维护 todos |
| 策略 B | ≤3 步 | **可跳过** |

### 2.2 关键节奏规定

协议规定了**关键节奏**：

1. **write_todos 必须与完成该步的实质工具调用放同一条 assistant 消息并行发出**
2. 唯一禁止：同一条消息 ≥2 个 write_todos
3. "中途更新是免费的"——鼓励模型在每个里程碑处更新

### 2.3 与 progress_boundary 的里程碑对齐

协议给出与 `progress_boundary` 一致的更新时机：

- 首个 schema 检索工具发出时 → 勾选"理解建模"
- 首个执行工具发出时 → 勾选"SQL 生成"并推进"查询执行"

这样即使模型忘了更新 todos，`progress_boundary` 也会确定性兜底。

## 3. 关键设计决策

### 3.1 为什么追加而非覆盖 system prompt

- deepagents `TodoListMiddleware` 已有默认指引（"simple 请求不要用"）
- 覆盖会丢失默认指引中的其他有用内容
- 追加的协议与默认指引兼容（复杂查询必须用，简单查询可跳过）

### 3.2 为什么只用于 nl2sql 子 agent

- 主 agent 的 todos 稳定不需要本协议（主 agent 不调数据查询工具）
- 子 agent 的查询流水线才需要进度追踪
- 减少主 agent 的 system prompt 体积

### 3.3 策略分级的好处

- 策略 B（简单查询如"今天几号"）省掉 2-3 次 LLM 调用
- 策略 A/C（复杂报告）保证进度条连续
- 前端 `deriveStepsFromSubMessages` 在策略 B 时从工具序列推导步骤兜底

## 4. 异步注意事项

`awrap_model_call` **必须 await handler**——不 await 会把 coroutine 对象外泄，下游 `_build_commands` 报 `'coroutine' object has no attribute 'result'`（2026-08-14 实证）。

```python
async def awrap_model_call(self, request, handler):
    modified = self._inject_protocol(request)
    result = handler(modified)
    if hasattr(result, "__await__"):
        return await result  # 关键：必须 await
    return result
```

## 5. 错误处理

- system_message 为 None → 不注入，返回原 request
- 协议文本构造异常 → fail-open，返回原 request
