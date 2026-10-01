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
     - ⚠️ **`end` 是开区间上界**：恒编译成 `col >= 'start' AND col < 'end'`（本地编译实测，月/天粒度都一样）。
       要含 9/30 就传 **`2026-10-01`**；传 `2026-09-30` 会**静默丢掉 9/30 一整天**（不报错、不降级）。
       `2026-09-30 23:59:59` 同样不行 —— 仍丢最后一秒。**一律传"次日"**。
       生产实测 2026-10-01（trace `e9c5a71b`）：传 `2026-09-30` 得 9 月新增 1049，传次日才是与手写版一致的 1063。
   - `filters = "dim:op[:value]"`，多值分号分隔。**op 是枚举名、不是比较符号**，只认
     `eq` / `neq` / `in` / `not_in` / `gt` / `gte` / `lt` / `lte` / `contains` /
     `starts_with` / `is_null` / `is_not_null`；写错整个调用直接失败（报
     `unknown variant`，不是降级）。
     - ✅ `user_name:eq:丛培强`、`story_count:gte:20`、`status:in:进行中;已完成`
     - ❌ `user_name:=:丛培强`（生产实测 2026-09-25 首调即失败，白丢一次调用）
3. **先 `sql_only=True` 预览**编译 SQL（官方 docstring 推荐，不连库）：核对聚合口径——**只看 expression 是否等价于问题要的口径，绝不看 type**（实测 wren_core 彻底忽略 measure.type，`type=sum` 也不会包 SUM）。
   **预览必须带上真实 `time_dimension` 区间**（别只预览不带区间的形状）：`WHERE` 的起止边界只有带上区间才看得见，
   上面那条「`end` 开区间吞掉末日」的坑就该在这一步发现——生产那次正是只预览了**不带区间**的 SQL，随后把带区间的直接拿去跑。
4. 分流：
   - 维度齐全 → 编译 SQL 即最终主体，交步骤(4) dry_run
   - 缺维度（三条件③不满足）→ 编译 SQL 作 CTE/子查询底座，交 **wren-sql-author** 外层补维度/过滤（混合路径）

> ⚠️ **要出数就摘掉 `sql_only`**：`sql_only=True` 只回编译后的 SQL、**不回数据行**。维度齐全、
> 指标已在 cube 内表达时，最终数据必须来自一次**不带 `sql_only`** 的 `query_cube`——否则数据
> 兜底走 run_sql，报告退回 SQL 通道，Cube 的「业务口径 / 查询结构 / 查询定义」三节一节都不出
> （生产实测 2026-09-25 trace `294bbc0f`：只预览不取数 → 报告零业务口径）。

## 何时不走本 skill

**cube 已声明的 measure / dimensions / time_dimensions 范围内是硬车道**，不是「软偏好」：
只要问题要的口径在 cube 里有具名指标，**必须**走本 skill 的 `query_cube`，哪怕你手写出
一条等价 SQL 也能跑通。

**多时间轴不是手写理由**：一次调用只接受**一个** `time_dimension`（CLI 恒建单元素列表）
⇒ 有几条轴就**分次调用几次**（如「新增」按 `mention_time`、「完成」按 `complete_time`
各一次），**结果并列呈现**，禁止因为"要拼两条轴"就退回手写 SQL。

只有 cube **表达不了**的情形才转手写（作答时必须写明为什么不走 cube）：
- 行级明细（cube 只出聚合指标）；
- 窗口函数、跨 cube / 跨源关联；
- 问题要的维度 / 口径 cube 里没有声明。

**允许混用**仅指一种形态：**cube 出指标主体 + 手写补 cube 未覆盖的那部分**
（＝报告里的「混合路径」）。它**不是**"拿不准就先手写"的许可 —— 拿不准时先 `query_cube`
预览编译 SQL 核对口径（步骤 3），别用 `describe_cube` 的 measure 描述当口径词典去手抄
表达式：那样数字对得上也只是"抄对了"，没有一致性校验兜底。

## 降级

- `query_cube` 报 cube/measure 不存在 → 回取料包核对拼写 1 次，仍败 → 整体降级手写路径。
- 时间参数拼错致编译失败 → 交 dry_run 修正循环（≤3），不单独开环。
