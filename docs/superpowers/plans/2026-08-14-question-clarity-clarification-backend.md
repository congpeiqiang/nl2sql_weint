# 问题清晰度判定与澄清追问 — 后端（P1+P2）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在前端提问链路中加入"问题不清晰 → 大模型追问"能力：主 agent 意图层挡泛问（B），nl2sql 子 agent 澄清门（Step 0）判缺条件/术语歧义/多义并返回 `[需要澄清]`，主 agent 转述、用户回答后合并重新委派。

**Architecture:** 分层判定。B 层是 `MAIN_AGENT_PROMPT.md` 纯提示词新增「意图不清 → 追问」类别。A 层是 nl2sql 子 agent 新增 `nl2sql-clarification` 技能作为 sql-of-thought 的 Step 0：用 `get_context`/`get_instructions` 做 Schema 感知裁决，不清晰时停止流水线并输出固定格式 `[需要澄清]` 消息；主 agent 靠该消息的【原始问题】+【待补充】结构确定性合并重委派（不靠记忆）。

**Tech Stack:** 纯 Markdown（deepagents 技能体系 + 系统提示词）。无 Python 代码改动。技能需同步两份目录（src 与 workspace）。

## Global Constraints

- 技能文件必须同时存在于 `src/agent/skills/nl2sql/` 与 `src/agent/workspace/skills/nl2sql/`（当前 diff 一致，改完必须保持）
- SKILL.md 必须以 YAML frontmatter（`name` + `description`）开头，description 含触发/输入/输出/跳过条件
- 子 agent 进程数据统一写入 `/workspace/nl2sql_process_data/{thread_id}/<skill-name>/`
- 清晰问题（如"查询 average_rating 最高的5部电影"）行为必须**零变化**，不得多一次追问
- 不因 `get_context`/`get_instructions` 工具故障阻塞查询（失败视为 clear 继续）
- 主 agent 规则全部追加到 `MAIN_AGENT_PROMPT.md`，不新增 Python 中间件/工具

---

### Task 1: 新增 `nl2sql-clarification` 技能

**Files:**
- Create: `src/agent/skills/nl2sql/nl2sql-clarification/SKILL.md`
- Copy: `src/agent/workspace/skills/nl2sql/nl2sql-clarification/SKILL.md`

**Interfaces:**
- Consumes: 委派 prompt 中的【任务目标】与【数据库名称】；WrenAI MCP 工具 `get_context` / `get_instructions`
- Produces: `/workspace/nl2sql_process_data/{thread_id}/clarification/verdict.json`（裁决）、`.../clarification/context.json`（get_context 结果）；不清晰时最终回复以 `[需要澄清]` 开头，格式含【原始问题】【当前数据库】【待补充】——Task 4 的主 agent 规则依赖此格式

- [ ] **Step 1: 创建源技能文件**

创建 `src/agent/skills/nl2sql/nl2sql-clarification/SKILL.md`，内容：

