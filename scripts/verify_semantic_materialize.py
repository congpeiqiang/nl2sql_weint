# -*- coding: utf-8 -*-
"""P2-10② 语义库**版本物化**（`semantic_db._materialize_semantic`）回归。

背景（2026-09-24 审计，详见 `docs/生产就绪度评估/优化改进TODO清单.md` P2-10②）：物化
目录 `semantic_refs/<db>/<ref>` 只由 (db, ref) 决定，而进程缓存键与 marker 都是
`(db, src, ref)` / `src|ref` ⇒ 同一个库的两个不同来源（`WREN_SEMANTIC_OVERRIDE` 的
`path@ref` 与「正在服务的库」`src=""`）**用同一个目录**，后到的会把前一个正在服务的那
份 `rmtree` 掉；而 `rmtree` + git archive + move 全程在锁外。本脚本钉住三件事：

  ① **目录身份含 src**：不同来源各用各的目录（不同 src / 同名不同路径都不撞）；
  ② **按 (db, src, ref) 串行**：同 key 并发只物化一次、区间不重叠，不同 key 仍并行；
  ③ **半成品不可见**：物化在暂存目录里做完再 rename 换入，读者永远看不到写了一半的
     目录；失败不动物化过的现场、不留残留。

跑法：`PYTHONIOENCODING=utf-8 uv run python scripts/verify_semantic_materialize.py`
"""
from __future__ import annotations

import ast
import hashlib
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


def make_project(p: Path, name: str, mark: str) -> None:
    (p / "models" / "m1").mkdir(parents=True, exist_ok=True)
    (p / "knowledge" / "glossary").mkdir(parents=True, exist_ok=True)
    (p / "wren_project.yml").write_text(
        f"name: {name}\ndataSource: mysql\nprofile: p_{name}\n", encoding="utf-8"
    )
    (p / "models" / "m1" / "metadata.yml").write_text(
        f"name: m1\nmark: {mark}\ntableReference:\n  table: t1\n  schema: public\ncolumns: []\n",
        encoding="utf-8",
    )
    (p / "knowledge" / "glossary" / "术语表.md").write_text(f"术语 {mark}\n", encoding="utf-8")


def setup_repo(base: Path, name: str, *, nested: bool, marks: tuple[str, ...] = ("v1", "v2")) -> tuple[Path, Path]:
    """建一个真实 git 仓库：`nested=True` 时项目在 `semantic/<name>/` 子目录（app 主仓库
    里那种形态），否则项目就在仓库根（独立库形态）。每个 mark 打一个 tag。

    返回 (仓库路径, 项目目录)。
    """
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)
    proj = repo / "semantic" / name if nested else repo
    git(repo, "init", "-b", "main")
    for i, mark in enumerate(marks):
        make_project(proj, name, mark)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", f"{name} {mark}")
        git(repo, "tag", mark)
        if i == 0 and nested:
            # 让后面几个 tag 的提交里也有个无关文件，避免与第一个 tag 完全同树
            (repo / "README.md").write_text("x\n", encoding="utf-8")
    return repo, proj


def leftovers(db_dir: Path) -> list[str]:
    """物化缓存目录下的残留（暂存/换出用的隐藏目录）。"""
    if not db_dir.exists():
        return []
    return sorted(p.name for p in db_dir.iterdir() if p.name.startswith((".stage-", ".old-", ".work_")))


# ══ ① 目录身份 ══════════════════════════════════════════════
def section_identity() -> None:
    print("\n① 目录身份：src 必须进目录名（不同来源不能共用目录）")
    import agent.utils.semantic_db as S

    base_dir = S._cache_dir("Chinook", "", "v1")
    path_dir = S._cache_dir("Chinook", "/opt/libs/Chinook", "v1")
    check(base_dir != path_dir, "src 空 vs src=path ⇒ 不同目录（旧实现是同一个）",
          (str(base_dir), str(path_dir)))
    check(base_dir.name == "base_v1", "src 空 ⇒ 'base_<ref>'", base_dir.name)
    check(base_dir.parent.name == "Chinook", "库名仍是一级目录", base_dir.parent.name)

    a = S._cache_dir("Chinook", "/a/libs/Chinook", "v1")
    b = S._cache_dir("Chinook", "/c/d/e/Chinook", "v1")
    check(a != b, "末段同名、路径不同的两个 src 也不撞（路径哈希进了目录名）",
          (a.name, b.name))
    check(a == S._cache_dir("Chinook", "/a/libs/Chinook", "v1"), "同一 src ⇒ 稳定同名")
    check(a != S._cache_dir("Chinook", "/a/libs/Chinook", "v2"), "同 src 不同 ref ⇒ 不同目录")
    check(S._cache_dir("Chinook", "C:\\libs\\Chinook", "v1").name.count(":") == 0
          and "/" not in S._cache_dir("Chinook", "C:\\libs\\Chinook", "v1").name,
          "Windows 路径不会带出分隔符/盘符冒号（目录名安全）")
    check(S._cache_dir("C/Weird:DB", "", "v1").parent.name == "C_Weird_DB",
          "库名仍走 _safe_ref 归一", S._cache_dir("C/Weird:DB", "", "v1").parent.name)


