# -*- coding: utf-8 -*-
"""P2-10② skill 版本物化（`skills_versioning.materialize_skills_ref`）回归。

背景（2026-09-24 审计，详见 `docs/生产就绪度评估/优化改进TODO清单.md` P2-10②）：物化目录
`skill_refs/<safe_ref(ref)>` 只由 ref 决定，而缓存键 `f"{src}@{ref}"`、`_key_lock(key)` 与
marker 都含 src ⇒ 同一个 ref 的两个来源（生产 env 的 `SKILLS_REF=<path>@<ref>` 与
`api/experiment.py` 的 `materialize_skills_ref(ref)`，后者 src=""）**各拿一把锁、共用同一个
目录**，后到的会把前一个正在服务的技能目录 `rmtree` 掉再覆写（正在跑的 run 技能凭空换内容）；
且物化是「rmtree 活目录 + 原地重建」，读者会读到残缺目录。本脚本钉住四件事：

  ① **目录身份含 src**：不同来源各用各的目录；且 `effective_skills_sources` 拼出的 VFS 路径
     必须命中 `materialize_skills_ref` 真正物化的那个目录（否则「物化了但读不到」）；
  ② **两种形态都能物化**：仓库子目录形态（`src/agent/shared/skills`，生产/开发机默认源）
     与独立仓库形态（src 直指 skill 根）；marker 快路径跨进程可用；换 ref 不动旧目录；
  ③ **失败不留半成品、不动已有现场**，换入失败按「物化失败」返回 None；
  ④ **暂存 + 原子换入**：并发下同 key 只物化一次、不同 key 并行，读者永远只看到
     「不存在」或「完整目录」两态，不留 `.stage-` / `.trash-` 残留。

跑法：`PYTHONIOENCODING=utf-8 uv run python scripts/verify_skills_ref_materialize.py`
"""
from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import PropertyMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

PASS = 0
FAIL = 0
WORK: Path = Path()


def check(cond: bool, label: str, extra: object = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" -> {extra!r}" if extra != "" else ""))
    return bool(cond)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/")] = sha(p)
    return out


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.quotepath=false", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )


def write_skills(root: Path, mark: str) -> None:
    """skill 组目录形态：<root>/{main,nl2sql}/<skill>/SKILL.md（SkillsMiddleware 消费格式）。"""
    for group in ("main", "nl2sql"):
        d = root / group / f"{group}-skill"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {group}-skill\nmark: {mark}\n---\n{mark}\n",
                                    encoding="utf-8")


def setup_repo(base: Path, name: str, *, nested: bool, marks: tuple[str, ...]) -> tuple[Path, Path]:
    """真 git 仓库：`nested=True` 时 skill 在 `src/agent/shared/skills/` 子目录（app 主仓库
    形态），否则就在仓库根（独立 skill 仓库形态）。每个 mark 打一个同名前缀 tag。

    返回 (仓库路径, skill 根目录)。
    """
    repo = base / name
    sub = repo / "src" / "agent" / "shared" / "skills" if nested else repo
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "main")
    for mark in marks:
        write_skills(sub, mark)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", f"{name} {mark}")
        git(repo, "tag", mark)
    return repo, sub


def leftovers(parent: Path) -> list[str]:
    """物化目录下的残留（暂存/换出/克隆用隐藏目录）。"""
    if not parent.exists():
        return []
    return sorted(p.name for p in parent.iterdir()
                  if p.name.startswith((".stage-", ".trash-", ".work_")))


def refs_dir() -> Path:
    return WORK / "offline_experiment" / "skill_refs"


def skills_ref_env(value: str | None):
    """临时设置 SKILLS_REF（None = 删除），退出时还原。"""
    class _Env:
        def __enter__(self):
            self.old = os.environ.get("SKILLS_REF")
            if value is None:
                os.environ.pop("SKILLS_REF", None)
            else:
                os.environ["SKILLS_REF"] = value
            return self

        def __exit__(self, *exc):
            if self.old is None:
                os.environ.pop("SKILLS_REF", None)
            else:
                os.environ["SKILLS_REF"] = self.old
            return False

    return _Env()


