# 终态失败的 run 被误报成「上一轮回复已中断（可继续）」

**日期**：2026-09-15
**类型**：判定缺口（run 终态失败 vs 被外部打断）+ 失败原因不可见
**严重程度**：中（新建会话第一轮就弹假「已中断」横幅，「继续」把同一个必失败的请求再发一遍，用户全程看不到原因）

---

## 一、问题描述

生产环境新建一个会话（`assistantId=chat_agent`，thread `01a0a394-e300-7e31-a04d-ba82f611220f`），
只发了一句「你好」，界面就出现：

> **上一轮回复已中断（未完成）**
> 服务重启或任务被外部中断，智能体没有跑完这一轮。点击「继续」接着执行，已产生的查询结果与文件不会丢失。　\[继续\]

实际情况与之相反：**这一轮跑过了，是被服务商 400 打回而失败的**，而且原因完全没露出来。

### 复现条件

1. 会话所选模型的名字不被当前 provider 支持（本例 `deepseek-v4.1-flash` 不在
   `api.deepseek.com/v1` 的可用列表里）；
2. 发任意一条消息 → 图在 `model` 节点上抛 `BadRequestError`，run 终态 `error`；
3. 前端轮询 `run-status` → 四判据全部成立 → 渲染「已中断」横幅。

点「继续」只是把同样的请求（同样的 `llm_model`）再发一次 → 同样的 400 → 横幅再出现，
形成死循环。

---

## 二、根因分析

### 根因 1：判据只有「图停在半轮」，没有「最后一轮跑失败了」

`src/api/thread_run_status.py::classify()` 原有四条判据（`next` 非空 + 无 `pending/running` run +
无 HITL interrupt + 最后一条不是终稿 assistant 文本）。本例：

| 判据 | 实际值 | 是否成立 |
|---|---|---|
| `next` 非空 | `["model"]` | ✅ |
| 无活跃 run | 只有 1 条 run，`status=error` | ✅ |
| 无 interrupt | `tasks[].interrupts=[]` | ✅ |
| 最后一条不是终稿 assistant | 线程里**只有 1 条 human**（模型从没回话） | ✅ |

四条**全都是事实**，但拼出来的结论是错的。「被打断」（run 没了、图还停在半轮）与
「失败了」（run 终态 error、图也停在半轮）在 state 形状上几乎一样，**只有 run 的终态能区分**。

### 根因 2：失败原因在前端无处可寻

- `run.error` **恒为 `None`**：`/threads/{tid}/runs` 列表与单条 `/runs/{id}` 实测都是 `None`
  （langgraph 没有把 run 级异常落进这个响应）。因此既有的「终态透传 `run.error` → 侧边栏
  红色错误行」链路对本类失败**根本不触发**，`check_progress.py` 里读 `run.get("error")`
  拿到的也是空。
- 真正的原因只活在 **checkpoint 的 `tasks[].error`** 里，且是异常对象的 repr
  （500+ 字符、含 provider 原始 JSON 字典），前端无法直接展示。

```
BadRequestError("Error code: 400 - {'error': {'message': 'The supported API model names
are deepseek-flash, deepseek-v4-pro, but you passed deepseek-v4.1-flash.', 'type':
'invalid_request_error', 'param': None, 'code': 'invalid_request_error'}}")
```

用户真正需要的是里面那一句 `message`。

---

## 三、解决方案

### 后端（`src/api/thread_run_status.py`）

1. **新增判据 5 `turn_failed`**：最后一轮 run 的 `status ∈ {error, timeout}`
   且无活跃 run、无 HITL 审批 → `turn_failed=True`，并让 `turn_incomplete` **让位**
   （`... and not turn_failed`）。前端两条横幅因此天然互斥，无需两侧同时改判断。

   **`cancelled`（用户点停止）与 `interrupted`（HITL 审批）故意不算失败**——那是用户/
   流程预期的暂停，报「执行失败」是另一种误导。

2. **新增 `last_error`**：`turn_failed` 时从 `state.tasks[].error` 取（优先 `next` 里那个
   节点，退一步取任意最后一个带 error 的 task），三步剥成一行可读文本：

   `_extract_error_text()`：异常 repr 外壳（`BadRequestError("…")`，`ast.literal_eval`
   解出内层字符串）→ provider 的 `message` 字段（正则，压掉 `{'error': {'type': …}}`
   等噪声）→ 空白压平 + 300 字截断。
   **任何一步解析失败都退回上一层原文**，绝不返回空串——宁可给用户一段丑的，也不能给
   「未知错误」；同时 `turn_failed` 与 `last_error` 解耦：拿不到文本时标志照样为真，
   界面退化成「执行失败（原因可在侧边栏查看）」而不是退回假「已中断」。

### 前端（`harness-deep-agents-ui`）

