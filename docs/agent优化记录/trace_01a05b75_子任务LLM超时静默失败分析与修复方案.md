# trace_01a05b75 子任务 LLM 超时静默失败分析与修复方案

> 2026-09-01 · 源码级排查 + Langfuse trace 证据
> 会话：`01a05b75-fdf1-7dc2-8252-a32ea00f42de`，trace：`04de75a543ff0d03259d7767d1a70d84`
> 现象：nl2sql 子任务内 LLM 调用 `Request timed out.`，前端无任何失败提示

## 一、核心结论

**后台子任务失败后，失败信息没有以用户可读的方式到达用户。** 三条断链叠加：

1. **错误详情丢失**：`async_tasks[task].error = None`，sync watcher 写终态时没带上 run 的真实错误。
2. **没有失败汇报续跑**：子任务进入 `error` 终态后，没有任何机制触发主 agent 生成一条失败说明消息（主 agent 在启动子任务后 12s 就按 async 协议正常结束了本轮）。
3. **前端聊天区静默**：前端只靠轮询侧边栏渲染一个 ✕ 图标，聊天流里没有任何失败内容；且因为 error=None，连侧边栏详情都是空的。

## 二、事件时间线（全部有 trace/日志证据）

| 时刻 (UTC) | 事件 |
|---|---|
| 05:34:15–05:34:27 | 主 agent 启动 nl2sql 后台子任务后按 async 协议立即结束本轮（AGENT span 仅 12s，lvl=DEFAULT） |
| 05:34:24–05:40:00 | nl2sql_agent 子任务运行（AGENT span，最终 lvl=ERROR） |
| 05:35:56.437 | 子任务内 `model` CHAIN 调用开始（该步为 sql-of-thought 流水线某次推理） |
| 05:36:56 | openai SDK `Retrying request to /chat/completions in 0.46s` |
| 05:37:57 | `Retrying request ... in 0.82s` |
| 05:38:58 | `Retrying request ... in 1.52s` |
| 05:40:00.162 | 第 4 次（共 4 次尝试）也超时 → `openai.APITimeoutError: Request timed out.`，CHAIN latency=243.7s |
| 05:40:00.203 | nl2sql_agent ERROR；主线程 async_tasks 被 sync watcher 写成 `status=error`，`active_queries=False`，**但 error=None** |

证据来源：Langfuse observations（`api.observations.get_many`）+ 容器 `docker logs` 的 openai SDK 重试日志 + 主线程 `GET /threads/{id}/state` 的 async_tasks。

## 三、根因分析

### 3.1 重试机制本身正常（已有）

`src/agent/llms/model.py:392-393` 对全部 provider 统一 `timeout=60, max_retries=3`。`max_retries` 传给 openai SDK 客户端，SDK 对超时/连接错误自动指数退避重试（日志 3 条 `Retrying` 即为证）。**4 次全超时后 APITimeoutError 上抛，子任务 run 失败——这是设计内行为，不是 bug。** 但持续 4 次超时（约 4 分钟）说明是网关侧真实故障，调参只能缓解。

### 3.2 断链 A：错误详情写入丢失

- `sync_subagent_todos.py` 写终态 async_tasks 时未携带 `run["error"]` → `async_tasks[task].error = None`。
- 前端侧边栏即使渲染 `✕ 失败`，也没有具体原因可展示。

### 3.3 断链 B：失败后无主 agent 汇报续跑

- 主 agent 的 AGENT span 05:34:27 就正常结束（launch → 报告 task_id → 停），这是 async_subagents 协议的设计，**正确**。
- 子任务失败后，唯一能"转述"失败给用户的是主 agent 的一次续跑（让 LLM 生成一条失败消息）。但现有 auto-continue 只在任务 **success** 时由前端触发；**error 路径没有任何触发方**（前端不触发，sync watcher 只写状态不触发续跑）。

### 3.4 断链 C：前端无失败消息渲染（次因）

- 聊天区没有「任务执行失败」占位渲染；失败信息只能以侧边栏状态图标存在。

## 四、解决方案

