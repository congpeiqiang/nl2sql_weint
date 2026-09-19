# Langfuse 功能测试与验证指南

> 对应实现：`docs/langfuse平台/Langfuse接入实现方案.md` 的 M1–M5 五阶段（监控 → 监控增强 → 评估 → 版本管理 → 灰度+发版）
> 适用环境：Langfuse Cloud（`LANGFUSE_BASE_URL` 指向 cloud.langfuse.com，密钥在项目根 `.env`）
> 更新时间：2026-08-23
> 验证基线：M1–M5 均已实施并实测通过（chinook_aliyun 数据库）

本文档是「上线后必做」的验收手册：每个功能点给出**怎么测**（具体命令/操作）和**怎么验**（在 UI / API / 日志里应看到什么）。全部命令在项目根 `d:\code_work_space\llm\nl2sql` 下执行，Python 用 `.venv\Scripts\python.exe`。

---

## 0. 测试环境准备

### 0.1 前置配置（项目根 `.env`）

| 变量 | 说明 |
|------|------|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Cloud 项目密钥（务必保密，勿外泄） |
| `LANGFUSE_BASE_URL` | Cloud 地址（`https://cloud.langfuse.com`） |
| `LANGFUSE_ENABLE=true` | 总开关（默认 true；false 一键停用所有运行时埋点/打分/prompt 拉取，见 §8） |
| `LANGFUSE_PROMPT_ENABLED=1` | M4 开关（1 走 Langfuse 拉 prompt；0/false 强制本地文件） |
| `LANGFUSE_RELEASE="v0.5.0-m5"` | 发版标记，注入 trace 的 release 属性（见 §5.2） |
| `LANGFUSE_PROMPT_LABEL` / `LANGFUSE_CANARY_LABEL` / `LANGFUSE_CANARY_RATIO` | M5 A/B 分流（默认注释，见 §5.1） |

> 密钥值在 `.env` 第 28–29 行，测试时勿打印到终端/日志/截图。

### 0.2 测试数据库

- **chinook_aliyun**（阿里云 PostgreSQL，无需 VPN）。所有验证查询一律用它。
- 经典冒烟问题：`统计 chinook_aliyun 数据库 artist 表有多少艺术家`（期望 artist 表 275 行）。

### 0.3 服务器启动/重启流程

```bash
# 1) 杀掉旧进程（先查 PID）
netstat -ano | grep ":2026" | grep LISTEN
taskkill //F //PID <pid>

# 2) 重新启动（分离进程，日志重定向）
powershell -NoProfile -Command "\$env:PYTHONUTF8='1'; \$env:PYTHONIOENCODING='utf-8'; \$env:NL2SQL_EVAL_JUDGE_SAMPLE='1.0'; Start-Process -FilePath 'D:\code_work_space\llm\nl2sql\.venv\Scripts\python.exe' -ArgumentList 'start_server.py' -WorkingDirectory 'D:\code_work_space\llm\nl2sql' -RedirectStandardOutput 'D:\code_work_space\llm\nl2sql\server_test.out.log' -RedirectStandardError 'D:\code_work_space\llm\nl2sql\server_test.err.log' -WindowStyle Hidden"

# 3) 等待 ~60–85s，确认端口起来
sleep 60 && netstat -ano | grep ":2026" | grep LISTEN
```

> ⚠ 每次**改代码后必须重启**才生效（prompt 装配 / 中间件 / A/B 分流都是进程级/启动时决定）。

### 0.4 UI 登录与项目

浏览器打开 `https://cloud.langfuse.com` → 选择本项目 → 顶部可切换：
**Traces**（trace 时间线）、**Scores**（打分）、**Datasets**（BadCase 集）、**Prompts**（版本管理）、**Settings → API Keys**（密钥）。

### 0.5 验证脚本目录

`d:/tmp/` 下已有历次验收脚本（`langfuse_m1_smoke.py`、`langfuse_m2_accept.py`、`lf_m3_accept.py`、`lf_m4_verify.py`、`lf_m5_verify.py`、`lf_query.py` 等）。运行示例：

```bash
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe /d/tmp/lf_m5_verify.py 15
# 参数 = 扫最近 N 分钟 traces（默认 15）
```

---

