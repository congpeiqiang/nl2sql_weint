# 主智能体（Orchestrator）记忆

## ECharts 引擎 SVG 输出支持（2026-08-01 完成）

### 核心结论
echarts-mcp 的 `generate_echarts` 工具**原生支持 `outputType` 参数**，设置 `outputType="svg"` 时返回 **SVG 字符串**（与 Semiotic 引擎一致），可被正常渲染为内嵌 iframe。

### echarts-mcp 关键事实
- **包名**：`mcp-echarts`（不是 echarts-mcp），本地全局安装于 `C:\Users\congpeiqiang\AppData\Roaming\npm\node_modules\mcp-echarts`
- **bin 命令**：`mcp-echarts`（直接调用，避免 `npx -p mcp-echarts` 下载最新版 0.7.1）
- **工具名**：`generate_echarts`（下划线，不是连字符）
- **参数**：`echartsOption`（JSON 字符串）、`width`、`height`、`theme`、`outputType`
- **outputType 取值**：`svg`（返回 SVG 字符串）/ `png`（返回 PNG）/ `option`（返回配置）
- **返回格式**：MCP 标准响应 `{content: [{type:"text", text:"<svg>..."}]}`（svg）或 `{content: [{type:"image", data:base64, mimeType:"image/png"}]}`（png）

### 已完成的代码修改
1. **`prompt/chart_specs/echarts.md`**：工具名改为 `generate_echarts`，参数名改为 `echartsOption`，强制要求 `outputType="svg"`
2. **`utils/path_resolver.py`**：
   - `_is_chart_tool` 关键词加入 `"echarts"`（识别 `generate_echarts`）
   - `_pack_chart_props` echarts 分支：参数名归一化（`echarts`→`echartsOption`）+ 强制 `outputType="svg"`
   - `_sanitize_echarts_result`：新增 `_svg_to_data_url` 复用逻辑，支持 SVG 字符串转 iframe；新增 MCP 标准响应格式处理（`{content:[{type:"text",text:svg}]}` 和 `{content:[{type:"image",...}]}`）
3. **`tools/mcp_tool.py`**：`_get_main_server_config()` echarts 分支命令改为 `mcp-echarts`（直接调用本地 bin，避免 npx 下载）
4. **`settings/setting.py`**：`load_dotenv(override=True)`，确保 `.env` 文件优先
5. **`.env`**：创建并设置 `CHART_ENGINE=echarts`

### 关键经验
- echarts-mcp 的 SVG 渲染用 `echarts.init(null, theme, {renderer:"svg", ssr:true})` + `chart.renderToSVGString()`
- echarts-mcp 的 PNG 渲染用 `@napi-rs/canvas`，返回 Base64 或 MinIO URL
- `_sanitize_echarts_result` 需同时处理：纯字符串 SVG、MCP 标准响应格式、PNG 文件路径、data URL、http URL

## 自动编排规则（强制执行）

数据查询类任务必须自动执行完整链路，无需用户额外要求：

1. **nl2sql 查询(必选)** — 委派 nl2sql 子智能体获取结构化数据
2. **suggestChart 推荐图表(可选)** — 用 suggestChart 工具分析数据特征，推荐最佳图表类型
3. **renderChart 渲染图表(可选)** — 用 renderChart 工具渲染可视化图表
4. **保存 SVG 到文件(必选)** — 从 renderChart 结果中提取 SVG，保存到 `/workspace/report/{report-name}_chart.svg`
5. **report-export 生成报告** — 将数据表 + 图表(SVG引用) + 分析解读整合为 Markdown 报告

### 执行时机（关键！）
- 当 nl2sql 子智能体返回结果后，**立即自动执行步骤 2~5**
- **禁止等待用户额外要求**（如"画个图"、"导出报告"）
- 如果用户只问了查询，没有说"画图"或"导出"，**仍然必须自动执行完整链路**

### 禁止行为
- ❌ 查询完只展示数据表格，不触发图表和报告
- ❌ 等用户说了"画图"才去渲染图表
- ❌ 等用户说了"导出"才去生成报告
- ❌ renderChart 后不保存 SVG 就直接生成报告（报告会缺少图表）

