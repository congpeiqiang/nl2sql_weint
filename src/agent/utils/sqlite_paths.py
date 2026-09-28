# -*- coding: utf-8 -*-
"""运行时 SQLite 库的落点（**一库一目录**）+ 老文件的接管。

**为什么要有这个模块**：三个运行时库此前直接落在 `<AGENT_DATA_ROOT>/` 根上 ——
`eval_queue.sqlite`（待评队列）、`trace_bind.sqlite`（trace 归并绑定镜像）、
`pending_terminal.sqlite`（子任务终态待补写登记表）。SQLite 还会在库文件旁边生成
`-wal` / `-shm`，于是数据根目录上散着 9 个 `.sqlite*` 文件，和 `shared/`
`workspace/` `logs/` 这些目录混在一起，看不出谁是谁。

现在按「一库一目录」归位：`<AGENT_DATA_ROOT>/<name>/<name>.sqlite`
—— 三件套（库 + `-wal` + `-shm`）天然同处一个目录，根上不再有散落的 `.sqlite*`，
目录名就是那个库的含义（对齐既有先例：`auth/auth.sqlite`、
`shared/checkpoint/checkpoints.sqlite`、`shared/trace/traces.sqlite`）。

**老数据不能丢**（本模块存在的第二个理由）：升级后首次建连时，若新路径还没有库、
而根上还留着老三件套，就把它们搬过去。为什么不能不管：
- `pending_terminal.sqlite` 里可能是**还没补写成功的终态行** —— 丢掉等于永久丢掉那个
  子任务终态（进度卡卡死 + 前端自动续跑不触发，见
  `docs/weint环境/NL2SQL-部署与更新手册.md` §1.5）；
- `trace_bind.sqlite` 是**进程重启后 trace 不分裂**的依据（丢了退回「照常新开」）。

搬运规则（改代码时别退回去）：
- **目标已存在就绝不动** —— 不覆盖、不合并（老文件原地留着，人工判断）；
- **先搬 `-wal`/`-shm`，最后搬主库** —— 反过来的话主库走了、WAL 留在原地，
  已提交未 checkpoint 的事务就没了，而且下次启动「目标已存在」不会再补；
- 同一文件系统内 `os.replace` 是原子的；多进程同时抢着搬只有一个成功，
  另一个吃到 `FileNotFoundError` 静默跳过（状态自然收敛，不需要锁）；
- 任何一步失败都只 warning 并放弃**本轮**（老文件留在原地，下次启动重试），不半搬。

用法：
    from agent.utils import sqlite_paths
    p = sqlite_paths.resolve_store_db(sqlite_paths.data_root(), "pending_terminal")
进程首启把三个库一次搬齐：`adopt_all_stores()`（挂在 `api/custom_app.py::_lifespan`）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

#: 一库一目录的库名（**目录名 == 库名**；顺序 = 首启接管顺序）
STORE_NAMES: tuple[str, ...] = ("eval_queue", "trace_bind", "pending_terminal")

#: SQLite 在库文件旁边生成的附属文件。**主库必须最后搬**（见文件头）。
_SIDECARS: tuple[str, ...] = ("-wal", "-shm", "-journal")


def data_root() -> Path:
    """`AGENT_DATA_ROOT`：走 workspace manager，解析失败回退 env → 当前目录。

    （与三个 store 原先各自内联的那段 try/except 完全同构，收敛到一处。）
    """
    try:
        from agent.workspace_manager import get_workspace_manager  # 惰性，防 import 环

        return Path(get_workspace_manager().data_root)
    except Exception:  # noqa: BLE001
        return Path(os.getenv("AGENT_DATA_ROOT", "") or ".")


def store_db_path(root: Path | str, name: str) -> Path:
    """库路径 `<root>/<name>/<name>.sqlite`（**只算路径**：不建目录、不搬老文件）。

    纯函数 —— 保留策略量占用时调它，不能让"量一下"顺手搬文件。
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"库名必须是简单目录名: {name!r}")
    root = Path(root)
    return root / name / f"{name}.sqlite"


def adopt_legacy_store(root: Path | str, name: str) -> bool:
    """把 `<root>/<name>.sqlite` 三件套搬进 `<root>/<name>/`；搬了返回 True。

    幂等且安全：目标已存在、或老文件不存在 ⇒ 直接 False（什么都不做）。
    """
    root = Path(root)
    new = store_db_path(root, name)
    legacy = root / f"{name}.sqlite"
    if new.exists() or not legacy.exists():
        return False
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _logger.warning("[sqlite-paths] 建目录失败，老文件保留原地 %s: %s", new.parent, e)
        return False
    # 先搬附属文件（-wal/-shm/-journal），主库放最后
    for suffix in _SIDECARS:
        src = Path(f"{legacy}{suffix}")
        if not src.exists():
            continue
        try:
            os.replace(str(src), f"{new}{suffix}")
        except FileNotFoundError:
            continue  # 另一个进程刚搬走/清掉，不是错
        except OSError as e:
            _logger.warning(
                "[sqlite-paths] 附属文件搬不动，本轮不搬主库（下次启动重试）%s: %s", src, e
            )
            return False
    try:
        os.replace(str(legacy), str(new))
    except OSError as e:
        _logger.warning("[sqlite-paths] 主库搬不动（下次启动重试）%s: %s", legacy, e)
        return False
    _logger.info("[sqlite-paths] 老库已归位: %s → %s", legacy, new)
    return True


def resolve_store_db(root: Path | str, name: str) -> Path:
    """库的**最终落点**：接管老文件 + 建目录 + 返回路径（各 store 的 `_db_path()` 都走这里）。"""
    adopt_legacy_store(root, name)
    p = store_db_path(root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def adopt_all_stores(root: Path | str | None = None) -> dict[str, bool]:
    """进程首启把三个库一次搬齐（返回 `{库名: 是否搬了}`）。

    为什么要在 lifespan 里显式调一次：`trace_bind` / `eval_queue` 是**惰性建连**
    （第一次用到才 resolve），只靠 `resolve_store_db` 的话，没人用它们的那台机器上
    老文件会一直躺在数据根目录里 —— 正是本次要消掉的东西。
    """
    root = Path(root) if root is not None else data_root()
    return {name: adopt_legacy_store(root, name) for name in STORE_NAMES}
