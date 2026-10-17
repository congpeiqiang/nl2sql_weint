# cube_route_gate.py — CubeRouteGateMiddleware 代码解读

> 源文件：`src/agent/middlewares/cube_route_gate.py`
> 类别：**执行监督 / 路由治理**
> 挂载点：`wrap_tool_call` / `awrap_tool_call`

## 1. 解决的问题

生产 trace `9afcd8ea`（2026-09-30，库 `witops`）：问题「今年 8 月和 9 月新增和完成缺陷」——`bug_quality` cube **完全覆盖**（`bug_count=COUNT(*)`、`finished_count=SUM(...)`），模型也 `describe_cube(name="bug_quality")` 看过它，却**一次 `query_cube` 都没调**，手写了 5 段 `LEFT JOIN ... FROM v_bug ... GROUP BY` 直接 `run_sql`。

**根因不是模型跑偏，是成文策略允许的**：`SKILL.md` 写着「多时间轴对比 → 转手写」。本中间件**不替代**那次政策改写（提示词/SKILL.md 同一轮一起收紧），它是**提示词之外的确定性兜底**。

## 2. 核心规则

三条全中才拦：

| # | 条件 | 说明 |
|---|---|---|
| ① | 目标是 `*_run_sql`（含 `run_sql`），**排除** `dbmcp_*` | 已建模库的直连通道由 `QueryGateMiddleware` 的通道硬闸兜住 |
| ② | SQL 里抽出的**关系集合非空且完全落在某个 cube 的 `base_object` 上** | 子查询的 `FROM v_bug` 会被独立命中 |
| ③ | SQL 含聚合函数或 `GROUP BY` | 明细行级查询不拦 |

**放行场景**：明细行级 / 窗口函数 / 跨 cube 关联 / 引用底座之外的表 → 关系集合不 ⊆ 单底座 → 放行。

## 3. SQL 关系提取

正则从 SQL 中提取被引用的关系名：

```python
_REL_RE = re.compile(
    r"\b(?:from|join)\s+([`\"]?[\w$]+[`\"]?(?:\s*\.\s*[`\"]?[\w$]+[`\"]?)*)",
    re.IGNORECASE,
)
```

**设计细节**：

- 首字符不允许 `(`/空格 → `FROM (SELECT …)` 派生表天然不匹配（正确：子查询里的 `FROM v_bug` 会被同一条正则独立命中）
- 段可带反引号/双引号 → 统一剥引号再取最后一段
- **已知保守不精确**：不认字符串字面量，`WHERE note='from x'` 会把 `x` 当成关系。但前提是它与某个 cube 底座逐字同名，且后果只是每线程一次软提醒

## 4. 软/防打扰设计

照抄 `query_gate` 的模式：

- **每线程至多拦一次**，用**独立**的 `_CUBE_GATE_REMINDED` 命名空间（不与 QueryGate 的 `_REMINDED` 共用——共用会让两个软门互相吃掉提醒机会）
- 文案里写明「cube 确实表达不了就直接原样重发」→ **重发必放行**（逃生口）
- 取不到 db_name / 项目路径 / cube 元信息 → 一律 **fail-open** 放行
- `NL2SQL_CUBE_ROUTE_GATE=off` → 全放行（一键回退）

## 5. deny 消息

```
Error: 当前查询涉及的所有表/视图均落在 Cube `{cube_name}` 的底座 `{base_object}` 上，
且包含聚合操作。请优先使用 `query_cube` 工具（支持维度筛选、度量聚合和时间粒度参数）。
如果 Cube 确实无法表达此查询（如跨底座关联），请直接原样重发本次 SQL。
```

`status="error"` + `Error:` 前缀让 `langfuse_span._maybe_score` 把它当 exec 失败（`looks_like_exec_error`）——⚠️ 观测口径：执行失败率会小幅抬升，别误判成回归。

## 6. 挂载位置

`nl2sql_agent._middleware`，紧挨 `QueryGateMiddleware()`：

- LangfuseSpan **内层** → 被拦的 run_sql 仍在 span 里可见
- SqlReadOnly **外层** → 写/DDL 仍由硬闸兜底

## 7. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `NL2SQL_CUBE_ROUTE_GATE` | 开启 | 设为 `off` 全放行 |

## 8. 与 query_gate 的协作

```
cube_route_gate（最先：cube vs SQL 路由）
    ↓ 非 cube 或已放行
query_gate（wrenai vs dbmcp 路由）
    ↓
sql_approval（SQL 只读 + LIMIT 归一）
```

各自维护独立的提醒 set，互不干扰。