### 方案 1（推荐，后端核心）：子任务失败自动触发主 agent 汇报

在 `sync_subagent_todos.py` 检测到子 run 进入 `error/timeout/cancelled` 终态时，除写 async_tasks 外：

1. **带真实错误**：写 `async_tasks[task].error = run["error"]`（截断 ≤500 字符），修断链 A。
2. **自动续跑**：通过 langgraph SDK `client.runs.create` 在主线程（会话 thread）触发一次续跑，先 `update_state` 注入一条系统消息：
   `[系统自动通知] 子任务 <agent_name> 执行失败：<错误摘要>，请向用户说明失败原因与建议。`
   让主 LLM 生成一条用户可读的失败消息（复用 M-T3d auto-continue 的 trace 归属机制，失败消息落到原 trace）。

**防抖/幂等/安全约束：**
- 每个 task_id 只触发一次（在 async_tasks 记录 `failure_reported=true` 标记）。
- 仅当主线程当前**无 in-flight run** 时才触发（避免打断用户正在进行的其它对话；用 run status 判断，参考 sync 现有 `client.runs.get` 能力）。
- 续跑自身失败静默降级（不级联报错，记日志即可）。
- 注入的消息必须是系统级（对齐现有 auto-continue 通知），避免被主 agent 当用户提问对待。

**工作量**：主要改 `sync_subagent_todos.py`（约 40–80 行）+ 复用现有续跑辅助。

### 方案 2（后端小改）：错误详情透传（修断链 A，独立可先行）

- `_build_check_result` / sync 写终态处，从 `client.runs.get()` 的 `run["error"]` 取真实错误写入 `task["error"]`（截断）。前端侧边栏即可显示具体原因。
- 低风险、可独立上线；方案 1 依赖它提供错误文本。

### 方案 3（前端兜底）：聊天区失败消息 + 侧边栏错误详情

- 轮询到任务进入 `error/timeout/cancelled` 终态时，聊天流插入「任务执行失败」占位（若方案 1 已自动汇报则不重复）。
- 侧边栏失败行 tooltip 展示 `task.error`。
- 涉及 useChat.ts（DLP 加密），需前端侧处理；后端已在 state 提供数据，前端只做渲染。

### 方案 4（可选，韧性）：LLM 超时策略增强

- 调 `model.py` 的 `timeout`/`max_retries`、或对「单 run 内多次超时」降级换 route（如 DeepSeek→Kimi）。对网关真实故障只是缓解，优先级低。

## 五、建议实施顺序

1. **方案 2**（错误透传，小改，立即上）→ 侧边栏至少能看到「APITimeoutError: Request timed out.」
2. **方案 1**（失败自动汇报，核心）→ 聊天区出现用户可读的失败消息，静默失败彻底解决
3. **方案 3**（前端兜底渲染）→ 双保险
4. **方案 4**（韧性）→ 视网关稳定性按需

**实施状态（2026-09-01）**：方案 2/1/3 均已实施并发版 weint（方案 2 commit aebcb38 + 11c48bd；方案 1 commit 48044a2；方案 3 commit d637a03）。方案 3 前端兜底：轮询到子任务进 error/timeout/cancelled 终态且 35s（> 方案 1 最坏等主线程空闲 30s）内 `failure_reported` 仍为假 → 聊天流渲染红色失败卡（标题 + error 详情 + 重试建议），interrupted 不兜底。**方案 4 取消**（用户 2026-09-01 明确「降级路由不用实现」）。

## 六、关联

- 前端侧边栏状态渲染：`harness-deep-agents-ui/src/app/components/ChatInterface.tsx`（✓ 成功 / ✕ 失败 / ⊘ 取消·超时·中断）
- auto-continue 机制：M-T3d（复用原 trace）、`thinking_toggle.py` P1-10（模型继承）
- sync watcher：`src/agent/subagents/sync_subagent_todos.py`
- 关联（不属本方案）：`check_progress._build_check_result` 把 `interrupted` 一律当 awaiting_user_approval，与「停止的子任务被新问题重启」bug（已暂停排查）相关
