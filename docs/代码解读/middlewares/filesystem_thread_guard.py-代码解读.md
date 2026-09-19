# filesystem_thread_guard.py 代码解读

> 文件路径：`src/agent/middlewares/filesystem_thread_guard.py`（173 行 → 实施两条高风险修复后 334 行）
> 解读日期：2026-09-18
> 修订：2026-09-18 复核（撤销原高风险①，重编号 ①~⑧；订正「前置事实」与 grep 修复方案；新增「延伸分析三」并修复其中②的 `_PATH_TOKEN_RE` 左边界缺陷，回归 40/40）
> 修订二：2026-09-18 落地高风险 ①②（写侧线程裁决 + grep 入参收窄 + ls/glob 结果后置过滤），顺带修复两条同源的路径变体读绕过；「核心实现」与「高风险两条的具体修复设计」两节已按实际代码更新，离线回归 73/73。**代码未发版**。

## 一句话概括

`FilesystemThreadGuardMiddleware`：在 nl2sql 子 agent 工具调用边界做**线程级读写裁决**——只允许读写本次会话自己的 `nl2sql_process_data/{thread}/` 目录（写入侧只收紧“写向他人目录”），拒绝其它会话/其它 run 中间产物的读写，并收窄 grep 的搜索范围、后置过滤 ls/glob 结果里的他人线程项。

## 解决的问题（同题多跑不一致归因 S2-1）

nl2sql 子 agent 文件读权限此前整条 `allow /workspace/**`，可 read_file/grep/ls/glob 到其它会话/其它 run 的：
- `conversation_history/`、`report/`
- `nl2sql_process_data/{其它线程}/`（含 eval-subject **参考答案**）

→ 跨线程上下文泄漏，答案变成「之前哪次会话怎么答的」的函数（同一问题多次跑结果不一致的根因之一）。

## 两层治理架构（本文件是第二层）

| 层 | 位置 | 能力 | 局限 |
|---|---|---|---|
| 静态层 | `file_permissions.py` 的 `NL2SQL_FILE_PERMISSIONS` | 读范围由 `/workspace/**` 收窄为 `/shared/**` + `/workspace/large_tool_results/**` + `/workspace/nl2sql_process_data/**`；conversation_history/、report/、tmp/、根文件一律 deny | **无法按线程 id 限权**（线程 id 运行时才知） |
| 运行时层（本中间件） | `wrap_tool_call` | process_data 下按 `{thread} == 当前会话线程 id` 放行 | 只管读工具 |

## 核心实现

### 线程 id 同源（关键一致性设计）
`_session_thread_id` 直接复用 `langfuse_span._thread_id` 解析链：
`metadata.langfuse_session_id → configurable.trace_parent_thread_id → configurable.thread_id → execution_info.thread_id`

保证「自有目录」判定与 process_data 落盘目录（`query_result_offload._write_full_table`、`langfuse_span._dump_process_data`）**完全一致**——子 agent 自己的产物、其它会话的产物用同一把尺子区分。

### 路径判定
- `_norm`：反斜杠→正斜杠、折叠 `//`、去空白、**`posixpath.normpath` 折叠 `.` 段**（Windows 兼容）。用 posixpath 而非 os.path——后者在 Windows 上吐反斜杠；自身先折叠 `//` 是因为 posixpath 会**保留**恰好两个前导斜杠（POSIX 特例）。
- `_to_vfs(path)`：`_norm` 之上补前导斜杠，**与工具侧 `validate_path()` 同构**。工具侧是「先 `validate_path()` 归一、再判权限」，本护栏拿到的是**原始入参**——不补同款归一，前缀判定形同虚设（2026-09-18 实测确认的既有绕过，见下）。
- `_process_data_thread(path)`：path 在 `/workspace/nl2sql_process_data/` 下时提取首段线程名；返回 `None`=不在范围、`""`=未指定线程（顶层或根自身）。
- `_covers_process_data(target)`：被搜索的目录是否**覆盖** process_data 根（缺省、`/`、`/workspace`、根自身都算）。不靠枚举字符串白名单。
- `_candidate_paths`：按工具提取目标路径——read_file 取 `file_path`、`ls` 取 `path`、`glob` 取 `pattern`+`path`、**write_file/edit_file 取 `file_path`**；grep 的 `path` 走上面的专用闸门。

