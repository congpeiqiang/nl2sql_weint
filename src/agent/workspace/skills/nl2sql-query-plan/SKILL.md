---
name: nl2sql-query-plan
description: "触发条件：NL2SQL流水线需要根据自然语言问题、Schema和子问题生成程序化的逐步查询执行计划；用户希望在SQL生成之前获得思维链推理计划。本技能是SQL-of-Thought流水线的第3步，在子问题分解之后、SQL生成之前使用。关键约束：本智能体不得生成SQL——仅生成程序化计划。跳过条件：用户要求不经过计划直接生成SQL；用户已有现成的查询计划。"
---

# NL2SQL 查询计划智能体

## 概述

查询计划智能体是SQL-of-Thought流水线的**第3步**。它使用**思维链（Chain-of-Thought，CoT）推理**，生成一个**逐步的程序化执行计划**，将用户意图映射到Schema和子问题。

这是一个关键的中级推理步骤，可减少幻觉并改善自然语言意图与最终SQL输出之间的对齐。本智能体**明确禁止在此阶段生成可执行的SQL**。

## 在流水线中的角色

```
输入：  自然语言问题 (Q) + 裁剪后的Schema (S) + 子句级子问题 (C)
输出：  程序化查询计划 (P) — 逐步推理，不含SQL
下一步：→ nl2sql-sql-generation（第4步）
```

## 关键约束

> ⚠️ **查询计划智能体不得生成SQL**
>
> 本智能体仅生成**程序化计划**——即对如何解决查询的逐步描述。SQL生成是SQL智能体（第4步）的专属职责。
>
> 违反此约束将导致：
> - 幻觉率增加
> - 准确率下降约5%（根据消融实验）
> - 自然语言意图与SQL输出之间不对齐

## 执行流程

### 第1步：审阅所有输入

收集并审阅：
- **问题 (Q)**：原始自然语言问题
- **Schema (S)**：裁剪后的Schema，含相关表、列、主键、外键
- **子问题 (C)**：子问题智能体输出的JSON键值对

### 第2步：思维链推理

对每个步骤，明确解释决策背后的推理。使用以下CoT结构：

```
问题："查询人数超过10人的部门中员工的平均薪资"
Schema：employees(id, name, salary, dept_id), departments(id, dept_name)
子问题：{ SELECT: "每个部门的平均薪资", JOIN: "通过dept_id连接employees↔departments", GROUP BY: "按部门", HAVING: "计数 > 10" }

推理步骤1：问题同时涉及员工和部门。
→ 需要JOIN employees和departments两张表。
→ 外键 employees.dept_id 引用 departments.id，因此连接条件为：employees.dept_id = departments.id

推理步骤2：问题提到每个部门的"平均薪资"。
→ 这需要按部门分组，因此按 departments.dept_name 分组。
→ 聚合函数为AVG，作用于 employees.salary。

推理步骤3：问题指定每个部门"超过10人"。
→ 这是一个聚合后条件，因此属于HAVING子句。
→ 需要计算每个部门的员工数量（COUNT(*)）并筛选 > 10。

推理步骤4：综合汇总：
→ 1. 从 employees 表开始
→ 2. 通过 dept_id 连接 departments 表
→ 3. 按 dept_name 分组
→ 4. 对每个分组计算 AVG(salary) 和 COUNT(*)
→ 5. 筛选 COUNT(*) > 10 的分组
→ 6. 返回符合条件的部门的 dept_name 和平均薪资
```

### 第3步：综合生成程序化计划

将推理转换为编号的程序化计划：

```
查询计划：
1. 使用外键关系将 employees 表与 departments 表连接：employees.dept_id = departments.id
2. 按部门名称（departments.dept_name）对连接结果进行分组
3. 对每个分组计算两个聚合值：
   a. 平均薪资：AVG(employees.salary)
   b. 员工人数：COUNT(*)
4. 应用聚合后过滤：仅保留员工人数（COUNT(*)）超过10的分组
5. 返回符合条件的部门的两列数据：部门名称和平均薪资
6. 问题中未指定显式的排序或数量限制
```

### 第4步：对照Schema验证计划

最终检查：
- 计划是否仅引用了裁剪后Schema中存在的表/列？
- 每个子问题 (C) 是否在计划中有对应的步骤？
- 连接条件是否使用了有效的外键？
- 聚合函数是否与适当的GROUP BY配对？

## 输出模板

```json
{
  "question": "原始自然语言问题",
  "schema_used": {
    "tables": ["……"],
    "key_columns": ["……"],
    "join_conditions": ["……"]
  },
  "subproblems_addressed": {
    "SELECT": "步骤X覆盖此项",
    "JOIN": "步骤Y覆盖此项",
    "GROUP BY": "步骤Z覆盖此项",
    "HAVING": "步骤W覆盖此项"
  },
  "cot_reasoning": [
    "每个决策的逐步推理……"
  ],
  "procedural_plan": [
    "1. 程序化步骤一……",
    "2. 程序化步骤二……",
    "3. 程序化步骤三……",
    "……"
  ],
  "plan_verification": {
    "all_subproblems_covered": true,
    "schema_references_valid": true,
    "joins_use_valid_fks": true
  }
}
```

## 示例

### 简单筛选计划
```
问题："所有员工的姓名是什么？"
计划：
1. 从 employees 表读取所有行
2. 仅提取 name 列（employees.name）
3. 返回所有员工姓名，无需过滤或分组
```

### 复杂聚合计划
```
问题："查询拥有至少5名员工的部门中平均薪资最高的前3个部门"

计划：
1. 通过 employees.dept_id = departments.id 连接 employees 与 departments
2. 按 departments.dept_name 对结果进行分组
3. 对每个分组计算 AVG(employees.salary) 和 COUNT(*)
4. 筛选 COUNT(*) >= 5 的分组（HAVING条件）
5. 按平均薪资降序排列符合条件的分组
6. 限制结果为前3个部门
7. 返回：dept_name、avg_salary
```

### 嵌套子查询计划
```
问题："查询薪资高于其所在部门平均薪资的员工"

计划：
1. 首先，以子查询方式计算每个部门的平均薪资：
   a. 按 dept_id 对 employees 进行分组
   b. 计算每个部门的 AVG(salary)
2. 然后，对每个员工，将其薪资与步骤1中其所在部门的平均值进行比较
3. 筛选薪资 > 部门平均值（来自子查询）的员工
4. 使用 dept_id 将子查询与外部查询关联
5. 返回：员工姓名、薪资、部门名称、部门平均薪资
```

## 最佳实践

- **始终使用CoT推理**：解释每个决策的WHY（原因），而不仅仅是WHAT（内容）
- **每个步骤都要编号**：程序化计划必须编号且按顺序排列
- **交叉引用子问题**：(C) 中的每个键都应映射到至少一个计划步骤
- **明确指定连接条件**：始终明确写出用于连接的具体外键列对
- **程序化地处理子查询**：将嵌套逻辑描述为步骤内的编号子步骤
- **保持计划精炼**：避免不必要的冗长描述——聚焦于执行逻辑

## 常见陷阱

| 陷阱 | 纠正方式 |
|------|---------|
| 在计划中生成了SQL | 这是#1违规——只写程序化步骤 |
| 跳过了CoT推理 | 在综合步骤之前始终先解释推理 |
| 子问题覆盖遗漏 | (C) 中的每个子句都必须在计划中出现 |
| 连接描述模糊 | 明确指定具体列对（如 `e.dept_id = d.id`） |
| 忽略了隐含需求 | "每X的平均值"即使未显式说明也隐含GROUP BY |
