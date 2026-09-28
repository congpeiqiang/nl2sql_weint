# -*- coding: utf-8 -*-
"""P2-10② 回归：语义库「更新 / 重建」必须在暂存副本里做，再整目录原子换入。

**被测的坑**：`target/mdl.json` 是**别人正在读**的文件——每次工具调用新起的
`wren serve mcp` 子进程都直接打开它（`wren_semantic._invalidate_detector` 的注释自证）。
而 wren 的 `context build` 是**原地截断重写**、`git checkout` 也就地改工作树：

- 读者会在窗口里打开「半写的 JSON」，或者**文件不存在**；
- wren 加载器对后者是硬失败（`Error: project found at ... but target/mdl.json missing.`），
  表现为该库工具全挂、甚至**重启后容器起不来**；
- 更糟的是构建中途失败/被杀，旧内容已经没了、新内容没写完 ⇒ 该库**永久**停在残缺态。

修法：写入全部在**暂存副本**里做完，最后两次同盘 `rename` 换入（`_stage_and_swap` /
`_swap_in`），失败就**根本不换**。本脚本用**假 wren 二进制**驱动**真实文件系统**去证明
这一点，而不是断言「代码里调用了某个函数」。

跑法（约 20~40 秒）：
    PYTHONIOENCODING=utf-8 uv run python scripts/verify_semantic_staging_swap.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

PASS = 0
FAIL = 0
WORK: Path | None = None


def check(cond: bool, label: str, extra: object = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" -> {extra!r}" if extra != "" else ""))
    return bool(cond)


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def manifest(root: Path) -> dict[str, str]:
    """整棵目录的 {相对路径: 内容哈希}（含 .git）——用来断言「一个字节都没动」。"""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out[str(p.relative_to(root)).replace("\\", "/")] = _sha(p)
            except OSError as e:  # pragma: no cover
                out[str(p.relative_to(root))] = f"<{e}>"
    return out


def read_mdl(project: Path) -> dict | None:
    """读线上产物：`None` = 文件不存在（真正的缺失）；`<坏 JSON>` = 半写/坏文件。

    `<被拒>` 是 **Windows 特有的读者侧现象**：替换正落在这一瞬间时，open 会被拒
    （Linux 不会）。它不是「文件缺失」也不是「内容坏」，单独归类，Linux 上必须为 0。
    """
    f = project / "target" / "mdl.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except PermissionError:
        return {"mark": "<被拒>"}
    except (OSError, json.JSONDecodeError):
        return {"mark": "<坏 JSON>"}


# ── 假 wren：只实现被测链路用到的子命令 ──────────────────────
_STUB = '''# -*- coding: utf-8 -*-
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get("STUB_LOG", "")
cmd = " ".join(args[:2])

if cmd == "context build":
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"START {time.time():.4f} {os.getcwd()}\\n")
    time.sleep(float(os.environ.get("STUB_SLEEP", "0") or 0))
    if os.environ.get("STUB_FAIL") == "1":
        sys.stderr.write("Error: 假 wren 构建失败（测试注入）\\n")
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"FAIL {time.time():.4f} {os.getcwd()}\\n")
        sys.exit(1)
    tgt = Path("target")
    tgt.mkdir(parents=True, exist_ok=True)
    (tgt / "mdl.json").write_text(
        json.dumps({"mark": os.environ.get("STUB_MARK", ""), "built_in": os.getcwd()}),
        encoding="utf-8",
    )
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"END {time.time():.4f} {os.getcwd()}\\n")
    print(f"Built: 1 models, 0 views -> {tgt / 'mdl.json'}")
    sys.exit(0)

# profile add / context set-profile / memory index / context validate ... 一律成功
sys.exit(0)
'''


def make_stub(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / "wren_stub.py"
    py.write_text(_STUB, encoding="utf-8")
    if os.name == "nt":
        shim = bin_dir / "wren.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{py}" %*\r\n', encoding="ascii")
    else:
        shim = bin_dir / "wren.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    return shim


def use_stub(stub: Path) -> None:
    """把 wren 可执行路径指到假实现上（`_run_wren` 走 `settings.WREN_BIN_PATH`）。"""
    import api.wren_semantic as W
    from agent.settings.setting import settings

    settings.WREN_BIN_PATH = str(stub)
    assert W._wren_bin() == str(stub), W._wren_bin()


def stub_env(**kw: str) -> None:
    for k, v in kw.items():
        os.environ[k] = str(v)


# ── 真实 git ────────────────────────────────────────────────
def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.quotepath=false", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=check,
    )


def make_wren_project(p: Path, name: str) -> None:
    (p / "models" / "m1").mkdir(parents=True, exist_ok=True)
    (p / "knowledge" / "glossary").mkdir(parents=True, exist_ok=True)
    (p / "wren_project.yml").write_text(
        f"name: {name}\ndataSource: mysql\nprofile: p_{name}\n", encoding="utf-8"
    )
    (p / "models" / "m1" / "metadata.yml").write_text(
        "name: m1\ntableReference:\n  table: t1\n  schema: public\ncolumns: []\n",
        encoding="utf-8",
    )
    (p / "knowledge" / "glossary" / "术语表.md").write_text("术语 v1\n", encoding="utf-8")


def setup_origin(base: Path, name: str) -> tuple[Path, Path]:
    """建一个 bare origin + 一条 main 提交；返回 (bare, 用于再提交的工作副本)。"""
    bare = base / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   capture_output=True, check=True)
    seed = base / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], capture_output=True, check=True)
    make_wren_project(seed, name)
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "init")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", "main")
    return bare, seed


def clone_like_production(bare: Path, dest: Path) -> None:
    """造一个**生产形态**的浅克隆：`--depth 1 --branch main`（单分支 + refspec 被锁）。

    不走 `git_repo.clone_shallow`：它只收 http(s)/ssh（本机路径会被拒），而这里要的是
    「浅克隆这个形状」（`pull_ref` 的 refspec 修复逻辑正对着它），不是再测一遍克隆。
    """
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", "main", str(bare), str(dest)],
        capture_output=True, check=True, text=True, encoding="utf-8", errors="replace",
    )


def scan_like_scan_wren_projects(ws: Path) -> list[str]:
    """复刻 `db_config._scan_wren_projects` 的判据：一级子目录 + 有 wren_project.yml。"""
    out = []
    for sub in sorted(ws.iterdir()):
        if not sub.is_dir():
            continue
        if "备份" in sub.name or "backup" in sub.name.lower():
            continue
        if (sub / "wren_project.yml").is_file():
            out.append(sub.name)
    return out


def backups_leftover(ws: Path) -> list[str]:
    b = ws / ".backups"
    if not b.exists():
        return []
    return [str(p.relative_to(ws)) for p in b.iterdir()]


def backup_dirs(ws: Path) -> list[str]:
    """一级目录里的备份（`.backups` 是暂存目录，不算备份；`.备份-<ts>` 才是）。"""
    return [
        p.name
        for p in ws.iterdir()
        if not p.name.startswith(".")
        and ("备份" in p.name or p.name.lower().endswith("backup"))
    ]


# ══ ① 原语 ══════════════════════════════════════════════════
def section_primitives() -> None:
    print("\n① 原语：暂存目录命名 / 换入 / 换入失败回滚 / 三态清理")
    import api.wren_semantic as W

    base = WORK / "sec1"
    proj = base / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "a.txt").write_text("old", encoding="utf-8")

    s1 = W._new_staging_dir(proj, "build")
    s2 = W._new_staging_dir(proj, "build")
    check(s1.parent == proj.parent / ".backups", "暂存目录落在 <父目录>/.backups（同盘）", s1)
    check(s1.name.startswith(".build-") and s1 != s2,
          "同一秒内两次取名不撞车（补序号）", (s1.name, s2.name))

    # 扫描可见性：正对照（一级目录带 wren_project.yml 能被扫到）+ 反对照（暂存目录扫不到，
    # 哪怕它下级真有一个 wren 项目）
    (base / "real").mkdir(exist_ok=True)
    (base / "real" / "wren_project.yml").write_text("name: real\n", encoding="utf-8")
    (s2 / "project").mkdir(parents=True, exist_ok=True)
    (s2 / "project" / "wren_project.yml").write_text("name: staged\n", encoding="utf-8")
    check(scan_like_scan_wren_projects(base) == ["real"],
          "扫描只认一级目录：暂存目录里的 wren 项目不会被当成语义库列出来",
          scan_like_scan_wren_projects(base))

    # 换入（成功）
    staging = s1 / "project"
    shutil.copytree(proj, staging)
    (staging / "a.txt").write_text("new", encoding="utf-8")
    (staging / "b.txt").write_text("added", encoding="utf-8")
    kept = W._swap_in(staging, proj)
    check((proj / "a.txt").read_text(encoding="utf-8") == "new", "换入后项目内容是副本内容")
    check((proj / "b.txt").is_file(), "副本新增的文件也在")
    check(Path(kept).is_dir() and (Path(kept) / "a.txt").read_text(encoding="utf-8") == "old",
          "旧目录被挪到备份路径（内容完好）", kept)
    check("备份" in Path(kept).name, "备份目录名含「备份」⇒ `_scan_wren_projects` 会跳过", Path(kept).name)
    shutil.rmtree(kept)
    check(not staging.exists(), "换入后暂存目录不复存在（被 rename 走了）")

    # 换入失败 → 回滚（用 path-like 让第二次 rename 必失败，无 mock）
    proj2 = base / "proj2"
    proj2.mkdir(parents=True, exist_ok=True)
    (proj2 / "a.txt").write_text("old2", encoding="utf-8")
    before = manifest(proj2)

    class _Boom:
        def __fspath__(self):  # noqa: D105
            raise OSError("注入：换入失败")

    raised = ""
    try:
        W._swap_in(_Boom(), proj2)  # type: ignore[arg-type]
    except OSError as e:
        raised = str(e)
    check("回滚" in raised, "换入失败会回滚并抛 OSError（文案含「回滚」）", raised)
    check(manifest(proj2) == before, "回滚后项目内容与换入前一模一样")
    check(backup_dirs(base) == [], "回滚后不留备份目录", backup_dirs(base))

    # _stage_and_swap 三态
    async def run() -> tuple:
        proj3 = base / "proj3"
        proj3.mkdir(parents=True, exist_ok=True)
        (proj3 / "a.txt").write_text("old3", encoding="utf-8")
        m0 = manifest(proj3)

        async def fail_prepare(st: Path):
            (st / "a.txt").write_text("gone", encoding="utf-8")
            return False, {"message": "业务失败"}

        ok_f, pay_f = await W._stage_and_swap(proj3, "retest", fail_prepare)
        after_fail = manifest(proj3)

        async def boom_prepare(st: Path):
            raise RuntimeError("注入：prepare 抛异常")

        err = ""
        try:
            await W._stage_and_swap(proj3, "retest", boom_prepare)
        except RuntimeError as e:
            err = str(e)
        after_boom = manifest(proj3)

        async def ok_prepare(st: Path):
            (st / "a.txt").write_text("new3", encoding="utf-8")
            return True, {"message": "done"}

        ok_s, pay_s = await W._stage_and_swap(proj3, "retest", ok_prepare)
        return (ok_f, pay_f, after_fail, m0, err, after_boom, ok_s, ok_prepare, proj3)

    (ok_f, pay_f, after_fail, m0, err, after_boom, ok_s, _okp, proj3) = asyncio.run(run())
    check(ok_f is False and pay_f == {"message": "业务失败"},
          "prepare 失败：原样交回载荷（端点错误文案不变）", pay_f)
    check(after_fail == m0, "prepare 失败：线上目录一个字节都没动")
    check("注入" in err, "prepare 抛异常：异常照抛", err)
    check(after_boom == m0, "prepare 抛异常：线上目录仍然没动")
    check(ok_s is True, "prepare 成功：返回 ok")
    check((proj3 / "a.txt").read_text(encoding="utf-8") == "new3", "prepare 成功：副本已换入")
    # §① 上面直接调过 `_new_staging_dir`（它「取名即占位」），那两个占位目录是本节的
    # 手工产物、不属于被测链路，清掉再断言「不留残留」（_stage_and_swap 自己已清干净）
    for d in (s1, s2):
        shutil.rmtree(d, ignore_errors=True)
    check(backups_leftover(base) == [], "三种情形都不留暂存残留", backups_leftover(base))
    check(backup_dirs(base) == [], "三种情形都不留备份目录", backup_dirs(base))


# ══ ② 「更新」链路（真实 git + 假 wren）════════════════════════
def section_pull() -> None:
    print("\n② 「更新」（git pull）：副本里拉取、被拒时不动线上、stash 跨换入存活")
    import api.wren_semantic as W

    base = WORK / "sec2"
    ws = base / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    bare, seed = setup_origin(base, "libB")
    dest = ws / "libB"
    clone_like_production(bare, dest)
    (dest / "target").mkdir(exist_ok=True)
    (dest / "target" / "mdl.json").write_text(
        json.dumps({"mark": "OLD", "built_in": "live"}), encoding="utf-8"
    )
    log = base / "stub.log"
    stub_env(STUB_LOG=str(log), STUB_SLEEP="0", STUB_MARK="", STUB_FAIL="")

    # (a) 干净树上更新到新提交
    (seed / "models" / "m2").mkdir(parents=True, exist_ok=True)
    (seed / "models" / "m2" / "metadata.yml").write_text(
        "name: m2\ntableReference:\n  table: t2\n  schema: public\ncolumns: []\n",
        encoding="utf-8",
    )
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "add m2")
    git(seed, "push", "origin", "main")

    seen: list[dict | None] = []

    async def pull_and_watch():
        task = asyncio.create_task(W._pull_swapped(dest, "", False))
        while not task.done():
            seen.append(read_mdl(dest))
            await asyncio.sleep(0.05)
        return await task

    ok, payload = asyncio.run(pull_and_watch())
    check(ok is True, "更新成功", payload)
    check(payload.get("changed") is True, "changed=True（确实换到了新提交）", payload.get("message"))
    check((dest / "models" / "m2" / "metadata.yml").is_file(), "新提交的文件已生效")
    check(all(m is not None for m in seen), "更新全程线上 target/mdl.json 一直存在", seen[:3])
    seen_ok = [m for m in seen
               if m is not None and (m or {}).get("mark") not in ("<被拒>", "<坏 JSON>")]
    check(seen_ok and all((m or {}).get("mark") == "OLD" for m in seen_ok),
          f"更新全程线上读到的一直是旧内容（{len(seen_ok)} 次有效采样，没有半写/空洞）", seen[:3])
    check((read_mdl(dest) or {}).get("mark") == "OLD",
          "仓库自带的构建产物语义不变：target/ 未被跟踪 ⇒ 原样留着（等「构建」刷新）")
    head = git(dest, "log", "--oneline", "-1").stdout.strip()
    check("add m2" in head, "换入后 .git 完好且指向新提交", head)
    check(backups_leftover(ws) == [], "更新后不留暂存残留", backups_leftover(ws))
    check(backup_dirs(ws) == [], "更新后不留备份目录", backup_dirs(ws))
    check(scan_like_scan_wren_projects(ws) == ["libB"], "更新期间/之后语义库列表没有多出条目")

    # (b) 脏树 + 未确认放弃 → 拒绝，且线上一个字节都没动
    (dest / "knowledge" / "glossary" / "术语表.md").write_text("本地手改\n", encoding="utf-8")
    before = manifest(dest)
    ok_b, pay_b = asyncio.run(W._pull_swapped(dest, "", False))
    check(ok_b is False, "有未提交改动且未确认放弃 → 拒绝", pay_b.get("message"))
    check("未换入" in str(pay_b.get("message")), "拒绝文案说明「未换入」", pay_b.get("message"))
    check(manifest(dest) == before, "被拒时线上目录一个字节都没动（含 .git）")
    check(backups_leftover(ws) == [], "被拒时不留暂存残留", backups_leftover(ws))

    # (c) 确认放弃 → 先 stash 再更新，stash 必须跨换入存活（靠 .git 一起被拷）
    ok_c, pay_c = asyncio.run(W._pull_swapped(dest, "", True))
    check(ok_c is True, "确认放弃后更新成功", pay_c.get("message"))
    check(bool(pay_c.get("stash_ref")), "返回 stash_ref", pay_c.get("stash_ref"))
    check((dest / "knowledge" / "glossary" / "术语表.md").read_text(encoding="utf-8") == "术语 v1\n",
          "本地改动被放弃（文件回到仓库版本）")
    stash_list = git(dest, "stash", "list").stdout
    check("nl2sql 更新语义库前自动备份" in stash_list,
          "stash 还在（备份没随换入丢掉）——证明 .git 是被整份拷过去再换入的", stash_list.strip())

    # (d) 线上缺构建产物 → 换入前就地补构建
    (dest / "target" / "mdl.json").unlink()
    (seed / "knowledge" / "glossary" / "术语表.md").write_text("术语 v2\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "bump glossary")
    git(seed, "push", "origin", "main")
    stub_env(STUB_MARK="AUTO")
    ok_d, pay_d = asyncio.run(W._pull_swapped(dest, "", True))
    mdl_d = read_mdl(dest) or {}
    check(ok_d is True, "缺构建产物时更新成功", pay_d.get("message"))
    check("已补构建" in str(pay_d.get("message")), "文案说明「已补构建」", pay_d.get("message"))
    check(mdl_d.get("mark") == "AUTO", "补构建的产物落到了线上目录", mdl_d)
    check(any(t in str(mdl_d.get("built_in")) for t in (".pull-", ".build-"))
          and ".backups" in str(mdl_d.get("built_in")),
          "产物是在暂存目录里构建出来的（built_in 指向 .backups/.*-*）", mdl_d.get("built_in"))
    check((dest / "knowledge" / "glossary" / "术语表.md").read_text(encoding="utf-8") == "术语 v2\n",
          "同时内容也更新到位")

    # (e) 补构建失败 → 更新照样生效，但把风险写进文案（不假装成功）
    (dest / "target" / "mdl.json").unlink()
    (seed / "models" / "m3").mkdir(parents=True, exist_ok=True)
    (seed / "models" / "m3" / "metadata.yml").write_text(
        "name: m3\ntableReference:\n  table: t3\n  schema: public\ncolumns: []\n",
        encoding="utf-8",
    )
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "add m3")
    git(seed, "push", "origin", "main")
    stub_env(STUB_FAIL="1")
    ok_e, pay_e = asyncio.run(W._pull_swapped(dest, "", True))
    check(ok_e is True, "补构建失败不阻断更新本身（内容才是用户要的）", pay_e.get("message"))
    check("补构建失败" in str(pay_e.get("message")) and "构建" in str(pay_e.get("message")),
          "文案明确提示补构建失败、要去点「构建」", pay_e.get("message"))
    check((dest / "models" / "m3" / "metadata.yml").is_file(), "内容仍然更新到位")
    check(read_mdl(dest) is None, "线上确实没有构建产物（如实反映，不假报成功）")
    stub_env(STUB_FAIL="")
    check(backups_leftover(ws) == [], "失败路径也不留暂存残留", backups_leftover(ws))


# ══ ③ 「重建」链路 ═══════════════════════════════════════════
def section_rebuild() -> None:
    print("\n③ 「重建」：构建期间线上读到的仍是旧产物；构建失败连旧产物都不动；并发串行")
    import api.wren_semantic as W

    base = WORK / "sec3"
    ws = base / "ws"
    proj = ws / "libC"
    proj.mkdir(parents=True, exist_ok=True)
    make_wren_project(proj, "libC")
    (proj / "target").mkdir(exist_ok=True)
    (proj / "target" / "mdl.json").write_text(
        json.dumps({"mark": "OLD", "built_in": "live"}), encoding="utf-8"
    )
    log = base / "stub.log"
    stub_env(STUB_LOG=str(log), STUB_MARK="NEW", STUB_SLEEP="1.2", STUB_FAIL="")

    before = manifest(proj)
    seen: list[dict | None] = []
    stage_seen: list[bool] = []

    # 采样必须**紧**（只 `sleep(0)` 让出控制权，不加间隔）：构建走的是「单文件 os.replace」
    # ⇒ 一个采样点都不该落空。50ms 采样会放过整目录交换里那 ~5ms 的目录空缺——
    # 2026-09-24 实测（Windows 紧循环 874 次轮询）会命中 8 次读不到，所以这里必须紧。
    async def rebuild_watch():
        task = asyncio.create_task(W._rebuild_swapped(proj))
        while not task.done():
            seen.append(read_mdl(proj))
            stage_seen.append(bool(backups_leftover(ws)))
            await asyncio.sleep(0)
        return await task

    ok, msg = asyncio.run(rebuild_watch())
    check(ok is True, "重建成功", msg)
    check(len(seen) >= 500, f"紧循环采样次数足够多（{len(seen)} 次）")
    miss = [i for i, m in enumerate(seen) if m is None]
    check(not miss,
          f"构建全程线上 target/mdl.json **一次都没缺失**（{len(seen)} 次紧采样，{len(miss)} 次落空）",
          miss[:5])
    denied = [m for m in seen if (m or {}).get("mark") == "<被拒>"]
    torn = [m for m in seen if (m or {}).get("mark") == "<坏 JSON>"]
    check(not torn, "从没读到半写/坏 JSON", torn[:3])
    check(not denied or os.name == "nt",
          f"读者被拒只允许出现在 Windows（{len(denied)} 次；Linux 必须 0 —— 生产是 Linux）")
    good = [m for m in seen
            if m is not None and (m or {}).get("mark") not in ("<被拒>", "<坏 JSON>")]
    marks = [(m or {}).get("mark") for m in good]
    # 采样会一直采到 `_rebuild_swapped` 返回（替换完成后还要清暂存目录、放锁），所以
    # 尾部必然有若干次读到新产物 —— 要断言的是**单调**：旧→新只翻一次，之后再没回退。
    first_new = marks.index("NEW") if "NEW" in marks else len(marks)
    check(set(marks) <= {"OLD", "NEW"},
          f"读到的只有旧/新两种内容，没有第三种（{len(marks)} 次有效采样）", sorted(set(marks)))
    check(first_new > 0 and all(m == "NEW" for m in marks[first_new:]),
          f"旧→新只翻一次且不回退（前 {first_new} 次全旧，其后全为新）", marks[-4:])
    check(any(stage_seen), "构建期间确实存在暂存目录（证明走的是副本，不是原地）")
    after = read_mdl(proj) or {}
    after_manifest = manifest(proj)
    check(after.get("mark") == "NEW", "构建完成后线上是新产物", after)
    check(".build-" in str(after.get("built_in")) and ".backups" in str(after.get("built_in")),
          "新产物是在暂存目录里构建出来的（built_in 指向 .backups/.build-*）",
          after.get("built_in"))
    # 单文件原子替换的可观测特征：除产物外**逐字节**没动（整目录交换做不到——它会重建
    # 整棵树的文件；这条同时证明构建没顺手改源文件）
    diff = {k for k in set(before) | set(after_manifest)
            if before.get(k) != after_manifest.get(k)}
    check(diff == {"target/mdl.json"},
          "重建只改了 target/mdl.json，其余文件逐字节未动（单文件原子替换）", sorted(diff))
    check(backups_leftover(ws) == [], "重建后不留暂存残留", backups_leftover(ws))
    check(backup_dirs(ws) == [], "重建不产生备份目录（不换目录）", backup_dirs(ws))
    check(scan_like_scan_wren_projects(ws) == ["libC"], "重建期间语义库列表没多出条目")

    # 构建失败 → 不换入，旧产物原样继续服务
    before_f = manifest(proj)
    stub_env(STUB_MARK="BROKEN", STUB_FAIL="1")
    ok_f, msg_f = asyncio.run(W._rebuild_swapped(proj))
    check(ok_f is False, "构建失败 → ok=False", msg_f)
    check("未换入" in msg_f, "失败文案说明「未换入」", msg_f)
    check(manifest(proj) == before_f, "构建失败：线上目录一个字节都没动（库照常服务）")
    check((read_mdl(proj) or {}).get("mark") == "NEW", "旧产物还在，没有被清掉")
    check(backups_leftover(ws) == [], "失败路径不留暂存残留", backups_leftover(ws))
    check(backup_dirs(ws) == [], "失败路径不产生备份目录", backup_dirs(ws))
    stub_env(STUB_FAIL="", STUB_MARK="CC", STUB_SLEEP="0.7")

    # 并发两次重建 → 假 wren 的 START/END 区间不得重叠（项目写锁生效）
    log.write_text("", encoding="utf-8")

    async def two():
        return await asyncio.gather(W._rebuild_swapped(proj), W._rebuild_swapped(proj))

    t0 = time.perf_counter()
    results = asyncio.run(two())
    wall = time.perf_counter() - t0
    check(all(r[0] for r in results), "并发两次重建都成功", results)
    intervals: list[tuple[float, float]] = []
    cur: float | None = None
    for line in log.read_text(encoding="utf-8").splitlines():
        kind, ts, *_ = line.split()
        if kind == "START":
            cur = float(ts)
        elif kind == "END" and cur is not None:
            intervals.append((cur, float(ts)))
            cur = None
    check(len(intervals) == 2, f"两次构建都跑到了（{len(intervals)} 段）", intervals)
    overlap = bool(
        len(intervals) == 2 and min(intervals[0][1], intervals[1][1]) > max(intervals[0][0], intervals[1][0])
    )
    check(not overlap, "两次构建的区间不重叠 ⇒ 项目写锁把并发串行化了", intervals)
    check(wall >= 1.2, f"墙钟 ≥ 两次串行（{wall:.2f}s，单次 0.7s）")
    check(backups_leftover(ws) == [], "并发后不留暂存残留", backups_leftover(ws))
    check(scan_like_scan_wren_projects(ws) == ["libC"], "并发后语义库列表仍然只有它自己")


# ══ ④ 结构断言（端点必须走带锁的 helper）═══════════════════════
def section_structure() -> None:
    print("\n④ 结构：端点走带锁 helper；接管路径的构建也在换入之前")
    import ast

    tree = ast.parse((SRC / "api" / "wren_semantic.py").read_text(encoding="utf-8"))
    fns: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fns.setdefault(node.name, node)

    def calls(fn_name: str) -> set[str]:
        node = fns.get(fn_name)
        if node is None:
            return set()
        return {
            (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
        }

    def names(fn_name: str) -> set[str]:
        """函数体里出现过的**名字**（含当实参传出去的，如 `offload_long(_swap_in, …

        `_swap_in` / `_build_with_profile` 这类都是被当实参交给 `offload_long` 的，在 AST
        里是 `ast.Name` 而不是 `Call.func` —— 只按调用找会漏（这个坑踩过两次）。
        """
        node = fns.get(fn_name)
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} if node else set()

    def attr_calls(fn_name: str) -> set[str]:
        """`模块.方法` 形态的调用（`attr_calls("_replace_target_mdl")` 里应有 `os.replace`）。"""
        node = fns.get(fn_name)
        if node is None:
            return set()
        return {
            f"{getattr(n.func.value, 'id', '?')}.{n.func.attr}"
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

    for name in ("git_pull", "build_project"):
        target = "_pull_swapped" if name == "git_pull" else "_rebuild_swapped"
        check(target in calls(name), f"{name} 调用了带锁的 {target}")
        check("_stage_and_swap" not in calls(name), f"{name} 不再自己调 _stage_and_swap（避免绕过锁）")
    for name in ("_pull_swapped", "_rebuild_swapped"):
        node = fns.get(name)
        has_lock = any(
            isinstance(n, ast.AsyncWith)
            and any("_project_lock" in ast.dump(item.context_expr) for item in n.items)
            for n in ast.walk(node)
        ) if node is not None else False
        check(has_lock, f"{name} 里确实取了 _project_lock")

    # 安装方式必须与「换的是什么」匹配：构建 → 单文件原子替换（零窗口）；拉取 → 整目录交换
    check("_stage_build_replace" in calls("_rebuild_swapped")
          and "_stage_and_swap" not in calls("_rebuild_swapped"),
          "_rebuild_swapped 走 _stage_build_replace（单文件原子替换，不换目录）",
          sorted(calls("_rebuild_swapped")))
    check("_stage_and_swap" in calls("_pull_swapped"),
          "_pull_swapped 走 _stage_and_swap（整目录交换）", sorted(calls("_pull_swapped")))
    check("os.replace" in attr_calls("_replace_target_mdl")
          and "os.rename" not in attr_calls("_replace_target_mdl"),
          "产物替换用的是原子 os.replace（不是 rename）", sorted(attr_calls("_replace_target_mdl")))
    check("_swap_in" not in names("_stage_build_replace")
          and "_swap_in" in names("_stage_and_swap"),
          "只有 _stage_and_swap 换目录（两次 rename），构建路径不碰目录",
          sorted(names("_stage_and_swap")))
    check("shutil.ignore_patterns" in attr_calls("_stage_build_replace"),
          "构建的暂存拷贝排除了 .git/target（少拷几十~几百 MB）",
          sorted(attr_calls("_stage_build_replace")))

    node = fns.get("_adopt_git_into")
    has_lock = node is not None and any(
        isinstance(n, ast.AsyncWith)
        and any("_project_lock" in ast.dump(item.context_expr) for item in n.items)
        for n in ast.walk(node)
    )
    check(has_lock and "_adopt_git_into_locked" in calls("_adopt_git_into"),
          "_adopt_git_into = 取锁 + 委托 _adopt_git_into_locked")

    # 接管路径：构建必须在「把本地目录 rename 走」之前（否则换入的目录一时没有 mdl.json）。
    # 注意 `_build_with_profile` 是**当实参**交给 `offload_long` 的（`offload_long(_build_with_profile, …)`），
    # 所以它在 AST 里是 `ast.Name` 而不是 `Call.func` —— 按调用找会什么都找不到（踩过）。
    body = fns.get("_adopt_git_into_locked")
    build_line = min(
        [n.lineno for n in ast.walk(body)
         if isinstance(n, ast.Name) and n.id == "_build_with_profile"]
        or [10**9]
    ) if body is not None else 0
    rename_line = min(
        [n.lineno for n in ast.walk(body)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute) and n.func.attr == "rename"
         and getattr(n.func.value, "id", "") == "os"]
        or [10**9]
    ) if body is not None else 0
    check(build_line < rename_line,
          "接管：构建（_build_with_profile）在第一次 os.rename 之前", (build_line, rename_line))


def main() -> int:
    global WORK
    WORK = Path(tempfile.mkdtemp(prefix="verify_staging_"))
    print(f"P2-10② 语义库变更「暂存副本 + 原子换入」回归（工作目录 {WORK}）")

    stub = make_stub(WORK / "bin")
    use_stub(stub)

    section_primitives()
    section_pull()
    section_rebuild()
    section_structure()

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    print(f"（工作目录保留供排查：{WORK}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
