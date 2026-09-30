"""归因内核：维度贡献分解（`2+2D` 条聚合查询），**纯计算、零 IO**。

**它是什么**：把「ΔY = Σ_d ΔY_d」这件确定的事做成代码 —— 给定两期窗口与候选维度，
算出每个维度取值的 `delta` / `contribution_pct`，做加性对账、排名、生成下钻条件。
**它不是什么**：不建立因果（方案 §1.1 的 T3 红线）、不编译 SQL、不连数据库、不读盘。
与外界唯一的接口是 :class:`AttributionRunner` 的两个方法，返回**行 dict 列表**
（列名 → 值），列解析由本模块自己做 —— 所以真实实现
（``agent.utils.attribution_runner.WrenCubeRunner``）与验证脚本里的桩实现签名完全一致，
单测就是「换一个实现」。

**移植来源**：``Datus-agent/datus/tools/func_tool/attribution_utils.py:88-953``
（``GenericAttributeAnalyzer``）。算法骨架、三个容差常量、错误码、排序口径全部照搬；
**三处刻意偏离**，每一处都写在对应代码旁边，也在下面列清楚：

1. **截断口径**（方案 §3.1 / §4.3）—— 参考实现把 ``max_values_per_dimension + 1`` 直接
   写进 SQL 的 ``LIMIT``；本仓写不进去（[`wren_plan._finish`](wren_plan.py) 对 cube 查询
   固定 append ``LIMIT 1001``，不接受传参）⇒ 本模块里这个参数只是**告警阈值**，
   真截断点 = **物理上限 1001 行**（见 :data:`_PHYSICAL_GROUPED_ROW_LIMIT`）；
   ``per_dimension.<dim>.values`` 保存**本次取回的全部取值**（参考实现截到 top_n）——
   裁掉一个取值就会让「Σ分组 ≠ 总量」凭空成立，把一个可加维度误判成不可加。
   ``top_n_values`` 只裁 ``top_dimension_values``（跨维展示摘要）。
2. **下钻条件**（方案 §1.2）—— 参考实现产出 ``where_sql`` 字符串；本仓的编译链只吃
   结构化过滤（``plan_cube_sql`` 的 ``args["filters"]`` 是 ``dim:op:value`` 列表）⇒
   产出 ``filters: [{dimension, operator, value}]``；``where_sql`` 降级为**只读渲染**
   （渲染 ``filters`` 整体，无父过滤时与参考实现逐字相同）。
   ⇒ 本模块因此不存在「模型自由文本进物理 SQL」的口子。
3. **策略分类不在这里**（方案 §3.6）—— ``term_wise`` / 比值 / ``AVG`` / ``COUNT(DISTINCT)``
   由工具层按 ``measure_expressions`` 的**算子形状**判定并决定放行还是拒答。
   所以本模块没有「不支持的度量」与 ``time_dimension_not_supported`` 这类分支，
   只保留参考实现里与分解过程本身有关的两个：``dimensions_required`` /
   ``no_additive_dimensions``。**本模块只算 ``term_wise``。**

**两个反直觉点**（照参考实现，别把它们「顺手改好」）：

① 传给 ``fetch_grouped`` 的 ``limit = max_values_per_dimension + 1`` 语义是**判截断**
   而**不是分页**（参考实现里多取的那一行不进结果）。协议保留它只为与参考实现同构、
   便于单测逐字对照；**本仓的 SQL 兑现不了它**，判截断看返回行数（见偏离 1）。
② 零总变化时 ``contribution_pct`` 是**整个键缺席**，不是 ``None`` / ``0`` / ``inf``。
   除以 0 产出的 ``inf`` 会被模型原样讲成「无限大贡献」，所以这里必须是「没有这个字段」，
   让模型无法引用。

上游方案：``docs/agent优化记录/归因分析方案.md`` §2.1（算法）/ §2.2（护栏）/ §4.2（本文件）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Literal, Protocol, Sequence

_logger = logging.getLogger(__name__)

# 维度取值归一化之后的标量类型（与参考实现 JsonScalar 同义）。
JsonScalar = str | int | float | bool

# ── 三个常量与参考实现逐字相同（attribution_utils.py:38-40）──
_ADDITIVITY_TOLERANCE = 0.02
_ZERO_DELTA_EPSILON = 1e-6
_MAX_DIMENSION_VALUES = 1000

# 分组查询的**物理**行数上限：`wren_plan` 对 cube 查询固定 append `LIMIT 1001`
# （= DEFAULT_ROW_LIMIT(1000) + 1 的探针行），不由调用方指定。
# ⇒ 本仓判「真截断」只能看返回行数是否触到天花板，**不能**看 max_values_per_dimension
#   （那是告警阈值，见模块 docstring 偏离 1）。
_PHYSICAL_GROUPED_ROW_LIMIT = 1001

_DEFAULT_MAX_VALUES_PER_DIMENSION = 500
_DEFAULT_TOP_N_VALUES = 10
_DEFAULT_TOP_N_DIMENSIONS = 3

# 下钻过滤的算子枚举（方案 §1.2：`operator` 限枚举，是注入面按构造关闭的一半）。
FILTER_OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "is_not_null")

_SQL_BINARY_OPERATOR = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

Period = Literal["baseline", "current"]


class TypedError(Exception):
    """带机器可读 ``code`` 的失败（方案 §2.2 的类型化错误码）。

    「按设计拒绝」与「工具故障」必须能分开：前者要求调用方告诉模型「换指标 / 换维度」，
    后者才值得重试。所以失败也要有码，不能只有一句自然语言。

    ``code`` 取值见方案 §2.2 末行。本模块新增一个参考实现没有的码：
    ``TOTAL_QUERY_FAILED`` —— 参考实现里两期总量查询直接抛出裸异常，本仓给它一个码，
    免得驱动层（pymysql 等）的错误文本原样流到模型面前。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        period: str | None = None,
        dimension: str | None = None,
        columns: Sequence[str] | None = None,
        row_count: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.period = period
        self.dimension = dimension
        self.columns = list(columns or ())
        self.row_count = row_count
        super().__init__(message)

    def to_payload(self) -> dict:
        """工具层回给模型的形状（与参考实现 AttributionValidationErrorPayload 同构）。"""
        return {
            "error_type": "attribution_validation_error",
            "code": self.code,
            "message": self.message,
            "period": self.period,
            "dimension": self.dimension,
            "columns": self.columns,
            "row_count": self.row_count,
        }