### `_precheck` 裁决流（入参侧）
1. 工具名不在 `_READ_TOOLS` + `_WRITE_TOOLS` → 放行。
2. 线程 id 解析不到（单测/无 config 场景）→ **fail-open** 放行，静态层兜底。
3. **grep 专用闸门**：`path` 落在**别人**线程目录 → `_deny`（读越权文案）；`path` **覆盖** process_data 根且不在自有线程目录内 → `_deny_scope`（提示收窄）；其余放行。
4. 逐个候选路径：不在 process_data 下 / 自有目录 / 顶层（仅暴露线程名无内容，交结果后置过滤）→ 放行；**其它线程目录 → `_deny`**（写工具用写越权文案）。

### `_postfilter` 结果后置过滤（ls/glob）
`content` 是 `str(list[str])` → `ast.literal_eval` 后剔除他人线程项，再 `str()` 回去。非 `[` 开头（error/`No matches found`）、解析失败、非 list → **原样放行**（被截断的部分模型同样看不到，没有“不给就泄漏”的压力），解析失败记 WARNING。异步 `awrap_tool_call` 同样走 `_precheck` → await → `_postfilter`。

deny 消息除拒绝外明确指路：「业务口径只准来自语义层 MCP 工具（get_instructions / recall_queries / get_all_knowledge），禁止翻找其它会话的 process_data」。

### 两条实测确认的路径变体绕过（2026-09-18 修复）
`_to_vfs` 之前只做「`//` 折叠 + 补前导斜杠」，不解析 `.`。用 deepagents 真实 `validate_path` 对拍，三种写法**全部归一成同一路径**、而静态层 `allow /workspace/nl2sql_process_data/**` 照放：

| 原始入参 | 护栏旧判定 | 工具侧归一后 | 结果 |
|---|---|---|---|
| `workspace/nl2sql_process_data/<其它>/x.json` | 不在前缀下 → 放行 | `/workspace/nl2sql_process_data/<其它>/x.json` | **读他人成功** |
| `/workspace/./nl2sql_process_data/<其它>/x.json` | 不在前缀下 → 放行 | 同上 | **读他人成功** |
| `/workspace//nl2sql_process_data//<其它>/x.json` | 同上 | 同上 | **读他人成功** |

故 `_norm` 改走 `posixpath.normpath`，与 `validate_path` 的 `os.path.normpath` 对齐（`validate_path` 自己的 docstring 就写着 `/./foo//bar` → `/foo/bar`）。

## 延伸分析一：工作区内四个产物目录的隔离机制对比（2026-09-18）

前提：`/workspace/` 本身是**跨 thread 共享**的（路由到前端选择的活跃工作区，不是按会话分目录）。因此“会话隔离”全靠下面这套目录布局 + 双层权限实现：

| 目录 | 磁盘布局（同一工作区内） | nl2sql 子 agent 可读性 | 隔离手段 |
|---|---|---|---|
| `nl2sql_process_data/` | **按线程分子目录** `{thread_id}/...` | ✅ 只准读自己的 | 物理分目录 + 本护栏运行时线程裁决 |
| `conversation_history/` | 按线程分**文件** `{thread_id}.md`（自动压缩 offload，见「上下文占用圆环方案」） | ❌ 一律禁读 | 静态层 deny（不在读白名单，连自己会话的也读不了，无需线程裁决） |
| `report/` | **扁平目录**，文件名=时间戳（`report_builder.py` 写 `active_workspace/report/{fname}`），所有会话混放 | ❌ 一律禁读 | 静态层 deny |
| `large_tool_results/` | **扁平目录**，文件名=`<tool_call_id>`（每次调用随机 id），所有会话混放 | ✅ **允许读全部** | ⚠️ 有意不隔离：靠指针按 id 寻址窄化（`file_permissions.py` 注释：“工具调用 id 键控，非会话目录，允许读回”） |

