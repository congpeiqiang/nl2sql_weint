"""定时任务「固化查询」的 spec 逻辑（纯函数，无 I/O）。

固化 = 把跑对过一次的查询存成**模板 + 日期锚点**，每次触发按当次时间渲染。
直接缓存 SQL 字符串是不行的：物理 SQL 里的日期是字面量，第二个月还在查上个月。

占位符语法 ``{{p:<anchor>}}``，锚点闭集见 ``ANCHOR_KINDS``，按任务时区计算。

本模块只做文本与数据结构，不碰数据库、不碰文件——执行见 ``pin_exec``，
编排见 ``pin_service``。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

#: 固化 spec 的版本。未知版本一律拒绝（未来兼容闸）。
SPEC_VERSION = 1

#: 锚点闭集。新增锚点属于 spec 语义变更，要同时升 SPEC_VERSION。
ANCHOR_KINDS: tuple[str, ...] = (
    "today",
    "yesterday",
    "tomorrow",
    "month_start",
    "last_month_start",
    "last_month_end",
    "year_start",
    "week_start",
)

_DATE_FMT = "%Y-%m-%d"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PLACEHOLDER = "{{{{p:{anchor}}}}}"


class SpecError(Exception):
    """固化失败。``code`` 是机读的失效码（见计划 §C.2），``detail`` 给人看。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def resolve_anchors(now: datetime, tz: str) -> dict[str, str]:
    """算出 ``now`` 在任务时区 ``tz`` 下各锚点的日期字符串。

    日期运算全部用 ``date``，不带时分秒——避免 DST 让 ``timedelta(days=1)`` 变成
    23/25 小时。
    """
    d = now.astimezone(ZoneInfo(tz)).date()
    month_start = d.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    return {
        "today": d.strftime(_DATE_FMT),
        "yesterday": (d - timedelta(days=1)).strftime(_DATE_FMT),
        "tomorrow": (d + timedelta(days=1)).strftime(_DATE_FMT),
        "month_start": month_start.strftime(_DATE_FMT),
        "last_month_start": last_month_end.replace(day=1).strftime(_DATE_FMT),
        "last_month_end": last_month_end.strftime(_DATE_FMT),
        "year_start": date(d.year, 1, 1).strftime(_DATE_FMT),
        "week_start": (d - timedelta(days=d.weekday())).strftime(_DATE_FMT),
    }


def templatize(
    text: str,
    anchors: dict[str, str],
    bindings: dict[str, str] | None = None,
    accept_constants: bool = False,
) -> tuple[str, list[str], list[str]]:
    """把文本里的日期字面量换成 ``{{p:anchor}}``。

    Returns: ``(模板文本, 用到的锚点[首次出现序], 常量告警)``。

    判定（逐**不同**字面量）：
    - 出现在 ``bindings`` 里 → 用指定锚点，但必须校验该锚点当天取值等于字面量；
    - 恰好命中 1 个锚点 → 用它；
    - 0 个 → ``accept_constants`` 为真则原样保留 + 告警，否则 ``unmatched_date``；
    - 命中 ≥2 个（如每月 1 号 today 与 month_start 同值）→ ``ambiguous_date``，
      必须由 ``bindings`` 消歧。选错锚点下个月口径就错了，绝不猜。
    """
    bindings = bindings or {}
    mapping: dict[str, str] = {}
    used: list[str] = []
    warns: list[str] = []

    for literal in dict.fromkeys(DATE_RE.findall(text)):
        if literal in bindings:
            anchor = bindings[literal]
            if anchor not in ANCHOR_KINDS:
                raise SpecError("bad_binding", f"{literal}: 未知锚点 {anchor}")
            if anchors.get(anchor) != literal:
                raise SpecError(
                    "bad_binding",
                    f"{literal} 不等于锚点 {anchor} 当天的取值 {anchors.get(anchor)}",
                )
        else:
            candidates = [k for k in ANCHOR_KINDS if anchors.get(k) == literal]
            if not candidates:
                if not accept_constants:
                    raise SpecError(
                        "unmatched_date",
                        f"{literal} 不对应任何日期锚点；如确认是业务常量请显式接受",
                    )
                warns.append(f"常量日期保留: {literal}")
                continue
            if len(candidates) > 1:
                raise SpecError(
                    "ambiguous_date",
                    f"{literal}: {candidates}（用 date_bindings 指定其中一个）",
                )
            anchor = candidates[0]
        mapping[literal] = anchor

    for literal in DATE_RE.findall(text):
        anchor = mapping.get(literal)
        if anchor and anchor not in used:
            used.append(anchor)

    rendered = text
    for literal, anchor in mapping.items():
        rendered = rendered.replace(literal, PLACEHOLDER.format(anchor=anchor))
    return rendered, used, warns


def render_text(template: str, anchors: dict[str, str]) -> str:
    """把 ``{{p:anchor}}`` 换成当次取值。未知占位符原样保留（调用方另有校验）。"""
    return re.sub(
        r"\{\{p:([a-z_]+)\}\}",
        lambda m: anchors.get(m.group(1), m.group(0)),
        template,
    )


def placeholders(template: str) -> list[str]:
    """模板里出现过的占位符锚点名（首次出现序，去重）。"""
    return list(dict.fromkeys(re.findall(r"\{\{p:([a-z_]+)\}\}", template)))


def end_of_month(d: date) -> date:
    """当月最后一天（供后续任务复用，避免各处自己算）。"""
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])
