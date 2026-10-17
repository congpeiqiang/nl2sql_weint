# 09 · LangChain/LangGraph 集成

> Langfuse 与 LangChain/LangGraph 的深度集成方案  
> 涵盖：CallbackHandler 自动追踪、Agent Graph 可视化、子 Agent 归并、DeepAgents 集成  
> 参考：[LangChain Integration](https://langfuse.com/integrations/frameworks/langchain)、[LangGraph Integration](https://langfuse.com/integrations/frameworks/langgraph)

---

## 1. CallbackHandler 自动追踪

### 1.1 原理

Langfuse 的 `langfuse.langchain.CallbackHandler` 实现了 LangChain 的 `BaseCallbackHandler` 接口，自动捕获以下事件：

| 事件 | 生成的 Observation 类型 | 记录内容 |
|------|------------------------|----------|
| `on_llm_start` / `on_chat_model_start` | `generation` | 模型名、input messages、参数 |
| `on_llm_end` | `generation` (update) | output、token usage、耗时 |
| `on_tool_start` | `tool` | 工具名、参数 |
| `on_tool_end` | `tool` (update) | 结果、错误 |
| `on_chain_start` | `chain` / `agent` | 链名、输入 |
| `on_chain_end` | `chain` (update) | 输出 |
| `on_retriever_start` | `retriever` | 查询 |
| `on_retriever_end` | `retriever` (update) | 文档列表 |

### 1.2 安装

```bash
pip install langfuse langchain langchain-openai
```

### 1.3 基础用法

```python
from langfuse.langchain import CallbackHandler
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

# 创建回调处理器（自动读取 LANGFUSE_* 环境变量）
handler = CallbackHandler()

# 注入到 LLM
llm = ChatOpenAI(model="gpt-4o", callbacks=[handler])

# 正常调用 — trace 自动上报
response = llm.invoke([HumanMessage(content="你好")])

# ⚠️ 必须 flush
handler.flush()
```

### 1.4 Chain / Pipeline 追踪

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

handler = CallbackHandler()

# 构建 chain
prompt = ChatPromptTemplate.from_template("翻译为英文: {text}")
llm = ChatOpenAI(model="gpt-4o", callbacks=[handler])
parser = StrOutputParser()

chain = prompt | llm | parser

# 调用 — 整个 chain 自动追踪
result = chain.invoke({"text": "你好世界"})
handler.flush()
```

在 Langfuse UI 中看到的 trace：

```
RunnableSequence (chain)
├── ChatPromptTemplate (chain)
├── ChatOpenAI (generation, model=gpt-4o)
└── StrOutputParser (chain)
```

## 2. LangGraph Agent 集成

### 2.1 基础 Agent

```python
from langfuse.langchain import CallbackHandler
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

handler = CallbackHandler()
llm = ChatOpenAI(model="gpt-4o")

# 创建 ReAct Agent
tools = [search_tool, calculator_tool]
agent = create_react_agent(llm, tools)

# 通过 config 注入回调
config = {
    "callbacks": [handler],
    "recursion_limit": 50,
}

# 运行 — 完整 Agent 执行过程自动追踪
result = agent.invoke(
    {"messages": [("user", "北京今天天气怎么样？")]},
    config=config,
)

handler.flush()
```

### 2.2 自定义 Graph

```python
from langgraph.graph import StateGraph, MessagesState, START, END

handler = CallbackHandler()

# 定义节点
def call_model(state: MessagesState):
    llm = ChatOpenAI(model="gpt-4o")
    response = llm.invoke(state["messages"])
    return {"messages": [response]}

def call_tool(state: MessagesState):
    # 工具调用逻辑
    ...

# 构建 Graph
graph = StateGraph(MessagesState)
graph.add_node("model", call_model)
graph.add_node("tools", call_tool)
graph.add_edge(START, "model")
graph.add_conditional_edges("model", should_continue, {"tools": "tools", "end": END})
graph.add_edge("tools", "model")

app = graph.compile()

# 运行 — 注入回调
result = app.invoke(
    {"messages": [("user", "查询数据库")]},
    config={"callbacks": [handler]},
)

handler.flush()
```

## 3. 子 Agent 归并（主-子 Trace 合并）

### 3.1 问题

主 Agent 和子 Agent 是两个独立的 Graph run，默认会产生两条独立的 trace。但我们希望**一次用户查询 = 一条 trace**。

### 3.2 解决方案：trace_context 注入

```python
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from opentelemetry import trace as otel_trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

langfuse = Langfuse()

async def run_with_parent_trace(sub_agent, input_data, parent_trace_id, parent_obs_id):
    """子 Agent 归入父 trace"""
    
    # 1. 创建携带父 trace_id 的 OTel NonRecordingSpan
    span_context = SpanContext(
        trace_id=int(parent_trace_id.replace("-", ""), 16),
        span_id=int(parent_obs_id.replace("-", ""), 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    non_recording = NonRecordingSpan(span_context)
    
    # 2. 在当前 context 中设置父 span
    token = otel_trace.set_current_span(non_recording)
    
    try:
        # 3. 子 Agent 的 CallbackHandler 在这个 context 下创建 observation
        #    → 自动成为父 trace 的子节点
        handler = CallbackHandler()
        config = {"callbacks": [handler]}
        result = await sub_agent.ainvoke(input_data, config=config)
        handler.flush()
        return result
    finally:
        # 4. 清理 context
        otel_trace.get_current_span().end()
```

### 3.3 DeepAgents 异步子任务集成

本项目中 `deepagents_async_config_patch.py` 的实现：

```python
# 在 run 创建时注入父 trace 信息
def patch_subagent_config(config, parent_trace_id, parent_obs_id):
    """将父 trace 信息注入子 Agent 的 configurable"""
    config["configurable"] = {
        **config.get("configurable", {}),
        "langfuse_parent_trace_id": parent_trace_id,
        "langfuse_parent_obs_id": parent_obs_id,
    }
    return config

# 子 Agent 启动时读取并注入 OTel context
def setup_otel_from_config(config):
    """从 configurable 读取父 trace 信息并设置 OTel context"""
    parent_trace_id = config.get("configurable", {}).get("langfuse_parent_trace_id")
    parent_obs_id = config.get("configurable", {}).get("langfuse_parent_obs_id")
    
    if parent_trace_id and parent_obs_id:
        span_context = SpanContext(
            trace_id=int(parent_trace_id.replace("-", ""), 16),
            span_id=int(parent_obs_id.replace("-", ""), 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        otel_trace.set_current_span(NonRecordingSpan(span_context))
```

### 3.4 Auto-continue 路由

当一个问题触发异步子任务，子任务完成后需要续跑主 Agent。续跑链必须归属到**原始查询的 trace**，而不是最新的 trace：

```python
# 按 task_id 路由到正确的 trace
def route_autocontinue(task_id):
    """续跑链按 task_id 归属到原始 trace"""
    # 从持久化 store 查找 task_id 对应的 trace_id
    trace_id = trace_bind_store.get_trace_for_task(task_id)
    if trace_id:
        return trace_id  # 归入原始 trace
    return None  # 新建 trace
```

## 4. Agent Graph 可视化

Langfuse v4 的 Agent Graph 功能自动将 trace 聚合成 DAG 视图：

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  chat_agent  │───▶│ nl2sql_agent │───▶│ execute-sql  │
│  (planning)  │    │  (SQL gen)   │    │  (tool)      │
└──────────────┘    └──────────────┘    └──────────────┘
       │                                      │
       ▼                                      ▼
┌──────────────┐                    ┌──────────────┐
│  summarize   │◀───────────────────│  result      │
│  (LLM)       │                    │              │
└──────────────┘                    └──────────────┘
```

Agent Graph 从 CallbackHandler 的 observation 树自动推导节点关系，无需额外配置。

## 5. 自定义 Span 中间件

### 5.1 技能级 Span

本项目通过 `LangfuseSpanMiddleware` 在 CallbackHandler 自动生成的 observation 上叠加技能级 span：

```python
class LangfuseSpanMiddleware:
    """将工具调用按技能分类，创建技能级 span"""
    
    SKILL_MAP = {
        "wrenai_get_schema": "schema-linking",
        "wrenai_generate_sql": "sql-generation",
        "dbmcp_execute_sql": "sql-execution",
        "get_instructions": "knowledge-retrieval",
        "create_chart": "chart-generation",
    }
    
    async def on_tool_start(self, tool_name, input_data, config):
        skill = self.SKILL_MAP.get(tool_name, "unknown")
        
        # 创建技能级 span
        with langfuse.start_as_current_observation(
            name=f"skill:{skill}:{tool_name}",
            as_type="span",
            input=input_data,
            metadata={"skill": skill},
        ) as span:
            result = await original_tool_call(tool_name, input_data)
            span.update(output=result)
            return result
```

在 UI 中看到的层级：

```
chat_agent
├── nl2sql_agent
│   ├── skill:schema-linking:wrenai_get_schema    ← 技能级 span
│   │   └── wrenai_get_schema (tool, CallbackHandler 自动生成)
│   ├── skill:sql-generation:wrenai_generate_sql
│   │   └── wrenai_generate_sql (tool)
│   └── skill:sql-execution:dbmcp_execute_sql
│       └── dbmcp_execute_sql (tool)
```

## 6. 中文序列化修复

Langfuse SDK 默认的 `EventSerializer` 使用 `ensure_ascii=True`，导致中文被转义为 `\uXXXX`。本项目通过 monkey-patch 修复：

```python
import json
from langfuse.serializer import EventSerializer

class ChineseFriendlySerializer(EventSerializer):
    def dumps(self, obj):
        return json.dumps(obj, ensure_ascii=False, default=self.default)

# 替换默认序列化器
import langfuse
langfuse.serializer.EventSerializer = ChineseFriendlySerializer
```

## 7. CancelledError 处理

```python
from langfuse.langchain import CallbackHandler

# 将 CancelledError 加入控制流异常（不标记为错误）
handler = CallbackHandler()
handler.CONTROL_FLOW_EXCEPTION_TYPES = (
    *handler.CONTROL_FLOW_EXCEPTION_TYPES,
    asyncio.CancelledError,
)
```

## 8. 最佳实践

### 8.1 回调处理器生命周期

```python
# ✅ 正确：每个请求创建新的 handler
async def handle_request(request):
    handler = CallbackHandler()
    config = {"callbacks": [handler]}
    result = await agent.ainvoke(input, config=config)
    handler.flush()  # 请求结束前 flush
    return result

# ❌ 错误：全局共享 handler（可能导致 trace 混乱）
global_handler = CallbackHandler()
```

### 8.2 错误处理

```python
try:
    handler = CallbackHandler()
    result = await agent.ainvoke(input, config={"callbacks": [handler]})
except Exception as e:
    # 错误也会上报到 trace
    logger.error(f"Agent error: {e}")
finally:
    handler.flush()  # 即使出错也要 flush
```

## 9. 下一步

- [08-NL2SQL 项目实践](08-NL2SQL项目实践.md)：本项目的完整集成方案
- [10-自托管部署](10-自托管部署与运维.md)：部署你自己的 Langfuse 实例