# ══ ① 目录身份 + VFS 路径同源 ═══════════════════════════════
def section_identity() -> None:
    print("\n① 目录身份：src 必须进目录名，且与 effective_skills_sources 拼的路径同源")
    import agent.utils.skills_versioning as S

    base_dir = S._ref_dir_name("skills-v1")
    path_dir = S._ref_dir_name("skills-v1", "/opt/libs/skills")
    check(base_dir != path_dir, "src 空 vs src=path ⇒ 不同目录（旧实现是同一个）",
          (base_dir, path_dir))
    check(base_dir == "base_skills-v1", "src 空 ⇒ 'base_<safe_ref>'", base_dir)

    a = S._ref_dir_name("skills-v1", "/a/libs/skills")
    b = S._ref_dir_name("skills-v1", "/c/d/e/skills")
    check(a != b, "末段同名、路径不同的两个 src 也不撞（路径哈希进了目录名）", (a, b))
    check(a == S._ref_dir_name("skills-v1", "/a/libs/skills"), "同一 src ⇒ 稳定同名")
    check(a != S._ref_dir_name("skills-v2", "/a/libs/skills"), "同 src 不同 ref ⇒ 不同目录")

    weird = S._ref_dir_name("skills/v1.0.2", "C:\\libs\\我的 skills")
    check(":" not in weird and "/" not in weird and "\\" not in weird,
          "Windows 路径/中文不会带出分隔符、盘符冒号（目录名安全）", weird)
    check(S._ref_dir_name("skills/v1.0.2") == "base_skills_v1.0.2",
          "ref 里的 / 仍走 _safe_ref 归一", S._ref_dir_name("skills/v1.0.2"))
    check(S._src_tag("") == "base" and S._src_tag("/a/libs/skills").startswith("skills-")
          and len(S._src_tag("/a/libs/skills")) > len("skills-"),
          "_src_tag：空 → 'base'，非空 → '<名>-<哈希>'",
          (S._src_tag(""), S._src_tag("/a/libs/skills")))


# ══ ② 物化端到端 + marker 快路径 + 两来源隔离 ════════════════
def section_materialize() -> None:
    print("\n② 物化：仓库子目录/独立仓库两种形态、marker 快路径、两来源互不干扰、VFS 路径可读")
    import agent.utils.skills_versioning as S

    base = WORK / "sec2"
    repo_a, skills_a = setup_repo(base, "repoA", nested=True, marks=("skills-v1", "skills-v2"))
    repo_b, skills_b = setup_repo(base, "repoB", nested=False, marks=("skills-v1",))

    calls = {"n": 0}
    real_archive = S.git_archive_materialize

    def counting(source: Path, ref: str, dest_root: Path):
        calls["n"] += 1
        return real_archive(source, ref, dest_root)

    S.git_archive_materialize = counting
    S.reset_skills_cache()
    old_base = S._default_skills_base
    S._default_skills_base = lambda: skills_a  # src="" 的默认源 = repoA 的 skills 子目录
    try:
        root_a = refs_dir() / "base_skills-v1"
        p1 = S.materialize_skills_ref("skills-v1")
        check(p1 is not None and Path(p1) == root_a,
              "src='' 物化到 base_<ref> 目录", p1)
        check(p1 is not None and (Path(p1) / "main" / "main-skill" / "SKILL.md").is_file(),
              "仓库子目录形态：archive 前缀被提升，main/ 直接在物化根下")
        check((root_a / "nl2sql" / "nl2sql-skill" / "SKILL.md").read_text(encoding="utf-8")
              .strip().endswith("skills-v1"), "内容是 v1 版本")
        check((root_a / ".skills_ok").read_text(encoding="utf-8") == "@skills-v1",
              "marker 记录完整 key（src@ref）", (root_a / ".skills_ok").read_text(encoding="utf-8"))
        check(calls["n"] == 1, "首次物化跑了一次 archive", calls)

        check(S.materialize_skills_ref("skills-v1") == p1 and calls["n"] == 1,
              "进程缓存命中：不再 archive")

        S.reset_skills_cache()
        check(S.materialize_skills_ref("skills-v1") == p1 and calls["n"] == 1,
              "清掉进程缓存（=新进程）后仍走**磁盘 marker 快路径**", calls)

        # VFS 路径同源：effective_skills_sources 拼的串必须指向真物化出来的目录
        from agent.workspace_manager import OFFLINE_EXPERIMENT_DIR_NAME

        with skills_ref_env("skills-v1"):
            sources = S.effective_skills_sources(["/shared/skills/main/"], "main")
        check(sources == [f"/{OFFLINE_EXPERIMENT_DIR_NAME}/skill_refs/base_skills-v1/main/"],
              "★ VFS sources 用的就是物化目录名（旧实现两侧各自拼串）", sources)
        vfs_phys = WORK / sources[0].lstrip("/")
        check((vfs_phys / "main-skill" / "SKILL.md").is_file(),
              "★ 返回的 VFS 路径在磁盘上真能读到 SKILL.md（不是「物化了但读不到」）", str(vfs_phys))

        # 换 ref：新目录，旧目录一个字节都不动
        before_a = manifest(root_a)
        p2 = S.materialize_skills_ref("skills-v2")
        check(p2 is not None and Path(p2) != root_a, "换 ref ⇒ 另一个目录", (p1, p2))
        check((Path(p2) / "main" / "main-skill" / "SKILL.md").read_text(encoding="utf-8")
              .strip().endswith("skills-v2"), "v2 内容是 v2")
        check(manifest(root_a) == before_a, "v1 目录没被 v2 物化碰过（逐字节一致）")

        # 关键回归：同一 ref、**两个来源**（显式 path@ref vs src=""）
        src_b = str(skills_b.resolve())
        root_b = refs_dir() / S._ref_dir_name("skills-v1", src_b)
        check(root_b != root_a, "两个来源的物化目录不同（旧实现共用一个目录）")
        p_b = S.materialize_skills_ref("skills-v1", src_b)
        check(p_b is not None and Path(p_b) == root_b,
              "独立仓库形态（src 直指 skill 根）也能物化", p_b)
        check((root_b / ".skills_ok").read_text(encoding="utf-8") == f"{src_b}@skills-v1",
              "显式 src 的 marker 身份用绝对路径",
              (root_b / ".skills_ok").read_text(encoding="utf-8"))
        check(manifest(root_a) == before_a,
              "★ 两个来源都物化后，先来的那份（src=''）仍然逐字节未动")
        S.reset_skills_cache()
        check(S.materialize_skills_ref("skills-v1", src_b) == p_b,
              "显式 src 也能走磁盘快路径（marker 一致 + tag 不可变）")

        with skills_ref_env(f"{src_b}@skills-v1"):
            sources_b = S.effective_skills_sources(["/shared/skills/main/"], "main")
        check(sources_b == [f"/{OFFLINE_EXPERIMENT_DIR_NAME}/skill_refs/"
                            f"{S._ref_dir_name('skills-v1', src_b)}/main/"],
              "★ `path@ref` 形态的 VFS 路径也落在自己的目录里（两来源不会互指）", sources_b)
        check((WORK / sources_b[0].lstrip("/") / "main-skill" / "SKILL.md").is_file(),
              "★ `path@ref` 的 VFS 路径也能真读到技能")

        check(leftovers(refs_dir()) == [], "物化目录下无暂存/换出残留", leftovers(refs_dir()))
    finally:
        S.git_archive_materialize = real_archive
        S._default_skills_base = old_base
        S.reset_skills_cache()


