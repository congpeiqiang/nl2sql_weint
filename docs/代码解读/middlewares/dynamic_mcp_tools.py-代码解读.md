# dynamic_mcp_tools.py — DynamicMCPToolsMiddleware 代码解读

> 源文件：`src/agent/middlewares/dynamic_mcp_tools.py`
> 类别：**工具面控制 / 热注册**
> 挂载点：`wrap_model_call` + `wrap_tool_call`

## 1. 解决的问题

管理员新增/删除一个库或语义库后**必须重启后端**：`ToolNode.tools_by_name` 在 graph 编译时固化，进程起来后新库的工具进不了通道。而发现层本来就是热的（`SemanticDbDetector.invalidate()` 已由 API 调用），语义库内容更新也不需要重启。

## 2. 核心机制：四条通道

利用 langchain 官方给的两条动态通道：

- **模型侧**：`wrap_model_call` 里 `request.override(tools=...)` — 本轮能看见哪些工具
- **执行侧**：`wrap_tool_call` 里 `request.override(tool=...)` — 这个名字执行哪个实例

数据源是 `mcp_tool` 的运行期注册表（`sub_tools_snapshot` / `lookup_sub_tool` / `sub_registry_warmed`）。

### 2.1 并入（模型侧）

注册表里有、请求清单里还没有的工具（管理员刚加的库）→ 追加，当轮可见。

```python
known = {getattr(t, "name", "") or "" for t in tools}
added = [t for t in fresh if (getattr(t, "name", "") or "") not in known]
return request.override(tools=tools + added)
```

### 2.2 摘除（模型侧）

看着是子工具（`wrenai_*` / `dbmcp*`）但注册表里已经没有的名字（库被删/改名/重新关联）→ 从清单摘掉，模型看不到就不会去试。

**只在注册表已预热时摘除**——未预热时（离线单测、启动加载未完成）绝不因"注册表里没有"把健康工具误杀。

### 2.3 改道（执行侧）

执行时按名字取注册表里的**当前**实例（重加载后静态列表里那个是陈旧的）→ `override(tool=...)`。

```python
registered = lookup_sub_tool(name)
if registered is not None:
    if registered is not getattr(request, "tool", None):
        return request.override(tool=registered)
    return request
```

### 2.4 拒绝（执行侧）

注册表已预热却仍收到已下线工具的调用（历史消息里的存量 tool_call、提示词里写了但本部署工具面并没有的名字）→ 返回 `status="error"` 的 ToolMessage，**绝不执行陈旧实例**。

## 3. 拒绝文案设计（只许陈述可证事实）

`_REMOVED_HINT` 只陈述「这个名字不在已预热的注册表里」，**不断言具体原因**：

```
工具 {tool} 当前不在可用工具清单里（可能的原因：它所属的库已被删除或重新关联，
或本次部署的语义服务/数据源不提供这个工具）。
本轮不要再用它、也不要重试。请改用当前可用的工具：...
```

**血案**（2026-09-26 `get_all_knowledge`）：旧文案断言「库已被删除或重新关联」，真因是上游 wren 0.13.0~0.15.0 全都没有这个工具（是提示词在推荐一个不存在的工具），用户因此以为语义库被人删了。

### 3.1 已知工具的替代做法

`_TOOL_FALLBACKS` 按名字后缀匹配替代路径：

| 工具 | 替代做法 |
|---|---|
| `get_all_knowledge` | 用 `list_knowledge()` 列出知识文件，再按需读取 |
| `describe_schema` | 用 `get_context(question)` 取 Schema 片段 |
| `get_mdl` | 用 `get_context(question)` 取相关 model 片段 |
| `describe_model` | 用 `get_context(question)` 或 `list_models()` |

## 4. fail-open 边界

| 状态 | 行为 |
|---|---|
| 注册表已预热 | 并入 + 摘除 + 改道 + 拒绝 |
| 注册表未预热 | **只并入**、不摘除、不拒绝（绝不误杀健康工具） |

预热 = 完成过一次全量对账（`sub_registry_warmed()` 返回 True）。

## 5. 挂载顺序

必须挂在 `_middleware` **最前**（`ToolFilterMiddleware` 的外层）：

```
DynamicMCPTools → ToolFilter → 其他中间件
```

先并入新库工具，再让 ToolFilter 按当前库裁剪。挂反了新库工具会绕过按库过滤。

## 6. 子工具判定

```python
def _is_sub_tool(name: str) -> bool:
    return name.startswith("wrenai_") or name == "dbmcp" or name.startswith("dbmcp_")
```

与 `tool_filter._is_dbmcp` / `query_gate` 同口径。

## 7. 与 tool_filter 的协作

```
DynamicMCPTools（注册表层）：
    ① 并入新工具  ② 摘除下线工具
    ↓
ToolFilter（过滤层）：
    按当前 db_name 筛选暴露给模型的工具子集
    ↓
模型调用
    ↓
DynamicMCPTools（执行层）：
    ③ 改道到当前实例  ④ 拒绝已下线工具
    ↓
实际执行
```
