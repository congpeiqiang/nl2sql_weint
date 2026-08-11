# 主智能体（Orchestrator）记忆

## 自动编排规则（强制执行）

数据查询类任务必须自动执行完整链路，无需用户额外要求：

1. **nl2sql 查询(必选)** — 委派 nl2sql 子智能体获取结构化数据
2. **推荐图表(可选)** — 使用图表工具分析数据特征，推荐最佳图表类型
3. **渲染图表(可选)** — 使用图表工具渲染可视化图表
4. **保存图表到文件(必选)** — 从图表工具结果中提取图表内容，保存到 `/workspace/report/{report-name}_chart.{ext}`
5. **report-export 生成报告** — 将数据表 + 图表引用 + 分析解读整合为 Markdown 报告

> **图表引擎说明：** 当前图表引擎由 `.env` 中的 `CHART_ENGINE` 控制（semiotic / echarts，二选一）。以下图表规范中，Semiotic 特有的内容仅当 `CHART_ENGINE=semiotic` 时适用。

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

**历史教训（2026-08-07）：** 生成报告时犯了两个错误：
1. **报告缺少图表引用** — 虽然图表已渲染并自动保存为 HTML 文件，但 Markdown 报告中没有用 `![图表说明](./xxx.html)` 引用图表文件。根因是生成报告时只写了文字描述，没有检查图表文件是否已保存并加入引用。
2. **文件名缺少时分秒** — 报告文件名写成了 `{name}_2026-08-07.md`，缺少 `_HH-mm-ss` 部分。规范要求 `{report-name}_{YYYY-MM-DD_HH-mm-ss}.md`。
3. **正确做法**：生成报告前先 `ls /workspace/report/` 确认图表文件已保存，然后在报告中用相对路径引用；文件名必须包含完整时间戳（通过 `python -c "from datetime import datetime; print(datetime.now().strftime('%Y-%m-%d_%H-%M-%S'))"` 获取）。

## 图表渲染规范（Semiotic 引擎专用）
> 仅当 `CHART_ENGINE=semiotic` 时适用。若 `CHART_ENGINE=echarts`，请参考 `prompt/chart_specs/echarts.md` 中的 ECharts 规范。

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

## 水平柱状图标签裁剪解决方案（Semiotic 引擎专用）
> 仅当 `CHART_ENGINE=semiotic` 时适用。ECharts 使用 `grid.left` 控制左边距。

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

## 图表保存必须调用 chart-saver 技能（2026-08-03 修正）

### 问题
自动编排链路中，渲染图表后保存 SVG 时，主智能体手动用 Python 脚本解码 base64 提取 SVG，而不是调用 chart-saver 技能。用户指出"为什么没有调用技能"。

### 根因
主智能体依赖 ECharts 的 `_save_svg_to_workspace` 自动保存机制，或手动写 Python 脚本，忽略了已创建的 chart-saver 技能。

### 强制流程（每次自动编排时必须执行）
```
Step 1: renderChart / generate_echarts 渲染图表
Step 2: 调用 chart-saver 技能（read_file SKILL.md → execute save_chart.py）保存图表
Step 3: 在 Markdown 报告中使用相对路径引用
```

### 正确做法
- ✅ 渲染图表后，**必须调用 chart-saver 技能**保存图表文件
- ✅ 使用 `python /workspace/skills/main/chart-saver/scripts/save_chart.py --content "<图表内容>" --name "<图表名>" --format svg`
- ✅ 保存后立即在报告中用相对路径引用

### 禁止行为
- ❌ 手动写 Python 脚本解码 base64 提取 SVG
- ❌ 仅依赖 ECharts 自动保存机制，不主动调用 chart-saver 技能

**历史教训：** 2026-08-03 生成电视剧系列数量统计报告时，保存图表 SVG 用了手动 Python 脚本，用户指出应调用 chart-saver 技能。后续所有图表保存必须走 chart-saver 技能。

## 图表保存到报告规范（Semiotic 引擎专用）
> 仅当 `CHART_ENGINE=semiotic` 时适用（SVG 格式）。ECharts 输出 PNG 图片，保存为 .png 文件。

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

