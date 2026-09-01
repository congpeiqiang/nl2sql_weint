# 主智能体 Token 级流式输出方案

> 2026-08-14 · 状态：已实施并验证

## 背景与需求

用户希望主 agent（`chat_agent`）改为流式输出，让前端交互更友好。曾误以为把 `invoke` 改为 `stream` 即可——实际 `invoke_with_thread_id` 是死代码，与流式无关；真正的开关是前端 streamMode，且已被 getter 追踪**隐式打开**。

**核心结论**：主 agent 的 token 流式链路已 100% 打通，**后端与 SDK 零改动**。本次工作 = 验证生效 + UX 增强（流式光标）+ 死代码清理。

## 链路各环节（逐环节证据）

1. **前端隐式请求 messages 流**：`useChat` 返回 `messages: stream.messages`（useChat.ts:971），每次渲染访问 getter → SDK `trackStreamMode("messages-tuple","values")`（`langgraph-sdk/dist/react/stream.lgp.js:427-430`）→ submit 时 `streamMode = unique([...trackStreamModeRef.current])` 自动携带（stream.lgp.js:257-261）。无需显式传 streamMode。
2. **后端归一化 + SSE 下发**：`POST /threads/{id}/runs/stream` 原样透传请求体 stream_mode；本地图把 `messages-tuple` 归一化为 `messages`（`langgraph_api/stream.py:238-240`）→ `graph.astream(["messages","values","debug"])`；客户端请求了 messages-tuple 时，SSE **事件名仍发 `event: messages`**、data 为 `[chunk, metadata]`（stream.py:536-547）——SDK 的 `matchEventType("messages")` 命中。
3. **模型可流式**：`stream_mode=["messages"]` 时 langgraph 挂 `StreamMessagesHandler`，`ainvoke` 经 `_should_stream`（chat_models.py:571-573）自动降级为逐 token `_astream`；`ChatDeepSeek` 支持 `_astream`。
4. **前端逐 token 累积渲染**：SDK `StreamManager` 对每个 `messages` 事件 `messages.add()` 累积 chunk + `setStreamValues()` 更新 `values.messages` + `notifyListeners` re-render（`dist/ui/manager.js:103-122`）；`stream.messages` 每 chunk 是新数组引用 → `processedMessages` useMemo 重算（ChatInterface.tsx:364-515）→ `isStreaming={isLastMessage && isLoading}`（ChatInterface.tsx:587）→ `StreamingMarkdownContent` 增量渲染（MarkdownContent.tsx:509-515）。

## 实施内容

### B1 流式光标（前端）

[MarkdownContent.tsx](D:/code_work_space/llm/huice/008/harness-deep-agents-ui/src/app/components/MarkdownContent.tsx) 的 `StreamingMarkdownContent` 末尾追加闪烁光标 `▍`：

- 该组件只在 `streaming=true` 时渲染，光标可无条件显示。
- 用 `React.cloneElement` 把光标内联追加到**最后一个 `<p>`** 的 children（行尾光标，非独立换行）；内容以代码块/表格/空行收尾时回退为尾部独立光标块。
- 类型注意：需用 `React.isValidElement<{ children?: React.ReactNode }>(r)` 收窄，否则 `r.props` 是 `unknown`、cloneElement 报 TS2769。
- 光标用 Tailwind `animate-pulse`（opacity 闪烁）。

### C 死代码清理（后端）

- [main_agent.py](D:/code_work_space/llm/nl2sql/src/agent/main_agent.py)：删 `invoke_with_thread_id`（170-182）+ 注释调用 + 仅死代码用到的 `import asyncio` / `Dict, Any` / `HumanMessage`（-23 行）。
- [nl2sql_agent.py](D:/code_work_space/llm/nl2sql/src/agent/nl2sql_agent.py)：删 `Context` dataclass、`invoke_with_thread_id`、`context_schema=Context` 参数、`from dataclasses import dataclass` / `from typing import Any, Dict`（-26 行）。
- 安全性核实：`context_schema` 在 `create_deep_agent` 默认 `None`（deepagents graph.py:274）；thread_id 已由 `SkillDataMiddleware.wrap_tool_call` 从 `request.state` 自动读取并 `set_thread_id`（skill_data.py:53-57），不依赖 Context。

### B2 流式滚动跟随（无改动）

已集成 `useStickToBottom`（ChatInterface.tsx:236），其内置 ResizeObserver 监测 contentRef 内容增长，用户近底部时自动跟随。现有手动 effect（ChatInterface.tsx:539-551）deps 不随 chunk 变化，但流式跟随由库处理，**无需改码**。

## 验证结果

**A1 SSE 直连实测（通过）**：Python urllib 直连 `POST /threads/{id}/runs/stream`，body `{"assistant_id":"chat_agent","stream_mode":["messages-tuple","values"],"input":{"messages":[{"type":"human","content":"你好"}]}}`，观察到 80+ 个 `event: messages`，`AIMessageChunk` content **逐 token 增量到达**（如 `你好！👋 我是**智能数据助手**…`，1.4 秒内）。这是流式生效的硬证据。脚本见 `d:\tmp\verify_sse_stream.py`（测试线程已删）。

- 前端 `tsc --noEmit`：MarkdownContent.tsx 改动 **0 报错**（其余存量 `@ts-expect-error`/`artifact` 错误非本次引入）。
- 后端 `py_compile` 通过；`grep -rn "invoke_with_thread_id" src/agent` 无运行模块残留（仅 `workspace/tmp/main_agent_copy.py` 备份）。
- 浏览器视觉确认（光标逐字打出）由用户实测。

## 已知局限

- **nl2sql 子 agent 的 token 不流式**：子 agent 是独立 HTTP run（`client.runs.create`），其 token 不会出现在主 run 的 messages 流里——主 run 只流编排器/最终报告的 token。查询执行阶段前端只能看到进度条（todos），最终报告阶段才逐字流式。若需子 agent 也逐字流式，要后端 v2 事件流/子流转发，另行设计。
- 后端死代码清理需重启 2026 生效（纯死代码，重启前行为不变）。

## 关键文件

| 动作 | 文件 |
|------|------|
| 修改（UX） | `D:\code_work_space\llm\huice\008\harness-deep-agents-ui\src\app\components\MarkdownContent.tsx`（流式光标） |
| 修改（死代码） | `D:\code_work_space\llm\nl2sql\src\agent\main_agent.py`、`src\agent\nl2sql_agent.py` |
| 只读参考 | `.venv/Lib/site-packages/langgraph_api/stream.py`、SDK `dist/react/stream.lgp.js` / `dist/ui/manager.js` / `dist/ui/messages.js` |
| 验证脚本 | `d:\tmp\verify_sse_stream.py`（SSE 直连，urllib line-by-line 增量读） |
