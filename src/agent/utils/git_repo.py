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
    ensure_ssh_key()  # 容器首启生成持久卷私钥（幂等）；Windows 开发机不落盘
    cmd = ["git"]
    if cwd:
        # git 2.35+ dubious ownership 检查：仓库目录属主≠进程用户（如容器 root vs
        # 挂载卷 uid1000）时拒绝执行，报 fatal: detected dubious ownership。
        # 用命令级 `-c safe.directory=<cwd>` 按仓库路径豁免——不依赖全局 gitconfig
        # （容器重建后全局配置会丢），作用域仅限本次操作的 cwd。
        cmd += ["-c", f"safe.directory={cwd}"]
    cmd += list(args)
    # ssh:// origin 时锁定持久卷私钥：仅当密钥真实存在（容器内）才注入 GIT_SSH_COMMAND。
    # Windows 开发机 ensure_ssh_key 不落盘 → 不注入，走用户自身 ssh（agent/凭据），
    # 否则 `-i /app/data/.ssh/id_ed25519`（不存在的路径）会让一切 ssh git 操作必败。
    _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if (ssh_dir() / "id_ed25519").exists():
        _env["GIT_SSH_COMMAND"] = _git_ssh_command()
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
            env=_env,
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

    ⚠ **ref 传 tag 的副作用**（2026-09-09 生产事故根因）：`--depth 1` 隐含
    `--single-branch`，会按 ref 写死 `remote.origin.fetch`——传 tag 时写的是
    `+refs/tags/<tag>:refs/tags/<tag>`，且 HEAD 处于 detached。此后裸
    `git pull origin`（无 refspec）只会拉那一个 tag、退出码 0、报 "Already up to
    date"，源文件纹丝不动（调用方看到 HTTP 200 却什么都没变）。**服务态语义库
    应传 branch**；已按 tag 建的仓库请用 `pull_ref()` 更新（它不依赖
    remote.origin.fetch 与当前 HEAD 位置）。
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


# ── 远程 ref 探测与「更新到指定 ref」（语义库更新用）────────────
_TAG_PEEL = "^{}"


def _current_branch(cwd: str) -> str:
    """当前分支名；detached HEAD / 空仓库返回空串。"""
    ok, out = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, timeout=15)
    b = out.strip() if ok else ""
    return "" if b in ("", "HEAD") else b


def _rev_parse(cwd: str, rev: str, timeout: int = 15) -> str:
    """解析 rev 为完整 sha；失败返回空串。"""
    ok, out = _run(["rev-parse", rev], cwd=cwd, timeout=timeout)
    return out.strip() if ok else ""


def _log_subject(cwd: str, rev: str) -> str:
    """取 rev 的提交标题（浅仓库也能取到 FETCH_HEAD 的）。失败返回空串。"""
    ok, out = _run(["log", "-1", "--format=%s", rev], cwd=cwd, timeout=15)
    return out.strip() if ok else ""


