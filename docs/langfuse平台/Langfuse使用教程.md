# Langfuse 使用教程（登录后 · 接入已有 Agent 项目）

> 适用环境：自托管 Langfuse **v4.16.0** @ `http://8.163.4.42:3001`（UI 与 cloud.langfuse.com 一致）
> 适用对象：已有 Agent 项目（如 NL2SQL Agent），需要接入监控 / 评估
> 更新时间：2026-08-24（升级 v4 后重写，SDK 用法已实测验证；补 §8 重启方式）

---

## 0. 登录

浏览器打开 `http://8.163.4.42:3001`

| 项目 | 值 |
|------|-----|
| 账号 | `admin@langfuse.local` |
| 密码 | `Langfuse@f3b933a41e9d` |

登录后左上角可见组织 **AgentLab**，项目 **default**（已建好，含 API 密钥，无需再建）。

## 1. 项目与密钥（接入前确认）

```text
项目名称        : default
公钥 Public Key : pk-cd49050e51b62fdcf1a4784e54ccd1ab
密钥 Secret Key : sk-de66697585ac4f49915f0259b0ce9d54a59206821070dfc4b3fccf635a13d59f
服务地址 Host   : http://8.163.4.42:3001
```

## 2. 界面导航（v4 新 UI，与云端一致）

| 菜单 | 用途 |
|------|------|
| **Traces** | 查看所有 trace（一次 Agent 任务 = 一条 trace），含 LLM 调用/工具调用时间线、token、耗时、成本 |
| **Generations** | 只看 LLM 生成记录 |
| **Scores** | 所有打分记录（人工/代码/LLM 评估） |
| **Evaluations** | 配置 LLM-as-Judge 自动评估 |
| **Datasets** | 沉淀测试集，批量回归评估 |
| **Prompts** | Prompt 版本管理与发布 |
| **Settings** | 项目密钥、成员、集成配置 |

> ⚠️ v4 架构说明：v4 的数据核心是**事件（events）**，SDK 上报会落入 ClickHouse 事件表；
> 旧版 `/api/public/traces` 等上报/查询接口在 v4 已移除（events_only 模式），请使用新版 SDK。

## 3. 方式一：Python SDK v4 接入（推荐，已实测）

### 3.1 安装（需要 v4 版本 SDK）

```bash
pip install -U langfuse
# 验证：python -c "import langfuse; print(langfuse.version)" 应 >= 4.x
# 本教程实测版本：4.14.4
```

### 3.2 客户端初始化

```python
from langfuse import Langfuse

langfuse = Langfuse(
    public_key="pk-cd49050e51b62fdcf1a4784e54ccd1ab",
    secret_key="sk-de66697585ac4f49915f0259b0ce9d54a59206821070dfc4b3fccf635a13d59f",
    host="http://8.163.4.42:3001",
)
```

### 3.3 NL2SQL Agent 完整示例（v4 官方 API，已端到端验证入库）

> v4 SDK 中 `langfuse.trace()` **已不存在**，改用 `start_as_current_observation()` 上下文管理器：
> 最外层观察（`as_type="agent"`）即 trace 根，嵌套的 `generation` / `tool` 自动成为子节点。

```python
from langfuse import Langfuse

langfuse = Langfuse(public_key="pk-...", secret_key="sk-...", host="http://8.163.4.42:3001")

def nl2sql_agent(question: str):
    with langfuse.start_as_current_observation(
        name="nl2sql", as_type="agent",          # 根 = trace
        input=question,
        metadata={"db": "chinook@8.163.4.42"},   # 任意元数据
    ):
        # 1) LLM 生成 SQL（generation 记录模型/token/成本）
        with langfuse.start_as_current_observation(
            name="generate-sql", as_type="generation",
            model="gpt-4o", model_parameters={"temperature": 0},
            input={"question": question},
        ) as gen:
            sql = llm_call(question)             # 你的 LLM 调用
            gen.update(output=sql)               # 或 gen.end(output=sql)

        # 2) 执行 SQL（tool 节点，记录结果/错误）
        with langfuse.start_as_current_observation(
            name="execute-sql", as_type="tool", input={"sql": sql},
        ) as span:
            rows, err = execute_on_chinook(sql)  # psycopg2 连 chinook
            span.update(output={"row_count": len(rows) if rows else 0,
                                "error": str(err) if err else None})

        # 3) 打分（评估正确性，可对比 golden SQL）
        langfuse.score_current_trace(
            name="sql-correctness", value=1.0 if not err else 0.0,
            comment="compare with golden SQL",
        )

    langfuse.flush()   # 程序退出前必须调用，否则数据不上报
    return answer
```