## 1. M1 全链路监控 — 测试与验证

**功能点**：主/子 graph 挂 `get_langfuse_callbacks()`；启动预检 `auth_check()`；主 trace + 子 trace + 工具 trace + 模型 trace 自动生成。

### 1.1 启动预检

```bash
# 启动后看日志
grep "云端连通\|auth_check\|Langfuse" server_test.out.log | head
```

**验证**：应出现 `✅ Langfuse: 云端连通`（auth_check 通过）。若 `.env` 密钥错或断网，此处告警但不阻断启动（监控是旁路）。

### 1.2 发一条真实查询

```bash
# 通过前端 UI 发，或 API（assistant_id 用 chat_agent，不是 default）：
curl -s -X POST http://localhost:2026/threads -H "Content-Type: application/json" -d '{"metadata":{"title":"m1-test"}}'
# 拿到 thread_id 后：
curl -s -X POST http://localhost:2026/threads/<tid>/runs -H "Content-Type: application/json" \
  -d '{"assistant_id":"chat_agent","input":{"messages":[{"role":"user","content":"统计 chinook_aliyun 数据库 artist 表有多少艺术家"}]},"config":{"recursion_limit":500}}'
```

### 1.3 验证（UI Traces + API）

```bash
# 用脚本扫最近 traces（看主/子 trace 结构）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe /d/tmp/langfuse_verify_trace.py  # 或 lf_query.py
```

**验收清单**：

| 项 | 预期 |
|----|------|
| 主 trace | 名 `chat_agent`，status `success` |
| 子 trace | 名 `nl2sql_agent`，同 session，success |
| 工具 trace | `wrenai_*` / `dbmcp_*` 等，含输入输出 |
| 模型 trace | `ChatDeepSeek`，含 token / 耗时 / 成本 |
| 结果正确 | artist 表 275 行，答案正确 |

### 1.4 一键关停验证（LANGFUSE_ENABLE=false）

```bash
# 在 .env 加 LANGFUSE_ENABLE=false → 重启 → 再发一条查询
# 应看不到新 trace；同时 auth_check 预检被跳过、get_langfuse_callbacks 返回空、打分/prompt 全旁路
```

**验证**：近 N 分钟 trace 数不增长；启动日志无 `✅ Langfuse: 云端连通`。测完改回 `true` 并重启。

---

## 2. M2 监控增强 — 测试与验证

**功能点**：请求级 metadata 注入（session/trace_name/tags/workspace/skills/db_name）；session 分组；`skill:{skill}:{tool}` span；VFS 真实路径。

### 2.1 请求级 metadata 注入

发一条查询（同 §1.2），**必须走 HTTP run 端点**（注入发生在 `LangfuseMetadataMiddleware`，见 `src/api/langfuse_metadata.py`）。

**验证**：UI 打开主 trace → 看 **metadata** 应有：

```json
{
  "langfuse_session_id": "<tid>",
  "langfuse_trace_name": "query:<tid>",
  "langfuse_tags": ["nl2sql"],
  "workspace": {"name": "...", "path": "..."},
  "skills": [{"name": "...", "version": "0.1.0"}],
  "db_name": "chinook_aliyun",
  "prompt": {"prompt_label": "production", "prompt_version": 1},   // M5 注入
  "langfuse_release": "v0.5.0-m5"                                   // M5 注入
}
```

### 2.2 session 分组验证

```bash
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe /d/tmp/langfuse_m2_accept.py
```

**验证**：同一次查询，`query:{tid}`（主）+ `nl2sql-agent:{tid}`（子）+ `skill:*` span 的 **session_id 全部相同**，且 `tags=['nl2sql']`；多轮同线程问题归到同一会话视图。

### 2.3 Skill span + VFS 真实路径

发一个会触发 Skill（如写报表/文档）的查询，UI 看子 span 名 `skill:{skill}:{tool}`；`write_file` 类工具 span 的 **metadata.vfs_path** 应是真实落盘路径（`large_tool_results/` 或 `nl2sql_process_data/` 下），而非临时目录。

> 相关验证脚本：`d:/tmp/lf_vfs_direct.py`（强制 write_file 验证 vfs_path）。

---

## 3. M3 评估 — 测试与验证