### ⚠️ 保存 SVG 的两种可靠方式（2026-08-03 修正认知）

**重要修正：** 之前认为"`execute` 无法访问虚拟文件系统"是**不准确的**。真正的问题是**路径解析错误**。

**根因（已验证）：** 在宿主 shell 中运行 Python 时，`/workspace/report/` 会被解析为 `D:\workspace\report\`（当前盘符根目录），而**不是**虚拟路径映射的 `D:\code_work_space\llm\nl2sql\src\agent\workspace\report\`。所以文件写到了错误位置，虚拟文件系统看不到。

**验证结论：** 虚拟文件系统 `/workspace/report/` 就是宿主路径 `D:\code_work_space\llm\nl2sql\src\agent\workspace\report\` 的映射。只要用**正确的宿主绝对路径**写入，虚拟文件系统就能看到。

**方式一（推荐，最可靠）：用 `write_file` 工具**
- 从图表结果中提取 SVG 内容，直接用 `write_file` 工具写入 `/workspace/report/{chart-name}.svg`
- 无需关心路径映射，最稳妥

**方式二（可用）：用 `execute` + 宿主绝对路径**
- 在 Python 脚本中使用宿主绝对路径：`D:\code_work_space\llm\nl2sql\src\agent\workspace\report\{chart-name}.svg`
- 注意：**不要**用 `/workspace/report/`（会被解析为 `D:\workspace\report\`，错误位置）
- 验证命令：`python -c "from pathlib import Path; print(Path('/workspace/report').resolve())"` 会显示 `D:\workspace\report`（错误），需手动替换为宿主绝对路径

**历史教训：** 2026-07-31 保存 Top 10 电影评分图表时，用 `execute` 命令写 SVG 文件，虽然命令返回成功（"SVG saved"），但文件并未出现在 `/workspace/report/` 目录中。根因是 Python 将 `/workspace/report/` 解析为 `D:\workspace\report\`（错误位置），而非宿主映射路径。后续保存 SVG 优先用 `write_file` 工具，或用 `execute` 时务必使用宿主绝对路径。

**2026-08-03 补充验证（重要）：**
- **用 `execute` + 宿主绝对路径复制文件是可行的**：`shutil.copy2(r'D:\code_work_space\llm\nl2sql\src\agent\workspace\report\src.svg', r'D:\code_work_space\llm\nl2sql\src\agent\workspace\report\dst.svg')` 复制后，`dst.svg` 会正确出现在虚拟文件系统 `/workspace/report/` 中。
- **注意：`execute` 环境的 Python `print` 输出可能被吞**（Windows GBK 控制台问题），命令返回成功但看不到 print 输出。**不要依赖 print 判断执行结果**，应通过 `ls` 工具检查文件是否真实生成。
- **`execute` 环境是 cmd.exe**（不是 PowerShell），`Out-String` 等 PowerShell 命令会报错。
- **推荐优先级：** ① `write_file` 工具（最可靠，直接操作虚拟文件系统）→ ② `execute` + 宿主绝对路径（可行，但需用 `ls` 验证结果）。**避免**用 `/workspace/report/` 虚拟路径（会被解析到 `D:\workspace\report\` 错误位置）。

**历史教训：** 2026-07-30 生成 Top 10 电影评分报告时，只写了"图表：水平柱状图"的文字描述，没有保存 SVG 图表文件到报告目录，导致报告缺少可视化内容。后续所有自动编排链路中，renderChart 后必须将 SVG 保存为独立文件并在报告中引用。

## 查询超时处理规范（强制执行）

当子智能体的 run_sql 长时间无响应时：
1. **禁止主动取消任务**
2. **禁止主动重试或换方案**
3. **正确做法：** 向用户报告当前状态（已执行多久），询问用户是否要取消或缩小范围
4. 等用户明确给出指令后再行动

## ECharts MCP 环境配置（canvas 依赖解决方案）

echarts-mcp 依赖 `canvas`（原生模块），在 Windows 上安装失败的两个原因及解决方案：

### 问题
1. `prebuild-install` 下载预编译二进制**超时**（默认 npm 源网络问题）
2. 回退到 `node-gyp rebuild` 需要 **Visual Studio C++ 工具链**，但环境未安装

### 解决方案（已成功）
1. **配置 npm 镜像**：`npm config set registry https://registry.npmmirror.com`
2. **配置 canvas 二进制镜像**：在项目根目录创建 `.npmrc`，写入 `canvas_binary_host_mirror=https://npmmirror.com/mirrors/canvas`
3. **安装 canvas**：`npm install canvas@3.1.2 --no-save`
4. 验证：`node -e "const c=require('canvas'); console.log('OK')"`

