# MessageSlimmerMiddleware 中间件分析

## 概述

`MessageSlimmerMiddleware` 是一个 LangGraph Agent 中间件，在工具调用结果（ToolMessage）进入 state/checkpoint 之前，对其进行主动瘦身，解决 LangGraph checkpoint 累积膨胀问题。

**核心问题**：LangGraph 每个 checkpoint 都存储"截至当前时刻的完整消息列表"，导致 O(n²) 式的存储膨胀。膨胀主因是超大 tool 结果（实测单条 73.4KB 的 `read_file` 占消息总大小 24%）与完全重复的 tool 结果（如 2×14KB 的 `write_todos` + 2×8.8KB 的 SKILL 读取）。

**方案定位**：方案 B 第一阶段（L1，低风险高收益）。

---

## 处理流程

```
wrap_tool_call / awrap_tool_call
  │
  ├─ 1. 执行原始 handler（工具实际调用）
  │
  ├─ 2. 从 state 中取出历史消息列表（用于去重比对）
  │
  ├─ 3. 逐个处理返回的 ToolMessage：
  │     │
  │     ├─ 3a. 去重检查（_dedup）
  │     │     提取消息文本 → 计算 MD5 → 与历史 ToolMessage 逐一比对
  │     │     ├─ 命中：返回占位消息（保留 tool_call_id/name/id）
  │     │     └─ 未命中：进入下一步
  │     │
  │     └─ 3b. 截断检查（_should_truncate）
  │           文本长度 > 阈值（默认 16000 字符）
  │           ├─ 超过且有 backend：落盘到 large_tool_results/，消息内保留 head+tail 预览
  │           └─ 未超过或无 backend：原样返回
  │
  └─ 4. 返回处理后的 ToolMessage 或 Command
```

---

## 两大核心动作

### 动作一：截断超大工具结果

| 项目 | 说明 |
|------|------|
| **触发条件** | 文本字符数 > `max_chars_before_truncate`（默认 16000，约 4k token） |
| **处理方式** | 完整内容落盘到 `large_tool_results/<tool_call_id>`，消息内只保留 head+tail 预览 + 路径指针 |
| **底层实现** | 复用 deepagents 的 `_offload_tool_message_content` / `_aoffload_tool_message_content` |
| **覆盖范围** | 包括 `read_file`、`execute` 等 deepagents 主动驱逐豁免的工具——这些正是本系统的膨胀源 |
| **阈值设计** | 16000 字符足以覆盖 73KB 的 read_file、超长 execute 输出；8.8KB SKILL.md 读取与 14KB write_todos 低于阈值，不被截断（重复场景交给去重处理） |

### 卸载文件在上下文中的体现

截断后，原始 ToolMessage 的 content 会被替换为以下格式的提示文本（定义在 deepagents 的 `TOO_LARGE_TOOL_MSG` 模板）：

```
Tool result too large, the result of this tool call {tool_call_id} was saved
in the filesystem at this path: {file_path}

You can read the result from the filesystem by using the read_file tool, but
make sure to only read part of the result at a time.

You can do this by specifying an offset and limit in the read_file tool call.
For example, to read the first 100 lines, you can use the read_file tool with
offset=0 and limit=100.

Here is a preview showing the head and tail of the result (lines of the form
`... [N lines truncated] ...` indicate omitted lines in the middle of the content):

{content_sample}
```

其中：

| 组成部分 | 说明 |
|----------|------|
| **文件路径** | `large_tool_results/<sanitized_tool_call_id>`，agent 可通过 `read_file` 工具按需读取 |
| **head+tail 预览** | 前 5 行 + 后 5 行，带行号，中间用 `... [N lines truncated] ...` 标记省略行数 |
| **非文本块保留** | 如图片、音频等非文本 content block 原样保留，只替换文本部分 |

**关键设计**：替换后的消息仍然保留原始的 `tool_call_id`、`name`、`id`、`artifact`、`status` 等字段，因此 LangGraph 的 tool_call 配对、前端步骤关联、以及消息身份追踪均不受影响。Agent 在后续对话中如需完整内容，可主动调用 `read_file` 工具指定路径和 offset/limit 分段读取。

### 动作二：去重完全重复的工具结果

| 项目 | 说明 |
|------|------|
| **触发条件** | 新 ToolMessage 的文本 MD5 与历史中某条同 name 的 ToolMessage 完全相同 |
| **处理方式** | 把 content 替换为占位文本，引用首次出现的 `tool_call_id` |
| **保留字段** | `tool_call_id`、`name`、`id` 原样保留，确保 LangGraph 的 tool_call 配对与前端 `deriveStepsFromSubMessages` 的步骤关联不受影响 |
| **占位文本** | `[内容重复已省略] 此工具结果与线程内先前同名工具结果完全相同（首次出现于 tool_call_id: xxx），不再重复展示。如需完整内容，请向上查阅历史中的该次结果。` |

---

## 关键函数说明

### 辅助函数

| 函数 | 作用 |
|------|------|
| `_text_md5(text)` | 计算文本内容的 MD5 哈希，用于完全重复检测 |
| `_state_messages(state)` | 从 Agent state（dict 或 BaseModel）中安全取出 `messages` 列表 |
| `_unwrap_command_messages(update)` | 从 Command update 中取出消息列表，并检测 `REMOVE_ALL_MESSAGES` 哨兵 |
| `_rewrap_command_messages(messages, wrapped)` | 还原 `REMOVE_ALL_MESSAGES` 哨兵 |

### 核心方法

