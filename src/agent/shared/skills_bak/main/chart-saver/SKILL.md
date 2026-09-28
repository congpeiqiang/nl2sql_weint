---
version: 0.1.0
name: chart-saver
description: >
  保存图表到工作区报告目录。触发条件包括："保存图表"、"保存 SVG"、"保存图片"、
  "把图表存到文件"、"保存图表文件"、"save chart"、"save svg"、"保存可视化结果"。
  当需要将图表工具（generate_echarts / renderChart）生成的图表内容保存为独立文件时激活本技能。
---

# Chart Saver — 图表保存技能

## 概述

本技能负责将图表工具（ECharts `generate_echarts`）生成的图表内容保存为独立文件到工作区报告目录 `/workspace/report/`，方便报告引用和用户下载。

**核心价值：** 无需每次手动写 Python 代码解码 base64 / 提取 SVG，直接调用封装好的辅助脚本即可。

## 触发条件

当用户表达以下意图时激活本技能：

- "保存图表" / "保存 SVG" / "保存图片"
- "把图表存到文件" / "保存图表文件"
- "save chart" / "save svg" / "save image"
- "保存可视化结果" / "导出图表"
- 自动编排链路中，渲染图表后需要将图表落盘时

## 核心工作流

### Step 1: 获取图表内容

从图表工具（`generate_echarts` / `renderChart`）的返回结果中获取图表内容。图表内容可能有三种格式：

| 格式 | 来源 | 示例 |
|------|------|------|
| **SVG 字符串** | ECharts `outputType="svg"` | `<svg xmlns="...">...</svg>` |
| **base64 HTML iframe** | ECharts 交互式 HTML | `<iframe src="data:text/html;base64,...">` |
| **PNG 文件路径** | ECharts `outputType="png"` | `C:\Users\xxx\Downloads\uuid.png` |

### Step 2: 调用辅助脚本保存

使用 `execute` 工具调用辅助脚本 `save_chart.py`，传入图表内容和目标文件名：

```bash
python /shared/skills/main/chart-saver/scripts/save_chart.py \
  --content "<图表内容>" \
  --name "IMDb_Movie_Genres_chart" \
  --format svg
```

**参数说明：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--content` | 是 | 图表内容（SVG 字符串 / base64 iframe / 文件路径） |
| `--name` | 是 | **文件基名**（不含扩展名、不含时间戳），如 `IMDb_Movie_Genres_chart` |
| `--format` | 否 | 输出格式：`svg`（默认）/ `png`。自动检测时可不填 |
| `--dir` | 否 | 保存目录，默认 `/workspace/report/` |

### Step 3: 确认保存结果

脚本输出 `✅ 图表已保存: <宿主绝对路径>`。**以该输出里的文件名为准**——脚本会在基名后
自动追加 `_{时间戳}_{4位随机}` 保证唯一（report/ 是全站共享目录，两个用户对同名主题出图
必须各存各的，不能互相覆盖）。**不要自己用基名拼文件名**。

### Step 4: 在报告中引用

在 Markdown 报告中使用**相对路径**引用图表（仅限 SVG / PNG 静态图），文件名取 Step 3
输出里的**真实文件名**：

```markdown
<!-- 假设脚本返回 …/report/Sales_Trend_20260923_101530_a3f1.svg -->
![销售趋势](./Sales_Trend_20260923_101530_a3f1.svg)
```

> ⚠️ **交互式 echarts 图表（generate_echarts + `outputType=option`）不要**用本 skill 保存或在本 skill 中用相对路径引用——`generate_echarts` 已自动保存 `.html` 并在返回结果中给出可交互 iframe，报告附录由 **report-export** 技能内嵌该 iframe（`<iframe src="data:text/html;base64,...">`），不要写 `![...](.html)`。

## 辅助脚本说明

辅助脚本 `save_chart.py` 封装了以下逻辑（无需手动编写）：

1. **SVG 字符串**：直接提取 `<svg>...</svg>` 内容保存
2. **base64 HTML iframe**：解码 base64 → 提取 `<svg>...</svg>` → 保存
3. **PNG 文件路径**：复制文件到工作区报告目录
4. **自动检测格式**：根据内容自动判断是 SVG / base64 / 文件路径
5. **唯一文件名**：基名后追加时间戳 + 随机后缀并**独占创建**，并发下也不会互相覆盖

## 文件命名规则

文件名由脚本生成，调用方只提供基名：

| 文件类型 | 命名格式 | 示例 |
|---------|---------|------|
| 图表 SVG | `{基名}_{YYYYMMDD_HHMMSS}_{4位随机}.svg` | `IMDb_Movie_Genres_chart_20260923_101530_a3f1.svg` |
| 图表 PNG | `{基名}_{YYYYMMDD_HHMMSS}_{4位随机}.png` | `IMDb_Movie_Genres_chart_20260923_101530_a3f1.png` |

后缀不是装饰：`report/` 是**全站共享目录**，同名主题（"销售趋势"）在不同用户/会话里
天然同名——不带后缀就是后写的人静默覆盖前一个人的图。引用图表时一律用脚本输出里的
真实文件名。

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| 图表内容为空 | 提示用户先渲染图表 |
| 无法识别图表格式 | 提示用户提供 SVG 字符串或文件路径 |
| 保存目录不存在 | 脚本自动创建目录 |
| 文件名含非法字符 | 脚本自动替换为安全字符 |


## 最佳实践

1. **优先使用 SVG 格式** — SVG 矢量图清晰且可被 Markdown 引用
2. **文件名语义化** — 使用能反映图表内容的名称，如 `Top10_Movies_by_Average_Rating_chart`
3. **保存后立即引用** — 保存后立即在报告中用相对路径引用，避免遗漏
4. **与 report-export 技能配合** — 保存图表后，用 report-export 技能生成包含图表引用的报告