# ══════════════════════════════════════════════════════════════════════
# 入参：窗口 / 请求 / 取数协议
# ══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Window:
    """OSI 半开区间 ``[start, end)``，两端都是 ISO 日期字符串。"""

    start: str
    end: str

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


def validate_window(window: Window, *, period: Period) -> int:
    """校验半开区间并返回天数；反序 / 空区间 / 非 ISO ⇒ ``INVALID_TIME_WINDOW``。"""
    try:
        days = (date.fromisoformat(window.end) - date.fromisoformat(window.start)).days
    except (TypeError, ValueError):
        raise TypedError(
            "INVALID_TIME_WINDOW",
            f"{period} 期窗口必须是以 ISO 日期表示的半开区间 [start, end)；"
            f"收到 [{window.start}, {window.end})。",
            period=period,
        ) from None
    if days <= 0:
        raise TypedError(
            "INVALID_TIME_WINDOW",
            f"{period} 期窗口必须是非空半开区间 [start, end)；收到 [{window.start}, {window.end})。",
            period=period,
        )
    return days


@dataclass
class Request:
    """一次归因的入参。**全部字段都是纯数据**（本模块零 IO）。"""

    measure: str
    baseline: Window
    current: Window
    candidate_dimensions: Sequence[str] = ()
    time_dimension: str | None = None
    cube: str | None = None
    db_name: str | None = None
    filters: Sequence[dict] = ()
    top_n_dimensions: int | None = None      # 缺省 3
    top_n_values: int | None = None          # 缺省 10，只裁 top_dimension_values
    max_values_per_dimension: int | None = None   # 缺省 500，**告警阈值**（硬顶 1000）
    query_budget: int | None = None          # 缺省不限；`2+2D` 超预算即拒（方案 §3.5）
    metadata_extra: dict[str, Any] = field(default_factory=dict)   # 透传进 comparison_metadata


class AttributionRunner(Protocol):
    """取数协议：两个方法、都返回**行 dict 列表**。

    列名解析（大小写归一、剥 ``a.b`` / ``a__b`` 前缀）由本模块做，所以真实实现与桩实现
    形状完全一致 —— 单测就是「换一个实现」。
    """

    def fetch_total(self, window: Window) -> list[dict]:
        """不带维度的聚合（两期各一次）。"""
        ...

    def fetch_grouped(self, dimension: str, window: Window, limit: int) -> list[dict]:
        """``GROUP BY dimension`` 的聚合。``limit`` 语义见模块 docstring 反直觉点 ①。"""
        ...


