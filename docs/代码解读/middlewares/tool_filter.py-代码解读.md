# tool_filter.py 代码解读

> 文件路径：`src/agent/middlewares/tool_filter.py`（176 行）
> 解读日期：2026-09-18

## 一句话概括

`ToolFilterMiddleware`：每次模型调用前按 `configurable.db_name` 过滤出站工具列表（`request.override(tools=...)`）——只暴露当前库的 wrenai 语义层工具，且**已建模库直接不绑 dbmcp 直连工具**（语义层独占）。

## 路线 A（多 server 常驻 + 按库过滤）

- 所有 MCP server 启动时全部加载（现状不变，切换零成本）；
- 每次 LLM 调用按当前库筛工具：其他库的 wrenai 工具对 LLM **不可见** → 提示词更干净、选工具更准。

## 过滤规则 `_filter_tools`

| 工具 | 处置 |
|---|---|
| `wrenai_<当前库>_*` | 保留 |
| `wrenai_<其他库>_*` | 过滤 |
| `dbmcp_*` | 当前库**实际存在** wrenai 工具且独占开关开 → **移除**；否则保留 |
| 其余（图表工具等） | 保留 |

db_name 为空 → 全量原样返回；过滤后数量无变化 → 不创建新对象（省 override 开销）。

## 语义层独占（2026-09-09）

**动机**：已建模库上 dbmcp 直连不经过语义层、丢业务口径，此前靠 QueryGate **事后拦截**——模型先试一次 → 收 status=error → 重读指引 → 重发语义层工具，**白费一轮**。改为在出站 payload 里**直接不绑定** dbmcp_*：模型看不到就不会试；且与 dynamic_prompt 的「已建模只讲 wrenai / 未建模只讲 dbmcp」二分支对齐（此前 prompt 说禁止而工具还绑着，模型才会去试）。

**判定用实际存在的工具而非配置**（关键防误伤）：用 `name.startswith(prefix)` 判 wrenai 可用性，**不用 `is_modeled()`**——后者查 db_config 配置，2026-09-08 预检事故证明 wrenai server 加载失败时配置仍显示已建模，据此移除 dbmcp 会让模型**一个查询工具都没有**（必须 fail-open 保留 dbmcp）。

**纵深而非二选一**：QueryGate 通道硬闸保留为第二道防线——历史消息里的存量 dbmcp 调用、模型从静态 prompt 幻觉出的工具名，仍由它接住（「看不见 + 拦得住」）。

**回退开关**：`NL2SQL_SEMANTIC_EXCLUSIVE_TOOLS=0/false/no/off` 免改代码秒回退旧行为（dbmcp 常驻，仅 QueryGate 事后拦）。

## 前缀推导（净化唯一源）

`_get_wrenai_prefix` 统一走 `semantic_db.wrenai_server_name(db_name) + "_"`——避免二次实现净化逻辑漂移：**库名含中文时旧 `\W+` 规则不折叠 CJK**，过滤前缀带着中文匹配不上 ASCII 工具名（见 `semantic_db._server_slug` 注释）。

## 原理

`ModelRequest.override(tools=[...])` 创建新 ModelRequest 实例替换 tools 列表，LangGraph 后续模型调用只传递新列表（只改出站，不动 graph 绑定的全量工具）。

## 关联文件

- `query_gate.py`：第二道防线（硬闸）
- `agent/utils/semantic_db.py`：wrenai_server_name 唯一净化源
- `nl2sql_agent.dynamic_prompt`：prompt 侧二分支路由（同源对齐）
