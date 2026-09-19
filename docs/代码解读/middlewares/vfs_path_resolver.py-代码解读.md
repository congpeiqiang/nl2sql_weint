# vfs_path_resolver.py 代码解读

> 文件路径：`src/agent/middlewares/vfs_path_resolver.py`（143 行）
> 解读日期：2026-09-18

## 一句话概括

`VfsPathResolverMiddleware`：模型响应后处理（`wrap_model_call` 的 handler 返回之后），把 **AIMessage 文本**里的虚拟文件系统路径（`/workspace/`、`/shared/memory/`、`/shared/skills/`）按映射改写为**真实磁盘路径**，让用户拿到的是可直接导航打开的物理路径。

## 解决的问题

Agent 全程在 VFS 命名空间里工作：`CompositeBackend` 把 `/workspace/` 路由到当前 thread 工作区、`/shared/memory/` → `<data_root>/memory`、`/shared/skills/` → 工作目录。deepagents 的 filesystem 中间件按 `/` 开头前缀解析路径、与真实 cwd 无关——所以模型在**聊天里回显**「结果在 /workspace/report/xxx」时，用户既不知道是哪个工作区，也无法直接打开。

## 核心实现

### `_rewrite_text`
- `_VFS_PREFIXES` 长前缀优先（`/shared/memory/`、`/shared/skills/` 先于裸 `/workspace/`，避免 `/shared/...` 被误映射成 workspace 根）；
- 正则负向前瞻 `(?<![A-Za-z0-9_:/\\])`：**排除 `D:/workspace/...` 这类已是真实路径的片段**，避免二次改写；
- 路径字符集排除空白与中英文标点（`,.;:!?，。；：！？、`）——路径后常紧跟「（可悬停查看）」等注解，须在此截断；
- 真实路径统一 `.replace("\\", "/")`（Windows 盘符路径保持正斜杠，用户可直接导航）。

### `_rewrite_message`
- content 为 str → 直接改写；为 block 列表 → 只改 `type in ("text","text-*")` 的块；
- **只处理 AIMessage**（`_post_process` 中判断），且无变化时返回原对象（幂等、零拷贝）。

## 安全边界（关键）

**绝不碰 ToolMessage 和 tool_call 参数**：
- ToolMessage 是工具返回的原始数据（如 read_file 内容），不应被改写；
- write_file 的 `path` 参数**必须保持 VFS 形态**——backend `_resolve_path` 按 `/` 前缀解析，改成物理路径会导致 backend 路由失败（docstring 显式警告）。

fail-open：任何异常只 warning，返回未改写的原响应。

## 挂载

挂主 agent（回复面向用户的层）——`main_agent.py` L254 实例化。同步/异步双路径同逻辑。

## 关联文件

- deepagents `CompositeBackend` / `_resolve_path`：VFS 路由机制
- `langfuse_span.py`：vfs 指针消息（另一类路径展示场景，口径 `/workspace/large_tool_results/...`）
- `query_result_offload.py`：full_result_file 指针的产出方