# ══════════════════════════════════════════════════════════════════════
# 主循环：2 + 2D
# ══════════════════════════════════════════════════════════════════════
def attribute(runner: AttributionRunner, request: Request) -> dict:
    """两期窗口内的维度贡献分解。返回字段名逐字对齐参考实现（方案 §1.2）。

    ``2 + 2D`` 条查询**全部在本函数内顺序跑完**，中间查询对 LLM 透明。
    单维失败只影响该维（``dimension_analysis_failed``），其余维度照常出结果。
    """
    measure = request.measure
    candidate_dimensions = list(dict.fromkeys(request.candidate_dimensions or ()))
    baseline_days = validate_window(request.baseline, period="baseline")
    current_days = validate_window(request.current, period="current")

    requested_max_values = (
        _DEFAULT_MAX_VALUES_PER_DIMENSION
        if request.max_values_per_dimension is None
        else int(request.max_values_per_dimension)
    )
    if requested_max_values < 1 or requested_max_values > _MAX_DIMENSION_VALUES:
        # 参考实现是静默 clamp（min(requested, 1000)）；本仓按方案 §3.5 明确拒 ——
        # 一个被悄悄改过的上限会让「为什么只告警不截断」无从解释。
        raise TypedError(
            "INVALID_ARGUMENT",
            f"max_values_per_dimension={requested_max_values} 越界；"
            f"合法范围 1..{_MAX_DIMENSION_VALUES}（它是**告警阈值**，不是 SQL 的 limit）。",
        )
    max_values_per_dimension = requested_max_values
    top_n_values = (
        _DEFAULT_TOP_N_VALUES if request.top_n_values is None else max(1, int(request.top_n_values))
    )
    top_n_dimensions = (
        _DEFAULT_TOP_N_DIMENSIONS
        if request.top_n_dimensions is None
        else max(1, int(request.top_n_dimensions))
    )
    # 协议值（判截断语义）；**本仓的 SQL 兑现不了它**，见模块 docstring 反直觉点 ①。
    grouped_query_limit = max_values_per_dimension + 1

    if request.query_budget is not None and 2 + 2 * len(candidate_dimensions) > request.query_budget:
        raise TypedError(
            "INVALID_ARGUMENT",
            f"候选维度太多：{len(candidate_dimensions)} 个维度需要 "
            f"{2 + 2 * len(candidate_dimensions)} 条聚合查询，超出预算 {request.query_budget}。",
        )

    warnings: list[dict] = []
    queries_executed = 2
    baseline_total = _fetch_total(
        runner, request.baseline, measure=measure, period="baseline", warnings=warnings
    )
    current_total = _fetch_total(
        runner, request.current, measure=measure, period="current", warnings=warnings
    )
    total_delta = current_total - baseline_total

    if baseline_days != current_days:
        warnings.append(
            {
                "code": "unequal_windows",
                "message": (
                    f"基期与当期窗口分别是 {baseline_days} 天和 {current_days} 天；"
                    "数值**未做任何归一化**，比较前必须披露这一点。"
                ),
            }
        )

    total_delta_is_zero = _is_effectively_zero_delta(
        total_delta, baseline_total=baseline_total, current_total=current_total
    )
    dimension_rankings: list[dict] = []
    all_contributions: dict[str, list[dict]] = {}
    per_dimension: dict[str, dict] = {}

    for dimension in candidate_dimensions:
        baseline_rows, baseline_lookup, error = _query_grouped_period(
            runner,
            dimension=dimension,
            period="baseline",
            window=request.baseline,
            limit=grouped_query_limit,
            measure=measure,
        )
        queries_executed += 1
        if error is not None:
            _record_dimension_failure(dimension=dimension, error=error, warnings=warnings)
            continue

        current_rows, current_lookup, error = _query_grouped_period(
            runner,
            dimension=dimension,
            period="current",
            window=request.current,
            limit=grouped_query_limit,
            measure=measure,
        )
        queries_executed += 1
        if error is not None:
            _record_dimension_failure(dimension=dimension, error=error, warnings=warnings)
            continue

        union_keys = list(dict.fromkeys([*baseline_lookup, *current_lookup]))
        # 偏离 1：真截断看**物理上限**，不看 max_values_per_dimension（那只是告警阈值）。
        row_cap_hit = (
            len(baseline_rows) >= _PHYSICAL_GROUPED_ROW_LIMIT
            or len(current_rows) >= _PHYSICAL_GROUPED_ROW_LIMIT
        )
        over_warn_threshold = len(union_keys) > max_values_per_dimension
        truncated = row_cap_hit
        if row_cap_hit or over_warn_threshold:
            if row_cap_hit:
                reason = (
                    f"返回行数触到物理上限 {_PHYSICAL_GROUPED_ROW_LIMIT}，分组结果**已被截断**"
                    f"（本次取回 {len(union_keys)} 个取值，实际更多）"
                )
            else:
                reason = (
                    f"取值数 {len(union_keys)} 超过告警阈值 {max_values_per_dimension}"
                    "（**未丢值、未截断**）"
                )
            warnings.append(
                {
                    "code": "high_cardinality_dimension",
                    "dimension": dimension,
                    "message": (
                        f"维度 '{dimension}' {reason}；请先归因更粗的维度，"
                        "再用 drill_down 收窄到这个维度。"
                    ),
                }
            )

        contributions = _build_contributions(
            dimension=dimension,
            union_keys=union_keys,
            baseline_lookup=baseline_lookup,
            current_lookup=current_lookup,
            total_delta=total_delta,
            total_delta_is_zero=total_delta_is_zero,
            base_filters=request.filters,
        )
        deltas = [contribution["delta"] for contribution in contributions]
        score = (
            max(abs(delta) for delta in deltas) / abs(total_delta)
            if deltas and not total_delta_is_zero
            else None
        )
        additivity = _check_additivity(
            baseline_total=baseline_total,
            current_total=current_total,
            baseline_values=list(baseline_lookup.values()),
            current_values=list(current_lookup.values()),
            total_delta_is_zero=total_delta_is_zero,
        )
        if additivity["status"] == "failed":
            warnings.append(
                {
                    "code": "non_additive_dimension",
                    "dimension": dimension,
                    "message": (
                        f"维度 '{dimension}' 的分组值无法对账到期间总量；"
                        "**不要**把它的贡献百分比当成加性分解来解读。"
                    ),
                }
            )
        if total_delta_is_zero and _has_material_component_change(
            deltas, baseline_total=baseline_total, current_total=current_total
        ):
            warnings.append(
                {
                    "code": "zero_total_delta_with_component_changes",
                    "dimension": dimension,
                    "message": (
                        f"维度 '{dimension}' 存在相互抵消的分量变化，而总变化量近似为 0；"
                        "请用**绝对增量**解读，不要用贡献百分比。"
                    ),
                }
            )

        non_additive = additivity["status"] == "failed"
        per_dimension[dimension] = {
            # 偏离 1：全部取回值（不是 top_n）—— 少一个值就会让对账凭空成立。
            "values": sorted(contributions, key=lambda item: abs(item["delta"]), reverse=True),
            "score": score,
            "non_additive": non_additive,
            "truncated": truncated,
            "reconciliation": {
                "baseline_residual": additivity["baseline_residual"] or 0.0,
                "current_residual": additivity["current_residual"] or 0.0,
                "passed": not non_additive,
            },
        }
        # 与参考实现同口径：非加性**或**已截断的维度都**不进排名**（截断 ⇒ 分组和不完整，
        # 贡献度不成立）。但 per_dimension 里保留取值，模型仍能看到最大变动项，
        # 并照 playbook 换成更粗的维度 —— 这正是本仓偏离 1 想要的效果。
        if non_additive or truncated:
            continue
        dimension_rankings.append(
            {"dimension": dimension, "score": score, "non_additive": False, "truncated": False}
        )
        all_contributions[dimension] = contributions

    dimension_rankings.sort(key=lambda ranking: ranking["score"] or 0.0, reverse=True)
    selected_dimensions = [ranking["dimension"] for ranking in dimension_rankings[:top_n_dimensions]]
    selected_contributions = [
        contribution for dimension in selected_dimensions for contribution in all_contributions[dimension]
    ]
    selected_contributions.sort(key=lambda contribution: abs(contribution["delta"]), reverse=True)

    strategy: Literal["term_wise", "unsupported"] = "term_wise"
    unsupported_reason: dict | None = None
    if not candidate_dimensions:
        strategy = "unsupported"
        unsupported_reason = {
            "code": "dimensions_required",
            "message": "归因至少需要一个候选维度。",
        }
    elif not dimension_rankings:
        strategy = "unsupported"
        unsupported_reason = {
            "code": "no_additive_dimensions",
            "message": "没有任何候选维度给出完整的加性分解（失败 / 非加性 / 已截断）。",
        }

    _logger.info(
        "归因完成：measure=%s 维度数=%d 已排名=%d 查询数=%d 警告=%s",
        measure,
        len(candidate_dimensions),
        len(dimension_rankings),
        queries_executed,
        [warning["code"] for warning in warnings],
    )

    return {
        "measure": measure,
        "strategy": strategy,
        "unsupported_reason": unsupported_reason,
        "total_change": {
            "baseline_value": baseline_total,
            "current_value": current_total,
            "delta": total_delta,
            "pct_change": (total_delta / abs(baseline_total) * 100 if baseline_total else None),
        },
        "dimension_ranking": dimension_rankings,
        "selected_dimensions": selected_dimensions,
        "top_dimension_values": selected_contributions[:top_n_values],
        "per_dimension": per_dimension,
        "warnings": warnings,
        "comparison_metadata": _comparison_metadata(
            request=request,
            baseline_days=baseline_days,
            current_days=current_days,
            queries_executed=queries_executed,
            max_values_per_dimension=max_values_per_dimension,
            top_n_dimensions=top_n_dimensions,
            top_n_values=top_n_values,
        ),
    }


