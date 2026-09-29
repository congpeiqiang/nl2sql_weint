# -*- coding: utf-8 -*-
"""混合检索薄层验收（离线：无数据库、无 GPU；lance 段需要装了 lancedb）。

**为什么要这份脚本**：检索层的正确性几乎全在「边界」上，而边界都是静默的 ——

1. 中文 FTS 的 **3 字符/整段下限**：`工时` / `部门` / `项目` 这类两字业务词在 trigram
   与 lance 的 `ngram` tokenizer 里**匹配不到任何东西且不报错**，只会安静地返回空
   （memory `chinese-fts-needs-3-chars`；lance 侧实测**连 3 字都零命中**，必须 icu）；
2. **标识符按 `_` 切开**：`customer_t` 不切开时 `customer` 一条也命中不了。自写腿与
   ICU 分词**都不做这件事**，两边各自要绕（自写腿按 `_` 分词；lance 侧归一化列 + 追加
   原始标识符）；
3. **缓存的 Table 对象在 overwrite 后仍返回旧数据**（实测）⇒ 缓存必须按版本提示失效；
4. **嵌入通道随时会死**（`.13` 不是我们的机器）：它死掉必须只损失质量、不损失可用性 ——
   即「纯 FTS 索引照样能建、照样能查」；
5. **索引是派生物**：mdl.json 缺了就必须拒绝构建，否则写出零条目的假索引，读侧会以为
   一切正常而永远返回空；
6. **输入只许来自注册表**：工作区里躺着 `<name>.备份-<ts>`，扫目录会把备份当活库。

**两个后端都跑同一套语义断言**（`jsonl` 与 `lance`），另有各自的后端专属段：
jsonl = 存储原语（稀疏向量/缓存分桶）；lance = 引擎面（icu 分词、下划线、缓存失效、
增量、索引缺失不抛）。每段带**负对照**（"开关关掉 ⇒ 与今天逐字一致"是核心不变量）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_retrieval.py

退出码 0 = 全部通过。lancedb 不可用时 lance 段会被跳过（不计入通过率，但会打印）。
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from unittest import mock

# ⚠️ 必须在 import 业务模块之前：数据落点由 AGENT_DATA_ROOT 推导
_REPO = pathlib.Path(__file__).resolve().parents[1]
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-retrieval-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ["NL2SQL_EMBED_DIM"] = "16"          # 假嵌入用 16 维，碰撞多但足以验融合
os.environ["NL2SQL_EMBED_TIMEOUT"] = "1"
os.environ["NL2SQL_EMBED_COOLDOWN"] = "0"     # 熔断默认关：避免跨段的窗口把后续用例带偏

sys.path.insert(0, str(_REPO / "src"))

results: list[tuple[bool, str, str]] = []
_scene = ["（未分组）"]


def section(title: str) -> None:
    _scene[0] = title
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def check(cond, label: str, extra: str = "") -> None:
    results.append((bool(cond), label, _scene[0]))
    tail = f"  | {extra}" if extra else ""
    print(f"  {'[OK]  ' if cond else '[FAIL]'} {label}{tail}")


def set_mode(mode: str | None) -> None:
    """切换检索开关（`None` ⇒ 恢复默认＝完全不设该 env）。"""
    if mode is None:
        os.environ.pop("NL2SQL_RETRIEVAL", None)
    else:
        os.environ["NL2SQL_RETRIEVAL"] = mode


def set_backend(name: str) -> None:
    os.environ["NL2SQL_RETRIEVAL_BACKEND"] = name


def set_base_url(url: str) -> None:
    os.environ["NL2SQL_EMBED_BASE_URL"] = url


# 默认把嵌入通道指向一个必然连不上的端口：**默认构建即纯 FTS**，无需任何 patch。
set_base_url("http://127.0.0.1:9/v1")

from agent.retrieval import backends, embedder, indexer, schema as S, search, store  # noqa: E402
from agent.retrieval.backends import jsonl as jsonl_be  # noqa: E402

# ── 夹具：一个真的（很小的）wren 项目 ────────────────────
MDL = {
    "models": [
        {
            "name": "workhour",
            "tableReference": {"catalog": "dw", "schema": "hr", "table": "t_workhour"},
            "properties": {"displayName": "工时表", "description": "员工每月工时记录"},
            "columns": [
                {"name": "emp_id", "type": "INTEGER", "properties": {"displayName": "员工编号"}},
                {
                    "name": "hours",
                    "type": "DOUBLE",
                    "properties": {"displayName": "工时", "description": "当月合计工时"},
                },
            ],
            "metrics": [
                {"name": "total_hours", "description": "总工时", "expression": "SUM(hours)"}
            ],
        },
        {
            "name": "dept",
            "tableReference": {"catalog": "dw", "schema": "hr", "table": "t_dept"},
            "properties": {"displayName": "部门表"},
            "columns": [
                {"name": "dept_name", "type": "VARCHAR", "properties": {"displayName": "部门名称"}}
            ],
        },
        {
            # 物理表名带下划线后缀 —— 「查 customer 要能命中 customer_t」的回归闸
            "name": "customer",
            "tableReference": {"catalog": "dw", "schema": "crm", "table": "customer_t"},
            "properties": {"displayName": "客户表"},
            "columns": [
                {"name": "cust_no", "type": "VARCHAR", "properties": {"displayName": "客户编号"}}
            ],
        },
    ],
    "views": [
        {
            "name": "dept_summary",
            "statement": "SELECT dept_name, COUNT(*) FROM t_dept GROUP BY 1",
            "properties": {"displayName": "部门汇总视图"},
        }
    ],
    "relationships": [
        {
            "name": "emp_dept",
            "models": ["workhour", "dept"],
            "joinType": "many_to_one",
            "condition": "workhour.emp_id = dept.emp_id",
            "properties": {"displayName": "员工所属部门"},
        }
    ],
    "cubes": [
        {
            "name": "workhour_cube",
            "baseObject": "workhour",
            "properties": {"displayName": "工时立方"},
            "measures": [
                {
                    "name": "total_hours",
                    "expression": "SUM(hours)",
                    "properties": {"displayName": "总工时"},
                }
            ],
            "dimensions": [{"name": "emp_dim", "properties": {"displayName": "员工维度"}}],
            "timeDimensions": [{"name": "month_td", "properties": {"displayName": "月份"}}],
        }
    ],
}


# 范例对夹具：SQL 只有一行，但**原文**要能被 `load_query_pairs` 逐字还原（含 tags 列表）
_EXAMPLE_MD = """---
nl: 查询每个员工的当月工时合计
sql: |
  SELECT emp_id, SUM(hours) AS total_hours
  FROM t_workhour
  GROUP BY emp_id
datasource: postgres
tags:
  - 工时
  - 报工