为什么只有 `nl2sql_process_data` 需要线程裁决：它是四者中唯一**既要放开读、又含高泄漏风险内容（eval-subject 参考答案）**的目录——整目录 deny 会自断功能（读回自己的 query_result 全量表），整目录 allow 就泄题；其余目录要么“禁读即可”，要么“按 id 寻址天然窄化”。

一句话总结：**线程级隔离只发生在 `nl2sql_process_data` 一个目录上；conversation_history / report 的“隔离”实际是子 agent 整体禁读；large_tool_results 是声明过的有意让渡。主 agent（宽 `FILE_PERMISSIONS`）对四者全部可读（产品需要：引用/复述历史报告）。**

## 延伸分析二：隔离机制的已知问题与收紧方案（2026-09-18 评审计）

### 高风险：存在成体系的绕过通道

> **2026-09-18 复核修订**：本节原列三条，原 ①「`execute`（shell）完全绕开线程护栏」经实测**推翻并删除**——nl2sql 子 agent 根本没有 `execute` 工具，这条通道不存在（撤销依据见「延伸分析三」）。原 ② ③ 上移为 ① ②，中/低风险顺次重编号（④~⑨ → ③~⑧）。原 ① 的示例命令也是错的，理由见延伸分析三。

**① 写侧无隔离**（**已修**，2026-09-18）
静态层 write 规则是 `/workspace/**` 全 allow，本护栏只管读工具：任一会话可 `write_file` 覆盖/污染其它会话的 report/、甚至别人 process_data 子目录。读隔离细，写隔离为零；写污染会经由后续会话的读间接生效。

放大条件：定向写需要知道别人的线程 id，而顶层 `ls process_data` 恰好泄露全部线程名（见 ⑦）——① 与 ⑦ 是配套的一对，只修 ① 会让“拿到 id”这一步照旧。

**② grep 结果可泄漏他人 process_data 内容片段**（**已修**，2026-09-18）
护栏对缺省 path 的 grep 显式不裁决（“由静态层结果过滤兜底”），但静态层对 process_data 的 allow 是**全线程通配**（`/workspace/nl2sql_process_data/**`），结果过滤（`filesystem.py` 的 `_filter_grep_matches_by_permission`）只查静态权限、不认识线程归属——`grep("口径")` 的命中可把其它线程文件的**内容片段**带回来。“静态层兜底”在这条路径上兜不住，被委托的那层本身是通配的。

**归因修正（2026-09-18 复核）**：泄漏的成因不是“**缺省** path”，而是“**被搜索的路径覆盖了 process_data 根**”。`grep(pattern, path="/workspace")`、`path="/workspace/nl2sql_process_data"` 与不带 path 漏得一模一样——`_candidate_paths` 对 grep 取到的 path 不在 process_data 前缀下时不裁决，直接放行。只堵“无 path”是打地鼠（模型补一个 `path="/workspace"` 就回来了），必须按“搜索范围是否覆盖他人线程目录”判定。

### 中风险：依赖假设成立

**③ large_tool_results 的“按 id 寻址窄化”可被枚举击穿**：`ls /workspace/large_tool_results/` 可枚举出所有会话的文件名（即全部 tool_call_id），随后任意读回——不是纯寻址，枚举一步就破。低概率被模型主动走通，但泄漏面真实存在。

**④ fail-open 触发面比注释里写的宽**：线程 id 解析链四跳，任一上游改动（deepagents 升级致 patch 静默失效、Langfuse 关闭时 metadata 注入变化）都会让生产流量**无声降级为无隔离**，且没有告警指标。fail-open 方向合理（可用性优先），缺的是降级发生率监控。