# ══ ② 物化端到端 + 磁盘快路径 ═══════════════════════════════
def section_materialize() -> None:
    print("\n② 物化：真 git archive（含仓库子目录形态）/ marker 快路径 / 不同来源互不干扰")
    import agent.utils.semantic_db as S

    base = WORK / "sec2"
    repo_nested, proj_nested = setup_repo(base, "libN", nested=True)
    repo_flat, proj_flat = setup_repo(base, "libF", nested=False)
    calls = {"n": 0}
    real_archive = S._git_archive_materialize

    def counting(source: Path, ref: str, dest_root: Path):
        calls["n"] += 1
        return real_archive(source, ref, dest_root)

    S._git_archive_materialize = counting
    try:
        src = str(proj_nested.resolve())
        root_a = S._cache_dir("libN", src, "v1")
        p1 = S._materialize_semantic("libN", src, "v1", repo_nested)
        check(p1 is not None and (Path(p1) / "wren_project.yml").is_file(),
              "仓库子目录型来源物化成功（wren 项目标记就在返回目录里）", p1)
        check(Path(p1) == root_a / "semantic" / "libN",
              "返回的是**项目目录**（缓存根的下级），不是缓存根", p1)
        check((Path(p1) / "knowledge" / "glossary" / "术语表.md").read_text(encoding="utf-8") == "术语 v1\n",
              "内容是 v1 版本")
        check(S._read_marker(root_a) == (f"{Path(src).resolve()}|v1", "semantic/libN"),
              "marker 写在缓存根：第一行=身份、第二行=项目相对位置", S._read_marker(root_a))
        check(calls["n"] == 1, "首次物化跑了一次 archive", calls)

        p1b = S._materialize_semantic("libN", src, "v1", repo_nested)
        check(p1b == p1 and calls["n"] == 1, "进程缓存命中：不再 archive")

        with S._semantic_override_lock:
            S._semantic_override_cache.clear()
        p1c = S._materialize_semantic("libN", src, "v1", repo_nested)
        check(p1c == p1 and calls["n"] == 1,
              "清掉进程缓存（=新进程）后仍走**磁盘 marker 快路径**（子目录形态也能命中）", calls)

        # 换 ref：新目录，旧目录一个字节都不动
        before_a = manifest(root_a)
        p2 = S._materialize_semantic("libN", src, "v2", repo_nested)
        check(p2 is not None and p2 != p1, "换 ref ⇒ 另一个目录", (p1, p2))
        check((Path(p2) / "knowledge" / "glossary" / "术语表.md").read_text(encoding="utf-8") == "术语 v2\n",
              "v2 内容是 v2")
        check(manifest(root_a) == before_a, "v1 目录没被 v2 物化碰过（逐字节一致）")

        # 关键回归：同一 db、同一 ref，**两个来源**（path@ref vs 正在服务的库）
        root_live = S._cache_dir("libN", str(proj_flat.resolve()), "v1")
        check(root_live != root_a, "两个来源的缓存目录不同（旧实现共用一个目录）")
        p_live = S._materialize_semantic("libN", "", "v1", proj_flat)
        check(p_live is not None and Path(p_live) == root_live,
              "src='' 物化到 base 自己的目录（独立库形态：项目就在缓存根）", p_live)
        check((Path(p_live) / "wren_project.yml").is_file(), "src='' 的产物是合法 wren 项目")
        check(manifest(root_a) == before_a,
              "★ 两个来源物化后，先来的那份（path@ref）仍然逐字节未动")
        check(S._read_marker(root_live) == (f"{proj_flat.resolve()}|v1", "."),
              "src='' 的 marker 身份用的是 base 的绝对路径", S._read_marker(root_live))
        with S._semantic_override_lock:
            S._semantic_override_cache.clear()
        p_live2 = S._materialize_semantic("libN", "", "v1", proj_flat)
        check(p_live2 == p_live, "src='' 也能走磁盘快路径（项目就在缓存根）")

        check(leftovers(root_a.parent) == [], "物化缓存目录下无暂存/换出残留", leftovers(root_a.parent))
    finally:
        S._git_archive_materialize = real_archive
        with S._semantic_override_lock:
            S._semantic_override_cache.clear()