**功能点**：五维评分（`sql_valid_score` / `schema_match_score` / `sql_exec_success` 确定性同步写 + `sql_biz_correct_score` / `report_table_score` / `analysis_report_score` LLM-judge 抽样）；用户反馈 `user-feedback`；BadCase 每日采集。

### 3.1 确定性三维 + LLM-judge 抽样

```bash
# 默认 judge 采样 NL2SQL_EVAL_JUDGE_SAMPLE=0.3；要全量 judge 就设 1.0（改 .env 或重启时注入）
# 发几条查询后：
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe /d/tmp/lf_m3_accept.py
```

**验证**：
- **Scores 页**按 trace 应能看到确定性三维（`sql_valid_score` / `schema_match_score` / `sql_exec_success`，值 0~1）。
- 若 `NL2SQL_EVAL_JUDGE_SAMPLE=1.0`，还应出现 `sql_biz_correct_score`（正确查询应 ≥0.9；单复数/表名归一化场景不会误判 0，见 §9 坑）。
- 报告类查询（写 `.md`/`.txt`）触发 `report_table_score` / `analysis_report_score`。

### 3.2 用户反馈

前端「👍/👎」按钮 → 后端 `put_feedback` 写 `user-feedback` score。

**验证**：点差评后，Scores 页该 trace 出现 `user-feedback=0`（好评=1），comment 带原文；按 session 查主 trace 可对上。

### 3.3 BadCase 采集

```bash
# 手动跑一次（进程外，日志追加到根目录）
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 1
# 重扫历史并重放（忽略去重 stamp）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 30 --force
```

**验证**：
- 日志打印 `扫描近 N 天 traces: X 条` 与 `完成：本次新采集 Y 条 BadCase → Dataset:badcase`。
- UI **Datasets → badcase** 应出现条目：`input.question`=问题原文，`metadata.reasons` 列出命中维度（如 `sql_valid_score=0.30` / `trace_error` / `user_feedback=0`），`metadata.source_trace_id` 链回原始 trace。
- 去重：同一条 trace 重跑不重复入集（除非 `--force`）。

---

## 4. M4 版本管理 — 测试与验证

**功能点**：主/子 system prompt 从 Langfuse 拉取（`get_prompt_text`，失败回退本地）；`sync_prompts.py` 本地→云端单向同步；标签切换即时回滚；`version: 0.1.0` 写入 trace 的 skills。

### 4.1 初始化同步

```bash
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts --all
# 输出：✓ main_system_prompt v1 labels=['latest','production'] ...  /  ✓ nl2sql_system_prompt v1 ...
# 内容未变会跳过：· main_system_prompt 与 production 内容一致，跳过（--force 可强制新版本）
```

**验证**：UI **Prompts** 页出现 `main_system_prompt` / `nl2sql_system_prompt` 两个 text 型 prompt，`production` 标签指向当前版本。

### 4.2 改版生效（版本号在启动日志）

```bash
# 在 Langfuse UI 编辑 prompt 保存 → 重启服务器
# 启动日志应打印（v 随版本递增）：
grep "prompt" server_test.out.log
# [langfuse] prompt main_system_prompt(label=production) v2 生效
# [langfuse] prompt nl2sql_system_prompt(label=production) v2 生效
```

**验证**：日志版本号 = UI 上的版本号，即装配走的是 Langfuse 正文。再发查询，trace 的 `metadata.prompt.prompt_version` 应同步为 v2。

### 4.3 回滚演练（标签切换）

```bash
# 用脚本/CLI 把 production 标签切回旧版本 v1（update_prompt_labels）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe /d/tmp/lf_m4_switch_check.py
# 或直接调用 langfuse_client.update_prompt_labels(name, version=1, new_labels=["production"])
```

**验证**：重启后启动日志回到 `v1 生效`；UI Prompts 页 `production` 标签指回 v1（`latest` 仍指最新版本，不受影响）。

### 4.4 强制本地（LANGFUSE_PROMPT_ENABLED=false）

`.env` 置 `LANGFUSE_PROMPT_ENABLED=0` → 重启 → 装配用本地 `src/agent/prompt/*.md`（日志不出现 `vN 生效`，或提示回退本地）。这是第二层回滚手段。

### 4.5 兜底加固验证（2026-08-24 评估落地）