**⑤ 隔离粒度是“会话”不是“查询”**：`_thread_id` 是会话级 id，同会话并发多查询的 N 个子任务共享同一 process_data 目录（靠 `{qid}_` 文件名前缀区分），并行子任务间可互读中间产物。若目标是“每个查询独立生成”则不满足；若“同会话允许继承上下文”是产品设定，则属 by design——建议在文档里明确表态。

### 低风险 / 设计自洽但值得知道

**⑥ 路径变体**（⚠️ **2026-09-18 复核订正：原文判断反了，实为真实泄漏，已修**）
原文称「`_norm` 不折叠 `./` → 护栏跳过裁决，但静态层同样不匹配 → 仍被 deny，歪打正着地安全」。**用 deepagents 真实 `validate_path` + `_check_fs_permission` 对拍后推翻**：

| 原始入参 | 归一化结果（`validate_path`） | 静态层判定 |
|---|---|---|
| `workspace/nl2sql_process_data/<other>/x.json` | `/workspace/nl2sql_process_data/<other>/x.json` | **allow** |
| `/workspace/./nl2sql_process_data/<other>/x.json` | 同上 | **allow** |
| `/workspace//nl2sql_process_data//<other>/x.json` | 同上 | **allow** |

根因：工具侧是**先归一、再判权限**，而静态层的 `allow /workspace/nl2sql_process_data/**` 是对**归一后**的路径生效的。所以归一变体在静态层和护栏层**同时**被放行——护栏是唯一能辨线程归属的那一层，它一跳过就真读了。原文「两层都不匹配 → 都严」的推理错在把静态层的 glob 也想成对原始串匹配。**已修：`_norm` 改走 `posixpath.normpath`，与 `validate_path` 的 `os.path.normpath` 对齐。** 大小写仍不归一（下文延伸分析二的 ⑥ 原描述保留其余部分）。

**⑦ 顶层 ls 暴露所有线程目录名**（**已修**，2026-09-18）：顶层 ls 的入参仍放行（仅暴露线程名、不给内容），但结果经 `_postfilter` 剔除他人线程项——配合 ①/② 的组合路径不再成立。

**⑧ 主 agent 完全无主题隔离**：可读历史报告，其内容会进入委派指令（`task_instructions`），等于**间接**给子任务注入历史锚点——与 S2-1 “答案不应是历史会话的函数”的目标并未彻底绝缘，只是路径更隐晦。

### 收紧方案（按性价比排序）

| # | 措施 | 堵住 | 状态 |
|---|---|---|---|
| 1 | 写侧至少禁写其它线程的 process_data 子目录（工具边界同款裁决，成本极低）。`write_file`/`edit_file` 入参字段名均为 `file_path`（已核 schema），且要求绝对路径，字符串前缀判定足够 | ① | ✅ 已实施 |
| 2 | 读侧按“搜索范围是否覆盖 process_data 根”收口：`grep` 在**输入端**拒搜（被搜索路径包含 process_data 根且不等于自有线程目录 → 拒并要求收窄 path）；`ls`/`glob` 因返回体是 `str(list[str])`，改为对结果 `ast.literal_eval` 后置过滤（顺带剔除顶层 ls 里的他人线程名） | ②⑦ | ✅ 已实施 |
| 2b | 附带：`_norm` 对齐 `validate_path`（折叠 `.` 段）——修掉「缺前导斜杠 / `.` 段 / 双斜杠」三种归一变体的读绕过 | ⑥ | ✅ 已实施（实施中发现） |
| 3 | large_tool_results 拒绝 ls 顶层、只放行精确文件路径读（把让渡收窄到“指针读回”而非“枚举”） | ③ | ⬜ 未做 |
| 4 | 加“thread_id 解析失败率”计数日志/指标；对 ⑤⑧ 在产品文档里明确是设定还是缺口 | ④⑤⑧ | ⬜ 未做 |