```markdown
---
name: nl2sql-clarification
description: "触发：任何数据查询问题的第一步（Step 0），在执行 sql-of-thought 策略决策之前先判断问题是否清晰。覆盖四类不清晰：缺查询条件、业务术语歧义、笼统/太泛、多义二选一。输入：用户问题。输出：清晰度裁决 verdict.json；不清晰时以 [需要澄清] 格式输出追问并停止流水线。跳过：用户直接给出精确 SQL；无 Schema 上下文的基础设施故障。"
---

# NL2SQL 问题清晰度裁决 Skill

## 概述

sql-of-thought 流水线 **Step 0（前置澄清门）**。在进入策略决策（A/B/C）之前，先用 WrenAI 工具做 Schema 感知的清晰度判定。

- **清晰** → 正常进入 sql-of-thought 策略决策
- **不清晰** → 停止流水线，输出 `[需要澄清]` 追问，**不生成 SQL、不执行任何查询**

## 数据存储说明

- **存储路径**: `/workspace/nl2sql_process_data/{thread_id}/clarification/verdict.json`
- 同时写入 `/workspace/nl2sql_process_data/{thread_id}/clarification/context.json`（get_context 检索结果，供后续 schema-linking 复用）
- **自动隔离**: 每个会话（thread_id）使用独立的存储目录

## 输入

- 用户问题（来自委派 prompt 中的【任务目标】）
- 当前数据库名（来自委派 prompt 中的【数据库名称】）
- 若委派 prompt 已含【补充信息】（即用户上轮回答过澄清问题）→ 判定时按补充信息裁决，仍缺才问

## 输出

- `verdict.json`：结构化裁决（必须 write_file 写入）
- 不清晰时，最终回复**必须以以下格式开头**（一字不差）：

```
[需要澄清]
【原始问题】{原样复述用户问题}
【当前数据库】{db_name}
【待补充】
1. {问题1}（选项：{A} / {B}）？
2. {问题2}？
```

## 执行步骤

### Step 1: Schema + 知识检索（只调 2 个工具）

1. 调用 `get_context(question)` — 语义检索与问题相关的 Schema 片段（哪个模型/列命中）
2. 调用 `get_instructions()` — 获取业务规则、指标/术语定义
3. 若两者任一失败（库离线/工具报错）→ 直接判定 `clear=true` 继续流水线（基础设施故障不阻塞查询），跳过 Step 2-4

将 `get_context` 的检索结果 write_file 到 `.../clarification/context.json`。

### Step 2: 对照判据裁决

按优先级判断，命中任一即触发（一个不清晰点即可问）：

| reason | 判定信号 |
|--------|---------|
| `VAGUE` | get_context 无任何模型命中 / 语义相似度极低，问题不像数据查询 |
| `MISSING_CONDITION` | 命中模型/列，但关键过滤字段（时间范围、实体名、分组维度）问题里没给 |
| `TERM_AMBIGUOUS` | 知识库/指标定义里同一术语有多个口径 |
| `MULTI_CHOICE` | get_context 命中多个互斥的维度列/模型，各自都"像" |
| `OK` | 单一模型+列明确映射 |

**防过度追问（硬性规则）**：
1. **能猜就猜**：单一解释明显占优（>7 成把握）→ 判定 `clear=true` 并写 assumption，不阻塞
2. **只问关键缺口**：issues 最多 1~3 个，一个问题最多 3 个选项
3. **一轮上限**：若委派 prompt 已含【补充信息】仍判不清 → 带 assumption 继续，不再追问
4. 清晰时也必须 write_file verdict.json（assumption 留痕），不得跳过

### Step 3: 输出 verdict.json

```json
{
  "clear": false,
  "reason": "MISSING_CONDITION",
  "original_question": "查询销售数据",
  "db_name": "imdb",
  "issues": [
    {
      "type": "missing_condition",
      "field": "时间范围",
      "question": "请提供要查询的时间范围（如：2024年全年、最近30天）"
    }
  ],
  "assumption": null
}
```

- `clear=true` 时：`reason="OK"`，`assumption` 写你采用的默认解释（如"按2024年全年、月活口径"）
- `issues[].type` 取值：`missing_condition` / `term_ambiguity` / `multi_choice` / `vague`

### Step 4: 分流

- `clear=true` → 回复"问题清晰，进入流水线"，然后正常加载 `sql-of-thought` 执行
- `clear=false` → **立即停止**，最终回复以 `[需要澄清]` 格式输出追问，**不要调用任何其他工具、不要生成 SQL、不要执行 dry_run/run_sql**

## 错误处理

- get_context/get_instructions 失败 → 写 `.../clarification/error.json`（`{"error": "澄清检查跳过", "detail": "..."}`），判定 clear 继续
- 若已存在 `.../clarification/error.json`，不重复写
```

- [ ] **Step 2: 复制到 workspace 技能目录**

```bash
mkdir -p "D:/code_work_space/llm/nl2sql/src/agent/workspace/skills/nl2sql/nl2sql-clarification"
cp "D:/code_work_space/llm/nl2sql/src/agent/skills/nl2sql/nl2sql-clarification/SKILL.md" \
   "D:/code_work_space/llm/nl2sql/src/agent/workspace/skills/nl2sql/nl2sql-clarification/SKILL.md"
```

- [ ] **Step 3: 验证技能可被发现（skill-discovery 检查）**

Run:

```bash
cd "D:/code_work_space/llm/nl2sql" && PYTHONPATH=src .venv/Scripts/python.exe -c "
from pathlib import Path
# SkillsMiddleware 从 /workspace/skills/nl2sql/ 加载（filesystem backend 映射到 src/agent/workspace）
p = Path('src/agent/workspace/skills/nl2sql/nl2sql-clarification/SKILL.md')
assert p.exists(), 'skill file missing'
text = p.read_text(encoding='utf-8')
assert text.lstrip().startswith('---'), 'must start with YAML frontmatter'
assert 'name: nl2sql-clarification' in text, 'missing name frontmatter'
assert 'get_context' in text and '[需要澄清]' in text, 'missing core protocol'
print('OK: nl2sql-clarification skill file valid, len=%d' % len(text))
"
```

