"""代理文件读写安全控制 —— deepagents FilesystemPermission 声明式规则。

VFS 根 = src/agent（FilesystemBackend(root_dir=src/agent, virtual_mode=True)）。
只读：整个根（即项目里 agent 实际需要的那部分：prompt/skills/memory/yaml/workspace）；
根外路径（.env 含密钥、docs、src/mcp_server、.venv）由 virtual_mode 路径锚定天然不可达。
可写：仅 workspace/{report,tmp,nl2sql_process_data} 三个子目录，对应
D:\\code_work_space\\llm\\nl2sql\\src\\agent\\workspace\\{report,tmp,nl2sql_process_data}。

注意：规则按声明顺序解析，第一条命中即胜，无命中默认放行（deepagents
`_check_fs_permission`）—— 所以必须是「先 allow 三个写目录、再 deny /** 兜底」，
顺序不能颠倒，否则 deny 会先命中导致三个目录也写不了。
"""
from deepagents import FilesystemPermission

FILE_PERMISSIONS: list[FilesystemPermission] = [
    # 1) 只读整个 VFS 根（显式声明，防御未来默认值变化；当前默认 allow 等价）
    FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    # 2) 仅这三个 workspace 子目录可写
    FilesystemPermission(operations=["write"], paths=[
        "/workspace/report/**",
        "/workspace/tmp/**",
        "/workspace/nl2sql_process_data/**",
    ], mode="allow"),
    # 3) 其余路径写操作一律拒绝（default allow → 必须显式 deny 兜底）
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]
