# NL2SQL Skill 文件读写传递数据 — 延迟分析与替代方案

> 2026-08-19（实施完成更新）

## 1. 现状：文件读写传递数据的延迟来源

当前策略A流水线每一步都通过 `FilesystemBackend` 做文件 I/O：

```
knowledge-loader  → write_file knowledge.json
schema-linking    → read_file knowledge.json  → write_file schema.json
subproblem        → read_file knowledge.json + schema.json → write_file subproblem.json
query-plan        → read_file knowledge.json + schema.json + subproblem.json → write_file query_plan.txt
sql-generation    → read_file 4个文件 → write_file sql.sql
performance-opt   → read_file sql.sql + schema.json → write_file optimization.json
run_sql           → read_file sql.sql
```

**每步延迟 = 工具调用往返（LLM 决定调用 → 执行 → 结果回注上下文）+ 文件系统 I/O + JSON 序列化/反序列化**。策略A 完整走一遍至少 7 次 write + 15+ 次 read，每次都是独立的 LLM 工具调用往返。

此外，Windows 下的 `FilesystemBackend` 还有 `\\?\` 前缀补丁问题（见 `src/agent/utils/filesystem_backend_patch.py`），`Path.resolve()` 在并发场景下偶发的非确定性前缀会导致额外的 `relative_to` 误判和重试。

---

## 2. 关键发现：所有 skill 运行在同一个 Agent 进程内

从 `src/agent/nl2sql_agent.py` 可以看出，整个 nl2sql 子 agent 是**一个** `create_deep_agent` 实例。`SkillsMiddleware` 通过 `load_skill()` 将 SKILL.md 内容注入到当前 agent 的 system prompt 中，**并不是启动新的子 agent**。所以：

- 所有 skill 共享同一个 Python 进程、同一个 LangGraph 状态
- 文件读写完全是 skill 提示词里的"约定"，不是框架强制的要求
- 已经有 `SkillDataMiddleware`（`src/agent/middlewares/skill_data.py`）提供了 `save_output()` / `get_output()` 方法，但**没有被 skill 使用**——skill 提示词里写的是 `write_file` / `read_file`

---

## 3. 替代方案

### 方案1：内存态数据传递（推荐，改动中等）

利用已有的 `SkillDataMiddleware`，新增两个 MCP 工具（或直接让 skill 调中间件）：

- `save_skill_output(skill_name, data)` → 写入内存 dict（`{thread_id: {skill_name: data}}`）
- `get_skill_output(skill_name)` → 从内存 dict 读取

**优点：**
- 零文件系统开销，数据在 Python 进程内存中
- 无需 JSON 序列化/反序列化往返（数据保持 Python 对象）
- `SkillDataMiddleware` 已存在，只需加 2 个工具 + 改 skill 提示词
- 线程隔离天然支持（dict key 按 thread_id）

**缺点：**
- 数据不持久化（进程重启丢失），但当前文件也只在单次 run 内有效
- 需要改所有 skill 的 SKILL.md 提示词

**延迟收益：** 每个 read/write 从 "LLM工具调用 + 文件I/O + JSON" 变为 "LLM工具调用 + 内存读写"，预计每步节省 50-200ms（取决于 Windows 文件系统状态）。

---

### 方案2：LangGraph State 传递

利用 LangGraph 的 `Command(update={...})` 机制，让中间件拦截 skill 的输出，自动写入 graph state 的 `skill_outputs` 字段，下游 skill 通过 `state.skill_outputs` 读取。

**优点：**
- LangGraph 原生机制，状态变更自动触发节点重算
- 可结合 `interrupt` 做断点续传

**缺点：**
- 当前 `deep_agent` 是 `create_deep_agent` 创建的黑盒 graph，自定义 state schema 需要深入框架内部
- 改动量大，需要重写中间件、graph 定义
- 与现有 `SkillsMiddleware` 的 prompt-injection 模式不兼容

---

### 方案3：直接上下文传递（零工具调用）

sql-of-thought 编排器加载每个 skill 后，直接从 LLM 回复中提取结构化输出（JSON），然后作为下一段 system prompt 注入。完全不走文件系统或工具调用。

**优点：**
- 零工具调用延迟——数据直接在 LLM 上下文流转
- 改动力度集中在 sql-of-thought 的 SKILL.md

**缺点：**
- 依赖 LLM 输出格式的稳定性（结构化输出的 JSON 解析容错）
- 上下文窗口会膨胀（所有中间数据都留在上下文里）
- 对大型 schema JSON（如 `schema.json` 可能有几十 KB）不友好

---

### 方案4：混合方案（✅ 已实施）

**核心思路：** 方案3 + 简化——让 skill 直接在当前上下文中输出结构化结果，由编排器收集，**不经过文件系统**。

具体做法：
1. 修改 sql-of-thought 的 SKILL.md：每个子 skill 产出**结构化 JSON 响应**（放在 ````json ... ```` 代码块中），编排器收集后作为下一 skill 的输入
2. 保留 `FilesystemBackend` 仅用于最终报告输出（`/workspace/report/`）和临时文件
3. 对于大型 schema（`schema.json`），可选择性保留文件写入作为缓存，但读取优先级改为"上下文传递 > 文件读取"

**优点：**
- 核心流水线数据零文件 I/O
- 改动主要在各 SKILL.md 提示词，不涉及框架代码
- 保留文件作为调试/回溯的辅助手段（可选）

**缺点：**
- 依赖 LLM 输出格式稳定性
- 上下文窗口膨胀

---

## 4. 方案对比

| 维度 | 方案1（内存工具） | 方案2（State） | 方案3（纯上下文） | 方案4（混合） |
|------|:---:|:---:|:---:|:---:|
| 改动量 | 中 | 大 | 低 | 低 |
| 延迟改善 | 显著 | 显著 | 最大 | 最大 |
| 风险 | 低 | 高 | 中 | 中 |
| 可调试性 | 好 | 好 | 一般 | 一般 |
| 改动文件 | 加2工具 + 8个SKILL.md | 中间件+graph+SKILL.md | 1个SKILL.md | 1-2个SKILL.md |

---

## 5. 实施记录（2026-08-19）

**已实施方案4（混合方案：零文件 I/O 上下文传递）**，改动如下：

### 5.1 修改的 Skill SKILL.md 文件（9 个）

| 文件 | 改动内容 |
|------|---------|
| `sql-of-thought/SKILL.md` | 新增"数据传递机制（零文件 I/O）"；编排器收集 skill 输出 JSON 并注入下一 skill |
| `nl2sql-knowledge-loader/SKILL.md` | 输出改为结构化 JSON 响应；文件 write 改为可选 fallback |
| `nl2sql-schema-linking/SKILL.md` | 输入来源改为上下文注入；输出结构化 JSON；文件读写降级为 fallback |
| `nl2sql-subproblem/SKILL.md` | 同上 |
| `nl2sql-query-plan/SKILL.md` | 同上 |
| `nl2sql-sql-generation/SKILL.md` | 同上 |
| `nl2sql-performance-optimization/SKILL.md` | 同上 |
| `nl2sql-correction/SKILL.md` | 同上 |
| `nl2sql-clarification/SKILL.md` | 同上 |

**每个 skill 新增的统一模式：**
- `## 数据传递机制（零文件 I/O 优先）` 章节，说明输入来源（编排器注入）和输出方式（回复末尾 JSON）
- 文件读写降级为 fallback：仅 >15KB 或调试时使用
- 读取优先级：**上下文注入 > 文件读取**

