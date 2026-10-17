# 08 · NL2SQL 项目实践

> 本项目的 Langfuse 深度集成架构、关键文件、中间件栈、环境变量和开发者操作指南  
> SDK 版本：`langfuse v4.14.4` | 平台：自托管 `v4.16.0` @ `http://192.168.25.64:3010`

---

## 1. 集成架构总览

本项目实现了**深度的多层 Langfuse 集成**，远超基础的埋点接入：

```
┌──────────────────────────────────────────────────────────────┐
│                    前端（👍/👎 反馈 + 标注页面）                │
└─────────────────────────┬────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────┐
│  ASGI 中间件层                                                │
│  ├─ AuthMiddleware（认证 + Cookie）                            │
│  ├─ LangfuseMetadataMiddleware ← 元数据注入 + 权限夹紧         │
│  └─ DrainGateMiddleware（蓝绿排空闸门）                       │
├──────────────────────────────────────────────────────────────┤
│  Agent 中间件层（外→内）                                       │
│  ├─ ModelRequiredMiddleware（模型可用性检查）                  │
│  ├─ ThinkingToggleMiddleware（模型路由）                       │
│  ├─ MessageSlimmerMiddleware（大消息截断）                     │
│  ├─ TokenMeterMiddleware（token 计量）                        │
│  ├─ TraceRecorderMiddleware ← 本地 SQLite 事件（独立于 LF）   │
│  ├─ LangfuseSpanMiddleware ← 技能级 span + 五维评分 + 落盘     │
│  └─ VfsPathResolverMiddleware（VFS 路径解析）                 │
├──────────────────────────────────────────────────────────────┤
│  LangGraph Agent（挂 CallbackHandler 自动追踪）                │
│  └─ callbacks: [Langfuse CallbackHandler]                    │
├──────────────────────────────────────────────────────────────┤
│  Langfuse SDK v4.14.4                                        │
│  ├─ Traces/Observations（OTel-based）                         │
│  ├─ Scores（5 维确定性+LLM-Judge + 用户反馈）                  │
│  ├─ Prompts（版本化 + A/B 标签 + canary）                     │
│  ├─ Datasets（badcase / goodcase）                            │
│  ├─ Dataset Runs（实验结果 + 回归门禁）                        │
│  └─ Experiments（langfuse.experiment.* OTel 属性注入）        │
└──────────────────────────────────────────────────────────────┘
```

## 2. 关键文件清单

### 2.1 核心基础设施（~3500 行）

| 文件 | 行数 | 职责 |
|------|------|------|
| `src/agent/trace/langfuse_client.py` | ~1340 | **中枢**：客户端单例、CallbackHandler、prompt 管理（get/create/update）、评分、trace 嵌套（M-T3/T5/T7 leaf anchoring）、A/B canary、中文序列化修复、trace input 截断、flush/atexit |
| `src/agent/middlewares/langfuse_span.py` | ~1190 | **技能 span 中间件**：工具分类 → 技能级 span 包裹、在线评估打分触发、中间产物落盘、评估证据采集 |
| `src/api/langfuse_metadata.py` | ~507 | **ASGI 中间件**：拦截 run 创建请求，注入 `langfuse_session_id`/`trace_name`/`tags`/`user_id` + 安全权限夹紧（P1-2） |
| `src/agent/middlewares/trace_recorder.py` | ~430 | **本地 trace 记录器**（独立于 Langfuse）：LLM/Tool 事件 → 本地 SQLite EventStore |
| `src/agent/trace/trace_bind_store.py` | ~SQLite | **持久化绑定**：thread→trace 和 task→trace 映射，跨重启存活 |

### 2.2 评估与打分

| 文件 | 职责 |
|------|------|
| `src/agent/eval/evaluators.py` | **五维评估器**：3 个确定性（同步）+ 3 个 LLM-Judge（采样/异步队列） |
| `src/agent/eval/eval_flags.py` | 运行时可切换评估开关（`NL2SQL_EVAL_ENABLED` 等） |
| `src/agent/eval/run_experiment.py` | **离线实验引擎**（~1517 行）：多臂 A/B、worker 子进程、回归门禁、双落点 |
| `src/agent/eval/collect_badcase.py` | BadCase 自动采集 → Dataset:badcase |
| `src/agent/eval/feedback_gate.py` | 在线用户反馈 A/B 门禁 |
| `src/agent/eval/badcase_status.py` | BadCase 状态跟踪（pending/reviewed/fixed/invalid） |

### 2.3 反馈与标注

| 文件 | 职责 |
|------|------|
| `src/api/message_feedback.py` | 用户反馈 API → `user-feedback` Score |
| `src/api/feedback_annotation.py` | 标注队列 API — 人工复审 BadCase + 补正确 SQL |
| `src/api/feedback_stats.py` | 反馈统计（信噪比等） |
| `src/agent/feedback/store.py` | 本地 SQLite 反馈存储（唯一真相） |

### 2.4 Agent 接线

