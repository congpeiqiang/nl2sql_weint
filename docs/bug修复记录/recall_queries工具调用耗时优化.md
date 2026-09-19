# wrenai_imdb_recall_queries 工具调用耗时优化

**日期**：2026-08-20
**类型**：性能优化
**严重程度**：中（首次调用 3-10s，后续每次 ~0.5s）

---

## 一、问题描述

`wrenai_imdb_recall_queries` 工具调用耗时异常，首次调用 3-10 秒，后续每次调用仍需 ~0.5 秒。对于一个仅检索 6 条 SQL 示例的语义搜索工具，这个延迟不合理。

### 复现条件

- 安装了 `wrenai[memory]` extra（`lancedb` + `sentence_transformers` 可用）
- 子智能体调用 `recall_queries(question, limit)` 时

---

## 二、根因分析

### 调用链路

```
recall_queries(question, limit)
  → get_index(ctx.project, mem_path)     # 每次调用都新建 MemoryStore
    → LanceDBIndex(project_path, path)
      → MemoryStore(path)
        → get_embedding_function()        # 创建 sentence-transformers 嵌入函数
        → warm_up(embed_fn)               # 触发模型加载 + 探针 embedding
  → idx.search(question, limit)
    → MemoryStore.recall_queries(query)
      → embed_fn.compute_query_embeddings(query)  # 对问题做向量化
      → LanceDB vector search                      # 向量检索
```

### 核心问题：每次调用都重建 MemoryStore

MCP stdio server 的工具函数是**无状态**的——每次工具调用都是独立的函数执行。WrenAI MCP server 中 `recall_queries` 的实现每次调用都执行 `get_index()`，而 `get_index()` 内部创建新的 `MemoryStore`，导致：

1. **每次连接 `lancedb.connect()`**
2. **每次创建 `sentence-transformers` 嵌入函数**
3. **每次 `warm_up()` 触发模型加载**（首次才真正加载，后续命中缓存但仍有一次探针 embedding）

### 耗时分解

| 阶段 | 首次调用 | 后续调用 | 说明 |
|------|---------|---------|------|
| 加载 `paraphrase-multilingual-MiniLM-L12-v2` 模型 | 3-10s | 0s | 420MB 模型文件，首次从磁盘加载到内存 |
| `warm_up()` 探针 embedding | 0.1-0.5s | 0.1-0.5s | 每次调用都执行 |
| `compute_query_embeddings(question)` | 0.1-0.3s | 0.1-0.3s | 每次调用都执行 |
| LanceDB 向量检索 | <0.01s | <0.01s | 6 条记录，极快 |
| 新建 `MemoryStore` + `lancedb.connect()` | 0.1s | 0.1s | 每次调用 |

### 对比：GrepIndex 回退模式

如果 `lancedb` 或 `sentence_transformers` 不可用，会回退到 `GrepIndex`（纯 token 匹配），**毫秒级完成**。当前 6 条 SQL 示例的规模下，token-overlap 匹配完全够用。

---

## 三、解决方案

### 方案 1：MCP Server 启动时缓存 MemoryIndex 实例（推荐）

**位置**：WrenAI MCP server 源码（`wrenai` 包中 `recall_queries` 工具所在的模块）

**原理**：将 `get_index()` 的结果缓存为模块级变量，避免每次工具调用都重建。

```python
# 模块级缓存
_idx_cache: dict[str, MemoryIndex] = {}

def _cached_index(project_path: Path, mem_path: str) -> MemoryIndex:
    """Get or create a cached MemoryIndex for the given project."""
    key = str(project_path)
    if key not in _idx_cache:
        _idx_cache[key] = get_index(project_path, mem_path)
    return _idx_cache[key]

@mcp.tool(annotations=ToolAnnotations(title="Recall Queries", readOnlyHint=True))
def recall_queries(question: str, limit: int = 3) -> dict:
    idx = _cached_index(ctx.project, mem_path)  # 使用缓存
    return {"matches": idx.search(question, limit=limit)}
```

**效果**：
- 首次调用：3-10s（模型加载无法避免）
- 后续调用：~0.2s（仅 embedding 计算）

**注意**：此修改在 WrenAI 上游包中，不在本项目代码内。可通过以下方式应用：
1. 向上游提 PR
2. 在本项目中 monkey-patch 对应模块
3. 等待上游更新后升级依赖

### 方案 2：设置环境变量切换回 GrepIndex（临时方案）

```bash
export WREN_MEMORY_BACKEND=grep
```

完全跳过 embedding 模型加载，使用 token-overlap 匹配。对于当前 6 条 SQL 示例的规模，精度差异可忽略，延迟降至毫秒级。

### 方案 3：减少不必要的 recall_queries 调用

检查 SKILL.md 中是否在策略 B（快速通道）阶段不必要地引导了 LLM 调用 `recall_queries`。`recall_queries` 主要用于 Phase 3（纠错阶段），简单查询无需调用。

---

## 四、相关文件

| 文件 | 说明 |
|------|------|
| `wren/memory/index_backend.py` | `get_index()` / `GrepIndex` / `LanceDBIndex` |
| `wren/memory/store.py` | `MemoryStore` — 每次调用重建 |
| `wren/memory/embeddings.py` | `get_embedding_function()` / `warm_up()` — 模型加载耗时 |
| `wren/memory/markdown.py` | `load_query_pairs()` — 读取 `knowledge/sql/*.md` |
| `src/agent/workspace/imdb_project/knowledge/sql/` | 6 个 SQL 示例文件 |