# Cube 通道报告缺「完整数据表」节 + 模型手写路径渲染出「点了才 404」的假附件

**日期**：2026-09-14
**类型**：链路缺口（落盘闸门工具名过滤过窄）+ 前端渲染无存在性校验
**严重程度**：中高（报告缺可核对的全量明细；附件按钮存在假链接，误导用户以为文件已被产出）

---

## 一、问题描述

生产会话 `01a09f1c-efaa-7c33-a622-23ba29c04df6`（问题「AI 与数据产品线近一年项目用时」，走 **Cube 快速通道**：子线程 `wrenai_WIT_query_cube` ×10 / `run_sql` ×0）两个症状：

1. **报告没有可核对的全量明细**。同日同题走 `run_sql` 通道的报告（14:29:56，16087 字）有「## 2. 完整数据表」节，内嵌代码读盘的全量 39 行；Cube 通道的报告（16:56:23，8450 字）只有模型自写的汇总表。
2. **三个附件只有第一个能下载**。终稿列出 3 个文件（1 个报告 + 2 个图表），报告能预览下载，两个图表点开都不存在。

### 复现条件

- 当前数据库已建模 → 走语义层，且模型选择 **Cube 快速通道**（`wrenai_<库>_query_cube`），不触发 `run_sql`；
- 结果行数 > 50 或文本 > 8000 字符（问题 1，落盘阈值）；
- 模型在正文里**自己拼**文件路径（问题 2，图表文件名带秒级时间戳，见 [[chart-html-filename-not-returned]] 已修的另一半）。

---

## 二、根因分析

### 问题 1：Cube 结果既不落盘也没指针，两处都是 `run_sql` 专属过滤

「完整数据表」节依赖一条**代码读盘**链路（0 模型 token）：

| 步骤 | 位置 | 过滤条件 |
|---|---|---|
| ① 大结果落盘 + 消息瘦身 | `QueryResultOffloadMiddleware._offload_result` | `tool_name.endswith("run_sql")` |
| ② 汇总全量文件指针给 check 结果 | `check_progress._collect_full_result_files` | `name.endswith("run_sql")` |
| ③ 读盘拼节 | `report_builder._full_table_section` | 无过滤，吃 ② 的 `full_result_files` |

两步闸门都只认 `run_sql` → `wrenai_<库>_query_cube` **结构上永不入选**：

- ① 在工具名那一步就被筛掉，**连阈值判断都没走**（该实例 Cube 结果 9412 字符，本来过了 8000 字符阈值）；
- ② 因此 `full_result_files` 恒空 → ③ 直接 `return ""` → 报告少一节。

注意 Cube 结果的 JSON 结构与 `run_sql` **同构**（`{columns, rows, row_count, truncated}`），所以①的解析/落盘/markdown 转换逻辑本来就是可复用的——缺的只是把闸门放宽。

### 问题 2：前端只做正则提取，不问文件在不在

`ReportFileActions` 用正则从 AI 正文里抽 `/report/<文件名>`，**抽到就渲染「预览/下载」按钮**，不校验存在性 → 模型写错时间戳（真名 `…_20260914_165606.html`，模型写 `…_165623`）时按钮照样出现，点下去才 404。

后端 `GET /api/reports/{filename}` 本来就用 404/200 表达存在与否，缺的是一个「只回状态码、不回正文」的探测入口。

---

## 三、解决方案

### 后端

1. **统一「数据表型工具」权威清单**（新增 `src/agent/utils/query_tools.py`，纯 stdlib 零依赖）：

   ```python
   DATA_TOOL_SUFFIXES = ("_run_sql", "_query_cube")
   def is_data_tool(name) -> bool: ...
   ```

   三个消费点全部改为调 `is_data_tool()`：① 落盘闸门（`query_result_offload`）；② 指针收集（`check_progress._collect_full_result_files`）；③ **工具超时**（`path_resolver._tool_timeout_for`）。各自写一份的后果分别是「结果永不落盘」「落了盘没人收」「同一类查询超时口径不一」——都是半死状态，故刻意单一来源。放 `agent.utils` 而非任一消费模块：中间件引 langchain、`path_resolver` 刻意不引，谁引谁都会带进多余依赖。

   `dry_run`/`dry_plan`（只有执行计划、没有行数据）与 `list_cubes`/`describe_cube`（元数据，不跑查询）故意不列；即使误入闸门也会在 `rows` 非 list 处被挡掉。

2. **`HEAD /api/reports/{filename}`**（`src/api/report_file.py`）：存在 → 200 + 真实字节数（`stat().st_size`，不读文件）；不存在 → 404。`GET` 行为逐字节不变（含 `?download=1` 的 RFC5987 文件名）。