验证点（均用 `PYTHONPATH=src` + 项目 venv；Langfuse 云项目当前处于 **v4 events_only**，prompts 读不到——恰好可直接验回退路径）：

1. **max_retries=0**：`get_prompt_text`/`get_prompt_version` 拉取失败时**只重试 0 次**（SDK 不再内层重试），单次超时 main=3s / skill 版本=2s 即回退。日志看「回退本地」，不再有 SDK 的 `Retrying...`。
2. **内容校验**：正常语言下缺占位符即回退。验法：`get_prompt_text("main_system_prompt", fallback="FB", required_markers=["{{CHART_SPEC}}","{{CHART_ENGINE_NAME}}","{{CHART_OUTPUT_FORMAT}}"], min_chars=100)` 应返回 `FB` 且日志「内容校验不过」；`min_chars=999999` 同理（过短）。sync_prompts 对比原文时不传校验参数，不受影响。
3. **404 分级日志**：prompt 不存在时 ERROR 级「不存在(404)…请检查 Prompt 配置」；网络故障是 WARNING「拉取失败」。验法：`get_prompt_text("nonexistent_prompt_xyz", fallback="FB404")` → 返回 `FB404` + ERROR 日志。
4. **source 标记**：`prompt_label_info()` 返回 `source`。prompts 拉不到/强制本地 → `local`；全部拉到 → `langfuse`；主/子来源不一致 → `mixed`。trace `metadata.prompt` 同步带 `source` 字段。

> 注：happy path（source=langfuse）需项目离开 v4 events_only、prompts 恢复可见后才能复验；当前回退路径本身即「兼容无 Langfuse」的验收证据。

---

## 5. M5 灰度 + 发版闭环 — 测试与验证

**功能点**：进程级 A/B 分流 `resolve_prompt_label()`；trace 注入 `metadata.prompt` + `langfuse_release` + `client._release`；离线 A/B 门禁 `run_experiment.py`；每日 BadCase cron。

### 5.1 A/B 分流 resolver（三级）

| 场景 | 配置 | 期望 label |
|------|------|-----------|
| 默认 | 无 canary 配置 | `production` |
| 显式指定 | `LANGFUSE_PROMPT_LABEL=prod-b` | `prod-b`（run_experiment A/B 用） |
| canary 掷骰 | `LANGFUSE_CANARY_LABEL=prod-b` + `LANGFUSE_CANARY_RATIO=0.1` | 约 10% 进程走 `prod-b`，其余 `production` |

**验证**：重启服务器，启动日志打印 `[langfuse] prompt label=production（A/B 分流）`（或对应 label）。因 `_CANARY_RESOLVED` 在 import 时只掷一次，**同一进程内恒为同一 label**。

### 5.2 release + prompt 可见性

发一条 HTTP 查询 → trace 应带：
- **Release 页**：`v0.5.0-m5`（来自 `client._release`，trace 统一取客户端全局 release）；
- **metadata**：`prompt={prompt_label, prompt_version}` 与 `langfuse_release=v0.5.0-m5`（来自中间件注入）。

用 `lf_m5_verify.py` 扫：每行应显示 `release=v0.5.0-m5` + `prompt={'prompt_label': 'production', 'prompt_version': 1}`。

### 5.3 离线 A/B 实验 + 回归门禁

```bash
# 准备查询集（JSON 数组，db_name 用 chinook_aliyun）
# eval/queries/regression.json 示例：
# [{"question": "统计 chinook_aliyun 数据库 artist 表有多少艺术家", "db_name": "chinook_aliyun"}]

# 跑 A/B 对比 + 门禁（production 为 reference，prod-a 为 candidate；--judge 追加 LLM-judge）
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=src \
  .venv/Scripts/python.exe -m agent.eval.run_experiment \
  --queries eval/queries/regression.json --labels production prod-a --threshold 0.05 --judge

# 只跑单 label（canary 预检，不做门禁）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
  --queries eval/queries/regression.json --labels prod-a
```

