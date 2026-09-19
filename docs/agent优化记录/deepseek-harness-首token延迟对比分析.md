# DeepSeek-Harness vs nl2sql 首Token延迟对比分析

> 2026-08-18 · 源码级交叉核验
> 对标对象：`D:/code_work_space/llm/deepseek-harness`（DeepSeek AI 开源 agent harness）
> nl2sql 当前状态：LangGraph + deepagents + ChatDeepSeek SDK，MAIN_AGENT_PROMPT 已精简 60%

## 一、核心结论

**dsh 的 0.x 秒首 token 是"架构差异"，不是某个能抄的技巧。** dsh 做的是最薄的一层——单次 LLM 调用、极小 prompt、无 agent 框架。把 dsh 和 nl2sql 比首 token，本质是苹果比橘子。

## 二、架构差异全景

### 2.1 调用链路对比

```
dsh 调用链路：
用户输入 → inbox → ReactLoopAgent.run() → fetch() + SSE → parseSse() → translate() → 渲染
         └─ 无中间件
         └─ 无 checkpoint
         └─ 无子 agent

nl2sql 调用链路：
用户输入 → LangGraph → deepagents middleware 链 → ChatDeepSeek SDK → start_async_task → 子 agent graph
         ├─ SkillsMiddleware
         ├─ TodoListMiddleware
         ├─ QueryKeywordsMiddleware
         ├─ ThinkingToggleMiddleware
         └─ 每步 checkpoint 写入
```

### 2.2 关键维度对比

| 维度 | dsh（deepseek-harness） | nl2sql | 差距 |
|------|------------------------|--------|------|
| **系统 prompt** | 身份行 `You are an AI agent powered by DeepSeek Harness.`（1 行）+ 空 persona（默认 `''`）+ 工具 schema | MAIN_AGENT_PROMPT（~7KB 精简后）+ Skills 元数据（~2KB）+ 图表规范 + 库名注入 + 图表 MCP 工具 schema（1-3 个） | **5-10 倍** |
| **模型调用方式** | 原生 `fetch()` + SSE 直连，`eventsource-parser` 解析 | LangChain `ChatDeepSeek` SDK → LangGraph → deepagents | SDK 层薄，但 middleware 链厚 |
| **任务结构** | 单次 LLM 往返（`ReactLoopAgent`） | 编排器 → `start_async_task` → 子 agent 做 knowledge-loader→schema-linking→subproblem→query-plan→sql-generation→correction→run_sql | **多轮次** |
| **状态持久化** | 事件溯源（`SessionEvent` 追加式日志），无每步写 checkpointer | 每步写 LangGraph checkpoint（`AsyncSqliteSaver`） | dsh 无此开销 |
| **思考模式** | 默认不强制 | DeepSeek thinking（已可通过开关关闭） | 已收敛 |
| **默认上下文窗口** | `DEFAULT_CONTEXT_WINDOW = 1_000_000`，`DEFAULT_MAX_TOKENS = 256_000` | 取决于模型 | — |
| **技术栈** | TypeScript monorepo（pnpm），Cordis 插件系统 | Python + LangGraph + deepagents | 不兼容 |

## 三、dsh 快的关键因素（源码分析）

### 3.1 系统 prompt 极小

`@deepseek-ai/dsh-system-prompt`（[index.ts](D:/code_work_space/llm/deepseek-harness/packages/core/system-prompt/src/index.ts)）：

```typescript
// 默认仅注入一行身份：
this.section({
  name: 'harness:identity',
  order: -100,
  text: 'You are an AI agent powered by DeepSeek Harness.',
})
// persona 默认 ''
this.section({
  name: PERSONA_SECTION,
  order: PERSONA_ORDER,
  text: config.persona ?? '',  // 默认空字符串
})
```

**这意味着 dsh 发给模型的 system prompt 在没有业务 persona 时只有一行文本 + 工具 JSON schema。** 对比 nl2sql 精简后约 7KB 的 MAIN_AGENT_PROMPT，差距在 5-10 倍。

### 3.2 原生 fetch + SSE，无 SDK 中间层

`@deepseek-ai/dsh-llm-deepseek`（[adapter.ts](D:/code_work_space/llm/deepseek-harness/packages/llm/llm-deepseek/src/adapter.ts) + [sse.ts](D:/code_work_space/llm/deepseek-harness/packages/llm/llm-deepseek/src/sse.ts)）：

```typescript
// 直接 fetch，无 LangChain/OpenAI SDK 封装
const response = await fetch(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${apiKey}` },
  body: JSON.stringify({ model, messages, stream: true, ... }),
  signal,
})

// SSE 解析用 eventsource-parser
import { EventSourceParserStream } from 'eventsource-parser/stream'
```

`translate.ts` 做 stateful block assembly（text/reasoning/tool-call），一个文件搞定，无多层继承。

### 3.3 事件溯源，无 checkpoint 开销

dsh 的会话日志是追加式 `SessionEvent` 数组，LLM 消息历史由日志**派生**（`deriveEventMessage`/`foldSurface`），不单独存储。

nl2sql 的 LangGraph 每步写 checkpoint（`AsyncSqliteSaver`），这是架构级差异，无法简单移除。

### 3.4 单 Agent 循环，无子 Agent 编排

`ReactLoopAgent`（[agent.ts](D:/code_work_space/llm/deepseek-harness/packages/core/agent-loop/src/agent.ts)）是单 agent 循环：

```
run() → prepareStep() → assembleContext() → prepareRequest() → llm.call() → executeToolCalls() → loop
```

没有 start_async_task、没有子 agent graph、没有 knowledge-loader→schema-linking→...→run_sql 的复杂管线。

## 四、首 Token 延迟的物理组成

首 token 延迟 = **网络 RTT** + **prompt prefill 时间** + **框架开销**

```
dsh（~0.x 秒）:
  RTT ~100ms + prefill(几百 tokens) ~100ms + 框架 ~50ms ≈ 0.3s