def _comparison_metadata(
    *,
    request: Request,
    baseline_days: int,
    current_days: int,
    queries_executed: int,
    max_values_per_dimension: int,
    top_n_dimensions: int,
    top_n_values: int,
) -> dict:
    """把「这次实际用了什么口径」原样记下来，供复算与审计（方案 §1.2 参数透传）。"""
    metadata = {
        "base": "wren_cube",
        "cube": request.cube,
        "measure": request.measure,
        "db_name": request.db_name,
        "baseline": request.baseline.to_dict(),
        "current": request.current.to_dict(),
        "baseline_days": baseline_days,
        "current_days": current_days,
        "equal_length_windows": baseline_days == current_days,
        "time_dimension": request.time_dimension,
        "filters": [dict(item) for item in (request.filters or ())],
        "candidate_dimensions": list(request.candidate_dimensions or ()),
        "top_n_dimensions": top_n_dimensions,
        "top_n_values": top_n_values,
        # 语义与本仓之外的同名参数不同：**告警阈值**，不是 SQL 的 limit（见模块 docstring）。
        "max_values_per_dimension": max_values_per_dimension,
        "physical_grouped_row_limit": _PHYSICAL_GROUPED_ROW_LIMIT,
        "queries_executed": queries_executed,
    }
    metadata.update(request.metadata_extra or {})
    return metadata