> `as_type` 可选：`generation` / `embedding` / `span` / `agent` / `tool` / `chain` / `retriever` / `evaluator` / `guardrail`

### 3.4 v4 SDK 常用 API 速查（实测签名）

| 需求 | 写法 |
|------|------|
| 开启根节点（=trace） | `with langfuse.start_as_current_observation(name="...", as_type="agent", input=...)` |
| 子节点 span | `with langfuse.start_as_current_observation(name="...", as_type="span" \| "tool", ...)` |
| LLM 记录 | `as_type="generation", model="gpt-4o", input=..., output=...` |
| 补输出/元数据 | `gen.update(output=...)` / `span.update(metadata=...)` / `...end(output=...)` |
| trace 级 IO | `langfuse.set_current_trace_io(input=..., output=...)` |
| 打分 | `langfuse.score_current_trace(name=..., value=0~1, comment=...)` / `score_current_span(...)` |
| 生成 trace id | `tid = langfuse.create_trace_id()` |
| 刷盘 | `langfuse.flush()`（程序结束前必调） |

## 4. 方式二：OpenTelemetry（OTLP）接入（语言无关）

v4 原生支持 OTel，SDK 底层即基于 OTel：

```bash
OTEL_EXPORTER_OTLP_ENDPOINT = http://8.163.4.42:3001/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS  = Authorization=Basic <base64(pk:sk)>
OTEL_SERVICE_NAME           = nl2sql-agent
```

（已验证该端点可达并接受上报。）

## 5. 方式三：LangChain 集成

```python
from langfuse import Langfuse
langfuse = Langfuse(public_key="pk-...", secret_key="sk-...", host="...")
# 用 langfuse.start_as_current_observation 包裹 chain.invoke(...)，
# 或使用 langfuse.langchain / langfuse.openai 模块（需安装对应包）
```

## 6. 查看与分析（UI 使用）

1. **Traces**：刷新页面 → 应能看到刚上报的 trace（本教程已上报一条 `v4-nl2sql-demo` 供验证）
2. 点开 trace：时间线树（agent → generate-sql → execute-sql），看输入/输出/token/耗时/成本；metadata 里是 SQL 与错误信息
3. **筛选**：按名称/用户/会话/标签/时间
4. **多轮会话**：相同 `trace_id` 自动归组
5. **Scores**：`sql-correctness` 打分显示在 trace 上，Scores 页可汇总统计

## 7. 评估（Evaluations）

- **在线打分**：`score_current_trace(...)`（见上）
- **LLM-as-Judge**（UI 配置）：Evaluations → 新建 → 选模型（需在 Models 配置 OpenAI/DeepSeek API Key）→ 写评估 Prompt → 指定目标分数
- **离线回归**：Datasets 录入 {问题, 预期 SQL} → 批量跑 Agent → 对比打分（NL2SQL 迭代利器）

## 8. 重启方式

> 分两个层面：**应用侧**（NL2SQL Agent 服务）与**平台侧**（自托管 Langfuse 服务）。日常使用（改 prompt / 改代码）只需重启**应用侧**。

### 8.1 应用侧：重启 NL2SQL Agent 服务（让 Langfuse 改动生效）

