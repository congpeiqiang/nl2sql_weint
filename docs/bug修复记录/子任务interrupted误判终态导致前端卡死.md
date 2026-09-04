# 子任务 transient「interrupted」被误判为终态，导致前端永久卡在「澄清判断」

**日期**：2026-09-04
**类型**：状态机缺陷（中断状态误判）
**严重程度**：高（查询实际已成功产出答案，但前端永久卡死、结果不落主线程）

---

## 一、问题描述

会话 `01a06af3-99ec-7900-85ec-cea62e9488a8`（问题「上周都有谁没有报工」）前端长时间停留在子 agent 的「澄清判断」步骤，无审批卡、无失败提示、无最终结果，永久卡死。

### 复现条件

- 主 agent 委派 nl2sql 子任务（`start_async_task`）后，主线程正常结束（主 run `success`，末句「查询已提交，请稍候 ⏳」）
- 子 agent 在「口径与数据校验」阶段大量跑 `run_sql`、上下文膨胀（~101 条消息）触发 deepagents 自动压缩（summarization），子 run **短暂进入 `interrupted` 状态**

---

## 二、根因分析

### 关键事实：子 run 其实已经跑完

通过后端 checkpoint 实测，子 run 的最终状态是 **`success`**，不是 stuck：

| 时刻 | 事件 |
|---|---|
| 05:45:49 | 子 run 创建（run_id `01a06af3-ace8-7393-9601-0b800ac0c952`） |
| ~05:55:55 | 子 run 短暂进入 **`interrupted`**（约 101 条消息，state 含 `_summarization_event`） |
| 05:56:32 | 同步器检测到 `interrupted`，当作**终态**写 `async_tasks[task].status="interrupted"`，**停止监听** |
| 05:58:44 | 子 run 恢复并 **`success` 完成**（113 条消息，6 个 sub-todo 全部 completed，消息 `[112]` 产出完整答案：应报工 189 / 已报工 176 / **13 人无报工记录**） |

而主线程 state 冻结在 `interrupted`：
- `async_tasks[task].status = "interrupted"`（05:56:32 之后再没更新）
- `subagent_steps_map = {}`（同步器从没写过进度映射）

### 三处「interrupted 是终态」的陈旧假设叠加

1. **同步器把 `interrupted` 当终态** —— `sync_subagent_todos.py:66`
   ```python
   _RUN_DONE_STATUSES = ("success", "error", "cancelled", "timeout", "interrupted")
   ```
   配合 `sync_subagent_todos.py:441` `if run_status in _RUN_DONE_STATUSES: sub_agent_done = True`，
   一看到 `interrupted` 就退出循环，再也听不到后面的 `success`。

2. **check_async_task 仍把 `interrupted` 解读成「等待 SQL 审批」** —— `check_progress.py:563-572`
   ```python
   elif run["status"] == "interrupted":
       result["awaiting_user_approval"] = True   # 旧 HITL 残留
   ```

3. **前端把 `interrupted` 当终态、且不兜底** —— `ChatInterface.tsx:342` 的 `TERMINAL` 集合含
   `interrupted`，同时 `ChatInterface.tsx:68` 明确「interrupted（HITL 审批暂停）非失败终态，不兜底」。

### 背景：`interrupted` 已变成「幽灵状态」

这三处假设都来自 2026-08-28 **之前**的 SQL 审批 HITL 闸门。而 `middlewares/sql_approval.py:1-15`
已把审批闸门升级为**只读硬拦截**——写/DDL 直接 `status="error"` 拒绝，**不再 raise 任何 interrupt**
（整个 `src/agent` 与 deepagents 包内均无 `interrupt()`/`GraphInterrupt` 调用）。

因此现在的 `interrupted`：
- 不是 SQL 审批（无 `action_requests`，同步器 `_extract_approval_payload` 返回 None）
- 而是一个**会自己恢复的短暂暂停**（大概率是 deepagents 上下文压缩 summarization 的瞬时态）

旧代码注释（`sync_subagent_todos.py:76-80`）本意是「`interrupted` = 暂停等用户、不是终态」，
但同一份代码里又把它塞进了 `_RUN_DONE_STATUSES`（终态集）——二者自相矛盾，正是这个矛盾在
审批闸门移除后被放大成「误判终态」。

---

## 三、解决方案

