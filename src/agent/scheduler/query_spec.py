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
from datetime import date, datetime, timedelta, timezone
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


#: cube 查询定义允许固化的键（wren ``query_cube`` 真正收下的编译键）。
CUBE_KEYS: tuple[str, ...] = (
    "cube",
    "measures",
    "dimensions",
    "filters",
    "time_dimension",
)

#: 固化执行时结果集的行上限（超出按截断回放，不判失效）。
DEFAULT_ROW_LIMIT = 5000


def extract_spec(
    check: dict,
    *,
    db_name: str,
    tz: str,
    semantic_version: str,
    source_thread_id: str,
    now: datetime,
    date_bindings: dict[str, str] | None = None,
    accept_constants: bool = False,
) -> tuple[dict, list[str]]:
    """check_async_task 结果 → 固化 spec。

    通道由 ``check["sql_kind"]`` 决定：``"cube"`` 走 cube 定义，其余走物理 SQL。
    ``expect_columns`` 这里留空——它必须来自**验证执行**的真实列（Task 5 填）。
    """
    anchors = resolve_anchors(now, tz)
    base = {
        "v": SPEC_VERSION,
        "db_name": db_name,
        "tz": tz,
        "semantic_version": semantic_version,
        "source_thread_id": source_thread_id,
        "captured_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }

    if check.get("sql_kind") == "cube":
        args = check.get("cube_args") or {}
        if not isinstance(args, dict):
            raise SpecError("no_query", "cube_args 不是对象")
        unknown = [k for k in args if k not in CUBE_KEYS]
        if unknown:
            raise SpecError(
                "cube_key_unsupported",
                f"{unknown}；cube 只认 {list(CUBE_KEYS)}",
            )
        if not args.get("cube") or not args.get("measures"):
            raise SpecError("no_query", "cube 定义缺少 cube 或 measures")

        used: list[str] = []
        warns: list[str] = []

        def _add(u: list[str], w: list[str]) -> None:
            for x in u:
                if x not in used:
                    used.append(x)
            warns.extend(w)

        template: dict = {}
        for key in ("cube", "measures", "dimensions"):
            if key in args:
                template[key] = args[key]
        if args.get("filters"):
            rendered_filters = []
            for f in args["filters"]:
                t, u, w = templatize(str(f), anchors, date_bindings, accept_constants)
                rendered_filters.append(t)
                _add(u, w)
            template["filters"] = rendered_filters
        if args.get("time_dimension"):
            t, u, w = templatize(
                str(args["time_dimension"]), anchors, date_bindings, accept_constants
            )
            template["time_dimension"] = t
            _add(u, w)

        return (
            {
                **base,
                "channel": "cube",
                "cube_template": template,
                "params": used,
                "expect_columns": [],
                "row_limit": DEFAULT_ROW_LIMIT,
                "warnings": warns,
            },
            warns,
        )

    sql = (check.get("dialect_sql") or "").strip() or (check.get("sql") or "").strip()
    if not sql:
        raise SpecError("no_query", "既没有物理 SQL 也没有 cube 定义")
    template_sql, used, warns = templatize(sql, anchors, date_bindings, accept_constants)
    return (
        {
            **base,
            "channel": "sql",
            "sql_template": template_sql,
            "params": used,
            "expect_columns": [],
            "row_limit": DEFAULT_ROW_LIMIT,
            "warnings": warns,
        },
        warns,
    )


def _template_placeholders(spec: dict) -> list[str]:
    """spec 模板里出现的占位符（sql 或 cube 通道）。"""
    found: list[str] = []
    if spec.get("channel") == "sql":
        found.extend(placeholders(spec.get("sql_template") or ""))
    else:
        tpl = spec.get("cube_template") or {}
        for f in tpl.get("filters") or []:
            found.extend(placeholders(str(f)))
        found.extend(placeholders(str(tpl.get("time_dimension") or "")))
    return list(dict.fromkeys(found))


def render_spec(spec: dict, now: datetime) -> dict:
    """按当次时间把 spec 渲染成可直接执行的 SQL / cube 定义。

    Returns: ``{"channel", "sql", "cube", "params"}``；``sql`` / ``cube`` 互斥。
    """
    if spec.get("v") != SPEC_VERSION:
        raise SpecError("spec_version_unknown", str(spec.get("v")))
    anchors = resolve_anchors(now, spec.get("tz") or "Asia/Shanghai")

    declared = set(spec.get("params") or [])
    needed = _template_placeholders(spec)
    missing = [p for p in needed if p not in declared]
    if missing:
        raise SpecError(
            "render_failed", f"模板占位符 {missing} 不在 params 里（spec 被改坏了？）"
        )

    params = {p: anchors[p] for p in needed}
    if spec.get("channel") == "sql":
        return {
            "channel": "sql",
            "sql": render_text(spec.get("sql_template") or "", anchors),
            "cube": None,
            "params": params,
        }

    tpl = spec.get("cube_template") or {}
    cube: dict = {}
    for key in ("cube", "measures", "dimensions"):
        if key in tpl:
            cube[key] = tpl[key]
    if tpl.get("filters"):
        cube["filters"] = [render_text(str(f), anchors) for f in tpl["filters"]]
    if tpl.get("time_dimension"):
        cube["time_dimension"] = render_text(str(tpl["time_dimension"]), anchors)
    return {"channel": "cube", "sql": None, "cube": cube, "params": params}


def validate_columns(spec: dict, columns: list[str]) -> tuple[bool, str]:
    """结果列与 ``expect_columns`` 比对：大小写不敏感、忽略顺序。

    顺序和大小写由驱动决定，不该算漂移；少列/多列才是（口径或表结构变了）。
    """
    expect = {str(c).lower() for c in spec.get("expect_columns") or []}
    got = {str(c).lower() for c in columns}
    if not expect:
        return True, ""
    missing = sorted(expect - got)
    extra = sorted(got - expect)
    if not missing and not extra:
        return True, ""
    return False, f"missing=[{', '.join(missing)}] extra=[{', '.join(extra)}]"


def _row_cells(row: Any, cols: list[str]) -> list[str]:
    """一行 → 单元格文本。

    **真实形状是 ``dict``**：``db.multi_sql.df_to_result`` 用
    ``df.to_dict(orient="records")``，所以按 ``columns`` 取值。只认 list/tuple
    会把整行渲染成一个 ``str(dict)`` 单元格——表头 2 列、数据 1 列，模型据此
    汇总/画图全错（2026-10-23 评审 C1）。
    """
    if isinstance(row, dict):
        return [str(row.get(c, "")) for c in cols]
    if isinstance(row, (list, tuple)):
        return [str(c) for c in row]
    return [str(row)]


def build_check_payload(
    spec: dict,
    *,
    thread_id: str,
    columns: list[str],
    rows: list,
    sql: str,
    cube_query: str,
    cube_args: dict,
    cube_tool: str,
    row_count: int,
    truncated: bool,
) -> dict:
    """合成一条 `check_async_task` 结果——报告的**唯一**数据源。

    ``build_report`` 按内容签名认它（status / thread_id / result 三键 + success），
    所以这几个键一个都不能少。
    """
    head = f"> 固化回放：按已固化查询取数，共 {row_count} 行"
    if row_count > len(rows):
        # 载荷只带前 N 行：不写明的话模型会把「看到的」当成「全部」
        head += f"（仅展示前 {len(rows)} 行）"
    if truncated:
        head += "（结果已截断）"
    lines = [head, ""]
    cols = [str(c) for c in columns]
    if cols:
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for r in rows:
            lines.append("| " + " | ".join(_row_cells(r, cols)) + " |")

    payload: dict = {
        "status": "success",
        "thread_id": thread_id,
        "result": "\n".join(lines),
        "result_size": row_count,
        "row_count": row_count,
        "truncated": truncated,
        "sql": sql,
        "pinned_replay": True,
    }
    if spec.get("channel") == "cube":
        payload["sql_kind"] = "cube"
        payload["cube_query"] = cube_query
        payload["cube_args"] = cube_args
        payload["cube_tool"] = cube_tool
    return payload
