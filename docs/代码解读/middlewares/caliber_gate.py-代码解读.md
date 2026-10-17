# caliber_gate.py — CaliberGateMiddleware 代码解读

> 源文件：`src/agent/middlewares/caliber_gate.py`
> 类别：**执行监督 / 口径治理**
> 挂载点：`after_model`（带 `@hook_config(can_jump_to=["model"])`）

## 1. 解决的问题

**纯提示词已证伪**：`NL2SQL_SYSTEM_PROMPT.md` §十一第 3 条与 `wren-execution/SKILL.md` 本来就写着「内容取原文」「出处必须是知识库里真实存在的文件名」，模型仍然不守。

生产实证（trace `9c81d3181f2a72f27cf9d092d7185fab`）：run 调过 `wrenai_witops_get_instructions`（20921 字符，含 `业务域口径`/`报工与工时`/`R1`/`R3`），报告里却写成自己的转述 + 伪造出处。

## 2. 核心设计哲学

### 2.1 本中间件只「逼」，不下判决

核验结论**只在报告装配时产生**（`report_builder` 用同一份磁盘语料重算，见 `utils/caliber_evidence.py`），本中间件只负责在终态答案不合规时把模型打回重写。

- 判决只有一处，不会出现「中间件说合规、报告说未核验」的两套账
- 重试耗尽也不需要往消息里塞结论（原文本会被摘要掉），报告侧独立重算即如实标注

### 2.2 不往答复里写任何「核验结论」行

曾设计过「重试耗尽时追加 `> 口径核验：…`」→ **已否决**：报告侧能摘到的这类行只可能是模型自己写的自评，把它印进报告就是「报告撒谎」换个人称。核验结论必须由代码产生——见 `report_builder._render_business_caliber` 的四态脚注。

## 3. 两条执法分支

| 终稿形态 | 判定函数 | 处置 | 预算 |
|---|---|---|---|
| 有 `## 业务口径` 块，但条目核验不过 | `_after` 主体 | 打回列出哪几条不合格 | `_MAX_RETRIES`=2 |
| **整节没有**块，且本轮取过知识料 | `_no_block` | 打回要求补块（或声明「本次未依据知识库口径」） | `_MAX_MISSING_RETRIES`=1 |
| 末条是**失败终局**（`failure_signal` 有戳） | `_after` 开头 | **不打回**，放行让回合结束 | — |

### 3.1 第二条的来由

2026-09-26 生产 trace `f222a8a5…`：提示词 v4 + 本中间件都已在线上，子 agent 手里有 20.9k 口径原文（含 R1/R6/R7/R8）却**整节省略**，报告完全没有业务口径节。原先「只对已存在的块执法」等于把「省略」变成最省事的过关方式。

### 3.2 第三条的来由

2026-09-28 生产 trace `3dcc9a66…`：超时被 `ModelTimeoutMiddleware` 吞成一条友好文档后掉进 `_no_block`，两条逃生口对失败消息都不成立 → 打回 → 图又发起第二次 240s 调用，用户看到 234s 空窗、整轮 620.8s 一 SQL 未执行。**失败消息不是"没写完的终稿"**。

## 4. 重答机制：after_model + jump_to="model"

### 4.1 三个关键事实

1. `after_model` **每轮必跑**（包括终态轮）
2. 但只往 state 注入消息**不会让模型重答**——必须用 `jump_to`（`EphemeralValue`，每个 superstep 后自动清空 → 不会死循环）
3. **必须显式 `@hook_config(can_jump_to=["model"])`**——因为本中间件不是 `loop_exit_node`

### 4.2 装饰器的必要性

`jump_to` 只在本节点的出边是**条件边**时才会被读：

- `loop_exit_node`（列表里第一条带 after_model 的中间件）的出边无条件读 `jump_to` → 不写装饰器也能跳
- 其余节点靠 `_add_middleware_edge` 建边：`can_jump_to` 非空才建条件边，**空则退化成平边** → `jump_to` 被静默丢弃

deepagents 把 `TodoListMiddleware()` 恒定放在 middleware 栈首位 → 本中间件**永远不是** `loop_exit_node` → 装饰器是**硬要求**。

### 4.3 静默坑

hook 的第二参数**必须叫 `runtime`**（langchain 按参数名注入），改叫 `r` 会在运行期抛 `missing 1 required positional argument`。

## 5. 关键设计决策

### 5.1 为什么不在工具边界拦截

- 口径校验需要看**最终答案**（不是 SQL 执行结果）
- `wrap_tool_call` 时还不知道模型最终会怎么写报告
- `after_model` 看到完整 AIMessage 后才能判定是否合规

### 5.2 为什么重试预算分开

- "有块但核验不过" → 模型可能只差几条引用 → 给 2 次机会
- "整节没有块" → 模型可能完全忽略口径要求 → 只给 1 次机会（行为更偏离，收敛更不确定）

### 5.3 为什么失败终局不打回

- `failure_signal` 有戳 = 上游已判定这轮失败（超时/熔断/其他致命错误）
- 打回只会再触发一轮无意义的 LLM 调用
- 放行让回合结束，由报告侧如实标注失败原因

## 6. 错误处理

- 核验函数异常 → fail-open，不打回
- `jump_to` 未生效（装饰器缺失） → `jump_to` 被静默丢弃，末条消息变成纠正提示（尾部污染）
- 重试耗尽 → 停止打回，模型的最后一次输出作为终稿
