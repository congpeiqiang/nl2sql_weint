# chart_artifact_owner.py — ChartArtifactOwnerMiddleware 代码解读

> 源文件：`src/agent/middlewares/chart_artifact_owner.py`
> 类别：**上下文管理 / 产物归属**
> 挂载点：`wrap_tool_call`

## 1. 解决的问题

图表工具（`create_echarts_visualization`）生成的 HTML 文件需要落盘到特定目录供前端预览和下载。但图表工具的返回值只包含 HTML 内容，不包含落盘路径。此前模型需要自己调 `write_file` 把 HTML 写入磁盘——这多一轮 LLM 交互，且模型可能写错路径或忘记写文件。

本中间件在图表工具返回后**自动代写文件**，并把落盘路径注入返回值，让模型直接引用路径回复用户。

## 2. 核心机制

### 2.1 触发条件

仅当工具名匹配 `_CHART_TOOLS` 时触发：

```python
_CHART_TOOLS = {"create_echarts_visualization", "create_chart", "create_visualization"}
```

其他工具完全透传。

### 2.2 自动落盘

图表工具返回 HTML 内容后：

1. **生成文件名**：`chart_{question_id}_{seq}.html`
   - `question_id` 从 `_question_id(config)` 获取（与 langfuse_span 同源）
   - `seq` 是当问题的图表序号（递增），多图表场景不冲突
2. **确定目录**：`{workspace}/nl2sql_process_data/{thread_id}/chart/`
   - 与 `query_result_offload` 的 `query_result/` 目录平级
   - 自动创建目录（`os.makedirs(exist_ok=True)`）
3. **写文件**：直接 `open().write(html_content)`
4. **注入返回值**：在原 ToolMessage 文本末尾追加：

```
📊 图表已保存到: {file_path}
请将此路径提供给用户，以便预览和下载。
```

### 2.3 与 wrap_tool 的 outputType 注入协作

`wrap_tool` 装饰器（在另一个模块中）为图表工具注入了 `outputType: "html"` 参数，使图表工具知道自己产出的是 HTML。本中间件读取这个标记来确认是否需要代写。

### 2.4 Command 返回值处理

如果图表工具返回 `Command` 对象（含 `update` 和 `goto`），本中间件：

1. 解包 `command.update` 中的 messages
2. 对每条 ToolMessage 执行落盘逻辑
3. 重新打包为 `Command` 返回

## 3. 关键设计决策

### 3.1 为什么不让模型自己写文件

- 模型需要额外一轮 `write_file` 调用 → 延迟翻倍
- 模型可能写错目录（写到 `/workspace/` 根目录而非 `process_data/{thread}/chart/`）
- 模型可能忘记写文件（直接回复 HTML 内容给前端）
- 自动代写 = 零额外 LLM 调用 + 路径确定 + 前端可立即渲染

### 3.2 为什么不集成进图表工具内部

- 图表工具是通用工具（可能被多个 agent 使用），不应耦合特定落盘路径
- 落盘路径涉及 `thread_id`、`question_id` 等 langgraph 运行时信息，图表工具拿不到
- 中间件层是注入运行时上下文的正确位置

### 3.3 文件名中的 question_id

使用 `question_id` 而非 `tool_call_id` 的原因：

- 前端按 question 组织产物，`question_id` 让文件自然归属到对应问题
- `tool_call_id` 是 langgraph 内部 ID，前端无法关联
- 同问题的多图表靠 `seq` 区分

## 4. 与 langfuse_span 的协作

落盘完成后，`langfuse_span` 的 `_dump_process_data` 会：

1. 发现 chart 目录有新文件
2. 记录 artifact 事件到 Langfuse
3. 在 trace 中标记产物路径

两者共享 `nl2sql_process_data/{thread}/` 根目录，但子目录不同：

- `chart/` — 本中间件写入
- `query_result/` — query_result_offload 写入
- `conversation_history/` — 其他模块写入

## 5. 与 message_slimmer 的交互

图表 HTML 通常很大（10-50KB，含 echarts 配置 + 内嵌数据）。`message_slimmer` 会截断超大结果——但图表 HTML 截断后无法渲染。

**解决方案**：本中间件落盘后，ToolMessage 只保留**路径指针 + 简短描述**（< 500 字符），原始 HTML 已在磁盘上。`message_slimmer` 看到的小消息不会触发截断。

## 6. 错误处理

| 场景 | 行为 |
|---|---|
| 目录创建失败 | fail-open，保留原 ToolMessage 不追加路径 |
| 文件写入失败 | 同上 |
| 图表工具返回非 HTML | 跳过落盘，透传 |
| thread_id 取不到 | 用 "unknown" 作为目录名（降级不阻塞） |

## 7. 配置项

无独立配置项。通过图表工具的存在与否自动生效。
