"""
定期清除开发环境 checkpoint 数据。

checkpoint（checkpoints.sqlite）是 LangGraph 的线程状态持久化文件，
会随对话/查询累积膨胀（可达数百 MB），拖慢每次 run 启动加载、
history 拉取与 sync 高频写入。本脚本用于在开发环境定期清空它。
后端重启时 checkpointer_factory 会自动重建空库，无需额外操作。

用法：
    python scripts/clean_checkpoint.py           # 直接清除
    python scripts/clean_checkpoint.py --check   # 只检查占用情况，不删除

建议通过 Windows 任务计划 / cron 定期运行（如每天一次），
或在每次手动重启后端前执行。
"""
import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env(path: str) -> dict[str, str]:
    """轻量解析 .env（处理引号与空白），不依赖第三方库。"""
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _resolve_db_path() -> str:
    """从 .env 的 CHECKPOINT_DB_PATH 解析 checkpoint 目录绝对路径。"""
    env = _load_env(os.path.join(PROJECT_ROOT, ".env"))
    raw = env.get("CHECKPOINT_DB_PATH", "").strip()
    if not raw:
        # 与 checkpointer_factory 的默认一致
        return os.path.join(PROJECT_ROOT, "src", "agent", "workspace", "checkpoint")
    raw = os.path.expandvars(raw)
    return raw if os.path.isabs(raw) else os.path.join(PROJECT_ROOT, raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="清除开发环境 checkpoint 数据")
    parser.add_argument("--check", action="store_true", help="只检查占用情况，不删除")
    args = parser.parse_args()

    db_path = _resolve_db_path()
    targets = ["checkpoints.sqlite", "checkpoints.sqlite-wal", "checkpoints.sqlite-shm"]

    total = 0
    locked: list[str] = []
    for name in targets:
        path = os.path.join(db_path, name)
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        try:
            if not args.check:
                os.remove(path)
        except PermissionError:
            # Windows 下被进程独占打开的文件无法删除。
            # 若后端（langgraph/uvicorn）正在运行，sqlite 文件通常被占用。
            locked.append(path)
            continue
        except FileNotFoundError:
            # 竞态：检查后文件已被删除（如后端重启重建），忽略
            continue
        total += size
        action = "deleted" if not args.check else "would delete"
        print(f"[{action}] {os.path.relpath(path, PROJECT_ROOT)} ({size / 1024 / 1024:.1f} MB)")

    if locked:
        print("\nThe following files are locked (backend may be running):")
        for p in locked:
            print("  ", p)
        print("Stop the backend (langgraph/uvicorn) first, then re-run this script.")
        return 1

    verb = "deleted" if not args.check else "would delete"
    print(f"\nTotal {verb}: {total / 1024 / 1024:.1f} MB (backend auto-recreates an empty DB on startup)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
