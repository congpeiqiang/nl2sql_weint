# 看门狗按「运行时长」误杀正常工作着的子任务，随后终态被轮询打回 running

**日期**：2026-10-16
**类型**：误杀（判据口径错）+ 终态保护缺口 + 失败去重标记写入失败
**严重程度**：高（一条已经完成 90% 的分析被丢弃；用户被告知「会自动恢复」，白等 35 分钟；点「停止」还会让卡片复活）

---

## 一、问题描述

生产 64 环境会话 `01a143da-ce84-7b70-bef8-ac6f60074304`，用户问「丛培强 8-9 月工时分析」，
子任务卡片长时间停在「查询执行」转圈。用户两次点「停止」，得到的回答是：

> 还没完成 ⏳ 子智能体当前处于**短暂暂停**（上下文压缩），会自动恢复，请稍候。

最终卡片以 `timeout` 收尾，**两个完整的分析草稿（各 2900~3500 字符）留在子线程消息里没有交付**。

### 复现条件

1. 子任务正常执行但单轮耗时**超过 600s**（本例如 `caliber_gate` 的口径核验重写循环
   `CALIBER_EVIDENCE_MAX_RETRIES=2`，把一次完整分析重写了三次）；
2. 看门狗在 600s 处 `runs.cancel()` 强杀；
3. 用户在卡片上点「停止」。

---

## 二、根因分析

### 根因 1：卡死判据用「跑了多久」而不是「有没有进展」

`sync_subagent_todos.py` 的 `STALE_RUN_TIMEOUT=600` 原语义是「run 运行时长超过 600s 即视为卡死」。
本例的子 agent **一直在推进**（todos 7/7、消息游标 44→46 持续增长），只是被核验重写推过了 600s。

关键细节：看门狗在 600.8s 触发时，子 agent 正在做**第三次、也就是本该通过的那一次**重写
（预算是 2 次重试）。判据与「健康度」无关，只与「年龄」有关 ⇒ 越认真干活的越容易被杀。

### 根因 2：`interrupted` 的两种来源同形，被一律当成「瞬时暂停」

看门狗走 `runs.cancel()` ⇒ 默认 `action=interrupt` ⇒ 平台 run 状态变 **`interrupted`**。
而这与「deepagents 上下文压缩的瞬时暂停」在 run 状态上**完全同形**。

`check_progress.py` 的 interrupted 分支当时无条件归一为 `running` + 一句「会自动恢复，
不要取消或重新委派」。于是：

| 时刻（本地） | 事件 |
|---|---|
| 16:46:26 | 看门狗判超时 → cancel → run 变 `interrupted` |
| 16:46:29 | watcher 写 `async_tasks=timeout`、`active_queries=false`、发失败汇报续跑 |
| 16:46:31 | 写 `failure_reported` 去重标记**失败**（"Thread is busy with a running job."） |
| 16:46:45 | 续跑 run 里主 agent 的 `check_async_task` 读到 `interrupted` → 归一 `running` → **写回 async_tasks**，终态被打回 |
| 16:47:43 | watcher 的重写守护把 timeout 写回（第 9 次重试），16:48:04 watcher 退出（`cycles=20`）—— 之后无人守护 |
| 17:04 / 17:12 | 用户点「停止」→ `cancel_task` 幂等分支把 `status_override="interrupted"` 写进 async_tasks → 重启的 watcher 判它「**非终态**」→ 写 `active_queries=true` + 重派发占位 → **卡片复活**，120s 后才被 `INTERRUPTED_STUCK_TIMEOUT` 收掉 |
| 17:16:29 | timeout 最终稳定 |

两个模型的账对不上：**工具轮询相信 run 状态，watcher 相信自己的判定**，谁最后写谁赢。

### 根因 3：`failure_reported` 是「一次性 best-effort」

写入时机恰好是失败汇报**续跑 run in-flight 的时候**，`update_state` 被拒（"Thread is busy"）
是**常态而非异常**。一次失败就永远丢标记 ⇒ 同一失败可能被反复汇报。

### 根因 4：`cancel_task` 把 `interrupted` 当可回写的终态

`_TERMINAL_STATUSES`（该端点内）包含 `interrupted`，于是点「停止」时把 `interrupted` 原样写进
`async_tasks`。但 sync watcher 的 `_RUN_DONE_STATUSES` **不含** `interrupted` —— 同一个仓库里
两份「终态」定义打架，重启的 watcher 判它非终态 ⇒ 卡片复活。

