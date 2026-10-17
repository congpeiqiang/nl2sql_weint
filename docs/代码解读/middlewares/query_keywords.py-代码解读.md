# query_keywords.py — QueryKeywordsMiddleware 代码解读

> 源文件：`src/agent/middlewares/query_keywords.py`
> 类别：**配置注入**
> 挂载点：`wrap_model_call` / `awrap_model_call`

## 1. 解决的问题

前端 localStorage 配置的查询触发关键词（如"查询、统计、分析"）需要与 LLM 的委派判断保持同步。如果前后端关键词不一致，前端判定为"查询意图"并委派 nl2sql 子任务，但模型可能因为不同的触发词列表而不执行查询。

## 2. 核心机制

每次模型调用前，从 `configurable.query_keywords` 读取前端传入的关键词，**替换系统提示词中 `**触发关键词**【数据查询】:` 标记行**。

### 2.1 关键词读取

```python
def _resolve_keywords(self, request):
    # 必须走 langgraph.config.get_config()（runtime.config 恒空）
    configurable = {}
    try:
        from langgraph.config import get_config as _cfg
        if _cfg is not None:
            configurable = (_cfg().get("configurable", {}) or {})
    except Exception:
        pass
    kw = configurable.get("query_keywords")
    if isinstance(kw, (list, tuple)) and kw:
        return "、".join(str(k) for k in kw if str(k).strip())
    if isinstance(kw, str) and kw.strip():
        return kw
    return self.default_keywords  # "查询、统计、分析、多少、列表、汇总、排名、占比、趋势"
```

### 2.2 marker 限定

标记行必须带 **【数据查询】** 限定：

```python
_KEYWORDS_MARKER = "**触发关键词**【数据查询】:"
```

**为什么需要限定**：MAIN_AGENT_PROMPT.md 中有四类意图的触发关键词（数据查询、文档处理、报告生成、图表可视化），只替换数据查询段的触发词行。如果 marker 不带限定，替换时会误覆盖其他三类意图的触发词，破坏意图识别。

### 2.3 替换逻辑

```python
def _inject_keywords(self, request):
    keywords = self._resolve_keywords(request)
    system_message = request.system_message
    if system_message is None:
        return request
    text = system_message.text or ""
    if self.marker not in text:
        # 无标记行 → 追加兜底
        new_text = f"{text}\n\n{self.marker} {keywords}"
    else:
        # 逐行替换，只保留替换后的关键词行
        lines = text.split("\n")
        new_lines = []
        for line in lines:
            if self.marker in line:
                new_lines.append(f"{self.marker} {keywords}")
            else:
                new_lines.append(line)
        new_text = "\n".join(new_lines)
    new_system_message = SystemMessage(content=new_text)
    return request.override(system_message=new_system_message)
```

## 3. 关键词形态

| 输入类型 | 处理 |
|---|---|
| `list` / `tuple`（如 `["查询", "统计"]`） | 用"、"join → `"查询、统计"` |
| `str`（如 `"查询、统计"`） | 直接使用 |
| 空 / None | 回退默认 `"查询、统计、分析、多少、列表、汇总、排名、占比、趋势"` |

## 4. 关键设计决策

### 4.1 为什么替换 system prompt 而非用户消息

- 触发关键词是**模型的系统级指令**，应在 system prompt 中
- 用户消息会被历史稀释，system prompt 每轮都在
- 与前端 localStorage 的配置语义对齐（系统级配置）

### 4.2 为什么用逐行替换而非正则

- 提示词是 markdown 格式，标记行是完整的一行
- 逐行替换更精确，不会误伤相邻行
- 避免正则的边界匹配问题

## 5. 错误处理

- `get_config()` 异常 → 回退默认关键词
- system_message 为 None → 不注入，返回原 request
- marker 不在提示词中 → 在末尾追加（兜底）
