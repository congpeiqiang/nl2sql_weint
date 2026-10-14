# 08 · NL2SQL 项目实践

> 本文档详细说明 NL2SQL Agent 项目中 Langfuse 的集成架构、关键文件、配置参数和开发者操作指南。

---

## 1. 集成架构总览

本项目使用 **Langfuse SDK v4.14.4**，连接到自托管 Langfuse 实例，实现了深度的多层集成：

```
┌─────────────────────────────────────────────────────────┐
│                    前端（👍/👎 反馈）                      │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│  ASGI 中间件层                                           │
│  ├─ AuthMiddleware（认证）                                │
│  ├─ LangfuseMetadataMiddleware ← 元数据注入 + 权限夹紧    │
│  └─ DrainGateMiddleware（排空闸门）                       │
├──────────────────────────────────────────────────────────┤
│  Agent 中间件层（外→内）                                   │
│  ├─ ModelRequiredMiddleware                             │
│  ├─ ThinkingToggleMiddleware                            │
│  ├─ MessageSlimmerMiddleware                            │
│  ├─ TokenMeterMiddleware                                │
│  ├─ TraceRecorderMiddleware ← 本地 SQLite 事件           │
│  ├─ LangfuseSpanMiddleware ← 技能级 span + 评分 + 落盘    │
│  └─ VfsPathResolverMiddleware                           │
├──────────────────────────────────────────────────────────┤
│  LangGraph Agent（挂 CallbackHandler 自动追踪）           │
│  └─ callbacks: [Langfuse CallbackHandler]               │
├──────────────────────────────────────────────────────────┤
│  Langfuse SDK v4.14.4                                    │
│  ├─ Traces/Observations（OTel）                          │
│  ├─ Scores（5 维 + 用户反馈）                             │
│  ├─ Prompts（版本化 + A/B 标签）                          │
│  ├─ Datasets（badcase / goodcase）                       │
│  ├─ Dataset Runs（实验结果）                              │
│  └─ Experiments（OTel 属性注入）                          │
└──────────────────────────────────────────────────────────┘
```

## 2. 关键文件

### 2.1 核心基础设施

| 文件 | 行数 | 职责 |
|------|------|------|
| `src/agent/trace/langfuse_client.py` | ~1340 | 客户端单例、CallbackHandler、prompt 管理、评分、trace 嵌套、A/B 灰度 |
| `src/agent/middlewares/langfuse_span.py` | ~1190 | 技能级 span 中间件、在线评估打分、中间产物落盘 |
| `src/api/langfuse_metadata.py` | ~507 | ASGI 中间件：注入 Langfuse 元数据 + 安全权限夹紧 |
| `src/agent/middlewares/trace_recorder.py` | ~430 | 本地 trace 记录器（独立于 Langfuse，写入本地 SQLite） |
| `src/agent/trace/trace_bind_store.py` | ~SQLite | thread→trace 和 task→trace 持久化绑定（跨重启） |

### 2.2 评估与打分

| 文件 | 职责 |
|------|------|
| `src/agent/eval/evaluators.py` | 5 维评估器：3 个确定性（同步）+ 3 个 LLM-Judge（采样/异步） |
| `src/agent/eval/eval_flags.py` | 运行时可切换的评估开关 |
| `src/agent/eval/run_experiment.py` | 离线 A/B 实验运行器（多臂、回归门禁） |
| `src/agent/eval/collect_badcase.py` | BadCase 自动采集 → Langfuse Dataset |
| `src/agent/eval/feedback_gate.py` | 在线用户反馈 A/B 门禁 |
| `src/agent/eval/badcase_status.py` | BadCase 状态跟踪 |

### 2.3 反馈与标注

| 文件 | 职责 |
|------|------|
| `src/api/message_feedback.py` | 用户反馈 API → 写入 `user-feedback` Score |
| `src/api/feedback_annotation.py` | 标注队列 API — 人工复审 BadCase |
| `src/agent/feedback/store.py` | 本地 SQLite 反馈存储 |

### 2.4 Prompt 管理

| 文件 | 职责 |
|------|------|
| `src/agent/prompt/sync_prompts.py` | 本地 prompt 文件 → Langfuse 上传同步 |
| `src/agent/utils/prompt_versioning.py` | 实验用 prompt 版本快照管理 |