Expected: `OK: nl2sql-clarification skill file valid, len=<N>`

- [ ] **Step 4: Commit**

```bash
git add src/agent/skills/nl2sql/nl2sql-clarification src/agent/workspace/skills/nl2sql/nl2sql-clarification
git commit -m "feat(nl2sql): 新增 nl2sql-clarification 澄清门技能 (Step 0)"
```

---

### Task 2: 接入 sql-of-thought Step 0

**Files:**
- Modify: `src/agent/skills/nl2sql/sql-of-thought/SKILL.md`（概述后新增 Step 0 章节，改策略决策图）
- Copy: `src/agent/workspace/skills/nl2sql/sql-of-thought/SKILL.md`

**Interfaces:**
- Consumes: Task 1 的技能 `nl2sql-clarification`（名字/输出格式）
- Produces: 编排器在策略决策前先跑澄清门；`load_skill("nl2sql-clarification")` 成为强制第一步

- [ ] **Step 1: 在源 sql-of-thought SKILL.md 概述后插入 Step 0 章节**

在 `## 策略决策（入口）` 一行**之前**插入以下内容：

```markdown
## Step 0：澄清检查（前置门槛，必做）

收到用户问题后，**先**加载 `nl2sql-clarification` 技能做清晰度裁决，**再**进入策略决策：

1. `load_skill("nl2sql-clarification")` → 按技能流程执行（get_context + get_instructions → verdict.json）
2. 读取 `/workspace/nl2sql_process_data/{thread_id}/clarification/verdict.json`：
   - `clear=true` → 继续下方「策略决策」
   - `clear=false` → **停止**，按该技能格式输出 `[需要澄清]` 追问，本技能结束，**不得**进入策略决策或调用任何查询工具
```

- [ ] **Step 2: 更新源 SKILL.md 策略决策入口图（加 Step 0 到最顶）**

将策略决策代码块的第一行 `用户问题` 之前插入一行 `Step 0: 澄清门（nl2sql-clarification）→ 不清晰则输出 [需要澄清] 并停止`，使其成为：

```markdown
```
用户问题
    │
    ├─ Step 0: 澄清门(nl2sql-clarification)  → clear=false 则输出 [需要澄清] 停止
    │   └─ clear=true → 按下方优先级选策略
    │
    ├─ 匹配 Cube 指标?
```

- [ ] **Step 3: 复制修改后的文件到 workspace**

```bash
cp "D:/code_work_space/llm/nl2sql/src/agent/skills/nl2sql/sql-of-thought/SKILL.md" \
   "D:/code_work_space/llm/nl2sql/src/agent/workspace/skills/nl2sql/sql-of-thought/SKILL.md"
```

- [ ] **Step 4: 验证两处同步且包含 Step 0**

```bash
cd "D:/code_work_space/llm/nl2sql" && diff -q src/agent/skills/nl2sql/sql-of-thought/SKILL.md src/agent/workspace/skills/nl2sql/sql-of-thought/SKILL.md && grep -c "Step 0" src/agent/skills/nl2sql/sql-of-thought/SKILL.md
```

Expected: `diff` 无输出（两文件一致），`grep -c` 输出 `≥2`

- [ ] **Step 5: Commit**

```bash
git add src/agent/skills/nl2sql/sql-of-thought src/agent/workspace/skills/nl2sql/sql-of-thought
git commit -m "feat(nl2sql): sql-of-thought 接入 Step 0 澄清门"
```

---

### Task 3: 更新 NL2SQL 子 agent 系统提示词技能清单

**Files:**
- Modify: `src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md`（技能加载策略列表 + 技能清单 + 工作流加 Phase 0）

**Interfaces:**
- Consumes: Task 1 的技能名 `nl2sql-clarification`
- Produces: 子 agent 知道澄清门技能存在及加载时机（对 Task 2 的编排器声明形成支撑）

- [ ] **Step 1: 三处插入 `nl2sql-clarification`**

对 `src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md` 做以下三处修改：

(a) 「## 四、技能加载策略」段，在"**技能名称列表：**"一行中 `sql-of-thought`（编排器）**前面**插入：

```markdown
`nl2sql-clarification`（Step 0 前置澄清门）、
```

使该行变成：`**技能名称列表：** `nl2sql-clarification`（Step 0 前置澄清门）、`sql-of-thought`（编排器）、`nl2sql-knowledge-loader`、...`

(b) 「## 六、 技能」清单，在 `- sql-of-thought（编排器）` 上一行插入：

```markdown
- nl2sql-clarification（Step 0 澄清门）
```

(c) 「## 三、三阶段工作流」的 Phase 1 之前，插入新的 Phase 0 小节：

```markdown
### Phase 0：问题清晰度裁决（前置门槛，必做）

