"""代理文件读写安全控制 —— deepagents FilesystemPermission 声明式规则。

多工作区 VFS 设计（2026-08-28 收紧为「共享只读 / 工作区可读写 / 代码根禁读写」；
同日外部基础目录：`/` 兜底路由由 src/agent/ 改为 AGENT_DATA_ROOT 数据根）：
- "/shared/memory/" → shared_memory_backend（共享 memory，只读）
- "/shared/skills/" → shared_skills_backend（共享 skills，只读）
- "/workspace/"     → workspace_data_backend（前端动态选择的活跃工作区，可读可写）
- "/"               → vfs_root_backend（VFS 根 = AGENT_DATA_ROOT 项目外数据根，
                      代码根 src/agent/ 彻底退出 VFS；未配置时回退 src/agent/，读写均拒绝）

读权限边界：**只有 /shared/**（共享 memory/skills）与 /workspace/**（前端动态选择的
工作区）可读**；数据根其余路径、代码根 src/agent/ 及一切未知路径读写均拒绝。
写权限边界：**只有 /workspace/**（前端动态选择的工作区）可写**。

注意：规则按声明顺序解析，第一条命中即胜，无命中默认放行（deepagents
`_check_fs_permission`）—— 所以每条操作必须是「先 allow、再 deny /** 兜底」，
顺序不能颠倒，否则 deny 会先命中导致 allow 目录也失效。
"""
from deepagents import FilesystemPermission

FILE_PERMISSIONS: list[FilesystemPermission] = [
    # 1) 可读：共享 memory/skills + 前端动态选择的工作区
    FilesystemPermission(operations=["read"], paths=["/shared/**", "/workspace/**"], mode="allow"),
    # 2) 其余路径读操作一律拒绝（代码根 src/agent/ 除 shared 外、及一切未知路径）
    FilesystemPermission(operations=["read"], paths=["/**"], mode="deny"),
    # 3) 可写：仅前端动态选择的工作区
    FilesystemPermission(operations=["write"], paths=["/workspace/**"], mode="allow"),
    # 4) 其余路径写操作一律拒绝（default allow → 必须显式 deny 兜底）
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]

# nl2sql 子 agent 专用（同题多跑答案不一致归因 S2-1）：读范围收窄，禁跨线程读。
# 与 FILE_PERMISSIONS 的差异仅「读」：/workspace/** 收窄为
#   - /workspace/large_tool_results/**（大结果表落盘，子 agent 读回自己 offload 的完整结果）
#   - /workspace/nl2sql_process_data/**（本次流水线中间产物；线程级细粒度由
#     FilesystemThreadGuardMiddleware 在工具边界二次裁决，只放行自有线程目录）
# 从而 deny 掉 conversation_history/、report/、tmp/、工作区根文件与一切未列路径。
# 写规则不变（process_data / large_tool_results / conversation_history 自动压缩 offload
# 仍需写活跃工作区）。
NL2SQL_FILE_PERMISSIONS: list[FilesystemPermission] = [
    # 1) 可读：共享 memory/skills
    FilesystemPermission(operations=["read"], paths=["/shared/**"], mode="allow"),
    # 2) 可读：大结果表落盘文件（工具调用 id 键控，非会话目录，允许读回）
    FilesystemPermission(operations=["read"], paths=["/workspace/large_tool_results/**"], mode="allow"),
    # 3) 可读：本次流水线中间产物（线程级护栏二次裁决，见 filesystem_thread_guard）
    FilesystemPermission(operations=["read"], paths=["/workspace/nl2sql_process_data/**"], mode="allow"),
    # 4) 其余路径读一律拒绝（conversation_history / report / tmp / 工作区根 / 未知路径）
    FilesystemPermission(operations=["read"], paths=["/**"], mode="deny"),
    # 5) 可写：仅前端动态选择的工作区
    FilesystemPermission(operations=["write"], paths=["/workspace/**"], mode="allow"),
    # 6) 其余路径写操作一律拒绝
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]
