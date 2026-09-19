# execute_guard.py 代码解读

> 文件路径：`src/agent/middlewares/execute_guard.py`（184 行）
> 解读日期：2026-09-18

## 一句话概括

`ExecuteGuardMiddleware`：在 `wrap_tool_call` 层拦截 `execute`（shell）工具，拒绝**破坏性命令**与**工作区外绝对路径引用**，堵住文件权限体系的执行旁路。

## 解决的问题

deepagents 的 `LocalShellBackend` 用 `subprocess.run(shell=True)` 把命令直接跑在宿主机上。`FilesystemPermission` 只约束 ls/read_file/write_file/edit_file/glob/grep 文件工具，**管不住 execute**——已实测：agent 用 `execute("rm /tmp/xxx.txt")` 删除了工作区之外的文件，绕过「共享只读 / 代码根禁读写」的文件权限。

## 两条拒绝规则（`_check_command`）

### 规则 1：破坏性命令（无开关，始终生效）
`_DESTRUCTIVE_RE` 正则覆盖 POSIX + Windows cmd/PowerShell：
`rm / rmdir / unlink / deltree / erase / del / rd / format / fdisk / mkfs.xxx / shutdown / reboot / taskkill / pkill / kill / remove-item / rmtree`

细节：`(?<![\w./-])` 防止命中路径中的片段；词边界 `\b` 设计使 `del` 不误中 "delete"（del 后是 e 仍是词字符无边界）、"model" 无 del 子串。

### 规则 2：工作区外路径（`EXECUTE_GUARD_STRICT=0` 可关，默认开）
- `_PATH_TOKEN_RE` 提取四类 token：POSIX `/xxx`、家目录 `~/xxx`、Windows `X:\xxx`、越界 `../`。
- `_is_outside` 判定允许范围：**活跃工作区**（workspace_manager.active_workspace）、**共享区**（shared_data_root）、VFS 前缀 `/shared`、`/workspace`（shell 里不一定真实存在，放行避免误伤）。
- Windows 盘符路径用 `Path.resolve().relative_to()` 逐级比对；POSIX 路径与工作区 resolve 后的正斜杠形式前缀比对。
- **解析失败按越界处理**（宁可多拦）。

## deny 行为

`_deny` 返回 `status="error"` 的 ToolMessage，content 为中文原因 + 改路指引（"请改用文件工具 read_file/write_file/edit_file 只读写工作区，或使用工作区内相对路径"）——LLM 看到原因后可自我修正，不会进入死循环。

## 定位声明（docstring 明确）

> 这是「护栏」而非「安全边界」——命令混淆（`cmd /c del`、变量拼接、编码）可绕过。要彻底隔离，请换沙箱后端（见 `agent/backends/sandbox_setup.py` 的 OpenSandboxBackend）。

## 结构要点

- `_resolve_roots`：每次调用实时读 workspace_manager（支持前端切工作区）。
- `_tool_name` / `_command`：兼容 tool_call 为 dict / 对象两种形态。
- 非 execute 工具一律透传 handler，同步/异步双路径同逻辑。

## 关联文件

- `agent/backends/sandbox_setup.py`：沙箱后端（真正的安全边界）
- `file_permissions.py`：静态文件权限（本护栏互补的另一半）
- `filesystem_thread_guard.py`：同层（wrap_tool_call）的文件读线程护栏