def list_remote_refs(cwd: str, timeout: int = 30) -> dict:
    """列出远程分支与 tag，并解析远程默认分支。失败返回空结构。

    用 `ls-remote --symref origin HEAD refs/heads/* refs/tags/*`：
    - `ref: refs/heads/main\tHEAD` 行给出远程默认分支；
    - 其余每行 `<sha>\t<refname>`；注解 tag 额外有一行 `refs/tags/x^{}`
      （解引用到 commit），按 tag 名去重并保留 tag 对象那一行。
    """
    empty = {"default_branch": "", "branches": [], "tags": []}
    ok, out = _run(
        ["ls-remote", "--symref", "origin", "HEAD", "refs/heads/*", "refs/tags/*"],
        cwd=cwd,
        timeout=timeout,
    )
    if not ok:
        return empty

    res = {"default_branch": "", "branches": [], "tags": []}
    seen_tags: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("ref:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                res["default_branch"] = parts[1][len("refs/heads/"):]
            continue
        sha, _, refname = line.partition("\t")
        sha, refname = sha.strip(), refname.strip()
        if not sha or not refname:
            continue
        if refname.startswith("refs/heads/"):
            res["branches"].append({"name": refname[len("refs/heads/"):], "sha": sha})
        elif refname.startswith("refs/tags/"):
            tag = refname[len("refs/tags/"):]
            if tag.endswith(_TAG_PEEL) or tag in seen_tags:
                continue
            seen_tags.add(tag)
            res["tags"].append({"name": tag, "sha": sha})
    return res


def _pull_fail(msg: str) -> dict:
    return {
        "ok": False, "message": msg, "kind": "", "ref": "",
        "commit": "", "changed": False, "rebuild_required": False,
    }


def pull_ref(cwd: str, ref: str = "", timeout: int = 120) -> dict:
    """把仓库更新到远程指定 ref（分支或 tag）；ref 为空 → 远程默认分支最新。

    返回 {"ok", "message", "kind", "ref", "commit", "changed", "rebuild_required"}；
    ok=False 时 message 为失败原因（前端直接展示）。

    为什么不裸 `git pull origin`（2026-09-09 生产事故）：
    语义库常由 `clone --depth 1 --branch <tag>` 建立，git 会把
    `remote.origin.fetch` 锁成 `+refs/tags/<tag>:refs/tags/<tag>` 且 HEAD detached；
    此时 `git pull origin` 只拉那一个 tag、退出码 0、报 "Already up to date"，
    用户看到 HTTP 200 而源文件纹丝不动。故改为：ls-remote 解析 → 显式 refspec
    fetch → 对齐 HEAD，全程不依赖 remote.origin.fetch 与当前 HEAD 位置。

    安全护栏（宁可拒绝也不覆盖用户数据）：
    - 本地有未提交改动（`target/` 构建产物除外，它可再生）→ 拒绝；
    - 本地分支 ≠ 其远程跟踪 ref（有未推送提交 / 无跟踪记录）→ 拒绝。
    """
    if not (Path(cwd) / ".git").exists():
        return _pull_fail("该目录不是 Git 仓库")

    remote = list_remote_refs(cwd, timeout=timeout)
    branches = {b["name"] for b in remote["branches"]}
    tags = {t["name"] for t in remote["tags"]}

    if ref:
        if ref in branches:
            kind, target = "branch", ref
        elif ref in tags:
            kind, target = "tag", ref
        else:
            avail = "、".join(sorted(branches) + sorted(tags)) or "（远程无分支/tag）"
            return _pull_fail(f"远程不存在分支或 tag「{ref}」；可用：{avail}")
    else:
        kind = "branch"
        cur = _current_branch(cwd)
        target = remote["default_branch"] or cur
        if target not in branches:
            target = next((c for c in (cur, "main", "master") if c in branches), "")
        if not target:
            avail = "、".join(sorted(branches)) or "（远程无分支）"
            return _pull_fail(f"无法确定远程默认分支；可用：{avail}")

    # 本地改动护栏：未提交改动（排除 target/ 构建产物）会让 checkout 覆盖用户工作。
    # 用 `diff --name-only`（已暂存 + 未暂存）而非 status --porcelain：后者每行带
    # 状态前缀，而 _run 会 strip 掉整段输出的前导空格 → 首个条目路径少一个字符。
    # 未跟踪文件不列入（checkout 不会覆盖它们）。
    dirty_blocking: list[str] = []
    dirty_target: list[str] = []
    dirty: list[str] = []
    for _args in (["diff", "--name-only"], ["diff", "--name-only", "--cached"]):
        ok_d, out_d = _run(_args, cwd=cwd, timeout=30)
        if ok_d and out_d:
            dirty.extend(p.strip() for p in out_d.splitlines() if p.strip())
    for path in dict.fromkeys(dirty):
        (dirty_target if path.startswith("target/") else dirty_blocking).append(path)
    if dirty_blocking:
        shown = "、".join(dirty_blocking[:5]) + ("…" if len(dirty_blocking) > 5 else "")
        return _pull_fail(f"本地有未提交改动，已中止以免覆盖：{shown}（请先提交或撤销后重试）")

    # 未推送提交护栏（放在 fetch 前，用两个本地 ref 比对，不依赖历史）：
    # 本地分支 sha ≠ 远程跟踪 ref sha → 本地多出了提交（push 失败/手动 commit），
    # checkout -B 会 reset 掉它。**不能用 rev-list 判祖先**：浅仓库的 FETCH_HEAD
    # 父提交被截断，"落后"会被误判成"领先"，正常更新也会被拒（实测）。
    if kind == "branch":
        local_sha = _rev_parse(cwd, f"refs/heads/{target}")
        if local_sha:
            anchor = _rev_parse(cwd, f"refs/remotes/origin/{target}")
            if not anchor:
                return _pull_fail(
                    f"本地分支 {target} 无远程跟踪记录，无法确认是否有未推送提交；"
                    f"请先手动 git fetch origin 对齐后再更新"
                )
            if local_sha != anchor:
                return _pull_fail(
                    f"本地分支 {target} 与上次拉取的远程状态不一致（可能有未推送提交），"
                    f"已中止以免覆盖；请先「推送 Git」或手动对齐后重试"
                )

    old_sha = _rev_parse(cwd, "HEAD")

    # 显式 refspec fetch：不依赖 remote.origin.fetch 当前值（tag 锁死也能拉分支）
    if kind == "branch":
        spec = f"+refs/heads/{target}:refs/remotes/origin/{target}"
    else:
        spec = f"+refs/tags/{target}:refs/tags/{target}"
    ok_fetch, out_fetch = _run(
        ["fetch", "origin", spec, "--depth", "1"], cwd=cwd, timeout=timeout
    )
    if not ok_fetch:
        return _pull_fail(f"git fetch 失败: {out_fetch}")

    new_sha = _rev_parse(cwd, "FETCH_HEAD")
    if not new_sha:
        return _pull_fail("fetch 成功但解析 FETCH_HEAD 失败（仓库状态异常）")

    # 顺手修复被 tag 锁死的 refspec（best-effort，失败不影响本次更新）
    if kind == "branch":
        ok_cfg, cfg_out = _run(["config", "--get", "remote.origin.fetch"], cwd=cwd, timeout=15)
        if not ok_cfg or "refs/heads/*" not in cfg_out:
            _run(
                ["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
                cwd=cwd, timeout=15,
            )

    force = ["-f"] if dirty_target else []
    if kind == "branch":
        ok_co, out_co = _run(
            ["checkout", *force, "-B", target, f"origin/{target}"], cwd=cwd, timeout=timeout
        )
        if ok_co:
            _run(["branch", "--set-upstream-to", f"origin/{target}", target], cwd=cwd, timeout=15)
    else:
        ok_co, out_co = _run(
            ["checkout", *force, "--detach", f"refs/tags/{target}"], cwd=cwd, timeout=timeout
        )
    if not ok_co:
        return _pull_fail(f"git checkout 失败: {out_co}")

    changed = old_sha != new_sha
    head = _rev_parse(cwd, "HEAD") or new_sha
    label = f"{target}@{head[:7]}" if kind == "branch" else f"tag {target}@{head[:7]}"

    # 是否需要重新构建：源码/知识变更不会自动进 target/mdl.json（wrenai 读的是产物）
    rebuild_required = False
    if changed:
        ok_diff, diff_out = _run(
            ["diff", "--name-only", old_sha, new_sha], cwd=cwd, timeout=30
        )
        if not ok_diff:
            rebuild_required = True  # 历史不可比（浅仓库/分叉）→ 保守提示
        else:
            rebuild_required = any(
                p.startswith(("models/", "knowledge/")) or p == "wren_project.yml"
                for p in diff_out.splitlines()
                if p.strip()
            )

    if changed:
        subject = _log_subject(cwd, head)
        msg = f"已更新到 {label}" + (f"（{subject}）" if subject else "")
    else:
        msg = f"已是最新（{label}）"
    if rebuild_required:
        msg += "；源文件已变，需点「构建」后生效（构建产物即刻被 MCP 读取，无需重启后端）"

    return {
        "ok": True, "message": msg, "kind": kind, "ref": target,
        "commit": head, "changed": changed, "rebuild_required": rebuild_required,
    }


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
