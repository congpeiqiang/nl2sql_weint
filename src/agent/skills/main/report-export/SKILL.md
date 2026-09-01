---
name: report-export
description: >
  将数据分析结果导出为 Markdown 格式报告文件。触发条件包括："下载报告"、
  "导出为 Markdown"、"生成分析报告"、"保存结果"、"下载分析结果"、
  "把结果写成文件"、"生成 md 文件"、"export report"、"save as markdown"、
  "下载 markdown"、"生成报告文档"。
---

# Report Export — Markdown 报告导出技能

## 概述

本技能负责将数据分析查询结果整理为结构化的 Markdown 报告，并写入本地文件系统，方便用户下载和使用。

## 触发条件

当用户表达以下意图时激活本技能：

- "下载报告" / "下载分析结果"
- "导出为 Markdown" / "导出为 md"
- "生成分析报告" / "生成报告文档"
- "保存结果到文件"
- "把结果写成 markdown 文件"
- "export report" / "save as markdown"
- "下载 markdown"

## 核心工作流

### Step 1: 获取数据

从当前对话上下文或子智能体（如 nl2sql）的查询结果中获取需要导出的数据。

数据来源可以是：
- 当前对话中已返回的查询结果
- 用户直接提供的文本内容
- 之前子智能体执行完成的结果

### Step 2: 整理为 Markdown 格式

根据数据类型选择合适的 Markdown 模板：

#### 模板 A：数据查询报告

```markdown
# {标题}

## 查询概述
{查询目的描述}

## 数据结果
| 列1 | 列2 | 列3 |
|-----|-----|-----|
| {v1} | {v2} | {v3} |

## 生成SQL
{生成的SQL}

## 关键发现
- {发现1}
- {发现2}
```

#### 模板 B：分析报告

```markdown
# {标题}

## 背景
{分析背景}

## 分析结果
{分析内容}

## 生成SQL
{生成的SQL}

## 结论与建议
{结论}
```

#### 模板 C：图表报告

```markdown
# {标题}

## 数据概览
{数据摘要}

## 图表说明
{图表类型和解读}

## 生成SQL
{生成的SQL}

## 详细数据
| 维度 | 指标 |
|------|------|
| {d1} | {m1} |

## 流程追溯
- Phase 1: (Knowledge Loader): [业务知识] → [LLM 调用次数]
- Step 2 (Schema Linking): [使用的表] → [LLM 调用次数]
- Step 3 (Subproblem): [识别到的子句] → [LLM 调用次数]
- Step 4 (Query Plan): [计划步骤数] → [LLM 调用次数]
- Step 5 (SQL Generation): [生成+后处理] → [LLM 调用次数]
- Step 6 (Execute): 成功

## 统计
- 总 LLM 调用次数: N
- 是否进入纠错循环: 否
```

#### 图表引用（若本次任务生成了 echarts 交互式图表）

`generate_echarts`（outputType=option）会把图表保存为 `.html` 文件到 `/workspace/report/` 目录，并在**工具返回结果中直接给出可交互的 iframe**（`<iframe src="data:text/html;base64,...">`）。导出报告时，**附录必须把该 iframe 原样内嵌进报告**，禁止只写文字描述（如"已渲染为交互式 HTML 图表"）：

```markdown
### 图表
{generate_echarts 工具返回的完整 <iframe> 标签，原样粘贴、一字不改}
```

