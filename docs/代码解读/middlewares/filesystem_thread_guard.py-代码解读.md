# filesystem_thread_guard.py — FilesystemThreadGuardMiddleware 代码解读

> 源文件：`src/agent/middlewares/filesystem_thread_guard.py`
> 类别：**安全护栏**
> 挂载点：`wrap_tool_call` / `awrap_tool_call`

## 1. 解决的问题

nl2sql 子 agent 文件读权限此前整条 `allow /workspace/**`，可 `read_file/grep/ls/glob` 到其它会话的 `conversation_history/`、`report/`、以及 `nl2sql_process_data/{其它线程}/`（含 eval-subject 参考答案）→ 跨线程上下文泄漏，答案变成"之前哪次会话怎么答的"的函数。

## 2. 两层治理

### 2.1 静态层（file_permissions.py）

读范围由 `/workspace/**` 收窄为：

- `/shared/**`
- `/workspace/large_tool_results/**`
- `/workspace/nl2sql_process_data/**`

`conversation_history/`、`report/`、`tmp/`、工作区根文件等一律 `deny /**`。但静态层管不住 `process_data` 内部——那条 allow 是**全线程通配**。

### 2.2 运行时层（本中间件）

在工具调用边界做线程级裁决，管**三件事**：

## 3. 三件事

### 3.1 读隔离（read_file / ls / glob / grep）

`/workspace/nl2sql_process_data/{thread}/...` 只放行 `{thread} == 当前会话线程 id`。

| 场景 | 行为 |
|---|---|
| 不在 `process_data/` 下 | 交静态层处理，放行 |
| `ls` 顶层 `/workspace/nl2sql_process_data/` | 放行（只暴露线程名列表） |
| `process_data/{own_thread}/` | 放行 |
| `process_data/{other_thread}/` | **拒绝** |
| 线程 id 解析不到 | fail-open |

### 3.2 写隔离（write_file / edit_file）

只收紧「写向**其它线程** `process_data` 子目录」，其余写行为维持静态层原有粒度。

- 读别人 = 泄题
- 写别人 = **投毒污染**（更严重）

### 3.3 grep 搜索范围收窄

未收窄的 grep（缺省 / `/workspace` / `process_data` 根）必然把其它线程的命中带回来，且结果过滤兜不住。

- **grep**：要求显式收窄到自有线程目录或 `/shared`
- **ls / glob**：无法在入参上收窄（pattern 本身可能跨目录），改为对**结果做后置过滤**

后置过滤同时堵住「顶层 ls 枚举他人线程名 → 定向读写」的组合路径。

## 4. 路径归一化（关键防御）

本护栏在 `wrap_tool_call` 拿到的是模型**未归一化的原始入参**，而工具侧先 `validate_path()` 再落盘（会给相对路径补前导斜杠）。不补同款归一就会被绕过。

### 4.1 已知绕过（2026-09-18 实测确认）

| 绕过路径 | 原因 |
|---|---|
| `workspace/nl2sql_process_data/<其它线程>/x` | 缺前导斜杠，前缀判定形同虚设 |
| `/workspace/./nl2sql_process_data/<其它线程>/x` | `.` 段导致前缀不匹配 |

### 4.2 _to_vfs 归一化

```python
def _to_vfs(path: str) -> str:
    p = _norm(path)  # 反斜杠→正斜杠、折叠 . 和 // 
    if p and not p.startswith("/"):
        p = "/" + p  # 补前导斜杠
    return p
```

`_norm` 镜像工具侧 `validate_path` 的行为：反斜杠→正斜杠、去首尾空白、折叠 `.` 与重复斜杠。用 `posixpath`（而非 `os.path`）避免 Windows 上吐反斜杠。

## 5. 线程 id 一致性

会话线程 id 与 `langfuse_span._thread_id` **同源**：

```
metadata.langfuse_session_id
  → trace_parent_thread_id
    → configurable.thread_id
      → execution_info.thread_id
```

保证"自有目录"判定与 `process_data` 落盘目录（`query_result_offload._write_full_table`、`langfuse_span._dump_process_data`）完全一致。

## 6. 工具辅助函数

`_tool_name`、`_tool_args`、`_thread_id` 这三个函数定义在本模块顶层，被 `cube_route_gate` 复用（`from agent.middlewares.query_gate import _thread_id, _tool_args, _tool_name`）。各自维护一份必然漂移，顶层 import 还能让改名在启动时就炸出来。

## 7. 关键设计决策

### 7.1 为什么入参必须自归一

- 工具侧 `validate_path()` 在 `_check_fs_permission` **之后**执行
- 本护栏拿到的是原始入参，必须做同款归一
- 否则 `workspace/xxx`（无前导斜杠）绕过前缀判定

### 7.2 为什么 grep 和 ls/glob 策略不同

- grep 有 `path` 参数可以在入参层收窄
- ls/glob 的 pattern 可能跨目录（如 `**/*.md`），入参层无法收窄
- 所以 grep 在入参层拦截，ls/glob 在结果层过滤

### 7.3 fail-open 的边界

- 线程 id 解析不到 → 放行（不阻断正常查询）
- 路径解析异常 → 放行
- `..` 段不在此解析 → 工具侧 `validate_path` 会直接抛错拒绝

## 8. 拒绝消息

读拒绝：
```
Error: 禁止读取其他会话的中间产物（{path}）。
业务口径只准来自语义层 MCP 工具（如 run_sql / get_all_knowledge），
禁止翻阅其它会话的 process_data 目录。
```

写拒绝：
```
Error: 禁止写入其他会话的 process_data 目录（{path}）。
每个会话只能写入自己的线程目录。
```
