# NL2SQL Skill 流水线全面优化方案

> 日期：2026-08-24
> 状态：已实施，待重启后端验证

---

## 一、优化背景

### 1.1 knowledge-loader 瓶颈（已解决）

`nl2sql-knowledge-loader` 是流水线第 1 步，原 v0.1.0 存在：
- **性能差**：7 步骤、~10 次 LLM 推理轮次 + ~10 次工具调用，串行耗时 20-50 秒
- **硬编码路径**：`/workspace/imdb_project/knowledge/...` 写死在 SKILL.md，切库后失效
- **幽灵工具**：引用 `get_metrics()` 但 Wren MCP server 中不存在

### 1.2 全流水线数据传递低效（本次解决）

knowledge-loader v0.2 已改为 ">15KB 才 write_file"，但下游 5 个 skill 全部写着 **"必须 read_file 读取"** 前序输出：

| 下游 Skill | 强制 read_file 的文件 |
|-----------|---------------------|
| schema-linking | knowledge.json |
| subproblem | knowledge.json + schema.json |
| query-plan | knowledge.json + schema.json + subproblem.json |
| sql-generation | knowledge.json + schema.json + subproblem.json + query_plan.txt |
| performance-optimization | sql.sql |

**数据断链**：knowledge-loader 不写文件时，下游 read_file 会报错。

---

## 二、优化方案

### 2.1 核心变更模式

```
优化前：每个 Skill 必须 read_file 读前序输出 → 必须 write_file 写自己的输出
优化后：优先从对话上下文获取前序输出 → 在回复中直接输出结果 → write_file 仅 >15KB fallback
```

**原理**：sql-of-thought 编排器在同一会话中顺序 `load_skill()` 各步骤，前序 skill 的输出仍在 LLM 上下文窗口中，无需再次 read_file。

### 2.2 各 Skill 具体改动

#### schema-linking（改动最大）

| 项目 | 优化前 | 优化后 |
|------|--------|--------|
| 步骤数 | 5 步 | 3 步 |
| MCP 工具 | list_models + describe_model + describe_schema + get_mdl + get_context + get_instructions + recall_queries | describe_schema + get_context + get_mdl（并行）→ describe_model（按需并行） |
| 删除的冗余调用 | — | list_models（describe_schema 已覆盖）、get_instructions + recall_queries（与 knowledge-loader 重复） |
| 强制顺序 | "必须按顺序依次执行，不得跳过" | 步骤 1 并行，步骤 2 依赖步骤 1 |
| I/O | 强制 read_file + 强制 write_file | 上下文优先，>15KB fallback |

#### subproblem / query-plan / sql-generation / performance-optimization

统一改动：
- **输入**：`必须 read_file` → `优先从对话上下文获取，read_file 作为 fallback`
- **输出**：`必须 write_file` → `在回复中直接输出，>15KB 时 write_file 作为 fallback`
- sql-generation：修正"流水线第2步"→"第5步"

#### sql-of-thought 编排器

- **策略 A 步骤描述**：去除冗长的"必须按顺序依次执行所有工具"文本，精简为每个 Step 一行说明
- **策略 A Step 5.5 / Step 6**：去除硬编码文件路径引用（`/workspace/nl2sql_process_data/...`）
- **策略 B Step 1**：精简描述
- **策略 B Step 3**：去除硬编码文件路径
- **Step 0**：`读取 verdict.json 文件` → `从对话上下文获取裁决结果`
- **新增说明**：`> 数据传递：各 Skill 优先从对话上下文获取前序输出，read_file 仅作为 fallback`

#### clarification / correction

无需改动（已是 MCP 工具优先 / 无文件 I/O 强制）。

---

## 三、Cube 快速通道（Strategy C 增强）

### 3.1 动态提示注入

在 `nl2sql_agent.py` 的 `dynamic_prompt` 中，modeled 分支新增 Cube 摘要注入：

```python
cubes_dir = project_path / "cubes"
cube_names = sorted(d.name for d in cubes_dir.iterdir() if d.is_dir() and (d / "metadata.yml").exists())
if cube_names:
    routing += f"\n可用 Cube（优先使用 Strategy C 快速通道）：{cube_names}\n"
    routing += f"调用 {prefix}_list_cubes() 查看详情，匹配则用 {prefix}_query_cube() 直接查询\n"
```

LLM 在策略选择时就知道有哪些 Cube 可用，匹配则走 Strategy C 跳过完整流水线。

### 3.2 Chinook_Aliyun Cube 定义

创建了 3 个 Cube（`src/agent/workspace/Chinook_Aliyun_semantic/cubes/<name>/metadata.yml`）：

