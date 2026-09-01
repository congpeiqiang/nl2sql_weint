r"""Windows `\\?\` 前缀不一致补丁 —— 修复 deepagents FilesystemBackend 误报越界。

问题：Python 3.13（Windows）下 `Path.resolve()` → `ntpath.realpath` 用
`GetFinalPathNameByHandle` 拿到 `\\?\D:\...` 扩展长度前缀，再按「重新解析去掉前缀
的路径、与带前缀结果比对」来决定是否剥离。该比对在文件系统状态变化（并发创建目录/
文件）时会失败，导致 `full` 偶尔保留 `\\?\` 前缀，而 `self.cwd`（__init__ 时解析一次）
通常无前缀 → `full.relative_to(self.cwd)` 误抛 "outside root directory"。

症状：write_file 到 `/workspace/nl2sql_process_data/{thread_id}/...` 时报
``ValueError: Path:\\?\D:\... outside root directory: D:\...``，且时好时坏。

修复：在 containment 检查前把两侧的 `\\?\`（及 `\\?\UNC\`）前缀都剥离，
使检查与 Windows 扩展长度前缀无关。其余逻辑（遍历拦截、symlink 环检测）原样保留。

使用：在创建任何 `FilesystemBackend` 实例前 import 本模块即可（`_resolve_path`
是实例方法，运行时按类属性查找，patch 类属性即对已建实例生效）。
"""
from __future__ import annotations

from pathlib import Path

from deepagents.backends.filesystem import (
    FilesystemBackend,
    _raise_if_symlink_loop,
)

_ORIGINAL_RESOLVE = FilesystemBackend._resolve_path


def _strip_extended_prefix(p: str) -> str:
    """剥掉 Windows 扩展长度前缀：`\\\\?\\UNC\\` → `\\\\`，`\\\\?\\` → ``。"""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[len("\\\\?\\UNC\\"):]
    if p.startswith("\\\\?\\"):
        return p[len("\\\\?\\"):]
    return p


def _resolve_path_fixed(self: FilesystemBackend, key: str) -> Path:
    if not self.virtual_mode:
        return _ORIGINAL_RESOLVE(self, key)

    vpath = key if key.startswith("/") else "/" + key
    if ".." in vpath or vpath.startswith("~"):
        raise ValueError("Path traversal not allowed")

    # 两侧都归一化掉 `\\?\` 前缀，避免 Path.resolve() 非确定性前缀导致的
    # relative_to 误判越界。
    cwd = Path(_strip_extended_prefix(str(self.cwd)))
    full = Path(_strip_extended_prefix(str((cwd / vpath.lstrip("/")).resolve())))
    try:
        full.relative_to(cwd)
    except ValueError:
        raise ValueError(f"Path:{full} outside root directory: {cwd}") from None
    _raise_if_symlink_loop(full)
    return full


# 只 patch 一次，避免重复 import 时多次包裹
if FilesystemBackend._resolve_path is _ORIGINAL_RESOLVE:
    FilesystemBackend._resolve_path = _resolve_path_fixed