# ══ ③ 失败路径 ══════════════════════════════════════════════
def section_failure() -> None:
    print("\n③ 失败路径：不留半成品、不动已有现场、换入失败按物化失败处理")
    import agent.utils.skills_versioning as S

    base = WORK / "sec3"
    plain = base / "plain"
    write_skills(plain, "v1")  # 不是 git 仓库 ⇒ archive 必然失败
    src = str(plain.resolve())

    root = refs_dir() / S._ref_dir_name("skills-v1", src)
    r = S.materialize_skills_ref("skills-v1", src)
    check(r is None, "非 git 源 ⇒ 物化失败返回 None（不静默退化）", r)
    check(not root.exists(), "失败时物化根根本没被创建（半成品不可见）", str(root))
    check(leftovers(refs_dir()) == [], "失败不留暂存残留", leftovers(refs_dir()))

    # 已有目录（上次成功物化的结果，marker 是**别的 ref**）→ 失败不得删它
    root2 = refs_dir() / S._ref_dir_name("skills-v2", src)
    root2.mkdir(parents=True)
    (root2 / "keep.txt").write_text("上一个版本\n", encoding="utf-8")
    S._write_marker(root2, f"{src}@skills-v0")
    before = manifest(root2)
    r2 = S.materialize_skills_ref("skills-v2", src)
    check(r2 is None, "同目录、不同 ref 物化失败仍返回 None", r2)
    check(root2.exists() and manifest(root2) == before,
          "失败不动物化过的现场（旧实现先 rmtree 再失败 ⇒ 现场没了）")
    check(leftovers(root2.parent) == [], "失败也不留暂存残留")

    # 换入失败：旧目录仍在 → 按「物化失败」返回 None，且不能把已被清掉的 stage 当结果
    real_install = S._install_staged

    def boom(stage: Path, dest_root: Path):
        raise OSError("模拟换入失败（Windows 上 rename 被占用）")

    S._install_staged = boom
    try:
        root3 = refs_dir() / S._ref_dir_name("skills-v3", src)
        root3.mkdir(parents=True)
        (root3 / "keep.txt").write_text("上个版本\n", encoding="utf-8")
        S._write_marker(root3, f"{src}@skills-v0")
        before3 = manifest(root3)
        # 用一个真能物化的源（repoA 的 skills 子目录），只让**换入**这一步失败
        repo_a, skills_a = setup_repo(base, "repoA3", nested=True, marks=("skills-v3",))
        S.reset_skills_cache()
        r3 = S.materialize_skills_ref("skills-v3", str(skills_a))
        check(r3 is None, "换入失败 ⇒ 返回 None（不当成成功路径返回 stage）", r3)
        check(manifest(root3) == before3, "换入失败时 dest 原样（回滚/未动）", manifest(root3))
        check(leftovers(refs_dir()) == [], "换入失败后暂存目录被清掉", leftovers(refs_dir()))
    finally:
        S._install_staged = real_install
        S.reset_skills_cache()


