# langfuse_span.py — LangfuseSpanMiddleware 代码解读

> 源文件：`src/agent/middlewares/langfuse_span.py`
> 类别：**可观测性**
> 挂载点：`wrap_tool_call` + `after_agent`
> 行数：~1156 行（目录内最大文件）

## 1. 解决的问题

Langfuse 三层观测方案中的 **② 结构化层 + ③ 存量数据层**（① CallbackHandler 自动层已有）。在关键工具边界包 skill 级 span，同时承担评分与产物落盘。

## 2. 核心数据结构

### 2.1 TOOL_SKILL_MAP

工具名后缀 → **启发式 skill 分类**：

| 后缀 | skill |
|---|---|
| `run_sql`, `dry_run`, `dry_plan` | `sql-execution` |
| `describe_schema`, `get_mdl`, `list_models` | `schema-linking` |
| `get_all_knowledge`, `get_context`, `recall_queries` | `recall-queries` |
| `query_cube` | `cube-query` |
| `write_file`, `edit_file` | `artifact-write` |

用于筛选需要包 span 的工具和评分定位。

### 2.2 _TOOL_OWNER_SKILLS

工具 → **正向使用它的真实编排 skill**（从各 SKILL.md 抽取）。值长度 = 1 → 唯一归属可直接覆盖过期活动 skill；> 1 → 共享工具需活动 skill 在 owners 内才继承。

### 2.3 _THREAD_ACTIVE_SKILL

进程级 thread→skill 表。deepagents 渐进披露下 `read_file` 命中 `.../{skill}/SKILL.md` 是"当前在跑哪个 skill"的权威信号，但模型常不重读就切 skill，需要 owner 表纠偏。

## 3. 上下文取值函数群

都带多级优先级兜底：

### 3.1 _thread_id（会话级）

```
metadata.langfuse_session_id
  → trace_parent_thread_id
    → configurable.thread_id
      → execution_info.thread_id
```

会话级线程 id（session 分组唯一权威），与执行线程 id 区分。

### 3.2 _parent_trace_id / _parent_obs_id

读 deepagents patch 注入的 metadata——异步子线程 OTel context 已丢失，靠显式注入嵌套回主 trace。

### 3.3 _question_id

每问题 = 一条 chat-turn trace，同会话多问题的产物靠它区分归属（文件名前缀）。

## 4. 主流程 _invoke（wrap_tool_call）

### 4.1 分类与筛选

`_classify_skill` 不命中的工具直接透传（零开销）。

### 4.2 展示 skill 解析

`_resolve_display_skill` 优先级：

1. SKILL.md 读取信号（`read_file` 命中 `{skill}/SKILL.md`）
2. 文件读写归活动 skill
3. 唯一归属（`_TOOL_OWNER_SKILLS` 值长度 = 1）
4. 共享工具条件继承（活动 skill 在 owners 内）
5. 启发式兜底（`TOOL_SKILL_MAP`）

### 4.3 span 启动（三条嵌套路径）

**Path A（子 agent）**：
- metadata 有 `langfuse_parent_trace_id` → `trace_context` 显式嵌套到主 trace
- `parent_obs_id` 优先读 `_ROOT_OBS_MAP`（子 agent root obs → 形成 chat_agent→nl2sql_agent→skill 层级）
- 兜底读 metadata 快照

**Path C（主 agent 直调工具）**：
- OTel 上下文仍在 → 自然挂载
- 丢失时用 `_THREAD_TRACE_MAP`（会话线程→最近新查询 trace）显式嵌套
- 再兜底 `handler.last_trace_id`

**无 parent** → 裸 `start_observation` + `session.id` 属性兜底

### 4.4 v4 兼容补偿

- **M-T8b** `_attach_app_root_claim`：防归巢 span 被误判 `is_app_root` 产生重复 UI 行
- **M-T6b**：给显式嵌套 span 设 `langfuse.trace.name` 属性防 v4 用 skill span 名污染主 trace 名

### 4.5 工具执行后

1. `_finish_span`：output ≤ `LARGE_RESULT_TRUNCATE_CHARS` 直接进 span，超限写 vfs 指针
2. `_dump_process_data`：服务端代写中间产物（0 模型开销）
3. `_record_subject_evidence`：记录评估证据
4. `_maybe_score`：触发评分

## 5. 旁路评估

全部 `try/except`，异常绝不影响工具执行。

### 5.1 _maybe_score（五维评分）

| skill | 评分维度 | 说明 |
|---|---|---|
| `sql-execution` | `sql_valid_score` + `sql_exec_success` | dbmcp 把 DB 报错当返回值，需 `looks_like_exec_error` 从文本识别 |
| `schema-linking` | `schema_match_score` | 表/列匹配度 |
| `artifact-write`（报告文件） | report 维度 judge | 正文取 write_file 的 content 参数而非返回值 |

SQL 执行成功 → 抽样调度 LLM-judge 做深度评估。

### 5.2 _record_subject_evidence + after_agent._assemble_subject

**P1 评估单元**：

- 子线程工具边界把 `run_sql` 完整载荷/报告头部写 sidecar
- 主线程看不到子线程未瘦身数据 → sidecar 是唯一的完整证据来源
- 主 agent run 收尾读证据组装 subject 落盘
- 落盘根在 **data_root** 而非 workspace（`/eval_runs/...`），VFS deny 范围外，物理封堵在线 agent 读到参考答案泄题
- 非终结 run 组不出 subject → 天然幂等

## 6. 进程级状态

| 缓存 | 键 | 值 | 上限 |
|---|---|---|---|
| `_THREAD_ACTIVE_SKILL` | thread_id | 当前活跃 skill 名 | 2000 条 |
| `_THREAD_TRACE_MAP` | thread_id | 最近 trace_id | 500 条 |
| `_ROOT_OBS_MAP` | thread_id | root observation_id | 500 条 |

全部 `threading.Lock` + 容量上限清空。

## 7. 与上下文瘦身的顺序关系

必须注册在 `MessageSlimmer` / `QueryResultOffload` **之后（内层）**：

```
请求流：Slimmer → Offload → LangfuseSpan → 工具
响应流：工具 → LangfuseSpan → Offload → Slimmer
```

LangfuseSpan 吃原始全量 payload → span 记录完整数据
Slimmer/Offload 在外层后处理 → 只有进 state 的消息被瘦身

## 8. 错误处理

- 任何旁路逻辑异常 → 记 warning，不影响工具执行
- Langfuse client 不可用 → 降级为无观测
- span 嵌套解析失败 → 裸 span + session.id 兜底
