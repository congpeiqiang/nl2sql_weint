### 图表引擎：Semiotic

**核心工具：**
- `suggestCharts(data)` — 推荐图表，分析数据特征，推荐最佳图表类型
- `getSchema(component)` — 获取组件 Schema，查看该组件实际支持的属性列表
- `renderChart(component, props)` — 渲染可视化图表

**调用 renderChart 工具，格式对照：**

柱状图: component=BarChart, categoryAccessor, valueAccessor
折线图: component=LineChart, xAccessor, yAccessor

**正确调用格式**

**折线图——直接照抄这个格式，改数据和标题即可：**
```
renderChart: data=[{x:1,y:2},{x:3,y:4},{x:5,y:6},{x:7,y:8},{x:9,y:10},{x:11,y:12},{x:13,y:14},{x:15,y:16},{x:17,y:18},{x:19,y:20},{x:21,y:22},{x:23,y:24}], xAccessor=x, yAccessor=y, title=月趋势, component=LineChart
```

**柱状图：**
```
renderChart: data=[{name:"A",value:10},{name:"B",value:20},{name:"C",value:30}], categoryAccessor=name, valueAccessor=value, title=分布, component=BarChart
```

⚡ data 的键名必须和 accessor 完全一致。使用 x/y 或 name/value。

**渲染前必查 Schema（强制）：**
1. 调用 `getSchema(component)` 查看该组件实际支持的属性列表
2. 从 schema 中找出轴标签属性（不同组件属性名不同）
3. 用正确的属性名传参

**轴标签属性对照表：**

| 图表组件 | 轴标签属性 |
|---------|-----------|
| BarChart | `categoryLabel`（分类轴）、`valueLabel`（数值轴） |
| LineChart | `xLabel`（X轴）、`yLabel`（Y轴） |
| 其他图表 | 以 `getSchema` 返回为准 |

**水平柱状图标签裁剪解决方案（强制）：**

渲染水平柱状图（BarChart, orientation="horizontal"）时，Y 轴标签如果过长会被默认左边距裁剪。必须按以下流程处理：

1. 遍历数据，找出 categoryAccessor 对应的最长字符串
2. 按字符长度计算 margin.left 和 width
3. 在 props 中显式设置 margin.left 和 width

**经验值对照表：**

| 最长标签长度 | margin.left | 画布宽度 |
|:---:|:---:|:---:|
| ≤ 10 字符 | 100 | 600 |
| 11~15 字符 | 150 | 700 |
| 16~20 字符 | 200 | 800 |
| 21~25 字符 | 250 | 900 |
| > 25 字符 | 300 | 1000 |

**示例：**
```javascript
// 数据中 categoryAccessor="title"，最长 title 为 39 字符
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

**图表输出格式：** SVG（可通过 renderChart 获取 SVG 内容，保存为 .svg 文件）