### 正确做法
- ✅ nl2sql 返回结果 → 立即 suggestChart → renderChart → 保存 SVG → 生成报告（含图表引用）
- ✅ 在最终回复中同时呈现：数据表 + 图表 + 报告路径

**历史教训：** 2026-07-30 用户查询各表数据量后，只展示了数据表格，没有自动触发图表和报告，导致用户追问"为什么没有自动触发"。根因是认为用户没明确要求就可以跳过。后续所有数据查询类任务必须无条件执行完整链路。

## 图表渲染规范（强制执行）

### 渲染前必查 Schema
调用 `renderChart` 前，**必须先调 `getSchema(component)`** 查看该组件实际支持的属性列表，禁止凭印象或猜测传参。

### 轴标签属性对照表（各组件不一致，渲染前必须查 Schema）

| 图表组件 | 轴标签属性 | 说明 |
|---------|-----------|------|
| **BarChart** | `categoryLabel`（分类轴）、`valueLabel`（数值轴） | 水平/垂直通用 |
| **LineChart** | `xLabel`（X轴）、`yLabel`（Y轴） | 与 BarChart 属性名不同！ |
| **其他图表** | 以 `getSchema` 返回为准 | **禁止猜测属性名** |

### 强制流程（渲染图表三步走）

```
Step 1: getSchema(component)       ← 查该组件实际支持的属性
Step 2: 从 schema 中找出轴标签属性  ← 不同组件属性名不同
Step 3: renderChart(component, props)  ← 用正确的属性名传参
```

### 常见错误
- ❌ `xAxisLabel` / `yAxisLabel` — BarChart 和 LineChart 都不支持
- ❌ 假设所有图表用同一套轴标签属性名 — 每个组件 schema 不同
- ✅ 正确做法：先 `getSchema`，再用 schema 中列出的属性

**历史教训：** 2026-07-30 渲染各表数据量柱状图时，传了 `xAxisLabel` / `yAxisLabel` 导致轴标签被忽略。原因是未先查 Schema 就凭猜测传参。后续所有图表渲染必须先用 `getSchema` 确认属性。

## 取消任务规则（强制执行）

**禁止主动调用 `cancel_async_task`**，除非用户明确要求取消。

即使遇到以下情况，也不得主动取消：
- run_sql 执行超时或长时间无响应
- 子智能体报错
- 你认为需要重试或换方案

**正确做法：** 向用户报告当前状态，等待用户指令。

**允许取消的唯一条件：** 用户说"取消"、"停掉"、"不要了"、"终止"等明确取消意图时，才可以调用 `cancel_async_task`。

**历史教训：** 2026-07-30 用户查询各表行数时，SQL 执行超时（3分多钟），我主动取消了任务。用户指出"不能自己取消"，违反了规则。根因是认为超时就可以自作主张取消。后续必须严格遵守——只有用户明确说取消才能取消。

## 水平柱状图标签裁剪解决方案（强制执行）

### 问题
水平柱状图（BarChart, orientation="horizontal"）的 Y 轴标签（电影标题等长文本）如果过长，会被默认的左边距裁剪。

### 根本原因
Semiotic 的 `margin.left` 默认值较小（约 70px），而长文本标签（如 "The Lord of the Rings: The Return of the King" 39字符）需要 300px+ 才能完整显示。

### 强制流程（每次渲染水平柱状图时必须执行）

```
Step 1: 遍历数据，找出 categoryAccessor 对应的最长字符串
Step 2: 按字符长度计算 margin.left 和 width
Step 3: 在 props 中显式设置 margin.left 和 width
```

### 经验值对照表

| 最长标签长度 | margin.left | 画布宽度 |
|:---:|:---:|:---:|
| ≤ 10 字符 | 100 | 600 |
| 11~15 字符 | 150 | 700 |
| 16~20 字符 | 200 | 800 |
| 21~25 字符 | 250 | 900 |
| > 25 字符 | 300 | 1000 |

### 示例代码

```javascript
// 数据中 categoryAccessor="title"，最长 title 为 "The Lord of the Rings: The Return of the King"（39字符）
// → margin.left=300, width=1000

renderChart({
  component: "BarChart",
  props: {
    data: [...],
    categoryAccessor: "title",
    valueAccessor: "average_rating",
    orientation: "horizontal",
    margin: { left: 300, bottom: 50, top: 50, right: 30 },
    width: 1000,
    height: 500
  }
})
```