3. `src/lib/threadRunStatus.ts`：`ThreadRunStatus` 增 `turn_failed: boolean` /
   `last_error?: string`（含「与 `turn_incomplete` 互斥」的注释）。
4. `ChatInterface.tsx`：
   - 新增红色横幅（复用子任务失败卡的视觉语言 `border-destructive/30 bg-destructive/5`）：
     标题「上一轮执行失败（未完成）」+ `last_error` 原文（`font-mono` 折行）+ 一句
     「点「重试」会从断点接着执行，已产生的查询结果与文件不会丢失；**若原因与模型配置有关，
     请先修正，否则重试会以同样方式失败**」——把「继续」按钮改成「重试」（`RotateCw` 图标），
     仍复用 `handleContinueTurn`（先 `stopStream()` 再发普通「继续」消息，不带 `checkpoint_id`）。
   - `turnStatusSettled` 与轮询静止计数都纳入 `turn_failed`：失败是终态结论，停止轮询。

---

## 四、验证

| 用例 | 结果 |
|---|---|
| `D:\tmp\test_run_status_failed.py`（离线 **26/26**，纯函数 + 假 `httpx.AsyncClient`，零网络） | A 生产原形（`error` + `next=['model']` + 只有一条 human）→ `turn_failed=True`、`turn_incomplete=False`、`last_error` 恰为 provider 那句 message（无 `BadRequestError` 外壳、无字典残留）；B 拿不到 error 文本时标志仍正确、无 `tasks` 不抛；C `cancelled`/`interrupted`/有审批/有活跃 run 都不算失败；D 回归真「被打断」形态（`success` + `next` 非空 + 末条工具调用 → 仍报中断；末条终稿答复 → 两条都不报）；E `_extract_error_text` 八例（裸串、dict repr、非字符串字面量、message 内嵌单引号、多行、500 字截断、空/None）；F 端点层 JSON 真到前端（新字段 + 既有字段一个不少、有活跃 run 时两条都不报、非法 thread_id 仍 400） |
| `D:\tmp\test_thread_run_status.py`（既有套件回归） | **26/26 ALL PASS**（判据 1~4 行为零变化） |
| 生产实测（LAN 只读探测） | `GET /threads/01a0a394…/state` 的 `tasks[0].error` 即上文那条 400；`/runs` 与单条 `/runs/{id}` 的 `error` 均为 `None`（根因 2 的证据） |
| 前端 `tsc --noEmit` | 全项目 27 条，与改动前**逐条相同**（均为存量 `@ts-expect-error` / 存量 TS2464）；改动文件零新增 |
| 前端 `eslint`（改动文件） | `threadRunStatus.ts` 干净；`ChatInterface.tsx` 3 error + 1 warning 与 `git show HEAD:` 版本**逐条相同**（仅行号位移）——本次改动零新增 |

**发版后 E2E（用户执行）**：

1. 重启后端 + 重建前端；
2. 把会话切到一个**不被 provider 支持**的模型名（或临时把 provider 的 `default_model`
   改成失效名）→ 发一条消息 → 界面应显示**红色**「上一轮执行失败（未完成）」+
   provider 的原始 message（不再是「已中断」）；
3. 点「重试」→ 应再次失败并回到同一条红色横幅（文案已说明原因）；
4. 把模型名改回可用值（`deepseek-flash` / `deepseek-v4-pro`）→ 点「重试」→ 应正常跑完，
   横幅消失、聊天流出现答复；
5. 回归：重启后端把一次 run 打死在 `model` 节点（真「被中断」）→ 仍应显示**琥珀色**
   「上一轮回复已中断（未完成）」+「继续」。

---

## 五、遗留与风险

- **失败横幅只在 `run-status` 轮询时出现**：轮询由「`isLoading` 或末条非终稿」驱动，
  失败后 `isLoading` 为假但末条仍非终稿（模型没回话）→ 会轮询到；但若某类失败恰好留下
  一条终稿文本（例如中间件把错误转成了 AIMessage），则既不报失败也不报中断——
  那正是**期望行为**（错误已经落在聊天流里，用户看得到）。
- **`last_error` 是 provider 原文**：模型名、配额、鉴权等会原样露出（含英文）。
  这是有意的（可归因），若后续要做「友好化」应另加映射层，不要在剥壳处做翻译。
- **未做第二层**（用户 2026-09-15 只批了第一层）：仿 `quota_error.py` / `model_timeout.py`
  加通用「模型请求被拒 → 友好 AIMessage」中间件，让失败原因直接进聊天流。代价是要小心
  不把真实的其它 `BadRequest` 掩掉；目前失败原因只在横幅里出现，聊天记录中仍无痕。
- **`timeout` 归为失败**：run 级 `timeout` 与 `ModelTimeoutMiddleware` 的「超时转友好
  AIMessage」是两条不同的路——后者会让末条成为终稿（不报横幅），前者才会报。