### 验证 echarts-mcp 启动
- 启动命令：`npx -p echarts-mcp echarts-mcp`
- **注意**：Windows 上 `npx` 是 `.cmd` 文件，Python subprocess 调用必须用 `shell=True`，否则找不到命令
- 工具：`generate-echarts`（参数 width/height/echarts）
- 输出：PNG 文件路径（保存到用户 Downloads 目录）

### 关键经验
- `canvas_binary_host_mirror` 不是 npm config 选项，必须通过 `.npmrc` 文件或环境变量 `npm_config_canvas_binary_host_mirror` 设置
- 镜像配置后 canvas 安装从失败变为成功（9 秒完成）
- echarts-mcp 的 `generate-echarts` 返回 PNG 文件路径字符串，`_sanitize_echarts_result` 会将其转为 `<img>` 标签

## ECharts 图片保存到工作区的永久方案（跨环境安全）

### 问题
echarts-mcp 的 `generate-echarts` 工具硬编码将图片保存到用户 Downloads 目录（`C:\Users\<用户名>\Downloads`），无法通过参数配置。直接修改 echarts-mcp 源码的方案在新环境重新下载依赖包后会失效。

### 永久方案（已实施，2026-08-01）
**不修改 echarts-mcp 源码，在主智能体侧自动移动图片到工作区。**

在 `/utils/path_resolver.py` 的 `_sanitize_echarts_result` 函数中：
1. 新增 `_move_echarts_image_to_workspace(src_path)` 辅助函数
2. 当检测到 echarts 返回的图片路径时，自动用 `shutil.copy2` 把图片从 Downloads 复制到 `/workspace/report/` 目录
3. 返回工作区路径的 `<img>` 标签

### 为什么跨环境安全
- echarts-mcp 源码**不动**（保存到 Downloads 的行为保留）
- 移动逻辑在主智能体项目代码里（`path_resolver.py`），属于项目自身代码，新环境克隆项目后保留
- 图片统一收拢到 `/workspace/report/`，方便管理

