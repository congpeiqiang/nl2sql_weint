# recall_queries 检索方案对比：GrepIndex vs LanceDBIndex

**日期**：2026-08-20
**类型**：技术调研
**决策**：当前阶段采用 **GrepIndex**，后续示例积累到一定规模后升级至 **LanceDBIndex**

---

## 一、背景

`recall_queries` 是 WrenAI MCP 工具之一，用于根据用户 NL 问题检索历史相似 NL→SQL 示例，辅助 NL2SQL 纠错阶段（Phase 3）的 SQL 修复。

该工具在 `wrenai` 包中实现了两种检索后端：

| 后端 | 依赖 | 当前状态 |
|------|------|---------|
| `GrepIndex` | 无（标准库） | ✅ 当前使用 |
| `LanceDBIndex` | `lancedb` + `sentence-transformers` | ❌ 未安装（需 `wrenai[memory]` extra） |

---

## 二、技术原理对比

### 2.1 GrepIndex — 词法匹配

**核心算法**：

```python
def _tokens(text: str) -> set[str]:
    """对中文文本做 token 切分后返回 token 集合"""
    # 使用 jieba 分词 + 正则切分英文/数字
    return set(tokens)

def _score(question: str, pair: dict) -> int:
    q_tokens = _tokens(question)
    nl_tokens = _tokens(pair["nl"])
    sql_tokens = _tokens(pair["sql"])
    
    # 交集大小 = 得分
    score = len(q_tokens & (nl_tokens | sql_tokens))
    
    # 子串精确命中加分
    if question in pair["nl"] or pair["nl"] in question:
        score += 5

    return score
```

**特点**：
- 零依赖，纯 Python 标准库
- 确定性：相同输入始终返回相同结果
- 完全基于 token 表面匹配，不理解语义

### 2.2 LanceDBIndex — 语义向量检索

**核心流程**：

```
问题文本
  → sentence-transformers 模型编码（384维向量）
  → LanceDB 向量索引 ANN 检索（余弦相似度）
  → 返回 Top-K 结果
```

**模型**：`paraphrase-multilingual-MiniLM-L12-v2`
- 参数量：~118M
- 模型大小：420MB
- 输出维度：384
- 支持语言：50+ 种（含中文）
- 架构：MiniLM（轻量 BERT 蒸馏版）

**特点**：
- 理解语义，能匹配同义词、同义句
- 近似结果，非确定性
- 重依赖：需要下载 420MB 模型文件

---

## 三、准确度对比

### 3.1 核心差异

| 场景 | GrepIndex | LanceDBIndex |
|------|-----------|-------------|
| "电影" vs "影片" | ❌ 零匹配（不同 token） | ✅ 语义相近 |
| "多少部" vs "数量" | ❌ 零匹配 | ✅ 语义相近 |
| "2020年之后" vs "2020年及以后" | ✅ 共享 `2020`、`年` | ✅ 语义相近 |
| 精确子串命中 | ✅ +5 分加权 | 无特殊加权 |
| 中英文混合 | ✅ 正则切分 | ✅ 多语言模型 |

### 3.2 中文同义异构的挑战

GrepIndex 的硬伤在于中文表达的同义异构非常普遍。

用户问同一个问题，可能说：
- "2020 年之后有多少部电影？"
- "统计 2020 年及以后的影片数量"
- "从 2020 年开始，一共出了多少电影？"

**GrepIndex**：只能靠 `2020`、`年` 等 token 匹配，三句话得分完全相同。如果历史库中只有"统计 2020 年及以后的影片数量"这条，而用户问的是"从 2020 年开始，一共出了多少电影"，token 重叠只有 `2020`，得分 2——**可能被其他得分更高的无关条目挤掉**（`recall_queries` 默认 `limit=3`）。

**LanceDBIndex**：三句话的 embedding 向量距离很近，都能稳定排到 top-3。

### 3.3 recall_queries 在 NL2SQL 中的角色

`recall_queries` 的使用场景决定了它对最终准确度的影响权重：

```
recall_queries 主要用于 Phase 3（纠错阶段）
  → SQL 执行失败
  → 检索相似历史正确 SQL
  → 作为参考辅助纠错
```

- **不是** SQL 生成的主路径输入
- 是**纠错时的辅助参考**
- 当前 `knowledge/sql/` 下只有 **6 条**示例，覆盖场景非常有限

### 3.4 准确度结论

| 知识库规模 | GrepIndex 适用性 | LanceDBIndex 适用性 | 推荐 |
|-----------|-----------------|--------------------|------|
| 6 条（当前） | ✅ 足够，6 条全部返回也才 6 条 | ❌ 大材小用，成本高 | **GrepIndex** |
| 50+ 条 | ⚠️ 部分问题，token 重合度低的可能漏 | ✅ 语义匹配优势开始显现 | LanceDBIndex |
| 200+ 条 | ❌ 词法匹配严重退化 | ✅ 必须 | **LanceDBIndex** |

**关键结论**：当前阶段（6 条示例），两个方案对最终 NL2SQL 准确度的影响差异**几乎为零**——因为：
1. `recall_queries` 本身不是主路径，只是纠错辅助
2. 6 条示例覆盖的场景太少，语义匹配的优势无法体现
3. 真正决定准确度的是 Schema Linking 和 SQL Generation 的提示词质量