| 方法 | 作用 |
|------|------|
| `_dedup(message, prior_messages)` | 与历史 ToolMessage 比对，完全重复则返回占位消息，否则返回 None |
| `_should_truncate(content_str)` | 判断文本是否超过截断阈值 |
| `_process_tool_message_sync(message, prior_messages)` | 同步路径：去重 → 落盘截断 → 返回处理后的消息 |
| `_process_tool_message_async(message, prior_messages)` | 异步路径：同上，但落盘步骤需 await |
| `_process_result_sync(result, state)` | 同步处理返回值（ToolMessage 或 Command），逐个处理其中的 ToolMessage |
| `_process_result_async(result, state)` | 异步处理返回值，逐个处理其中的 ToolMessage |

### 中间件接口

| 方法 | 作用 |
|------|------|
| `wrap_tool_call(request, handler)` | 同步钩子：先执行 handler，再对结果瘦身 |
| `awrap_tool_call(request, handler)` | 异步钩子：同上，异步版本 |

---

## 安全设计

### Fail-Open 策略

全程 fail-open：任何异常只记日志并返回原始结果，绝不阻断 agent 循环。

| 层级 | 异常处理 |
|------|----------|
| 落盘截断 | `try/except` 包裹，落盘失败保留原结果不截断 |
| 中间件入口 | `try/except` 包裹，瘦身失败原始异常照常向上抛，不吞异常 |

### 作用范围

- **只处理 ToolMessage**：不触碰 AI/Human 消息（AI 消息瘦身属 L2，暂缓）
- **只处理文本内容**：通过 `_extract_text_from_message` 提取文本，非文本内容不受影响

### 可配置性

| 参数 | 说明 |
|------|------|
| `backend` | 落盘超大工具结果用的后端。None 时仍可去重，但超大结果不落盘（等价只做去重） |
| `max_chars_before_truncate` | 触发截断的文本字符阈值。None 关闭截断（只去重）。默认 16000 |

---

## 落盘路径设计

| Backend 类型 | 落盘路径前缀 | 实际落盘位置 |
|-------------|-------------|-------------|
| `CompositeBackend` | `large_tool_results`（相对路径） | fallback 到 default（shell_backend），落盘到 `workspace/large_tool_results/` |
| 非 CompositeBackend | `/large_tool_results`（绝对路径） | 由 backend 路由决定 |

> **设计原因**：CompositeBackend 的路由 `{"/": file_backend}` 会匹配所有以 "/" 开头的路径，导致 `"/large_tool_results"` 错误地落到 `file_backend`（`src/agent/large_tool_results/`）。改用不以 "/" 开头的相对路径后，CompositeBackend 不匹配任何路由，fallback 到 default（shell_backend），落盘到 `workspace/large_tool_results/`。

---

## 依赖关系

```
deepagents.backends.CompositeBackend
deepagents.backends.protocol.BackendProtocol
deepagents.middleware._message_eviction:
  ├── _extract_text_from_message      # 从消息中提取文本（只取 text block，忽略图片/音频等）
  ├── _create_content_preview         # 生成 head(前5行) + tail(后5行) + 截断标记的预览
  ├── _build_evicted_content          # 构建替换后的 content（保留非文本 block）
  ├── _build_evicted_tool_message     # 构建替换后的 ToolMessage（保留 tool_call_id/name/id/artifact/status 等）
  ├── _offload_tool_message_content   # 同步落盘：backend.write + 构建替换消息
  └── _aoffload_tool_message_content  # 异步落盘：await backend.awrite + 构建替换消息
```

### deepagents 落盘函数内部流程

`_offload_tool_message_content` / `_aoffload_tool_message_content` 内部执行：

1. **sanitize tool_call_id**：调用 `sanitize_tool_call_id()` 清理 tool_call_id 中的非法字符
2. **拼接落盘路径**：`{large_tool_results_prefix}/{sanitized_id}`，如 `large_tool_results/call_abc123`
3. **写入 backend**：`backend.write(file_path, content_str)` 或 `await backend.awrite(...)`
4. **失败返回 None**：如果 backend 写入失败（result 为 None 或 result.error 为真），返回 None，调用方保留原始消息
5. **构建替换消息**：
   - 用 `TOO_LARGE_TOOL_MSG` 模板生成替换文本，内含文件路径、read_file 使用指引、head+tail 预览
   - 通过 `_build_evicted_content` 保留原始消息中的非文本 content block（图片、音频等）
   - 通过 `_build_evicted_tool_message` 保留 `tool_call_id`、`name`、`id`、`artifact`、`status`、`additional_kwargs`、`response_metadata` 等所有身份字段

---

## 在 main_agent.py 中的使用

```python
from src.agent.middlewares.message_slimmer import MessageSlimmerMiddleware

# 创建中间件实例
slimmer = MessageSlimmerMiddleware(
    backend=composite_backend,
    max_chars_before_truncate=16000,  # 可调大或置 None 关闭截断
)

# 注册到 agent 的 middleware 列表
agent = create_agent(
    ...
    middleware=[..., slimmer],
)
```

---

## 设计约束与注意事项

1. **异步落盘必须 await**：`_aoffload_tool_message_content` 是协程，必须 await，否则协程对象会泄漏进 messages 通道导致 reducer 崩溃。
2. **去重只比对同 name 的 ToolMessage**：不同工具名即使内容相同也不判定重复，避免误杀。
3. **去重保留 tool_call_id**：前端 `deriveStepsFromSubMessages` 依赖 tool_call_id 关联步骤，必须保留。
4. **Command 中的 REMOVE_ALL_MESSAGES 哨兵**：处理 Command 返回值时需要检测并还原 `REMOVE_ALL_MESSAGES` 哨兵，确保消息列表语义不变。
5. **阈值下限**：16000 字符确保 8.8KB/14KB 的常见重复结果不被截断，截断只针对真正的超大结果（73KB+）。