## 3. 五维评估体系

### 3.1 确定性评估（同步、零成本、每条查询）

| 维度 | Score 名 | 计算方式 | 值域 |
|------|----------|----------|------|
| Schema 匹配 | `schema_match_score` | 发现成功 1.0 / 失败 0.3 | 0.3 ~ 1.0 |
| SQL 安全 | `sql_valid_score` | `classify_sql()`: read=1.0, full_dump=0.4, write/DDL=0.0 | 0.0 ~ 1.0 |
| 执行成功 | `sql_exec_success` | 执行成功 1.0 / 失败 0.0 | 0.0 ~ 1.0 |

### 3.2 LLM-as-Judge 评估（采样、异步队列）

| 维度 | Score 名 | 说明 | 采样率 |
|------|----------|------|--------|
| SQL 业务正确性 | `sql_biz_correct_score` | SQL 结果是否回答了用户的问题 | 30% |
| 报表准确性 | `report_table_score` | 报表/表格的准确度 | 30% |
| 分析报告质量 | `analysis_report_score` | 分析质量 + 幻觉检测 | 30% |

### 3.3 用户反馈

| Score 名 | 值 | 触发 |
|----------|-----|------|
| `user-feedback` | 1.0 (👍) / 0.0 (👎) / -1.0 (撤销) | 用户在前端点击 |

## 4. 环境变量配置

### 4.1 核心连接

```bash
LANGFUSE_ENABLE=true                # 总开关（false = 停用所有埋点）
LANGFUSE_SECRET_KEY=sk-lf-xxx       # Secret Key
LANGFUSE_PUBLIC_KEY=pk-lf-xxx       # Public Key
LANGFUSE_BASE_URL=http://host:3001  # Langfuse 服务地址
LANGFUSE_PROJECT_ID=proj_xxx        # 项目 ID
LANGFUSE_RELEASE=v1.0.0             # 发版标记
LANGFUSE_TRACING_ENVIRONMENT=production  # 环境标签
```

### 4.2 Prompt 管理

```bash
LANGFUSE_PROMPT_ENABLED=1           # 1=启用 prompt 拉取, 0=强制本地
LANGFUSE_PROMPT_LABEL=production    # 显式指定 prompt 标签
LANGFUSE_CANARY_LABEL=staging       # 灰度标签
LANGFUSE_CANARY_RATIO=0.1           # 灰度比例（0~1）
```

### 4.3 评估控制

```bash
NL2SQL_EVAL_ENABLED=1               # 评估总开关
NL2SQL_EVAL_JUDGE_ENABLED=1         # LLM-Judge 开关
NL2SQL_EVAL_JUDGE_SAMPLE=0.3        # LLM-Judge 采样率（0~1）
NL2SQL_EVAL_SUBJECT=1               # 评估证据采集开关
```

### 4.4 调试与落盘

```bash
NL2SQL_PROCESS_DATA_DUMP=1          # 中间产物落盘
NL2SQL_PROCESS_DATA_DUMP_OUTPUT=0   # 是否包含工具输出
LARGE_RESULT_TRUNCATE_CHARS=8000    # span 输出截断阈值
```

## 5. Trace 嵌套方案

本项目的 trace 嵌套方案（M-T3/T5/T7）确保子 Agent 的 observation 正确归属到主 trace：

```
主 Trace: chat_agent
├── 🤖 nl2sql_agent（子 Agent）
│   ├── schema-linking（技能 span）
│   │   └── wrenai_get_schema（工具调用）
│   ├── sql-generation（技能 span）
│   │   └── wrenai_generate_sql（工具调用）
│   └── sql-execution（技能 span）
│       └── dbmcp_execute_sql（工具调用）
│
├── 🤖 report_builder（子 Agent，报表场景）
│   └── ...
│
└── 📊 Scores（五维评分）
```

### 关键技术点

- **OTel NonRecordingSpan 注入**：子 Agent 的 CallbackHandler 通过 OTel context 继承主 trace 的 trace_id
- **Leaf Nesting（M-T7）**：从分离线程产生的孤儿 observation 通过 `_AnchoredClient` 锚定回父 trace
- **Auto-continue 路由**：连续问同一问题时，复用原 trace ID
- **中文序列化修复**：monkey-patch `EventSerializer` 使用 `ensure_ascii=False`

