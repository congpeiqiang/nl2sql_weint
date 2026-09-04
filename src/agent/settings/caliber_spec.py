# -*- coding: utf-8 -*-
"""业务口径规范（权威口径的单一事实来源，驱动 S3 澄清门护栏 + S5 数据快照）。

背景（同题多跑答案不一致归因 S3/S5）：
- 「应报工人员池」等口径无权威定义 → 模型自由发挥（18/12 人分歧）。S3 用确定性规则
  把「默认口径」注入子 agent 提示词，并在最终 SQL 上做「禁止表」后置校验，让
  「换个口径直接溜过」在代码上不可能。
- 活库数据在评测窗口内漂移 → 两次 run 结果不同被误判为模型不稳。S5 用数据快照
  （指纹 SQL + 时间戳）让「数据变了」可观测、可比对。

规范按库组织（key = db_name.casefold()）。新增口径/库时在此追加一条；命中匹配
大小写不敏感（与 normalize_db_name / SemanticDbDetector 同口径）。

注意：本规范是「结构化骨架」，驱动确定性护栏；面向 LLM 的业务知识解释在语义库
`knowledge/glossary|rules`（S1），两者应引用同一口径，改口径需同步。
"""
from __future__ import annotations

from typing import Any

# 库级口径规范。key 一律小写（casefold）。
#  - sensitive_terms：口径敏感术语；命中才触发护栏，未命中零干预。
#  - default_roster_table：该口径的默认人员池表（权威）；为空 = 尚无默认口径 → 需澄清。
#  - forbidden_roster_tables：非默认口径表，最终 SQL 命中 → 后置护栏告警。
#  - reported_table：报工/事实记录表（差集公式用，文档性字段）。
#  - fingerprint_sql：S5 数据快照指纹（廉价 count/max 查询，可空）。
CALIBER_SPECS: dict[str, dict[str, Any]] = {
    # WIT 运营管理平台数据库（默认口径已拍板 2026-09-04：应报工池 = 在职员工）
    "wit运营管理平台数据库": {
        "sensitive_terms": [
            {
                "term": "报工",
                "aliases": ["工时填报", "填工时", "打卡工时", "工作日志"],
                "default_roster_table": "do_department_user_detail",
                "forbidden_roster_tables": ["do_table_user"],
                "reported_table": "do_work_hour_examine",
                "default_desc": (
                    "在职员工（do_department_user_detail 中 deleted='0'，"
                    "并按 entry_date/leave_date 剔除统计窗口外尚未入职/已离职者）；"
                    "「是否报工」以员工本人提交的报工单 do_work_hour_examine 判定"
                    "（含 status=1 待审核，禁止用 do_work_hour 判报工——该表含系统补录行）"
                ),
            },
        ],
        "fingerprint_sql": [
            "SELECT count(*) AS work_hour_cnt, max(date) AS max_date "
            "FROM do_work_hour WHERE deleted='0'",
            "SELECT count(*) AS roster_cnt "
            "FROM do_department_user_detail WHERE deleted='0'",
        ],
    },
}


def lookup_spec(db_name: str) -> dict[str, Any] | None:
    """按 db_name 查口径规范（大小写不敏感）；无 → None。"""
    if not db_name:
        return None
    key = str(db_name).casefold()
    for k, v in CALIBER_SPECS.items():
        if k.casefold() == key:
            return v
    return None


def _contains_any(text: str, needles: list[str]) -> bool:
    t = (text or "").casefold()
    return any((n or "").casefold() in t for n in needles if n)


def match_sensitive_terms(question: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """返回命中问题文本的口径敏感术语（term 或 alias 子串命中，大小写不敏感）。"""
    if not spec or not question:
        return []
    out = []
    for t in spec.get("sensitive_terms", []) or []:
        needles = [t.get("term", "")] + list(t.get("aliases", []) or [])
        if _contains_any(question, needles):
            out.append(t)
    return out


def caliber_constraint_text(question: str, db_name: str) -> str:
    """S3-1 预检：命中口径敏感术语 → 返回要注入子 agent 提示词的权威口径约束。

    返回 "" 表示无命中（零干预）。命中但无默认口径（default_roster_table 空）
    → 返回澄清指令（要求停下追问）；命中且有默认口径 → 返回「默认口径表 + 禁表」约束。
    """
    spec = lookup_spec(db_name)
    if not spec:
        return ""
    matched = match_sensitive_terms(question, spec)
    if not matched:
        return ""

    lines = ["\n\n## 业务口径约束（权威，勿猜）"]
    for t in matched:
        term = t.get("term", "")
        default = t.get("default_roster_table", "")
        forbidden = t.get("forbidden_roster_tables", []) or []
        if not default:
            lines.append(
                f"- 「{term}」存在多种合理口径且无权威默认定义，**不得自行猜测**；"
                "必须停下向用户追问该术语的确切口径。"
            )
            continue
        desc = t.get("default_desc") or f"默认口径表 {default}"
        lines.append(f"- 「{term}」口径：{desc}。")
        if forbidden:
            lines.append(
                f"- 「{term}」的应报工人员池**必须**用 `{default}`，"
                f"**禁止**用 {', '.join('`' + f + '`' for f in forbidden)} 作为该口径。"
            )
    return "\n".join(lines)


def caliber_sql_warning(sql: str, db_name: str) -> str | None:
    """S3-2 后置护栏：最终 SQL 命中断言用的禁止表 → 返回告警；否则 None。

    只查「最终产出 SQL」（check_progress._extract_last_sql 的结果），不查探索期
    的探测 SQL——探索期对比口径表是合法行为，只有最终答案选错口径表才告警。
    """
    if not sql:
        return None
    spec = lookup_spec(db_name)
    if not spec:
        return None
    lower = sql.casefold()
    for t in spec.get("sensitive_terms", []) or []:
        default = t.get("default_roster_table", "")
        forbidden = t.get("forbidden_roster_tables", []) or []
        for ft in forbidden:
            if ft and ft.casefold() in lower:
                term = t.get("term", "")
                return (
                    f"⚠ 口径护栏：最终 SQL 使用了非默认口径表 `{ft}`"
                    f"（「{term}」默认口径表应为 `{default}`）。"
                    "若非用户明确要求该口径，请改用默认口径表并重新执行。"
                )
    return None


def fingerprint_sqls(db_name: str) -> list[str]:
    """S5：返回该库的数据快照指纹 SQL 列表（可空）。"""
    spec = lookup_spec(db_name)
    if not spec:
        return []
    return list(spec.get("fingerprint_sql", []) or [])