### 关键点
- 宿主路径映射：`/workspace/` → `D:\code_work_space\llm\nl2sql\src\agent\workspace\`
- `_move_echarts_image_to_workspace` 用 `Path` + `shutil.copy2` 操作文件系统（Python 进程内，非 execute shell）
- 复制失败时回退到原路径，不影响图表显示

## ECharts SVG 自动保存到工作区的永久方案（已实施，2026-08-03）

### 问题
ECharts 引擎 `outputType="svg"` 时返回 SVG 字符串，`_sanitize_echarts_result` 只将其转为内嵌 iframe 显示，**不会自动保存 SVG 文件**。导致报告（report-export）无法引用图表文件，需手动用 `write_file` 保存。

### 永久方案（已实施）
在 `/utils/path_resolver.py` 中：
1. 新增 `_save_svg_to_workspace(svg)` 辅助函数：
   - 提取 `<svg>...</svg>` 完整内容
   - 尝试从 SVG 内 `<text>` 标题提取图表名（清理非法文件名字符），否则用 `echarts` 默认名
   - 文件名格式：`{图表名}_{YYYYMMDD_HHMMSS}.svg`
   - 用 `Path.write_text` 写入 `WORKSPACE_DIR / "report"` 目录
   - 返回文件名；失败返回空字符串
2. 在 `_sanitize_echarts_result` 的**所有 6 处 SVG 处理分支**（dict content text、dict content joined、list text、list joined、str、tuple）中，转 iframe 前先调用 `_save_svg_to_workspace`

### 效果
- 每次生成 SVG 图表都会**自动落盘**到 `/workspace/report/`，报告可直接用相对路径引用
- 主智能体**无需再手动用 `write_file` 保存 SVG**
- 与 PNG 的 `_move_echarts_image_to_workspace` 机制对称，统一收拢图表到工作区

### 验证
- 语法检查通过（`ast.parse`）
- 实测 `_save_svg_to_workspace` 和 `_sanitize_echarts_result` 均正常：SVG 自动保存 + 返回 iframe
- 自动保存的文件在虚拟文件系统 `/workspace/report/` 中可见

### 注意
- 图表名从 SVG 内第一个 `<text>` 提取，若 SVG 无 `<text>` 则用 `echarts` 默认名
- 文件名含中文时在 Windows GBK 控制台显示乱码，但实际文件名是正确的 UTF-8，不影响使用

## LangSmith Trace 查询方法（2026-08-11）

### 背景
用户要求调用 `langsmith-trace` 技能查询任务耗时。`langsmith` CLI 无法安装（Windows 环境，`langsmith-cli` 包在镜像源不存在），改用 **Python langsmith SDK** 查询。

### 关键方法
1. **环境已配置**：`LANGSMITH_API_KEY` 已设置、`LANGSMITH_TRACING=true`、`LANGSMITH_PROJECT=nl2sql`
2. **项目列表**：`client.list_projects()` 列出所有项目。nl2sql 子智能体项目为 `studio::nl2sql_agent::9ac9c340` 和 `studio::nl2sql_agent::aa79fbc2`，但**当前子智能体任务的 trace 记录在 `nl2sql` 项目下**（与主智能体同项目）
3. **查询子智能体 trace**：子智能体任务的 trace_id 以任务 thread_id 前缀开头（如任务 019fee9a → trace_id `019fee9a-16a2-7903-8455-2badd2dc6e6c`）
4. **用 filter 查询**：`client.list_runs(project_name="nl2sql", filter='and(gte(start_time, "2026-08-11T02:00:00Z"))', limit=100)` 然后过滤 trace_id 前缀
5. **limit 上限 100**：`list_runs` 的 limit 最大 100，超过会报 400 错误
6. **`has` 比较器需要完整 trace_id**：`filter='and(has(trace_id, "019fee9a"))'` 会报错（需要完整 UUID），用 `eq` 配合完整 trace_id 或用 `gte(start_time)` 过滤后手动匹配前缀

### 分析耗时
- 子智能体 trace 中，`wrenai_run_sql` 工具调用是耗时大头（导演-演员组合任务 7 次 run_sql 共 701 秒）
- 单次最长 run_sql 300 秒（5 分钟），可能触发超时
- `ChatDeepSeek`（LLM 调用）单次约 3-5 秒，不是瓶颈
- 中间件包装（FilesystemMiddleware 等）透明，耗时与内部工具一致

### 注意
- Windows GBK 控制台 print 中文会出错被吞，分析结果写入文件再读取
- 工具结果去重：相同命令重复执行会返回"内容重复已省略"，需写入不同文件或用 read_file 读取

## 阿里云 CLI 安装（2026-08-03）

### 安装位置
- 可执行文件：`C:\aliyun-cli\aliyun.exe`（版本 3.4.11）
- 已添加到用户级 PATH（`[Environment]::SetEnvironmentVariable('Path', ..., 'User')`）

### 安装步骤（Windows）
1. 下载：`curl -o aliyun-cli.zip "https://aliyuncli.alicdn.com/aliyun-cli-windows-latest-amd64.zip"`
2. 解压：`powershell -Command "Expand-Archive -Path 'aliyun-cli.zip' -DestinationPath 'aliyun-cli' -Force"`
3. 复制到固定目录：`copy "aliyun-cli\aliyun.exe" "C:\aliyun-cli\aliyun.exe"`
4. 添加 PATH（用户级，无需管理员）：
   `powershell -Command "[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';C:\aliyun-cli', 'User')"`

### 注意事项
- 系统级 PATH 修改需要管理员权限（会报"不允许所请求的注册表访问权"），改用用户级 PATH
- 当前 shell 会话不会自动刷新 PATH，需用完整路径 `"C:\aliyun-cli\aliyun.exe"` 调用，或新开 shell
- 验证：`"C:\aliyun-cli\aliyun.exe" version` → 输出 `3.4.11`

### 使用前提
- 使用 alibabacloud-find-skills 技能前需先配置阿里云凭证（`aliyun configure`），否则会报认证错误

## 性能优化步骤在 write_todos 中独立显示（2026-08-03）

### 问题
子智能体执行策略A标准流水线时，虽然实际调用了 `nl2sql-performance-optimization` 技能，但在 `write_todos` 的进度列表中没有单独列出"性能优化"步骤，而是把它合并到了 sql-generation 步骤中。导致 `check_async_task` 返回的 steps 中看不到性能优化环节，用户误以为没调用该技能。

### 根因
`/prompt/NL2SQL_SYSTEM_PROMPT.md` 原则 9 的进度追踪示例中，todos 列表没有包含"性能优化"步骤。子智能体按照示例创建 todos，自然就没有单独列出。

### 解决方案（已实施）
修改 `/prompt/NL2SQL_SYSTEM_PROMPT.md` 原则 9：
1. 明确要求：write_todos 的每个 content 必须与流水线步骤一一对应，性能优化必须作为独立步骤列出，不得合并到 SQL 生成步骤中
2. 更新示例，加入"性能优化"步骤（策略A标准流水线共 8 个 todos：Knowledge Loader / Schema Linking / Subproblem分解 / Query Plan生成 / SQL生成与验证 / 性能优化 / 查询执行 / 结果汇总）
3. 注明：策略B（快速通道）不经过性能优化，可省略该步骤；策略A（标准流水线）必须包含

### 关键点
- steps 是通过子智能体的 `write_todos` 生成的，主智能体无法直接控制
- 要修改 steps 显示，必须修改子智能体的系统提示词（NL2SQL_SYSTEM_PROMPT.md）
- AGENTS.md 技能清单已包含 performance-optimization（Step 5.5），无需修改

## SQL 性能优化技能调研（2026-08-03）

### 背景
用户需求：在 nl2sql 过程编排（sql-of-thought 技能流）中新增 SQL 性能优化技能环节，插入在 `nl2sql-correction` 之前。

### 工具调研结论
- **sqlglot 30.12.0**（已安装，被 wrenai 依赖）：32 方言、解析/转译/优化器可用。性能检测 8 类规则全部实测通过（SELECT *、无 LIMIT、JOIN 无 ON、子查询、函数包裹列、NOT IN、DISTINCT、DELETE/UPDATE 无 WHERE）
- **sqlfluff 4.2.2**（已安装，官方源）：28 方言、Linter 可用。性能相关规则：AM08（隐式交叉连接）、AM09（LIMIT 无 ORDER BY）、RF02（未限定 SELECT *）、AM05（JOIN 未限定）

### 关键架构约束（重要！）
**nl2sql 子智能体无法运行 Python 脚本。**
- 子智能体用 `FilesystemBackend`，只实现 `BackendProtocol`（ls/read/write/edit/grep/glob），**不实现 `SandboxBackendProtocol`**
- 因此 `execute` 工具会返回错误，无法运行 Python 调用 sqlglot/sqlfluff
- 子智能体工具集 = WrenAI MCP 工具 + 文件工具，无 execute/Python

### 技能设计方案（推荐：混合方案 C）
设计 `nl2sql-performance-optimization` 技能：
- SKILL.md 指导 LLM 基于规则集手动检查 SQL
- references/performance-rules.md 提供完整规则集（来自 sqlglot/sqlfluff 验证）
- 插入点：sql-generation 生成 SQL 并 dry_run 成功后、run_sql 执行前（Step 5.5）
- 定位区分：correction=执行失败纠错（正确性）；performance-optimization=执行成功但可优化（性能）

### 报告位置
`/workspace/report/SQL性能优化技能调研报告_2026-08-03.md`

### 技能已实施（方案C，2026-08-03）
创建 `nl2sql-performance-optimization` 技能：
- 位置：`/workspace/skills/nl2sql/nl2sql-performance-optimization/`
- `SKILL.md`：触发条件、流程、10 类规则速查
- `references/performance-rules.md`：完整规则集（10 类，含严重度分级）
- 插入点：sql-of-thought 策略A流水线 Step 5.5（sql-generation 后、run_sql 前）
- 已更新 `sql-of-thought/SKILL.md`（Step 5.5 + 流程图）
- 已更新 `/memory/AGENTS.md` 技能清单
- 已验证：SkillsMiddleware 正确发现并解析该技能（name/description/path 均正确）

## chart-saver 技能（已创建，2026-08-03）

### 概述
创建了 `chart-saver` 技能，绑定主智能体，用于将图表工具生成的图表内容保存为独立文件到工作区报告目录。避免每次保存图表时手动用 Python 解码 base64 获取 SVG 内容。

### 技能位置
- 技能目录：`/workspace/skills/main/chart-saver/`
- `SKILL.md`：技能说明（触发条件、工作流、命名规则、错误处理）
- `scripts/save_chart.py`：辅助脚本

### 触发条件
用户说"保存图表"、"保存 SVG"、"保存图片"、"把图表存到文件"、"保存图表文件"、"save chart"、"save svg"、"保存可视化结果"等。

### save_chart.py 用法
```bash
python save_chart.py --content "<图表内容>" --name "IMDb_Movie_Genres_chart" [--format svg|base64|png] [--dir /workspace/report/]
```

支持三种输入格式（自动检测）：
1. **SVG 字符串**：直接提取 `<svg>...</svg>` 保存
2. **base64 HTML iframe**：解码 base64 → 提取 SVG → 保存
3. **PNG 文件路径**：复制文件到工作区报告目录

### 路径映射（关键）
- 脚本内部用宿主绝对路径写入：`_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent.parent.parent`（scripts→chart-saver→main→skills→workspace→项目根）
- `_WORKSPACE_REPORT_DIR = _PROJECT_ROOT / "workspace" / "report"`
- 若传入 `/workspace/report/` 虚拟路径，脚本自动映射到宿主绝对路径

### 主智能体绑定
- 已在 `main_agent.py` 的 `SkillsMiddleware` sources 中添加 `"/workspace/skills/main/chart-saver/"`
- 验证：`_list_skills(fb, "/workspace/skills/main/")` 返回 3 个技能（alibabacloud-find-skills、chart-saver、report-export）

### 技能目录配置（2026-08-03 优化为根目录方式）
- **主智能体**：`main_agent.py` 的 `SkillsMiddleware` sources 改为 `["/workspace/skills/main/"]`（指向根目录，自动发现所有技能）
- **nl2sql 智能体**：`nl2sql_agent.py` 的 sources 为 `["/workspace/skills/nl2sql/"]`（已是根目录方式）
- **好处**：以后在 `/workspace/skills/main/` 下新增技能子目录（含 SKILL.md），**无需改代码**，配合新会话即可生效
- **注意**：`skills_metadata` 会被 checkpoint 持久化，`before_agent` 只在会话首次执行时加载技能。因此增减技能后仍需**新会话**（或重启服务）才生效，但无需改代码
- 验证：`/workspace/skills/main/` 发现 3 技能，`/workspace/skills/nl2sql/` 发现 7 技能

### 验证结果
- 技能被 SkillsMiddleware 正确发现和解析（name=chart-saver，description 正确）
- save_chart.py 三种格式均正常保存，虚拟文件系统可见
- 注意：Windows GBK 控制台 print 中文会出错被吞（脚本输出无显示但文件实际保存成功），验证时用写文件方式而非 print