| Cube | 基础表 | 度量 | 维度 | 典型问题 |
|------|--------|------|------|----------|
| sales_analytics | invoiceline→invoice→customer→track→genre | total_revenue, invoice_count, avg_order_value, tracks_sold, line_count | billing_country, billing_city, genre_name, invoice_year, invoice_month | "各国销售额排名" |
| track_popularity | invoiceline→track→album→artist→genre→mediatype | times_purchased, revenue, track_count, avg_unit_price | genre_name, artist_name, album_title, media_type | "最畅销流派" |
| artist_catalog | track→album→artist→genre | track_count, album_count, total_duration_min, total_size_mb, catalog_value | artist_name, genre_name | "哪个艺人专辑最多" |

---

## 四、新增 MCP 工具

### get_all_knowledge()

在 `.venv/Lib/site-packages/wren/mcp_server.py` 的 `_register_knowledge_tools()` 中新增：

```python
@mcp.tool(annotations=ToolAnnotations(title="Get All Knowledge", readOnlyHint=True))
def get_all_knowledge() -> dict:
    """Read all knowledge files (metrics, glossary, caveats) in one call."""
    knowledge_dir = ctx.project / "knowledge"
    result = {}
    for subdir in ("metrics", "glossary", "caveats"):
        d = knowledge_dir / subdir
        if not d.is_dir():
            continue
        entries = []
        for f in sorted(d.glob("*.md")):
            entries.append({"name": f.name, "content": f.read_text(encoding="utf-8")})
        if entries:
            result[subdir] = entries
    return {"knowledge": result}
```

已加入 `nl2sql.yaml` 工具白名单。

---

## 五、预期效果

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 策略 A read_file 调用 | ~10 次 | 0（fallback 时才触发） |
| 策略 A write_file 调用 | ~6 次 | 0（>15KB 时触发） |
| schema-linking MCP 工具 | 7 个（含 2 个冗余） | 5 个（无冗余） |
| knowledge-loader 耗时 | 20-50s | 5-10s |
| 整体流水线预估提速 | — | 30-50% |
| 硬编码 imdb_project 路径 | 4 处 | 0 |
| 幽灵工具引用 | 1 处 | 0 |

---

## 六、改动文件清单

| 文件 | 改动类型 |
|------|----------|
| `.venv/Lib/site-packages/wren/mcp_server.py` | 新增 `get_all_knowledge()` 工具 |
| `src/agent/subagents/configs/nl2sql.yaml` | 工具白名单加 `get_all_knowledge` |
| `src/agent/shared/skills/nl2sql/nl2sql-knowledge-loader/SKILL.md` | v0.1→v0.2 重写 |
| `src/agent/shared/skills/nl2sql/nl2sql-schema-linking/SKILL.md` | 5→3 步 + I/O 改上下文优先 |
| `src/agent/shared/skills/nl2sql/nl2sql-subproblem/SKILL.md` | I/O 改上下文优先 |
| `src/agent/shared/skills/nl2sql/nl2sql-query-plan/SKILL.md` | I/O 改上下文优先 |
| `src/agent/shared/skills/nl2sql/nl2sql-sql-generation/SKILL.md` | I/O 改上下文优先 + 修正步骤号 |
| `src/agent/shared/skills/nl2sql/nl2sql-performance-optimization/SKILL.md` | I/O 改上下文优先 |
| `src/agent/shared/skills/nl2sql/sql-of-thought/SKILL.md` | 编排器策略描述精简 |
| `src/agent/graphs/nl2sql_agent.py` | dynamic_prompt 加 Cube 摘要注入 |
| `src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md` | 第九节改通用 MCP 工具表 |
| `src/agent/workspace/Chinook_Aliyun_semantic/cubes/*/metadata.yml` | 新建 3 个 Cube 定义 |
| 所有 `src/agent/skills/nl2sql/*/SKILL.md` | mirror 同步 |

---

## 七、验证方式

1. **重启后端**
2. **重新构建 Chinook_Aliyun_semantic 语义库**（前端语义库面板 → 🔨 构建）
3. 选择 Chinook_Aliyun 数据库，发起查询：
   - 确认 knowledge-loader 步骤调用 `get_all_knowledge()` 而非 `read_file`
   - 确认不出现 `imdb_project` 硬编码路径
   - 确认下游 skill 不再强制 read_file
   - 确认耗时明显缩短
4. 测试 Cube 通道：问"各国销售额排名" → 应走 Strategy C → `list_cubes` → `query_cube`
5. 切回 imdb 数据库，确认知识库加载仍正常