---
"""


def make_project(root: pathlib.Path, mdl: dict | None = MDL, tag: str = "live") -> pathlib.Path:
    (root / "target").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "rules").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "sql").mkdir(parents=True, exist_ok=True)
    (root / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
    if mdl is not None:
        payload = json.loads(json.dumps(mdl))
        if tag != "live":  # 备份目录塞一条独有表名，用来验「没被索引」
            payload["models"].append(
                {
                    "name": "BACKUP_ONLY_TABLE",
                    "properties": {"displayName": "只在备份里"},
                    "columns": [],
                }
            )
        (root / "target" / "mdl.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    (root / "knowledge" / "rules" / "口径规则.md").write_text(
        "## 工时口径\n\n工时以人力资源系统为准，加班工时单独统计。\n", encoding="utf-8"
    )
    (root / "knowledge" / "sql" / "工时示例.sql").write_text(
        "SELECT SUM(hours) FROM t_workhour;\n", encoding="utf-8"
    )
    # 范例对（P1-2）：`knowledge/sql/*.md` 有 nl/sql frontmatter ⇒ 一文件一条 example_sql
    (root / "knowledge" / "sql" / "工时范例.md").write_text(
        _EXAMPLE_MD, encoding="utf-8"
    )
    # 负对照：缺 nl 的 md 必须回落旧分支（knowledge_rule + 按 `##` 分块），不许静默丢文件
    (root / "knowledge" / "sql" / "缺字段.md").write_text(
        "## 第一节\n\n没有 frontmatter 的说明。\n\n## 第二节\n\n继续。\n", encoding="utf-8"
    )
    return root


class _FakeWM:
    """只提供 schema 需要的三个属性（真 manager 会顺带拉起别的东西）。"""

    def __init__(self, ws: pathlib.Path) -> None:
        self.active_workspace = ws
        self.db_config_path = ws / "db_config.json"
        self.semantic_dir = ws


def write_db_config(ws: pathlib.Path, proj: pathlib.Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "db_config.json").write_text(
        json.dumps(
            {"databases": [{"name": "demo", "db_type": "postgres",
                            "wren_project": str(proj), "status": "active"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _bag_of_chars(text: str) -> list[float]:
    """确定性「嵌入」：字符袋（同字符多 ⇒ 余弦高）。够用来验融合编排。"""
    dim = embedder.dim()
    vec = [0.0] * dim
    for ch in text:
        vec[ord(ch) % dim] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


# ══════════════════════════════════════════════════════════
# 通用语义（两个后端都跑）
# ══════════════════════════════════════════════════════════
def run_shared(backend_name: str, ws: pathlib.Path, proj: pathlib.Path,
               backup: pathlib.Path) -> None:  # noqa: C901
    import agent.workspace_manager as wm

    set_backend(backend_name)
    with mock.patch.object(S, "workspace_root", lambda: ws), mock.patch.object(
        wm, "get_workspace_manager", lambda: _FakeWM(ws)
    ):
        make_project(proj)
        make_project(backup, tag="backup")
        write_db_config(ws, proj)
        idx_dir = S.index_dir_for(proj)

        # ── ① 开关 ───────────────────────────────────────
        section(f"① 开关与值域 [{backend_name}]")
        set_mode(None)
        check(S.retrieval_mode() == S.MODE_OFF, "默认（不设 env）＝ off")
        check(not S.is_enabled() and not S.vector_enabled(), "off ⇒ 未启用、不用向量腿")
        check(search.search(proj, "工时") == [], "off ⇒ search 恒返回 []（今天的行为逐字不变）")
        check(S.retrieval_backend() == backend_name, "后端开关读到 env 值")
        set_mode("off")
        check(S.retrieval_mode() == S.MODE_OFF, "显式 off ⇒ off")
        set_mode("HYBRID")
        check(S.retrieval_mode() == S.MODE_HYBRID, "大写/空白容错 ⇒ hybrid")
        check(S.vector_enabled(), "hybrid ⇒ 用向量腿")
        set_mode("bogus")
        check(S.retrieval_mode() == S.MODE_OFF, "无法识别的值 ⇒ off（安全侧）")
        set_mode("fts")
        check(S.is_enabled() and not S.vector_enabled(), "fts ⇒ 启用但不用向量腿")

        # ── ② 语料抽取 ───────────────────────────────────
        section(f"② 语料抽取（kind 与中文 displayName）[{backend_name}]")
        items = indexer.collect_items(proj, "demo")
        by_kind: dict[str, list] = {}
        for it in items:
            by_kind.setdefault(it.kind, []).append(it)
        check(len(by_kind[S.KIND_SCHEMA_TABLE]) == 3, "3 张表", str(len(by_kind.get(S.KIND_SCHEMA_TABLE, []))))
        check(len(by_kind[S.KIND_SCHEMA_COLUMN]) == 4, "4 个列", str(len(by_kind.get(S.KIND_SCHEMA_COLUMN, []))))
        check(len(by_kind[S.KIND_MEASURE]) == 1, "1 个度量（来自 cube.measures）")
        check(len(by_kind[S.KIND_DIMENSION]) == 2, "2 个维度（cube_dimension + time_dimension 归一）")
        check(len(by_kind[S.KIND_CUBE]) == 1, "1 个 cube")
        check(len(by_kind[S.KIND_VIEW]) == 1, "1 个视图（独立 kind，不与 cube 混）")
        check(len(by_kind[S.KIND_RELATIONSHIP]) == 1, "1 条关系（join 路径可被检索）")
        # example_sql 从 P1-2 起有**两个**来源：老的 `*.sql` 与 `knowledge/sql/*.md` 范例对
        check(len(by_kind[S.KIND_EXAMPLE_SQL]) == 2,
              "2 条示例（1 条 .sql + 1 条 md 范例对）",
              str(len(by_kind.get(S.KIND_EXAMPLE_SQL, []))))
        check(bool(by_kind.get(S.KIND_KNOWLEDGE_RULE)), "知识文件按 rules 子目录归口")

        # ── ②b `knowledge/sql/*.md` 范例对（P1-2 新增语义，逐字对齐 wren）──
        from wren.memory.markdown import load_query_pairs

        pairs = {p["path"]: p for p in load_query_pairs(proj)}
        md_file = proj / "knowledge" / "sql" / "工时范例.md"
        # src_path 是 `f"knowledge/{rel}"`（rel 用平台原生分隔符）——照抄这个拼法，别自己归一
        md_src = f"knowledge/{pathlib.Path('sql') / md_file.name}"
        md_item = next((it for it in items if it.src_path == md_src), None)
        check(md_item is not None and md_item.kind == S.KIND_EXAMPLE_SQL,
              "md 范例对 ⇒ example_sql（不再被当 knowledge_rule 切块）", md_src)
        md_pair = pairs.get(str(md_file.relative_to(proj)))
        if md_item is not None and md_pair is not None:
            check(md_item.meta.get("nl") == md_pair["nl"] and md_item.meta.get("sql") == md_pair["sql"],
                  "meta 里的 nl/sql 与 wren load_query_pairs 逐字相等")
            check(md_item.meta.get("path") == md_pair["path"],
                  "meta.path 照抄 wren（平台原生分隔符，不砍 sql/ 段）", str(md_item.meta.get("path")))
            check([str(t) for t in md_item.meta.get("tags") or []] ==
                  [str(t) for t in md_pair.get("tags") or []],
                  "tags 与 wren 一致", str(md_item.meta.get("tags")))
            check(md_item.meta.get("chunk") == 0 and "#" not in md_item.src_path,
                  "一文件一条、不分块（分块会切碎 SQL）")
            check(md_item.title == md_pair["nl"][:120], "title 取 NL 问句（召回主信号）")
            check(md_pair["sql"] in md_item.text, "text 含 SQL 原文（供 FTS/向量腿）")
        else:
            check(False, "md 范例对与 load_query_pairs 对得上", "两边任一缺失")

        # 负对照：缺 nl 的 md 走旧分支（knowledge_rule + `##` 分块），不静默丢文件
        bad = [it for it in items if it.src_path.startswith("knowledge/sql") and "缺字段.md" in it.src_path]
        check(bad and all(it.kind == S.KIND_KNOWLEDGE_RULE for it in bad),
              "★ 负对照：缺 nl 的 md 仍按 knowledge_rule 归口", f"{len(bad)} 块")
        check(len(bad) > 1 and all("#" in it.src_path for it in bad),
              "★ 负对照：缺字段的 md 仍按 `##` 分块", str([it.src_path for it in bad]))
        hour_item = next(
            it for it in items if it.kind == S.KIND_SCHEMA_COLUMN and it.meta.get("name") == "hours"
        )
        check(hour_item.title == "工时" and "工时" in hour_item.text, "列的中文业务名进了 title/text")
        check("当月合计工时" in hour_item.text, "列说明（口径文字）进了 text")
        table_item = next(it for it in items if it.meta.get("name") == "workhour")
        check(table_item.title == "工时表" and "员工每月工时记录" in table_item.text, "表条目含中文业务名与说明")
        check("t_workhour" in table_item.text, "表条目补上了物理表名（wren 抽取器本身不含 tableReference）")
        measure_item = by_kind[S.KIND_MEASURE][0]
        check(measure_item.title == "总工时" and "SUM(hours)" in measure_item.text, "度量条目带中文名与表达式")
        rel_item = by_kind[S.KIND_RELATIONSHIP][0]
        check("workhour" in rel_item.text and "dept" in rel_item.text, "关系条目含左右表名")
        check(len({it.id for it in items}) == len(items), "id 无重复")
        check(all(S.project_rev(proj) in it.rev for it in items), "全部条目带 rev")

        # ── ③ 构建与幂等 ─────────────────────────────────
        section(f"③ 构建与幂等 [{backend_name}]")
        first = indexer.build_index(proj, "demo", with_vectors=False)
        check(first.get("ok") and not first.get("skipped"), "首次构建成功", first.get("reason", ""))
        check(first.get("backend") == backend_name, "meta 记下生效后端", str(first.get("backend")))
        check(backends.open_backend(idx_dir).ready(), "索引就绪（后端自报）", str(idx_dir))
        check(not str(idx_dir).startswith(str(proj)), "索引不在项目目录内（不污染 git 版本化的语义库）")
        second = indexer.build_index(proj, "demo", with_vectors=False)
        check(second.get("skipped") is True, "rev 未变 ⇒ 跳过重建")
        mdl_path = proj / "target" / "mdl.json"
        raw = json.loads(mdl_path.read_text(encoding="utf-8"))
        raw["views"].append({"name": "new_view", "properties": {"displayName": "新视图"}})
        mdl_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        third = indexer.build_index(proj, "demo", with_vectors=False)
        check(third.get("skipped") is False and third.get("items") == len(items) + 1, "mdl 变化 ⇒ rev 变 ⇒ 重建并多一条")
        check(third.get("rev") != first.get("rev"), "rev 确实变了")

        # ── ④ 两字查询 + 下划线标识符 ─────────────────────
        section(f"④ 两字查询与标识符回归闸 [{backend_name}]")
        hits = search.search(idx_dir, "工时")
        check(bool(hits), "两字查询「工时」有结果（引擎下限的回归闸）")
        check("工时" in json.dumps(hits, ensure_ascii=False), "结果里真的含「工时」")
        # 负对照必须用**生僻字**：`zzz不存在的词xyz` 这种写法里 `的/词` 是独立 token，
        # 而语料里有 `业务名/同义词: …` 这类生成文本 ⇒ 命中是真的（icu 按词切，不是误召回）。
        junk = search.search(idx_dir, "龘靐齉爩")
        check(not junk, "生僻词 ⇒ 空（打分器不是恒返回东西）", str([h["title"] for h in junk][:3]))
        junk_ascii = search.search(idx_dir, "qqqzzzwww")
        check(not junk_ascii, "ASCII 垃圾串 ⇒ 空", str([h["title"] for h in junk_ascii][:3]))
        check(search.search(idx_dir, "") == [], "空查询 ⇒ 空")
        check(bool(search.search(idx_dir, "部门")), "两字查询「部门」有结果")
        # ⚠️ 下划线陷阱：物理表名 customer_t 必须被「customer」命中（两侧都要绕，见模块 docstring）
        cust = search.search(idx_dir, "customer")
        check(bool(cust), "查 customer 能命中 customer_t（下划线必须当分隔符）", str([h["title"] for h in cust[:3]]))
        kind_hits = search.search(idx_dir, "工时", kinds={S.KIND_SCHEMA_COLUMN})
        check(
            bool(kind_hits) and all(h["kind"] == S.KIND_SCHEMA_COLUMN for h in kind_hits),
            "kinds 过滤生效",
            str([h["kind"] for h in kind_hits][:3]),
        )
        check(
            all(h["db_name"] == "demo" for h in search.search(idx_dir, "工时", db_name="demo")),
            "db_name 过滤生效",
        )
        check(search.search(idx_dir, "工时", db_name="不存在的库") == [], "负对照：db_name 不匹配 ⇒ 空")

        # ── ⑤ 索引缺失 / 损坏 ────────────────────────────
        section(f"⑤ 索引缺失与损坏（永不抛）[{backend_name}]")
        ghost = S.index_dir_for(ws / "no_such_project")
        check(search.search(ghost, "工时") == [], "索引不存在 ⇒ []")
        check(backends.open_backend(ghost).ready() is False, "后端自报未就绪")
        check(bool(search.search(idx_dir, "工时")), "负对照：同一 query 在索引存在时有结果")

        # ── ⑥ 嵌入通道死掉（§8.1 的 L1）──────────────────
        section(f"⑥ 嵌入通道不可达 ⇒ 纯 FTS 索引（L1）[{backend_name}]")
        set_mode("hybrid")
        set_base_url("http://127.0.0.1:9/v1")  # 必然 connection refused
        check(embedder.embed_texts(["工时"]) is None, "通道不可达 ⇒ embed_texts 返回 None（不抛）")
        check(embedder.health(timeout=1)["ok"] is False, "health 报 not ok")
        dead = indexer.build_index(proj, "demo", force=True)
        check(dead.get("ok") and dead.get("vectors") == 0, "hybrid 下构建仍成功，向量 0 条")
        check(bool(search.search(idx_dir, "工时")), "纯 FTS 索引照样能查（质量损失、可用性不损失）")
        check(
            all(h["legs"] == ["fts"] for h in search.search(idx_dir, "工时")),
            "查询期也只用了 FTS 腿（不花嵌入超时：查询期 4s / 索引期 2.5s）",
        )

        # ── ⑥-b 嵌入响应校验：模型名闸（同维数换模型是静默错乱）────
        section(f"⑥-b 嵌入响应校验：模型名 / 维数 / 条数 [{backend_name}]")
        want = embedder.model_name()
        good_vec = [0.1] * embedder.dim()

        def _resp(model, vec=good_vec, n=1):
            """伪造一次 embedding 响应（替代 `_post`，不碰网络）。"""
            return {
                "model": model,
                "data": [{"index": i, "embedding": list(vec)} for i in range(n)],
            }

        def _with_post(payload, texts=("工时",)):
            with mock.patch.object(embedder, "_post", lambda *a, **kw: payload):
                return embedder.embed_texts(list(texts))

        check(_with_post(_resp(want)) is not None, "正对照：模型名一致 ⇒ 正常返回向量")
        check(
            _with_post(_resp(f"{want}-另一个")) is None,
            "**同维数换模型 ⇒ None**（修复前这里会返回 16 维向量，与旧索引静默混用）",
            f"期望 {want!r}",
        )
        check(
            _with_post(_resp(f"BAAI/{want}")) is not None,
            "负对照：路径式全名 BAAI/<model> 归一后一致 ⇒ 放行（不误杀）",
        )
        check(
            _with_post(_resp(want.upper())) is not None,
            "负对照：大小写不同 ⇒ 放行（归一含小写）",
        )
        check(
            _with_post({"data": [{"index": 0, "embedding": list(good_vec)}]}) is not None,
            "负对照：服务端**不回** model 字段 ⇒ 放行（只留维数闸，不误杀正常通道）",
        )
        check(
            _with_post(_resp(12345)) is not None,
            "负对照：model 非字符串 ⇒ 当「没报告」放行（判不出不误杀）",
        )
        check(
            _with_post(_resp(want, vec=[0.1] * (embedder.dim() - 1))) is None,
            "负对照：模型名对但**维数不符** ⇒ None（两道闸各自独立）",
        )
        check(
            _with_post(_resp(want, n=2)) is None,
            "负对照：条数与入参不符 ⇒ None（模型名闸不掩盖原有闸）",
        )

        # ── ⑥-c 索引 meta 与嵌入配置不同源（响应闸抓不到的那条路）────
        # 「配置改了、索引没重建」：服务端报告的正是**新**名字 ⇒ 响应闸放行，
        # 只能靠 meta 里记的建索引那一刻的真相判。
        section(f"⑥-c 索引 meta vs 当前嵌入配置（同维数混用）[{backend_name}]")
        check(embedder.same_model("bge-m3", "BAAI/BGE-M3") is True, "same_model 归一：路径/大小写")
        check(embedder.same_model("bge-m3", "bge-m3-v2") is False, "same_model 不模糊匹配")
        check(embedder.same_model("bge-m3", "") is False, "same_model：空 ⇒ False（判不出不放行）")

        # 真场景：**带向量**建索引 → 只改配置（不重建）⇒ 必须只走 FTS。
        # （不去手改 meta —— lance 的 meta 有「版本提示」缓存，改 meta 不改数据集版本
        #   时后端读到的仍是旧 meta，「陈旧缓存闸」会先接管，测不到本闸。）
        env_model_key = "NL2SQL_EMBED_MODEL"
        before_env = os.environ.get(env_model_key)
        want = embedder.model_name()
        ec_proj = make_project(ws / "embedcfg_wrenai")
        ec_dir = S.index_dir_for(ec_proj)
        fake_embed = lambda texts, **kw: [_bag_of_chars(t) for t in texts]  # noqa: E731
        with mock.patch.object(embedder, "embed_texts", fake_embed):
            ec_build = indexer.build_index(ec_proj, "embedcfg", force=True)
        ec_meta = S.read_meta(ec_dir)
        check(
            ec_meta.get("embed_model") == want and ec_meta.get("vectors") == ec_meta.get("items") > 0,
            "夹具：索引带向量且 meta 记了建索引时的 embed_model",
            f"model={ec_meta.get('embed_model')!r} vectors={ec_meta.get('vectors')}/{ec_meta.get('items')}",
        )

        calls: list[str] = []
        spy = lambda text, **kw: calls.append(text) or [0.5] * embedder.dim()  # noqa: E731
        try:
            check(search._embed_cfg_matches(ec_dir) is True, "正对照：meta 与当前配置同源 ⇒ 放行")
            with mock.patch.object(embedder, "embed_one", spy):
                same_hits = search.search(ec_dir, "工时")
            check(
                bool(calls) and any("vector" in h["legs"] for h in same_hits),
                "正对照：同源 ⇒ 向量腿真的跑了（嵌入被调用、结果带 vector 腿）",
                f"legs={[h['legs'] for h in same_hits][:2]}",
            )

            os.environ[env_model_key] = f"{want}-另一个"   # 模拟「换配置没重建索引」
            calls.clear()
            with mock.patch.object(embedder, "embed_one", spy):
                check(
                    search._embed_cfg_matches(ec_dir) is False,
                    "**配置改了没重建索引 ⇒ 拒绝向量腿**（修复前放行 ⇒ 同维数静默混用）",
                )
                mixed_hits = search.search(ec_dir, "工时")
                check(not calls, "不同源 ⇒ 连嵌入调用都没发生（向量腿进都没进）")
                check(
                    bool(mixed_hits) and all(h["legs"] == ["fts"] for h in mixed_hits),
                    "不整体拒绝索引：FTS 腿照常召回（§8.1 的 L1，只掉质量）",
                    f"{len(mixed_hits)} 条",
                )

            os.environ[env_model_key] = want
            S.write_meta(ec_dir, {**ec_meta, "embed_dim": embedder.dim() + 8})
            check(
                search._embed_cfg_matches(ec_dir) is False,
                "负对照：模型名同但 meta.embed_dim 不符 ⇒ 也拒绝",
            )
            S.write_meta(ec_dir, {**ec_meta, "embed_model": ""})
            check(
                search._embed_cfg_matches(ec_dir) is True,
                "负对照：`--no-vectors` 索引（embed_model 空）⇒ 放行（由 vectors==0 短路接管）",
            )
            S.write_meta(ec_dir, {**ec_meta, "embed_dim": "不是数字"})
            check(
                search._embed_cfg_matches(ec_dir) is True,
                "负对照：meta.embed_dim 被改坏 ⇒ 当「没记」放行（读侧永不抛）",
            )
        finally:
            S.write_meta(ec_dir, ec_meta)
            if before_env is None:
                os.environ.pop(env_model_key, None)
            else:
                os.environ[env_model_key] = before_env
        check(os.environ.get(env_model_key) == before_env, "NL2SQL_EMBED_MODEL 已复原")

        # ── ⑥-d 重试与查询期熔断（2026-09-29 生产实证的离散停顿）────
        # 生产：正常 73ms 的通道偶发停顿 ≥2.5s ⇒ 单次 timed out ⇒ 那一次检索降级纯 FTS。
        # 形状是「重试能救」的，所以加一次重试；再用**查询期熔断**兜住「通道整个消失」时
        # 重试带来的额外开销（否则每次检索 4s×2 > 原来 2.5s×1）。
        section(f"⑥-d 重试与查询期熔断 [{backend_name}]")

        class _Resp:
            """最小可用响应：满足 `with ... as resp:` + `resp.read()`。"""

            def __init__(self, payload: dict) -> None:
                self._body = json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self) -> bytes:
                return self._body

        ok_payload = _resp(embedder.model_name())

        def _transport(plan: list):
            """按 plan 依次出结果；元素是 Exception ⇒ 抛，是 dict ⇒ 当成功响应。返回计数器。"""
            calls = {"n": 0}

            def fake(req, timeout):
                i = calls["n"]
                calls["n"] += 1
                step = plan[i] if i < len(plan) else plan[-1]
                if isinstance(step, Exception):
                    raise step
                return _Resp(step)

            return fake, calls

        def _with_transport(plan: list, fn):
            fake, calls = _transport(plan)
            before = {k: os.environ.get(k) for k in
                      ("NL2SQL_EMBED_RETRIES", "NL2SQL_EMBED_COOLDOWN", "NL2SQL_EMBED_QUERY_TIMEOUT")}
            os.environ["NL2SQL_EMBED_COOLDOWN"] = "0"   # 本节单独验熔断时再打开
            embedder._reset_breaker_for_test()
            try:
                with mock.patch.object(embedder, "_open", fake):
                    return fn(), calls, before
            finally:
                for k, v in before.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                embedder._reset_breaker_for_test()

        to = lambda: TimeoutError("timed out")  # noqa: E731

        class _cooldown_env:
            """临时把 `NL2SQL_EMBED_COOLDOWN` 设成 value，退出时**还原**（不是 pop）。

            为什么强调「还原」：套件级那个 `= 0`（文件开头）一旦被 pop 掉，本节之后**所有**
            段的熔断就悄悄变成默认 60s ⇒ 某一节的一次失败会开一个跨段的窗口，把后面用例
            的向量腿变成静默空腿（**假通过**）。本类自带 `_reset_breaker_for_test()`，
            所以每段不留窗口、也不改坏套件级默认值。
            """

            def __init__(self, value: str) -> None:
                self._value = value

            def __enter__(self):
                self._before = os.environ.get("NL2SQL_EMBED_COOLDOWN")
                os.environ["NL2SQL_EMBED_COOLDOWN"] = self._value
                embedder._reset_breaker_for_test()
                return self

            def __exit__(self, *a):
                if self._before is None:
                    os.environ.pop("NL2SQL_EMBED_COOLDOWN", None)
                else:
                    os.environ["NL2SQL_EMBED_COOLDOWN"] = self._before
                embedder._reset_breaker_for_test()
                return False

        check(embedder.query_timeout_s() == 4.0 and embedder.timeout_s() == 1.0,
              "值域：查询期默认 4s、索引期单批仍是 env 里的 1s（两条独立预算）",
              f"query={embedder.query_timeout_s()} batch={embedder.timeout_s()}")
        res, calls, _ = _with_transport([to(), ok_payload], lambda: embedder.embed_one("工时"))
        check(res is not None and calls["n"] == 2,
              "首次超时 + 第二次成功 ⇒ **重试救回向量**（生产那两次的形状）", f"尝试 {calls['n']} 次")
        res, calls, _ = _with_transport([to()], lambda: embedder.embed_one("工时"))
        check(res is None and calls["n"] == 2, "一直超时 ⇒ None，且尝试次数 = 1+retries（不是无限）",
              f"尝试 {calls['n']} 次")
        os.environ["NL2SQL_EMBED_RETRIES"] = "0"
        try:
            res, calls, _ = _with_transport([to()], lambda: embedder.embed_one("工时"))
            check(res is None and calls["n"] == 1,
                  "负对照：NL2SQL_EMBED_RETRIES=0 ⇒ 只试 1 次（能关掉）", f"尝试 {calls['n']} 次")
        finally:
            os.environ.pop("NL2SQL_EMBED_RETRIES", None)
        err400 = urllib.error.HTTPError("u", 400, "bad model", {}, None)
        res, calls, _ = _with_transport([err400], lambda: embedder.embed_one("工时"))
        check(res is None and calls["n"] == 1,
              "负对照：HTTP 400（模型名/路径写错）⇒ **不重试**（重试一万次也一样）",
              f"尝试 {calls['n']} 次")
        err503 = urllib.error.HTTPError("u", 503, "model is loading", {}, None)
        res, calls, _ = _with_transport([err503, ok_payload], lambda: embedder.embed_one("工时"))
        check(res is not None and calls["n"] == 2, "HTTP 503（Ollama 载模型中）⇒ 重试", f"尝试 {calls['n']} 次")

        # 熔断：一次失败后窗口内**零等待**，且不再发请求
        with _cooldown_env("30"):
            fake, calls = _transport([to()])
            with mock.patch.object(embedder, "_open", fake):
                first = embedder.embed_one("工时")
                n_after_first = calls["n"]
                second = embedder.embed_one("工时")
                third = embedder.embed_one("工时")
            check(first is None and n_after_first == 2 and calls["n"] == 2
                  and second is None and third is None,
                  "熔断：一次失败 ⇒ 冷却窗口内直接 None，**不再发请求**（通道消失时不再每次白烧 8s）",
                  f"请求数 {calls['n']}")
            check(embedder.breaker_open() is True, "熔断状态对外可观测（日志/排查用）")
            embedder._reset_breaker_for_test()
            check(embedder.breaker_open() is False, "reset 后窗口关闭（测试钩子）")
            res, calls, _ = _with_transport([ok_payload], lambda: embedder.embed_one("工时"))
            check(res is not None, "reset 后恢复：下一次成功调用正常返回", f"尝试 {calls['n']} 次")

        # ⚠️ 熔断只管查询期：索引期失败一次不许让整批向量静默丢失
        with _cooldown_env("30"):
            fake, calls = _transport([to(), to(), _resp(embedder.model_name(), n=2)])
            with mock.patch.object(embedder, "_open", fake):
                embedder.embed_one("工时")                     # 2 次都超时 ⇒ 打开熔断
                opened = embedder.breaker_open()
                batch = embedder.embed_texts(["工时", "报工"])  # 索引期必须照试不误
            check(opened and batch is not None and calls["n"] == 3,
                  "负对照：熔断不管索引期 —— 查询期刚失败（熔断已开），embed_texts 仍照试并成功",
                  f"请求数 {calls['n']}")

        # 熔断的**可观测性**：窗口本身是静默的（不发请求 ⇒ 没有失败日志），必须自己说出来。
        # 生产日志级别到 INFO ⇒ 开启要 WARNING、跳过要 INFO；两者任一掉到 DEBUG，日志里就
        # 只剩「一条失败 + 60 秒静默 + 又一条失败」，查不出中间几次检索为什么没向量腿。

        class _Cap(logging.Handler):
            """只收集、不输出（免得把日志刷进验收输出）。"""

            def __init__(self) -> None:
                super().__init__(level=logging.DEBUG)
                self.records: list[logging.LogRecord] = []

            def emit(self, record: logging.LogRecord) -> None:
                self.records.append(record)

        cap = _Cap()
        elog = logging.getLogger("agent.retrieval.embedder")
        level_before = elog.level
        elog.setLevel(logging.DEBUG)   # 让 DEBUG 也进 cap，才能证明「没掉到 DEBUG」
        elog.addHandler(cap)
        try:
            with _cooldown_env("30"):
                fake, calls = _transport([to()])
                with mock.patch.object(embedder, "_open", fake):
                    embedder.embed_one("工时")   # 两次超时 ⇒ 开熔断
                    embedder.embed_one("工时")   # 窗口内 ⇒ 跳过
        finally:
            elog.removeHandler(cap)
            elog.setLevel(level_before)

        opened = [r for r in cap.records if "熔断开启" in r.getMessage()]
        check(bool(opened) and opened[0].levelno >= logging.WARNING,
              "熔断**开启**打 WARNING（状态变化，运维该看见；含冷却秒数）",
              f"{len(opened)} 条 / {opened[0].levelname if opened else '-'}")
        skipped = [r for r in cap.records if "冷却中" in r.getMessage()]
        check(bool(skipped) and skipped[0].levelno == logging.INFO,
              "负对照：窗口内**跳过**打 INFO（不是 DEBUG）⇒ 生产日志里能看见降级原因",
              f"{len(skipped)} 条 / {skipped[0].levelname if skipped else '无'}")
        check(embedder.cooldown_s() == 0.0 and embedder.breaker_open() is False,
              "本节结束后套件级「熔断默认关」安然无恙（env 只许还原、不许 pop，否则窗口跨段污染）",
              f"cooldown={embedder.cooldown_s()}s open={embedder.breaker_open()}")

        # **成功也必须留痕**：否则「向量可用」与「压根没调用/形状闸没过」在日志里长得一样。
        # 判据分两半：① 成功 ⇒ 一条 INFO「向量可用」（含尝试次数与耗时）；② 负对照 ——
        # 形状闸没过（维数不符）⇒ **不许**打「向量可用」（否则「成功」被高估成 HTTP 200）。
        cap2 = _Cap()
        elog.addHandler(cap2)
        # ⚠️ 必须显式抬到 DEBUG/INFO：`Logger.setLevel(NOTSET)` 会**继承 root 的 WARNING**,
        # INFO 记录直接在这一层被丢掉、根本到不了 handler（这条踩过一次）。
        elog.setLevel(logging.DEBUG)
        try:
            with _cooldown_env("0"):
                embed_once, _, _ = _with_transport([ok_payload], lambda: embedder.embed_one("工时"))
                rescued, _, _ = _with_transport([to(), ok_payload], lambda: embedder.embed_one("工时"))
                bad = _resp(embedder.model_name(), vec=[0.0] * 8)   # HTTP 通、维数闸不过
                embed_bad, _, _ = _with_transport([bad], lambda: embedder.embed_one("工时"))
        finally:
            elog.removeHandler(cap2)
            elog.setLevel(level_before)

        # 发版后评估「加的重试有没有用」就靠这一条：成功行里写 `第 2/2 次尝试`
        rescued_line = [r.getMessage() for r in cap2.records if "第 2/2 次尝试" in r.getMessage()]
        check(rescued is not None and bool(rescued_line),
              "**重试救回在日志里可判**：成功行写「第 2/2 次尝试」（不是只看失败 WARNING）",
              rescued_line[0].replace("[embedder] ", "") if rescued_line else "无")

        ok_lines = [r for r in cap2.records if "向量可用" in r.getMessage()]
        check(embed_once is not None and len(ok_lines) == 2 and ok_lines[0].levelno == logging.INFO,
              "成功打一条 INFO「向量可用」（带 维数/尝试次数/耗时；4 次调用里 2 次成功 ⇒ 恰 2 条）",
              ok_lines[0].getMessage() if ok_lines else "无")
        check(embed_bad is None and len(ok_lines) == 2,
              "负对照：维数闸不过（HTTP 200 但 8 维）⇒ **不打**「向量可用」，只有「维数不符」",
              f"向量可用 {len(ok_lines)} 条 / 降级 {[r.getMessage()[:28] for r in cap2.records if '降级' in r.getMessage()]}")

        # ── ⑦ 空 mdl 拒绝构建 ────────────────────────────
        section(f"⑦ 空 mdl 拒绝构建 [{backend_name}]")
        empty = make_project(ws / "empty_wrenai", mdl=None)
        result = indexer.build_index(empty, "empty", with_vectors=False)
        check(result.get("ok") is False and "mdl" in result.get("reason", ""), "无 mdl.json ⇒ 拒绝", result.get("reason", ""))
        check(not backends.open_backend(S.index_dir_for(empty)).ready(), "拒绝时不落盘（不留零条目假索引）")
        (empty / "target" / "mdl.json").write_text(
            json.dumps({"models": [{"name": "t1", "properties": {"displayName": "表一"}}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        check(indexer.build_index(empty, "empty", with_vectors=False).get("ok") is True, "负对照：补上 mdl ⇒ 构建成功")

        # ── ⑧ 注册表为唯一输入（R8）──────────────────────
        section(f"⑧ 注册表为唯一输入（备份目录不进索引）[{backend_name}]")
        entries = S.registered_projects()
        check(len(entries) == 1 and entries[0]["db_name"] == "demo", "注册表只认 1 个库", str(len(entries)))
        check(str(entries[0]["project"]) == str(proj), "项目路径取自 databases[].wren_project（不是按名字拼）")
        built = indexer.build_all(with_vectors=False)
        check(len(built) == 1, "build_all 只覆盖注册表里的库", str([b.get("db_name") for b in built]))
        check(not backends.open_backend(S.index_dir_for(backup)).ready(), "备份目录没有被建索引")
        # 断的是「备份的表名没进索引」，不是「该查询必须零命中」：字符 n-gram 腿对
        # 不存在标识符会有极低分的偶然重叠（召回腿的通病，RRF 里靠名次稀释），
        # 所以判据取「返回条目的正文里不许出现这个名字」。
        leaked = [
            h for h in search.search(idx_dir, "BACKUP_ONLY_TABLE", limit=20)
            if "BACKUP_ONLY_TABLE" in (h["title"] + h["text"])
        ]
        check(not leaked, "备份独有的表名没混进活库索引", str([h["title"] for h in leaked][:3]))

        # ── ⑨ RRF 融合 ───────────────────────────────────
        section(f"⑨ RRF 融合（两腿）[{backend_name}]")
        set_mode("hybrid")
        with mock.patch.object(embedder, "embed_texts", lambda texts, **kw: [_bag_of_chars(t) for t in texts]):
            fused_build = indexer.build_index(proj, "demo", force=True, with_vectors=True)
        check(fused_build.get("vectors") == fused_build.get("items"), "假嵌入下向量条数＝条目数")
        with mock.patch.object(
            embedder, "embed_texts", lambda texts, **kw: [_bag_of_chars(t) for t in texts]
        ), mock.patch.object(embedder, "embed_one", lambda text, **kw: _bag_of_chars(text)):
            fused = search.search(idx_dir, "工时表")
            set_mode("fts")
            fts_only = search.search(idx_dir, "工时表")
            set_mode("hybrid")
        check(bool(fused) and any(len(h["legs"]) > 1 for h in fused), "有结果同时被两腿命中", str([h["legs"] for h in fused[:3]]))
        check(all(h["legs"] == ["fts"] for h in fts_only), "负对照：只开 FTS 腿时 legs 全是 fts")
        check(len(fused) <= 8 and len(fts_only) <= 8, "默认 limit=8")
        check(
            bool(set(h["id"] for h in fused) & set(h["id"] for h in fts_only)),
            "两腿结果有交集（同一个语料库）",
        )
        check(fused[0]["score"] > 0, "融合分数为正")
        check(all(h["score"] > 0 for h in fused), "无零分条目混入")

        # ── ⑩ 结果形状与截断 ─────────────────────────────
        section(f"⑩ 结果形状、截断与文本上限 [{backend_name}]")
        long_hit = search.search(idx_dir, "工时", text_cap=10)
        check(bool(long_hit) and all(len(h["text"]) <= 11 for h in long_hit), "text_cap 生效（含省略号）")
        check(
            set(long_hit[0].keys()) >= {"id", "kind", "title", "text", "db_name", "src_path", "score", "meta", "legs"},
            "返回字段与契约一致（后端私有列不外泄）",
            str(sorted(long_hit[0].keys())),
        )
        check(
            not ({"text_idx", "vector", "meta_json"} & set(long_hit[0].keys())),
            "后端私有列（text_idx/vector/meta_json）不出现在结果里",
        )
        stats = backends.open_backend(idx_dir).stats()
        check(stats["ready"] and stats["items"] > 0 and stats["bytes"] > 0, "stats 可读",
              json.dumps({k: stats[k] for k in ("items", "vectors", "bytes")}, ensure_ascii=False))
        check(stats["rev"] == S.project_rev(proj), "stats.rev 与当前 mdl 一致")
        check(not (ws / S.INDEX_DIR_NAME / "wren_project.yml").exists(), "索引目录不会被认成语义库")


# ══════════════════════════════════════════════════════════
# 后端专属
# ══════════════════════════════════════════════════════════
def run_jsonl_only(ws: pathlib.Path, proj: pathlib.Path) -> None:
    import agent.workspace_manager as wm

    set_backend("jsonl")
    with mock.patch.object(S, "workspace_root", lambda: ws), mock.patch.object(
        wm, "get_workspace_manager", lambda: _FakeWM(ws)
    ):
        idx_dir = S.index_dir_for(proj)
        set_mode("hybrid")

        section("⑪-jsonl 打分器下限与缓存分桶")
        check(
            jsonl_be._ngrams("工时") == {"工时"},
            "两字词产出的正是二元组（下限取 2 ⇒ 两字词能进 idf 排序，不靠扁平子串加成）",
            str(sorted(jsonl_be._ngrams("工时"))),
        )
        check(len(jsonl_be._ngrams("月度工时明细")) >= 3, "长词产出二/三/…元组")
        tokens = jsonl_be._TOKEN_RE.findall("customer_t")
        check(tokens == ["customer", "t"], "ASCII 标识符按 `_` 切开（customer_t → customer + t）", str(tokens))
        check({"cu", "us", "st"} <= jsonl_be._ngrams("customer_t"), "切出的 token 真的进了 n-gram 倒排")
        rows_a, _ = store.load(idx_dir)
        rows_b, _ = store.load(idx_dir)
        check(rows_a is rows_b, "同一路径二次 load 命中缓存（同一对象）")
        check(store.ready(idx_dir) and store.items_path(idx_dir).is_file(), "jsonl 落的是 items.jsonl")
        check(not (idx_dir / S.TABLE_NAME).is_dir(), "jsonl 后端不留 lance 目录")

        section("⑪-jsonl 稀疏向量与增量")
        with mock.patch.object(embedder, "embed_texts", lambda texts, **kw: [_bag_of_chars(t) for t in texts]):
            indexer.build_index(proj, "demo", force=True, with_vectors=True)
        check(store.vectors_path(idx_dir).exists(), "vectors.bin 落盘")
        rows_now, _ = store.load(idx_dir)
        partial: list[list[float] | None] = [None] * len(rows_now)
        partial[0] = [0.5] * embedder.dim()
        store.save(idx_dir, rows_now, partial)
        rows_back, vecs_back = store.load(idx_dir)
        check(len(rows_back) == len(rows_now), "稀疏向量：条目数不变")
        check(vecs_back is not None and len(vecs_back) == len(rows_now), "稀疏向量：长度与条目对齐")
        check(
            vecs_back is not None and vecs_back[0] is not None and all(v is None for v in vecs_back[1:]),
            "稀疏向量：空洞位置保留为 None（不是被压紧）",
        )
        # 增量：upsert 不许把已有向量抹掉（只嵌了新条目）
        with mock.patch.object(embedder, "embed_texts", lambda texts, **kw: [_bag_of_chars(t) for t in texts]):
            indexer.build_index(proj, "demo", force=True, with_vectors=True)
        _rows, before_vecs = store.load(idx_dir)
        have_before = sum(1 for v in before_vecs or [] if v)
        fresh = indexer.collect_items(proj, "demo")[:1]
        new_item = S.Item(id="brand-new-id", kind=S.KIND_EXAMPLE_SQL, text="全新的一条例外 SQL",
                          title="全新", db_name="demo", src_path="knowledge/sql/new.sql",
                          rev=S.project_rev(proj), meta={})
        added = indexer.upsert_items(proj, "demo", [new_item])
        _rows2, after_vecs = store.load(idx_dir)
        have_after = sum(1 for v in after_vecs or [] if v)
        check(added.get("ok") is True, "jsonl 增量写成功", json.dumps(added, ensure_ascii=False)[:80])
        check(have_after == have_before, "增量后**已有向量条数不变**（只嵌新条目不许抹掉旧的）",
              f"{have_before} → {have_after}")
        check(bool(search.search(idx_dir, "例外 SQL")), "增量写入的新条目立刻可搜")


def run_lance_only(ws: pathlib.Path, proj: pathlib.Path) -> None:  # noqa: C901
    import agent.workspace_manager as wm

    set_backend("lance")
    with mock.patch.object(S, "workspace_root", lambda: ws), mock.patch.object(
        wm, "get_workspace_manager", lambda: _FakeWM(ws)
    ):
        idx_dir = S.index_dir_for(proj)
        set_mode("hybrid")

        section("⑪-lance 引擎面：icu 分词 / 下划线 / 缓存失效")
        check(S.table_dir(idx_dir).is_dir(), "落的是 items.lance 目录（目录名由 lancedb 按表名生成）")
        check(not store.items_path(idx_dir).is_file(), "lance 后端不留 items.jsonl（同一目录只一份索引）")
        from agent.retrieval.backends import lance as lance_be  # noqa: E402

        idx_text = lance_be._index_text("客户表", "客户表 (customer_t)：客户主数据。")
        check("customer t" in idx_text, "索引列做 `_`→空格 归一化（让 customer 命中）")
        check("customer_t" in idx_text, "索引列同时追加原始标识符（让 customer_t 精确命中）")
        check(lance_be._quote("o'brien") == "'o''brien'", "SQL 字面量转义（单引号双写）")

        with mock.patch.object(embedder, "embed_texts", lambda texts, **kw: [_bag_of_chars(t) for t in texts]):
            indexer.build_index(proj, "demo", force=True, with_vectors=True)
        # 故意在换语料**之前**拿住后端对象：陈旧 Table 陷阱的回归闸（旧对象会一直返回旧数据）
        stale = backends.open_backend(idx_dir)
        check(bool(stale.fts("工时", limit=5)), "有向量构建后可搜")

        section("⑪-lance 增量、稀疏向量与索引缺失")
        sparse_item = S.Item(id="lance-new", kind=S.KIND_EXAMPLE_SQL,
                             text="lance 增量独有短语 ZZINCR", title="增量",
                             db_name="demo", src_path="knowledge/sql/new.sql",
                             rev=S.project_rev(proj), meta={}, vector=[0.5] * embedder.dim())
        added = indexer.upsert_items(proj, "demo", [sparse_item])
        check(added.get("ok") is True, "lance 增量（merge_insert on id）成功", json.dumps(added, ensure_ascii=False)[:90])
        check(bool(search.search(idx_dir, "增量独有短语")), "**新增行被既有 FTS 索引自动覆盖**（立刻可搜）")
        check(bool(search.search(idx_dir, "工时")), "增量后旧语料仍可搜")
        with mock.patch.object(embedder, "embed_one", lambda text, **kw: _bag_of_chars(text)):
            hyb = search.search(idx_dir, "工时表")
        check(any("vector" in h["legs"] for h in hyb), "有向量的条目两腿都参与", str([h["legs"] for h in hyb[:3]]))
        check(
            not any(sparse_item.id == h["id"] for h in hyb if "vector" in h["legs"]),
            "向量腿不返回向量为 null 的行（where vector IS NOT NULL 生效）",
        )

        section("⑪-lance 分数列必须显式 select（不靠自动投影）")
        # 2026-09-29 修：lancedb 0.37.1 仍会**自动投影** `_score`/`_distance`（每次查询打
        # Deprecation warning），官方明说将来不再投影 ⇒ 不显式列进 select，两个 `r.get(...)`
        # 会拿到 None ⇒ 上报分数**静默全 0**（召回与排序不受影响，所以更难发现）。
        raw = backends.open_backend(idx_dir)
        raw_fts = raw.fts("工时", limit=3)
        check(bool(raw_fts) and all("_score" in r["row"] for r in raw_fts),
              "FTS 腿原始行含 `_score`（显式 select 生效）")
        with mock.patch.object(embedder, "embed_one", lambda text, **kw: _bag_of_chars(text)):
            raw_vec = raw.vector("工时", limit=3)
        check(bool(raw_vec) and all("_distance" in r["row"] for r in raw_vec),
              "向量腿原始行含 `_distance`（同上）")
        set_mode("fts")
        only_fts = search.search(idx_dir, "工时", limit=3)
        check(bool(only_fts) and all(h["score"] != 0 for h in only_fts),
              "单腿 fts 模式下上报 score 非零（分数被真读到，而不是 0 兜底）",
              str([h["score"] for h in only_fts[:3]]))
        set_mode("hybrid")

        section("⑪-lance 分词器事实（换 tokenizer/换配置就会漂）")
        import lancedb  # noqa: E402
        import pyarrow as pa  # noqa: E402
        from lancedb.index import FTS  # noqa: E402

        trap_dir = ws / "trap_lance"
        trap_dir.mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(str(trap_dir))
        cn = ["本月工时分析 报表", "报工工时统计", "部门月度汇总", "客户(customer_t)主数据"]

        def _hits(tokenizer: str, query: str, _n: list = [0], **kw) -> int:
            _n[0] += 1
            table = db.create_table(f"t{_n[0]}", schema=pa.schema([pa.field("text", pa.string())]),
                                    data=[{"text": x} for x in cn], mode="overwrite")
            table.create_index("text", config=FTS(stem=False, remove_stop_words=False,
                                                 ascii_folding=False, base_tokenizer=tokenizer, **kw))
            return len(table.search(query, query_type="fts").limit(5).to_list())

        # 前两条是 `_index_text` 存在的**理由**：删掉归一化列，`customer` 立刻退回零命中
        check(_hits("icu", "customer") == 0, "icu 不把 `_` 当分隔符：原文里 `customer` 命中不了 `customer_t`")
        check(_hits("icu", "customer_t") > 0, "icu 对完整标识符 `customer_t` 是命中的")
        check(_hits("icu", "工时") > 0, "icu 两字中文词命中（生产配置就是它）")
        # ⚠️ 曾经的错判在此钉死：`ngram_min_length` 的 3 是**配置默认值**，不是引擎下限。
        # （旧结论「ngram 对中文静默零命中」来自废弃 API `create_fts_index(tokenizer_name=)`
        #  的默认 min=3 —— 两字查询产不出 ≥3 的 gram，于是恒空且不报错。）
        check(_hits("ngram", "工时", ngram_min_length=3, ngram_max_length=3) == 0,
              "ngram(min=3) 两字中文零命中 —— 「3 字符下限」的真身是配置默认值")
        check(_hits("ngram", "工时", ngram_min_length=2, ngram_max_length=6) > 0,
              "ngram(min=2) 两字中文命中 ⇒ 引擎侧并无 3 字符硬下限")
        check(_hits("ngram", "customer", ngram_min_length=2, ngram_max_length=6) > 0,
              "ngram 靠字符重叠反而能命中 `customer_t`（它不需要归一化列；我们仍选 icu 求精度）")

        # ── 缓存失效（放最后：它要覆盖掉 idx_dir 里的语料）──────
        # 同名项目 ⇒ 同一个索引目录（index_dir_for 只取项目目录名）⇒ 真正复现 overwrite
        swapped = ws / "swap" / proj.name
        (swapped / "target").mkdir(parents=True, exist_ok=True)
        (swapped / "knowledge" / "rules").mkdir(parents=True, exist_ok=True)
        (swapped / "target" / "mdl.json").write_text(
            json.dumps({"models": [{"name": "solo", "properties": {"displayName": "独有表"}}]},
                       ensure_ascii=False), encoding="utf-8")
        (swapped / "knowledge" / "rules" / "口径规则.md").write_text(
            "## 独有口径\n\n独有短语 SWAPPED_ONLY 只存在于这份语料里。\n", encoding="utf-8")
        with mock.patch.object(embedder, "embed_texts", lambda texts, **kw: [_bag_of_chars(t) for t in texts]):
            swap_build = indexer.build_index(swapped, "demo", force=True, with_vectors=True)
        check(swap_build.get("ok") is True and swap_build.get("skipped") is False, "同目录换成另一份语料（overwrite）")
        check(stale.fts("工时", limit=5) == [], "换语料后**旧对象读不到旧数据**（陈旧 Table 陷阱的回归闸）")
        check(bool(stale.fts("SWAPPED_ONLY", limit=5)), "同一旧对象立刻看到新数据（缓存按版本提示失效）")

        broken = ws / "broken_lance"
        bdir = S.index_dir_for(broken)
        S.table_dir(bdir).mkdir(parents=True, exist_ok=True)   # 目录在，但不是合法的 lance 数据集
        (bdir / S.META_FILENAME).write_text(json.dumps({"backend": "lance", "items": 1}), encoding="utf-8")
        check(search.search(broken, "工时") == [], "损坏/非法的 lance 数据集 ⇒ [] 且不抛")
        check(isinstance(backends.open_backend(bdir).stats(), dict), "stats 在损坏目录上也能读（不抛）")

        # 元数据声明的是别的后端 ⇒ 不当成自己的索引（防止读到不属于自己的残留）
        mismatch = ws / "mismatch_lance"
        mdir = S.index_dir_for(mismatch)
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / S.META_FILENAME).write_text(json.dumps({"backend": "jsonl", "items": 3}), encoding="utf-8")
        check(backends.open_backend(mdir).ready() is False, "meta 声明后端不符 ⇒ 不认作自己的索引")


def run_boot_safety() -> None:
    """⓿ 惰性 import 与后端降级 —— 这条是**镜像级**铁律。

    生产镜像在重建之前**没有 lancedb**（它是按 `langchain-qwq` 先例单独装的），任何
    import 期的 lancedb 依赖都会把整个 agent 拖死。所以这条闸门在**子进程**里真跑一次
    import，而不是靠读代码相信。
    """
    section("⓿ 惰性 import 与后端降级（镜像没装 lancedb 也不许炸）")
    code = (
        "import sys\n"
        "import agent.retrieval, agent.retrieval.search, agent.retrieval.indexer\n"
        "import agent.retrieval.backends, agent.retrieval.store\n"
        "assert 'lancedb' not in sys.modules, 'import 期就加载了 lancedb'\n"
        "print('lazy-ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_REPO / "src"), "AGENT_DATA_ROOT": str(_WORK)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    check(proc.returncode == 0 and "lazy-ok" in proc.stdout,
          "import 业务模块不会加载 lancedb", (proc.stderr or "").strip()[-200:])

    # 模拟「镜像里装不出来」：直接改探测缓存（私有全局，但这是唯一能造出该状态的开关）
    saved = backends._lance_ok
    try:
        backends._lance_ok = False
        set_backend("lance")
        check(backends.backend_name() == "jsonl", "env 指定 lance 但装不出来 ⇒ 自动退回 jsonl")
        check(backends.open_backend(_WORK / "nowhere").name == "jsonl", "open_backend 同样退到 jsonl")
    finally:
        backends._lance_ok = saved


def main() -> int:
    have_lance = backends.lance_available()
    print(f"lancedb 可用 = {have_lance}")
    run_boot_safety()
    for backend_name in ("jsonl", "lance"):
        if backend_name == "lance" and not have_lance:
            print("\n[lance] 跳过（lancedb 未安装）")
            continue
        ws = _WORK / backend_name / "workspace"
        proj = ws / "demo_wrenai"
        backup = ws / "demo_wrenai.备份-20260921-101010"
        run_shared(backend_name, ws, proj, backup)
        if backend_name == "jsonl":
            run_jsonl_only(ws, proj)
        else:
            run_lance_only(ws, proj)

    passed = sum(1 for ok, _label, _scene in results if ok)
    total = len(results)
    print(f"\n{'=' * 62}\n{passed}/{total} 通过")
    if passed != total:
        print("失败项（带所属段，两后端同名断言靠它区分）：")
        for ok, label, scene in results:
            if not ok:
                print(f"  - [{scene}] {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_WORK, ignore_errors=True)
    raise SystemExit(code)