其中 1 与 S2-1 初衷直接相关，属“治理未完成”；2、3 是已声明让渡的扩散；其余可观察后决策。
1、2、2b 已于 2026-09-18 离线实施并回归（73/73），**待发版**（需重启后端生效）。

## 延伸分析三：原高风险①（execute 绕过）的撤销依据与相邻缺陷（2026-09-18 复核）

### ① 撤销：nl2sql 子 agent 根本没有 `execute` 工具

原①主张“shell 读等价内容畅通无阻——当前隔离体系最大的洞”。实测**这条通道不存在**，证据链五环：

| # | 事实 | 位置 |
|---|---|---|
| 1 | 子 agent 的 `CompositeBackend.default = workspace_data_backend`（`DynamicFilesystemBackend`，继承 `FilesystemBackend`），**不是** `LocalShellBackend` | `nl2sql_agent.py:95-105` |
| 2 | `supports_execution(CompositeBackend)` == `isinstance(backend.default, SandboxBackendProtocol)` | `filesystem.py:653-672` |
| 3 | 实测：`supports_execution(nl2sql composite) == False`；`main composite == True` | 直接构造两边 backend 调用 |
| 4 | `wrap_model_call` 在 `supports_execution == False` 时**把 `execute` 从 `request.tools` 中剔除**（async 版同）——**每轮模型调用都摘一次** | `filesystem.py:1890-1901` / `1958-1969` |
| 5 | `resolved_tools` 只来自 MCP 白名单，无 shell 类工具；`execute_guard` 只挂在主 agent 链上（子 agent 链不需要它） | `nl2sql_agent.py:68-72`、`main_agent.py:248` |

本机 trace 落库的直接实证（`shared/trace/traces.sqlite`，73 次 subagent spawn / 1300 条 `nl2sql_agent` 事件）：

```
chat_agent     execute       8   ← shell 只出现在主 agent
nl2sql_agent   execute       0   ← 从未出现
nl2sql_agent   grep          2 / read_file 12 / write_file 4 / write_todos 46
```

文档当时引用的 `nl2sql_agent.py:90` 注释「nl2sql_agent **不需要 shell backend** 和 memory backend」，正是这条事实的原文——当时把“没有 shell”读成了“有 shell 但没护栏”。

**原①的示例命令本身也是错的**。实测 `_check_command`：

```
cat nl2sql_process_data/01a06a51-aaaa/eval_subject.json   -> DENY（被“工作区外路径”误拦，非线程裁决）
cat /workspace/nl2sql_process_data/other/x.json           -> ALLOW  ← 真洞，且方向相反
```

真洞是**绝对 VFS 形式**（`/workspace` ∈ `execute_guard._ALLOWED_PREFIXES` 直接放行），而它属于**主 agent**——主 agent 按设计读得到全部 process_data，这正是本文件 ⑧（原⑨）已声明的让渡。**把 ⑧ 重列成高风险①，是同一条缺口被计了两次。**

> 结论：① 删除，不修。若产品上确要收紧，落点应是“主 agent 的 execute 是否该按线程裁 process_data”，属产品决策（主 agent 读别人 report 与读别人 process_data 敏感性相当），不是“补护栏”。

### ② 相邻缺陷：`execute_guard._PATH_TOKEN_RE` 缺左边界 → 大面积误杀（**已修**）