**Step 0** → 加载 `nl2sql-clarification`：基于 Schema（get_context）与知识库（get_instructions）判断问题是否清晰。
- 清晰 → 进入 Phase 1
- 不清晰 → **立即停止**，以 `[需要澄清]` 格式输出追问，不进入任何查询流程
```

- [ ] **Step 2: 验证三处均已插入**

```bash
cd "D:/code_work_space/llm/nl2sql" && grep -c "nl2sql-clarification" src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md
```

Expected: `3`

- [ ] **Step 3: Commit**

```bash
git add src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md
git commit -m "feat(nl2sql): 子 agent 系统提示词登记澄清门技能"
```

---

### Task 4: 主 agent 转述 + 合并重委派 + B 意图层

**Files:**
- Modify: `src/agent/prompt/MAIN_AGENT_PROMPT.md`

**Interfaces:**
- Consumes: Task 1 的 `[需要澄清]` 消息格式（【原始问题】【当前数据库】【待补充】）
- Produces: 主 agent 三层行为——(a) 识别 `[需要澄清]` 并原样转述；(b) 用户回答后从历史最后一条该标记消息提取【原始问题】合并重委派；(c) 意图层新增「意图不清 → 追问」

- [ ] **Step 1: B 意图层——在「## 🔍 意图识别规则」最顶部插入新类别**

将下面内容插在 `### 一般对话 → 直接回答` 之前：

```markdown
### 意图不清 / 信息不足 → 直接追问（优先级最高）

触发关键词：问题笼统到无法判断任务类型、无明确查询对象、指代不明
示例：

- "分析一下数据" → 追问想分析哪方面
- "随便看看" → 追问想看什么数据
- "帮我查查"（没说查什么）→ 追问要查什么

行为：直接向用户追问 **1 个问题**（不调用任何工具、不委派子智能体），只问最关键的一点。

**防误伤（关键）**：能归为数据查询的问题**一律交给 nl2sql 子智能体**判断（它有 Schema，判得更准）；本层只在"连类别都无法确定"时才开口。若用户回答后仍无法归类 → 按最可能的类别继续，不再追问。
```

- [ ] **Step 2: 在「### 异步子智能体操作」小节内追加两条强制规则**

在该小节末尾（`#### 进度查询规则（严格遵守）` 之前或之后均可，保持相邻）追加：

```markdown
#### 子智能体返回 [需要澄清]（强制处理）

`check_async_task` 返回的结果若**以 `[需要澄清]` 开头**，说明子智能体判定问题信息不足，需要用户补充：

1. **原样转述**给用户——保留【原始问题】【当前数据库】【待补充】格式，**不要改写、不要丢选项**
2. **不要**把该结果当作查询结果呈现；**不要**触发"推荐图表 → 渲染图表 → 生成报告"链路
3. 结束回复，等用户回答

无论结果是自动通知续跑取回还是用户询问进度取回，只要命中 `[需要澄清]` 前缀，一律按本条处理。

#### 用户回答澄清问题后重新委派（强制）

上一轮你转述过 `[需要澄清]` 消息后，用户本轮的回答是对澄清问题的补充回答，**不是**新问题。

重新委派时：

1. 找到对话历史中**最后一条**以 `[需要澄清]` 开头的消息
2. 从其中提取【原始问题】和【当前数据库】
3. 把「原始问题 + 用户本轮回答」合并为新【任务目标】，重新 `start_async_task`：
   - 例：原始问题"查询销售数据" + 用户回答"2024年按地区" → 【任务目标】"查询销售数据，2024年，按地区"
4. 把用户回答原样作为【补充信息】附带在委派 prompt 中，供子智能体澄清门复核
5. 若用户回答仍模糊（如"随便/都行"）→ 直接以合理默认继续执行，不再追问
```

- [ ] **Step 3: 验证三处均已插入**

```bash
cd "D:/code_work_space/llm/nl2sql" && grep -c "需要澄清" src/agent/prompt/MAIN_AGENT_PROMPT.md && grep -c "意图不清" src/agent/prompt/MAIN_AGENT_PROMPT.md
```

Expected: 第一行 `≥4`（转述规则+合并规则+示例+意图类别触发描述），第二行 `≥2`

- [ ] **Step 4: Commit**

```bash
git add src/agent/prompt/MAIN_AGENT_PROMPT.md
git commit -m "feat(agent): 主 agent 增加澄清转述/合并重委派协议与意图不清追问层"
```

