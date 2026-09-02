"""git 仓库操作封装（语义库管理用）。

用 subprocess 调系统 `git`（不引入 GitPython 依赖）。只做语义库管理需要的
最小面：浅克隆（branch/tag）、读取仓库元信息（remote/branch/commit/tag）。

安全约束（由调用方 + 本模块共同保证）：
- repo_url 只允许 http(s)/ssh，禁止 git://、本地路径、file:// 等协议；
- 所有命令用 list 传参（`shell=False`），杜绝 shell 注入；
- 克隆/删除目标路径必须在 workspace 白名单内（调用方校验）。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

_REMOTE_URL_RE = re.compile(r"^(?:https?|ssh)://", re.IGNORECASE)

# ── SSH 密钥（语义库 ssh:// 推送用）────────────────────────────
# 密钥放持久卷（AGENT_DATA_ROOT，容器内=/app/data/.ssh），重建镜像不丢；
# 若放在 /root/.ssh，容器一 recreate 就消失，GitLab 上已挂的 key 全部失效。
_SSH_ENSURED = False


def ssh_dir() -> Path:
    """SSH 密钥目录（持久卷下）。"""
    root = os.environ.get("AGENT_DATA_ROOT", "/app/data")
    return Path(root) / ".ssh"


def ensure_ssh_key() -> Path:
    """确保持久卷下存在 ed25519 私钥（首启/空卷自动生成），幂等。返回私钥路径。"""
    global _SSH_ENSURED
    d = ssh_dir()
    priv = d / "id_ed25519"
    if priv.exists() and _SSH_ENSURED:
        return priv
    if os.name != "nt":  # 生成/权限只对 Linux 容器有意义；Windows 本地开发不落盘
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        except OSError:
            pass
        if not priv.exists():
            try:
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-q",
                     "-C", "nl2sql-push"],
                    capture_output=True, check=False, stdin=subprocess.DEVNULL,
                )
            except OSError:
                pass
        for f in (priv, priv.with_suffix(".pub")):
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
    _SSH_ENSURED = True
    return priv


def _git_ssh_command() -> str:
    """git 走 ssh:// 时使用的 ssh 命令：锁定持久卷里的私钥 + known_hosts，
    自动接受新指纹、禁交互/密码（纯密钥），避免无 TTY 服务进程挂起。"""
    d = ssh_dir()
    return (
        f"ssh -i {d / 'id_ed25519'} -o IdentitiesOnly=yes "
        f"-o UserKnownHostsFile={d / 'known_hosts'} "
        "-o StrictHostKeyChecking=accept-new -o BatchMode=yes -o PasswordAuthentication=no"
    )


def _run(args: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[bool, str]:
    """执行 git 命令，返回 (ok, output)。异常统一转 (False, 错误信息)。"""
    ensure_ssh_key()  # 保证 GIT_SSH_COMMAND 引用的私钥在持久卷上（幂等）
    cmd = ["git"]
    if cwd:
        # git 2.35+ dubious ownership 检查：仓库目录属主≠进程用户（如容器 root vs
        # 挂载卷 uid1000）时拒绝执行，报 fatal: detected dubious ownership。
        # 用命令级 `-c safe.directory=<cwd>` 按仓库路径豁免——不依赖全局 gitconfig
        # （容器重建后全局配置会丢），作用域仅限本次操作的 cwd。
        cmd += ["-c", f"safe.directory={cwd}"]
    cmd += list(args)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": _git_ssh_command(),
            },
        )
    except FileNotFoundError:
        return False, "git 未安装或不在 PATH"
    except subprocess.TimeoutExpired:
        return False, f"git {' '.join(args[:2])} 超时（>{timeout}s）"
    except Exception as e:  # noqa: BLE001
        return False, f"git 执行失败: {e}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, (err or out or f"git 退出码 {proc.returncode}")
    return True, out


def validate_repo_url(url: str) -> str:
    """校验并返回归一化的 repo_url；非法时抛 ValueError。

    只允许 http(s)/ssh。用 list 传参 + 非 shell 执行，即便 URL 含特殊字符也不会
    被 shell 解释，但显式白名单协议可进一步杜绝 `--upload-pack` 等注入面。
    """
    url = (url or "").strip()
    if not _REMOTE_URL_RE.match(url):
        raise ValueError("repo_url 必须是 http(s) 或 ssh 地址")
    if any(c in url for c in ("\n", "\r", "\0")):
        raise ValueError("repo_url 含非法字符")
    return url


def clone_shallow(repo_url: str, ref: str, dest: str, timeout: int = 300) -> str:
    """浅克隆 repo_url 到 dest（ref 可为 branch 或 tag）。返回 dest；失败抛 RuntimeError。

    用 `--depth 1 --branch <ref>`：对 branch 直接拉取；对 tag，现代 git 的
    `--branch <tag>` 也能检出对应 tag（浅克隆）。`dest` 须为不存在的目录，
    若已存在则抛错（避免覆盖已有项目）。
    """
    repo_url = validate_repo_url(repo_url)
    dest_path = Path(dest)
    if dest_path.exists():
        raise RuntimeError(f"目标目录已存在: {dest}")
    args = ["clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += [repo_url, str(dest_path)]
    ok, out = _run(args, timeout=timeout)
    if not ok:
        # 清理可能残留的半成品目录
        if dest_path.exists():
            import shutil

            shutil.rmtree(dest_path, ignore_errors=True)
        raise RuntimeError(f"git clone 失败: {out}")
    return str(dest_path)


def repo_info(path: str) -> Optional[dict]:
    """读取仓库元信息。非 git 仓库返回 None。

    返回 {remote, branch, commit, tag}，各字段可能为空串（如无 remote / 无 tag）。
    """
    p = Path(path)
    if not (p / ".git").exists():
        return None
    cwd = str(p)

    def _git(*args: str) -> str:
        ok, out = _run(list(args), cwd=cwd, timeout=15)
        return out if ok else ""

    remote = _git("remote", "get-url", "origin")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    commit = _git("rev-parse", "--short", "HEAD")
    tag = _git("describe", "--tags", "--exact-match", "HEAD")
    if not commit:
        # 空仓库 / 无提交：至少确认它确实是个 git 目录，避免返回全空的信息
        return {"remote": remote, "branch": branch, "commit": commit, "tag": tag}
    return {"remote": remote, "branch": branch, "commit": commit, "tag": tag}


def checkout(path: str, ref: str, timeout: int = 300) -> str:
    """切换到指定 ref（branch 或 tag）。浅克隆历史不全时可能失败，返回错误信息。

    用于「本地多版本切换」场景；本期前端不直接暴露，保留供后续版本管理用。
    """
    ok, out = _run(["fetch", "origin", "--tags"], cwd=path, timeout=timeout)
    if not ok:
        return f"fetch 失败: {out}"
    ok, out = _run(["checkout", ref], cwd=path, timeout=timeout)
    if not ok:
        return f"checkout 失败: {out}"
    return ""
