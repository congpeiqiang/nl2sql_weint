# -*- coding: utf-8 -*-
"""P3-7 静态回归：wren/CPU 重活不得在事件循环线程上直接调用。

**被测的坑**：`LANGGRAPH_ALLOW_BLOCKING=true` 关掉了 langgraph 的阻塞护栏，于是一个
`async def` 里直接写同步重活（wren 建引擎/dry_plan、结果解析、回写 thread state）会
**静默**把 10 个后台 job 共用的那一个事件循环串起来 —— 别人全在等，日志里什么都没有。
P1-14 已经把 116 个搬运点搬进 `agent.utils.offload` 的两个池，本脚本负责**证明搬了**
且**防止再搬回来**。

判据：对下面这份「重活名单」里的每个名字，扫描 `src/**/*.py` 的 AST：
出现在 `async def` 体内、且**没有**被 `offload / offload_long / asyncio.to_thread /
loop.run_in_executor` 的实参包住的调用 = 违规。

为什么按 AST 而不是 grep：`plan_run_sql` 这些名字在**同步**函数里直接调是完全正确的
（同步函数本来就会被 `offload` 整体搬走，见 `check_progress._build_check_result`）；
只有「自己就在协程里、且没人搬」才是坑。grep 分不清这两者。

名单是**推导出来的**：`agent.utils.wren_plan` 里被 `import wren` 的重活路径所触达的
对外函数 + 已知的 CPU 密集入口。加名字是刻意行为（新写一个重活入口就该加进来）。

跑法：
    PYTHONIOENCODING=utf-8 uv run python scripts/verify_wren_offload.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 「重活」名字（最后一段标识符）：命中即在协程里必须被搬运
HEAVY = {
    # wren 语义层复算（建引擎可到秒级 + dry_plan 编译）
    "plan_run_sql",
    "plan_cube_sql",
    "plan_cube_sql_checked",
    # Cube 通道快照（内部就调 plan_cube_sql）
    "cube_snapshot",
    # 检查子任务结果（内部串 plan_run_sql/结果解析/state 回写）
    "_build_check_result",
    "_engine_for",
    # 报告装配（读全量结果 + 生成文件）
    "build_report",
}

# 搬运容器：这些调用会把实参丢到别的线程去执行
CARRIERS = {"offload", "offload_long", "to_thread", "run_in_executor", "to_thread_ctx"}

PASS = 0
FAIL = 0


def check(cond: bool, label: str, extra: object = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" -> {extra!r}" if extra != "" else ""))
    return bool(cond)


def _name_of(node: ast.AST) -> str:
    """取被调名的最后一段：`W.plan_run_sql` / `plan_run_sql` 都归一成 `plan_run_sql`。"""
    f = node.func if isinstance(node, ast.Call) else None
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _carried_call_ids(tree: ast.AST) -> set[int]:
    """所有「作为搬运容器实参出现」的调用节点 id。"""
    carried: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name_of(node) in CARRIERS:
            for arg in list(node.args) + [k.value for k in node.keywords]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Call):
                        carried.add(id(sub))
                    carried.add(id(sub))
    return carried


class _Scan(ast.NodeVisitor):
    """在 async def 体内找未被搬运的重活调用（同步 def 体里跳过：由调用方整体搬走）。"""

    def __init__(self, carried: set[int], path: str) -> None:
        self.carried = carried
        self.path = path
        self.hits: list[tuple[int, str]] = []
        self._async_depth = 0

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._async_depth += 1
        self.generic_visit(node)
        self._async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # 顶层/类内同步函数：整体由调用方 offload ⇒ 不在此判定。
        # ⚠️ 但**嵌在协程里的**同步函数不能重置深度：那种 `def g(): ...` 是就地调用、
        # 仍在事件循环线程上跑（`async def f(): def g(): plan_run_sql(); return g()`），
        # 重置就成漏判。这里只在「本来就不在协程里」时才清零。
        depth_here = self._async_depth
        if depth_here == 0:
            self._async_depth = 0
        self.generic_visit(node)
        self._async_depth = depth_here

    def visit_Call(self, node: ast.Call) -> None:
        name = _name_of(node)
        if (
            name in HEAVY
            and self._async_depth > 0
            and id(node) not in self.carried
        ):
            self.hits.append((node.lineno, name))
        self.generic_visit(node)


def scan_tree(root: Path) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for py in sorted(root.rglob("*.py")):
        rel = str(py.relative_to(ROOT)).replace("\\", "/")
        if "workspace-temp/" in rel or rel.startswith("src/test/"):
            # 死代码/测试样本，不参与判定（但也别把它们当反例）。
            # workspace-temp 已随单工作区改造删除（2026-09-25），这一条只兜
            # 开发机上残留的旧副本。
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as e:  # pragma: no cover
            out.append((rel, 0, f"<parse failed: {e}>"))
            continue
        s = _Scan(_carried_call_ids(tree), rel)
        s.visit(tree)
        out.extend((rel, ln, nm) for ln, nm in s.hits)
    return out


def section_real_tree() -> list[tuple[str, int, str]]:
    print("\n① 真实代码树：协程里没有未搬运的重活调用")
    hits = scan_tree(SRC)
    check(not hits, f"src/ 下 0 处违规（扫描 {len(list(SRC.rglob('*.py')))} 个 .py）", hits[:6])
    # 反证：名单里的名字在树里**确实存在**（否则本规则会退化成恒真）
    text = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in SRC.rglob("*.py")
    )
    present = sorted(n for n in HEAVY if n in text)
    missing = sorted(HEAVY - set(present))
    check(len(present) >= 4, f"名单中至少 4 个名字在树里真实出现（{len(present)} 个）", present)
    if missing:
        print(f"  · 说明：这些名字在树里没出现（可能是可选通道）：{missing}")
    return hits


def section_synthetic() -> None:
    print("\n② 合成负对照：违规写法必须被抓到，正确写法不得被误报")
    import tempfile

    cases: list[tuple[str, str, bool]] = [
        # (标签, 源码, 是否应判违规)
        ("裸调（违规）", "async def f():\n    return plan_run_sql(p, c, s)\n", True),
        ("属性调用（违规）", "async def f():\n    return W.plan_run_sql(p, c, s)\n", True),
        ("嵌套在协程里（违规）", "async def f():\n    if x:\n        r = _build_check_result(a, b)\n    return r\n", True),
        ("offload 包住（正确）", "async def f():\n    return await offload(plan_run_sql, p, c, s)\n", False),
        ("offload_long 包住（正确）", "async def f():\n    return await offload_long(cube_snapshot, msgs)\n", False),
        ("to_thread 包住（正确）", "async def f():\n    return await asyncio.to_thread(plan_cube_sql, p, c, a)\n", False),
        ("同步函数里裸调（正确）", "def f():\n    return plan_run_sql(p, c, s)\n", False),
        ("同步函数被 offload（正确）", "async def g():\n    return await offload(f, 1)\ndef f(x):\n    return _build_check_result(x)\n", False),
        # 漏判防线：嵌在协程里的同步函数仍是「就地调用、在 loop 上跑」
        ("协程内嵌同步函数裸调（违规）", "async def f():\n    def g():\n        return plan_run_sql(p, c, s)\n    return g()\n", True),
        ("同步函数内嵌协程裸调（违规）", "def f():\n    async def g():\n        return cube_snapshot(m)\n    return g\n", True),
    ]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for label, src, should_flag in cases:
            p = td_path / "case.py"
            p.write_text(src, encoding="utf-8")
            tree = ast.parse(src)
            s = _Scan(_carried_call_ids(tree), "case.py")
            s.visit(tree)
            got = bool(s.hits)
            check(got == should_flag, f"{label}：{'命中' if should_flag else '不命中'}", s.hits)


def section_cache() -> None:
    """P3-7 第二半：引擎缓存上限「按库数扩容」（env 可调、坏值退回默认）。"""
    print("\n③ 引擎缓存上限：默认/覆盖/坏值/淘汰/关闭")
    import os

    sys.path.insert(0, str(SRC))
    from agent.utils import wren_plan as W

    check(W.DEFAULT_CACHE_MAX >= 8, f"默认上限已调大（{W.DEFAULT_CACHE_MAX} ≥ 8）")
    saved = {k: os.environ.get(k) for k in ("WREN_PLAN_CACHE_MAX", "WREN_PLAN_CACHE")}
    try:
        os.environ.pop("WREN_PLAN_CACHE_MAX", None)
        check(W.cache_max() == W.DEFAULT_CACHE_MAX, "未设 env → 默认")
        # 坏值一律退回默认（不静默关掉上限）
        for raw in ("0", "-3", "abc", "", "   "):
            os.environ["WREN_PLAN_CACHE_MAX"] = raw
            check(W.cache_max() == W.DEFAULT_CACHE_MAX, f"坏值 {raw!r} → 退回默认")
        os.environ["WREN_PLAN_CACHE_MAX"] = " 12 "
        check(W.cache_max() == 12, "合法值（含空白）→ 生效")
        # 淘汰循环与新上限同形（不建真引擎：这里验的是算术，真建引擎在 §④）
        os.environ["WREN_PLAN_CACHE_MAX"] = "3"
        W._ENGINE_CACHE.clear()
        for i in range(6):
            W._ENGINE_CACHE[("k", i)] = object()
            while len(W._ENGINE_CACHE) > W.cache_max():
                W._ENGINE_CACHE.popitem(last=False)
        check(len(W._ENGINE_CACHE) == 3, f"上限=3 时塞 6 个键 → 保留 3（实际 {len(W._ENGINE_CACHE)}）")
        check(list(W._ENGINE_CACHE) == [("k", 3), ("k", 4), ("k", 5)], "淘汰的是最旧的（LRU）")
        W._ENGINE_CACHE.clear()
        os.environ["WREN_PLAN_CACHE"] = "0"
        check(W._cache_enabled() is False, "WREN_PLAN_CACHE=0 → 关闭缓存（每调用都建）")
        os.environ["WREN_PLAN_CACHE"] = "1"
        check(W._cache_enabled() is True, "WREN_PLAN_CACHE=1 → 开")
    finally:
        W._ENGINE_CACHE.clear()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def section_engine_smoke() -> None:
    """§④ 真实引擎冷/热（有本地语义库才跑；容器里没有就跳过）。"""
    print("\n④ 真实引擎：冷建一次、热命中同一对象（有本地 mdl.json 才跑）")
    import os

    sys.path.insert(0, str(SRC))
    from agent.utils import wren_plan as W

    # 语义库就在**钉死的那份工作区**里（单工作区，2026-09-25）：
    # 本地 = <AGENT_DATA_ROOT>/workspace/<项目>/target/mdl.json，容器里同一套。
    # 旧的 `src/agent/workspace-temp/*`（死副本）已删除，故这里只问 WorkspaceManager。
    from agent.workspace_manager import get_workspace_manager as _get_wm

    ws_root = _get_wm().active_workspace
    cands = sorted(p for p in ws_root.rglob("target/mdl.json") if p.is_file())
    if not cands:
        print(f"  · 跳过：{ws_root} 下没有可用的 mdl.json（容器内属正常）")
        return
    conn = {
        "datasource": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "x", "user": "u", "password": "p",
    }
    mdl = cands[0]
    saved = os.environ.pop("WREN_PLAN_CACHE_MAX", None)
    try:
        W._ENGINE_CACHE.clear()
        import time

        t = time.perf_counter()
        e1 = W._engine_for(mdl, conn)
        cold = time.perf_counter() - t
        t = time.perf_counter()
        e2 = W._engine_for(mdl, conn)
        warm = time.perf_counter() - t
        check(e1 is e2, f"热命中返回同一引擎对象（冷 {cold*1000:.0f}ms / 热 {warm*1000:.3f}ms）")
        check(warm < max(cold, 0.001), "热路径不慢于冷路径")
        check(len(W._ENGINE_CACHE) == 1, "只留下 1 个缓存键")
        # 换连接指纹 → 新键（缓存键含连接 ⇒ 键数 ≈ 库数）
        W._engine_for(mdl, {**conn, "database": "y"})
        check(len(W._ENGINE_CACHE) == 2, "换连接指纹 → 新键（键数 ≈ 活跃库数）")
    finally:
        W._ENGINE_CACHE.clear()
        if saved is not None:
            os.environ["WREN_PLAN_CACHE_MAX"] = saved


def main() -> int:
    print("P3-7 wren/CPU 重活搬运静态回归")
    hits = section_real_tree()
    section_synthetic()
    section_cache()
    section_engine_smoke()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    if hits:
        print("违规：")
        for rel, ln, nm in hits:
            print(f"  {rel}:{ln}  {nm}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