# ══ ③ 失败不留半成品、不动物化过的现场 ══════════════════════
def section_failure() -> None:
    print("\n③ 失败路径：不留半成品、不动已有目录")
    import agent.utils.semantic_db as S

    base = WORK / "sec3"
    base.mkdir(parents=True, exist_ok=True)
    not_a_repo = base / "plain"
    make_project(not_a_repo, "libP", "v1")  # 不是 git 仓库 ⇒ archive 必然失败

    src = str(not_a_repo.resolve())
    root = S._cache_dir("libP", src, "v1")
    r = S._materialize_semantic("libP", src, "v1", base)
    check(r is None, "非 git 源 + 显式 src ⇒ 物化失败返回 None（不静默退化）", r)
    check(not root.exists(), "失败时缓存根根本没被创建（半成品不可见）", str(root))
    check(leftovers(root.parent) == [], "失败不留暂存残留", leftovers(root.parent))

    # 已有目录（上次成功物化的结果，marker 是**别的 ref**）→ 失败不得删它
    root2 = S._cache_dir("libP", src, "v2")
    root2.mkdir(parents=True)
    (root2 / "keep.txt").write_text("上一个版本\n", encoding="utf-8")
    S._write_marker(root2, f"{Path(src).resolve()}|v1", ".")
    before = manifest(root2)
    r2 = S._materialize_semantic("libP", src, "v2", base)
    check(r2 is None, "同目录、不同 ref 物化失败仍返回 None", r2)
    check(root2.exists() and manifest(root2) == before,
          "失败不动物化过的现场（旧实现先 rmtree 再失败 ⇒ 现场没了）")
    check(leftovers(root2.parent) == [], "失败也不留暂存残留")


# ══ ④ 并发：按 key 串行、不同 key 并行、半成品不可见 ══════════
def section_concurrency() -> None:
    print("\n④ 并发：同 key 只物化一次（区间不重叠）、不同 key 并行、换入前 dest 永不可见")
    import agent.utils.semantic_db as S

    base = WORK / "sec4"
    repo, proj = setup_repo(base, "libC", nested=True)
    repo2, proj2 = setup_repo(base, "libD", nested=False)
    real = S._git_archive_materialize
    events: list[tuple[str, str, float]] = []
    ev_lock = threading.Lock()

    def tag_of(stage_name: str) -> str:
        """段子目录名 → 缓存根名（`.stage-<根名>[-n]` → `<根名>`）。"""
        name = stage_name[len(".stage-"):] if stage_name.startswith(".stage-") else stage_name
        head, _, tail = name.rpartition("-")
        return head if tail.isdigit() else name

    def slow(source: Path, ref: str, dest_root: Path):
        tag = tag_of(str(dest_root.name))
        with ev_lock:
            events.append(("START", tag, time.perf_counter()))
        trail = real(source, ref, dest_root)
        time.sleep(0.35)  # 物化已完成、还没换入 —— 这一段时间里 dest_root 不该存在
        with ev_lock:
            events.append(("END", tag, time.perf_counter()))
        return trail

    S._git_archive_materialize = slow
    try:
        src_c, src_d = str(proj.resolve()), str(proj2.resolve())
        root_c = S._cache_dir("libC", src_c, "v1")
        results: dict[str, list] = {"libC": [], "libD": []}
        partial: list[str] = []
        stop = False

        def worker(src: str, db: str, repo_path: Path):
            results[db].append(S._materialize_semantic(db, src, "v1", repo_path))

        def watcher() -> None:
            # 轮询「缓存根」：只允许「不存在」或「标记齐全的完整物化」两种状态
            while not stop:
                if root_c.exists():
                    mk = S._read_marker(root_c)
                    if mk is None:
                        partial.append("存在但没有 marker（半成品）")
                    elif not (root_c / mk[1] / "wren_project.yml").is_file():
                        partial.append("marker 在但项目文件还没齐")
                time.sleep(0.002)

        t_watch = threading.Thread(target=watcher, daemon=True)
        t_watch.start()
        t1 = threading.Thread(target=worker, args=(src_c, "libC", repo))
        t2 = threading.Thread(target=worker, args=(src_c, "libC", repo))
        t3 = threading.Thread(target=worker, args=(src_d, "libD", repo2))
        t0 = time.perf_counter()
        for t in (t1, t2, t3):
            t.start()
        for t in (t1, t2, t3):
            t.join()
        wall = time.perf_counter() - t0
        stop = True
        t_watch.join(timeout=1)

        all_results = results["libC"] + results["libD"]
        check(len(all_results) == 3 and all(r is not None for r in all_results),
              "三个并发调用都拿到结果", results)
        check(len(results["libC"]) == 2 and results["libC"][0] == results["libC"][1]
              and results["libC"][0] is not None,
              "同 key 的两个线程拿到同一个目录", results["libC"])
        check(results["libD"][0] != results["libC"][0], "不同 key 的线程拿到各自目录", results)
        check(partial == [], f"轮询期间从没看到半成品缓存根（{len(partial)} 次）", partial[:3])

        intervals: dict[str, list[tuple[float, float]]] = {}
        cur: dict[str, float] = {}
        for kind, tag, ts in events:
            if kind == "START":
                cur[tag] = ts
            elif tag in cur:
                intervals.setdefault(tag, []).append((cur.pop(tag), ts))
        c_iv = intervals.get(root_c.name, [])
        d_iv = intervals.get(S._cache_dir("libD", src_d, "v1").name, [])
        check(len(c_iv) == 1, f"同 key 只物化了一次（{len(c_iv)} 段）——锁内快路径命中", c_iv)
        check(len(d_iv) == 1, f"另一 key 也只物化一次（{len(d_iv)} 段）", d_iv)
        if c_iv and d_iv:
            # 「没被全局串行化」的判据是**区间重叠**，不是墙钟（Windows 上文件系统抖动
            # 会让墙钟不可控 —— 一开始拿墙钟当判据，偶发假失败）
            check(d_iv[0][0] < c_iv[0][1] and c_iv[0][0] < d_iv[0][1],
                  "不同 key 的两段物化**有重叠**（锁按 key 分，不是全局串行）", (c_iv[0], d_iv[0]))
        print(f"  [info] 三个并发调用的墙钟 {wall:.2f}s（单段 ~0.35s）")
        check(leftovers(root_c.parent) == [], "并发后不留残留", leftovers(root_c.parent))
    finally:
        S._git_archive_materialize = real
        with S._semantic_override_lock:
            S._semantic_override_cache.clear()