# ══════════════════════════════════════════════════════════════════════
# 取数与解析
# ══════════════════════════════════════════════════════════════════════
def _fetch_total(
    runner: AttributionRunner,
    window: Window,
    *,
    measure: str,
    period: Period,
    warnings: list[dict],
) -> float:
    """跑一次不带维度的聚合并解析。两期总量是**致命**项：拿不到就没有归因。"""
    try:
        rows = runner.fetch_total(window)
    except TypedError:
        raise
    except Exception as error:  # noqa: BLE001 —— 驱动层异常一律收敛成类型化错误
        raise TypedError(
            "TOTAL_QUERY_FAILED", f"{period} 期总量查询失败：{error}", period=period
        ) from error
    return _parse_total(rows, measure=measure, period=period, warnings=warnings)


def _query_grouped_period(
    runner: AttributionRunner,
    *,
    dimension: str,
    period: Period,
    window: Window,
    limit: int,
    measure: str,
) -> tuple[list[dict], dict[JsonScalar | None, float] | None, TypedError | None]:
    """查一个维度的一期。**任何失败都只回错误、不抛出** —— 逐维故障隔离靠这个。"""
    try:
        rows = runner.fetch_grouped(dimension, window, limit)
    except TypedError as error:
        return [], None, error
    except Exception as error:  # noqa: BLE001
        _logger.warning("归因分组查询失败：维度=%s 期=%s：%s", dimension, period, error)
        return [], None, TypedError(
            "DIMENSION_QUERY_FAILED", str(error), period=period, dimension=dimension
        )
    try:
        lookup = _parse_grouped(rows, measure=measure, dimension=dimension, period=period)
    except TypedError as error:
        _logger.warning(
            "归因分组结果校验失败：维度=%s 期=%s：[%s] %s", dimension, period, error.code, error.message
        )
        return rows, None, error
    return rows, lookup, None


def _record_dimension_failure(*, dimension: str, error: TypedError, warnings: list[dict]) -> None:
    """单维失败只记警告；其余维度照常跑完（方案 §2.2 逐维故障隔离）。"""
    warnings.append(
        {
            "code": "dimension_analysis_failed",
            "dimension": dimension,
            "message": (
                f"维度 '{dimension}' 在 {error.period} 期无法分析：[{error.code}] {error.message}"
                "其余维度独立分析，不受影响。"
            ),
        }
    )


