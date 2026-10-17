# knowledge_trim.py — KnowledgeTrimMiddleware 代码解读

> 源文件：`src/agent/middlewares/knowledge_trim.py`
> 类别：**上下文管理 / 知识瘦身**
> 挂载点：`wrap_tool_call`

## 1. 解决的问题

wren 的 `get_instructions()` **不带任何参数**（工具层拿不到问题），返回全量知识规则文件。大语料下（如 20.9k 字符）大量内容与当前查询无关，浪费 token 且干扰模型注意力。

只有中间件同时看得见 state / run metadata 与工具结果，也才有能力改写 ToolResult。

## 2. 裁剪对象

**只裁 `get_instructions`**（按后缀匹配以兼容 `wrenai_<库名>_` 前缀）：

| 工具 | 是否裁剪 | 原因 |
|---|---|---|
| `get_instructions` | ✅ | 含规则文件正文，可按相关性筛 |
| `list_knowledge` | ❌ | 只有文件名没有正文，裁它等于给模型假信息 |
| `list_stored_queries` | ❌ | 语义就是「枚举全部」，已被 `path_resolver` fast-path 接管 |
| `get_all_knowledge` | ❌ | 生产容器里没有这个工具 |

## 3. 安全闸

**任一条不过 → 逐字原样返回**，只记一条 info：

| # | 闸 | 说明 |
|---|---|---|
| 1 | **重建 == 原文** | 按 wren `load_rules` 的口径重建分片；拼起来与原文不相等就放弃。这条闸把「同构」变成自验证的——顺序或 strip 差一点只会静默不裁，**绝不会裁错内容** |
| 2 | 原文 ≥ `MIN_CHARS`（默认 8000） | 小语料没必要裁 |
| 3 | 有当前问题 | 取不到问题不裁 |
| 4 | 检索有命中 | 无命中不裁 |
| 5 | **地板**：保留文件数 ≥ `MIN_FILES`（默认 3）且字符数 ≥ 原文 × `KEEP_RATIO`（默认 0.4） | 不足按**原序**补足（口径有先后依赖，不许重排） |

裁剪后追加说明段并列出未展示的文件名；追加后若**反而更长** → 不裁。

## 4. 检索机制

用当前问题对知识文件做检索，识别相关文件。具体实现依赖 `NL2SQL_RETRIEVAL` 配置的检索后端。

## 5. 关键设计决策

### 5.1 说明段措辞

措辞是「**与当前问题无关**」，**不是「读不到」** — 后者会让模型去 VFS 里找并不存在的路径。

### 5.2 保守的默认值

现行默认值下，生产（`knowledge/rules/` 只有 3 个文件）**基本不会触发裁剪**（地板就是 3 份）。这是有意的保守：这条中间件的价值在大语料上，宁可当 no-op 也不冒丢口径的风险。要放开就调 `NL2SQL_KNOWLEDGE_TRIM_MIN_FILES`。

### 5.3 fail-open（与 MessageSlimmer 不同）

本中间件**吞掉所有异常**（与 `MessageSlimmerMiddleware` 的 `raise` 不同）：那里 raise 是为了让瘦身失败不阻断 agent，而这里 raise 会打断工具调用链、最坏损失只是「少吃一点 token」。

## 6. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `NL2SQL_KNOWLEDGE_TRIM` | 开启 | 设为 `off` 全放行 |
| `NL2SQL_RETRIEVAL` | 开启 | 检索后端开关，`off` 时本中间件也不动作 |
| `NL2SQL_KNOWLEDGE_TRIM_MIN_CHARS` | `"8000"` | 原文低于此值不裁 |
| `NL2SQL_KNOWLEDGE_TRIM_MIN_FILES` | `"3"` | 最少保留文件数 |
| `NL2SQL_KNOWLEDGE_TRIM_KEEP_RATIO` | `"0.4"` | 最少保留字符比例 |
| `NL2SQL_KNOWLEDGE_TRIM_HITS` | — | 检索命中数上限 |