# ══ ④ 并发：按 key 串行 / 不同 key 并行 / 半成品不可见 ════════
def section_concurrency() -> None:
    print("\n④ 并发：同 key 只物化一次（区间不重叠）、不同 key 并行、换入前 dest 只有两态")
    import agent.utils.skills_versioning as S

    base = WORK / "sec4"
    repo, skills = setup_repo(base, "repoC", nested=True, marks=("skills-v1", "skills-v2"))
    real = S.git_archive_materialize
    events: list[tuple[str, str, float]] = []
    ev_lock = threading.Lock()

    def tag_of(stage_name: str) -> str:
        """暂存目录名 → 物化目录名（`.stage-<名>[-n]` → `<名>`）。"""
        name = stage_name[len(".stage-"):] if stage_name.startswith(".stage-") else stage_name
        head, _, tail = name.rpartition("-")
        return head if tail.isdigit() else name

    def slow(source: Path, ref: str, dest_root: Path):
        tag = tag_of(str(dest_root.name))
        with ev_lock:
            events.append(("START", tag, time.perf_counter()))
        trail = real(source, ref, dest_root)
        time.sleep(0.35)  # 物化已完成、还没换入 —— 这段时间 dest_root 不该出现半成品
        with ev_lock:
            events.append(("END", tag, time.perf_counter()))
        return trail

    old_base = S._default_skills_base
    S._default_skills_base = lambda: skills
    S.reset_skills_cache()
    S.git_archive_materialize = slow
    try:
        root_v1 = refs_dir() / "base_skills-v1"
        root_v2 = refs_dir() / "base_skills-v2"
        for d in (root_v1, root_v2):
            shutil.rmtree(d, ignore_errors=True)

        results: dict[str, list] = {"skills-v1": [], "skills-v2": []}
        partial: list[str] = []
        stop = False

        def worker(ref: str):
            results[ref].append(S.materialize_skills_ref(ref))

        def watcher() -> None:
            # 只允许两态：目录不存在 / 目录完整（两组技能都在且有 marker）
            while not stop:
                for root in (root_v1, root_v2):
                    if root.exists() and not (S._dest_valid(root) and (root / ".skills_ok").is_file()):
                        partial.append(f"{root.name} 可见但不完整")
                time.sleep(0.002)

        t_watch = threading.Thread(target=watcher, daemon=True)
        t_watch.start()
        threads = [threading.Thread(target=worker, args=("skills-v1",)) for _ in range(2)]
        threads.append(threading.Thread(target=worker, args=("skills-v2",)))
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        stop = True
        t_watch.join(timeout=1)

        v1, v2 = results["skills-v1"], results["skills-v2"]
        check(len(v1) == 2 and v1[0] == v1[1] and v1[0] is not None,
              "同 key 的两个线程拿到同一个目录", v1)
        check(v2 and v2[0] != v1[0] and v2[0] is not None, "不同 key 拿到各自目录", (v1, v2))
        check(partial == [], f"轮询期间从没看到半成品物化目录（{len(partial)} 次）", partial[:3])

        intervals: dict[str, list[tuple[float, float]]] = {}
        cur: dict[str, float] = {}
        for kind, tag, ts in events:
            if kind == "START":
                cur[tag] = ts
            elif tag in cur:
                intervals.setdefault(tag, []).append((cur.pop(tag), ts))
        iv1 = intervals.get("base_skills-v1", [])
        iv2 = intervals.get("base_skills-v2", [])
        check(len(iv1) == 1, f"同 key 只物化了一次（{len(iv1)} 段）——锁内快路径命中", iv1)
        check(len(iv2) == 1, f"另一 key 也只物化一次（{len(iv2)} 段）", iv2)
        if iv1 and iv2:
            # 「没被全局串行化」的判据是**区间重叠**，不是墙钟（Windows 文件系统抖动会让
            # 墙钟不可控 —— semantic 侧一开始拿墙钟当判据，偶发假失败）
            check(iv2[0][0] < iv1[0][1] and iv1[0][0] < iv2[0][1],
                  "不同 key 的两段物化**有重叠**（锁按 key 分，不是全局串行）", (iv1[0], iv2[0]))
        print(f"  [info] 三个并发调用的墙钟 {wall:.2f}s（单段 ~0.35s）")
        check(leftovers(refs_dir()) == [], "并发后不留残留", leftovers(refs_dir()))
    finally:
        S.git_archive_materialize = real
        S._default_skills_base = old_base
        S.reset_skills_cache()