核查①时顺带实测发现，`execute_guard.py` 的第一个分支 `(/(?:[^\s;|&<>"'`()]*))` **没有左边界锚定**（对比 `_DESTRUCTIVE_RE` 有 `(?<![\w./-])`），于是相对路径里的 `/` 也会起一个匹配：

```
cat data/sales.csv                   -> DENY   ← 正常相对路径
python scripts/save_chart.py out.csv -> DENY   ← 正常脚本
ls report/                           -> DENY   ← 连 ls 目录都拒
```

`nl2sql_process_data/01a06a51/x.json` 因此被切成 token `/01a06a51/x.json` 判为“工作区外”。**结论：主 agent 的 shell 基本处于残废状态**（除非生产把 `EXECUTE_GUARD_STRICT=0`）——功能/可用性缺陷，天天在生效。

**2026-09-18 已修**，实际是三处（写成一处会留下两个同族漏网）：

| # | 改动 | 修掉的症状 |
|---|---|---|
| 1 | 第一分支加左边界 `(?<![\w./\\*?\[\]-])`（排除路径字符 **和 glob 元字符**） | `data/sales.csv`、`ls report/`、`a/*/b.json` 被截成伪 token |
| 2 | 第三分支 `[A-Za-z]:\\` → `[A-Za-z]:[\\/]`（同时认 `X:/`） | `D:/…` 在盘符后的 `/` 处被第一分支截成 `/…`（丢盘符）→ **工作区内的物理路径也被判越界** |
| 3 | 左边界类含 `*?[]` | `nl2sql_process_data/*/x.json` 被截成 `/x.json` |

**为什么加左边界不是放水**：shell 的 cwd 就是活跃工作区，相对路径天然落在工作区内，本就不该由“工作区外绝对路径”这条规则裁决；真正的越界只由 `../` 表达，由第四分支兜住（`foo/../../etc/passwd` 仍 DENY）。

**回归**：`D:\tmp\test_execute_guard_path_token.py`（40/40，零网络）两侧都守——误杀侧（相对路径、单段文件名、`1/2` 这类表达式除号、glob、工作区内物理路径）必须 ALLOW；漏拦侧（`/etc`、`/tmp`、`ls /`、`../`、`~/`、盘符越界、`rm`/`del`）必须 DENY。附 token 提取形状断言。

> 该脚本同时暴露一个**测试陷阱**：`_check_command` 的 `workspace`/`shared_root` 必须是 `Path`（生产由 `_resolve_roots()` 保证）。传 `str` 会让 `_is_outside` 里的 `.resolve()` 抛 `AttributeError`，被外层 `except Exception: return True` 吞成 DENY——同一命令传 str 得 DENY、传 Path 得 ALLOW，容易误判成“规则正确”。

## 高风险两条的具体修复设计（2026-09-18 复核修订，**已实现**）

> **实施状态**：本节两条已于 2026-09-18 落地（`filesystem_thread_guard.py` 重写，同步/异步双路径），离线回归 **73/73**（`D:\tmp\test_fs_thread_guard_p1p2.py`）；相邻 `execute_guard` 回归 40/40、22/22，`VfsPathResolver` 13/13 未受影响。**待发版**（生产尚未重启后端）。
> 落地时在既有读护栏上额外发现并修复两条**同源绕过**（`.` 段 / 缺前导斜杠，见「核心实现 › 两条实测确认的路径变体绕过」）——它们与本节两条共用 `_to_vfs` 这一根因，属于同一把尺子的问题。
> 未做：开关（下文「两条共性实施约束」里的 `NL2SQL_FS_GUARD_SCOPE`）未加——本条本身是收口而非放宽，默认即最严；若将来要放开“全工作区 grep”，再加开关。
> 本节设计描述保留原文，实际代码与之的差异已在上节「核心实现」如实反映（如 grep 显式指向他人线程目录时用读越权文案而非收窄文案）。
>
> 原为三条，`execute` 那条已**撤销**（依据见「延伸分析三」）。核心原则不变：不另起判定逻辑，**两条均复用本护栏已有的 `_session_thread_id` + `_process_data_thread` + `_norm`**，把“读/写”两条通道收敛到同一把尺子，避免判定漂移。

### 前置事实（已核 deepagents 源码，2026-09-18 复核）

- **写工具入参路径字段名为 `file_path`**（与 read_file 同），且 schema 要求绝对路径；`write_file`/`edit_file` 经 CompositeBackend 按前缀路由到活跃工作区。✅ 复核通过（`WriteFileSchema.file_path` / `EditFileSchema.file_path`）。
- **本护栏在 `wrap_tool_call` 层拿到的是格式化后的 `ToolMessage`，不是结构化结果** —— ⚠️ **原表述需订正**。`GrepResult`/`GrepMatch` 确实由 CompositeBackend 产出且 path 带完整 VFS 前缀，但 `FilesystemMiddleware` 返回前已经 `_format_grep_tool_result()` **把它格式化成文本**了。各工具返回体的真实形态：

  | 工具 | `ToolMessage.content` 真实形态 | 可解析性 |
  |---|---|---|
  | `grep`（默认 `files_with_matches`） | `"/path/a\n/path/b"`（行==路径） | 精确可解析 |
  | `grep`（`count`） | `"/path/a: 3"` | 需按 `": "` 切分 |
  | `grep`（`content`） | `"/path/a:\n  12: 命中行"` | 需区分 `/` 开头的 header 与 `  ` 开头的命中行 |
  | `ls` / `glob` | `str(list[str])`（先截断成合法 list 再 `str()`） | `ast.literal_eval` 精确可解析，**无截断残段问题** |

  另有 `"No matches found"`、`Error...\n\nPartial matches:` 前缀、字符串截断（`str` 分支 `result[:N] + TRUNCATION_GUIDANCE`，**会切断行**）三种非正常形态。**结论：grep 不适合做结果后置过滤（原③设计的前提不成立），`ls`/`glob` 适合且很干净。**

### ① 写侧无隔离 → 把写工具纳入线程裁决

当前 `_guard` 首步对非读工具直接放行。方案：受约束工具集从 `_READ_TOOLS` 扩到含 `write_file`/`edit_file`，**仅对“写向别的线程 process_data 子目录”收紧**，其余写行为维持静态层原有粒度：

```text
name ∈ {write_file, edit_file}：取 args["file_path"]
  落在 /workspace/nl2sql_process_data/<seg>/ 且 seg != own_thread → deny
  其余（写 report/、写自己 process_data、写 large_tool_results） → 放行，交静态层