### 备选方案
如果标签 > 30 字符且不想用大画布：
- **缩写标签**：在数据预处理阶段将长名映射为缩写
- **改用垂直柱状图**：标签在 X 轴，通过 `xAxisRotate` 旋转显示

**历史教训：** 2026-07-30 渲染 Top 10 电影评分柱状图时，Y 轴电影标题被裁剪。根因是未考虑水平柱状图左边距默认值过小的问题。后续所有水平柱状图渲染必须按此规范计算动态边距。

## 图表保存到报告规范（强制执行）

### 问题
自动编排链路中，renderChart 渲染图表后，报告（report-export）中只写了文字描述"图表：水平柱状图"，没有实际保存图表文件，导致报告缺少可视化内容。

### 根本原因
renderChart 返回的是 HTML iframe 或 SVG 内容，但报告生成步骤没有将 SVG 提取并保存为独立文件，也没有在 Markdown 中引用。

### 强制流程（每次自动编排时必须执行）

```
Step 1: renderChart 渲染图表（获取 SVG 内容）
Step 2: 将 SVG 内容保存到 /workspace/report/{chart-name}.svg
Step 3: 在 Markdown 报告中使用相对路径引用：![图表说明](./{chart-name}.svg)
```

### 文件命名规则

| 文件类型 | 命名格式 | 示例 |
|---------|---------|------|
| 图表 SVG | `{report-name}_chart.svg` | `Top10_Movies_by_Average_Rating_chart.svg` |
| 报告 MD | `{report-name}_{YYYY-MM-DD_HH-mm-ss}.md` | `Top10_Movies_by_Average_Rating_2026-07-30_16-43-08.md` |

### 图表引用路径规范

- Markdown 中图片路径使用**相对路径**：`![图表说明](./{chart-name}.svg)`
- ❌ 禁止使用绝对路径：`/workspace/report/{chart-name}.svg`
- ✅ 正确：`![Top 10 Movies by Average Rating](./Top10_Movies_by_Average_Rating_chart.svg)`

### 提取 SVG 的方法

renderChart 返回的是 HTML iframe（data:text/html;base64,...），需要从中提取 SVG 标签内容：

1. 从 base64 data URL 中解码 HTML
2. 提取 `<svg>...</svg>` 标签内容
3. 保存为 `.svg` 文件到 `/workspace/report/` 目录

### ⚠️ 保存 SVG 必须用 write_file 工具（关键！）

**禁止用 `execute` 命令保存 SVG 文件！**

- `execute` 运行在**宿主 shell**，无法访问虚拟文件系统 `/workspace/report/`
- 用 `execute` 写 `/workspace/report/xxx.svg` 实际写到了宿主机的 `D:\code_work_space\llm\nl2sql\src\agent\workspace\report\xxx.svg`，虚拟文件系统中看不到
- **正确做法：** 从 renderChart 返回的 HTML 中提取 SVG 内容，直接用 `write_file` 工具写入 `/workspace/report/{chart-name}.svg`
- 报告 `.md` 文件用 `write_file` 保存是正常的，SVG 图表也必须用 `write_file` 保存

**历史教训：** 2026-07-31 保存 Top 10 电影评分图表时，用 `execute` 命令写 SVG 文件，虽然命令返回成功（"SVG saved"），但文件并未出现在 `/workspace/report/` 目录中。根因是 `execute` 运行在宿主 shell，无法访问虚拟文件系统。后续所有 SVG 图表保存必须用 `write_file` 工具。

**历史教训：** 2026-07-30 生成 Top 10 电影评分报告时，只写了"图表：水平柱状图"的文字描述，没有保存 SVG 图表文件到报告目录，导致报告缺少可视化内容。后续所有自动编排链路中，renderChart 后必须将 SVG 保存为独立文件并在报告中引用。

## 查询超时处理规范（强制执行）

当子智能体的 run_sql 长时间无响应时：
1. **禁止主动取消任务**
2. **禁止主动重试或换方案**
3. **正确做法：** 向用户报告当前状态（已执行多久），询问用户是否要取消或缩小范围
4. 等用户明确给出指令后再行动
