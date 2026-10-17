# quota_error.py — QuotaErrorMiddleware 代码解读

> 源文件：`src/agent/middlewares/quota_error.py`
> 类别：**错误翻译**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

模型账号额度耗尽（HTTP 402/403、"Free quota exhausted"/"insufficient_quota"/"Insufficient Balance"）→ 需要友好提示用户充值，而非让底层报错穿透到前端（前端 SDK 不渲染 stream.error）。

## 2. 核心机制

### 2.1 错误翻译

捕获额度相关异常后返回友好中文 AIMessage：

```
⚠️ 模型额度已耗尽

当前模型服务的调用额度已用完，无法继续处理请求。
请联系管理员充值，或在「设置 → 模型配置」中切换到其他模型。
```

### 2.2 识别策略（防误伤）

**强关键词**（单独命中即判定）：

```
insufficient_quota, quota exhausted, quota_exceeded,
额度, 余额不足, 欠费, insufficient balance
```

**弱关键词**（必须同时出现状态码）：

```
quota, balance, 余额
```

状态码判定：文本中含 402/403/429，或异常的 `status_code` 属性为 402/403/429。

**为什么分强弱**：

- "balance" 可能出现在 SQL 业务报错中（如"账户余额查询"）→ 误判为额度问题
- 弱关键词 + 状态码双保险避免误伤
- 强关键词足够明确，单独命中即可

### 2.3 不处理的情况

- 401 鉴权错误 → 原样上抛（不是额度问题）
- 非额度相关的 403 → 原样上抛
- 无法识别的错误 → 原样上抛

## 3. QuotaExhaustedError 类

供非 agent 场景手动翻译使用：

```python
class QuotaExhaustedError(Exception):
    """额度耗尽异常——供 auto_title 等自带 try/except 的调用点捕获。"""
    pass
```

如 `auto_title`（自动生成会话标题）有自己的 try/except，可以直接捕获此类并展示友好提示。

## 4. 关键设计决策

### 4.1 只翻译文案，不读/回退 .env 模型配置

P1-9 配置权威性约束：中间件只负责把错误翻译成用户可读的 AIMessage，**不做模型回退**。模型切换由 `thinking_toggle` 统一管理。

### 4.2 与 model_timeout 的同构设计

两者共用相同模式：

```
try:
    response = handler(request)
except Exception as e:
    if is_target_error(e):
        return ModelResponse(result=[AIMessage(content="友好中文提示")])
    raise  # 非目标错误原样上抛
```

### 4.3 为什么不在 deepagents 层处理

- deepagents 的错误处理是通用的（不区分超时/额度/其他）
- 本系统在中间件层做精确分类 + 中文文案
- 中间件层可以访问 configurable（知道当前用哪个模型），给出更有针对性的提示

## 5. 错误处理

- handler 未抛异常 → 正常返回 response
- 目标错误 → 返回 AIMessage
- 非目标错误 → 原样上抛
- 翻译过程本身异常 → 原样上抛（不吞异常）
