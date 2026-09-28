# -*- coding: utf-8 -*-
"""单工作区（路径钉死）验证 —— 工作区概念 T1/T2/T3 的验收脚本（2026-09-25）。

**要钉死的性质**（每条都有对应负对照，防"看起来对了"）：
  ① 工作区 = `<AGENT_DATA_ROOT>/workspace`，**不受**盘上 `workspaces.json` 与
     `WORKSPACE_PATH` env 影响 —— 负对照里的候选目录**真实存在**（旧代码本来会选它），
     所以"没被带走"才是有效断言；
  ② `active_name` 恒为 `"default"`（run metadata 的标签读者仍要拿得到）；
  ③ 多工作区机件真的没了：`api.workspace` 模块不存在、`custom_app` 不再挂载它的路由、
     `WorkspaceManager` 上没有 CRUD/切换/守卫方法、`setting` 里两个作废开关也没了；
  ④ 首启仍会建出工作区骨架（**全新部署就靠它** —— 仓库内 `src/agent/workspace` 种子
     已被发版 tar 排除且通常不存在 ⇒ `_seed_data_root_once` 那条路是空转）；
  ⑤ 保留策略的目录来源 `retention._ws_subdirs` 仍能拿到工作区子目录（拿不到 =
     `tmp/`、`report/` 再也不清理的**静默退化**），且危险根护栏仍有效；
  ⑥ 删掉"切换时清缓存"的 `cache_reset.py` 后，**db_config / 语义库写路径**仍能一次
     清掉三个「发现类」缓存（`invalidate_db_discovery_caches`）。漏这条的后果不是显示
     问题：新建模的库被判"未建模" → 静默掉到 dbmcp 直连，口径/物理 SQL 全变且不报错。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_workspace_pinned.py

退出码 0 = 全部通过。

⚠️ `AGENT_DATA_ROOT` 由本脚本钉到临时目录，且**必须在 import 任何 agent 模块之前**
（`workspace_manager._DATA_ROOT` / `_DEFAULT_WORKSPACE_DIR` 都是**导入期**读 env 的）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_WORK = Path(tempfile.mkdtemp(prefix="nl2sql-verify-pinned-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)

# 作废的逃生开关：故意设成"指向一个真实存在的目录"，让负对照真的能翻车。
# `WORKSPACE_PATH` 曾优先于默认目录，`workspaces.json` 的 active 曾优先于它俩。
_FAKE_WS = _WORK / "somewhere-else"
_FAKE_WS.mkdir(parents=True, exist_ok=True)
os.environ["WORKSPACE_PATH"] = str(_FAKE_WS)
(_WORK / "workspaces.json").write_text(
    json.dumps(
        {
            "version": 1,
            "active": "bogus",
            "workspaces": {
                "default": {"path": str(_WORK / "workspace"), "name": "默认工作区"},
                "bogus": {"path": str(_FAKE_WS), "name": "作废的注册表条目"},
            },
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
sys.path.insert(0, str(_SRC))

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    results.append((bool(cond), label))
    tag = "PASS" if cond else "FAIL"
    color = "\033[32m" if cond else "\033[31m"
    line = f"  [{color}{tag}\033[0m] {label}"
    if detail:
        line += f"  {detail}"
    print(line, flush=True)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def _pinned() -> Path:
    return (Path(os.environ["AGENT_DATA_ROOT"]) / "workspace").resolve()


# ── ① 路径钉死 ────────────────────────────────────────────────
def verify_pinned_path() -> None:
    section("① 工作区路径钉死（作废的注册表 / env 都不得生效）")
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    check(wm.active_workspace == _pinned(), "active_workspace == <AGENT_DATA_ROOT>/workspace",
          str(wm.active_workspace))
    check(wm.active_workspace != _FAKE_WS.resolve(),
          "负对照：没被 WORKSPACE_PATH env 带走", f"(该目录真实存在: {_FAKE_WS})")
    reg = json.loads((_WORK / "workspaces.json").read_text(encoding="utf-8"))
    reg_path = Path(reg["workspaces"][reg["active"]]["path"]).resolve()
    check(wm.active_workspace != reg_path,
          "负对照：没被 workspaces.json 的 active 带走", f"(该目录真实存在: {reg_path})")
    check(wm.active_name == "default", "active_name 恒为 default", wm.active_name)

    # 注册表文件既没被读、也没被改写（内容与我们写进去的一字不差）
    after = json.loads((_WORK / "workspaces.json").read_text(encoding="utf-8"))
    check(after == reg, "盘上的 workspaces.json 没被读写（内容原样）")

    # 工作区目录自身由 manager 解析得出，且是 data_root 的子目录
    check(str(wm.active_workspace).startswith(str(_WORK.resolve())),
          "工作区落在本脚本的临时根下（未触碰真实数据）", str(_WORK))
    # 两边都 resolve：`data_root` 从不 resolve（历史行为，未改），而 Windows 的
    # `mkdtemp` 可能给出 8.3 短名（`CONGPE~1`）—— 不 resolve 会假红。
    check(wm.data_root.resolve() == _WORK.resolve(), "data_root == AGENT_DATA_ROOT",
          str(wm.data_root))


# ── ② 首启骨架 ────────────────────────────────────────────────
def verify_fresh_init() -> None:
    section("② 首启仍建出工作区骨架（全新部署的唯一初始化点）")
    from agent.workspace_manager import manager as M

    fresh = _WORK / "fresh" / "workspace"
    orig = M._DEFAULT_WORKSPACE_DIR
    M._DEFAULT_WORKSPACE_DIR = fresh
    try:
        M.WorkspaceManager()  # 直接构造，绕开单例
    finally:
        M._DEFAULT_WORKSPACE_DIR = orig

    for d in ("report", "tmp", "nl2sql_process_data", "large_tool_results", "checkpoint", "feedback"):
        check((fresh / d).is_dir(), f"首启建出 {d}/")
    db_config = fresh / "db_config.json"
    check(db_config.is_file(), "首启建出 db_config.json")
    if db_config.is_file():
        check(json.loads(db_config.read_text(encoding="utf-8")).get("databases") == [],
              "db_config.json 是空配置（不是 None/坏 JSON）")

    # 幂等：已有内容不被覆盖（重启不能把配好的库清掉）
    sentinel = {"version": 1, "databases": [{"name": "keepme"}]}
    db_config.write_text(json.dumps(sentinel, ensure_ascii=False), encoding="utf-8")
    keep = fresh / "report" / "do-not-touch.md"
    keep.write_text("x", encoding="utf-8")
    M._DEFAULT_WORKSPACE_DIR = fresh
    try:
        M.WorkspaceManager()
    finally:
        M._DEFAULT_WORKSPACE_DIR = orig
    check(json.loads(db_config.read_text(encoding="utf-8")) == sentinel,
          "再次启动不覆盖已有 db_config.json")
    check(keep.is_file(), "再次启动不删已有工作区文件")


# ── ③ 机件已删 ────────────────────────────────────────────────
def verify_machinery_gone() -> None:
    section("③ 多工作区机件已删除")
    try:
        import api.workspace  # noqa: F401

        check(False, "api.workspace 模块已删除", "它还能被 import")
    except ModuleNotFoundError:
        check(True, "api.workspace 模块已删除（import 失败）")
    except Exception as e:  # noqa: BLE001
        check(False, "api.workspace 模块已删除", f"意外的导入错误: {e!r}")

    custom_app = (_SRC / "api" / "custom_app.py").read_text(encoding="utf-8")
    check("api.workspace" not in custom_app, "custom_app 不再 import/挂载 api.workspace")

    from agent.workspace_manager import WorkspaceManager
    from agent.workspace_manager import manager as M

    for meth in (
        "list_workspaces", "register_workspace", "activate_workspace",
        "unregister_workspace", "delete_workspace", "reset_dependent_caches",
        "invalidate", "_read_registry", "_write_registry", "_migrate_registry_once",
        "_resolve_active_path", "_guard_active_runs",
    ):
        check(not hasattr(WorkspaceManager, meth), f"WorkspaceManager.{meth} 已删除")
    for name in ("WorkspaceBusyError", "_active_run_count", "_DEFAULT_REGISTRY_PATH",
                 "_LEGACY_REGISTRY_PATH", "_LOCK"):
        check(not hasattr(M, name), f"manager 模块级 {name} 已删除")

    check(not (_REPO / "src" / "api" / "workspace.py").exists(), "src/api/workspace.py 文件已删除")
    check(not (_REPO / "src" / "agent" / "workspace_manager" / "cache_reset.py").exists(),
          "src/agent/workspace_manager/cache_reset.py 文件已删除")
    check(not (_REPO / "src" / "agent" / "workspace_manager" / "workspaces.json").exists(),
          "仓库内旧注册表 workspaces.json 已删除")

    hits = [
        str(p.relative_to(_REPO))
        for p in _SRC.rglob("*.py")
        if "/api/workspaces" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    check(hits == [], "src/ 下不再有 /api/workspaces 路由声明", str(hits))

    from agent.settings.setting import settings

    check(not hasattr(settings, "WORKSPACE_PATH") and not hasattr(settings, "WORKSPACE_REGISTRY_PATH"),
          "setting 里两个作废开关（WORKSPACE_PATH / WORKSPACE_REGISTRY_PATH）已删除")


# ── ④ 保留策略 ────────────────────────────────────────────────
def verify_retention_source() -> None:
    section("④ 保留策略仍能拿到工作区目录 + 危险根护栏")
    from agent.utils import retention as R

    subs = R._ws_subdirs("report")
    check(subs == [_pinned() / "report"],
          "_ws_subdirs('report') 恰好一项，且指向钉死的工作区", str(subs))
    check(R._ws_subdirs("no-such-subdir") == [], "不存在的子目录 → 空列表（调用方记 skipped）")

    check(R._unsafe_root(Path("/")) is True, "危险根护栏：文件系统根仍被拒")
    check(R._unsafe_root(Path.home()) is True, "危险根护栏：家目录仍被拒")
    check(R._unsafe_root(_WORK) is False, "正常目录不算危险根")

    import agent.workspace_manager as pkg

    class _BadManager:  # 模拟 AGENT_DATA_ROOT 被配成家目录
        active_workspace = Path.home()

    orig = pkg.get_workspace_manager
    pkg.get_workspace_manager = lambda: _BadManager()  # type: ignore[assignment]
    try:
        check(R._ws_subdirs("tmp") == [], "负对照：工作区指到家目录 → 一个候选都不返回")
    finally:
        pkg.get_workspace_manager = orig  # type: ignore[assignment]


# ── ⑤ 缓存失效重挂 ────────────────────────────────────────────
def verify_cache_rewire() -> None:
    section("⑤ 写路径仍能一次清掉三个「发现类」缓存")
    from agent.utils import semantic_db as S

    # 前置：让三个缓存都真的有值（否则"清掉"是恒真的）
    S._load_db_name_norm()
    S._semantic_override_cache["primed"] = "somewhere"
    det = S.get_detector()
    det._cache["primed"] = "/tmp/proj"
    det._cache_valid = True
    check(S._db_name_norm_cache is not None, "前置：db_name 归一化缓存已装载")
    check(S._semantic_override_cache != {}, "前置：语义库版本物化缓存非空")
    check(det._cache_valid is True and det._cache != {}, "前置：detector 缓存有效")

    S.invalidate_db_discovery_caches()
    check(S._db_name_norm_cache is None, "① db_name 归一化缓存被清")
    check(S._semantic_override_cache == {}, "② 语义库版本物化缓存被清")
    check(det._cache_valid is False and det._cache == {}, "③ detector 缓存被清")

    # 接线判据（静态）：光有函数、没人调 = 缓存永不失效，所以调用点必须钉住
    call_sites = {
        "src/api/db_config.py": 3,       # upsert / delete / reload-mcp
        "src/api/wren_semantic.py": 1,   # 语义库增删改 / 重新关联
    }
    for rel, least in call_sites.items():
        src = (_REPO / rel).read_text(encoding="utf-8")
        n = src.count("invalidate_db_discovery_caches")
        check(n >= least, f"{rel} 有 {n} 处调用（期望 ≥{least}）")

    # 反面判据：不该再有"只清 detector"的残留（那正是漏清归一化的经典错法）
    stale = [
        rel for rel in call_sites
        if "get_detector().invalidate()" in (_REPO / rel).read_text(encoding="utf-8")
    ]
    check(stale == [], "上述写路径已无『只清 detector』的旧写法", str(stale))


def main() -> int:
    print(f"单工作区（路径钉死）验证（临时根 {_WORK}）")
    print(f"  钉死目标：{_pinned()}")
    verify_pinned_path()
    verify_fresh_init()
    verify_machinery_gone()
    verify_retention_source()
    verify_cache_rewire()

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 60}")
    for ok, label in results:
        if not ok:
            print(f"  [FAIL] {label}")
    print(f"{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
