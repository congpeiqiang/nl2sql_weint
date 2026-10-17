# -*- coding: utf-8 -*-
"""get_context 知识出口（Part B）验收 —— 离线 fixture，无数据库、无 GPU、无 lancedb 依赖。

**验的是什么**：`NL2SQL_SYSTEM_PROMPT.md` §九新写的「口径轴」承诺 ——
`get_context(question, item_type=glossary/metrics/caveats/rules/knowledge)` 能取出
knowledge/ 三分目录的业务口径片段。这条通道此前**不存在**（三个 kind 建了索引但全仓
零读取点），实现在 `agent/retrieval/adapters.py` 的知识分支。

四条核心不变量（对应方案 Part B 第 5 项）：
  A 知识取值命中知识 kind，且 `text` **未被默认 600 截断**（`text_cap=4000`）；
  B 知识取值零命中**不回落** full schema（返回说明性产物，键集仍是 strategy/schema/note）；
  C 非知识 item_type（model/column/未知名）行为与改动前**逐字一致**
    （含 model_name 过滤、零命中回落 None、note 原文、skeleton 追加）；
  D `NL2SQL_RETRIEVAL` off 时整个出口不生效（context_artifact → None，与现状一致）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_knowledge_retrieval.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import 业务模块之前：数据落点由 AGENT_DATA_ROOT 推导
_REPO = pathlib.Path(__file__).resolve().parents[1]
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-knowledge-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ["NL2SQL_RETRIEVAL"] = "fts"          # fts 腿不需要嵌入模型
os.environ["NL2SQL_RETRIEVAL_BACKEND"] = "jsonl"  # 无 lancedb 依赖

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


# ── fixture ────────────────────────────────────────────────
MDL = {
    "models": [
        {
            "name": "workhour",
            "tableReference": {"catalog": "dw", "schema": "hr", "table": "t_workhour"},
            "properties": {"displayName": "工时"},
            "columns": [
                {"name": "hours", "type": "INTEGER",
                 "properties": {"displayName": "工时"}},
            ],
        },
    ],
    "views": [],
    "relationships": [],
    "cubes": [],
}

# 单块 ~900 字符（>TEXT_CAP=600、<CHUNK_CHARS=1200 ⇒ 恰好一整块）：
# 若知识分支没把 text_cap 放大，块尾的 TAILMARK 会被腰斩掉 —— 这就是 A 类判据。
_GLOSSARY_BODY = "血红蛋白的正常范围判读需要结合性别与年龄综合给出。" * 24
GLOSSARY_TAIL = "HGB_TAILMARK尾标"


def make_project(root: pathlib.Path) -> pathlib.Path:
    (root / "target").mkdir(parents=True, exist_ok=True)
    for sub in ("rules", "sql", "glossary", "metrics", "caveats"):
        (root / "knowledge" / sub).mkdir(parents=True, exist_ok=True)
    (root / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
    (root / "target" / "mdl.json").write_text(
        json.dumps(MDL, ensure_ascii=False), encoding="utf-8")
    (root / "knowledge" / "rules" / "口径规则.md").write_text(
        "## 工时口径\n\n工时以人力资源系统为准，加班工时单独统计。RULETAIL\n",
        encoding="utf-8")
    (root / "knowledge" / "glossary" / "术语档案.md").write_text(
        f"## 血红蛋白术语\n\n{_GLOSSARY_BODY}{GLOSSARY_TAIL}\n", encoding="utf-8")
    (root / "knowledge" / "metrics" / "指标档案.md").write_text(
        "## 完赛率\n\n完赛率等于 statusId = 1 的行数占比。METTAIL\n", encoding="utf-8")
    (root / "knowledge" / "caveats" / "陷阱档案.md").write_text(
        "## 文本日期\n\n日期列是文本形态，比较前先确认格式。CAVTAIL\n", encoding="utf-8")
    return root


def main() -> int:
    ws = _WORK / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    proj = make_project(ws / "demo_wrenai")

    from agent.retrieval import adapters
    from agent.retrieval import indexer
    from agent.retrieval import schema as S
    from agent.retrieval import search

    built = indexer.build_index(proj, "demo", with_vectors=False)
    check(built.get("ok") is True, "fixture 索引构建成功",
          f"items={built.get('items')}")
    adapters.forget_freshness(proj)

    q_hit = "血红蛋白的正常范围"
    q_miss = "qqqq zzzz vvvv 不存在的检索词"

    # ── A 知识取值命中 + text_cap 放大 ──
    section("A 知识出口命中（glossary/metrics/caveats/rules/knowledge）")
    art = adapters.context_artifact(proj, q_hit, item_type="glossary", db_name="demo")
    check(isinstance(art, dict), "item_type=glossary 返回产物（非 None）")
    if isinstance(art, dict):
        check(set(art) == {"strategy", "schema", "note"}, "键集恰为 strategy/schema/note",
              str(sorted(art)))
        check(art.get("strategy") == "retrieval", "strategy=retrieval")
        schema_text = str(art.get("schema") or "")
        check(GLOSSARY_TAIL in schema_text,
              "块尾 TAILMARK 在产物里 ⇒ text 未被 600 截断（text_cap 已放大）",
              f"schema {len(schema_text)} chars")
        check("[全部对象（仅名字）]" not in schema_text, "知识分支不追加 schema 骨架")
        check("glossary" in schema_text, "片段带 src_path（可溯源到 knowledge/glossary/…）")
    # 直查 search 层复核 kind 与 text 长度
    hits = search.search_project(proj, q_hit, kinds={S.KIND_KNOWLEDGE_GLOSSARY},
                                 db_name="demo", text_cap=adapters.KNOWLEDGE_TEXT_CAP)
    check(bool(hits) and all(h.get("kind") == S.KIND_KNOWLEDGE_GLOSSARY for h in hits),
          "search 命中的 kind 全是 knowledge_glossary")
    check(any(len(str(h.get("text") or "")) > 600 for h in hits),
          "存在 text > 600 的知识片段（默认 TEXT_CAP 下不可能）")

    for it, tail in (("metrics", "METTAIL"), ("caveats", "CAVTAIL"), ("rules", "RULETAIL")):
        q = {"metrics": "完赛率怎么算", "caveats": "文本日期格式陷阱",
             "rules": "工时口径"}[it]
        a = adapters.context_artifact(proj, q, item_type=it, db_name="demo")
        check(isinstance(a, dict) and tail in str(a.get("schema") or ""),
              f"item_type={it} 命中自己的档案（含 {tail}）")
    a = adapters.context_artifact(proj, q_hit, item_type="knowledge", db_name="demo")
    check(isinstance(a, dict) and GLOSSARY_TAIL in str(a.get("schema") or ""),
          "item_type=knowledge（四 kind 并集）也能命中")
    a = adapters.context_artifact(proj, q_hit, item_type="glossary",
                                  model_name="随便什么模型名", db_name="demo")
    check(isinstance(a, dict) and GLOSSARY_TAIL in str(a.get("schema") or ""),
          "model_name 过滤**不作用于**知识分支（知识条目 meta 没有 model 字段）")

    # ── B 零命中不回落 ──
    section("B 知识零命中 ⇒ 说明性产物（不回落 full schema）")
    art = adapters.context_artifact(proj, q_miss, item_type="metrics", db_name="demo")
    check(isinstance(art, dict), "零命中返回 dict 而不是 None（None=回落 full schema）")
    if isinstance(art, dict):
        check(set(art) == {"strategy", "schema", "note"}, "键集恰为 strategy/schema/note")
        check(art.get("strategy") == "retrieval", "strategy=retrieval（检索跑过了）")
        check(bool(str(art.get("schema") or "").strip()), "schema 非空且说明性")
        check("get_instructions" in str(art.get("note") or ""),
              "note 指路 get_instructions / 换 item_type 重试")

    # ── C 非知识行为逐字一致 ──
    section("C 非知识 item_type 行为与改动前一致")
    check(adapters._kinds_for_item_type("model") == {S.KIND_SCHEMA_TABLE},
          "item_type=model → 只搜 schema_table（原映射未被知识映射污染）")
    check(adapters._kinds_for_item_type(None) == set(adapters._SCHEMA_KINDS),
          "item_type 缺省 → 结构类全搜（原行为）")
    check(adapters._kinds_for_item_type("nonsense_type") == set(adapters._SCHEMA_KINDS),
          "未知 item_type → 结构类全搜（原行为，不窄化）")
    check(not adapters._is_knowledge_request(adapters._kinds_for_item_type("model")),
          "model 不被判成知识请求")
    art = adapters.context_artifact(proj, "工时", item_type="model", db_name="demo")
    check(isinstance(art, dict), "结构检索（item_type=model）命中")
    if isinstance(art, dict):
        check(set(art) == {"strategy", "schema", "note"}, "键集恰为 strategy/schema/note")
        check(art.get("note") == adapters._CONTEXT_NOTE, "note 是结构版原文（未换文案）")
    art = adapters.context_artifact(proj, "工时", item_type="model",
                                    model_name="no_such_model", db_name="demo")
    check(art is None, "model_name 过滤照旧：全灭 ⇒ None（回落 full schema）")
    art = adapters.context_artifact(proj, q_miss, db_name="demo")
    check(art is None, "结构检索零命中 ⇒ None（宁多给不可空给，原语义保留）")

    # ── D off 模式负对照 ──
    section("D NL2SQL_RETRIEVAL off ⇒ 出口整体不生效（与现状一致）")
    os.environ.pop("NL2SQL_RETRIEVAL", None)
    try:
        adapters.forget_freshness(proj)
        art = adapters.context_artifact(proj, q_hit, item_type="glossary", db_name="demo")
        check(art is None, "off：知识取值返回 None（不得返回说明性产物）")
        art = adapters.context_artifact(proj, "工时", item_type="model", db_name="demo")
        check(art is None, "off：结构取值返回 None（原行为）")
        check(search.search_project(proj, q_hit, kinds={S.KIND_KNOWLEDGE_GLOSSARY}) == [],
              "off：search 层直接空集")
    finally:
        os.environ["NL2SQL_RETRIEVAL"] = "fts"
        adapters.forget_freshness(proj)
    art = adapters.context_artifact(proj, q_hit, item_type="glossary", db_name="demo")
    check(isinstance(art, dict) and GLOSSARY_TAIL in str((art or {}).get("schema") or ""),
          "恢复 fts ⇒ 出口重新生效（sanity）")

    # ── 汇总 ──
    total = len(results)
    bad = [r for r in results if not r[0]]
    print("\n" + "=" * 62)
    if bad:
        print("失败明细：")
        for _, label, scene in bad:
            print(f"  [{scene}] {label}")
    print(f"{total - len(bad)}/{total} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