# ══ ⑤ 结构断言 ══════════════════════════════════════════════
def section_structure() -> None:
    print("\n⑤ 结构：目录名含 src / 两侧同源取名 / 只在暂存目录里写")
    tree = ast.parse((SRC / "agent" / "utils" / "skills_versioning.py").read_text(encoding="utf-8"))
    fns: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fns.setdefault(node.name, node)

    def calls(fn: str) -> set[str]:
        node = fns.get(fn)
        if node is None:
            return set()
        return {
            (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
            for n in ast.walk(node) if isinstance(n, ast.Call)
        }

    def names(fn: str) -> set[str]:
        node = fns.get(fn)
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} if node else set()

    def attr_calls(fn: str) -> set[str]:
        node = fns.get(fn)
        if node is None:
            return set()
        return {
            f"{getattr(n.func.value, 'id', '?')}.{n.func.attr}"
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

    check("_ref_dir_name" in calls("materialize_skills_ref"),
          "materialize_skills_ref 的目录名走 _ref_dir_name")
    check("_ref_dir_name" in calls("effective_skills_sources"),
          "★ effective_skills_sources 的 VFS 路径也走 _ref_dir_name（两侧同源）")
    check("_src_tag" in calls("_ref_dir_name"), "_ref_dir_name 里含 src 标签")
    check("hashlib.sha1" in attr_calls("_src_tag"), "_src_tag 用路径哈希防撞名",
          sorted(attr_calls("_src_tag")))

    check("_new_stage_dir" in calls("materialize_skills_ref")
          and "_install_staged" in calls("materialize_skills_ref"),
          "慢路径：暂存目录 + 原子换入", sorted(calls("materialize_skills_ref")))
    rmtree_args = {
        getattr(n.args[0], "id", "")
        for n in ast.walk(fns["materialize_skills_ref"])
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "rmtree"
    }
    check("dest_root" not in rmtree_args and "stage" in rmtree_args,
          "materialize_skills_ref 只 rmtree 暂存目录，不碰 dest_root（旧实现的第一件事）",
          rmtree_args)
    check("stage" in names("_materialize_local") or "dest_root" in
          [a.arg for a in fns["_materialize_local"].args.args],
          "_materialize_local 收到的就是暂存目录（形参名沿用 dest_root）")
    args = [a.arg for a in fns["materialize_skills_ref"].args.args]
    check(args[:2] == ["ref", "src"], "materialize_skills_ref 签名仍是 (ref, src)", args)

    check("_rename_retry" in calls("_install_staged")
          and "shutil.rmtree" in attr_calls("_install_staged"),
          "_install_staged：rename 换入（带重试）+ 删被换出的旧目录",
          sorted(calls("_install_staged")))
    check("os.replace" in attr_calls("_rename_retry")
          and "time.sleep" in attr_calls("_rename_retry"),
          "_rename_retry 是 os.replace + 退避重试（Windows 瞬时 WinError 5）",
          sorted(attr_calls("_rename_retry")))
    check("_key_lock" in calls("materialize_skills_ref"),
          "慢路径仍取 _key_lock(key)（同 key 串行）")


def main() -> int:
    global WORK
    WORK = Path(tempfile.mkdtemp(prefix="verify_skills_ref_"))
    print(f"P2-10② skill 版本物化回归（工作目录 {WORK}）")

    from agent.workspace_manager import get_workspace_manager

    mgr = get_workspace_manager()
    p = patch.object(type(mgr), "offline_experiment_dir",
                     new_callable=PropertyMock, return_value=WORK / "offline_experiment")
    p.start()

    section_identity()
    section_materialize()
    section_failure()
    section_concurrency()
    section_structure()

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    print(f"（工作目录保留供排查：{WORK}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
