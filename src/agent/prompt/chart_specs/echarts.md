### 图表引擎：ECharts

**核心工具：**
- `generate_echarts` — 根据 ECharts 配置生成图表（注意：工具名是下划线 `generate_echarts`，不是连字符）

**调用 generate_echarts 工具，参数说明：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `width` | number | 图表宽度（像素），如 1000 |
| `height` | number | 图表高度（像素），如 500 |
| `echartsOption` | string | ECharts 配置对象（JSON 字符串） |
| `outputType` | string | 输出类型：`option`（推荐，返回 ECharts 配置 JSON，系统自动渲染为**可交互** HTML 图表）/ `svg`（返回静态 SVG 字符串）/ `png`（返回 PNG 图片） |

> ✅ **推荐设置 `outputType="option"`**：返回 ECharts 配置 JSON，系统自动包装为**交互式 HTML** 图表（内联 echarts.js），支持 tooltip 悬停、缩放、图例切换等交互，并可保存为 `.html` 文件。

**正确调用格式（推荐 outputType="option"）：**

**柱状图：**
```
generate_echarts: width=1000, height=500, outputType=option, echartsOption={backgroundColor:'#fff', title:{text:'分布'}, tooltip:{}, legend:{data:['Sales']}, xAxis:{data:['A','B','C']}, yAxis:{}, series:[{name:'Sales', type:'bar', data:[10,20,30]}]}
```

**折线图：**
```
generate_echarts: width=1000, height=500, outputType=option, echartsOption={backgroundColor:'#fff', title:{text:'月趋势'}, tooltip:{trigger:'axis'}, legend:{data:['Sales']}, xAxis:{type:'category', data:['Jan','Feb','Mar','Apr','May','Jun']}, yAxis:{type:'value'}, series:[{name:'Sales', type:'line', data:[120,200,150,80,70,110]}]}
```

**饼图：**
```
generate_echarts: width=1000, height=500, outputType=option, echartsOption={backgroundColor:'#fff', title:{text:'占比'}, tooltip:{trigger:'item'}, legend:{orient:'vertical', left:'left'}, series:[{name:'占比', type:'pie', radius:'50%', data:[{value:1048, name:'A'},{value:735, name:'B'},{value:580, name:'C'}]}]}
```

**ECharts 常用配置说明：**

| 配置项 | 说明 |
|--------|------|
| `title.text` | 图表标题 |
| `tooltip` | 提示框（`{}` 启用，`{trigger:'axis'}` 坐标轴触发，`{trigger:'item'}` 数据项触发） |
| `legend` | 图例（`{data:['系列名']}`） |
| `xAxis` | X 轴（`{type:'category', data:[...]}` 分类轴，`{type:'value'}` 数值轴） |
| `yAxis` | Y 轴（同上） |
| `series` | 数据系列数组，每个系列需指定 `type`（bar/line/pie/scatter 等）和 `data` |
| `grid` | 网格布局（`{left: 100, right: 30, top: 50, bottom: 50}`） |

**常用图表类型（series.type）：**

| 类型 | 说明 |
|------|------|
| `bar` | 柱状图 |
| `line` | 折线图 |
| `pie` | 饼图 |
| `scatter` | 散点图 |
| `area` | 面积图（line + areaStyle） |

> ⚠️ **强约束：bar / line / scatter 必须同时配置 `xAxis` 与 `yAxis`**，否则图表校验失败报 "Invalid ECharts option"（系统会自动补缺省轴，但最好一次生成正确）。柱状/折线：`xAxis:{type:'category', data:[类目]}` + `yAxis:{type:'value'}`；散点：`xAxis:{type:'value'}` + `yAxis:{type:'value'}`。pie 不需要坐标轴。

**水平柱状图（标签过长时）：**
```javascript
// 通过 grid.left 控制左边距，避免长标签被裁剪
generate_echarts: width=1000, height=500, outputType=option, echartsOption={backgroundColor:'#fff', title:{text:'Top 10'}, tooltip:{trigger:'axis'}, grid:{left:300, right:30, top:50, bottom:50}, xAxis:{type:'value'}, yAxis:{type:'category', data:['The Lord of the Rings: The Return of the King', ...]}, series:[{type:'bar', data:[9.5, ...]}]}
```

**图表输出格式：** `outputType="option"` 返回 ECharts 配置 JSON，系统自动包装为交互式 HTML 图表渲染在会话中，并保存为 `.html` 文件到 `/workspace/report/` 目录。

**报告必须内嵌图表（可交互）：**

> ✅ **优先用 `build_report` 工具装配报告**——它会自动把 generate_echarts 返回的 iframe 内嵌进报告附录，无需手工粘贴。

生成图表后，使用 `report-export` 技能导出报告时，附录（`## 5. 附录`）**必须把工具返回的可交互 iframe 原样内嵌进报告**，禁止只写文字描述（如"已渲染为交互式 HTML 图表"）。附录必须包含：

```markdown
### 图表
{generate_echarts 工具返回的完整 <iframe> 标签，原样粘贴、一字不改}
```

> 💡 可交互图表：鼠标悬停查看数值、可缩放。
> 图表文件（可分享）：/workspace/report/<图表文件名>.html
```

- **iframe 必须原样复制** generate_echarts 工具返回结果中的 `<iframe ...>` 完整标签（含 src 的 base64 和 height，禁止自己手写、禁止只取 src 或改写 height），一字不改。
- 若工具返回的是 `<img>`（png）或 SVG，说明没用 `outputType=option`，需用 `outputType=option` 重跑。
- `<图表文件名>` 取自 generate_echarts 返回结果中保存的 HTML 文件名（如 `Top 10 电影评分排行_20260808_085554.html`）。
- 聊天消息中呈现报告摘要时，**不要**把 iframe 或相对路径图片写进聊天正文（前端会自动把该轮交互图表附随到报告消息上方，避免显示坏图）。
