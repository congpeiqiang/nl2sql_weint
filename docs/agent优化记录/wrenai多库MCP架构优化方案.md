# WrenAI 多库 MCP 架构优化方案（多 server vs 单 server）

**日期**：2026-08-20
**类型**：架构优化方案
**状态**：**路线 A 已实施**（2026-08-20，ToolFilterMiddleware）；路线 B 待规划
**关联**：[[多工作区隔离方案]] — 2026-08-20 已实施，对本方案有影响（见 §七）

---

## 一、背景与问题

当前 NL2SQL 项目为**每个 Wren 语义库（wren_project）都启动一套独立的 wrenai MCP server**，工具名按库名前缀区分（`wrenai_<库名>_run_sql`）。由此带来三个痛点：

1. **工具膨胀**：N 个库 → N 套工具（约 17 个工具 × N），全部塞给子智能体，提示词里同时包含多套 wrenai 工具，LLM 选工具负担重、易选错。
2. **增库要改代码/重启**：新增语义库 → 需要启动新 server、新增工具，而工具在进程启动时固化，**必须重启后端**。
3. **提示词污染**：多库工具名都出现在 prompt 中，即使当前只查一个库，其他库的工具描述也占 token、干扰选择。

---

## 二、现状架构（固化机制）

### 2.1 启动流程

```
进程启动
  → mcp_tool.py: load_sub_tools()
    → _get_sub_server_config()  遍历 db_config.json
      → 每个 wren_project 非空的库，启动一个子进程：
          wren serve mcp --project <project_path>
      → server 名 = wrenai_<库名>
    → MultiServerMCPClient({...}, tool_name_prefix=True)
      → 工具名 = wrenai_<库名>_<工具名>
  → nl2sql_agent.py: create_deep_agent(tools=resolved_tools)
    → 工具烘焙进 LangGraph 图
```

### 2.2 两层固化（重启的根因）

