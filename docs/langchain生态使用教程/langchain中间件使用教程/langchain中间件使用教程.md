# LangChain 中间件使用教程

> 基于 langchain 1.3.14 + deepagents 0.6.12 源码通读编写。

---

## 目录

- [一、架构概述](#一架构概述)
- [二、中间件基类与钩子体系](#二中间件基类与钩子体系)
- [三、装饰器快捷方式](#三装饰器快捷方式)
- [四、LangChain 内置中间件](#四langchain-内置中间件)
  - [4.1 SummarizationMiddleware — 上下文压缩](#41-summarizationmiddleware--上下文压缩)
  - [4.2 ModelRetryMiddleware — 模型调用重试](#42-modelretrysmiddleware--模型调用重试)
  - [4.3 ModelFallbackMiddleware — 模型降级](#43-modelfallbackmiddleware--模型降级)
  - [4.4 ModelCallLimitMiddleware — 模型调用次数限制](#44-modelcallimitmiddleware--模型调用次数限制)
  - [4.5 ToolRetryMiddleware — 工具调用重试](#45-toolretrysmiddleware--工具调用重试)
  - [4.6 ToolErrorMiddleware — 工具错误处理](#46-toolerrormiddleware--工具错误处理)
  - [4.7 ToolCallLimitMiddleware — 工具调用次数限制](#47-toolcallimitmiddleware--工具调用次数限制)
  - [4.8 HumanInTheLoopMiddleware — 人机协同审批](#48-humanintheloopmiddleware--人机协同审批)
  - [4.9 PIIMiddleware — 敏感信息检测](#49-piimiddleware--敏感信息检测)
  - [4.10 ContextEditingMiddleware — 上下文裁剪](#410-contexteditingmiddleware--上下文裁剪)
  - [4.11 FilesystemFileSearchMiddleware — 文件搜索](#411-filesystemfilesearchmiddleware--文件搜索)
  - [4.12 TodoListMiddleware — 任务规划](#412-todolistmiddleware--任务规划)
  - [4.13 LLMToolSelectorMiddleware — 智能工具选择](#413-llmtoolselectormiddleware--智能工具选择)
  - [4.14 LLMToolEmulator — 工具模拟执行](#414-llmtoolemulator--工具模拟执行)
  - [4.15 ProviderToolSearchMiddleware — 服务端工具搜索](#415-providertoolsearchmiddleware--服务端工具搜索)
  - [4.16 ShellToolMiddleware — Shell 会话](#416-shelltoolmiddleware--shell-会话)
- [五、DeepAgents 扩展中间件](#五deepagents-扩展中间件)
  - [5.1 SummarizationMiddleware (deepagents) — 增强压缩](#51-summarizationmiddleware-deepagents--增强压缩)
  - [5.2 FilesystemMiddleware — 文件系统工具集](#52-filesystemmiddleware--文件系统工具集)
  - [5.3 MemoryMiddleware — AGENTS.md 记忆注入](#53-memorymiddleware--agentsmd-记忆注入)
  - [5.4 SkillsMiddleware — 技能加载与注入](#54-skillsmiddleware--技能加载与注入)
  - [5.5 SubAgentMiddleware — 子代理调度](#55-subagentmiddleware--子代理调度)
  - [5.6 AsyncSubAgentMiddleware — 异步后台任务](#56-asyncsubagentmiddleware--异步后台任务)
  - [5.7 RubricMiddleware — 自我评估迭代](#57-rubricmiddleware--自我评估迭代)
  - [5.8 PatchToolCallsMiddleware — 悬挂工具调用修补](#58-patchtoolcallsmiddleware--悬挂工具调用修补)
- [六、中间件组合与排序](#六中间件组合与排序)
- [七、自定义中间件编写](#七自定义中间件编写)

---

## 一、架构概述

LangChain 中间件是一种**插件机制**，在 Agent 的主循环（model → tools → model → …）的各个节点插入自定义逻辑。中间件可以：

- **拦截模型调用**（修改请求 / 重试 / 降级 / 限流）
- **拦截工具调用**（重试 / 错误处理 / 监控 / 修改参数）
- **修改 state**（压缩历史消息 / 注入系统提示 / 跟踪计数）
- **控制流程**（跳转 / 中断 / 人机协同）

### Agent 主循环与中间件钩子位置

```
┌─────────────────────────────────────────────────────────────┐
│                     Agent 完整执行生命周期                     │
│                                                              │
│  ① before_agent ─────── Agent 启动前（初始化、加载数据）        │
│        │                                                      │
│        ▼                                                      │
│  ┌─── 循环开始 ◄─────────────────────────────────────┐       │
│  │                                                    │       │
│  │  ② before_model ──── 模型调用前（压缩、限流检查）    │       │
│  │       │                                            │       │
│  │       ▼                                            │       │
│  │  ③ wrap_model_call ─ 包裹模型调用（重试、降级）      │       │
│  │       │                                            │       │
│  │       ▼                                            │       │
│  │  ④ after_model ──── 模型调用后（计数、HITL 审批）    │       │
│  │       │                                            │       │
│  │       ├──有 tool_calls──► ⑤ wrap_tool_call       │       │
│  │       │                    （工具重试、错误处理）     │       │
│  │       │                        │                    │       │
│  │       └────────────────────────┘                    │       │
│  │                                                    │       │
│  └─── 继续循环（无 tool_calls 则退出）──────────────────┘       │
│        │                                                      │
│        ▼                                                      │
│  ⑥ after_agent ──────── Agent 完成后（清理、评估）              │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 中间件排序规则

中间件以列表形式传入 `create_agent(middleware=[...])`，**列表顺序 = 洋葱模型的外到内**：

```python
middleware = [A, B, C]

# wrap_model_call 执行顺序（外层先拦截）：
# A.wrap_model_call → B.wrap_model_call → C.wrap_model_call → 实际模型调用

# before_model 执行顺序（外层先执行）：
# A.before_model → B.before_model → C.before_model

# after_model 执行顺序（内层先执行）：
# C.after_model → B.after_model → A.after_model
```

---

## 二、中间件基类与钩子体系

### AgentMiddleware 基类

所有中间件继承自 `AgentMiddleware`，通过重写钩子方法实现自定义逻辑：

```python
from langchain.agents.middleware import AgentMiddleware

class MyMiddleware(AgentMiddleware):
    # 可选：自定义 state schema
    state_schema = MyState

    # 可选：注册额外工具
    tools = []

    # 生命周期钩子（均可选实现）
    def before_agent(self, state, runtime): ...
    def before_model(self, state, runtime): ...
    def after_model(self, state, runtime): ...
    def after_agent(self, state, runtime): ...

    # 拦截钩子（均可选实现）
    def wrap_model_call(self, request, handler): ...
    def wrap_tool_call(self, request, handler): ...
```

每个钩子都有对应的 `async` 版本（前缀 `a`），例如 `abefore_model`、`awrap_model_call`。

### 核心数据类型

| 类型 | 说明 |
|---|---|
| `ModelRequest` | 模型请求（model、messages、system_message、tools、state、runtime） |
| `ModelResponse` | 模型响应（result: list[BaseMessage], structured_response） |
| `ToolCallRequest` | 工具调用请求（tool_call dict、tool、state、runtime） |
| `AgentState` | Agent 状态字典（messages、jump_to、structured_response） |

### ModelRequest.override()

`ModelRequest` 是不可变对象，修改需通过 `override()` 创建新实例：

```python
def wrap_model_call(self, request, handler):
    # 替换模型
    new_request = request.override(model=fallback_model)

    # 替换系统消息
    from langchain_core.messages import SystemMessage
    new_request = request.override(
        system_message=SystemMessage(content="新指令")
    )

    # 替换消息列表
    new_request = request.override(messages=filtered_messages)

    return handler(new_request)
```

---

## 三、装饰器快捷方式

对于简单场景，可以用装饰器直接创建中间件，无需继承 `AgentMiddleware`：

```python
from langchain.agents.middleware import (
    before_agent, before_model, after_model, after_agent,
    wrap_model_call, wrap_tool_call, dynamic_prompt,
)

# ---- 生命周期钩子 ----

@before_agent
def log_start(state, runtime):
    print(f"Agent 启动，消息数: {len(state['messages'])}")

@before_model(can_jump_to=["end"])
def early_exit(state, runtime):
    """条件性提前退出"""
    if some_condition(state):
        return {"jump_to": "end"}
    return None

@after_model
def log_response(state, runtime):
    print(state["messages"][-1].content)

@after_agent
def log_done(state, runtime):
    print(f"Agent 完成，消息数: {len(state['messages'])}")


# ---- 拦截钩子 ----

@wrap_model_call
def retry_on_error(request, handler):
    """模型调用重试"""
    for attempt in range(3):
        try:
            return handler(request)
        except Exception:
            if attempt == 2:
                raise

@wrap_tool_call
def tool_cache(request, handler):
    """工具结果缓存"""
    if cached := get_cache(request):
        return ToolMessage(content=cached, tool_call_id=request.tool_call["id"])
    result = handler(request)
    save_cache(request, result)
    return result


# ---- 动态系统提示 ----

@dynamic_prompt
def context_aware_prompt(request):
    """根据对话长度动态调整系统提示"""
    msg_count = len(request.state["messages"])
    if msg_count > 10:
        return "你在一段长对话中，请简洁回答。"
    return "你是一个有帮助的助手。"
```

---

## 四、LangChain 内置中间件

### 4.1 SummarizationMiddleware — 上下文压缩

**用途：** 当对话消息 token 数超过阈值时，自动将旧消息压缩为摘要，保留近期消息。

**源码版本：** `langchain 1.3.14`

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `model` | `str \| BaseChatModel` | *必填* | 用于生成摘要的 LLM |
| `trigger` | `ContextSize \| TriggerClause \| list \| None` | `None` | 触发条件；`None` = 永不触发 |
| `keep` | `ContextSize` | `("messages", 20)` | 压缩后保留的上下文量 |
| `token_counter` | `Callable` | `count_tokens_approximately` | token 计数函数（Anthropic 自动校准 3.3 chars/token） |
| `summary_prompt` | `str` | `DEFAULT_SUMMARY_PROMPT` | 摘要生成提示模板 |
| `trim_tokens_to_summarize` | `int \| None` | `4000` | 送入摘要 LLM 前裁剪待压缩部分的上限 |

#### `keep` 三种形式

| 形式 | 含义 |
|---|---|
| `("messages", N)` | 保留最近 N 条消息 |
| `("tokens", N)` | 保留最近消息，总 token ≤ N（二分搜索） |
| `("fraction", f)` | 保留最近消息，总 token ≤ f × 模型最大上下文 |

#### `trigger` 触发条件

| 形式 | 语义 |
|---|---|
| `("tokens", 3000)` | token 数 ≥ 3000 |
| `("messages", 50)` | 消息数 ≥ 50 |
| `{"tokens": 4000, "messages": 10}` | **AND**：两个条件都满足 |
| `[("fraction", 0.8), ("messages", 100)]` | **OR**：任一满足 |

#### 内部处理过程（逐步）

```
每次模型调用前执行（before_model 钩子）：

1. 确保消息 ID
   └─ 为没有 ID 的消息分配 UUID（RemoveMessage 依赖 ID）

2. 计算 token 总数
   ├─ 近似计数：count_tokens_approximately(messages)
   └─ 或取上次 AIMessage.usage_metadata["total_tokens"]（需 provider 匹配）

3. 判断是否触发
   ├─ trigger=None → 直接返回（不压缩）
   ├─ 单条件 → 满足才继续
   ├─ dict(AND) → 所有条件都满足才继续
   └─ list(OR) → 任一条件满足即继续

4. 确定切割点 cutoff_index
   ├─ ("messages", N) → cutoff = len(messages) - N
   ├─ ("tokens"/"fraction") → 二分搜索最早能放下的位置
   └─ 安全校正 _find_safe_cutoff：
       ├─ 若 cutoff 落在 ToolMessage 上 → 向前搜索对应的 AIMessage
       ├─ 保证 AI tool_calls 与 ToolMessage 不被拆散
       └─ 找不到对应 AIMessage → 向后跳过 ToolMessage（更激进地压缩）

5. 分区消息
   ├─ messages_to_summarize = messages[:cutoff]
   └─ preserved_messages = messages[cutoff:]

6. 准备摘要输入
   ├─ trim_messages 裁剪到 trim_tokens_to_summarize（默认 4000 token）
   │   └─ strategy="last", start_on="human", include_system=True
   ├─ 裁剪失败 → 回退到最近 15 条消息
   └─ get_buffer_string(trimmed, format="xml") 序列化

7. 调用 LLM 生成摘要
   ├─ 使用 summary_prompt + 序列化消息
   ├─ 标记 metadata={"lc_source": "summarization"}
   └─ 异常 → 摘要 = "Error generating summary: {e}"（不崩溃）

8. 构建新消息列表
   ├─ 摘要包装为 HumanMessage：
   │   content = "Here is a summary of the conversation to date:\n\n{summary}"
   │   additional_kwargs = {"lc_source": "summarization"}
   └─ 返回 state 更新：
       {"messages": [
           RemoveMessage(id=REMOVE_ALL_MESSAGES),  # 清空全部
           summary_human_message,                    # 摘要在前
           *preserved_messages,                      # 近期消息在后
       ]}
```

#### 代码示例

```python
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware

# 基础用法：按消息数触发
middleware = SummarizationMiddleware(
    model="openai:gpt-4o-mini",
    trigger=("messages", 50),    # 消息数 ≥ 50 时触发
    keep=("messages", 10),       # 保留最近 10 条
)

# 按 token 比例触发
middleware = SummarizationMiddleware(
    model="openai:gpt-4o-mini",
    trigger=("fraction", 0.8),   # 达到 80% 上下文窗口时触发
    keep=("fraction", 0.1),      # 保留 10% 上下文
)

# AND 条件：tokens ≥ 4000 且 messages ≥ 10 才触发
middleware = SummarizationMiddleware(
    model="openai:gpt-4o-mini",
    trigger={"tokens": 4000, "messages": 10},
    keep=("messages", 6),
)

# OR 条件：任一满足即触发
middleware = SummarizationMiddleware(
    model="openai:gpt-4o-mini",
    trigger=[("fraction", 0.8), ("messages", 100)],
    keep=("messages", 20),
)

agent = create_agent(
    model="openai:gpt-4o",
    tools=[...],
    middleware=[middleware],
)
```

#### ⚠️ 已知问题

1. **`state["messages"]` 中的 `SystemMessage` 会被压缩掉**（[issue #33856](https://github.com/langchain-ai/langchain/issues/33856)）——通过 `create_agent(system_prompt=...)` 设置的系统提示安全（存在 state 之外），但放在消息列表里的 `SystemMessage` 会被删除
2. **破坏性操作**：`RemoveMessage(REMOVE_ALL_MESSAGES)` 清空后不可恢复
3. **`trigger=None`（默认）= 永不压缩**——必须主动配置

---

### 4.2 ModelRetryMiddleware — 模型调用重试

**用途：** 模型调用失败时自动重试，支持指数退避和异常过滤。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_retries` | `int` | `2` | 最大重试次数 |
| `retry_on` | `tuple[type] \| Callable` | `(Exception,)` | 重试的异常类型或判断函数 |
| `on_failure` | `"continue" \| "error" \| Callable` | `"continue"` | 重试耗尽后的行为 |
| `backoff_factor` | `float` | `2.0` | 指数退避倍数 |
| `initial_delay` | `float` | `1.0` | 初始延迟（秒） |
| `max_delay` | `float` | `60.0` | 最大延迟上限（秒） |
| `jitter` | `bool` | `True` | ±25% 随机抖动防雷群效应 |

#### 内部处理过程

```
wrap_model_call 钩子：

1. 进入重试循环（max_retries + 1 次机会）

2. 调用 handler(request)
   ├─ 成功 → 直接返回 ModelResponse
   └─ 失败 → 进入异常处理

3. 异常处理
   ├─ 检查异常是否在 retry_on 范围内
   │   ├─ 不在范围内 → 直接调用 _handle_failure()
   │   └─ 在范围内 → 继续
   ├─ 检查是否还有重试机会
   │   ├─ 已用完 → 调用 _handle_failure()
   │   └─ 还有 → 计算延迟并等待
   └─ 延迟公式：
       delay = initial_delay × (backoff_factor ^ attempt)
       delay = min(delay, max_delay)
       if jitter: delay *= random(0.75, 1.25)
       time.sleep(delay)

4. on_failure 处理
   ├─ "continue" → 返回 AIMessage(content="Model call failed after N attempt(s)...")
   ├─ "error" → 重新抛出异常
   └─ callable → 调用 callable(exception) → 返回其结果作为 AIMessage content
```

#### 代码示例

```python
from langchain.agents.middleware import ModelRetryMiddleware

# 基础重试
retry = ModelRetryMiddleware(max_retries=3)

# 只重试特定异常
from openai import RateLimitError, APIConnectionError
retry = ModelRetryMiddleware(
    max_retries=3,
    retry_on=(RateLimitError, APIConnectionError),
)

# 自定义判断函数
retry = ModelRetryMiddleware(
    max_retries=2,
    retry_on=lambda e: "timeout" in str(e).lower(),
)

# 重试耗尽后自定义处理
retry = ModelRetryMiddleware(
    max_retries=2,
    on_failure=lambda e: f"模型暂时不可用，请稍后再试。错误: {e}",
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[retry])
```

---

### 4.3 ModelFallbackMiddleware — 模型降级

**用途：** 主模型失败时，按顺序尝试备选模型。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `first_model` | `str \| BaseChatModel` | *必填* | 第一个备选模型 |
| `*additional_models` | `str \| BaseChatModel` | *可变* | 更多备选模型 |

#### 内部处理过程

```
wrap_model_call 钩子：

1. 尝试主模型（create_agent 中配置的 model）
   └─ handler(request)
   ├─ 成功 → 直接返回
   └─ 失败 → 记录异常，进入降级链

2. 遍历备选模型列表
   ├─ request.override(model=fallback_model)
   ├─ Anthropic 缓存标记清理：
   │   └─ 若备选模型不是 Anthropic 系列 → 去除 cache_control 标记
   │       （避免非 Anthropic 模型因 cache_control 报错）
   ├─ handler(overridden_request)
   ├─ 成功 → 返回
   └─ 失败 → 记录异常，继续下一个

3. 所有模型都失败 → 抛出最后一个异常
```

#### 代码示例

```python
from langchain.agents.middleware import ModelFallbackMiddleware

fallback = ModelFallbackMiddleware(
    "anthropic:claude-sonnet-4-5-20250929",  # 第一备选
    "openai:gpt-4o-mini",                     # 第二备选
)

# 主模型在 create_agent 中配置，不在 fallback 中
agent = create_agent(
    model="openai:gpt-4o",           # 主模型
    tools=[...],
    middleware=[fallback],            # 降级链
)
```

---

### 4.4 ModelCallLimitMiddleware — 模型调用次数限制

**用途：** 限制模型调用次数（线程级 / 单次运行级），超限后终止 Agent。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `thread_limit` | `int \| None` | `None` | 每线程最大调用次数（跨 run 持久化） |
| `run_limit` | `int \| None` | `None` | 单次 run 最大调用次数（临时） |
| `exit_behavior` | `"end" \| "error"` | `"end"` | 超限行为 |

#### 内部处理过程

```
before_model 钩子（每次模型调用前）：

1. 读取当前计数
   ├─ thread_model_call_count（PrivateStateAttr，持久化）
   └─ run_model_call_count（UntrackedValue，仅当前 run）

2. 检查是否已超限
   ├─ thread_limit 不为 None 且 count ≥ thread_limit → 超限
   ├─ run_limit 不为 None 且 count ≥ run_limit → 超限
   └─ 未超限 → 返回 None（继续）

3. 超限处理
   ├─ exit_behavior="end" →
   │   返回 {"jump_to": "end", "messages": [AIMessage("模型调用次数已达上限...")]}
   └─ exit_behavior="error" →
       抛出 ModelCallLimitExceededError(thread_count, run_count, ...)

after_model 钩子（每次模型调用后）：
1. thread_model_call_count += 1
2. run_model_call_count += 1
```

#### 代码示例

```python
from langchain.agents.middleware import ModelCallLimitMiddleware

# 限制单次 run 最多 20 次模型调用
limit = ModelCallLimitMiddleware(run_limit=20)

# 限制线程级最多 100 次，超限报错
limit = ModelCallLimitMiddleware(
    thread_limit=100,
    exit_behavior="error",
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[limit])
```

---

### 4.5 ToolRetryMiddleware — 工具调用重试

**用途：** 工具执行失败时自动重试，支持按工具过滤和指数退避。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_retries` | `int` | `2` | 最大重试次数 |
| `tools` | `list[BaseTool \| str] \| None` | `None` | 目标工具；`None` = 全部 |
| `retry_on` | `tuple \| Callable` | `(Exception,)` | 重试的异常类型 |
| `on_failure` | `"continue" \| "error" \| Callable` | `"continue"` | 重试耗尽后行为 |
| `backoff_factor` | `float` | `2.0` | 退避倍数 |
| `initial_delay` | `float` | `1.0` | 初始延迟（秒） |
| `max_delay` | `float` | `60.0` | 最大延迟（秒） |
| `jitter` | `bool` | `True` | 随机抖动 |

#### 内部处理过程

```
wrap_tool_call 钩子：

1. 工具过滤检查
   ├─ tools=None → 对所有工具生效
   ├─ 当前工具名在 _tool_filter 中 → 进入重试逻辑
   └─ 当前工具不在过滤范围 → 直接 handler(request)

2. 重试循环
   ├─ GraphBubbleUp 异常（Interrupt/Command）→ 立即传播，不重试
   ├─ retry_on 范围内的异常 →
   │   ├─ 还有重试次数 → 计算延迟等待后重试
   │   └─ 重试耗尽 → _handle_failure()
   └─ 非 retry_on 异常 → 立即传播（不进入 on_failure）

3. on_failure 处理
   ├─ "continue" → 返回 ToolMessage(content="Tool 'X' failed after N attempt(s)...")
   ├─ "error" → 重新抛出
   └─ callable → callable(exception) 结果作为 ToolMessage content
```

#### 代码示例

```python
from langchain.agents.middleware import ToolRetryMiddleware

# 所有工具重试 2 次
retry = ToolRetryMiddleware(max_retries=2)

# 只对特定工具重试
retry = ToolRetryMiddleware(
    max_retries=3,
    tools=["search_api", "database_query"],
    retry_on=(ConnectionError, TimeoutError),
)

# 与 ToolErrorMiddleware 组合：内层重试，外层错误处理
from langchain.agents.middleware import ToolErrorMiddleware

error_handler = ToolErrorMiddleware(
    on_error=lambda exc, req: f"工具 {req.tool_call['name']} 出错: {type(exc).__name__}"
)
retry = ToolRetryMiddleware(max_retries=2, on_failure="error")

agent = create_agent(
    model="openai:gpt-4o",
    tools=[...],
    middleware=[error_handler, retry],  # error_handler 在外层，retry 在内层
)
```

---

### 4.6 ToolErrorMiddleware — 工具错误处理

**用途：** 将工具执行异常转换为错误 `ToolMessage` 返回给模型，而非直接崩溃。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `on_error` | `Callable \| None` | `None` | 同步错误处理器 `(Exception, ToolCallRequest) -> str \| None` |
| `aon_error` | `Callable \| None` | `None` | 异步错误处理器 |
| `tools` | `list \| None` | `None` | 目标工具；`None` = 全部 |

#### 内部处理过程

```
wrap_tool_call 钩子：

1. 工具过滤检查
   └─ 不在目标范围 → 直接 handler(request)

2. 调用 handler(request)
   ├─ 成功 → 返回结果
   ├─ GraphBubbleUp → 立即传播
   └─ 其他异常 → 调用 on_error(exception, request)

3. on_error 返回值
   ├─ str → 包装为 ToolMessage(content=str, status="error")
   ├─ None → 重新抛出异常
   └─ list[ContentBlock] → 包装为多模态 ToolMessage
```

#### 代码示例

```python
from langchain.agents.middleware import ToolErrorMiddleware

def handle_tool_error(exc, request):
    tool_name = request.tool_call["name"]
    if isinstance(exc, PermissionError):
        return f"权限不足，无法执行 {tool_name}"
    if isinstance(exc, TimeoutError):
        return f"{tool_name} 执行超时"
    # 返回 None = 让异常继续传播
    return None

error_mw = ToolErrorMiddleware(on_error=handle_tool_error)
agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[error_mw])
```

---

### 4.7 ToolCallLimitMiddleware — 工具调用次数限制

**用途：** 限制工具调用次数（全局或按工具名），支持线程级和运行级。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `tool_name` | `str \| None` | `None` | 目标工具；`None` = 全局 |
| `thread_limit` | `int \| None` | `None` | 线程级上限（持久化） |
| `run_limit` | `int \| None` | `None` | 运行级上限（临时） |
| `exit_behavior` | `"continue" \| "error" \| "end"` | `"continue"` | 超限行为 |

#### 内部处理过程

```
after_model 钩子（模型调用后检查）：

1. 检查最后一条 AIMessage 的 tool_calls
   └─ 无 tool_calls → 返回 None

2. 逐个检查 tool_call
   ├─ 获取当前工具调用计数
   │   ├─ thread_tool_call_count[tool_name 或 "__all__"]
   │   └─ run_tool_call_count[tool_name 或 "__all__"]
   ├─ 判断是否超限
   │   ├─ 未超限 → 加入 allowed 列表，计数 +1
   │   └─ 已超限 → 加入 blocked 列表

3. 处理 blocked 列表
   ├─ "continue" →
   │   为每个 blocked 调用注入错误 ToolMessage:
   │   "Tool call limit exceeded. Do not call '{name}' again."
   │   （模型看到后自行决定是否停止）
   ├─ "error" →
   │   抛出 ToolCallLimitExceededError
   └─ "end" →
       注入 ToolMessage + AIMessage + {"jump_to": "end"}
       （若有并行工具调用到其他工具 → NotImplementedError）
```

#### 代码示例

```python
from langchain.agents.middleware import ToolCallLimitMiddleware

# 全局：每次 run 最多 50 次工具调用
global_limit = ToolCallLimitMiddleware(run_limit=50)

# 特定工具限制
search_limit = ToolCallLimitMiddleware(
    tool_name="web_search",
    run_limit=10,
    exit_behavior="continue",  # 超限后注入错误消息，让模型自己决定
)

# 多实例独立跟踪
agent = create_agent(
    model="openai:gpt-4o",
    tools=[...],
    middleware=[
        ToolCallLimitMiddleware(tool_name="search", run_limit=10),
        ToolCallLimitMiddleware(tool_name="code_exec", run_limit=20),
        ToolCallLimitMiddleware(run_limit=50),  # 全局兜底
    ],
)
```

---

### 4.8 HumanInTheLoopMiddleware — 人机协同审批

**用途：** 工具调用前暂停，等待人类审批（approve/edit/reject/respond）。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `interrupt_on` | `dict[str, bool \| InterruptOnConfig]` | *必填* | 工具审批配置 |
| `description_prefix` | `str` | `"Tool execution requires approval"` | 审批描述前缀 |

#### InterruptOnConfig

```python
InterruptOnConfig(
    when=lambda request: ...,     # 条件判断（返回 True 才中断）
    description="...",            # 静态描述 或 callable(tool_call, state, runtime) -> str
    allow_decisions=["approve", "edit", "reject", "respond"],
)
```

#### 内部处理过程

```
after_model 钩子（模型调用后、工具执行前）：

1. 提取最后一条 AIMessage 的所有 tool_calls

2. 为每个 tool_call 判断是否需要审批
   ├─ interrupt_on[tool_name] = False → 自动通过
   ├─ interrupt_on[tool_name] = True → 需要审批
   └─ interrupt_on[tool_name] = InterruptOnConfig →
       ├─ 调用 when(request) → False → 自动通过
       └─ when(request) → True → 需要审批

3. 构建 HITLRequest 列表
   ├─ description = description_prefix + tool_name + args
   └─ 或调用 description callable 生成

4. 调用 langgraph.types.interrupt(HITLRequest 列表)
   └─ Agent 暂停，等待人类决策

5. 处理人类决策（恢复时）
   ├─ "approve" → 保持原始 tool_call 不变
   ├─ "edit" → 替换 tool_name / args
   ├─ "reject" → 注入错误 ToolMessage，跳过执行
   └─ "respond" → 注入人类回复作为 ToolMessage，跳过执行

6. 验证：决策数量必须 == 中断的工具调用数量（否则 ValueError）
```

#### 代码示例

```python
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig

# 简单模式：特定工具总是需要审批
hitl = HumanInTheLoopMiddleware(
    interrupt_on={
        "delete_file": True,
        "send_email": True,
    }
)

# 条件审批：只审批大额操作
hitl = HumanInTheLoopMiddleware(
    interrupt_on={
        "transfer_money": InterruptOnConfig(
            when=lambda req: req.tool_call["args"].get("amount", 0) > 10000,
            description=lambda tc, state, runtime: (
                f"转账 {tc['args']['amount']} 元到 {tc['args']['to']}"
            ),
        ),
    }
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[hitl])
```

---

### 4.9 PIIMiddleware — 敏感信息检测

**用途：** 检测并处理对话中的个人敏感信息（邮箱、信用卡号、IP 等）。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `pii_type` | `str` | *必填* | PII 类型：`"email"`, `"credit_card"`, `"ip"`, `"mac_address"`, `"url"` 或自定义 |
| `strategy` | `str` | `"redact"` | 处理策略：`"block"` / `"redact"` / `"mask"` / `"hash"` |
| `detector` | `Callable \| str \| None` | `None` | 自定义检测器（函数或正则） |
| `apply_to_input` | `bool` | `True` | 检测用户输入 |
| `apply_to_output` | `bool` | `False` | 检测模型输出 |
| `apply_to_tool_results` | `bool` | `False` | 检测工具结果 |

#### 内部处理过程

```
before_model 钩子（apply_to_input）：

1. 找到最后一条 HumanMessage
2. 运行 PII 检测器
   ├─ 内置类型 → 使用预定义正则
   └─ 自定义 detector → 调用 detector(text) → list[PIIMatch]
3. 按 strategy 处理
   ├─ "block" → 抛出 PIIDetectionError(pii_type, matches)
   ├─ "redact" → 替换为 [REDACTED_email]
   ├─ "mask" → 部分遮掩 ****-1234
   └─ "hash" → 替换为 <email_hash:a1b2c3d4>
4. 替换 state 中的消息内容

after_model 钩子（apply_to_output）：
- 同样流程处理最后一条 AIMessage

Stream Transformer（apply_to_output 或 apply_to_tool_results）：
- 实时扫描流式 token
- 使用 lookback buffer（128 chars）处理跨 delta 边界的 PII
- "block" 策略在流中直接抛出 PIIDetectionError
```

#### 代码示例

```python
from langchain.agents.middleware import PIIMiddleware

# 检测邮箱，替换处理
email_guard = PIIMiddleware(pii_type="email", strategy="redact")

# 检测信用卡号，阻止执行
cc_guard = PIIMiddleware(
    pii_type="credit_card",
    strategy="block",
    apply_to_input=True,
    apply_to_output=True,
)

# 自定义 PII 类型（用正则）
phone_guard = PIIMiddleware(
    pii_type="phone",
    strategy="mask",
    detector=r"1[3-9]\d{9}",  # 中国手机号正则
    apply_to_input=True,
)

# 自定义检测函数
def detect_id_number(text):
    """检测身份证号"""
    import re
    return [(m.start(), m.end(), m.group()) for m in re.finditer(r"\d{17}[\dXx]", text)]

id_guard = PIIMiddleware(
    pii_type="id_number",
    strategy="hash",
    detector=detect_id_number,
)

agent = create_agent(
    model="openai:gpt-4o",
    tools=[...],
    middleware=[email_guard, cc_guard, phone_guard],
)
```

---

### 4.10 ContextEditingMiddleware — 上下文裁剪

**用途：** 当 token 数超阈值时，清除旧的工具调用结果（保留最近 N 个），类似 Anthropic 的 `clear_tool_uses`。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `edits` | `Iterable[ContextEdit] \| None` | `None` | 编辑策略列表；默认 = `[ClearToolUsesEdit()]` |
| `token_count_method` | `"approximate" \| "model"` | `"approximate"` | token 计数方式 |

#### ClearToolUsesEdit 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `trigger` | `int` | `100_000` | 触发裁剪的 token 阈值 |
| `keep` | `int` | `3` | 保留最近 N 个工具结果 |
| `clear_at_least` | `int` | `0` | 每次至少清理的 token 数 |
| `clear_tool_inputs` | `bool` | `False` | 同时清除 AIMessage 上对应的 tool_call args |
| `exclude_tools` | `Sequence[str]` | `()` | 排除的工具名 |
| `placeholder` | `str` | `"[cleared]"` | 清除后的占位文本 |

#### 内部处理过程

```
wrap_model_call 钩子：

1. 深拷贝消息列表（不修改原始 state）

2. 计算 token 总数
   ├─ "approximate" → count_tokens_approximately()
   └─ "model" → model.get_num_tokens_from_messages()

3. 检查是否触发（total_tokens ≥ trigger）
   └─ 未触发 → 直接用原始消息调用 handler

4. 找出所有 ToolMessage（按时间排序）
   ├─ 排除最近 keep 个（保留最新的）
   ├─ 排除 exclude_tools 中的工具
   └─ 排除已标记 cleared 的（response_metadata["context_editing"]["cleared"]）

5. 逐个清除
   ├─ message.content = placeholder
   ├─ message.artifact = None
   ├─ message.response_metadata["context_editing"]["cleared"] = True
   └─ 若 clear_tool_inputs=True：
       ├─ 找到对应 AIMessage 的 tool_call
       ├─ tool_call.args = {}
       └─ 记录 cleared_tool_inputs

6. 检查是否清够了 clear_at_least tokens
   └─ 不够 → 继续清除更多（但不碰 keep 范围内的）

7. handler(request.override(messages=edited_messages))
```

#### 代码示例

```python
from langchain.agents.middleware import ContextEditingMiddleware, ClearToolUsesEdit

# 默认行为：100k tokens 时清除旧工具结果，保留最近 3 个
ctx_edit = ContextEditingMiddleware()

# 自定义
ctx_edit = ContextEditingMiddleware(
    edits=[ClearToolUsesEdit(
        trigger=50_000,        # 50k tokens 触发
        keep=5,                # 保留最近 5 个工具结果
        clear_at_least=10_000, # 每次至少清 10k tokens
        clear_tool_inputs=True,
        exclude_tools=["important_tool"],
        placeholder="[内容已清除]",
    )],
    token_count_method="model",  # 精确计数
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[ctx_edit])
```

---

### 4.11 FilesystemFileSearchMiddleware — 文件搜索

**用途：** 提供 `glob_search` 和 `grep_search` 工具，在本地文件系统中搜索文件和内容。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `root_path` | `str` | *必填* | 搜索根目录（沙箱边界） |
| `use_ripgrep` | `bool` | `True` | 使用 ripgrep；不可用时回退到 Python |
| `max_file_size_mb` | `int` | `10` | 最大搜索文件大小（MB） |

#### 提供的工具

- **`glob_search(pattern, path="/")`** — 文件模式匹配，返回虚拟路径列表
- **`grep_search(pattern, path="/", include=None, output_mode="files_with_matches")`** — 正则内容搜索

#### 代码示例

```python
from langchain.agents.middleware import FilesystemFileSearchMiddleware

fs_search = FilesystemFileSearchMiddleware(
    root_path="/home/user/project",
    use_ripgrep=True,
    max_file_size_mb=5,
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[fs_search])
# Agent 现在可以使用 glob_search 和 grep_search 工具
```

---

### 4.12 TodoListMiddleware — 任务规划

**用途：** 为 Agent 提供 `write_todos` 工具，进行结构化任务规划和进度跟踪。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `system_prompt` | `str` | 内置长提示 | 指导 Agent 何时/如何使用 todo |
| `tool_description` | `str` | 内置描述 | write_todos 工具的描述 |

#### 内部处理过程

```
wrap_model_call 钩子：
1. 将 todo 系统提示追加到系统消息中

after_model 钩子：
1. 检查最后 AIMessage 的 tool_calls
2. 若有多个并行 write_todos 调用 →
   返回错误 ToolMessage（不允许并行，因为会覆盖彼此）

write_todos 工具：
1. 接收 todos: list[{content, status}]
2. 返回 Command(update={"todos": new_todos, "messages": [确认 ToolMessage]})
3. 整个 todo 列表被替换（增量更新由模型自行管理）
```

#### 代码示例

```python
from langchain.agents.middleware import TodoListMiddleware

todo = TodoListMiddleware()

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[todo])

# Agent 会在复杂任务中自动调用 write_todos：
# write_todos(todos=[
#     {"content": "搜索相关文档", "status": "completed"},
#     {"content": "分析数据", "status": "in_progress"},
#     {"content": "生成报告", "status": "pending"},
# ])
```

---

### 4.13 LLMToolSelectorMiddleware — 智能工具选择

**用途：** 用辅助 LLM 从大量工具中选出与当前查询最相关的，减少 token 消耗。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `model` | `str \| BaseChatModel \| None` | `None` | 选择用模型；`None` = 用主模型 |
| `system_prompt` | `str` | 内置提示 | 选择指令 |
| `max_tools` | `int \| None` | `None` | 最多选几个工具 |
| `always_include` | `list[str] \| None` | `None` | 始终包含的工具名 |

#### 内部处理过程

```
wrap_model_call 钩子：

1. 收集所有可用工具
   ├─ 分离 BaseTool 实例和 dict 工具
   └─ 剔除 always_include 中的工具（它们直接进入结果）

2. 构建结构化输出 schema
   └─ TypedDict，每个工具名是 Literal 类型 + 工具描述注解

3. 调用选择模型
   ├─ 输入：用户消息 + 工具列表 + 描述
   ├─ 输出：选中的工具名列表（按相关性排序）
   └─ 若 max_tools 不为 None → 只取前 max_tools 个

4. 过滤 request.tools
   └─ 保留选中的 + always_include 的 + dict 工具

5. handler(request.override(tools=filtered_tools))
```

#### 代码示例

```python
from langchain.agents.middleware import LLMToolSelectorMiddleware

# 自动选择，最多 5 个工具
selector = LLMToolSelectorMiddleware(
    model="openai:gpt-4o-mini",  # 用小模型选择，降低成本
    max_tools=5,
    always_include=["search"],    # search 总是可用
)

agent = create_agent(
    model="openai:gpt-4o",
    tools=[tool1, tool2, ..., tool50],  # 50 个工具
    middleware=[selector],              # 每次只选最相关的 5 个
)
```

---

### 4.14 LLMToolEmulator — 工具模拟执行

**用途：** 测试模式下，用 LLM 模拟工具返回结果，而非真正执行工具。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `tools` | `list \| None` | `None` | 要模拟的工具；`None` = 全部；`[]` = 不模拟 |
| `model` | `str \| BaseChatModel \| None` | `None` | 模拟用模型；默认 claude-sonnet-4-5 |

#### 内部处理过程

```
wrap_tool_call 钩子：

1. 检查当前工具是否在模拟范围
   ├─ tools=None → 全部模拟
   ├─ tools=[] → 不模拟任何工具
   └─ 工具名在 tools 列表中 → 模拟

2. 构建模拟提示
   ├─ "You are emulating the tool '{name}'."
   ├─ 工具描述（若有）
   └─ 调用参数

3. 调用模拟 LLM（temperature=1 增加多样性）

4. 返回 ToolMessage(content=模拟结果, tool_call_id=...)
   └─ 真实工具不会被执行
```

#### 代码示例

```python
from langchain.agents.middleware import LLMToolEmulator

# 模拟所有工具（测试用）
emulator = LLMToolEmulator()

# 只模拟特定工具
emulator = LLMToolEmulator(
    tools=["send_email", "delete_file"],  # 这些工具不真正执行
    model="openai:gpt-4o-mini",
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[emulator])
```

---

### 4.15 ProviderToolSearchMiddleware — 服务端工具搜索

**用途：** 将部分工具标记为"延迟加载"，由 provider 原生的服务端工具搜索按需获取 schema。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `searchable_tools` | `list \| None` | `None` | 可延迟加载的工具列表 |

#### 支持的 Provider

| Provider | 搜索工具 |
|---|---|
| Anthropic | `tool_search_tool_bm25_20251119` |
| OpenAI | `tool_search` |

#### 代码示例

```python
from langchain.agents.middleware import ProviderToolSearchMiddleware

# 将不常用的工具延迟加载
tool_search = ProviderToolSearchMiddleware(
    searchable_tools=["rare_tool_1", "rare_tool_2", "rare_tool_3"],
)

agent = create_agent(
    model="anthropic:claude-sonnet-4-5-20250929",
    tools=[common_tool, rare_tool_1, rare_tool_2, rare_tool_3],
    middleware=[tool_search],
)
# 常用工具的 schema 每次都发送
# rare_tool_* 的 schema 由 Anthropic 服务端按需搜索获取
```

---

### 4.16 ShellToolMiddleware — Shell 会话

**用途：** 为 Agent 提供持久 Shell 会话工具，支持宿主机/Codex 沙箱/Docker 三种隔离模式。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `workspace_root` | `str \| None` | `None` | 工作目录；`None` = 自动创建临时目录 |
| `execution_policy` | `BaseExecutionPolicy` | `HostExecutionPolicy` | 执行隔离策略 |
| `startup_commands` | `list \| None` | `None` | 启动时执行的命令 |
| `shutdown_commands` | `list \| None` | `None` | 关闭时执行的命令 |
| `redaction_rules` | `list[RedactionRule] \| None` | `None` | 输出脱敏规则 |
| `shell_command` | `str` | `"/bin/bash"` | Shell 可执行文件 |
| `env` | `dict \| None` | `None` | 环境变量 |

#### 内部处理过程

```
before_agent 钩子：
1. 启动 Shell 子进程（stdin/stdout/stderr 管道）
2. 执行 startup_commands
   └─ 任一失败 → 抛出 RuntimeError

shell 工具调用：
1. 将命令写入 stdin
2. 追加唯一标记 __LC_SHELL_DONE__{uuid}
3. 通过 reader 线程读取 stdout 直到标记出现
4. 输出截断（max_output_lines / max_output_bytes）
5. 应用 redaction_rules 脱敏
6. 超时 → 重启 session + 返回错误

after_agent 钩子：
1. 执行 shutdown_commands（失败不抛异常）
2. weakref.finalize 释放资源（进程 + 临时目录）
```

#### 代码示例

```python
from langchain.agents.middleware import (
    ShellToolMiddleware,
    HostExecutionPolicy,
    DockerExecutionPolicy,
    CodexSandboxExecutionPolicy,
    RedactionRule,
)

# 宿主机模式（开发环境）
shell = ShellToolMiddleware(
    workspace_root="/home/user/project",
    execution_policy=HostExecutionPolicy(
        timeout=30,
        max_output_lines=100,
    ),
    startup_commands=["source .env", "cd src"],
    env={"DEBUG": "1"},
)

# Docker 隔离模式（生产环境）
shell = ShellToolMiddleware(
    execution_policy=DockerExecutionPolicy(
        image="python:3.12-slim",
        timeout=60,
    ),
)

# 输出脱敏
shell = ShellToolMiddleware(
    redaction_rules=[
        RedactionRule(pattern=r"password=\S+", replacement="password=***"),
        RedactionRule(pattern=r"[A-Za-z0-9+/]{40,}", replacement="[REDACTED_KEY]"),
    ],
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[shell])
```

---

## 五、DeepAgents 扩展中间件

### 5.1 SummarizationMiddleware (deepagents) — 增强压缩

**用途：** LangChain SummarizationMiddleware 的增强版，核心改进：**不破坏 state**、**历史落盘可恢复**、**ContextOverflowError 自动降级**。

#### 与 LangChain 版本的关键差异

| 特性 | LangChain 版 | deepagents 版 |
|---|---|---|
| state 修改方式 | 破坏性清空重建 | **非破坏性**（原始消息留在 state） |
| 被压缩消息去向 | **永久丢失** | **落盘**到 `/conversation_history/{thread_id}.md` |
| SystemMessage 安全 | ❌ state 中的会被删 | ✅ 安全 |
| 默认触发 | `None`（不触发） | 自动计算 |
| 工具参数截断 | 无 | 预压缩阶段截断大参数 |
| ContextOverflowError | 不处理 | 自动压缩+重试 |

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `model` | `str \| BaseChatModel` | *必填* | 摘要用 LLM |
| `backend` | `BACKEND_TYPES` | *必填* | 历史落盘后端 |
| `trigger` | `ContextSize \| ... \| None` | `None` | 触发条件 |
| `keep` | `ContextSize` | `("messages", 20)` | 保留量 |
| `truncate_args_settings` | `TruncateArgsSettings \| None` | `None` | 工具参数截断设置 |

#### 内部处理过程

```
wrap_model_call 钩子：

1. 重建有效消息列表
   ├─ 从 _summarization_event 获取之前的压缩事件
   └─ 将摘要消息 + 保留消息拼接为当前有效消息

2. 预压缩：工具参数截断（若配置）
   ├─ 扫描旧消息中的 write_file/edit_file 工具调用
   ├─ 大参数截断到 max_length（默认 2000 chars）
   ├─ 截断后重新计算 token
   └─ 若 token 已降到阈值以下 → 跳过完整压缩

3. 检查是否触发压缩
   ├─ 未触发 → request.override(messages=effective_messages) → handler()
   └─ 触发 → 进入压缩流程

4. 提取内联媒体
   ├─ base64/SVG 等 → 上传到 backend
   └─ 替换为路径引用

5. 历史落盘
   ├─ 过滤掉之前的摘要消息（避免重复存储）
   ├─ 将被压缩消息以 XML/Markdown 写入：
   │   /conversation_history/{thread_id}.md
   └─ 失败 → 非致命（警告，file_path=None）

6. 生成摘要
   ├─ DEEPAGENTS_DEFAULT_SUMMARY_PROMPT（包含媒体引用附录）
   └─ 摘要消息内容包含落盘路径引用

7. 构建 ExtendedModelResponse
   ├─ model_response = handler(request.override(messages=new_messages))
   └─ command = Command(update={"_summarization_event": event})

8. ContextOverflowError 降级
   └─ handler() 抛出 → 执行压缩 → 重试
```

#### 代码示例

```python
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    create_summarization_middleware,
    create_summarization_tool_middleware,
)

# 使用工厂函数（自动计算默认值）
summ = create_summarization_middleware(
    model="openai:gpt-4o-mini",
    backend=backend,
)
# 若模型有 profile：trigger=("fraction", 0.85), keep=("fraction", 0.10)
# 若无 profile：trigger=("tokens", 170000), keep=("messages", 6)

# 手动配置
summ = SummarizationMiddleware(
    model="openai:gpt-4o-mini",
    backend=backend,
    trigger=("fraction", 0.8),
    keep=("fraction", 0.15),
)

# 配合手动压缩工具
summ_tool = SummarizationToolMiddleware(summ)
# Agent 获得 compact_conversation 工具，可主动触发压缩

# 一步创建两者
summ, summ_tool = create_summarization_tool_middleware(
    model="openai:gpt-4o-mini",
    backend=backend,
)
```

---

### 5.2 FilesystemMiddleware — 文件系统工具集

**用途：** 提供完整的文件系统工具集（`ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`）+ 可选的 `execute` 工具。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `backend` | `BACKEND_TYPES \| None` | `None` → `StateBackend()` | 文件后端 |
| `system_prompt` | `str \| None` | `None`（动态） | 系统提示 |
| `tool_token_limit_before_evict` | `int \| None` | `20000` | 工具结果超此 token 则驱逐到文件 |
| `human_message_token_limit_before_evict` | `int \| None` | `50000` | HumanMessage 超此 token 则驱逐 |
| `max_execute_timeout` | `int` | `3600` | execute 工具最大超时 |

#### 内部处理过程

```
wrap_model_call 钩子：

1. 动态构建系统提示
   ├─ 文件系统工具说明
   ├─ execute 工具说明（若 backend 支持 SandboxBackendProtocol）
   └─ 路径映射（host path → container path）

2. 过滤 execute 工具
   └─ backend 不支持 SandboxBackendProtocol → 从 tools 中移除

3. HumanMessage 驱逐
   ├─ 检查每条 HumanMessage 的 token 数
   ├─ 超限 → 写入 /conversation_history/{uuid}.md
   ├─ 标记 lc_evicted_to 属性
   └─ 替换为截断版本 + 路径引用

wrap_tool_call 钩子：

1. 执行工具
2. 检查结果大小
   ├─ 小于 tool_token_limit → 直接返回
   └─ 超限 → 驱逐处理：
       ├─ 写入 /large_tool_results/{tool_call_id}
       ├─ 替换为 head + tail 预览 + 路径引用
       └─ 排除的工具：ls, glob, grep, read_file, edit_file, write_file
```

#### 代码示例

```python
from deepagents.middleware import FilesystemMiddleware, FilesystemPermission

# 基础用法
fs = FilesystemMiddleware(backend=backend)

# 带权限控制
fs = FilesystemMiddleware(
    backend=backend,
    _permissions=[
        FilesystemPermission(pattern="/etc/**", decision="deny"),
        FilesystemPermission(pattern="/home/user/project/**", decision="allow"),
        FilesystemPermission(pattern="*.env", decision="deny"),
    ],
)

# 自定义工具描述
fs = FilesystemMiddleware(
    backend=backend,
    custom_tool_descriptions={
        "read_file": "读取项目文件，支持分页",
    },
)
```

---

### 5.3 MemoryMiddleware — AGENTS.md 记忆注入

**用途：** 从 backend 加载 `AGENTS.md` 文件内容，注入到系统提示中。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `backend` | `BACKEND_TYPES` | *必填* | 文件后端 |
| `sources` | `list[str]` | *必填* | AGENTS.md 文件路径列表 |
| `add_cache_control` | `bool` | `False` | 添加 Anthropic 缓存标记 |
| `system_prompt` | `str \| None` | 内置提示 | 必须包含 `{agent_memory}` 占位符 |

#### 内部处理过程

```
before_agent 钩子：
1. 从 backend 下载所有 sources 中的 AGENTS.md
2. 存储为 dict[str, str]（path → content）
3. 跳过 HTML 注释
4. 文件不存在 → 静默跳过
5. 存入 state["memory_contents"]（PrivateStateAttr）

wrap_model_call 钩子：
1. 格式化记忆内容（路径 + 内容）
2. 替换系统提示中的 {agent_memory} 占位符
3. 若 add_cache_control=True 且模型是 ChatAnthropic：
   └─ 在最后一个系统消息 content block 上添加 cache_control
```

#### 代码示例

```python
from deepagents.middleware import MemoryMiddleware

memory = MemoryMiddleware(
    backend=backend,
    sources=["/project/AGENTS.md", "/project/.claude/AGENTS.md"],
    add_cache_control=True,  # Anthropic 缓存优化
)

agent = create_agent(model="anthropic:claude-sonnet-4-5", tools=[...], middleware=[memory])
```

---

### 5.4 SkillsMiddleware — 技能加载与注入

**用途：** 从 backend 加载 `SKILL.md` 文件（含 YAML frontmatter），注入技能元数据到系统提示（渐进式披露：先看摘要，需要时再读取完整内容）。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `backend` | `BACKEND_TYPES` | *必填* | 文件后端 |
| `sources` | `Sequence[SkillSource]` | *必填* | 技能源目录列表 |
| `system_prompt` | `str \| None` | 内置提示 | 必须包含 `{skills_locations}` 等占位符 |

#### 内部处理过程

```
before_agent 钩子：
1. 扫描每个 source 目录下的子目录
2. 查找包含 SKILL.md 的子目录
3. 解析 YAML frontmatter（name, description, compatibility）
4. 验证技能名（1-64 chars, lowercase + hyphens）
5. 后源覆盖前源（last-one-wins by name）
6. 加载错误收集到 warnings

wrap_model_call 钩子：
1. 格式化技能列表（name + description + path）
2. 替换系统提示中的占位符：
   ├─ {skills_locations}
   ├─ {skills_list}
   └─ {skills_load_warnings}
3. 模型看到摘要后，需要时用 read_file(SKILL.md) 读全文
```

#### 代码示例

```python
from deepagents.middleware import SkillsMiddleware

skills = SkillsMiddleware(
    backend=backend,
    sources=[
        "/skills/built_in",
        "/skills/custom",
        ("/skills/experimental", "Experimental"),  # (路径, 标签)
    ],
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[skills])
```

---

### 5.5 SubAgentMiddleware — 子代理调度

**用途：** 提供 `task` 工具，启动隔离子代理处理子任务。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `backend` | `BackendProtocol` | *必填* | 文件后端 |
| `subagents` | `Sequence[SubAgent \| CompiledSubAgent]` | *必填* | 子代理定义列表 |
| `system_prompt` | `str \| None` | 内置提示 | task 工具使用指导 |

#### 内部处理过程

```
wrap_model_call 钩子：
1. 在系统提示中追加子代理使用说明 + 可用类型列表

task 工具调用：
1. 接收 agent_type + description + 可选参数
2. 查找对应的 SubAgent 定义
3. 创建隔离的 Agent 实例
   ├─ 排除 messages, todos, structured_response
   └─ 排除 private_state_keys
4. 运行子代理到完成
5. 提取结果
   ├─ structured_response 非空 → JSON 序列化
   └─ 否则 → 最后一条 AIMessage 文本
6. 返回 ToolMessage(content=结果)
```

#### 代码示例

```python
from deepagents.middleware import SubAgentMiddleware, SubAgent

sub_mw = SubAgentMiddleware(
    backend=backend,
    subagents=[
        SubAgent(
            name="researcher",
            description="专门搜索和整理资料",
            tools=[search_tool, read_tool],
            system_prompt="你是研究员，负责搜索和整理信息。",
        ),
        SubAgent(
            name="coder",
            description="编写和调试代码",
            tools=[write_tool, execute_tool],
        ),
    ],
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[sub_mw])

# Agent 会这样使用：
# task(agent_type="researcher", description="搜索 2024 年 AI 趋势")
# task(agent_type="coder", description="编写数据清洗脚本")
```

---

### 5.6 AsyncSubAgentMiddleware — 异步后台任务

**用途：** 提供工具在远程 LangGraph Platform 上启动/监控/管理后台任务。

#### 提供的工具

| 工具 | 说明 |
|---|---|
| `start_async_task` | 启动后台任务，立即返回 task_id |
| `check_async_task` | 检查任务状态和结果 |
| `update_async_task` | 更新任务输入（中断当前运行） |
| `cancel_async_task` | 取消任务 |
| `list_async_tasks` | 列出所有任务及状态 |

#### 代码示例

```python
from deepagents.middleware import AsyncSubAgentMiddleware, AsyncSubAgent

async_mw = AsyncSubAgentMiddleware(
    async_subagents=[
        AsyncSubAgent(
            name="data_pipeline",
            url="https://pipeline.langgraph.app",
            description="数据处理流水线",
        ),
    ],
)

agent = create_agent(model="openai:gpt-4o", tools=[...], middleware=[async_mw])
```

---

### 5.7 RubricMiddleware — 自我评估迭代

**用途：** Agent 完成后，用评分 Agent 按评分标准（rubric）自我评估。不满足则注入反馈继续迭代。

#### 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `model` | `str \| BaseChatModel` | *必填* | 评分模型 |
| `max_iterations` | `int` | `3` | 最大迭代次数（1-20） |
| `on_evaluation` | `Callable \| None` | `None` | 评估回调 |

#### 内部处理过程

```
after_agent 钩子（Agent 正常结束后）：

1. 检查 state 中是否有 rubric
   └─ 无 → 跳过（安全包含）

2. 构建评分输入
   ├─ 评分标准（rubric from state）
   ├─ 对话记录（截取最近 30 条，每条最多 4000 chars）
   └─ nonce 标记防注入

3. 调用评分模型（response_format=GraderResponse）
   ├─ 结构化输出：每个 criterion 的 pass/fail + 总体 verdict
   └─ 校验自洽性（如 satisfied 但有 failing criteria → 报错）

4. 根据 verdict 决策
   ├─ "satisfied" → 结束
   ├─ "needs_revision" →
   │   ├─ 注入 HumanMessage(content=反馈, name="rubric_grader")
   │   ├─ 返回 {"jump_to": "model"}（继续迭代）
   │   └─ iteration_count += 1
   ├─ "failed" → 结束（记录状态）
   └─ max_iterations 达到 → 结束

5. 调用 on_evaluation(RubricEvaluation) 回调
```

#### 代码示例

```python
from deepagents.middleware import RubricMiddleware

rubric = RubricMiddleware(
    model="openai:gpt-4o",
    max_iterations=3,
    on_evaluation=lambda eval: print(f"评分: {eval.verdict}"),
)

# 在 state 中传入 rubric（评分标准）
agent = create_agent(
    model="openai:gpt-4o",
    tools=[...],
    middleware=[rubric],
)
result = agent.invoke({
    "messages": [...],
    "rubric": [
        {"criterion": "回答包含数据来源", "description": "..."},
        {"criterion": "SQL 语法正确", "description": "..."},
    ],
})
```

---

### 5.8 PatchToolCallsMiddleware — 悬挂工具调用修补

**用途：** Agent 启动前，检查历史消息中未响应的工具调用，插入占位 `ToolMessage`。

#### 内部处理过程

```
before_agent 钩子：

1. 扫描所有消息中的 AIMessage.tool_calls
2. 为每个 tool_call 检查是否有对应的 ToolMessage
3. 找到悬挂的（无响应的）tool_call

4. 为每个悬挂 tool_call 生成占位 ToolMessage
   ├─ invalid_tool_calls →
   │   "Tool call {name} with id {id} could not be executed -
   │    arguments were malformed or truncated."
   └─ 正常但未完成的 →
       "Tool call {name} with id {id} was cancelled -
        another message came in before it could be completed."

5. 原子替换：
   [RemoveMessage(id=REMOVE_ALL_MESSAGES), *patched_messages]

6. 无悬挂 → 不做任何操作
```

#### 代码示例

```python
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware

# 放在中间件列表的最前面
agent = create_agent(
    model="openai:gpt-4o",
    tools=[...],
    middleware=[PatchToolCallsMiddleware(), ...],
)
# 解决中断恢复后模型看到悬挂 tool_call 报错的问题
```

---

## 六、中间件组合与排序

### 推荐排序（洋葱模型）

```python
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelRetryMiddleware,
    ModelFallbackMiddleware,
    ModelCallLimitMiddleware,
    ToolRetryMiddleware,
    ToolErrorMiddleware,
    ToolCallLimitMiddleware,
    HumanInTheLoopMiddleware,
    SummarizationMiddleware,
    ContextEditingMiddleware,
)

agent = create_agent(
    model="openai:gpt-4o",
    tools=[...],
    middleware=[
        # 外层：全局控制
        ModelCallLimitMiddleware(run_limit=30),

        # 中间：上下文管理
        ContextEditingMiddleware(),
        SummarizationMiddleware(
            model="openai:gpt-4o-mini",
            trigger=("fraction", 0.8),
            keep=("fraction", 0.1),
        ),

        # 内层：模型调用保障
        ModelRetryMiddleware(max_retries=2),
        ModelFallbackMiddleware("anthropic:claude-sonnet-4-5"),

        # 工具层
        ToolErrorMiddleware(on_error=lambda e, r: f"错误: {e}"),
        ToolRetryMiddleware(max_retries=1, on_failure="error"),
        ToolCallLimitMiddleware(run_limit=50),

        # 人机协同（最内层，最接近工具执行）
        HumanInTheLoopMiddleware(interrupt_on={"delete_file": True}),
    ],
)
```

### 组合原则

| 场景 | 推荐组合 |
|---|---|
| 重试 + 降级 | `ModelRetryMiddleware` 内层 + `ModelFallbackMiddleware` 外层 |
| 重试 + 错误处理 | `ToolRetryMiddleware(on_failure="error")` 内层 + `ToolErrorMiddleware` 外层 |
| 压缩 + 裁剪 | `ContextEditingMiddleware` 外层（轻量裁剪）+ `SummarizationMiddleware` 内层（全量压缩） |
| 限流 + 重试 | `ModelCallLimitMiddleware` 外层 + `ModelRetryMiddleware` 内层 |

---

## 七、自定义中间件编写

### 方式一：继承 AgentMiddleware

```python
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

class TokenTrackerMiddleware(AgentMiddleware):
    """跟踪每次 run 的 token 消耗"""

    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        last_msg = state["messages"][-1]
        if hasattr(last_msg, "usage_metadata") and last_msg.usage_metadata:
            tokens = last_msg.usage_metadata.get("total_tokens", 0)
            runtime.stream_writer({
                "type": "token_usage",
                "tokens": tokens,
            })
        return None
```

### 方式二：装饰器

```python
from langchain.agents.middleware import wrap_model_call, wrap_tool_call

@wrap_model_call
def log_model_calls(request, handler):
    """记录每次模型调用的耗时"""
    import time
    start = time.time()
    response = handler(request)
    elapsed = time.time() - start
    print(f"模型调用耗时: {elapsed:.2f}s, "
          f"消息数: {len(request.messages)}")
    return response

@wrap_tool_call
def audit_tool_calls(request, handler):
    """审计工具调用"""
    tool_name = request.tool_call["name"]
    args = request.tool_call["args"]
    print(f"[AUDIT] 调用工具: {tool_name}, 参数: {args}")

    result = handler(request)

    status = getattr(result, "status", "unknown")
    print(f"[AUDIT] 结果: {tool_name} → {status}")
    return result
```

### 方式三：带 state schema 的中间件

```python
from typing import Annotated, Any
from typing_extensions import NotRequired, TypedDict
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage
from langgraph.channels.untracked import UntrackedValue
from langgraph.runtime import Runtime
from langchain.agents.middleware.types import OmitFromSchema

class CounterState(TypedDict):
    model_calls: NotRequired[Annotated[int, UntrackedValue, OmitFromSchema()]]
    tool_calls: NotRequired[Annotated[int, UntrackedValue, OmitFromSchema()]]

class CountingMiddleware(AgentMiddleware):
    state_schema = CounterState

    def __init__(self, max_model_calls: int = 10):
        self.max_model_calls = max_model_calls

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        count = state.get("model_calls", 0)
        if count >= self.max_model_calls:
            return {
                "jump_to": "end",
                "messages": [AIMessage(content=f"已达 {self.max_model_calls} 次模型调用上限")],
            }
        return None

    def after_model(self, state, runtime):
        return {"model_calls": state.get("model_calls", 0) + 1}
```

---

> **源码版本：** langchain 1.3.14 + deepagents 0.6.12
> **编写日期：** 2026-10-14
> **数据来源：** 通读 `.venv/Lib/site-packages/langchain/agents/middleware/` 和 `deepagents/middleware/` 全部源码