def _parse_total(
    rows: list[dict], *, measure: str, period: Period, warnings: list[dict]
) -> float:
    """两期总量：0 行 ⇒ 记 ``no_data_*`` 并按 0 处理；>1 行 ⇒ ``MULTI_ROW_TOTAL``。"""
    columns = _columns_of(rows)
    row_count = len(rows)
    if row_count > 1:
        raise TypedError(
            "MULTI_ROW_TOTAL",
            f"{period} 期总量查询返回 {row_count} 行，最多只能是 1 行。",
            period=period,
            columns=columns,
            row_count=row_count,
        )
    if row_count == 0:
        # 与参考实现同口径：0 行按 0 处理并记警告 —— 但「没有数据」与「真是 0」是两件事，
        # 所以下游读 pct_change 前必须先看警告（playbook 里那条纪律的由来）。
        warnings.append(
            {
                "code": f"no_data_{period}",
                "message": f"{period} 期总量查询没有返回任何行；总量按 0 处理。",
            }
        )
        return 0.0
    metric_column = _resolve_column(
        requested=measure,
        columns=columns,
        missing_code="MISSING_METRIC_COLUMN",
        period=period,
        row_count=row_count,
    )
    row = rows[0]
    if metric_column not in row:
        raise TypedError(
            "MISSING_METRIC_COLUMN",
            f"解析出的度量列 '{metric_column}' 不在 {period} 期的结果行里。",
            period=period,
            columns=columns,
            row_count=row_count,
        )
    value = row[metric_column]
    if value is None:
        warnings.append(
            {
                "code": f"null_total_{period}",
                "message": f"{period} 期总量是 NULL，按 0 处理；请先确认数据覆盖。",
            }
        )
        return 0.0
    numeric_value = _coerce_finite_metric(
        value, period=period, columns=columns, row_count=row_count
    )
    if numeric_value == 0:
        warnings.append(
            {
                "code": f"zero_or_no_data_{period}",
                "message": (
                    f"{period} 期总量是 0，无法区分「真的是 0」与「聚合输入为空」；"
                    "请先确认数据覆盖。"
                ),
            }
        )
    return numeric_value


def _parse_grouped(
    rows: list[dict], *, measure: str, dimension: str, period: Period
) -> dict[JsonScalar | None, float]:
    """分组结果 → ``{归一化取值: 度量值}``。

    **键就是归一化后的取值本身**：``None`` 代表 SQL NULL，与字面量字符串 ``'(null)'``
    是**两个成员**（方案 §2.2 —— 合并两者会算错一整类数据）。
    """
    columns = _columns_of(rows)
    row_count = len(rows)
    if row_count == 0:
        return {}
    metric_column = _resolve_column(
        requested=measure,
        columns=columns,
        missing_code="MISSING_METRIC_COLUMN",
        period=period,
        dimension=dimension,
        row_count=row_count,
    )
    dimension_column = _resolve_column(
        requested=dimension,
        columns=columns,
        missing_code="MISSING_DIMENSION_COLUMN",
        period=period,
        dimension=dimension,
        row_count=row_count,
    )

    values: dict[JsonScalar | None, float] = {}
    for row in rows:
        if metric_column not in row:
            raise TypedError(
                "MISSING_METRIC_COLUMN",
                f"解析出的度量列 '{metric_column}' 在 {period} 期的分组行里缺失。",
                period=period,
                dimension=dimension,
                columns=columns,
                row_count=row_count,
            )
        if dimension_column not in row:
            raise TypedError(
                "MISSING_DIMENSION_COLUMN",
                f"解析出的维度列 '{dimension_column}' 在 {period} 期的分组行里缺失。",
                period=period,
                dimension=dimension,
                columns=columns,
                row_count=row_count,
            )
        normalized = _normalize_dimension_value(row[dimension_column])
        if normalized in values:
            raise TypedError(
                "DUPLICATE_DIMENSION_KEY",
                (
                    f"维度 '{dimension}' 在 {period} 期返回了重复取值 "
                    f"'{_display_dimension_value(normalized)}'；"
                    "检查是否存在隐式时间粒度或多出的分组列。"
                ),
                period=period,
                dimension=dimension,
                columns=columns,
                row_count=row_count,
            )
        values[normalized] = _coerce_finite_metric(
            row[metric_column],
            period=period,
            dimension=dimension,
            columns=columns,
            row_count=row_count,
        )
    return values


def _columns_of(rows: Iterable[dict]) -> list[str]:
    """按**首次出现顺序**汇总所有列名（行可能参差）。"""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