---

## 三、修复

### 1. 卡死判据改成「进度停滞」（`sync_subagent_todos.py`）

- 新增 `_progress_probe()`：取子线程进展指纹 = **消息数 + 末条消息 id + todos 状态序列**。
  只取 `status` 不取 content —— 渲染后的 content 带耗时后缀（`(5s)`→`(37s)`）会自己变，
  把它算作进展会让卡死任务永远不被判超时。
- 有进展 → 停滞计时清零；连续 `STALE_RUN_TIMEOUT`(600s) **无进展**才判卡死。
- 另加 `ABSOLUTE_RUN_TIMEOUT=3600` 兜死循环（无进展判据之外的绝对上限，两者独立）。
- 探针每 `STALL_PROBE_EVERY_CYCLES`(8 → 4s) 探一次（要一次 `get_state`，不每轮都探）；
  **探针读失败返回 None 且不重置停滞计时**（否则一次读取抖动就能让卡死任务永生）。

### 2. interrupted 分诊：用证据而不是猜（`check_progress.py` + `sync_subagent_todos.py`）

两条**互相独立**的判据，命中任一即如实报 `timeout`：

- **看门狗标记**：watcher 在 cancel 前 `note_watchdog_kill(run_id, reason)` 登记进程内标记，
  check 侧 `_watchdog_kill_reason(run)` 查它 —— 唯一能区分两种 interrupted 的证据在**杀人者**手里。
- **时长熔断**：`_interrupted_stuck_seconds(run)` 用 `run.updated_at`（**不是** `created_at`：
  按创建时刻算会把「第 10 分钟才被中断」的合法长查询误杀）超过 `_INTERRUPTED_STUCK_GRACE_SECS=120` 即熔断。

只有两条都不成立才维持「瞬时暂停」旧语义，并补一句「若数分钟后仍为 interrupted，说明已被看门狗终止」。

### 3. 终态保护：条目已终态，结果不得是非终态（`check_progress.py`）

- `_reconcile_result_with_task(result, task)`：对账，把结果改判为条目终态、丢弃「会自动恢复」
  这类乐观说明、附 `state_note` 说明为什么（不让模型自己发现两套账）。
- `_enhanced_build_check_command`（`result["status"]` 落 `async_tasks` 的**唯一漏斗**）里一道硬闸：
  终态条目 + 非终态结果一律拒绝，**无论谁调用**。
- 重派发不受影响：`update_async_task` 换 run 时**先**把条目写成 `running`
  （deepagents `async_subagents.py:544`），硬闸前提不成立。

### 4. `failure_reported` 写入改为有界重试（`sync_subagent_todos.py`）

`30 × 2s`（60s 窗口），仍失败才记 error 日志并放弃。

### 5. 点「停止」不再复活卡片（`api/task_cancel.py` + `sync_subagent_todos.py`）

- `cancel_task`：run 停在 `interrupted` 时把回写状态归一为 `cancelled`（停止的语义就是取消，
  且 `cancelled` 是 watcher 认的终态）；返回体同样归一。
- watcher 启动自愈：`_read_task_status` 读不到时**不许断言「在跑」**，退回启动方给的条目；
  条目说终态而 state 不是终态（写入被 in-flight 拒绝）时以终态为准。
  收敛到终态是幂等且安全的，反向才会留下永久转圈的卡片。

---

## 四、影响面与回归

- 触碰文件：`src/agent/subagents/sync_subagent_todos.py`、`src/agent/subagents/check_progress.py`、
  `src/api/task_cancel.py`（无接口/字段变更，全部是判定与收尾路径）。
- 新增回归：
  - `src/test/unit/test_terminal_state_guard.py`（终态对账 / 硬闸 / interrupted 分诊 / 标记接线）
  - `src/test/unit/test_sync_stall_detection.py`（进展指纹的「真进展 vs 假进展」/ 读失败 fail-open / 标记容量）
- 未覆盖（需生产 E2E）：卡死判据的真实超时行为、`update_state` 被拒时 watcher 的启动兜底。
- ⚠️ 与既有 `docs/bug修复记录/子任务interrupted误判终态导致前端卡死.md`（2026-09）叠加：
  那次把 `interrupted` 从 `_RUN_DONE_STATUSES` 移除（**不**把瞬时暂停当终态是对的），
  但把 `check_progress` 的 interrupted 一律说成「会自动恢复」，本文件修的是它的另一半。