---

## 四、耗时对比

### 4.1 分阶段耗时

| 阶段 | GrepIndex | LanceDBIndex |
|------|-----------|-------------|
| 初始化（首次） | **0ms** | **3-10s**（加载 420MB 模型） |
| 初始化（后续） | **0ms** | **~0.2s**（warm_up 探针 embedding） |
| 单次搜索 | **<5ms** | **~0.2-0.4s** |
| └ 问题向量化 | — | 0.1-0.3s |
| └ 向量检索 | — | <0.01s |
| └ 文件读取 + token 切分 | <5ms | — |

### 4.2 端到端对比

| 调用次数 | GrepIndex | LanceDBIndex |
|----------|-----------|-------------|
| 首次 | **<5ms** | **3-10s** |
| 每次 | **<5ms** | **~0.3-0.5s** |
| 10 次累计 | **<50ms** | **~5-8s** |

**耗时差距约 100 倍**（5ms vs 500ms 每次）。

对于策略 B 快速通道中也可能调用 `recall_queries` 的场景，500ms 的延迟会直接加在用户感知的端到端时延上。

### 4.3 MCP stdio 开销

两种方案共享的 MCP 开销是另一个延迟来源：

```
子智能体 → MCP stdio (JSONRPC) → WrenAI MCP Server → recall_queries()
```

每次调用需要经过 JSONRPC 序列化/反序列化 + 子进程通信，这部分开销对两种方案是相同的。但 `GrepIndex` 的 <5ms 搜索时间使得 MCP 开销成为主要瓶颈，而 `LanceDBIndex` 的 0.5s 搜索时间会让 MCP 开销显得微不足道。

---

## 五、资源对比

| 维度 | GrepIndex | LanceDBIndex |
|------|-----------|-------------|
| 额外依赖 | 无 | `lancedb` + `sentence-transformers` + `torch` |
| 磁盘占用 | 0 | ~420MB（模型文件） |
| 内存占用 | 忽略不计 | ~500MB（模型加载后） |
| 首次启动 | 即时 | 需下载模型（首次） + 加载 3-10s |
| pip 安装 | 无需额外操作 | `pip install wrenai[memory]` |
| 跨平台兼容 | ✅ 100% | ⚠️ torch 在部分 Windows 环境有问题 |

---

## 六、决策与迁移路径

### 6.1 当前决策：采用 GrepIndex

**理由**：
1. 知识库仅 6 条示例，语义匹配优势无法体现
2. 零依赖、零延迟、零维护成本
3. `recall_queries` 在 NL2SQL 中只是纠错辅助，非主路径
4. 100 倍的耗时差距对用户体验有直接影响

### 6.2 升级触发条件

当满足以下**任一**条件时，应评估升级至 LanceDBIndex：

1. **知识库规模**：`knowledge/sql/` 中示例积累到 **50+** 条
2. **使用场景变化**：`recall_queries` 从纠错辅助提升为主路径输入（如策略决策前的 few-shot 检索）
3. **准确度瓶颈**：实际使用中发现 GrepIndex 频繁返回不相关结果，影响纠错效率
4. **用户反馈**：频繁出现"同义词匹配失败"导致纠错不准

### 6.3 升级实施步骤

```
1. pip install wrenai[memory]
   → 安装 lancedb + sentence-transformers + torch

2. 模型预热
   → 首次启动时自动下载 paraphrase-multilingual-MiniLM-L12-v2（420MB）
   → 也可提前下载到 ~/.cache/huggingface/

3. 修改 wrenai MCP server 添加索引缓存
   → 模块级 _idx_cache 避免每次工具调用重建 MemoryStore
   → 详见 docs/bug修复记录/recall_queries工具调用耗时优化.md

4. 渐进式切换
   → 先在一个项目上开启 LanceDBIndex 做 A/B 对比
   → 验证准确度提升和延迟可接受后全量切换

5. 回退方案
   → 设置环境变量 WREN_MEMORY_BACKEND=grep 即可回退
   → 或 pip uninstall lancedb sentence-transformers
```

### 6.4 长期优化建议

1. **扩大知识库**：6 条示例太少，无论用哪种检索方案，覆盖度都是瓶颈。建议逐步积累真实场景的 NL→SQL 对
2. **混合检索**：可考虑 GrepIndex（精确匹配）+ LanceDBIndex（语义匹配）混合打分，取交集或加权合并
3. **索引预热**：如果后续升级到 LanceDBIndex，建议在 MCP server 启动时预热模型，避免首次调用 3-10s 的冷启动

---

## 七、相关文件

| 文件 | 说明 |
|------|------|
| `wren/memory/index_backend.py` | `GrepIndex` / `LanceDBIndex` 实现 |
| `wren/memory/store.py` | `MemoryStore` — 每次调用重建 |
| `wren/memory/embeddings.py` | `get_embedding_function()` / `warm_up()` |
| `wren/memory/markdown.py` | `load_query_pairs()` — 读取 `knowledge/sql/*.md` |
| `src/agent/workspace/imdb_project/knowledge/sql/` | 当前 6 条 SQL 示例 |
| [recall_queries工具调用耗时优化.md](../bug修复记录/recall_queries工具调用耗时优化.md) | 耗时优化 bug 记录（含索引缓存方案） |