**第一层：`_sub_tools` 进程级单例**（[mcp_tool.py:217-230](src/agent/tools/mcp_tool.py#L217-L230)）

```python
_sub_tools: Optional[List] = None   # 单例

def load_sub_tools() -> List:
    global _sub_tools
    if _sub_tools is not None:       # ← 已加载就返回缓存，永不重建
        return _sub_tools
    servers = _get_sub_server_config()   # ← 只在首次扫描 db_config
    _sub_tools = _load_mcp_servers(servers, "sub")
    return _sub_tools
```

**第二层：agent 图编译时固化工具**（[graphs/nl2sql_agent.py:44-50](src/agent/graphs/nl2sql_agent.py#L44-L50)）

```python
# 模块导入时执行，工具快照烘焙进图
tool_index = {t.name: t for t in mcp_tools}   # mcp_tools 是 import 时快照
resolved_tools = [...]                          # 按 YAML pattern 子串匹配
agent = create_deep_agent(tools=resolved_tools, ...)  # 工具进图
```

即使清掉 `_sub_tools` 缓存重新扫描，`graphs/nl2sql_agent.py` 模块已 import、`agent` 图已编译，新工具进不去——除非重新 import 模块（等价于重启）。

### 2.3 `SemanticDbDetector.invalidate()` 解决不了重启

[wren_semantic.py:266-272](src/api/wren_semantic.py#L266-L272) 每个写操作后调用 `_invalidate_detector()`，只刷新 detector 内存缓存（前端 semantic 标记），**不重建 MCP server、不刷新 agent 图**。所以标记对了，但实际工具没变。

---

## 三、候选路线

### 路线 A：多 server 常驻 + 按库过滤工具（**已实施**，见 §九）

**思路**：server 生命周期不变（启动时全部拉起），但**每次 LLM 调用时只暴露当前库的工具**，其他库的工具对 LLM 不可见。

```
所有 MCP server 始终运行（内存占用小，各持一个 WrenEngine）
  → 所有工具始终加载
  → 请求时按 configurable.db_name 过滤：
      只保留 wrenai_<当前库名>_* + dbmcp_* 工具
  → 其他库的工具对 LLM 隐藏
```

**实现**：在 `graphs/nl2sql_agent.py` 的 `dynamic_prompt` 或新增一个工具过滤中间件，按 `db_name` 从 `resolved_tools` 里筛出当前库的工具子集。

**效果**：
- ✅ LLM 只看到 ~17 个工具（不是 51 个），提示词干净，选工具更准
- ✅ 切库无延迟（工具已就绪，只做过滤）
- ❌ **新增库仍需重启**（新库要启动新 server 子进程 + 新工具进图，两层固化仍在）

### 路线 B：单 server + 运行时 project 路由 + 免重启

**思路**：只启动一个 MCP server，工具名固定（不带库前缀），工具内部按 `db_name`/`project` 参数路由到对应的 WrenEngine。

```
单个 MCP server（自研或 monkey-patch wrenai serve mcp）
  → 进程内维护 {project_path: WrenEngine} 池（懒加载）
  → 工具名固定：run_sql / get_mdl / describe_model / recall_queries ...
  → 每个工具带 db_name 参数（middleware 注入，复用现有 _inject_db_name 逻辑）
  → server 内部按 db_name → project → engine 路由
  → 新增库：detector.invalidate() 刷新映射，engine 池懒加载新 project，免重启
```

**关键难点**：`wren serve mcp` 是 wrenai 0.11.0 的固定 CLI，底层 `WrenEngine` 绑定**单 project + 单 connection**，一个进程天然不能服务多 project。要做到单 server 多 project 路由，必须：
- 要么改 wrenai 源码（fork / monkey-patch `serve mcp`）
- 要么**自研一个 MCP server 包装层**，内部持有多 project 的 engine 池

**效果**：
- ✅ 工具名固定，无库前缀，**彻底消除工具膨胀和提示词污染**
- ✅ **新增/删除库免重启**（engine 池懒加载）
- ⚠️ 实现复杂度最高，需要自研 MCP server 层或改上游

### 路线 C：动态启动 server（用户提的 --project 动态切换）

**思路**：不启动时加载全部，而是切库时动态启动对应 server、关掉旧的。

```
用户切库
  → 启动 wren serve mcp --project <新库project>
  → 加载工具列表
  → 关闭旧 server
```

**耗时**：`wren serve mcp` 子进程启动 ~1-3s（加载 MDL + 建 WrenEngine + 连库）+ `get_tools()` 握手 ~0.5-1s，合计 **2-4s**。

**延迟触发时机**：取决于实现——
- 切库动作即预加载 → 用户在切库时等待，提问时已就绪，无额外延迟
- 发问题才加载 → 2-4s 加在首次提问端到端延迟上（体验差）

**效果**：
- ⚠️ 仍有 2-4s 切换成本，且需处理 server 生命周期（关旧进程）
- ⚠️ 工具列表变更仍需重建 agent 图（LangGraph 非 trivial）
- ❌ 复杂度不低，收益不如路线 B

---

## 四、优劣势对比

| 维度 | 现状（每库一套） | 路线 A（过滤） | 路线 B（单 server 路由） | 路线 C（动态启动） |
|------|----------------|---------------|------------------------|-------------------|
| 工具数量 | N×17 全暴露 | 全加载但只暴露当前库 | 固定 17 个 | 当前库 17 个 |
| 提示词污染 | ❌ 严重 | ✅ 干净 | ✅ 最干净 | ✅ 干净 |
| 切库延迟 | 无 | 无 | 无 | 2-4s |
| 新增库重启 | 需要 | **需要** | **不需要** | 不需要 |
| 删除库重启 | 需要（残留僵尸 server） | 需要 | **不需要** | 不需要 |
| 实现难度 | —（现状） | 低 | 高（自研 server 层） | 中 |
| 上游依赖 | wrenai CLI | wrenai CLI | 需改 wrenai | wrenai CLI |
| 风险 | — | 低 | 中（自研层质量） | 中（生命周期管理） |

### 关键结论

1. **已存在的多个库切换**：三条路线都无需重启（现状已支持，`dynamic_prompt` 按 `db_name` 注入前缀）。
2. **新增库**：现状和路线 A **都需要重启**（两层固化）；路线 B / C 可免重启。
3. **删除库**：功能上可容忍不重启（`dynamic_prompt` 判断未建模后走 dbmcp 直连，不再用旧库工具），但会**残留僵尸 server 子进程**占连接，资源上需重启才干净。
4. **重启的根因**不是"工具多"，而是**工具名绑死库名前缀**（`wrenai_<库名>_`）。工具名固定为 `run_sql`（不带前缀），新库上线就无需新增工具、无需重建图。

---

## 五、待优化点

1. **消除库名前缀**：工具名从 `wrenai_<库名>_run_sql` 改为固定 `run_sql`，是根治工具膨胀 + 免重启的关键前提。
2. **自研 MCP server 包装层**（路线 B 核心）：单进程内维护 `{project_path: WrenEngine}` 池，工具按 `db_name` 路由。
3. **工具动态过滤中间件**（路线 A，低风险可先行）：按 `configurable.db_name` 每次调用只暴露当前库工具，改善提示词。
4. **engine 池懒加载 + 生命周期管理**：新增库即时懒加载，删除库即时回收连接。
5. **复用 `_inject_db_name` 注入逻辑**：路线 B 中工具 `db_name` 参数注入可复用现有 [path_resolver.py:538](src/agent/utils/path_resolver.py#L538) 的注入逻辑。
6. **删除库的僵尸 server 回收**：即使不做路线 B，也应考虑删除库时主动关闭对应 server 子进程，避免空转连接。

---

## 六、决策建议（当前）

- **短期（低风险、可立即实施）**：路线 A 的"工具过滤"——不改 server 生命周期，只在请求时过滤工具子集，纯收益（提示词更干净、选工具更准），代价极小。
- **中期（根治）**：路线 B——单 server + 运行时 project 路由，一次性解决工具膨胀、提示词污染、增删库免重启三个问题，但需自研 MCP server 层或改 wrenai 上游。
- **不建议**：路线 C 动态启动——2-4s 切换成本 + 生命周期管理复杂度，收益不如路线 B。

---

## 七、多工作区隔离的影响（2026-08-20 校验）

多工作区隔离方案（[[多工作区隔离方案]]）已于 2026-08-20 实施，对本方案的影响如下：

### 7.1 路径变更

| 方案文档中的旧路径 | 实际新路径 |
|---|---|
| `src/agent/nl2sql_agent.py` | `src/agent/graphs/nl2sql_agent.py`（模块导入时 `base_dir` 调整为 `Path(__file__).resolve().parent.parent`） |
| `src/agent/checkpointer_factory.py` | `src/agent/checkpoint/checkpointer_factory.py` |

### 7.2 数据隔离与本方案的关联

| 多工作区隔离设计 | 对本方案的影响 |
|---|---|
| `db_config.json` 按工作区隔离 | 每个工作区有自己的一套库和 Wren project。`SemanticDbDetector` 读 `db_config_store`（已工作区感知），MCP server 集合**天然按工作区变化** |
| `semantic/`（Wren 项目目录）按工作区隔离 | 切换工作区 = 切换整套 Wren 项目 = 切换整套 MCP tool |
| 工作区切换需要重启后端（checkpointer 单例） | 新增 Wren 语义库需要重启 → 与工作区切换重启**一致**，不再是一个"额外的"痛点，重启成本被稀释了 |

### 7.3 对三条路线的影响

- **路线 A（过滤）**：无影响。`dynamic_prompt` 仍按 `configurable.db_name` 注入前缀，`db_name` 来自当前工作区的 `db_config.json`，过滤逻辑无需感知工作区。
- **路线 B（单 server 路由）**：增加了一个维度——单 server 内的 `{project_path: WrenEngine}` 池需要按工作区变化。但切换工作区 = 重启 = 重建 engine 池，与重启行为一致，不是额外问题。
- **路线 C（动态启动）**：无影响。

### 7.4 结论

多工作区隔离**不改变原有分析和推荐**，反而让"重启"变得合理——因为工作区切换也需要重启，新增语义库的额外重启成本被稀释了。路线 A 仍是最低风险的先行项，路线 B 仍是根治目标。

---

## 八、相关文件

| 文件 | 说明 |
|------|------|
| [mcp_tool.py](src/agent/tools/mcp_tool.py) | `_get_sub_server_config()` 每库启动 server；`_sub_tools` 单例 |
| [graphs/nl2sql_agent.py](src/agent/graphs/nl2sql_agent.py) | 工具按 YAML pattern 烘焙进 agent 图；`dynamic_prompt` 注入路由 |
| [semantic_db.py](src/agent/utils/semantic_db.py) | `SemanticDbDetector` / `wrenai_server_name()` 库名→前缀 |
| [path_resolver.py](src/agent/utils/path_resolver.py) | `_inject_db_name()` dbmcp 工具 db_name 注入（可复用于路线 B） |
| [wren_semantic.py](src/api/wren_semantic.py) | 语义库管理 API，写操作返回 `requires_restart: true` |
| [db_config.json](src/agent/workspace/db_config.json) | 库配置（默认工作区），`wren_project` 字段关联语义库 |
| [多工作区隔离方案](多工作区隔离方案.md) | 2026-08-20 已实施，工作区隔离 + 重启策略 |

---

## 九、路线 A 实施记录（2026-08-20）

### 9.1 实施内容

- 新增 [tool_filter.py](src/agent/middlewares/tool_filter.py)：`ToolFilterMiddleware`。
  - 每个 LLM 调用读取 `configurable.db_name`（走 `langgraph.config.get_config()`，与现有 `CurrentDbContextMiddleware` / `dynamic_prompt` 同一路径）。
  - 保留当前库的 `wrenai_<库名>_*` 工具，过滤其他库的 `wrenai_<其他库名>_*` 工具。
  - 保留 `dbmcp_*`、图表等非 wrenai 工具。
  - 通过 `ModelRequest.override(tools=[...])` 替换后续模型调用看到的工具列表。
- [graphs/nl2sql_agent.py](src/agent/graphs/nl2sql_agent.py) middleware 链新增：

  ```python
  _middleware = [
      skills_middleware,
      skill_data_middleware,
      ProgressTrackerMiddleware(),
      ToolFilterMiddleware(),
      dynamic_prompt,
  ]
  ```

### 9.2 验证结果

- 全部工具 11 个（3 库 wrenai × 3/2/3 + `dbmcp_*` × 2 + chart × 1）。
- 过滤后 `db=imdb`：6 个（3 imdb + 2 dbmcp + 1 chart）。
- 过滤后 `db=clickhouse`：5 个（2 clickhouse + 2 dbmcp + 1 chart）。
- 过滤后 `db=aix_report`：6 个（3 aix_report + 2 dbmcp + 1 chart）。

### 9.3 实施结论

- 当前工具命名仍遵循 `wrenai_<库名>_<工具名>`（`wrenai_server_name()` 统一推导，本次未发现命名变更）。
- 路线 A 不改变 server 生命周期；新增/删除语义库仍需重启后端（与多工作区切换重启策略一致）。
- 后续根治目标仍是路线 B：单 server + 运行时 project 路由（免重启 + 消除库名前缀）。