**验证**：
- orchestrator 打印各 label 聚合：`{count, sql_biz_correct_score, sql_valid_score, sql_exec_success, schema_match_score, no_sql_ratio, exec_fail_ratio}`；
- 门禁通过：`✅ production vs prod-a：无回归，门禁通过`，**exit 0**；
- 有回归：打印 `❌ sql_valid_score: 0.9 → 0.7（掉 0.2 > 阈值 0.05）`，**exit 1**（CI 会拦下发版）；
- manifest 落盘：`{active_workspace}/eval/experiment_runs/run_<stamp>.json`；
- 实验 trace 隔离：Langfuse 按 `tags=["nl2sql","experiment"]` + `session=exp:{label}:{run_id}` 可过滤，不污染生产会话视图。

> 若 `prod-a` label 在 Langfuse 上不存在，`get_prompt_text` 404 会自动回退本地（不崩），跑出的就是基线内容——**mint 新 label 用 `sync_prompts --label prod-a --force`**（sync 的跳过逻辑只比 production，不 force 不会新建）。

### 5.4 每日 BadCase cron

```bash
# 注册任务计划（一次性）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_collect_badcase_task.ps1
# 查看任务
powershell -NoProfile -Command "Get-ScheduledTask -TaskName 'nl2sql-collect-badcase' | Select-Object TaskName, State"
```

**验证**：任务 `State=Ready`，每天 **02:13** 进程外执行 `daily_collect_badcase.ps1` → 日志追加到根目录 `server_collect_badcase.log`（含 `[时间] begin` / 采集输出 / `exit=0`）。进程外启动避免随 Claude 会话被杀。