### 方案 1（P0，治本）：同步器不再把 `interrupted` 当终态

**文件**：`src/agent/subagents/sync_subagent_todos.py`

- `:66` 从 `_RUN_DONE_STATUSES` 里删掉 `"interrupted"`（`→ ("success","error","cancelled","timeout")`）
- `:73` 顺带删掉 `"interrupted": "已中断"`
- 新增独立的 `INTERRUPTED_STUCK_TIMEOUT = 120` + `interrupted_since` 计时器（见下方说明），
  防「永远 interrupted 不恢复」时 0.5s 轮询空转：停在 `interrupted` 超 2min 才按 timeout 收尾

> ⚠️ **对原建议的修正**：原建议是 `if run_status == "running"` 改成
> `if run_status in ("running", "interrupted")`（复用 `STALE_RUN_TIMEOUT`）。但 `STALE_RUN_TIMEOUT`
> 用的是 `loop_start`（**任务总时长**），若照搬会误杀「第 10 分钟才短暂 interrupted、随后正常完成」
> 的合法长查询（本案例正是 05:45:49 起、约 10 分钟处进入 interrupted、13 分钟才 success）。
> 故改用**独立计时器**：只按「停在 interrupted 的时长」判定，与任务总时长解耦。

**效果**：watcher 继续 0.5s 轮询；子 run 恢复成 `success` 后即把结果/进度正确回传主线程，
前端随之刷新，此类会话自动恢复。

### 方案 2（P1）：清理 check_async_task 残留分支

**文件**：`src/agent/subagents/check_progress.py:563-572`

`interrupted → awaiting_user_approval` 分支改为把 `result["status"]` 归一为 `"running"`，并附
note「短暂暂停、稍后重查、勿取消/勿重新委派」。这样主 agent 与 `async_tasks.status` 都视其为
「进行中」、继续轮询 `check_async_task`，而非误判「等 SQL 审批」。

### 方案 3（P1）：前端把 `interrupted` 从终态移除

**文件**：`harness-deep-agents-ui/src/app/components/ChatInterface.tsx`

- `:342` `TERMINAL` 集合去掉 `"interrupted"`（不再把瞬时暂停当终态）
- **不**纳入失败兜底 `FAIL_TERMINAL_STATUSES`：interrupted 是自恢复的瞬时暂停而非失败，
  兜底反而会把正常暂停误渲染成「失败占位卡」。后端 sync watcher 持续轮询到真实终态即兜底。

### 即时处理（无需改代码）

该会话的答案其实已在子线程消息 `[112]`（13 人无报工名单 + 176/189 统计）。可：

1. 直接从子线程 `01a06af3-ace5-71c1-87dd-7567055a79de` 的 state 取回最后一条 AI 答案给用户；或
2. 重启后端，让 sync watcher 重新拉起后按真实 run 状态（`success`）回填。

---

## 四、相关文件

| 文件 | 说明 |
|------|------|
| `src/agent/subagents/sync_subagent_todos.py` | 同步器主循环；`_RUN_DONE_STATUSES`（:66）、终态判断（:441）、超时兜底（:421） |
| `src/agent/subagents/check_progress.py` | `_enhanced_build_check_result` 的 `interrupted` 分支（:563-572） |
| `src/agent/middlewares/sql_approval.py` | 只读硬拦截（2026-08-28 升级，已移除审批 interrupt） |
| `harness-deep-agents-ui/src/app/components/ChatInterface.tsx` | 前端 `TERMINAL` 集合（:342）、失败兜底（:68 注释） |

---

## 五、次要待查项

`interrupted` 的**确切触发源**未 100% 锁定（需后端日志或 Langfuse trace）。相关 trace
`1848ab842c386f6a761b28f8fb36a03a` 在 Langfuse project `cmtkuc1za000bnw07w0i5a3zg`，而文档
`docs/weint环境/langfuse-连接信息.md` 里的 key 是另一个 project `proj_6nMyvFmN`，用文档 key 查
该 trace 会报「not found within authorized project」。若要钉死，需用 app 实际写入的 `.env.prod`
中 `LANGFUSE_PUBLIC_KEY/SECRET_KEY`（对应 `cmtkuc1za000bnw07w0i5a3zg`）查该 trace 最后一条
observation。当前最可能就是 deepagents 自动压缩的短暂暂停，不影响上述修复方向。