# ══════════════════════════════════════════════════════════════════════
# 贡献度
# ══════════════════════════════════════════════════════════════════════
def _build_contributions(
    *,
    dimension: str,
    union_keys: list[JsonScalar | None],
    baseline_lookup: dict[JsonScalar | None, float],
    current_lookup: dict[JsonScalar | None, float],
    total_delta: float,
    total_delta_is_zero: bool,
    base_filters: Sequence[dict],
) -> list[dict]:
    """两期取值并集逐项做差；缺席的一侧按 0，并标 ``entered`` / ``exited``。"""
    contributions: list[dict] = []
    for key in union_keys:
        baseline_value = baseline_lookup.get(key, 0.0)
        current_value = current_lookup.get(key, 0.0)
        delta = current_value - baseline_value
        if key not in baseline_lookup:
            segment_kind = "entered"
        elif key not in current_lookup:
            segment_kind = "exited"
        else:
            segment_kind = "normal"
        item: dict = {
            "dimension": dimension,
            "value": _display_dimension_value(key),
            "baseline_value": baseline_value,
            "current_value": current_value,
            "delta": delta,
        }
        if not total_delta_is_zero:
            item["contribution_pct"] = delta / total_delta * 100
        # 反直觉点 ②：零总变化时上面那个键**整个缺席**，不是 None / 0 / inf。
        item["segment_kind"] = segment_kind
        item["drill_down"] = _drill_down(dimension, key, base_filters)
        contributions.append(item)
    return contributions


def _drill_down(dimension: str, value: JsonScalar | None, base_filters: Sequence[dict]) -> dict:
    """下钻条件：结构化 ``filters``（可当入参）+ 只读 ``where_sql``（渲染给人看）。

    ``filters`` = 父过滤 + 本维取值这一个条件 —— 与 playbook 那句「下钻时保留每一层父过滤」
    一致。``where_sql`` 渲染的是 ``filters`` 整体，所以无父过滤时它与参考实现逐字相同
    （``orders.region = 7`` / ``orders.region IS NULL``）。
    """
    condition = {
        "dimension": dimension,
        "operator": "is_null" if value is None else "eq",
        "value": None if value is None else value,
    }
    filters = [dict(item) for item in (base_filters or ())] + [condition]
    return {"filters": filters, "where_sql": render_where(filters)}


def render_where(filters: Sequence[dict]) -> str:
    """把结构化过滤渲染成只读 SQL 片段（**绝不作为入参接收**，方案 §1.2）。"""
    return " AND ".join(_render_predicate(item) for item in filters)


def _render_predicate(item: dict) -> str:
    dimension = item.get("dimension")
    operator = item.get("operator")
    value = item.get("value")
    if operator == "is_null":
        return f"{dimension} IS NULL"
    if operator == "is_not_null":
        return f"{dimension} IS NOT NULL"
    if operator in ("in", "not_in"):
        members = value if isinstance(value, (list, tuple)) else [value]
        rendered = ", ".join(_sql_literal(member) for member in members)
        return f"{dimension} {'IN' if operator == 'in' else 'NOT IN'} ({rendered})"
    sql_operator = _SQL_BINARY_OPERATOR.get(str(operator), "=")
    return f"{dimension} {sql_operator} {_sql_literal(value)}"


