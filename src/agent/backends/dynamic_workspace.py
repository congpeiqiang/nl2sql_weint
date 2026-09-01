"""动态工作区后端 —— 让 FilesystemBackend / LocalShellBackend 的 root_dir 延迟解析。

问题：main_agent.py / nl2sql_agent.py 在模块导入时捕获 `_active_workspace`，
      FilesystemBackend(root_dir=...) 固化 cwd，切换工作区后仍指向旧目录，必须重启。

方案：包装原生 backends，每次文件操作前从 WorkspaceManager 重新解析 root_dir，
      使工作区切换即时生效，无需重启后端。

用法：
    from agent.backends.dynamic_workspace import DynamicFilesystemBackend, DynamicLocalShellBackend

    data_backend = DynamicFilesystemBackend(get_root_dir=lambda: wm.active_workspace)
    shell_backend = DynamicLocalShellBackend(get_root_dir=lambda: wm.active_workspace)
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.local_shell import LocalShellBackend


class DynamicFilesystemBackend(FilesystemBackend):
    """FilesystemBackend whose root_dir is resolved lazily from a callable.

    Each file operation (_resolve_path) re-resolves root_dir, so workspace
    switching takes effect immediately without restart.
    """

    def __init__(
        self,
        get_root_dir: Callable[[], Path],
        virtual_mode: bool = True,
        max_file_size_mb: int = 10,
    ) -> None:
        self._get_root_dir = get_root_dir
        super().__init__(
            root_dir=get_root_dir(),
            virtual_mode=virtual_mode,
            max_file_size_mb=max_file_size_mb,
        )

    def _resolve_path(self, key: str) -> Path:
        # 每次文件操作前同步 cwd 到当前活跃工作区
        self.cwd = Path(self._get_root_dir()).resolve()
        return super()._resolve_path(key)


class DynamicLocalShellBackend(LocalShellBackend):
    """LocalShellBackend whose root_dir is resolved lazily from a callable.

    File operations and shell commands both re-resolve root_dir before execution.
    """

    def __init__(
        self,
        get_root_dir: Callable[[], Path],
        virtual_mode: bool = True,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        inherit_env: bool = False,
    ) -> None:
        self._get_root_dir = get_root_dir
        super().__init__(
            root_dir=get_root_dir(),
            virtual_mode=virtual_mode,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            env=env,
            inherit_env=inherit_env,
        )

    def _resolve_path(self, key: str) -> Path:
        self.cwd = Path(self._get_root_dir()).resolve()
        return super()._resolve_path(key)

    def execute(self, command: str, *, timeout: int | None = None):
        # shell 命令也使用当前工作区
        self.cwd = Path(self._get_root_dir()).resolve()
        return super().execute(command, timeout=timeout)