> 💡 可交互图表：鼠标悬停查看数值、可缩放。
> 图表文件（可分享）：/workspace/report/<图表文件名>.html
```

- **iframe 必须原样复制** generate_echarts 工具返回结果中的 `<iframe ...>` 完整标签（含 src 的 base64 和 height，禁止自己手写、禁止只取 src 或改写 height），一字不改。
- 若工具返回的是 `<img>`（png）或 SVG，说明没用 `outputType=option`，需用 `outputType=option` 重跑一次。
- `<图表文件名>` 取自 generate_echarts 返回结果中保存的 HTML 文件名。
- 聊天消息里**不要**再写相对路径图片 `![...](./xxx.html)`，也**不要**把 iframe 写进聊天正文——前端会自动把该轮交互图表附随到报告消息上方。

### Step 3: 写入文件

**首选：使用 `build_report` 工具程序化装配报告**——它会自动从当前对话提取最近一次
`check_async_task` 成功的数据结果（数据表 + SQL + 洞察）与 `generate_echarts` 生成的
交互式图表（内嵌 iframe），自动加「精确到时分秒」的时间戳文件名并落盘到
`/workspace/report/`。模型只需提供报告标题与解读文本，无需手工搬运表格/iframe：

```
build_report(report_name="各类型电影数量分布", analysis="各类型电影中，喜剧类数量最多……")
```

**若需完全自定义报告内容**，再退回 `write_file` 工具将 Markdown 内容写入文件系统。

**文件命名规则：**
- 文件名：`{report-name}_{YYYY-MM-DD_HH-mm-ss}.md`
  - 在文件路径里直接写 `{ts}` 占位符，系统会自动展开为当前本地时间（精确到时分秒），
    同一文件名的 {ts} 在同一任务内固定不变，写完即可 read 校验
  - 示例：`/workspace/report/各类型电影数量分布_{ts}.md` → `各类型电影数量分布_2026-07-30_13-56-27.md`
- 存放路径：`/workspace/report/` 目录下
- 如果用户指定了文件名，优先使用用户指定的名称

### Step 4: 告知用户

向用户报告文件已生成，包括：
- 文件路径
- 文件内容概要
- 如何使用该文件

## 输出格式规范

### 报告结构

​```markdown
# 报告标题

> 生成时间：{timestamp}
> 数据来源：{source}

## 1. 概述
{简要说明}

## 2. 核心数据
{表格或列表}

## 3.生成SQL
{生成的SQL}

## 4. 分析解读
{分析内容}

## 5. 附录
{原始数据、SQL 等}

若本次任务生成了 echarts 交互式图表，附录**必须**内嵌 generate_echarts 返回的可交互 iframe：

### 图表
{generate_echarts 工具返回的完整 <iframe> 标签，原样粘贴、一字不改}

> 💡 可交互图表：鼠标悬停查看数值、可缩放。
> 图表文件（可分享）：/workspace/report/<图表文件名>.html

- **iframe 必须原样复制** generate_echarts 工具返回的 `<iframe ...>` 完整标签（含 src 的 base64 和 height，禁止只取 src 或改写 height）。
- 若得到的是 `<img>`（png）或 SVG，说明没用 `outputType=option`，需用 `outputType=option` 重跑。
- `<图表文件名>` 取自 generate_echarts 结果中保存的 HTML 文件名。
- 禁止只写文字描述（如"已渲染为交互式 HTML 图表"）。
```

### Markdown 格式要求

| 元素 | 规范 |
|------|------|
| 标题 | `#` 一级、`##` 二级、`###` 三级 |
| 表格 | 使用标准 GFM 表格语法 |
| 代码块 | SQL 用 ` ```sql `，其他用 ` ``` ` |
| 列表 | 无序列表用 `-`，有序列表用 `1.` |
| 强调 | 重要内容用 **加粗** |
| 引用 | 说明性文字用 `> ` 引用块 |

## 示例

### 用户说："把刚才的查询结果下载为 markdown"

```
1. 获取对话中最近的查询结果数据
2. 整理为 Markdown 格式报告
3. 写入 /workspace/report/analysis_2024-01-01.md
4. 告知用户文件已生成
```

### 用户说："生成一份黄金期分析报告"

```
1. 从 nl2sql 子智能体获取黄金期分析数据
2. 使用分析报告模板整理内容
3. 写入 /workspace/report/黄金期分析报告_2024-01-01.md
4. 告知用户文件路径和内容概要
```

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| 没有可导出的数据 | 提示用户先执行查询 |
| 文件写入失败 | 检查目录权限，尝试备用路径 |
| 用户指定了不合法文件名 | 自动替换特殊字符，使用安全文件名 |

## 最佳实践

1. **先确认数据** — 导出前确认用户要导出的是哪部分数据
2. **结构化呈现** — 使用标题层级、表格、代码块让报告清晰易读
3. **包含元信息** — 生成时间、数据来源、SQL 语句等
4. **中英文兼容** — 报告语言与用户提问语言保持一致
5. **文件路径告知** — 始终告知用户文件存放的完整路径