### 5.2 修改的主智能体提示词

| 文件 | 改动内容 |
|------|---------|
| `prompt/NL2SQL_SYSTEM_PROMPT.md` | 第八节拆分为 8.1（零文件 I/O 数据传递）和 8.2（文件输出规则），明确流水线 skill 间不经过文件系统 |

### 5.3 无需修改的文件

| 文件 | 原因 |
|------|------|
| `memory/AGENTS.md` / `workspace/memory/AGENTS.md` | 技能清单和加载时机不变，未涉及文件 I/O 传递细节 |
| `memory/ORCHESTRATOR.md` / `workspace/memory/ORCHESTRATOR.md` | 主智能体编排规则不涉及子智能体内部 skill 间数据传递 |
| `subagents/configs/nl2sql.yaml` | 工具列表和技能路径不变 |
| `middlewares/skill_data.py` | 仅方案1需要，方案4不涉及 |
| `nl2sql_agent.py` | 仅方案1需要注册新工具，方案4不涉及 |

### 5.4 数据流变化

**实施前（文件 I/O）：**
```
knowledge-loader → write_file → read_file → schema-linking → write_file → read_file → subproblem → ...
```

**实施后（上下文传递）：**
```
knowledge-loader → 回复 JSON → 编排器提取 → 注入 schema-linking prompt → 回复 JSON → ...
                                                   ↓ fallback（仅 >15KB）
                                              write_file / read_file
```

### 5.5 预期收益

- 策略A 完整流水线：从 7+ write + 15+ read 工具调用 → 0 次文件 I/O（正常情况）
- 每个跳过的 read/write 节省一次 LLM 工具调用往返（含文件系统 I/O）
- 延迟主要来自 LLM 推理本身，不再有文件系统的额外开销

---

## 6. 建议

**推荐先试方案4（混合方案）**：改动最小、延迟收益最大，只需改 skill 提示词。如果 LLM 输出格式不稳定，再回退到方案1（加内存工具做兜底）。

两者可以共存——优先从上下文提取，失败时 fallback 到文件读取。

### 涉及文件清单

| 文件 | 改动类型 | 状态 |
|------|---------|:---:|
| `src/agent/workspace/skills/nl2sql/sql-of-thought/SKILL.md` | 修改编排器逻辑：上下文收集+传递 | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-knowledge-loader/SKILL.md` | 输出改为结构化 JSON 响应 | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-schema-linking/SKILL.md` | 输入来源改为上下文；输出结构化 JSON | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-subproblem/SKILL.md` | 同上 | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-query-plan/SKILL.md` | 同上 | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-sql-generation/SKILL.md` | 同上 | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-performance-optimization/SKILL.md` | 同上 | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-correction/SKILL.md` | 同上 | ✅ |
| `src/agent/workspace/skills/nl2sql/nl2sql-clarification/SKILL.md` | 同上 | ✅ |
| `src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md` | 新增零文件 I/O 数据传递说明 | ✅ |
| `src/agent/middlewares/skill_data.py` | 仅方案1需要：暴露内存读写接口 | 不需要 |
| `src/agent/nl2sql_agent.py` | 仅方案1需要：注册新工具 | 不需要 |