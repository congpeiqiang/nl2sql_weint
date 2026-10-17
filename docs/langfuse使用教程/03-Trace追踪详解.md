# 03 · Trace 追踪详解

> 深入理解 Langfuse 的四层数据模型、Observation 嵌套规则、OTel context 传递  
> 参考：[Langfuse 官方数据模型文档](https://langfuse.com/docs/observability/data-model)、[Best Practices](https://langfuse.com/docs/observability/best-practices)

---

## 1. 四层数据模型

Langfuse 的追踪基于四层结构：

```
Session（会话）── 1:N ── Trace（一次查询）── 1:N ── Observation（执行步骤，可嵌套成树）
                                                          └── Score（评分，挂在 trace 或 observation 上）
```

### 1.1 Session（会话）

| 属性 | 说明 |
|------|------|
| **定义** | 将多个 trace 分组的标识（典型：聊天线程中的多轮对话） |
| **对应** | LangGraph 的 `thread_id`、聊天窗口 ID |
| **作用** | 在 Sessions 页面查看同一会话的所有 trace，分析多轮交互 |
| **设置** | `session_id` 参数 |

```python
# 多轮对话自动归组
session_id = f"thread-{user_id}-{conversation_id}"

with langfuse.start_as_current_observation(
    name="chat-turn", as_type="agent",
    session_id=session_id,
    input="第一个问题",
):
    ...
```

### 1.2 Trace（追踪）

Trace 是最顶层的容器，代表一次完整的 Agent 任务。

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | str | Trace 名称（低基数、稳定标识） |
| `input` | Any | 用户输入 |
| `output` | Any | 最终输出 |
| `metadata` | dict | 任意 JSON 可序列化的键值对 |
| `session_id` | str | 所属会话 |
| `user_id` | str | 用户标识 |
| `tags` | list[str] | 标签 |
| `release` | str | 发版标记 |
| `scores` | list | 评估打分 |
| `timestamp` | datetime | 创建时间 |
| `latency` | float | 总耗时（秒） |
| `total_cost` | float | LLM 总成本（USD） |

**在 v4 SDK 中，Trace 由最外层 `start_as_current_observation(as_type="agent")` 自动创建。**

### 1.3 Observation（观察）

Trace 内的每一个步骤都是一个 Observation，可嵌套成树：

```
Trace
└── AGENT: chat_agent (root)
    ├── GENERATION: llm-call-1（规划）
    ├── SPAN: skill:schema-linking
    │   └── GENERATION: llm-call-2（schema 识别）
    ├── AGENT: nl2sql_agent（子 Agent）
    │   ├── GENERATION: llm-call-3（SQL 生成）
    │   └── TOOL: execute-sql（SQL 执行）
    └── GENERATION: llm-call-4（最终回答）
```

### 1.4 Score（评分）

Score 挂在 Trace 或 Observation 上，不参与树的嵌套：

```python
# Trace 级评分
langfuse.score_current_trace(name="correctness", value=0.9)

# Observation 级评分
langfuse.score_current_span(name="tool-success", value=1.0)
```

## 2. Observation 类型详解

### 2.1 类型对照表

| `as_type` | 语义 | 自动记录的特殊属性 | 典型场景 |
|-----------|------|-------------------|----------|
| `agent` | Agent 根节点 | 输入输出、完整子树 | 最外层包裹 |
| `generation` | LLM 调用 | `model`、`model_parameters`、token、cost | GPT/DeepSeek 生成 |
| `tool` | 工具调用 | 参数、结果、错误 | 执行 SQL、API 调用 |
| `span` | 通用步骤 | 输入输出 | 数据转换、格式处理 |
| `chain` | 链式调用 | 输入输出 | LangChain chain |
| `retriever` | 检索 | 查询、文档列表、分数 | 向量检索、全文检索 |
| `embedding` | 嵌入计算 | 模型、维度、token | 文本向量化 |
| `evaluator` | 评估 | 评分结果 | LLM-as-Judge |
| `guardrail` | 护栏 | 检查结果、拦截原因 | 安全检查、内容过滤 |

### 2.2 Generation 的特殊属性

`generation` 类型有额外的属性用于成本和 token 计算：

```python
with langfuse.start_as_current_observation(
    name="generate-sql",
    as_type="generation",
    model="gpt-4o",                     # 模型名 → 匹配 Models 页的单价
    model_parameters={                   # 模型参数
        "temperature": 0,
        "max_tokens": 4000,
        "top_p": 1.0,
    },
    input={                              # prompt / 消息列表
        "messages": [
            {"role": "system", "content": "你是 SQL 专家..."},
            {"role": "user", "content": question},
        ],
    },
) as gen:
    response = call_llm(question)
    gen.update(
        output={"content": sql},         # completion
        usage={                          # 手动指定 token（可选，自动模式更好）
            "input": 500,
            "output": 200,
            "total": 700,
        },
    )
```

**成本自动计算**：如果模型已在 **Settings → Models** 页配置了单价，Langfuse 会根据 token 用量自动计算成本。

## 3. 嵌套规则与 OTel Context

### 3.1 自动嵌套（同线程）

在同一线程内，`with` 块自动嵌套：

```python
# 父 observation
with langfuse.start_as_current_observation(name="parent", as_type="agent"):
    # 子 observation 自动成为 parent 的子节点
    with langfuse.start_as_current_observation(name="child", as_type="span"):
        # 孙 observation
        with langfuse.start_as_current_observation(name="grandchild", as_type="tool"):
            pass
```

原理：SDK 基于 Python `contextvars` 管理当前 observation 的上下文，`with` 块的嵌套自动维护父子关系。

### 3.2 手动嵌套（跨线程/异步）

跨线程时 `contextvars` 不会自动传递，需要手动指定 `trace_context`：

```python
import threading

# 主线程创建 trace
with langfuse.start_as_current_observation(name="main", as_type="agent") as root:
    trace_id = root.trace_id  # 获取当前 trace ID
    parent_span_id = root.id  # 获取当前 span ID

    def worker():
        # 子线程手动指定 trace_context
        with langfuse.start_as_current_observation(
            name="worker-task",
            as_type="tool",
            trace_context={
                "trace_id": trace_id,
                "parent_span_id": parent_span_id,
            },
        ) as span:
            do_work()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
```

### 3.3 异步任务嵌套（asyncio）

```python
import asyncio

async def main():
    with langfuse.start_as_current_observation(name="main", as_type="agent") as root:
        trace_id = root.trace_id

        async def sub_task():
            with langfuse.start_as_current_observation(
                name="sub-task",
                as_type="tool",
                trace_context={"trace_id": trace_id},
            ):
                await asyncio.sleep(1)

        # asyncio.gather 内的协程自动继承 contextvars
        await asyncio.gather(sub_task(), sub_task())
```

> ⚠️ **注意**：`asyncio.gather` / `asyncio.create_task` 在 CPython 中会自动继承 `contextvars`，但 `threading.Thread` 不会。使用 `asyncio.to_thread()` 时也需要手动传递。

### 3.4 OTel NonRecordingSpan 注入

在复杂的 Agent 框架（如 DeepAgents/LangGraph）中，子 Agent 运行在独立的工作线程中，OTel context 可能丢失。解决方案是注入 `NonRecordingSpan`：

```python
from opentelemetry import trace as otel_trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

# 创建携带父 trace_id 的 NonRecordingSpan
span_context = SpanContext(
    trace_id=int(parent_trace_id.replace("-", ""), 16),
    span_id=int(parent_span_id.replace("-", ""), 16),
    is_remote=False,
    trace_flags=TraceFlags(TraceFlags.SAMPLED),
)
non_recording = NonRecordingSpan(span_context)

# 在子 Agent 执行前设置 context
token = otel_trace.set_current_span(non_recording)
try:
    # 子 Agent 的 CallbackHandler 会在这个 context 下创建 observation
    result = await sub_agent.ainvoke(input, config)
finally:
    otel_trace.get_current_span().end()
```

这是本项目中 `deepagents_async_config_patch.py` 的核心机制（详见 [08-NL2SQL 项目实践](08-NL2SQL项目实践.md)）。

## 4. 创建 Trace 的完整示例

### 4.1 多层嵌套 Agent

```python
from langfuse import Langfuse

langfuse = Langfuse()

def multi_agent_pipeline(user_question: str):
    """模拟一个多层 Agent 的完整执行"""

    # ━━━ 最外层 = trace 根 ━━━
    with langfuse.start_as_current_observation(
        name="orchestrator",
        as_type="agent",
        input=user_question,
        session_id="session-001",
        user_id="user-42",
        metadata={
            "db_name": "chinook",
            "query_type": "aggregation",
            "release": "v1.2.0",
        },
    ):
        # ━━━ 子 Agent 1：意图识别 ━━━
        with langfuse.start_as_current_observation(
            name="intent-classifier",
            as_type="agent",
            input={"question": user_question},
        ) as intent_agent:
            with langfuse.start_as_current_observation(
                name="classify",
                as_type="generation",
                model="gpt-4o-mini",
                input={"prompt": f"Classify intent: {user_question}"},
            ) as gen:
                intent = "data_query"
                gen.update(output={"intent": intent, "confidence": 0.95})
            intent_agent.update(output={"intent": intent})

        # ━━━ 子 Agent 2：SQL 生成 ━━━
        with langfuse.start_as_current_observation(
            name="sql-generator",
            as_type="agent",
            input={"question": user_question, "intent": intent},
        ) as sql_agent:
            # Schema 检索
            with langfuse.start_as_current_observation(
                name="schema-retrieval",
                as_type="retriever",
                input={"query": user_question},
            ) as ret:
                schema = {"tables": ["artist"], "columns": ["name", "artistid"]}
                ret.update(output={"documents": [str(schema)]})

            # SQL 生成
            with langfuse.start_as_current_observation(
                name="sql-llm",
                as_type="generation",
                model="gpt-4o",
                model_parameters={"temperature": 0},
                input={"schema": schema, "question": user_question},
            ) as gen:
                sql = "SELECT COUNT(*) FROM artist"
                gen.update(output=sql)
            sql_agent.update(output=sql)

        # ━━━ 工具调用：执行 SQL ━━━
        with langfuse.start_as_current_observation(
            name="execute-sql",
            as_type="tool",
            input={"sql": sql},
        ) as tool:
            result = {"rows": [{"count": 275}], "error": None}
            tool.update(output=result, metadata={"execution_time_ms": 45})

        # ━━━ 打分 ━━━
        langfuse.score_current_trace(name="end-to-end-quality", value=0.9)
        langfuse.set_current_trace_io(input=user_question, output=f"共 275 位艺术家")

    langfuse.flush()
```

在 UI 中看到的 trace 树：

```
orchestrator (8.2s, $0.05)
├── intent-classifier (1.2s)
│   └── classify (generation, 0.8s, 150 tokens, gpt-4o-mini)
├── sql-generator (4.5s)
│   ├── schema-retrieval (retriever, 0.3s)
│   └── sql-llm (generation, 3.8s, 800 tokens, gpt-4o)
└── execute-sql (tool, 0.045s)
Scores: end-to-end-quality = 0.9
```

## 5. 命名规范与最佳实践

### 5.1 命名原则（来自 Langfuse 官方 Best Practices）

> "Keep dynamic values out of names. A name should identify the operation, not a single execution of it."

| 属性 | ✅ 放什么 | ❌ 不放什么 |
|------|----------|-----------|
| `name` | 操作标识（低基数） | 用户问题、ID、时间戳 |
| `input` | 用户问题原文 | 原始 JSON blob |
| `output` | 最终回答 | 全部中间结果 |
| `metadata` | db_name、user_role | 敏感数据（PII） |
| `tags` | 功能标签 | 高基数动态值 |

### 5.2 命名建议

| 层级 | 建议 | 好例子 | 坏例子 |
|------|------|--------|--------|
| Trace | Agent 名 / 操作类型 | `"chat-turn"` | `"user-asked-about-sales-at-3pm"` |
| Generation | 描述 LLM 用途 | `"generate-sql"` | `"gpt4o-call"` |
| Tool | 工具名 | `"execute-sql"` | `"tool-1"` |
| Span | 步骤描述 | `"schema-linking"` | `"step-3"` |
| Chain | 流程描述 | `"sql-pipeline"` | `"chain"` |

### 5.3 Metadata 规范

```python
metadata = {
    # ✅ 结构化、低基数
    "db_name": "chinook",
    "user_role": "analyst",
    "query_type": "aggregation",
    "complexity": "medium",
    "prompt_version": "v12",
    "model": "gpt-4o",

    # ✅ 可筛选的分类信息
    "skill_tags": ["nl2sql", "sql-of-thought"],
    "tables_involved": ["artist", "album"],

    # ❌ 不要放敏感数据
    # "password": "...",
    # "api_key": "...",

    # ❌ 不要放超长文本
    # "full_conversation_history": "... 100KB ...",
}
```

### 5.4 大文本处理

```python
MAX_OUTPUT_CHARS = 8000

def safe_output(result):
    """截断超长输出，避免 trace 膨胀"""
    text = str(result)
    if len(text) > MAX_OUTPUT_CHARS:
        return {
            "truncated": True,
            "preview": text[:MAX_OUTPUT_CHARS],
            "total_chars": len(text),
            "note": f"完整结果已落盘: vfs://large_results/{trace_id}",
        }
    return result
```

## 6. flush() 的重要性

### 6.1 为什么必须 flush

SDK 采用**批量异步上报**机制：trace 数据先缓存在内存中，按 `flush_interval`（默认 1s）和 `batch_size`（默认 100）批量发送到服务端。

**如果不调用 `flush()`**：

- 程序正常退出 → 缓存中的数据**可能丢失**
- 程序异常退出 → 缓存中的数据**一定丢失**
- 短生命周期脚本 → 数据**大概率丢失**

### 6.2 正确的 flush 方式

```python
# ✅ 方式 1：显式 flush
langfuse.flush()

# ✅ 方式 2：atexit 注册
import atexit
atexit.register(langfuse.flush)

# ✅ 方式 3：try-finally 保证
try:
    with langfuse.start_as_current_observation(...):
        ...
finally:
    langfuse.flush()

# ✅ 方式 4：context manager + flush
from contextlib import contextmanager

@contextmanager
def traced_agent(name):
    with langfuse.start_as_current_observation(name=name, as_type="agent") as obs:
        yield obs
    langfuse.flush()
```

## 7. Trace ID 预生成与跨服务传递

### 7.1 预生成 Trace ID

```python
# 在请求入口预生成
trace_id = langfuse.create_trace_id()

# 传递给下游服务
headers = {"X-Trace-Id": trace_id}
response = requests.post(downstream_url, headers=headers, json=payload)

# 在下游服务中使用
with langfuse.start_as_current_observation(
    name="downstream-service",
    as_type="span",
    trace_context={"trace_id": trace_id},
):
    process_request()
```

### 7.2 HTTP 传播

```python
# 上游：注入 trace context 到 HTTP header
from opentelemetry.propagate import inject

headers = {}
inject(headers)
response = requests.post(url, headers=headers)

# 下游：从 HTTP header 提取 trace context
from opentelemetry.propagate import extract

ctx = extract(request.headers)
with langfuse.start_as_current_observation(
    name="downstream",
    as_type="span",
    trace_context=ctx,
):
    ...
```

## 8. 下一步

- [04-评估与打分](04-评估与打分.md)：学习如何为 trace 添加评分
- [08-NL2SQL 项目实践](08-NL2SQL项目实践.md)：查看本项目的 Trace 嵌套方案
- [09-LangChain 集成](09-LangChain集成.md)：LangChain/LangGraph 自动追踪
