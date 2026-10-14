# 03 · Trace 追踪详解

> 深入理解 Langfuse 的 Trace 数据模型与嵌套结构

---

## 1. Trace 数据模型

Langfuse 的追踪基于三层结构：

```
Trace（一次完整的 Agent 任务）
├── Observation 1（一个步骤：LLM 调用 / 工具调用 / 检索等）
│   ├── Sub-Observation 1.1
│   └── Sub-Observation 1.2
├── Observation 2
└── Observation 3
```

### 1.1 Trace（追踪）

Trace 是最顶层的容器，代表一次完整的 Agent 任务。

| 属性 | 说明 | 示例 |
|------|------|------|
| `name` | Trace 名称 | `"nl2sql-agent"` |
| `input` | 用户输入 | `"统计 artist 表有多少艺术家"` |
| `output` | 最终输出 | `{"answer": "共 275 位艺术家"}` |
| `metadata` | 附加信息 | `{"db": "chinook", "user": "alice"}` |
| `session_id` | 会话标识（多轮分组） | `"session-abc-123"` |
| `user_id` | 用户标识 | `"user-42"` |
| `tags` | 标签 | `["production", "v1.2.0"]` |
| `release` | 发版标记 | `"v1.2.0"` |
| `scores` | 评估打分 | `[{"name": "correctness", "value": 1.0}]` |

### 1.2 Observation（观察）

Trace 内的每一个步骤都是一个 Observation。通过 `as_type` 区分类型：

| `as_type` | 用途 | 典型场景 |
|-----------|------|----------|
| `agent` | Agent 根节点（= Trace） | 最外层包裹 |
| `generation` | LLM 调用 | GPT/DeepSeek 生成 SQL |
| `tool` | 工具调用 | 执行 SQL、调用 API |
| `span` | 通用步骤 | 数据转换、格式处理 |
| `chain` | 链式调用 | LangChain 的 chain |
| `retriever` | 检索 | 向量检索、全文检索 |
| `embedding` | 嵌入计算 | 文本向量化 |
| `evaluator` | 评估 | LLM-as-Judge 评估 |
| `guardrail` | 护栏 | 安全检查、过滤 |

### 1.3 Generation 的特殊属性

`as_type="generation"` 的 observation 有额外属性用于成本和 token 计算：

```python
with langfuse.start_as_current_observation(
    name="generate-sql",
    as_type="generation",
    model="gpt-4o",                          # 模型名（用于成本计算）
    model_parameters={                        # 模型参数
        "temperature": 0,
        "max_tokens": 2000,
    },
    input={"messages": [...]},               # prompt / 消息列表
    output={"content": "SELECT ..."},         # completion
):
    pass
```

## 2. 创建 Trace

### 2.1 基础用法

```python
from langfuse import Langfuse

langfuse = Langfuse()

# 最外层 as_type="agent" 自动成为 trace 根
with langfuse.start_as_current_observation(
    name="my-agent",
    as_type="agent",
    input="用户的问题",
    metadata={"source": "web"},
    session_id="session-001",     # 多轮会话分组
    user_id="user-42",            # 用户标识
) as trace:
    # 子步骤自动嵌套在这个 trace 下
    with langfuse.start_as_current_observation(
        name="step-1",
        as_type="span",
        input={"data": "..."},
    ) as span:
        result = process_data()
        span.update(output=result)
    
    # 设置 trace 级别的输出
    langfuse.set_current_trace_io(
        input="用户的问题",
        output="最终回答",
    )

langfuse.flush()
```

### 2.2 嵌套示例（多层 Agent）

```python
# 主 Agent
with langfuse.start_as_current_observation(
    name="main-agent", as_type="agent",
    input=user_question,
):
    # 子 Agent 1：Schema 发现
    with langfuse.start_as_current_observation(
        name="schema-linking", as_type="agent",
        input={"question": user_question},
    ):
        # LLM 调用
        with langfuse.start_as_current_observation(
            name="llm-schema", as_type="generation",
            model="gpt-4o",
            input={"prompt": "识别涉及的表和字段..."},
        ) as gen:
            schema = call_llm("识别涉及的表和字段...")
            gen.update(output=schema)

    # 子 Agent 2：SQL 生成
    with langfuse.start_as_current_observation(
        name="sql-generation", as_type="agent",
        input={"question": user_question, "schema": schema},
    ):
        with langfuse.start_as_current_observation(
            name="llm-sql", as_type="generation",
            model="gpt-4o",
            input={"prompt": f"基于 {schema} 生成 SQL..."},
        ) as gen:
            sql = call_llm(f"基于 {schema} 生成 SQL...")
            gen.update(output=sql)

    # 工具调用：执行 SQL
    with langfuse.start_as_current_observation(
        name="execute-sql", as_type="tool",
        input={"sql": sql},
    ) as tool:
        result = execute_sql(sql)
        tool.update(output={"rows": len(result), "data": result[:5]})

langfuse.flush()
```

