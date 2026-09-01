# -*- coding: utf-8 -*-
"""M4 版本管理：把本地 prompt 文件同步为 Langfuse Prompt（text 型）。

Langfuse UI 编辑 prompt 是版本管理的入口；本脚本负责「本地文件 → Langfuse」的单向
初始化/同步，保证云端与磁盘基线一致，之后改动一律在 Langfuse UI 完成（打标签即发版）。

用法（PYTHONPATH=src，脚本自载项目 .env）：
    python -m agent.prompt.sync_prompts --all                          # 同步主/子两个 system prompt
    python -m agent.prompt.sync_prompts --name main_system_prompt      # 同步单个（默认文件映射）
    python -m agent.prompt.sync_prompts --name main_system_prompt --file prompt/MAIN_AGENT_PROMPT.md
    python -m agent.prompt.sync_prompts --name X --file Y --label staging --commit "..."   # 打 staging 标签
    python -m agent.prompt.sync_prompts --all --force                  # 内容未变也强制新版本
    python -m agent.prompt.sync_prompts --skills                       # M6：同步全部 14 个 SKILL.md
    python -m agent.prompt.sync_prompts --skills --label staging       # skill 打 staging 标签（A/B）

标签语义：默认 production+latest；--label staging 只打 staging+latest（production 不动，
供 A/B 灰度用）。「latest」恒指最新版本，「production」指当前对外版本（M5 分流可改 prod-a/prod-b）。
M6：`--skills` 把 shared_skills_dir 下每个 SKILL.md 同步为 prompt `skill/{group}/{skill_dir}`，
trace metadata.skills 会带 Langfuse 解析版本 + source 标记（见 skill_manifest 注释）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 项目根：src/agent/prompt/ → 上溯 3 层
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 默认 prompt 名 → 本地文件（相对项目根）
_DEFAULT_FILES = {
    "main_system_prompt": "src/agent/prompt/MAIN_AGENT_PROMPT.md",
    "nl2sql_system_prompt": "src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md",
}


def _load_env() -> None:
    """加载项目根 .env（幂等；langfuse 密钥/开关都在这）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(_PROJECT_ROOT / ".env", override=False)
    except Exception:  # noqa: BLE001
        pass


def _get_client():
    from agent.trace.langfuse_client import get_client

    return get_client()


def _current_text(name: str, label: str) -> str:
    """当前 label 命中的正文（无则空串）。"""
    from agent.trace.langfuse_client import get_prompt_text

    return get_prompt_text(name, label=label, fallback="", cache_ttl_seconds=0)


def _push(name: str, text: str, label: str, commit: str) -> None:
    from agent.trace.langfuse_client import create_prompt

    labels = ["latest", label]  # latest 恒指最新；label 决定对外版本
    if not create_prompt(name, text, labels=labels, commit_message=commit):
        print(f"  ✗ {name} 上传失败", flush=True)
        sys.exit(1)
    try:
        p = _get_client().get_prompt(name, label=label, type="text", cache_ttl_seconds=0)
        print(
            f"  ✓ {name} v{p.version} labels={p.labels} chars={len(text)}",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ {name} 已上传，但回读校验失败: {e}", flush=True)


def sync(name: str, file_path: str | None, label: str, commit: str, force: bool) -> None:
    resolved = file_path or _DEFAULT_FILES.get(name)
    if not resolved:
        print(f"  ✗ {name}：未提供 --file，也不在默认映射 {list(_DEFAULT_FILES)}", flush=True)
        sys.exit(1)
    path = Path(resolved)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    if not path.is_file():
        print(f"  ✗ 文件不存在: {path}", flush=True)
        sys.exit(1)

    text = path.read_text(encoding="utf-8")
    cur = _current_text(name, "production")
    if not force and cur == text:
        print(f"  · {name} 与 production 内容一致，跳过（--force 可强制新版本）", flush=True)
        return
    print(f"  → {name} <- {path} (label={label})", flush=True)
    _push(name, text, label, commit)


# ── M6：Skill 资产同步（每个 SKILL.md = 一个 Langfuse prompt `skill/{group}/{skill_dir}`）──

def sync_skills(label: str, commit: str, force: bool) -> None:
    """把 shared_skills_dir/{main,nl2sql}/*/SKILL.md 同步为 Langfuse text prompt。

    - prompt 名：`skill/{entry['path']}`（path 如 `main/chart-saver` / `nl2sql/nl2sql-sql-generation`），
      与 M6 trace metadata.skills 里 `entry['path']` 一一对应（skill_manifest 用
      `get_prompt_version(f"skill/{entry['path']}")` 解析版本，两边必须同名）。
    - 标签：默认 production+latest；`--label staging` 只打 staging+latest（A/B 灰度）。
    - 跳过逻辑同 system prompt：与 production 内容一致则跳过，`--force` 强制新版本。
    """
    from agent.workspace_manager import get_workspace_manager
    from agent.trace.skill_manifest import build_skill_manifest

    skills_root = get_workspace_manager().shared_skills_dir
    entries = build_skill_manifest(skills_root)
    if not entries:
        print(f"  ✗ 未扫描到 SKILL.md（{skills_root} 下 {('main','nl2sql')}/*/SKILL.md）", flush=True)
        sys.exit(1)
    print(f"  → 扫描到 {len(entries)} 个 skill：{skills_root}", flush=True)

    for entry in entries:
        prompt_name = f"skill/{entry['path']}"  # 与 M6 manifest 解析一致
        skill_md = skills_root / entry["path"] / "SKILL.md"
        if not skill_md.is_file():
            print(f"  · {prompt_name}：SKILL.md 缺失，跳过", flush=True)
            continue
        sync(prompt_name, str(skill_md), label, commit, force)


def main(argv: list[str] | None = None) -> None:
    # Windows 控制台默认 GBK，⚠/✓/→ 等符号会炸编码——强制 UTF-8（与 start_server 同处理）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    _load_env()
    ap = argparse.ArgumentParser(description="同步本地 prompt 到 Langfuse")
    ap.add_argument("--all", action="store_true", help="同步所有默认 prompt")
    ap.add_argument("--name", default="", help="prompt 名（默认映射 main_system_prompt / nl2sql_system_prompt）")
    ap.add_argument("--file", default=None, help="本地 prompt 文件（相对项目根或绝对路径）")
    ap.add_argument("--skills", action="store_true", help="M6：同步 shared_skills_dir 下全部 SKILL.md 为 skill/* prompt")
    ap.add_argument("--label", default="production", help="打上的标签（production/staging/prod-a/...）")
    ap.add_argument("--commit", default="", help="本次变更说明")
    ap.add_argument("--force", action="store_true", help="内容未变也强制新版本")
    args = ap.parse_args(argv)

    if args.skills:
        sync_skills(args.label, args.commit, args.force)
    elif args.all:
        names = list(_DEFAULT_FILES)
        for n in names:
            sync(n, args.file, args.label, args.commit, args.force)
    elif args.name:
        sync(args.name, args.file, args.label, args.commit, args.force)
    else:
        print("需指定 --name / --all / --skills", flush=True)
        sys.exit(1)

    print("SYNC_DONE", flush=True)


if __name__ == "__main__":
    main()
