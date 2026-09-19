# query_keywords.py 代码解读

> 文件路径：`src/agent/middlewares/query_keywords.py`（126 行）
> 解读日期：2026-09-18

## 一句话概括

`QueryKeywordsMiddleware`：每次模型调用前，从 `configurable.query_keywords` 读取前端配置的「数据查询触发关键词」，**动态替换系统提示词中的触发词行**，让 LLM 的委派判断与前端完全一致。

## 解决的问题（前后端关键词一致性）

此前触发关键词硬编码在 `MAIN_AGENT_PROMPT.md`，前端改配置后端不知道，判断标准漂移。现在前端把关键词（localStorage 配置）随 `stream.submit` 传入 `configurable.query_keywords`（与 db_name 同一通道）→ **改词只需改前端 localStorage，前后端同步生效，零漂移**。

## 核心实现

### `_resolve_keywords`
- 必须走 `langgraph.config.get_config()`（`request.runtime.config` 恒为空 dict，实证 2026-08-11：前端传了 query_keywords 仍注入默认）；runtime 路径仅兜底。
- 列表/tuple → 「、」join；字符串 → 原样；缺失/空 → `_DEFAULT_KEYWORDS`（"查询、统计、分析、多少、列表、汇总、排名、占比、趋势"，与提示词文件保持一致）。

### `_inject_keywords`
- 定位标记行 `_KEYWORDS_MARKER = "**触发关键词**【数据查询】:"` 并整行替换。
- **marker 特意带【数据查询】限定**：提示词里文档处理/报告生成/图表可视化段也有 `**触发关键词**:`（无限定）行——若无差别替换，用户设置的查询关键词会覆盖其他三类意图的触发词，**破坏意图识别**。
- 提示词中无标记行 → 末尾追加一段（兜底，防提示词文件回退后失效）。
- 替换按行处理，只保留替换后的关键词行，避免新旧两行同时出现。
- `request.override(system_message=SystemMessage(new_text))` 只改出站请求。

## 挂载

主 agent（chat_agent）——决定是否委派 nl2sql 子任务的意图判断发生在主 agent 层。

## 关联文件

- `MAIN_AGENT_PROMPT.md`：标记行所在的基础提示词
- `current_db_context.py` / `thinking_toggle.py` / `tool_filter.py`：同一 configurable 通道的姊妹中间件
