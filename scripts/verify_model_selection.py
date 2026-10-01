# -*- coding: utf-8 -*-
"""语义库「按指定表和指定字段构建」回归。

**被测的坑（2026-10-01 实测，全部是本脚本要钉死的事实）**：

1. **隐藏字段的键名是 `is_hidden`，不是 `hidden`。** wren 把 metadata.yml 的 snake_case
   键经 `wren/context.py` 的映射表转成 mdl.json 的 camelCase（`is_hidden` → `isHidden`），
   之后 `wren/mdl/cte_rewriter.py` 按 `isHidden` 过滤；`hidden` **不在映射表里**，会被原样
   搬进 mdl.json 成一个**没有任何代码读**的键。生产库 `witops-wrenai` 一度有 6 个文件
   15 处写的都是 `hidden: true` ⇒ 那些「已废弃」标注**全部静默失效**。
2. **不能删列，只能隐藏**：`relationships.yml` 的条件是 `a.id = b.a_id` 这种列名文本，
   删列会让 `wren context build` 报错。
3. **隐藏列不能出现在关系条件里**：构建能过，之后任何 JOIN 才报 `No field named …`
   ⇒ 主键列与被关系引用的列必须**拒绝隐藏**（fail-closed，一个字节都不落盘）。
4. **关系过滤的老判据是 `and`**（两端都没选才跳过）⇒ 只取消勾选一侧时照样写出引用
   「不存在的模型」的关系，`wren context build` 直接 `references unknown model` 失败。
5. **已有库不能重新生成**：用内省结果重写会抹掉手改的描述 ⇒ `patch_existing=True`
   走文本手术，只改隐藏开关。

本脚本用**真 wren**（`.venv` 里的 `wren`）在真实文件系统上构建并检查 `target/mdl.json`，
而不是只断言「代码里调用了某个函数」。

跑法：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_model_selection.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-modelsel-"))

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from api.wren_semantic import (  # noqa: E402
    _apply_model_selection,
    _read_existing_models,
    _relationship_fk_columns,
    _render_model_yaml,
    _set_column_hidden,
    _validate_column_selection,
)

PASS = 0
FAIL = 0
SKIP = 0
WORK: Path | None = None

WREN = next(
    (p for p in (ROOT / ".venv/Scripts/wren.exe", ROOT / ".venv/bin/wren") if p.is_file()),
    None,
)


def check(cond: bool, label: str, extra: object = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" -> {extra!r}" if extra != "" else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 68 - len(title)))


def skip(label: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {label}")


# ── 夹具 ─────────────────────────────────────────────────────

def col(name: str, *, pk: bool = False, nullable: bool = True, comment: str = ""):
    return SimpleNamespace(
        name=name, type="INTEGER" if name.endswith("id") or name == "id" else "VARCHAR",
        wren_type="INTEGER" if name.endswith("id") or name == "id" else "VARCHAR",
        nullable=nullable, comment=comment, is_primary_key=pk,
    )


def introspect_result():
    """alpha (主表) ← beta.alpha_id；两张表各有可废弃的业务列。"""
    alpha = SimpleNamespace(
        name="alpha", comment="主表",
        columns=[col("id", pk=True, nullable=False),
                 col("name", comment="名称"),
                 col("old_flag", comment="废弃标记")],
        primary_key=["id"],
    )
    beta = SimpleNamespace(
        name="beta", comment="从表",
        columns=[col("id", pk=True, nullable=False), col("alpha_id"), col("old_note")],
        primary_key=["id"],
    )
    fks = [SimpleNamespace(source_table="beta", source_column="alpha_id",
                           target_table="alpha", target_column="id")]
    return SimpleNamespace(tables=[alpha, beta], foreign_keys=fks)


def make_project(root: Path, name: str = "libsel") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "wren_project.yml").write_text(
        "schema_version: 5\n"
        f"name: {name}\n"
        "version: '1.0'\n"
        "catalog: wren\n"
        "schema: public\n"
        "data_source: mysql\n",
        encoding="utf-8",
    )
    (root / "models").mkdir(exist_ok=True)
    return root


def wren_build(project: Path) -> tuple[bool, str]:
    """真跑 `wren context build`；返回 (是否成功, 输出)。"""
    env = dict(os.environ, PYTHONUTF8="1")
    r = subprocess.run(
        [str(WREN), "context", "build"], cwd=str(project), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def dry_plan(project: Path, sql: str) -> tuple[bool, str]:
    """`wren dry-plan --sql`：只编译不执行（不需要真库连接）。"""
    env = dict(os.environ, PYTHONUTF8="1")
    r = subprocess.run(
        [str(WREN), "dry-plan", "--sql", sql], cwd=str(project), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def mdl(project: Path) -> dict:
    return json.loads((project / "target" / "mdl.json").read_text(encoding="utf-8"))


def model_cols(mdl_data: dict, name: str) -> dict[str, dict]:
    for m in mdl_data.get("models", []):
        if m["name"] == name:
            return {c["name"]: c for c in m.get("columns", [])}
    return {}


# ── ① 隐藏生效（真 wren）──────────────────────────────────────

def section_hidden_effective() -> None:
    section("① 隐藏生效：is_hidden → mdl.json 的 isHidden，且真的查不到")
    proj = make_project(WORK / "hidden")
    res = introspect_result()
    _apply_model_selection(
        proj, res, ["alpha", "beta"], {"alpha": ["id", "name"]},
        include_relationships=True, patch_existing=False, prune=False,
    )

    text = (proj / "models" / "alpha" / "metadata.yml").read_text(encoding="utf-8")
    check("  is_hidden: true" in text, "metadata.yml 里写的是 `is_hidden: true`（不是 hidden）")
    check("\n  hidden: true" not in text, "没有写出那个没人读的 `hidden: true`")
    check("old_flag" in text, "被隐藏的列**仍在** columns 里（不是删掉，可还原）")

    if WREN is None:
        skip("真 wren 未找到，跳过 mdl.json 断言")
        return
    ok, out = wren_build(proj)
    if not check(ok, "wren context build 成功", out[-400:]):
        return
    data = mdl(proj)
    cols = model_cols(data, "alpha")
    check(cols.get("old_flag", {}).get("isHidden") is True,
          "mdl.json: alpha.old_flag 的 isHidden=true", cols.get("old_flag"))
    check("hidden" not in cols.get("old_flag", {}),
          "mdl.json: 没有 `hidden` 这个死键（老写法会留下它）", cols.get("old_flag"))
    check(cols.get("name", {}).get("isHidden") in (None, False),
          "未勾掉的列没有被隐藏")

    # 真查询。⚠️ 实测口径（别把断言写松也别写错）：
    #   - `SELECT *` **不含**隐藏列 ⇒ 它确实不在模型注册的 schema 里；
    #   - 在 **JOIN 条件**里引用隐藏列 ⇒ 规划期硬报错 `No field named`（见下面负对照）；
    #   - 只在 **SELECT 列表**里引用隐藏列 ⇒ dry-plan **不报错**，但模型那层 CTE 会退化成
    #     `SELECT 1`（列没了）—— 所以「dry-plan 没报错」不等于「列还在」。
    ok_name, out_name = dry_plan(proj, "SELECT name FROM alpha")
    check(ok_name, "`SELECT name FROM alpha` 能编译（未隐藏列可用）", out_name[-300:])
    ok_star, out_star = dry_plan(proj, "SELECT * FROM alpha")
    check(ok_star and "old_flag" not in out_star,
          "`SELECT * FROM alpha` 不含 old_flag ⇒ 隐藏列已摘出模型 schema", out_star[-300:])
    check("," in out_star and '"id"' in out_star and '"name"' in out_star,
          "`SELECT *` 里 id / name 都还在", out_star[-300:])

    # 负对照：手工把一个**被 JOIN 条件引用**的列标成隐藏 ⇒ 规划期硬报错。
    # 这正是 _validate_column_selection 必须拒绝「藏关系列」的原因。
    bad = make_project(WORK / "hidden_join")
    _apply_model_selection(bad, res, ["alpha", "beta"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    bmeta = bad / "models" / "beta" / "metadata.yml"
    bmeta.write_text(_set_column_hidden(bmeta.read_text(encoding="utf-8"), "alpha_id", True),
                     encoding="utf-8")
    bad_ok, bad_out = wren_build(bad)
    check(bad_ok, "构建还是能过（这就是它危险的地方：坏库构建不报错）", bad_out[-300:])
    j_ok, j_out = dry_plan(bad, "SELECT beta.alpha_id, alpha.name FROM beta JOIN alpha ON beta.alpha_id = alpha.id")
    check(not j_ok and "field" in j_out.lower(),
          "负对照：隐藏列被 JOIN 引用 ⇒ `No field named` 硬报错", j_out[-300:])


# ── ② 缺省向后兼容 ────────────────────────────────────────────

def section_default_compat() -> None:
    section("② 不传 selected_columns ⇒ 与旧行为逐字相同")
    proj = make_project(WORK / "compat")
    res = introspect_result()
    _apply_model_selection(proj, res, ["alpha", "beta"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    text = (proj / "models" / "alpha" / "metadata.yml").read_text(encoding="utf-8")
    check("is_hidden" not in text, "没有 is_hidden 键")
    check("hidden" not in text, "也没有 hidden 键")
    # 与「旧代码会写出的内容」逐字比对：_render_model_yaml 的 visible=None 分支
    expect = _render_model_yaml("alpha", res.tables[0], None)
    check(text == expect, "内容与 _render_model_yaml(visible=None) 逐字相同")


# ── ③ 文本手术 ────────────────────────────────────────────────

def section_text_surgery() -> None:
    section("③ _set_column_hidden：一行之差、幂等、认不出就报错")
    # 用带长描述的真实形态做样本（yaml.dump 会按 80 列重排 —— 这正是不能用 dump 的原因）
    src = (
        "# alpha table\nname: alpha\ntable_reference:\n  table: alpha\ncolumns:\n"
        "- name: id\n  type: INTEGER\n  is_calculated: false\n  not_null: true\n"
        "  is_primary_key: true\n"
        "- name: name\n  type: VARCHAR\n  is_calculated: false\n  not_null: false\n"
        "  is_primary_key: false\n  properties:\n"
        "    description: 这是一个很长的中文描述，长到足以让 yaml.dump 的默认宽度把它折成两行，"
        "从而在重排时产生与本次改动无关的 diff\n"
        "primary_key:\n- id\ncached: false\n"
    )
    out = _set_column_hidden(src, "name", True)
    import difflib
    diff = [l for l in difflib.unified_diff(src.splitlines(), out.splitlines(), lineterm="")
            if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    check(diff == ["+  is_hidden: true"], "只多出一行 `is_hidden: true`（长描述未被重排）", diff)
    check(_set_column_hidden(out, "name", True) == out, "重复打开幂等")
    check(_set_column_hidden(out, "name", False) == src, "关闭后与原文逐字相等")
    check(_set_column_hidden(src, "name", False) == src, "本来没开时关闭是 no-op")

    legacy = src.replace("  is_primary_key: false\n  properties:",
                         "  is_primary_key: false\n  hidden: true\n  properties:")
    norm = _set_column_hidden(legacy, "name", True)
    check("is_hidden: true" in norm and "\n  hidden: true" not in norm,
          "历史 `hidden: true` 就地归一为 `is_hidden: true`（不新增行）")
    check(len(norm.splitlines()) == len(legacy.splitlines()), "归一不改变行数")

    for label, fn in (
        ("列不存在", lambda: _set_column_hidden(src, "__nope__", True)),
        ("同名列出现多次", lambda: _set_column_hidden(src + "- name: name\n  type: VARCHAR\n", "name", True)),
    ):
        try:
            fn()
            check(False, f"{label} 应当抛 ValueError")
        except ValueError:
            check(True, f"{label} ⇒ ValueError（不猜着改）")

    probed = make_project(WORK / "probe")
    (probed / "models" / "alpha").mkdir()
    (probed / "models" / "alpha" / "metadata.yml").write_text(legacy, encoding="utf-8")
    got = _read_existing_models(probed)
    check(got.get("alpha", {}).get("hidden") == ["name"],
          "_read_existing_models 认得出历史 `hidden:` 写法", got)


# ── ④ 校验 fail-closed ────────────────────────────────────────

def section_validation() -> None:
    section("④ 校验 fail-closed：拒绝的请求一个字节都不落盘")
    res = introspect_result()
    proj = make_project(WORK / "reject")

    cases = [
        ("隐藏主键列", ["alpha", "beta"], {"alpha": ["name", "old_flag"]}, "主键"),
        ("隐藏被关系引用的列", ["alpha", "beta"], {"beta": ["id"]}, "表关联"),
        ("列不存在", ["alpha"], {"alpha": ["id", "name", "__ghost__"]}, "不存在"),
    ]
    for label, tables, cols, expect_kw in cases:
        err = _validate_column_selection(res, tables, cols)
        check(err is not None and expect_kw in err, f"{label} ⇒ 拒绝", err)
        check(err is not None and tables[0] in err, f"{label} ⇒ 文案含表名", err)
    check(_validate_column_selection(res, ["alpha"], {"alpha": ["id", "name", "old_flag"]}) is None,
          "全列可见时放行")
    check(_validate_column_selection(res, ["alpha"], None) is None, "不传 selected_columns 时放行")
    check(_validate_column_selection(res, ["alpha"], {"alpha": ["id", "old_flag"]}) is None,
          "藏一个普通业务列放行（old_flag 既非主键也非关系列）")

    # **一字节都不落盘**：校验在核心函数第一步，拒绝时直接抛 ValueError
    before = sorted(str(p.relative_to(proj)) for p in proj.rglob("*"))
    for label, tables, cols in (
        ("藏主键列", ["alpha"], {"alpha": ["name", "old_flag"]}),
        ("藏关系列", ["alpha", "beta"], {"beta": ["id"]}),
        ("列不存在", ["alpha"], {"alpha": ["id", "name", "__ghost__"]}),
    ):
        try:
            _apply_model_selection(proj, res, tables, cols,
                                   include_relationships=True,
                                   patch_existing=False, prune=False)
            check(False, f"{label} 应当抛 ValueError")
        except ValueError:
            check(True, f"{label} ⇒ ValueError")
    check(sorted(str(p.relative_to(proj)) for p in proj.rglob("*")) == before,
          "三次被拒后目录结构逐项未变（拒绝的请求不落盘）")
    check(not (proj / "models" / "alpha").exists(), "被拒时连 models/ 目录都没建")


# ── ⑤ 关系过滤修复 ────────────────────────────────────────────

def section_relationship_filter() -> None:
    section("⑤ 关系过滤 and → or：只选一侧时不再写出引用不存在模型的关系")
    res = introspect_result()
    fk_cols, _ = _relationship_fk_columns(res.foreign_keys, ["alpha"])
    check(fk_cols == {}, "只选 alpha ⇒ 这条外键整条不算数（两端都要在）", fk_cols)
    fk_cols2, _ = _relationship_fk_columns(res.foreign_keys, ["alpha", "beta"])
    check(fk_cols2 == {"alpha": {"id"}, "beta": {"alpha_id"}},
          "两端都选 ⇒ 两侧列都被记为关系列", fk_cols2)

    proj = make_project(WORK / "rel")
    _, _, n = _apply_model_selection(proj, res, ["alpha"], None,
                                     include_relationships=True, patch_existing=False, prune=False)
    check(n == 0, "只选 alpha ⇒ relationships.yml 里 0 条关系", n)
    data = __import__("yaml").safe_load((proj / "relationships.yml").read_text(encoding="utf-8"))
    check(data.get("relationships") == [], "relationships.yml 内容为空列表", data)

    if WREN is None:
        skip("真 wren 未找到，跳过「构建必须成功」断言")
        return
    ok, out = wren_build(proj)
    check(ok, "只选一张表 ⇒ wren context build 成功（老判据在这里会失败）", out[-500:])

    # 负对照：把老判据会写出的内容手工放回去 ⇒ 构建必须失败（钉死这个 bug 的形状）
    (proj / "relationships.yml").write_text(
        "relationships:\n"
        "- name: alpha_beta\n"
        "  models:\n  - alpha\n  - beta\n"
        "  join_type: MANY_TO_ONE\n"
        "  condition: beta.alpha_id = alpha.id\n",
        encoding="utf-8",
    )
    bad_ok, bad_out = wren_build(proj)
    check(not bad_ok and "unknown model" in bad_out.lower(),
          "负对照：关系指向未选中的表 ⇒ 构建失败（证明确实是坏库）", bad_out[-300:])


# ── ⑥ prune ──────────────────────────────────────────────────

def section_prune() -> None:
    section("⑥ prune：删掉未选中的表目录，且不碰 models/ 之外的东西")
    res = introspect_result()
    proj = make_project(WORK / "prune")
    _apply_model_selection(proj, res, ["alpha", "beta"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    (proj / "knowledge" / "glossary").mkdir(parents=True)
    (proj / "knowledge" / "glossary" / "术语表.md").write_text("# 术语\n", encoding="utf-8")
    check((proj / "models" / "beta" / "metadata.yml").is_file(), "先有 beta 模型")

    gen, removed, _ = _apply_model_selection(proj, res, ["alpha"], None,
                                             include_relationships=True,
                                             patch_existing=True, prune=True)
    check(removed == 1, "removed 计数 = 1", removed)
    check(not (proj / "models" / "beta").exists(), "beta 目录已删除")
    check((proj / "models" / "alpha" / "metadata.yml").is_file(), "alpha 保留")
    check((proj / "knowledge" / "glossary" / "术语表.md").is_file(),
          "knowledge/ 不受影响")

    # 不传 prune 时不许删（新建流程不受影响）
    proj2 = make_project(WORK / "noprune")
    _apply_model_selection(proj2, res, ["alpha", "beta"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    _, removed2, _ = _apply_model_selection(proj2, res, ["alpha"], None,
                                           include_relationships=True,
                                           patch_existing=False, prune=False)
    check(removed2 == 0 and (proj2 / "models" / "beta").exists(),
          "prune=False ⇒ 一个目录都不删（老行为）")


# ── ⑦ patch_existing 保手改 ───────────────────────────────────

def section_patch_existing() -> None:
    section("⑦ patch_existing：保住手改过的模型文件内容")
    res = introspect_result()
    proj = make_project(WORK / "patch")
    _apply_model_selection(proj, res, ["alpha"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    meta = proj / "models" / "alpha" / "metadata.yml"
    hand = meta.read_text(encoding="utf-8").replace(
        "description: 名称", "description: 名称（这是同事手改过的描述，不许丢）")
    meta.write_text(hand, encoding="utf-8")

    _apply_model_selection(proj, res, ["alpha"], {"alpha": ["id", "name"]},
                           include_relationships=True, patch_existing=True, prune=True)
    after = meta.read_text(encoding="utf-8")
    check("这是同事手改过的描述，不许丢" in after, "手改的描述还在")
    check("  is_hidden: true" in after, "同时确实加上了隐藏开关")
    check(len(after.splitlines()) == len(hand.splitlines()) + 1,
          "只多了一行", (len(hand.splitlines()), len(after.splitlines())))

    # 反向：patch_existing=False 会把手改抹掉（这是**已知且有意**的：新建流程的旧行为）
    _apply_model_selection(proj, res, ["alpha"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    check("这是同事手改过的描述，不许丢" not in meta.read_text(encoding="utf-8"),
          "patch_existing=False 仍会重写（新建流程旧行为，向后兼容）")


# ── ⑧ introspect 加法 + ⑨ 接线闸 ──────────────────────────────

def section_wiring() -> None:
    section("⑧ introspect 响应加法 / ⑨ 接线闸（文本 + AST）")
    src = (SRC / "api" / "wren_semantic.py").read_text(encoding="utf-8")
    check('"existing_models": _read_existing_models(project)' in src,
          "introspect 响应里挂上 existing_models（调整对话框的当前状态来源）")
    check("selected_columns" in src and "patch_existing" in src and '"prune"' in src,
          "generate_models 接受 selected_columns / patch_existing / prune")
    check("and fk.target_table not in selected_tables" not in src,
          "老的关系过滤 `and` 判据已消除")
    check(src.count("admin_guard(request, \"生成模型\")") == 1,
          "generate_models 仍走 admin_guard（不是抛异常的 require_admin）")
    check("require_admin(request)" not in src, "本文件里没有抛异常式的 require_admin")

    # 用真内省结果跑一遍「藏一个普通列」，证明端到端不报错
    proj = make_project(WORK / "e2e")
    res = introspect_result()
    gen, removed, n = _apply_model_selection(
        proj, res, ["alpha", "beta"], {"alpha": ["id", "name"], "beta": ["id", "alpha_id"]},
        include_relationships=True, patch_existing=False, prune=False)
    check((gen, removed) == (2, 0), "两张表都写了、没删东西", (gen, removed))
    check(n == 1, "两端都选中 ⇒ 1 条关系", n)
    got = _read_existing_models(proj)
    check(got["alpha"]["hidden"] == ["old_flag"], "读回来与写出去一致", got["alpha"])
    check(got["beta"]["hidden"] == ["old_note"], "beta 也一致", got["beta"])


def main() -> int:
    global WORK
    WORK = Path(tempfile.mkdtemp(prefix="verify_modelsel_"))
    print(f"语义库「按表/字段构建」回归（工作目录 {WORK}）")
    print(f"wren 二进制：{WREN or '未找到（真构建类断言会跳过）'}")

    try:
        section_hidden_effective()
        section_default_compat()
        section_text_surgery()
        section_validation()
        section_relationship_filter()
        section_prune()
        section_patch_existing()
        section_wiring()
    finally:
        if os.environ.get("KEEP_WORK") != "1":
            shutil.rmtree(WORK, ignore_errors=True)

    print(f"\n{PASS}/{PASS + FAIL} 通过" + (f"（{SKIP} 项跳过）" if SKIP else ""))
    if os.environ.get("KEEP_WORK") == "1":
        print(f"（工作目录保留供排查：{WORK}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