| 文件 | 职责 |
|------|------|
| `src/agent/graphs/nl2sql_agent.py` | NL2SQL 子 agent：挂载 SpanMiddleware + CallbackHandler |
| `src/agent/main_agent.py` | 主 agent：挂载 SpanMiddleware + CallbackHandler + prompt 拉取 |
| `src/agent/middlewares/deepagents_async_config_patch.py` | 注入 `langfuse_parent_trace_id/obs_id` 到异步子 run |
| `src/api/custom_app.py` | 注册 LangfuseMetadataMiddleware + shutdown flush |
| `start_server.py` | 启动预检：`auth_check()` |

## 3. Trace 嵌套方案

### 3.1 目标层级

```
Session: {thread_id}                          ← LangGraph thread
│
├── Trace: "chat-turn"                        ← 第 1 次查询
│   │   metadata: {user_question, db_name, workspace, skills, prompt}
│   │
│   └── AGENT: chat_agent                     ← root observation
│       ├── AGENT: nl2sql_agent               ← 异步子 Agent（M-T3 注入）
│       │   ├── SPAN: skill:schema-linking:wrenai_get_schema
│       │   ├── SPAN: skill:sql-generation:wrenai_generate_sql
│       │   └── SPAN: skill:sql-execution:dbmcp_execute_sql
│       │
│       ├── AGENT: chat_agent (auto-continue) ← M-T3d 归入本 trace
│       └── GENERATION: llm-call（最终回答）
│
└── Trace: "chat-turn"                        ← 第 2 次查询（新 trace）
```

### 3.2 三个嵌套路径

| 路径 | 场景 | 机制 |
|------|------|------|
| **Path A** | 子 Agent | `NonRecordingSpan` OTel context 注入 |
| **Path C** | 主 Agent 直接 | OTel context 自动继承 |
| **Path M-T7** | 分离线程孤儿 | `_AnchoredClient` 锚定回父 trace |

### 3.3 关键技术点

- **`on_chain_start` monkey-patch**：在根 chain 启动时注入 `root_observation_id`
- **Auto-continue 路由（M-T5）**：按 `task_id` 归属到原始 trace，避免误挂到最新 trace
- **中文序列化修复**：monkey-patch `EventSerializer` 使用 `ensure_ascii=False`
- **`CancelledError` 处理**：加入 `CONTROL_FLOW_EXCEPTION_TYPES`，取消的 run 不标记为错误
- **wrenai 工具显示名**：ASCII 工具名映射回中文数据库名

## 4. 五维评估体系

### 4.1 确定性评估（同步、零成本、每条查询）

| 维度 | Score 名 | 计算方式 | 值域 | 触发点 |
|------|----------|----------|------|--------|
| Schema 匹配 | `schema_match_score` | 发现成功 1.0 / 失败 0.3 | 0.3~1.0 | schema-linking span 结束 |
| SQL 安全 | `sql_valid_score` | `classify_sql()`: read=1.0, full_dump=0.4, write/DDL=0.0 | 0.0~1.0 | sql-execution span 结束 |
| 执行成功 | `sql_exec_success` | 执行成功 1.0 / 失败 0.0 | 0.0~1.0 | sql-execution span 结束 |

### 4.2 LLM-Judge 评估（采样、异步队列）

| 维度 | Score 名 | 评估内容 | 采样率 |
|------|----------|----------|--------|
| SQL 业务正确性 | `sql_biz_correct_score` | SQL 是否回答了用户问题 | `NL2SQL_EVAL_JUDGE_SAMPLE`（默认 30%） |
| 报表准确性 | `report_table_score` | 报表/表格准确度 | 30% |
| 分析报告质量 | `analysis_report_score` | 分析质量 + 幻觉检测 | 30% |

**评估流水线**：

```
查询完成 → 组装评估单元 → 采样判定 → 命中?
                              │
                    ┌─────────┴─────────┐
                    │ 命中               │ 未命中
                    ▼                   ▼
            投入 eval_queue.sqlite    不评估
                    │
                    ▼
            daemon worker 消费
                    │
                    ▼
            拉取 rubric prompt
                    │
                    ▼
            调用 Judge LLM
                    │
                    ▼
            写入 Score + reasoning
```

### 4.3 用户反馈

| Score 名 | 值 | 触发 |
|----------|-----|------|
| `user-feedback` | 1.0 (👍) / 0.0 (👎) / -1.0 (撤销) | 用户在前端点击 |

## 5. 环境变量完整清单

### 5.1 核心连接

```bash
LANGFUSE_ENABLE=true                # 总开关（false=停用所有埋点）
LANGFUSE_SECRET_KEY=sk-lf-xxx       # Secret Key
LANGFUSE_PUBLIC_KEY=pk-lf-xxx       # Public Key
LANGFUSE_BASE_URL=http://host:3001  # Langfuse 服务地址
LANGFUSE_PROJECT_ID=proj_xxx        # 项目 ID
LANGFUSE_RELEASE=v1.0.0             # 发版标记
LANGFUSE_TRACING_ENVIRONMENT=production  # 环境标签
```

