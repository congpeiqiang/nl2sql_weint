# model_timeout.py — ModelTimeoutMiddleware 代码解读

> 源文件：`src/agent/middlewares/model_timeout.py`
> 类别：**错误翻译**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

模型 API 超时（httpx `ReadTimeout` / openai `APITimeoutError`）被 LangGraph 序列化进 run stream 的 error 事件，而前端 SDK 不渲染 `stream.error` → 表现为"卡住、无提示"（trace `ef83792` 实证：4×60s 重试后界面空白）。

## 2. 核心机制

### 2.1 错误翻译

**不抛异常**，捕获超时后返回一条带友好中文文案的 AIMessage：

```
⚠️ 模型响应超时

当前模型服务响应时间过长，请稍后重试，或尝试切换到其他模型。

如果问题持续，请在「设置 → 模型配置」中检查模型服务状态。
```

### 2.2 AIMessage 特性

- 不带 `tool_calls` → 模型节点后直接 END，不触发工具节点
- 不会因缺工具结果再抛错
- 消息写入 checkpoint → 刷新页面仍可见

### 2.3 超时识别

`is_timeout_error(e)` 四级判定链：

```python
def is_timeout_error(e):
    # 1. httpx.TimeoutException（最常见）
    if isinstance(e, httpx.TimeoutException): return True
    # 2. 内置 TimeoutError
    if isinstance(e, TimeoutError): return True
    # 3. 类名含 Timeout
    if "Timeout" in type(e).__name__: return True
    # 4. 正则匹配消息文本
    if re.search(r'(timeout|timed\s*out)', str(e), re.I): return True
    return False
```

并**递归遍历 `__cause__`/`__context__` 异常链**（带 seen 集合防环）：

```python
def _check_cause_chain(e, seen=None):
    seen = seen or set()
    if id(e) in seen: return False
    seen.add(id(e))
    for attr in ("__cause__", "__context__"):
        cause = getattr(e, attr, None)
        if cause and is_timeout_error(cause): return True
        if cause and _check_cause_chain(cause, seen): return True
    return False
```

### 2.4 非超时错误

其他错误原样上抛，保持失败语义——只有超时才翻译为 AIMessage。

## 3. 与 quota_error 的同构关系

| 维度 | model_timeout | quota_error |
|---|---|---|
| 触发条件 | LLM 超时 | 额度耗尽（402/403） |
| 返回形式 | AIMessage（中文） | AIMessage（中文） |
| 不抛异常 | ✅ | ✅ |
| tool_calls | 无 | 无 |
| 可重试 | 用户手动重试 | 充值后重试 |

两者共用"错误翻译为 AIMessage"的统一模式。

## 4. 关键设计决策

### 4.1 为什么不抛异常

- LangGraph 把异常序列化进 run stream 的 error 事件
- 前端 SDK 不渲染 `stream.error` → 用户看到空白
- AIMessage 是最可靠的"把信息传递给用户"的方式

### 4.2 为什么要遍历异常链

- httpx 超时可能被包装在 `openai.APIConnectionError.__cause__` 中
- 某些代理层会把超时包装在自定义异常中
- 只看顶层异常会漏判

### 4.3 为什么用 seen 集合

- 异常链可能成环（A.__cause__ = B, B.__cause__ = A）
- 无保护会无限递归 → stack overflow
- seen 集合按 `id(e)` 去重
