---
version: 0.1.0
name: wren-metric-query
description: "触发：编排器判为具名指标（纯指标或混合）。cube 信息已在取料包内，直接组 query_cube 参数；默认先 sql_only=True 预览编译 SQL 核对口径，缺维度时输出作子查询底座交 wren-sql-author 包外层。判断聚合口径一律读 expression，永不读 type。"
---

# 具名指标查询（query_cube）

## 输入

- `retrieval.json` 的 `structure.cubes`（cube 名 / measures{name,expression} / dimensions / time_dimensions[+levels]）
- `knowledge.glossary/metrics`（业务词 → 标准指标名映射）

## 执行步骤

1. **就地定参**：从取料包锁定 cube + measures + dimensions + 时间粒度，**零新增调用**。
2. **组装参数**（时间/过滤格式钉死）：
   - `time_dimension = "name:granularity:start,end"`（粒度取该 time_dimension 的 `levels` 列表；起止为字面日期串，相对时间词如"最近30天"须先解析成绝对日期）
   - `filters = "dim:op[:value]"`，多值分号分隔。**op 是枚举名、不是比较符号**，只认
     `eq` / `neq` / `in` / `not_in` / `gt` / `gte` / `lt` / `lte` / `contains` /
     `starts_with` / `is_null` / `is_not_null`；写错整个调用直接失败（报
     `unknown variant`，不是降级）。
     - ✅ `user_name:eq:丛培强`、`story_count:gte:20`、`status:in:进行中;已完成`
     - ❌ `user_name:=:丛培强`（生产实测 2026-09-25 首调即失败，白丢一次调用）
3. **先 `sql_only=True` 预览**编译 SQL（官方 docstring 推荐，不连库）：核对聚合口径——**只看 expression 是否等价于问题要的口径，绝不看 type**（实测 wren_core 彻底忽略 measure.type，`type=sum` 也不会包 SUM）。
4. 分流：
   - 维度齐全 → 编译 SQL 即最终主体，交步骤(4) dry_run
   - 缺维度（三条件③不满足）→ 编译 SQL 作 CTE/子查询底座，交 **wren-sql-author** 外层补维度/过滤（混合路径）

> ⚠️ **要出数就摘掉 `sql_only`**：`sql_only=True` 只回编译后的 SQL、**不回数据行**。维度齐全、
> 指标已在 cube 内表达时，最终数据必须来自一次**不带 `sql_only`** 的 `query_cube`——否则数据
> 兜底走 run_sql，报告退回 SQL 通道，Cube 的「业务口径 / 查询结构 / 查询定义」三节一节都不出
> （生产实测 2026-09-25 trace `294bbc0f`：只预览不取数 → 报告零业务口径）。

## 何时不走本 skill（软偏好，非互斥车道）

- 单次只接受**一个** time_dimension（CLI 恒建单元素列表）→ 多时间轴对比 → 转手写。
- 跨 cube 关联、窗口函数、Cube 未覆盖的明细查询 → 转手写。
- 判断拿不准时**允许混用**：Cube 出指标主体 + 手写补其余，不强制二选一。

## 降级

- `query_cube` 报 cube/measure 不存在 → 回取料包核对拼写 1 次，仍败 → 整体降级手写路径。
- 时间参数拼错致编译失败 → 交 dry_run 修正循环（≤3），不单独开环。