3. **工具超时统一 300s**（`path_resolver._tool_timeout_for` + `_TOOL_TIMEOUTS`）：

   原判据是 `startswith("wrenai_") and "run_sql" in name` → `wrenai_<库>_query_cube` 落到 `default: 120s`。同一类「真跑了一次库查询」的语义层工具超时口径不一：Cube 侧大查询更容易被工具超时打断（现在 Cube 大结果也要落盘，超时意味着「完整数据表」整节没数据）。改为 `startswith("wrenai_") and is_data_tool(name)` → 300s；补 `query_cube: 300` 遗留裸名 key 与 `run_sql` 对称。非查询工具（`dry_run`/`dry_plan`/`list_cubes`/`read_file`）超时值不变。

### 前端（`harness-deep-agents-ui`）

`ReportFileActions` 渲染按钮前先探测：

- **只有 404 才判「不存在」**——405（旧后端无 HEAD）/ 5xx / 网络错误一律按「可能存在」处理（fail-open），因此**新前端配旧后端不会把真附件藏起来**，不存在发版顺序耦合；
- 探测结果按文件名做模块级缓存：存在永久缓存，不存在带 15s TTL（文件可能是稍晚才落盘，不能永久隐藏）；
- 探测按「文件名集合指纹」触发，不按消息内容——流式输出每 token 都重渲染，否则每个 token 打一轮 HEAD；
- 渲染策略：**已确认存在**的正常出按钮；**已确认不存在**的隐藏并给一行提示（`N 个引用文件不存在，已隐藏`，悬停可看文件名）；**尚未探测完**的暂不渲染，避免按钮闪现后消失。

---

## 四、验证

| 用例 | 结果 |
|---|---|
| `D:\tmp\test_cube_full_table.py`（离线 24/24） | Cube 100 行落盘+瘦身、落盘文件含全量 100 行、阈值以下/`dry_run`/`dry_plan`/非查询工具/空结果行为不变、`run_sql` 回归、指针收集器含 Cube 且保序去重、**端到端报告出「## 2. 完整数据表」（100 行）+「## 3. 查询定义（Cube 语义层）」且仍无「执行 SQL」字样**、指针文件缺失时降级不报错、**三处共用同一份清单（`is` 同一 tuple 对象）+ Cube 超时 300s 且非查询工具超时值不变** |
| `D:\tmp\test_report_exists_head.py`（离线 18/18） | HEAD 存在中文名+空格 → 200 且回真实字节数不回正文；拼错的时间戳 → 404；空文件算存在；5 类路径穿越/非法名 HEAD 与 GET 均 404；**GET 字节级不变**（含 `?download=1` 的 `filename*=UTF-8''`） |
| `test_report_cube_section.py` / `test_check_result_anchor.py` / `test_chart_path_returned.py` | 11/11、30/30、12/12 仍全绿 |
| 前端 `tsc --noEmit` | `ReportFileActions.tsx` 无错误（全项目 27 条为存量 `@ts-expect-error` 失效，与本次无关） |
| 前端 `eslint` | 该文件仅 2 条存量 `no-useless-escape`（正则 `\[\]`，HEAD 即存在） |

**发版后 E2E（用户执行）**：

1. 重启后端 + 重建前端；
2. 走 Cube 通道问一次大结果问题 → 报告出现「## 2. 完整数据表」且行数与落盘文件一致；
3. 让模型在正文里写一个**故意错误**的 `/report/…_20990101_000000.md` → 该按钮**不出现**，只出一行「1 个引用文件不存在，已隐藏」；正确路径仍可预览/下载；
4. 临时把后端 HEAD 路由去掉（或指向旧后端）→ 附件按钮照常出现（fail-open 生效）。

---

## 五、遗留与风险

- **Cube 大结果现在也会被瘦身**：子 agent 手里同样只剩 20 行样例 + 文件指针（与 `run_sql` 通道一致，是有意为之——防超长生成撞模型超时）。若某类 Cube 问法依赖子 agent 直接读全量行，需观察其最终答复质量。
- **否定结果 15s TTL**：极端情况下文件在 15s 后才落盘，用户需刷新才能看到按钮；正因如此没有做永久缓存。
- **Cube 工具超时从 120s 提到 300s**：真·挂死的 Cube 查询现在最多占用 5 分钟才被打断（与 `run_sql` 一致，是有意的口径统一）。若发现 Cube 侧挂死体感变长，改回 `_TOOL_TIMEOUTS` 的单点值即可（判据集中在 `agent/utils/query_tools.py`）。
- 前端对**非 `/report/` 段**的文件链接（如 `/workspace/…`）仍不做存在性校验。