---

### Task 5: 端到端验证（重启 2026 后实测）

**Files:**
- Create: `d:\tmp\verify_clarification_flow.py`
- Note: 本任务需要重启 `start_server.py`（langgraph dev 服务）让新提示词/技能生效，且只能由用户执行重启

**Interfaces:**
- Consumes: 全部 Task 1-4 的产出；运行的 2026 服务
- Produces: 行为证据——缺条件问题返回 `[需要澄清]`；清晰问题零变化

- [ ] **Step 1: 编写 SSE 验证脚本**

创建 `d:\tmp\verify_clarification_flow.py`：

```python
"""验证澄清门：缺条件问题 → [需要澄清]；清晰问题 → 直接出结果。"""
import io, json, sys, urllib.request

BASE = "http://localhost:2026"
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

def post_json(path, payload, accept="application/json", timeout=15):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": accept}, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        print("HTTP ERROR", e.code, e.read().decode("utf-8", "replace")[:500]); sys.exit(1)

def run_question(q, label):
    thread_id = json.loads(post_json("/threads", {}).read())["thread_id"]
    payload = {
        "assistant_id": "chat_agent",
        "stream_mode": ["messages-tuple", "values"],
        "input": {"messages": [{"type": "human", "content": q}]},
        "config": {"recursion_limit": 500, "configurable": {"enable_thinking": "false"}},
    }
    resp = post_json(f"/threads/{thread_id}/runs/stream", payload, accept="text/event-stream", timeout=300)
    evt, ai_text = None, ""
    for raw in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        if line.startswith("event:"):
            evt = line[6:].strip()
        elif line.startswith("data:") and evt == "messages":
            try:
                chunk, _ = json.loads(line[5:].strip())
                if chunk.get("type") not in ("ai", "AIMessageChunk"):
                    continue
                c = chunk.get("content")
                text = c if isinstance(c, str) else (c[0].get("text") if isinstance(c, list) and c else "")
                if text:
                    ai_text += text
            except Exception:
                pass
    needs = "[需要澄清]" in ai_text
    print(f"[{label}] 「{q}」 → 需澄清={needs} 正文len={len(ai_text)}")
    print(f"    预览: {ai_text[:180]!r}")
    try:
        req = urllib.request.Request(BASE + f"/threads/{thread_id}", method="DELETE")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
    return needs

# 缺条件问题 → 期望 [需要澄清]=True（如果模型判定能猜，则 False 但应带假设说明）
run_question("查询电影的平均评分", "缺条件")
# 清晰问题 → 期望 [需要澄清]=False
run_question("查询 average_rating 最高的5部电影", "清晰基线")
```

- [ ] **Step 2: 用户重启 2026 服务**

请用户在终端重启 `start_server.py`（提示词/技能改动需重启生效），确认启动日志无报错。

- [ ] **Step 3: 运行验证脚本**

```bash
cd "D:/code_work_space/llm/nl2sql" && .venv/Scripts/python.exe d:/tmp/verify_clarification_flow.py
```

Expected:
- `[缺条件] 「查询电影的平均评分」→ 需澄清=True`（理想；若模型判"能猜"则 False，需人工确认转述内容合理）
- `[清晰基线] 「查询 average_rating 最高的5部电影」→ 需澄清=False`（**必须** False，且能正常返回查询结果）

- [ ] **Step 4: 人工复核不清晰问题的转述链路**

浏览器发"查询电影的平均评分"：
- 应看到主 agent 原样转述 `[需要澄清]`（含【原始问题】【待补充】）
- 回复"按类型分组，只要2024年" → 应重新委派并返回真实结果
- **不得**出现把澄清问题当作查询结果 + 触发图表/报告链路的错误

- [ ] **Step 5: 记录验证结果到方案文档**

在 `docs/agent优化记录/问题清晰度判定与澄清追问方案.md` 的验证段追加实测结果（需澄清/清晰基线两行 + 浏览器链路确认）。

---

## Follow-up plans（本计划范围外，独立子系统）

- **P3 前端澄清 chips**：把 `[需要澄清]` 消息解析为可点击选项（另一仓库 `harness-deep-agents-ui`，独立计划）
- **P4 术语歧义精度增强**：澄清门接知识库 glossary/metrics 全文检索（本计划已用 get_instructions 覆盖基础场景）
- **显式状态硬化（可选）**：若实测主 agent 合并仍不稳，再引入 `MainAgentState.pending_clarification` + 专用工具（本计划先用消息历史协议，零代码成本）
