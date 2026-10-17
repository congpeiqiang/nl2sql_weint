# failure_circuit.py — FailureCircuitMiddleware 代码解读

> 源文件：`src/agent/middlewares/failure_circuit.py`
> 类别：**执行监督 / 失败熔断**
> 挂载点：`wrap_tool_call`（后处理）

## 1. 解决的问题

**trace ec484c693f**（2026-10-09）：语义库删掉 model `do_bug` 后，模型拿到 `table 'wren.public.do_bug' not found` 后不收手：改 SQL 写法、在 `dry_run` → `run_sql` → `query_cube` → `dry_plan` 之间换工具。实测 98 次工具调用 / 26 次失败，其中 **18 次是同一个对象**，耗时 406.7s。

### 1.1 既有护栏全漏

| 护栏 | 为什么没拦住 |
|---|---|
| `nl2sql_agent.dynamic_prompt` | 「若 not found 立即改 dbmcp」**只在 db 未建模的分支**注入；本库建了模 → 不在提示词里 |
| `MessageSlimmerMiddleware` | 去重只认逐字节完全相同；模型每次微调 SQL → md5 变了永不触发；且只省 context 不省调用 |
| 全仓无同类失败计数 | 根本没人统计 |

## 2. 签名口径（关键设计）

```
签名 = (错误族, 归一化对象名)    ← 不含工具名
```

**不带工具名是刻意的**：同一根因会被 `dry_run` / `run_sql` / `query_cube` / `dry_plan` 四种工具各撞一次。若把工具名并入签名，每种工具各自计数 → 熔断形同虚设。

对象名取**最后一段**（`wren.public.do_bug` → `do_bug`），跨工具对齐。

## 3. 作用域

计数来源是 **state 里「最后一条 HumanMessage 之后」的 ToolMessage**，不落任何进程内账本。

好处：
- 子 agent 线程即使被复用，新问题从新的 HumanMessage 起算 → **不误伤**
- 无进程内状态 → 重启 / 多 worker 都不会错乱
- 同一批并行调用互相看不见 → 并发扇出天然不算重复

## 4. 升级与硬闸

| 第几次 | 行为 |
|---|---|
| 1 | 放行原错误，但改写为**可收手的信息**：语义层没有该对象、换写法/换工具都不会让它出现、先去 `list_models()` 核实 |
| ≥ WARN（默认 2） | 改写为「这是本 run 第 N 次撞同一对象，**停止重试**」 |
| ≥ STOP（默认 3） | 改写为终态措辞；此后该对象进**硬闸** — 任何 args 里再出现这个对象名的调用**直接拒绝、不再执行** |

### 4.1 跨对象失败预算

本轮「对象不存在」类失败**跨不同对象**的总量 ≥ TOTAL（默认 6）时，在消息尾追加「已达失败预算，立刻收尾」。

单对象熔断挡不住「换个不存在的对象就重置计数」的试探（trace 里模型另探了 `do_bug_wide` / `do_story_wide`，都是知识库注明已停用的幻觉名）。这一档**只加措辞、不硬拦** — 跨对象硬拦的误伤面太大。

### 4.2 机读标记

改写后的消息内嵌 `【fail-circuit:<族>:<对象>】`，让下一轮的签名解析不依赖自由文本（模型可能转述、中间件可能改写措辞）。

## 5. 挂载位置

`nl2sql_agent._middleware`，紧挨 `LangfuseSpanMiddleware` **之前（= 外层）**。

位置讲究：`wrap_tool_call` 链 first=outermost、**内层后处理先跑**：

```
请求流：FailureCircuit → LangfuseSpan → 工具
响应流：工具 → LangfuseSpan → FailureCircuit
```

- span 与 process_data dump 吃到的是**原始**报错
- Slimmer 体积判据/md5 去重作用在**改写后**进 state 的那份文本上

## 6. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `NL2SQL_FAIL_CIRCUIT` | 开启 | 设为 `off` 全放行 |
| `NL2SQL_FAIL_CIRCUIT_WARN` | `"2"` | 警告阈值 |
| `NL2SQL_FAIL_CIRCUIT_STOP` | `"3"` | 拦截阈值（进硬闸） |
| `NL2SQL_FAIL_CIRCUIT_TOTAL` | `"6"` | 跨对象失败预算 |

## 7. status="error" 保留

langfuse_span 依赖它把该次记成 exec 失败而不是 `sql_exec_success=1`。
