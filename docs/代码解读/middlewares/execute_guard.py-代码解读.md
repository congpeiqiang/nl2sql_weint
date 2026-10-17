# execute_guard.py — ExecuteGuardMiddleware 代码解读

> 源文件：`src/agent/middlewares/execute_guard.py`
> 类别：**安全护栏**
> 挂载点：`wrap_tool_call` / `awrap_tool_call`

## 1. 解决的问题

deepagents 的 `LocalShellBackend` 用 `subprocess.run(shell=True)` 直跑宿主机。`FilesystemPermission` 只管文件工具（ls/read_file/write_file/edit_file/glob/grep），**管不住 execute**——实测 agent 用 `execute("rm /tmp/xxx.txt")` 绕过文件权限删了工作区外文件。

## 2. 核心机制

### 2.1 两条拒绝规则

**规则一：破坏性命令**

正则词边界匹配以下动词（POSIX + Windows cmd/PowerShell）：

```
rm / rmdir / unlink / deltree / erase / del / rd
format / fdisk / mkfs.*
shutdown / reboot / taskkill / pkill / kill
remove(-item|-file)? / rmtree
```

```python
_DESTRUCTIVE_RE = re.compile(
    r"(?<![\w./-])("
    r"\brm\b|\brmdir\b|\bunlink\b|\bdeltree\b|\berase\b|\bdel\b|\brd\b|"
    r"\bformat\b|\bfdisk\b|\bmkfs(?:\.[a-z0-9]+)?\b|"
    r"\bshutdown\b|\breboot\b|\btaskkill\b|\bpkill\b|\bkill\b|"
    r"\bremove(?:-item|-file)?\b|\brmtree\b"
    r")",
    re.IGNORECASE,
)
```

词边界设计避免误伤："delete" 不含独立 "del"、"model" 不含 "del" 子串。

**规则二：工作区外路径**（`EXECUTE_GUARD_STRICT=0` 可关）

从命令中提取绝对路径 token：

| 模式 | 示例 | 说明 |
|---|---|---|
| POSIX 绝对路径 | `/etc/passwd` | 左边界排除路径中途的 `/` |
| 家目录 | `~/xxx` | 家目录在工作区外 |
| Windows 盘符 | `D:\xxx` 或 `D:/xxx` | 同时接受 `\` 和 `/` |
| 越界 `../` | `../secret` | 向上越界 |

判断是否落在「活跃工作区 / /shared / /workspace VFS 前缀」之外。解析失败按越界处理（宁可多拦）。

### 2.2 路径提取的正则细节

```python
_PATH_TOKEN_RE = re.compile(
    r"((?<![\w./\\*?\[\]-])/(?:[^\s;|&<>\"'`()]*))"  # POSIX 绝对路径
    r"|(~[\\/][^\s;|&<>\"'`()]*)"                      # 家目录
    r"|([A-Za-z]:[\\/][^\s;|&<>\"'`()]*)"              # Windows 盘符
    r"|(\.\.[\\/][^\s;|&<>\"'`()]*)"                   # 越界 ../
)
```

**左边界必须排除路径中途字符**：否则 `cat data/sales.csv` 会提取出 `/sales.csv` → 误判为工作区外。第三个分支同时接受 `X:\` 与 `X:/`，避免 `D:/workspace/...` 被截断。

### 2.3 拒绝行为

被拒返回 `status="error"` 的 ToolMessage，content 带中文原因：

```
Error: execute 已被安全护栏拦截——execute 禁止破坏性命令（rm/del/format 等删除、系统级操作），
请改用文件工具 write_file/edit_file，且仅限工作区。
```

LLM 看到后可改用文件工具或工作区内相对路径，不死循环。

## 3. 关键设计决策

### 3.1 护栏 ≠ 安全边界

代码注释明确定位：**这是护栏而非安全边界**。命令混淆（`cmd /c del`、变量拼接、编码）可绕过。彻底隔离需换沙箱后端（`OpenSandboxBackend`）。

### 3.2 STRICT 模式可关

`EXECUTE_GUARD_STRICT=0` 时只保留破坏性命令拦截，关闭路径检查。用于调试场景（agent 需要访问工作区外的日志文件等）。

### 3.3 只拦截 execute 工具

其他工具（read_file、write_file 等）由 `FilesystemPermission` 静态层管辖，本中间件不重复裁决。

## 4. 工作区解析

```python
def _resolve_roots(self):
    from agent.workspace_manager import get_workspace_manager
    wm = get_workspace_manager()
    return wm.active_workspace, wm.shared_data_root
```

每次拦截时动态解析，确保切工作区后护栏立即生效。
