# 输入框上下文圆环按钮 — 设计方案（暂不实施）

> 状态：**设计稿**。仅创建本文件存档，未改任何代码。待评审通过后再进入实现。
> 对标对象：Claude Code 输入框的圆环按钮（进度环 + 悬停显示上下文用量 + 点击触发上下文压缩）。

## 1. 目标

在 composer（输入框）增加一个**圆形按钮**：

1. **圆环进度**：显示当前上下文窗口占用百分比（视觉环形进度）。
2. **悬停显示用量**：hover 弹出上下文用量分解（system / tools / messages 三段，或按 token 维度）。
3. **点击触发压缩**：点击按钮触发一次「上下文压缩 / 摘要」。

## 2. 现状梳理（已核实，均为「零基础」起点）

### 2.1 后端：无 token 计量、无压缩 API

- **唯一相关机制**是 `MessageSlimmerMiddleware`（`src/agent/middlewares/message_slimmer.py`），只做被动瘦身：
  - 截断 >16K 字符的 ToolMessage，落盘到 `large_tool_results/<tool_call_id>`；
  - md5 去重完全重复的工具结果。
- **明确没有**：LLM 摘要、对话历史压缩、任何 `compact`/`summarize`/`ContextSeek` 触发式压缩（`message_slimmer.py` 注释明确「L2 暂缓」）。
- **无 token 估算**：全项目用 `len()` 字符数；`model.py:155` 的 `ModelProfile(max_input_tokens=120000)` 是硬编码常量，未参与任何计量。
- **无 usage/context/token API**：现有自定义端点只有 `db_config` / `message_feedback` / `auto_title` / `model_config` / `sql_approval` / `thread_fork` / `thread_search`。

### 2.2 前端：无用量展示、composer 结构已定位

- `ChatInterface.tsx` 的 `<form onSubmit={handleSubmit}>` 行 1029-1113；底部工具条 `div.flex.justify-between` 行 1047-1112：
  - 左组（1048-1074）：`Plus` 附件按钮 + `DatabaseSelector`。
  - 右组（1075-1111）：`ModelSelector` + 发送/停止 `Button`。
- **圆环按钮落点**：右组内、`ModelSelector` 与发送按钮之间（行 1088/1089 处），对标 Claude Code 的「输入框内圆环」。
- `useChat.ts` 用 `pollClient.threads.getState(threadId)` 轮询，只读 `messages/todos/query_headers` 等，不读任何 token/usage 字段。

## 3. 设计

### 3.1 数据链路：先做 token 计量（前置依赖）

进度环的 `%` 与用量分解**后端当前完全不提供**，需先落地「P2-1 token 计量」。

**方案：中间件层统计 → 写回 state → API 暴露**。

1. **token 计量中间件**（新 `src/agent/middlewares/token_usage.py`）：在 `wrap_model_call` / `wrap_tool_call` 边界统计三类 token：
   - **system**：system prompt + 各 middleware 注入的 prompt 部分；
   - **messages**：对话消息（user/assistant/tool 之外的历史文本）；
   - **tools**：工具调用 + 工具结果（含落盘的大型结果）。
   - 估算方法：优先用模型返回的 `usage`（`response_metadata` 里的 `token_usage`）；无则用 `len(text) / 4` 字符估算（中文约 /1.5，需校准）。
2. **写回 state**：新增 `MainAgentState.token_usage`（dict，按 `task_id`/维度 reducer 合并，参照 `subagent_steps_map` 合并模式 `main_agent.py:137-139`）。
3. **模型窗口**：`max_input_tokens` 从 `ModelProfile` 或 `model_config_store` 读真实值（不再硬编码 120000；不同 provider 窗口不同）。

### 3.2 用量 API

新 `src/api/context_usage.py`（暴露 `routes`，`custom_app.py` 加一行）：

```
GET /api/threads/{thread_id}/context-usage
  → { total_tokens, max_tokens, ratio, breakdown: { system, messages, tools } }
```

- 数据源：读该 thread state 的 `token_usage`（若已写入）+ 实时计算最近消息；无计量数据时回退 `len(text)/4` 粗估。

### 3.3 压缩 API（点击触发）

新 `src/api/context_compaction.py`（或并入上模块）：