在 Langfuse UI 中看到的结构：

```
main-agent (12.3s, $0.05)
├── schema-linking (3.1s)
│   └── llm-schema (generation, 2.8s, 800 tokens)
├── sql-generation (5.2s)
│   └── llm-sql (generation, 4.5s, 1200 tokens)
└── execute-sql (tool, 1.1s)
```

### 2.3 Trace ID 预生成

如果需要提前生成 trace ID（例如跨服务传递）：

```python
# 预生成 trace ID
tid = langfuse.create_trace_id()

# 在后续调用中使用
with langfuse.start_as_current_observation(
    name="downstream-service",
    as_type="span",
    trace_context={"trace_id": tid},
):
    pass
```

## 3. Session（会话分组）

通过 `session_id` 将多个 trace 归组到同一个会话中：

```python
session_id = f"session-{user_id}-{conversation_id}"

# 第一轮
with langfuse.start_as_current_observation(
    name="chat-turn-1", as_type="agent",
    session_id=session_id,
    input="第一个问题",
):
    ...

# 第二轮（同一 session）
with langfuse.start_as_current_observation(
    name="chat-turn-2", as_type="agent",
    session_id=session_id,
    input="第二个问题（追问）",
):
    ...
```

在 **Sessions** 页面可以看到同一会话的所有 trace。

## 4. Metadata 与 Tags

### 4.1 Metadata（键值对）

任意 JSON 可序列化的数据：

```python
with langfuse.start_as_current_observation(
    name="query-agent", as_type="agent",
    input=question,
    metadata={
        "db_name": "chinook",
        "user_role": "analyst",
        "query_type": "aggregation",
        "tables_involved": ["artist", "album"],
        "complexity": "medium",
    },
):
    ...
```

### 4.2 Tags（标签）

用于分类和筛选：

```python
with langfuse.start_as_current_observation(
    name="query-agent", as_type="agent",
    input=question,
    # tags 在最外层 trace 上设置
):
    # 通过 score 或 update 添加 tags
    pass
```

### 4.3 动态更新

```python
with langfuse.start_as_current_observation(
    name="my-span", as_type="span",
) as span:
    result = do_work()
    # 运行过程中动态添加 metadata
    span.update(metadata={"result_size": len(result), "cache_hit": True})
```

## 5. 最佳实践

### 5.1 命名规范

| 层级 | 建议 | 示例 |
|------|------|------|
| Trace | Agent 名称 | `"nl2sql-agent"`, `"report-builder"` |
| Generation | 描述 LLM 用途 | `"generate-sql"`, `"summarize-result"` |
| Tool | 工具名称 | `"execute-sql"`, `"search-knowledge"` |
| Span | 步骤描述 | `"data-preprocessing"`, `"format-output"` |

### 5.2 Input/Output 格式

- **Input**：尽量结构化（dict），方便后续筛选和分析
- **Output**：包含关键指标（如行数、错误信息）
- **大文本**：截断到合理长度（建议 < 8000 字符），避免存储压力

### 5.3 flush() 必调

```python
# ✅ 正确：with 块结束后立即 flush
with langfuse.start_as_current_observation(...):
    ...
langfuse.flush()

# ✅ 正确：程序退出前 flush
import atexit
atexit.register(langfuse.flush)

# ❌ 错误：忘记 flush → 数据丢失
with langfuse.start_as_current_observation(...):
    ...
# 程序退出，数据未上报！
```

## 6. 下一步

- [04-评估与打分](04-评估与打分.md)：学习如何为 trace 添加评分
- [08-NL2SQL 项目实践](08-NL2SQL项目实践.md)：查看本项目的 Trace 嵌套方案
