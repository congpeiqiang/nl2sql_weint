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
6. **「库里多出来的列」不能拿去做文本手术**：DDL 加了列而语义库没重建时，内省结果比
   模型文件多出列。`patch_existing` 若按**内省列**逐个开隐藏开关，就会在那一列上定位失败、
   让整张表 409 —— 而它的错误文案会把这口锅扣给「该文件可能被手工改过结构」，把
   **DB 漂移**说成**手改**（2026-10-08 生产 `witops-wrenai.do_table` 多出 `default_flag`
   实撞）。模型文件是「这个模型包含哪些列」的唯一权威 ⇒ 文件里没有的列**跳过不碰**、
   但要如实点名回报（响应 `warnings` + 后端 WARNING），不能静默吞掉。
7. **删除有两条通道，口径不同**：`prune=True` = 不在选择里的**全删**，它是唯一能清掉
   「数据库里已不存在的表」的办法（那种模型在对话框里没有勾选态可取消）；`delete_tables`
   = 调用方**逐表点名**（界面上「取消勾选」）。若让「取消勾选」也复用 `prune`，用户一旦
   顺手打开那个全局框，就会把自己从没碰过的孤儿模型一并删掉（静默误删）。
   （2026-10-08 生产实撞：前端在「新增模型后不重取 introspect」的旧快照下，全局框的候选
   列表是空的 ⇒ 框是灰的、勾不上 ⇒ 取消勾选后保存**静默不删**。）


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
    _declared_model_name,
    _file_column_names,
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
    """alpha (主表) ← beta.alpha_id；两张表各有可废弃的业务列。

    ⚠️ `primary_key` 必须是**字符串**：`db_introspect.TableInfo.primary_key` 是
    `str | None`（单列主键），真实内省从不产出列表。这里曾写成 `primary_key=["id"]`，
    于是「隐藏主键」的校验用真实的字符串时就静默失效（`for c in "id"` 逐字符）。
    """
    alpha = SimpleNamespace(
        name="alpha", comment="主表",
        columns=[col("id", pk=True, nullable=False),
                 col("name", comment="名称"),
                 col("old_flag", comment="废弃标记")],
        primary_key="id",
    )
    beta = SimpleNamespace(
        name="beta", comment="从表",
        columns=[col("id", pk=True, nullable=False), col("alpha_id"), col("old_note")],
        primary_key="id",
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
        "primary_key: id\ncached: false\n"
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

    # 夹具形态闸：primary_key 必须是字符串。写成列表时 `for c in pk` 逐字符迭代，
    # 「隐藏主键」在真实数据上会静默失效 —— 这条断言就是防它回来的。
    check(all(isinstance(t.primary_key, str) for t in res.tables),
          "夹具 primary_key 是字符串（与 db_introspect.TableInfo 一致）")
    err_pk = _validate_column_selection(res, ["alpha"], {"alpha": ["name", "old_flag"]})
    check(err_pk is not None and "alpha.id" in err_pk,
          "单列主键（字符串 id）被拒，且文案点出 alpha.id", err_pk)
    err_pk2 = _validate_column_selection(res, ["beta"], {"beta": ["alpha_id", "old_note"]})
    check(err_pk2 is not None and "beta.id" in err_pk2,
          "单列表 beta 同理被拒（文案点出 beta.id）", err_pk2)

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
    _, _, n, _ = _apply_model_selection(proj, res, ["alpha"], None,
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

    gen, removed, _, _ = _apply_model_selection(proj, res, ["alpha"], None,
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
    _, removed2, _, _ = _apply_model_selection(proj2, res, ["alpha"], None,
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
    gen, removed, n, _ = _apply_model_selection(
        proj, res, ["alpha", "beta"], {"alpha": ["id", "name"], "beta": ["id", "alpha_id"]},
        include_relationships=True, patch_existing=False, prune=False)
    check((gen, removed) == (2, 0), "两张表都写了、没删东西", (gen, removed))
    check(n == 1, "两端都选中 ⇒ 1 条关系", n)
    got = _read_existing_models(proj)
    check(got["alpha"]["hidden"] == ["old_flag"], "读回来与写出去一致", got["alpha"])
    check(got["beta"]["hidden"] == ["old_note"], "beta 也一致", got["beta"])


# ── ⑩ 库里多出来的列（DDL 漂移）───────────────────────────────

def _toggleable(text: str, name: str) -> bool:
    """文件里的这个列名，`_set_column_hidden` 认不认得。"""
    try:
        _set_column_hidden(text, name, True)
        return True
    except ValueError:
        return False


def section_db_drift() -> None:
    section("⑩ 库里多出来的列：跳过 + 点名，不再把「DB 漂移」误报成「文件被手改」")
    res = introspect_result()
    proj = make_project(WORK / "drift")
    _apply_model_selection(proj, res, ["alpha"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    meta = proj / "models" / "alpha" / "metadata.yml"
    before = meta.read_text(encoding="utf-8")

    # 对照：库与文件一致时，一个跳过项都不该有，也不许白改文件
    _, _, _, skipped0 = _apply_model_selection(
        proj, res, ["alpha"], None,
        include_relationships=True, patch_existing=True, prune=False)
    check(skipped0 == [], "库和文件一致 ⇒ 跳过列表为空（不是「内省列全算跳过」）", skipped0)
    check(meta.read_text(encoding="utf-8") == before, "没有漂移时文件一个字节都没动")
    check("new_flag" not in before, "夹具就绪：模型文件里没有 new_flag")

    # 模拟 DDL 漂移：库给 alpha 加了一列、语义库没重建 ⇒ 内省结果比文件多出 new_flag
    res.tables[0].columns.append(col("new_flag", comment="库里新加的列"))

    # (a) 这张表没有任何列被藏（前端在「一列都没勾掉」时根本不发 selected_columns）
    _, _, _, skipped_a = _apply_model_selection(
        proj, res, ["alpha"], None,
        include_relationships=True, patch_existing=True, prune=False)
    check(skipped_a == ["alpha.new_flag"], "漂移列被如实点名（表名.列名）", skipped_a)
    check(meta.read_text(encoding="utf-8") == before,
          "漂移列**没被写进文件**：跳过就意味着这个文件与漂移前逐字相同",
          meta.read_text(encoding="utf-8")[:200])

    # (b) 前端真实形态：对话框拿**数据库**列渲染 ⇒ new_flag 在里面是个正常的、被勾成
    #     「可见」的列；同时 old_flag 被勾掉。漂移列必须完全不影响结果。
    _, _, _, skipped_b = _apply_model_selection(
        proj, res, ["alpha"], {"alpha": ["id", "name", "new_flag"]},
        include_relationships=True, patch_existing=True, prune=False)
    after = meta.read_text(encoding="utf-8")
    check(skipped_b == ["alpha.new_flag"], "同一次请求里仍然点名漂移列", skipped_b)
    check(after == _set_column_hidden(before, "old_flag", True),
          "结果与「没有漂移列」时**逐字相同**（漂移列对文件零影响）",
          after)
    check("new_flag" not in after, "没有把库里的新列顺手补进模型（口径：跳过就是不碰）")
    check(_file_column_names(after) == {"id", "name", "old_flag"},
          "文件的列集合与漂移前完全一致", _file_column_names(after))
    check(len(after.splitlines()) == len(before.splitlines()) + 1,
          "只多出隐藏开关那一行", (len(before.splitlines()), len(after.splitlines())))

    # 两处判据必须同源：凡是 `_file_column_names` 说「在文件里」的列，`_set_column_hidden`
    # 都得认（否则会出现「没跳过却改不动」）；反之文件里真没有的列必须是可跳过的。
    judge = (
        "columns:\n"
        "- name: id\n  type: INTEGER\n  is_primary_key: true\n"
        "- name: 'quoted_name'\n  type: VARCHAR\n"
        "primary_key: id\n"
    )
    names = _file_column_names(judge)
    check(names == {"id", "quoted_name"}, "_file_column_names 认得出带引号的列名", names)
    check(all(_toggleable(judge, n) for n in names),
          "文件里的每个列名 _set_column_hidden 都认得（两处判据同源，不会「跳过了其实能改的列」）",
          [n for n in names if not _toggleable(judge, n)])
    check(not _toggleable(judge, "ghost") and "ghost" not in names,
          "文件里真没有的列：两边一致地判为「不在文件里」")

    # 负对照：**真的**结构性坏文件（同名列出现多次）仍然硬报错 —— 跳过只针对「文件里没有
    # 的列」，不针对「文件里有但认不出的列」。
    bad = make_project(WORK / "drift_bad")
    (bad / "models" / "alpha").mkdir()
    (bad / "models" / "alpha" / "metadata.yml").write_text(
        "name: alpha\ntable_reference:\n  table: alpha\ncolumns:\n"
        "- name: id\n  type: INTEGER\n  is_primary_key: true\n"
        "- name: name\n  type: VARCHAR\n"
        "- name: name\n  type: VARCHAR\n",
        encoding="utf-8",
    )
    try:
        _apply_model_selection(bad, res, ["alpha"], None,
                               include_relationships=True, patch_existing=True, prune=False)
        check(False, "同名列文件应当抛 RuntimeError（409），不许跳过")
    except RuntimeError as e:
        check("出现多次" in str(e), "真·结构坏 ⇒ RuntimeError 且点出原因", e)
    except ValueError as e:
        check(False, "应当被包装成 RuntimeError(409) 而不是 ValueError(400)", e)

    # 接线：跳过项必须**出得来**（响应 warnings + 后端 WARNING），否则等于静默吞掉
    src = (SRC / "api" / "wren_semantic.py").read_text(encoding="utf-8")
    check("in_file = _file_column_names(text)" in src,
          "patch 分支先取「文件里有哪些列」再逐个手术")
    check('payload["warnings"]' in src, "跳过项进入 generate-models 响应的 warnings")
    check("在数据库里有、模型文件里没有" in src,
          "同时打一条后端 WARNING（只回前端不够：日志里也要能查到）")


# ── ⑪ 点名删除（界面上「取消勾选」）────────────────────────────

def section_explicit_delete() -> None:
    section("⑪ delete_tables：取消勾选即删，且不连坐孤儿模型")
    res = introspect_result()
    proj = make_project(WORK / "explicit_del")
    _apply_model_selection(proj, res, ["alpha", "beta"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    # 孤儿模型：库里有文件、数据库里已经没有这张表了（对话框列表里根本没有它，无从取消勾选）
    ghost = proj / "models" / "gone_table"
    ghost.mkdir()
    (ghost / "metadata.yml").write_text(
        "name: gone_table\ntable_reference:\n  table: gone_table\ncolumns:\n"
        "- name: id\n  type: INTEGER\n  is_primary_key: true\n",
        encoding="utf-8")

    # 用户在界面上把 beta 取消勾选 ⇒ 点名删除。prune=False ⇒ 孤儿必须活着。
    _, removed, _, _ = _apply_model_selection(
        proj, res, ["alpha"], None, include_relationships=True,
        patch_existing=True, prune=False, delete_tables=["beta"])
    check(removed == 1, "点名 beta ⇒ removed=1", removed)
    check(not (proj / "models" / "beta").exists(), "beta 目录已删")
    check((ghost / "metadata.yml").is_file(),
          "**孤儿模型没被连坐**（这就是它不能复用 prune 的原因）")
    check((proj / "models" / "alpha" / "metadata.yml").is_file(), "alpha 保留")

    # 反向：全局框那条通道（prune=True）依旧把孤儿一起清掉 —— 两者分工不同
    _, removed, _, _ = _apply_model_selection(
        proj, res, ["alpha"], None, include_relationships=True,
        patch_existing=True, prune=True, delete_tables=None)
    check(removed == 1 and not ghost.exists(),
          "prune=True ⇒ 孤儿被清掉（全局框的职责，与点名删除分工不同）", removed)

    # 目录名 ≠ 声明名时，按声明名点名也要删得掉（否则又是静默 no-op）
    proj2 = make_project(WORK / "explicit_alias")
    _apply_model_selection(proj2, res, ["alpha"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    alias = proj2 / "models" / "alpha" / "metadata.yml"
    alias.write_text(
        alias.read_text(encoding="utf-8").replace("name: alpha", "name: alpha_alias", 1),
        encoding="utf-8")
    check(_declared_model_name(alias) == "alpha_alias",
          "夹具就绪：声明名已改成 alpha_alias（目录名仍是 alpha）", _declared_model_name(alias))
    _, removed2, _, _ = _apply_model_selection(
        proj2, res, ["beta"], None, include_relationships=True,
        patch_existing=True, prune=False, delete_tables=["alpha_alias"])
    check(removed2 == 1 and not (proj2 / "models" / "alpha").exists(),
          "按**声明名**点名 ⇒ 照样删得掉（前端手里的键就是声明名）", removed2)

    # 防御：同一个名字既在「选中」又在「点名删除」里 ⇒ 以选中为准，不删
    proj3 = make_project(WORK / "explicit_conflict")
    _apply_model_selection(proj3, res, ["alpha"], None,
                           include_relationships=True, patch_existing=False, prune=False)
    _, removed3, _, _ = _apply_model_selection(
        proj3, res, ["alpha"], None, include_relationships=True,
        patch_existing=True, prune=False, delete_tables=["alpha"])
    check(removed3 == 0 and (proj3 / "models" / "alpha" / "metadata.yml").is_file(),
          "既选中又点名 ⇒ 以「选中」为准，不删（矛盾输入取安全的一边）", removed3)

    # 点名一张不存在的模型 ⇒ 幂等 no-op，不报错
    _, removed4, _, _ = _apply_model_selection(
        proj3, res, ["alpha"], None, include_relationships=True,
        patch_existing=True, prune=False, delete_tables=["__nope__"])
    check(removed4 == 0, "点名不存在的模型 ⇒ 幂等（重复提交同一份清单不会炸）", removed4)

    # 接线：端点解析 + 同一道守卫 + 透传
    src = (SRC / "api" / "wren_semantic.py").read_text(encoding="utf-8")
    check('data.get("delete_tables")' in src and "delete_tables=delete_tables" in src,
          "端点解析 delete_tables 并透传给核心函数")
    check("delete_tables 必须与 patch_existing 同时使用" in src,
          "delete_tables 与 prune 同一道守卫（删除前必须确认是调整已有库）")
    check('"delete_tables 需为 [表名] 形式"' in src, "类型闸：非字符串列表直接 400")


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
        section_db_drift()
        section_explicit_delete()
    finally:
        if os.environ.get("KEEP_WORK") != "1":
            shutil.rmtree(WORK, ignore_errors=True)

    print(f"\n{PASS}/{PASS + FAIL} 通过" + (f"（{SKIP} 项跳过）" if SKIP else ""))
    if os.environ.get("KEEP_WORK") == "1":
        print(f"（工作目录保留供排查：{WORK}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