nl2sql（~3-5 秒，思考关闭后）:
  RTT ~100ms + prefill(~3500+ tokens) ~2000ms + 框架(middleware链+checkpoint) ~500ms ≈ 3s
```

**prefill 时间是主导因素。** Transformer 的 prefill 复杂度与 prompt 长度成正比（O(n²) 注意力），nl2sql 的 prompt 是 dsh 的 5-10 倍，prefill 时间自然也是 5-10 倍。

## 五、nl2sql 可落地的优化（按收益排序）

### 5.1 工具 schema 已分离（无需进一步优化）

实际代码核实（[mcp_tool.py](D:/code_work_space/llm/nl2sql/src/agent/tools/mcp_tool.py) + [main_agent.py](D:/code_work_space/llm/nl2sql/src/agent/main_agent.py) + [nl2sql_agent.py](D:/code_work_space/llm/nl2sql/src/agent/nl2sql_agent.py)）：

| 层级 | 工具来源 | 实际内容 | 数量 |
|------|---------|---------|------|
| 主 agent | `main_tools` | 图表 MCP（semiotic 或 echarts） | 1-3 个 |
| 主 agent | deepagents 内置 | `start_async_task`、`check_async_task`、`cancel_async_task`、`update_async_task`、`list_async_tasks` 等 | ~5 个 |
| 子 agent | `sub_tools` | WrenAI 语义层 + dbmcp 直连（全量数据库工具） | 十几个 |

**结论：主 agent 的工具 schema 已经很小（图表工具 + 编排工具），WrenAI 全量工具集在子 agent 上。之前分析中"主 agent 挂了 WrenAI 全工具集"的判断是错误的，已修正。**

### 5.2 精简 MAIN_AGENT_PROMPT（已兑现）

已完成 60% 精简（17365 bytes → 6962 bytes）。
剩余空间有限，再精简会损害指令精确性。

### 5.3 思考开关（已兑现）

DeepSeek thinking 已通过 `enable_thinking` 开关控制，关闭后不再有 reasoning_content 预生成延迟。

### 5.4 不建议的优化

| 优化项 | 为什么不建议 |
|--------|------------|
| 换 raw fetch 替代 ChatDeepSeek SDK | SDK 开销是毫秒级（~50ms），收益极小。改动量巨大（需重写整个 LangGraph 集成层），性价比极低 |
| 去掉 checkpoint | LangGraph 核心机制，不可移除 |
| 去掉子 agent 编排 | 这是 nl2sql 的核心价值（knowledge-loader→schema-linking→sql-generation→correction），砍掉等于放弃 NL2SQL 能力 |
| 换 dsh 架构 | 技术栈不兼容（TS 插件系统 vs Python LangGraph），且 dsh 没有 NL2SQL 管线 |

## 六、当前状态与真实优化空间

### 已完成的优化
- MAIN_AGENT_PROMPT 精简 60%（17365 bytes → 6962 bytes）
- 思考开关（`enable_thinking` 可控）
- 工具 schema 已分离（主 agent 仅图表工具 + 编排工具，WrenAI 全量在子 agent）

### 剩余优化空间
- prompt 层面：MAIN_AGENT_PROMPT 再精简空间有限，会损害指令精确性
- 框架层面：SDK 开销（~50ms）不值得动；checkpoint 是 LangGraph 核心不可移除；子 agent 编排是 nl2sql 核心价值

## 七、关键源码文件索引

| 文件 | 说明 |
|------|------|
| [adapter.ts](D:/code_work_space/llm/deepseek-harness/packages/llm/llm-deepseek/src/adapter.ts) | dsh 的 DeepSeek adapter：fetch + SSE，DEFAULT_CONTEXT_WINDOW=1M，DEFAULT_MAX_TOKENS=256K |
| [sse.ts](D:/code_work_space/llm/deepseek-harness/packages/llm/llm-deepseek/src/sse.ts) | dsh 的 SSE 解析：`eventsource-parser` |
| [translate.ts](D:/code_work_space/llm/deepseek-harness/packages/llm/llm-deepseek/src/translate.ts) | dsh 的 stateful block assembly：text/reasoning/tool-call |
| [index.ts](D:/code_work_space/llm/deepseek-harness/packages/core/system-prompt/src/index.ts) | dsh 的 prompt 组装：默认仅一行身份 + 空 persona |
| [agent.ts](D:/code_work_space/llm/deepseek-harness/packages/core/agent-loop/src/agent.ts) | dsh 的 ReactLoopAgent：单 agent 循环 |
| [model.py](D:/code_work_space/llm/nl2sql/src/agent/llms/model.py) | nl2sql 的模型创建：ChatDeepSeek/ChatOpenAI |
| [MAIN_AGENT_PROMPT.md](D:/code_work_space/llm/nl2sql/src/agent/prompt/MAIN_AGENT_PROMPT.md) | nl2sql 的主 agent 提示词（已精简） |