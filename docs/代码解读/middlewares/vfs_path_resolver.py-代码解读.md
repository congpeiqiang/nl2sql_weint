# vfs_path_resolver.py — VfsPathResolverMiddleware 代码解读

> 源文件：`src/agent/middlewares/vfs_path_resolver.py`
> 类别：**上下文管理**
> 挂载点：`wrap_model_call` / `awrap_model_call`（后处理）

## 1. 解决的问题

agent 全程在 VFS 命名空间工作（`/workspace/`、`/shared/memory/`、`/shared/skills/`），模型在聊天里回显"结果在 `/workspace/report/xxx`"，对用户既不知道是哪个工作区也不可打开。

## 2. 核心机制

### 2.1 后处理

模型响应后处理（handler 返回之后），把 **AIMessage 文本**中的 VFS 路径改写为真实磁盘路径：

```
/workspace/report/chart.html → D:/workspace1/report/chart.html
/shared/memory/notes.md → D:/shared_memory/notes.md
```

### 2.2 VFS 前缀映射

```python
_VFS_PREFIXES = {
    "/shared/memory/":  "shared_memory_dir",
    "/shared/skills/":  "shared_skills_dir",
    "/workspace/":      "active_workspace",
}
```

与 `main_agent` 的 `CompositeBackend routes` 一一对应。

### 2.3 路径匹配正则

```python
_VFS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_:/\\])"  # 负向前瞻：排除真实磁盘路径
    r"/(?:workspace|shared/(?:memory|skills))"
    r"/[^\s,，。；;:：!！?？()（）\[\]{}<>\"'`、|]+"
)
```

**负向前瞻**排除 `D:/workspace/...` 这类已是真实路径的片段，避免二次改写。路径字符排除空白与中英文标点（路径后常跟"（可悬停查看）"等注解）。

### 2.4 路径解析

```python
def _resolve_vfs_path(vpath, wm):
    for prefix, attr in _VFS_PREFIXES.items():
        if vpath.startswith(prefix):
            root = getattr(wm, attr)
            return _forward(root / vpath[len(prefix):])
    return vpath
```

`_forward` 统一转正斜杠（Windows 下可直接导航）。

## 3. 安全边界

### 3.1 只改 AIMessage 文本

**绝不碰 ToolMessage 和 tool_call 参数**——`write_file` 的 path 必须保持 VFS 形态（backend `_resolve_path` 按 `/` 前缀解析）。

### 3.2 只改字符串 content

content 为 block 列表（`[{"type": "text", "text": "..."}]`）时不处理——当前只改 `str` 类型的 content。

### 3.3 幂等

改写后的路径不以 VFS 前缀开头（`D:/workspace/...` 不匹配 `/workspace/`），重复处理无副作用。

## 4. 错误处理

- `get_workspace_manager()` 异常 → 记 warning，返回原响应
- 消息列表不是 list → 返回原响应
- 单个消息改写异常 → 跳过该消息，其余正常处理
- 全程 fail-open：绝不阻断 agent 循环

## 5. 快速路径

```python
def _rewrite_text(text, wm):
    if "/workspace/" not in text and "/shared/" not in text:
        return text  # 快速路径：无 VFS 路径直接返回
    return _VFS_PATH_RE.sub(lambda m: _resolve_vfs_path(m.group(0), wm), text)
```

大多数 AIMessage 不含 VFS 路径，快速路径零开销。