```

要点：这是**增量收紧**，不砍“子 agent 可写活跃工作区”既有能力；写 deny 与读 deny 不对称合理（读别人=泄题，写别人=投毒污染）。

⚠️ **实施订正**：原文称「`file_path` 已是绝对路径（schema 强制），字符串前缀判定足够，无需处理相对形式」——**不成立**。schema 只是*要求*绝对路径，工具**并不拒绝**相对形式，而是经 `validate_path` 归一后照常执行（这正是读侧绕过能成立的机制）。故写侧与读侧共用 `_to_vfs`，`write_file("workspace/nl2sql_process_data/<其它>/a.json")` 同样被拦（已入回归）。

### ② grep 泄漏他人片段 → **输入端收窄**，不做结果解析

**为什么换掉“结果后置过滤”**：本护栏拿到的是格式化文本（见前置事实），解析它要同时应付三种 mode、`No matches found`、error 前缀与“切断行”的截断；而“解析失败则整次 deny”会把这些常见良性情况全变成报错。**更根本的是，洞的成因在输入侧**——只要被搜索范围覆盖 process_data 根，结果里就必然混入他人线程的命中。堵在输入侧更短、更稳，且天然覆盖全部形态：

```text
name == "grep"：
  target = args["path"]（缺省视为后端根 = 活跃工作区）
  若 target 覆盖 process_data 根（process_data_root 位于 target 之下）
     且 target 不落在自有线程目录内
       → deny，提示把 path 收窄到 /workspace/nl2sql_process_data/{own_thread}/
              或改用 /shared、语义层 MCP 工具（get_instructions / recall_queries）
  其余（path 就是自有线程目录 / 在 /shared 下 / 与 process_data 无关）→ 放行