# ══ ⑤ 结构断言 ══════════════════════════════════════════════
def section_structure() -> None:
    print("\n⑤ 结构：目录名含 src / 慢路径持锁 / 只在暂存目录里写")
    tree = ast.parse((SRC / "agent" / "utils" / "semantic_db.py").read_text(encoding="utf-8"))
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
        # 当实参传出去的名字（如 `_git_archive_materialize(source, ref, stage)` 里的 stage）
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

    check("_source_tag" in calls("_cache_dir"), "_cache_dir 的目录名由 _source_tag 参与拼出")
    args = [a.arg for a in fns["_cache_dir"].args.args]
    check(args[:3] == ["db_name", "source", "ref"], "_cache_dir 签名收了 (db_name, source, ref)", args)
    check("hashlib.sha1" in attr_calls("_source_tag"), "_source_tag 用路径哈希防撞名",
          sorted(attr_calls("_source_tag")))

    node = fns.get("_materialize_semantic")
    has_with = any(
        isinstance(n, ast.With)
        and any("_materialize_key_lock" in ast.dump(item.context_expr) for item in n.items)
        for n in ast.walk(node)
    ) if node is not None else False
    check(has_with, "_materialize_semantic 慢路径取了 _materialize_key_lock(key)")
    check({"_reuse_if_ready", "_materialize_now"} <= calls("_materialize_semantic"),
          "快路径与慢路径实体都被调用到", sorted(calls("_materialize_semantic")))

    check("stage" in names("_materialize_now"), "_materialize_now 在暂存目录上干活")
    check("dest_root" not in {
        getattr(n.args[0], "id", "") for n in ast.walk(fns["_materialize_now"])
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "rmtree"
    }, "慢路径不 rmtree dest_root（旧实现的第一件事）")

    check("_rename_retry" in calls("_install_staged")
          and "shutil.rmtree" in attr_calls("_install_staged"),
          "_install_staged：rename 换入（带重试）+ 删被换出的旧目录",
          sorted(calls("_install_staged")))
    check("os.replace" in attr_calls("_rename_retry")
          and "time.sleep" in attr_calls("_rename_retry"),
          "_rename_retry 是 os.replace + 退避重试（Windows 瞬时 WinError 5）",
          sorted(attr_calls("_rename_retry")))
    check("_read_marker" in calls("_reuse_if_ready") and "_wren_markers_hit" in calls("_reuse_if_ready"),
          "_reuse_if_ready 用 marker 身份 + 项目标记双重判据", sorted(calls("_reuse_if_ready")))
    check("_write_marker" in calls("_materialize_now"), "marker 只由慢路径写")


def main() -> int:
    global WORK
    WORK = Path(tempfile.mkdtemp(prefix="verify_materialize_"))
    print(f"P2-10② 语义库版本物化回归（工作目录 {WORK}）")

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
