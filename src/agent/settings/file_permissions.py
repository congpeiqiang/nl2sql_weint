"""代理文件读写安全控制 —— deepagents FilesystemPermission 声明式规则。

多工作区 VFS 设计：
- "/shared/memory/" → shared_memory_backend（共享 memory，只读）
- "/shared/skills/" → shared_skills_backend（共享 skills，只读）
- "/workspace/"        → workspace_data_backend（当前工作区，可写 report/tmp/nl2sql_process_data）
- "/"                  → shared_code_backend（代码文件，只读）

注意：规则按声明顺序解析，第一条命中即胜，无命中默认放行（deepagents
`_check_fs_permission`）—— 所以必须是「先 allow 写目录、再 deny /** 兜底」，
顺序不能颠倒，否则 deny 会先命中导致写目录也写不了。
"""
from deepagents import FilesystemPermission

FILE_PERMISSIONS: list[FilesystemPermission] = [
    # 1) 只读整个 VFS 根（显式声明，防御未来默认值变化；当前默认 allow 等价）
    FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    # 2) 可写路径：共享技能/记忆 + workspace 下的运行时数据目录
    FilesystemPermission(operations=["write"], paths=[
        # 共享代码/技能/记忆（只读，但允许 write 以兼容未来需要）
        "/**",
        "/shared/skills/**",
        "/shared/memory/**",
        # 当前工作区运行时数据目录
        "/workspace/report/**",
        "/workspace/tmp/**",
        "/workspace/nl2sql_process_data/**",
        "/workspace/large_tool_results/**",
    ], mode="allow"),
    # 3) 其余路径写操作一律拒绝（default allow → 必须显式 deny 兜底）
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]