```

要点：
- 覆盖三种漏法——无 path、`path="/workspace"`、`path="/workspace/nl2sql_process_data"`，不靠枚举字符串白名单。
- 复用 `_norm` + `_process_data_thread`/前缀比较，不新增判定真源。
- 代价：模型不能一次 grep 整个工作区（须先收窄）。对隔离目标这是可接受代价；`/shared` 与自有线程目录不受影响。
- 若将来仍要“全工作区 grep”，再叠加结果过滤作为**增强而非替代**：优先只处理默认的 `files_with_matches`（行==路径，零歧义），`content`/`count` 维持拒搜。

**配套（⑦ 顶层枚举）**：`ls`/`glob` 返回体是合法 list repr，后置过滤干净且安全：

```text
result = handler(request)                          # 正常执行（不阻断）
name ∈ {ls, glob} 且 content 可 literal_eval 出 list：
  剔除 _process_data_thread(item) 命中且 != own_thread 的项
  → 覆盖「ls process_data 顶层列出他人线程名」（⑦）
     与「glob("**/x") 无 path 枚举出他人线程文件路径」两条组合路径
content 以 "Error:" 开头 / 非 list（错误分支）→ 原样放行
literal_eval 失败 → 理论上不出现（list 先被截断成合法 list 再 str()）；真出现则保留原结果并记 WARNING，不整次 deny
```

### 两条共性实施约束

| 维度 | 建议（含实施结果） |
|---|---|
| 单点真源 | 均复用 `_session_thread_id`/`_process_data_thread`/`_norm`，不复制判定逻辑 ✅ 已遵守（新增 `_to_vfs`/`_covers_process_data` 两个纯函数，未另起真源） |
| 开关 | 各加独立环境变量（如 `NL2SQL_FS_GUARD_SCOPE=process_data\|full`），默认保守、可灰度回退 ⬜ **未加**：本条是收口而非放宽，默认即最严；将来若要放开“全工作区 grep”再加 |
| fail 方向 | 输入侧（grep 收窄）按“范围是否覆盖他人线程”**从严拒绝**；结果侧（ls/glob）能精确解析则精确过滤、解析不出则保留并记 WARNING ✅ 已按此实施；仅“线程 id 本身取不到”保留现有 fail-open（配④监控）✅ |
| 回归重点 | 单测覆盖：写自己/写别人 process_data、`./` 与大小写变体、grep 多种 path 形态的拒与放、ls/glob 过滤后仍是合法 list repr ✅ `D:\tmp\test_fs_thread_guard_p1p2.py` 73/73（含同步与异步两条路径）。**注**：原文「确保改动后仍 fail-closed、不破坏⑥现有安全」的前提（⑥ 本来是安全的）已被推翻——⑥ 是真实泄漏，见上 |

## 关联文件

- `langfuse_span.py`：`_thread_id` 来源；产物落盘目录口径
- `query_result_offload.py`：同目录写方
- `file_permissions.py`：静态层（互补第一道）。**注意它是按归一后的路径匹配的**，所以「静态层兜底」对路径变体不成立（见 ⑥）
- `execute_guard.py`：execute 护栏。**只服务主 agent**——子 agent 的 backend 不支持执行，`execute` 工具在 `wrap_model_call` 阶段即被 deepagents 摘除，故与本护栏的线程隔离无关（见「延伸分析三」）。其 `_PATH_TOKEN_RE` 缺左边界导致相对路径大面积误杀，2026-09-18 已修（同见「延伸分析三」②）
- `main_agent.py` / `nl2sql_agent.py`：CompositeBackend 路由与两套权限的挂载点（本护栏挂在 `nl2sql_agent.py:235`，主 agent 不挂）
- 回归脚本：`D:\tmp\test_fs_thread_guard_p1p2.py`（本护栏 73/73）、`D:\tmp\test_execute_guard.py`（22/22）、`D:\tmp\test_execute_guard_path_token.py`（40/40）