**何时需要**：在 Langfuse **Prompts** 页修改了 prompt、把某版本打了 `production` 标签，或改了项目代码（如 `langfuse_client.py`、主/子 agent 装配逻辑）后。system prompt 是在服务**启动时**从 Langfuse 拉取并装配进 graph（失败回退本地），不重启则继续用旧版。

**启动命令**（项目根目录 `D:\code_work_space\llm\nl2sql`，必须用项目虚拟环境）：

```bash
# 前台启动（开发/调试，Ctrl+C 停止）
.venv/Scripts/python.exe start_server.py
```

> ⚠️ 不能用 PATH 上的系统 python：可能是旧版，会命中用户 site-packages 的 langfuse 2.x，启动报 `cannot import name 'get_client'`。服务监听 `http://localhost:2026`，`/ok` 为健康检查。

**长跑/生产：分离式启动**（关闭终端、结束 Agent 会话都不杀进程）：

```powershell
# 1) 停旧进程（占用 2026 端口的进程）
$p = (Get-NetTCPConnection -LocalPort 2026 -State Listen).OwningProcess
Stop-Process -Id $p -Force

# 2) UTF-8 编码后台启动（Start-Process 脱离当前会话）
cd D:\code_work_space\llm\nl2sql
$env:PYTHONUTF8 = '1'          # 必须：否则启动打印重定向到文件时按 GBK 解码报错
$env:PYTHONIOENCODING = 'utf-8'
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "start_server.py" `
  -WindowStyle Hidden `
  -RedirectStandardOutput logs/server.out.log `
  -RedirectStandardError  logs/server.err.log
```

> 日志落 `logs\agent-server.log`（INFO 级、自动轮转）。

**验证生效**：

| 检查 | 期望 |
|------|------|
| `http://localhost:2026/ok` | 返回 OK |
| 日志 `logs\agent-server.log` | `[langfuse] prompt main_system_prompt(label=production) v<N> 生效`（N = 实际拉到的版本号）|
| 日志 | `[langfuse] prompt nl2sql_system_prompt(label=production) v<N> 生效`（子 agent 同样打印）|
| 回退场景 | 拉取失败（404 / 网络）时日志打印 `... 本地兜底: ...`，服务照常启动（用本地 prompt）|
| 未生效排查 | 重启后上述 `[langfuse]` 行没出现，先查 `LANGFUSE_ENABLE` / `LANGFUSE_PROMPT_ENABLED` 是否误置 0 |

### 8.2 平台侧：重启自托管 Langfuse（一般不需要）

仅升级镜像或排查平台故障时：

```bash
# SSH 到 8.163.4.42
cd /opt/langfuse
sudo docker compose restart                     # 重启全部 5 个容器
sudo docker compose logs -f langfuse-web-1      # 看 web 启动日志
```

> PostgreSQL 是宿主机 systemd 服务（非容器）：`sudo systemctl restart postgresql`。
> 完整运维命令见 [Langfuse连接说明.md](Langfuse连接说明.md) §8。

## 9. 常见问题排查

| 现象 | 原因/解决 |
|------|-----------|
| UI 里看不到新 trace | 未调用 `flush()`；host/密钥配错；SDK 版本 < 4.x（旧 SDK 走已废弃的接口） |
| 用 `langfuse.trace()` 报错 | v4 SDK 已移除该方法，改用 `start_as_current_observation` |
| trace 有但无 token/成本 | `model` 参数未填或模型未在 Models 中配置单价 |
| 数据量大的延迟 | `batch_size`/`flush_interval` 调优，或提升实例 |
| 想清理测试数据 | Settings → 项目设置删除/清空项目（慎用） |

## 10. 已验证（2026-08-23，v4.16.0）

- 升级迁移完成（v3 数据保留：旧 trace `fb3cec15-...` 仍在）
- SDK 4.14.4 端到端上报：`v4-nl2sql-demo`（AGENT）+ `generate-sql`（GENERATION）+ `execute-sql`（TOOL）+ 打分 `sql-correctness=1` 全部入库（ClickHouse events + scores）
- 打开 UI 的 **Traces** 页即可看到该条记录