### 5.2 Prompt 管理

```bash
LANGFUSE_PROMPT_ENABLED=1           # 1=启用 prompt 拉取, 0=强制本地
LANGFUSE_PROMPT_LABEL=production    # 显式指定 prompt 标签
LANGFUSE_CANARY_LABEL=staging       # 灰度标签
LANGFUSE_CANARY_RATIO=0.1           # 灰度比例（0~1）
```

### 5.3 评估控制

```bash
NL2SQL_EVAL_ENABLED=1               # 评估总开关
NL2SQL_EVAL_JUDGE_ENABLED=1         # LLM-Judge 开关
NL2SQL_EVAL_JUDGE_SAMPLE=0.3        # LLM-Judge 采样率（0~1）
NL2SQL_EVAL_SUBJECT=1               # 评估证据采集开关
NL2SQL_EVAL_JUDGE_QUEUE=1           # Judge 队列开关（实验中置 0）
```

### 5.4 调试与落盘

```bash
NL2SQL_PROCESS_DATA_DUMP=1          # 中间产物落盘
NL2SQL_PROCESS_DATA_DUMP_OUTPUT=0   # 是否包含工具输出
LARGE_RESULT_TRUNCATE_CHARS=8000    # span 输出截断阈值
LANGFUSE_LEAF_NESTING_ENABLE=1      # Leaf observation 嵌套
```

## 6. 开发者操作手册

### 6.1 启动服务

```bash
# 开发模式（前台，Ctrl+C 停止）
cd D:\code_work_space\llm\nl2sql
.venv/Scripts/python.exe start_server.py

# 生产模式（后台分离）
powershell -NoProfile -Command "
  \$env:PYTHONUTF8='1';
  \$env:PYTHONIOENCODING='utf-8';
  \$env:NL2SQL_EVAL_JUDGE_SAMPLE='1.0';
  Start-Process -FilePath '.venv\Scripts\python.exe' \
    -ArgumentList 'start_server.py' \
    -RedirectStandardOutput 'logs/server.out.log' \
    -RedirectStandardError 'logs/server.err.log' \
    -WindowStyle Hidden
"
```

### 6.2 验证 Langfuse 连通

```bash
grep "langfuse\|Langfuse" logs/agent-server.log | head
```

期望：

```
✅ Langfuse: 云端连通 (auth_check 通过)
[langfuse] prompt main_system_prompt(label=production) v12 生效
[langfuse] prompt nl2sql_system_prompt(label=production) v8 生效
```

### 6.3 同步 Prompt

```bash
# 上传所有 prompt 到 staging
PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts \
    --all --label staging --commit "修复 JOIN 逻辑"

# 查看当前版本
PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts --list
```

### 6.4 运行实验

```bash
# BadCase 回归
PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
    --from-badcase --labels production staging \
    --run-name "fix-join-20261014"

# 自定义数据集
PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
    --dataset nl2sql-regression --semantic main \
    --run-name "test-new-prompt"
```

### 6.5 BadCase 管理

```bash
# 手动采集
PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 1

# 查看 pending
.venv/Scripts/python.exe -m agent.eval.badcase_status list --status pending

# 交互式复审
.venv/Scripts/python.exe -m agent.eval.badcase_status review

# 标记
.venv/Scripts/python.exe -m agent.eval.badcase_status mark <trace_id> fixed --note "..."
.venv/Scripts/python.exe -m agent.eval.badcase_status mark <trace_id> invalid --note "..."
```

## 7. 项目内相关文档

| 文档 | 路径 | 内容 |
|------|------|------|
| 接入实现方案 | `docs/langfuse平台/Langfuse接入实现方案.md` | 完整架构设计（M1~M7） |
| 评估精准化方案 | `docs/langfuse平台/NL2SQL-评估精准化设计方案.md` | 评估单元 + rubric + 校准回路 |
| 反馈闭环方案 | `docs/langfuse平台/NL2SQL反馈闭环优化设计方案.md` | 反馈类型 + 标注队列 |
| 全流程实现手册 | `docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md` | 操作手册 |
| Trace 层级方案 | `docs/langfuse平台/Langfuse标准Trace层级方案.md` | 嵌套规范 |
| 实验离线方案 | `docs/langfuse平台/LangfuseExperiment离线实施方案.md` | 四维 A/B |
| 连接说明 | `docs/langfuse平台/Langfuse连接说明.md` | 部署架构 + 运维 |
| Dataset 字段契约 | `docs/langfuse平台/Dataset字段契约.md` | Item 字段约定 |
| BadCase 状态指南 | `docs/langfuse平台/BadCase状态标记使用指南.md` | 状态机操作 |
| 脚本命令手册 | `docs/langfuse平台/项目脚本命令手册.md` | 所有命令参考 |

## 8. 下一步

- [09-LangChain 集成](09-LangChain集成.md)：LangChain/LangGraph 自动追踪
- [12-常见问题与排查](12-常见问题与排查.md)：故障排查指南