def _sql_literal(value: Any) -> str:
    """渲染字面量。**只用于展示**，不参与入参。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# ══════════════════════════════════════════════════════════════════════
# 对账与零变化
# ══════════════════════════════════════════════════════════════════════
def _check_additivity(
    *,
    baseline_total: float,
    current_total: float,
    baseline_values: list[float],
    current_values: list[float],
    total_delta_is_zero: bool,
) -> dict:
    """``Σ分组值`` vs 期间总量，容差 ``0.02``（方案 §2.2 可加性对账）。

    不可加维度（去重计数、比率）的「贡献度」是假的 ⇒ 宁可不出，不可出错。
    """
    baseline_sum = sum(baseline_values)
    current_sum = sum(current_values)
    baseline_residual = baseline_sum - baseline_total
    current_residual = current_sum - current_total
    baseline_tolerance = _ADDITIVITY_TOLERANCE * max(
        abs(baseline_total), sum(abs(value) for value in baseline_values)
    )
    current_tolerance = _ADDITIVITY_TOLERANCE * max(
        abs(current_total), sum(abs(value) for value in current_values)
    )
    status = (
        "passed"
        if abs(baseline_residual) <= baseline_tolerance
        and abs(current_residual) <= current_tolerance
        else "failed"
    )
    total_delta = current_total - baseline_total
    grouped_delta = current_sum - baseline_sum
    return {
        "status": status,
        "baseline_residual": baseline_residual,
        "current_residual": current_residual,
        "baseline_residual_pct": (
            baseline_residual / abs(baseline_total) * 100 if baseline_total != 0 else None
        ),
        "current_residual_pct": (
            current_residual / abs(current_total) * 100 if current_total != 0 else None
        ),
        "delta_residual_pct": (
            (grouped_delta - total_delta) / abs(total_delta) * 100 if not total_delta_is_zero else None
        ),
    }


def _is_effectively_zero_delta(
    total_delta: float, *, baseline_total: float, current_total: float
) -> bool:
    """总变化量相对量级是否可忽略（``1e-6``）。"""
    scale = max(abs(baseline_total), abs(current_total))
    return abs(total_delta) <= _ZERO_DELTA_EPSILON * scale if scale else total_delta == 0


def _has_material_component_change(
    deltas: list[float], *, baseline_total: float, current_total: float
) -> bool:
    """总变化为 0 时，是否仍有分量在动（对冲）⇒ 打 ``zero_total_delta_with_component_changes``。"""
    threshold = _ADDITIVITY_TOLERANCE * max(abs(baseline_total), abs(current_total))
    return any(abs(delta) > threshold for delta in deltas)


# ══════════════════════════════════════════════════════════════════════
# 列解析与取值归一化
# ══════════════════════════════════════════════════════════════════════
def column_leaf(column: str) -> str:
    """取列名叶子：``a.b`` → ``b``、``t__c`` → ``c``。

    编译侧（真实 runner）与内核必须**同一份**规则，所以这里导出它；
    需要的地方 import 本函数，**不要复制第二份**（否则两套规则会漂移）。
    """
    return column.rsplit(".", 1)[-1].rsplit("__", 1)[-1]


def _resolve_column(
    *,
    requested: str,
    columns: list[str],
    missing_code: str,
    period: Period,
    dimension: str | None = None,
    row_count: int | None = None,
) -> str:
    """先整名 ``casefold`` 匹配，再退到叶子名匹配 —— 两条通道都可能给别名带前缀。"""
    exact_matches = [column for column in columns if column.casefold() == requested.casefold()]
    if len(exact_matches) == 1:
        return exact_matches[0]

    requested_leaf = column_leaf(requested)
    leaf_matches = [
        column for column in columns if column_leaf(column).casefold() == requested_leaf.casefold()
    ]
    if len(leaf_matches) == 1:
        return leaf_matches[0]

    detail = "不存在" if not leaf_matches else f"有歧义（{'、'.join(leaf_matches)}）"
    kind = "度量" if missing_code == "MISSING_METRIC_COLUMN" else "维度"
    raise TypedError(
        missing_code,
        f"请求的{kind}列 '{requested}' 在 {period} 期结果里{detail}。",
        period=period,
        dimension=dimension,
        columns=columns,
        row_count=row_count,
    )


def _normalize_dimension_value(value: Any) -> JsonScalar | None:
    """把驱动层给回来的值收敛成可哈希、可比较的标量；``None`` / NaN 都归一成 ``None``。"""
    if value is None:
        return None
    try:
        if not isinstance(value, (str, bytes)) and math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):  # 必须在 int 之前：bool 是 int 的子类
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        normalized = float(value)
        return normalized if math.isfinite(normalized) else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def _display_dimension_value(value: JsonScalar | None) -> str:
    return "(null)" if value is None else str(value)


def _coerce_finite_metric(
    value: Any,
    *,
    period: Period,
    columns: list[str],
    row_count: int,
    dimension: str | None = None,
) -> float:
    """度量值必须是有限数 —— NULL / 字符串 / ``inf`` 一律拒（``NON_NUMERIC_METRIC_VALUE``）。"""
    if value is None:
        raise TypedError(
            "NON_NUMERIC_METRIC_VALUE",
            f"{period} 期分组行里的度量值是 NULL。",
            period=period,
            dimension=dimension,
            columns=columns,
            row_count=row_count,
        )
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        numeric_value = math.nan
    if not math.isfinite(numeric_value):
        raise TypedError(
            "NON_NUMERIC_METRIC_VALUE",
            f"度量值 {value!r} 不是有限数（{period} 期结果）。",
            period=period,
            dimension=dimension,
            columns=columns,
            row_count=row_count,
        )
    return numeric_value


__all__ = [
    "AttributionRunner",
    "FILTER_OPERATORS",
    "JsonScalar",
    "Request",
    "TypedError",
    "Window",
    "attribute",
    "column_leaf",
    "render_where",
    "validate_window",
]