```
POST /api/threads/{thread_id}/compact
  → 触发一次上下文压缩
```

- **方案 A（推荐，复用既有范式）**：参照 `sql_approval.py` / `thread_fork.py` 的 `update_state` 读改写范式——用 LLM 把早期消息摘要成一段，`update_state` 替换掉旧消息（保留最近 N 轮 + 摘要前缀）。副作用小、可回滚。
- **方案 B（主动 interrupt）**：向运行中 run 注入 `interrupt` 让主 agent 自己压缩，复杂度高、本期不做。
- **约束**：压缩是破坏性改写历史，需 CAS 保护（`if_version`）；压缩后前端触发一次 `GET /state` 刷新。

### 3.4 前端组件

新 `src/app/components/ContextUsageRing.tsx`：

```tsx
interface ContextUsageRingProps {
  threadId: string;
  usage: ContextUsage | null;   // 来自 useChat 轮询
  onCompact: () => void;
}
```

- **圆环进度**：SVG `<circle>` stroke-dasharray 按 `ratio` 画弧；低占用绿、>70% 橙、>90% 红。
- **悬停用量**：`title` / 自定义 tooltip 显示 `总 token / 窗口 + system/messages/tools 三段`。
- **点击压缩**：调用 `POST /compact`，请求中禁用 + 完成后刷新；loading 态圆环转圈。
- **数据刷新**：复用现有 C 方案 `pollClient.threads.getState`，把 `context-usage` 结果并入轮询返回或独立 `GET /context-usage` 按需拉。

### 3.5 落点与按钮形态

- 位置：右组 `div.flex.justify-end.gap-2` 内、`ModelSelector` 与发送按钮之间。
- 形态：直径约 28px 的圆形 `Button`（`variant="ghost"` + `size="icon"`），内嵌 SVG 圆环；对标 Claude Code 的纯圆形无文字。

## 4. 文件改动清单

| 动作 | 文件 |
|------|------|
| 新增（后端） | `src/agent/middlewares/token_usage.py`（token 计量） |
| 修改（后端） | `src/agent/main_agent.py`（state 加 `token_usage` + 挂中间件） |
| 新增（后端） | `src/api/context_usage.py`、`src/api/context_compaction.py`（或合并） |
| 修改（后端） | `src/api/custom_app.py`（加 routes 展开） |
| 新增（前端） | `src/app/components/ContextUsageRing.tsx` |
| 修改（前端） | `src/app/components/ChatInterface.tsx`（composer 右组插入圆环） |
| 修改（前端） | `src/app/hooks/useChat.ts`（轮询 context-usage） |

## 5. 风险与边界

1. **token 估算是近似值**：无 `usage` 返回时用 `len/4` 估算，中文会偏差（中文 token 密度更高）。可用「按 provider 校准系数」缓解，但仍是近似。文档 `deepseek-harness对标清单` 已把「P2-1 token 计量」列为压缩环的前置，本项目**目前零基础**，需先落地计量。
2. **压缩破坏性**：改写 checkpoint 历史不可逆，需 CAS + 明确「压缩后不可撤销」提示；首次只做「保留最近 N 轮 + 摘要前缀」。
3. **types.ts DLP 加密**：新增类型字段有编译风险，尽量复用现有 `QueryTask`/`ContextUsage` 结构，避免改 `src/app/types/types.ts`。
4. **后端改动需重启 2026**（新增中间件/API）。
5. **ModelProfile 硬编码 120000**：若要精确 `max_tokens`，需从 `model_config_store` 读真实窗口。

## 6. 验证

1. token 计量：发问后 `GET /context-usage` 返回非零 `total_tokens` 与三段分解。
2. 圆环：前端 composer 出现圆环，占用 % 随对话增长而上升。
3. 悬停：显示 `system/messages/tools` 分解。
4. 压缩：点击后 `POST /compact` 返回成功，`GET /state` 消息变短（早期消息被摘要替代）。

## 7. 参考

- `docs/agent优化记录/deepseek-harness对标清单与实现方案.md`（P2-4 压缩 + spill、P2-7 占用环、P2-1 token 计量）
- memory `[[main-agent-token-streaming-ready]]`、`[[langgraph-custom-app-hook]]`