## 6. 开发者操作指南

### 6.1 启动服务

```bash
# 开发模式（前台，Ctrl+C 停止）
.venv/Scripts/python.exe start_server.py

# 生产模式（后台分离）
powershell -NoProfile -Command "
  \$env:PYTHONUTF8='1';
  \$env:PYTHONIOENCODING='utf-8';
  Start-Process -FilePath '.venv\Scripts\python.exe' \
    -ArgumentList 'start_server.py' \
    -RedirectStandardOutput 'logs/server.out.log' \
    -RedirectStandardError 'logs/server.err.log' \
    -WindowStyle Hidden
"
```

### 6.2 验证 Langfuse 连通

```bash
# 检查启动日志
grep "langfuse\|Langfuse" logs/agent-server.log | head
```

期望看到：

```
✅ Langfuse: 云端连通 (auth_check 通过)
[langfuse] prompt main_system_prompt(label=production) v12 生效
[langfuse] prompt nl2sql_system_prompt(label=production) v8 生效
```

### 6.3 同步 Prompt

```bash
# 将本地 prompt 文件上传到 Langfuse
PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts --label staging

# 查看当前版本
PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts --list
```

### 6.4 运行实验

```bash
# 使用 BadCase 数据集做回归
PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
    --dataset badcase \
    --prompt-label staging \
    --from-badcase \
    --run-name "fix-issue-20261014"

# 使用自定义数据集
PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
    --dataset nl2sql-regression \
    --semantic main \
    --run-name "test-new-prompt"
```

### 6.5 BadCase 管理

```bash
# 手动触发采集
PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 1

# 查看 pending 状态
.venv/Scripts/python.exe -m agent.eval.badcase_status list --status pending

# 交互式复审
.venv/Scripts/python.exe -m agent.eval.badcase_status review

# 标记
.venv/Scripts/python.exe -m agent.eval.badcase_status mark <trace_id> fixed --note "修复说明"
.venv/Scripts/python.exe -m agent.eval.badcase_status mark <trace_id> invalid --note "误报原因"
```

## 7. 中间件栈详解

### 7.1 LangfuseMetadataMiddleware（ASGI 层）

拦截 run 创建请求，注入：

| 字段 | 来源 | 用途 |
|------|------|------|
| `langfuse_session_id` | thread_id | Langfuse Sessions 分组 |
| `langfuse_trace_name` | assistant_id | Trace 名称 |
| `langfuse_tags` | metadata.tags | Trace 标签 |
| `langfuse_user_id` | authenticated user | 用户标识 |

同时做安全权限夹紧：将 `configurable.user_id` 钳制到已认证身份。

### 7.2 LangfuseSpanMiddleware（Agent 层）

将工具调用按技能分类并创建 span：

| 技能类别 | 包含工具 |
|----------|----------|
| schema-linking | `wrenai_get_schema`, `dbmcp_get_schema` |
| sql-generation | `wrenai_generate_sql`, `wrenai_dry_plan` |
| sql-execution | `dbmcp_execute_sql`, `dbmcp_execute_query` |
| knowledge-retrieval | `get_instructions`, `get_knowledge` |
| chart-generation | `create_chart`, `create_html_chart` |

每个 span 创建时触发在线评估打分和中间产物落盘。

## 8. 项目相关文档

更多详细文档位于 `docs/langfuse平台/` 目录：

| 文档 | 内容 |
|------|------|
| `Langfuse接入实现方案.md` | 完整实现设计方案 |
| `NL2SQL-评估精准化设计方案.md` | 五维评估设计 |
| `NL2SQL反馈闭环优化设计方案.md` | 反馈闭环设计 |
| `NL2SQL-Langfuse全流程实现手册.md` | 操作手册 |
| `LangfuseExperiment离线实施方案.md` | 离线实验方案 |
| `Dataset字段契约.md` | Dataset Item 字段约定 |
| `Langfuse标准Trace层级方案.md` | Trace 嵌套规范 |
| `项目脚本命令手册.md` | 所有脚本命令参考 |

## 9. 下一步

- [09-常见问题与排查](09-常见问题与排查.md)：遇到问题时的排查指南