> 手动触发一次：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily_collect_badcase.ps1`，然后 `cat server_collect_badcase.log` 确认链路。

---

## 6. M6 Skill 资产管理 — 测试与验证

### 6.1 同步：SKILL.md → Langfuse（`sync_prompts --skills`）

```bash
# 必须用项目 venv 解释器——PATH 上的系统 python 会命中用户 site-packages 旧版
# langfuse 2.x（`from langfuse import get_client` 报 cannot import name）。
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts --skills
```

**预期**：
- 先扫出 `shared_skills_dir` 下 SKILL.md 数量（当前 15 个）；
- 首次同步逐个输出 `✓ skill/{group}/{skill_dir} v1 labels=['latest', 'production'] chars=N`；
- 重跑幂等：`· skill/... 与 production 内容一致，跳过（--force 可强制新版本）`。

**验证 Langfuse 侧**：Prompts 页应出现 15 个 `skill/*` prompt 全部 v1 + `latest/production`；API 列 prompt 见 §10 脚本逻辑（`/d/tmp/lf_m6_verify.py` 里 `lf.api.prompts.list(limit=100)` 过滤 `skill/` 前缀，注意 `name=` 参数是精确匹配不能当前缀用）。

### 6.2 trace 元数据验证（metadata.skills 带云端版本 + source）

跑一条真实查询（同 §1.2），然后在 Langfuse 查该 session 的 trace（`lf.api.trace.list(session_id=tid)`）。

**预期**：
- 主 trace `query:{tid}` 与子 trace `nl2sql-agent:{tid}` 的 `metadata.skills` 均为 15 条：
  `{name, path, version, source}`，其中 `source='langfuse'`、`version=1`（云端整数版本号）。
- `release` / `metadata.prompt={production, vN}` 不受影响。
- skill span（`skill:schema-linking` 等工具观察）不注入 metadata.skills —— 既有已知限制，主/子 trace 已够回溯。

### 6.3 skill 灰度 / 回滚演练（复用 M5 label 机制）

1. **改版**：Langfuse UI 打开某个 `skill/*` prompt 编辑保存 → 新版本 v2。
2. **发布**：重跑 `sync_prompts --skills` 把本地 SKILL.md 推为 v2 打 production（或直接在 UI 给 v2 打 `production` 标签）→ 重启服务。
3. **回滚**：`update_prompt_labels(name='skill/nl2sql/nl2sql-sql-generation', version=1, new_labels=['production'])` → 重启 → trace 里该 skill 的 `version` 回到 1（labels 跨版本唯一，切回旧版时新版 production 自动移除）。
4. **A/B**：`sync_prompts --skills --label staging` 只打 `staging+latest`（production 不动），服务端 `LANGFUSE_PROMPT_LABEL=staging`（或 canary 掷骰）让部分实例走新 skill 指令——与 system prompt 同 label（`get_prompt_version` 缺省走 `resolve_prompt_label`），切换后 trace `metadata.skills` 版本变化可见。

### 6.4 回退本地验证（source=local）

- 前置：`sync_prompts --skills` 已同步 → trace 里该 skill `source='langfuse'`。
- 关掉：`.env` 加 `LANGFUSE_PROMPT_ENABLED=0` → 重启。
- **预期**：新 trace 的 `metadata.skills` 该 skill `source='local'`（`get_prompt_version` 返回 None → 回退本地 frontmatter 版本），运行时不受影响（deepagents 始终读本地 SKILL.md）。

---

## 7. M7 反馈闭环 — 测试与验证

### 7.1 BadCase 携带 db_name

```bash
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 1
```

**预期**：新采集的 BadCase item `metadata.db_name` 与源 trace 的 `trace.metadata.db_name` 一致（如 `chinook_aliyun`）；历史 item（本改动前）缺省。

**API 查证**：`client.api.dataset_items.list(dataset_name='badcase', limit=...)` 看每个 item 的 `metadata.db_name`。

### 7.2 真实反馈门禁（feedback_gate）

```bash
# 只看报表（不门禁，exit 恒 0）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.feedback_gate --days 7 --report-only

# 门禁：ref=production vs cand=prod-a（默认，缺省参数）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.feedback_gate --days 7

# 数据不足按失败处理（发版场景强制门槛）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.feedback_gate --days 7 --fail-insufficient
```

**预期**：
- 输出各组好评率表（`rated/pos/rate`）+ 最近差评明细（comment/time/trace）；manifest 落 `{active_workspace}/eval/feedback_gates/feedback_gate_{stamp}.json`。
- 分组按 trace `metadata.prompt.prompt_label`；M5 元数据上线前的旧 trace 归类 unknown（单列不参与门禁）。
- 退出码：数据不足默认 **0** 跳过、`--fail-insufficient` **2**、`--report-only` 恒 0、好评率回退 **1**。
- 回归场景：让 `--ref`/`--cand` 指向有数据的两个组（如先攒 production 差评后对比），`candidate < reference − threshold` → exit 1。

### 7.3 BadCase 回灌离线回归（run_experiment --from-badcase）

```bash
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
  --from-badcase --labels production prod-a --threshold 0.05 [--from-badcase-limit 20]
```

**预期**：日志 `Dataset:badcase 装载 N 条查询` → 按 `(question, db_name)` 去重后落临时查询集（`.tmp/experiment/queries_{stamp}.json`）→ 走与 `--queries` 完全相同的 worker 回归门禁；manifest 落 `{active_workspace}/eval/experiment_runs/`。

### 7.4 每日 cron 顺序

手动跑 `scripts/daily_collect_badcase.ps1`，`server_collect_badcase.log` 应有：
`[stamp] collect_badcase exit=0` → `[stamp] feedback_gate exit=0`（数据不足时 feedback_gate 也是 0）。

---

## 8. 总开关与回滚矩阵

| 配置 | 停用范围 | 验证方式 |
|------|----------|----------|
| `LANGFUSE_ENABLE=false` | **一切**运行时埋点/打分/prompt 拉取（最高层回滚） | 发查询无新 trace；`get_client()` 仍可用（collect_badcase 仍能读历史） |
| `LANGFUSE_PROMPT_ENABLED=0` | 仅 prompt/skill 版本拉取（装配回退本地文件；`metadata.skills` 的 source 变 local） | 重启后启动日志无 `vN 生效` |
| 清空 `LANGFUSE_CANARY_*` | 停掉 canary 分流 → 全量 `production` | 重启后日志 `prompt label=production` |

**回滚演练标准流程**（M4/M5/M6 均验证过）：
1. 改坏 → 切 `LANGFUSE_PROMPT_LABEL` 回 `production`（或清 canary 环境变量）→ 重启；
2. 仍不放心 → `LANGFUSE_PROMPT_ENABLED=0` 强制本地 → 重启；
3. 彻底停 → `LANGFUSE_ENABLE=false` → 重启。

---

## 9. 常见验证坑（实测踩过）

| 现象 | 原因/规避 |
|------|-----------|
| `trace.list` 只返回 ≤100 条 | Langfuse API 单页上限 100，分页或缩短时间窗 |
| 主 trace input 只有用户消息、model CHAIN input 为空 | deepagents 栈下 system prompt 正文不进 trace——**生效证据看启动日志版本号 + get_prompt 内容** |
| LLM-judge 把正确查询判 0 分 | 表名单复数（`artists` vs `artist`）——rubric 已加「NL 名可归一化、以结果回答意图为准」，复查时看 comment |
| 进程内 invoke 工具为 0 个（`Cannot run the event loop`） | MCP `_load_mcp_servers` 自建 event loop——graph 必须**顶层 import**（asyncio.run 之外），run_experiment worker 即此模式 |
| `span.start_observation(trace_context=...)` 的 session 不生效 | 4.14.4 只认 trace_id/parent_span_id——session 分组走 `span._otel_span.set_attribute("session.id", tid)` |
| trace 无 release | `client._release` 在 get_client() 惰性创建时注入；重启后看 Langfuse 是否仍带旧值 |
| Windows 控制台 GBK 崩 | 所有脚本开头 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；bash 里跑 powershell 的 `$env:` 要转义成 `\$env:` |
| 切库仍连旧库 | 消息级注入【当前数据库】；测试用 `chinook_aliyun`（无需 VPN） |
| cron 不触发 | Windows 任务计划需 `Register-ScheduledTask`（schtasks /TR 空格会碎）；`State=Ready` 但日志无输出 → 手动跑 `daily_collect_badcase.ps1` 看报错 |
| feedback_gate 好评率重复计数 | 同一 trace 可能多次打分（点赞后又存评论各写一条 score）——必须按 trace 取最新去重（脚本已做） |
| feedback_gate 退出码取错 | bash 管道 `cmd \| tail` 后 `$?` 是 tail 的退出码——直接跑 python 或 `PIPESTATUS` 取真实值 |
| 反馈数据稀疏不触发门禁 | `min_rated` 门槛（默认 5）未达时默认 exit 0 跳过——放量/发版场景用 `--fail-insufficient` 强制按失败处理 |
| 旧 trace 无 prompt_label 归类 unknown | M5 元数据中间件上线前的 trace 没有 `metadata.prompt`——feedback_gate 单列 unknown 不参与门禁，放量判断只看新流量 |

---

## 10. 回归冒烟清单（一次完整验收）

按顺序跑一遍即可覆盖 M1–M7：

```bash
# 1. 重启服务器，确认启动日志：
#    ✅ Langfuse: 云端连通
#    [langfuse] prompt label=production（A/B 分流）
#    [langfuse] prompt main_system_prompt(label=production) vN 生效
#    [langfuse] prompt nl2sql_system_prompt(label=production) vN 生效
#    [skill_manifest] built N enriched skills（M6：N=当前 SKILL.md 数）

# 2. 发 2~3 条 chinook_aliyun 查询（含 1 条报错场景制造 BadCase）

# 3. 扫 traces 确认结构 + release + prompt
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe /d/tmp/lf_m5_verify.py 15

# 4. 确认 Scores 三维 + user-feedback
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe /d/tmp/lf_m3_accept.py

# 5. A/B 门禁跑分
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
  --queries eval/queries/regression.json --labels production prod-a --threshold 0.05

# 6. BadCase 入库（新 item 带 db_name，见 §7.1）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 1

# 7. M7 真实反馈门禁报表（exit 恒 0）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.feedback_gate --days 7 --report-only

# 8. M7 BadCase 回灌离线回归（--from-badcase 等价于 --queries 的回归门禁）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.eval.run_experiment \
  --from-badcase --labels production prod-a --threshold 0.05 --from-badcase-limit 5

# 9. 每日 cron 完整顺序（collect_badcase + feedback_gate 写 server_collect_badcase.log）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/daily_collect_badcase.ps1

# 7. M6：skill 同步 + trace 元数据（§6）
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts --skills
#    （重跑应全跳过，说明幂等）→ 再发 1 条查询，Langfuse 主/子 trace 的
#    metadata.skills 应 15 条全 source=langfuse + version=1（见 §6.2）。
```

全部通过 = 本阶段 Langfuse 功能验收完成。
