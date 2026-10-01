# -*- coding: utf-8 -*-
"""`attribution_analyze` 工具（S3）：把「两期差异」**分解**成各维度的贡献度。

**它做什么**（方案 §1.1 的 T1）：给定一个语义库、一个 cube、一组度量（`measures`）、
一个时间锚点与两期窗口，逐候选维度算出「本维度各取值的 `delta` + `contribution_pct`」，
并做加性对账、排名、生成结构化下钻条件。算法全在
[`attribution_core`](../utils/attribution_core.py)（纯函数、零 IO），取数全在
[`attribution_runner`](../utils/attribution_runner.py)（编译 + 执行），本模块只做
**入参解析 / 形状闸 / 拼装**。

**它不做什么**（方案 §1.1 的 T3 红线）：**不建立因果**。返回的是「贡献度分解」，
不是「为什么会降」—— 反事实推断单靠数据不可确定。工具描述里明写这条，
因为那是模型每次调用都会读到的地方（写在文档里没用）。

## 入参从哪来（方案 §1.2 的三级解析）

- `db_name`：显式入参 → 「答案指纹」(`answer_fingerprints`) 里的 `db_name` →
  `report_builder._current_db_name()`（`configurable.db_name` + 会话账本兜底）。
  ⚠️ **第 3 级必须走那个函数、不能只读 `configurable`**：主 agent 的活**永远**由
  同步循环触发的**续跑 run** 接着干，那条 run 的 `configurable` 是手拼的、`db_name`
  读成空（2026-09-26 生产实证）—— 本工具挂在主 agent 上，踩的是同一条路。
  三级全空 ⇒ `UNKNOWN_DB`，**不猜一个库**。
- `cube` / `measures` / `time_dimension` / `baseline` / `current`：显式入参 →
  指纹里的 `final_cube_spec.{cube,measures,time_dimension}` 与 `windows.{baseline,current}`。
  取不到就**要模型显式给**（`NO_FINGERPRINT`），**绝不从聊天史猜**（那正是幻觉的温床）。
  **`measures` 本来就是列表**（`normalize_cube_spec` 保序去重）⇒「上一轮问了几个
  指标，这次就归因几个」是直读。
- `candidate_dimensions`：缺省 = 该 cube 的全部 `dimensions`（模型该主动挑 —— 全取
  多半会撞查询预算，那时工具会把可用维度名列出来让它重试）。

## 本模块负责的闸：**度量形状闸**（§4.4）

按 `metadata.yml` 里度量表达式的**算子形状**判 `term_wise` / 比值 / `AVG` / 乘积 /
`COUNT(DISTINCT)`（`classify_measure_expression`，判据见 §2.3），**不是按度量名猜** ——
所以语义库里新增 measure 不需要改这里的代码。命中不支持形状 ⇒
`NON_TERM_WISE_MEASURE` / `UNSUPPORTED_MEASURE`，**一票否决整个调用**（§4.4）：
只算支持的那几个、把不支持的静默丢掉更坏 —— 模型会把它讲成「没有变化」。
错误文本里**点名**是哪个度量、什么形状、怎么改（去掉它重试 / 换一个指标）。

## 三道闸**全在本文件里**（方案 §4.5，S4）—— 因为没有中间件管得着这个工具

现成的 SQL 护栏全部按**工具名后缀**匹配（`sql_approval._is_run_sql_tool` / `_is_cube_tool`、
`query_gate._is_query_tool`）⇒ `attribution_analyze` 一条都不命中。所以：

- **闸① 只读硬闸**：在 [`attribution_runner._compile_checked`](../utils/attribution_runner.py)
  （编译产物过 `classify_sql`，`verdict == "write"` 即拒 —— 纵深防御，任何调用方都绕不过）。
- **闸② 判权**：`_caller_can_use_db(resolved_db)`，**在建连接 / 算 project_path 之前**调用
  （越权时连 `resolve_wren_ctx_by_db` 都不该跑）。
- **闸③ 入参白名单**：`_list_cubes` 判 cube 名、`_build_requests` 判
  `measures` / `candidate_dimensions` / `time_dimension` / `filters.{dimension,operator}` ——
  四个名字各自对**正确的集合**校验（`time_dimension` 对 `time_dimensions`、不是 `dimensions`），
  任一不命中就带名字拒答，**绝不拼进 SQL**。

由此本工具对「模型能往物理 SQL 里送什么」的回答是：**只有白名单内的列名和值**
（`value` 原样交给引擎，不做字符串拼接 —— 见 `render_filter_spec` 的实测记录）。

## 开关（§3.5，默认全关 ⇒ 工具拒绝执行，不建任何连接）

`NL2SQL_ATTRIBUTION_ENABLE`（关）/ `NL2SQL_ATTRIBUTION_MAX_VALUES`（500，告警阈值；
硬顶 1000）/ `NL2SQL_ATTRIBUTION_TOP_DIMS`（3）/ `NL2SQL_ATTRIBUTION_TOP_VALUES`（10）/
`NL2SQL_ATTRIBUTION_QUERY_BUDGET`（12，`2+2D` 超预算即拒 —— **按调用算、不乘度量个数**）。

## ⚠️ 本文件**不得**加 `from __future__ import annotations`（2026-09-30 生产事故）

`runtime: Annotated[ToolRuntime, InjectedToolArg()]` 的注入要过两层，两层的解析方式**不同**：

- `langgraph.prebuilt.tool_node._get_all_injected_args` 用 `get_type_hints(func, include_extras=True)`
  ⇒ **能解析字符串**，所以它照常把 runtime 注入进 tool_call；
- `langchain_core.tools.structured.StructuredTool._injected_args_keys` 用**未解析的**
  `signature(fn).parameters[k].annotation` 逐个 `_is_injected_arg_type()` ⇒ 字符串恒 False。

future-annotations 会把签名里的标注变成字符串 ⇒ 第二层认不出 runtime ⇒ 校验阶段把它当普通字段
丢掉 ⇒ 协程实际以「缺少 1 个必填位置参数 `runtime`」告终。生产实证（trace `476c977f13d460e5f9a3e9b3e9f8c268`）：
`_attribution_analyze_coro() missing 1 required positional argument: 'runtime'`。
同仓 `report_builder.py` / `check_progress.py` / `sync_launcher.py` 都没有这个 import、都正常。
`scripts/verify_attribution.py` 段⑰ 把「本文件的 runtime 注入对校验层可见」钉成了断言。
"""
import json
import logging
import os
import re
from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

from agent.utils.attribution_core import (
    FILTER_OPERATORS,
    Request,
    TypedError,
    Window,
)
# 实现已搬到 `agent.utils.wren_call_extract.list_cubes`（2026-09-30）：cube 车道软闸
# （middlewares/cube_route_gate.py）也要这份「目录列举 + 防路径穿越」，而中间件不该反向
# import 工具模块。别名保留原名 ⇒ 本模块内的调用点与 `verify_attribution` 的源码断言
# （要求看到 `_list_cubes(project_path)`）全部不变；语义不变：空集 ⇒ 成员判定必不过 ⇒
# 拒绝（与 `load_cube_metadata` 读不到同向：**fail-closed，不会因此放行**）。
# `wren_call_extract` 顶层只有 stdlib（其模块 docstring 明写），此处无循环 import 风险。
from agent.utils.wren_call_extract import list_cubes as _list_cubes

_logger = logging.getLogger(__name__)

# ── 开关（§3.5）───────────────────────────────────────────────
_ENV_ENABLE = "NL2SQL_ATTRIBUTION_ENABLE"
_ENV_MAX_VALUES = "NL2SQL_ATTRIBUTION_MAX_VALUES"
_ENV_TOP_DIMS = "NL2SQL_ATTRIBUTION_TOP_DIMS"
_ENV_TOP_VALUES = "NL2SQL_ATTRIBUTION_TOP_VALUES"
_ENV_BUDGET = "NL2SQL_ATTRIBUTION_QUERY_BUDGET"

# 与 `net_guard` 同一份真值集合（未设 / 不识别一律当关：**默认关**是硬要求）。
_TRUTHY = ("1", "true", "yes", "on")

# 参数缺省（与 §1.2 的签名逐字对齐）。
_DEFAULT_MAX_VALUES = 500
_DEFAULT_TOP_DIMS = 3
_DEFAULT_TOP_VALUES = 10
_DEFAULT_BUDGET = 12
# `max_values_per_dimension` 的硬顶（内核同一常量；超过的入参直接拒，§4.3 方框）。
_MAX_DIMENSION_VALUES = 1000

# 指纹 / state 的键名（与 `check_progress._build_answer_fingerprint` 同名同源）。
_STATE_FP_KEY = "answer_fingerprints"
_STATE_TASKS_KEY = "async_tasks"
_FP_DB = "db_name"
_FP_CUBE_SPEC = "final_cube_spec"
_FP_WINDOWS = "windows"


def attribution_enabled() -> bool:
    """归因总开关（默认 `off`）。未设 / 不识别 ⇒ False（fail-closed）。"""
    return os.environ.get(_ENV_ENABLE, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    """env 里的整数；未设 / 不是整数 / ≤ 0 ⇒ `default`（**不静默变成 0**）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("[attribution] %s=%r 不是整数，按默认 %d 处理", name, raw, default)
        return default
    return value if value > 0 else default


# ══════════════════════════════════════════════════════════════════════
# 度量表达式的算子形状（方案 §2.3）：按形状判，不按名字猜
# ══════════════════════════════════════════════════════════════════════
# 只认这两个聚合作为「加性构件」：SUM(...) / COUNT(*)。
_ADDITIVE_AGG_RE = re.compile(r"\b(SUM|COUNT)\s*\(", re.IGNORECASE)
_AVG_RE = re.compile(r"\bAVG\s*\(", re.IGNORECASE)
# 剥掉聚合之后**允许残留**的字符（数字、四则、括号、空白）—— 之外的一律算「认不出」。
_RESIDUE_OK_RE = re.compile(r"^[\s\d.+\-*()]*$")
# 聚合被替换成的占位串：不是 `0`（否则 `COUNT(*) * 1.0` 这种常数缩放的 `*` 会被
# 误判成「两个聚合相乘」——`0 * 1.0` 里那个 `0` 分不清是聚合还是字面量）。
_AGG_TOKEN = "@AGG@"
# 「两个聚合相乘」= 乘积型（§2.3 的 factor_shapley）。
_PRODUCT_RE = re.compile(r"@AGG@\s*\*\s*@AGG@")


def _strip_aggregates(text: str) -> str | None:
    """把每个 `SUM(...)` / `COUNT(...)` 调用整体替换成 `_AGG_TOKEN`；括号不配对 ⇒ None。

    **按括号配对整体吃掉的**（不是正则到第一个 `)`）——`SUM(CASE WHEN x THEN y ELSE 0 END)`
    里没有内嵌括号，但 `SUM(IF(a, b, c))` 有；吃半个调用会让后面的残留检查读到
    `ELSE 0 END)` 这类噪声，把可加度量误判成「认不出」而拒答。
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        match = _ADDITIVE_AGG_RE.match(text, i)
        if not match:
            out.append(text[i])
            i += 1
            continue
        depth = 0
        j = match.end() - 1  # 停在 `(`
        while j < n:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            return None
        out.append(_AGG_TOKEN)
        i = j + 1
    return "".join(out)


def classify_measure_expression(expression: str) -> str:
    """度量表达式 → 策略分类（`term_wise` / `mix_shift` / `factor_shapley` / `unsupported`）。

    **判定顺序是有讲究的**（照 §2.3 / §3.6 的实测分类表，不是为了好看）：

    1. 含 `/` ⇒ `mix_shift`。比值型 = 两个可加度量之比（`SUM(a)/SUM(b)`、
       `… / NULLIF(COUNT(*),0)`）。**这一条排在 `COUNT(DISTINCT)` 之前**：本库
       `avg_hours_per_user` 是 `SUM(work_hour) / NULLIF(COUNT(DISTINCT user_id), 0)`，
       §3.6 把它归在「比值」而不是「`DISTINCT` 不可加」—— 两者的实现路径不同
       （将来做 T2 时是两条路），混为一谈会误导后续工作。
    2. `AVG(` ⇒ `mix_shift`（`AVG` 在参考实现里走脱糖成比值的路径）。
    3. `DISTINCT` ⇒ `unsupported`（`COUNT(DISTINCT …)` 不可加）。
    4. 剥掉全部 `SUM(...)` / `COUNT(...)` 后，剩下的**只许**是数字与四则运算：
       - 出现「聚合 × 聚合」⇒ `factor_shapley`（乘积型）；
       - 出现别的字母（裸列名、`AVG`、`NULLIF`、`ROUND` 等没认出来的调用）⇒ `unsupported`；
       - 只剩数字与 `+ - *`（含 `COUNT(*) * 1.0` 这种常数缩放）⇒ `term_wise`。

    **失败的方向是「拒」**（认不出 ⇒ `unsupported`）：把一个其实可加的度量拒掉，
    代价是模型换个说法重试；把不可加的算成贡献度，模型会把它当结论讲给用户。
    空表达式同样 ⇒ `unsupported`（**不是**默认放行）。
    """
    text = str(expression or "").strip()
    if not text:
        return "unsupported"
    if "/" in text:
        return "mix_shift"
    if _AVG_RE.search(text):
        return "mix_shift"
    if re.search(r"\bDISTINCT\b", text, re.IGNORECASE):
        return "unsupported"
    residue = _strip_aggregates(text)
    if residue is None:
        return "unsupported"
    if _PRODUCT_RE.search(residue):
        return "factor_shapley"
    # 残留里的 `_AGG_TOKEN` 换成 `0` 再看出界字符：换之前先判乘积型（上面一条）。
    if not _RESIDUE_OK_RE.match(residue.replace(_AGG_TOKEN, "0")):
        return "unsupported"
    return "term_wise"


# 形状 → 错误码（§2.2 的「口径类」两个码；`term_wise` 不在表里）。
_SHAPE_ERROR_CODE = {
    "unsupported": "UNSUPPORTED_MEASURE",
    "mix_shift": "NON_TERM_WISE_MEASURE",
    "factor_shapley": "NON_TERM_WISE_MEASURE",
}
# 展示用的形状说明（进错误文本，让模型知道「为什么不行」）。
_SHAPE_LABEL = {
    "term_wise": "加性（可精确加总）",
    "mix_shift": "比值型（含 AVG）",
    "factor_shapley": "乘积型",
    "unsupported": "不可加（COUNT(DISTINCT) 等）",
}


# ══════════════════════════════════════════════════════════════════════
# 入参 schema（§1.2 的签名逐字对齐）
# ══════════════════════════════════════════════════════════════════════
# `operator` 的枚举**取自内核那一份** `FILTER_OPERATORS`，不复制第二份 ⇒ 不会漂移
# （§1.2：注入面按构造关闭的一半）。
# ⚠️ 类型是 `str` + `json_schema_extra={"enum": …}`，**不是** `Literal[*FILTER_OPERATORS]`：
# `json_schema_extra` **只进 JSON schema、不做校验**（段⑮ 有一条断言专门证明 `operator="like"`
# 构造得出来）⇒ 真正的闸是 `_build_requests` 里那道 `operator not in FILTER_OPERATORS`。
# 故意不用 `Literal`：那样非法值会在 pydantic 层抛异常（错误形状与其余拒答不一致），
# 而这里要的是同一套 `to_payload()` 机读形状 + 允许集合清单。
class AttributionFilter(BaseModel):
    """一条结构化过滤：`dimension` 限定为 cube 维度名，`value` 原样交给编译链。"""

    dimension: str = Field(description="cube 的维度名（必须是语义层声明里的名字）")
    operator: str = Field(
        description="比较算子：" + " / ".join(FILTER_OPERATORS),
        json_schema_extra={"enum": list(FILTER_OPERATORS)},
    )
    value: Any = Field(
        default=None,
        description="比较值；`in` / `not_in` 传列表，`is_null` / `is_not_null` 不传。",
    )


class AttributionAnalyzeSchema(BaseModel):
    """attribution_analyze 入参。除窗口外全部可缺省 —— 缺省值来自「答案指纹」。"""

    db_name: str | None = Field(
        default=None,
        description="（可选）语义库名，如 `witops`。不传则按「上一轮问的是哪个库」或当前会话选中的库解析。",
    )
    cube: str | None = Field(
        default=None,
        description="（可选）语义层 cube 名，如 `workhour_analysis`。不传则取上一轮问数用的 cube。",
    )
    measures: list[str] | None = Field(
        default=None,
        description=(
            "（可选）要归因的度量名**列表**，如 `[\"total_hours\", \"record_count\"]`。"
            "同一个 cube 的多个度量一次传完（成本与度量个数无关）；"
            "**跨 cube 的指标必须分次调用**（一次调用只编译一个 cube）。"
            "不传则取上一轮的指标。只有加性度量能算（SUM/COUNT 型）。"
        ),
    )
    time_dimension: str | None = Field(
        default=None,
        description="（可选）时间锚点维度名（cube 的 `time_dimensions` 里的名字，如 `work_date`）。",
    )
    baseline: list[str] | None = Field(
        default=None,
        description="（可选）基线期，两个 ISO 日期 `[start, end)`（半开区间），如 `[\"2026-07-01\", \"2026-08-01\"]`。",
    )
    current: list[str] | None = Field(
        default=None,
        description="（可选）当期，两个 ISO 日期 `[start, end)`（半开区间），如 `[\"2026-08-01\", \"2026-09-01\"]`。",
    )
    candidate_dimensions: list[str] | None = Field(
        default=None,
        description=(
            "（可选）要分解的维度名列表（建议 3~5 个）。不传则用该 cube 的全部维度 —— "
            "但维度太多会超出查询预算被拒，那时请按错误里列出的可用维度挑几个重试。"
            "**不要**选人名/工号/编号这类高基数字段。"
        ),
    )
    filters: list[AttributionFilter] | None = Field(
        default=None,
        description="（可选）全局过滤条件（两期同时生效），如只看某个事业部。",
    )
    fingerprint: str | None = Field(
        default=None,
        description="（可选）用哪条「答案指纹」（上一轮问数的 task_id）；不传则取最近一条。",
    )
    top_n_dimensions: int = Field(default=_DEFAULT_TOP_DIMS, description="返回排名前几的维度，默认 3。")
    top_n_values: int = Field(default=_DEFAULT_TOP_VALUES, description="每个维度返回排名前几的取值，默认 10。")
    max_values_per_dimension: int = Field(
        default=_DEFAULT_MAX_VALUES,
        description=(
            f"（可选）单维度取值数的**告警阈值**，默认 {_DEFAULT_MAX_VALUES}、硬顶 {_MAX_DIMENSION_VALUES}："
            "取值数超过它只会多一条高基数警告（**不丢值、不截断**）。"
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# 答案指纹（§4.9）：读侧就是一次 state 取值（工具与指纹同在主 agent）
# ══════════════════════════════════════════════════════════════════════
def _fingerprints(state: dict) -> dict:
    """state 里的指纹表；缺键 / 形状不对 ⇒ `{}`（**不是 None**，读取处一律 `.get`）。"""
    value = (state or {}).get(_STATE_FP_KEY)
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, dict)}


def _latest_task_id(fingerprints: dict, state: dict) -> str:
    """「最近一条」指纹的 task_id：优先按 `async_tasks` 的时间戳，排不出再按写入序。

    `_merge_dict_by_key` 是 `dict(existing); update(new)` ⇒ **新写的键在末尾**，
    所以「写入序的最后一条」是天然兜底。但并发多查询里，**重查一个旧任务**也会把它
    挪到末尾 —— 那时时间戳更准。时间戳缺失 / 格式不一（不同写者各写各的）时退回写入序：
    这只是「缺省取哪条」的启发式，模型随时可以用 `fingerprint` 显式指定。
    """
    keys = list(fingerprints)
    if not keys:
        return ""
    tasks = (state or {}).get(_STATE_TASKS_KEY) or {}

    def stamp(task_id: str) -> str:
        task = tasks.get(task_id) if isinstance(tasks, dict) else None
        if isinstance(task, dict):
            for field in ("last_updated_at", "last_checked_at", "created_at"):
                value = task.get(field)
                if value:
                    return str(value)
        return ""

    stamped = [(stamp(key), key) for key in keys]
    if any(value for value, _ in stamped):
        return max(stamped)[1]
    return keys[-1]


def _fingerprint_of(state: dict, wanted: str) -> dict:
    """取指定 / 最近一条指纹；`wanted` 指定了但没有 ⇒ 抛 `NO_FINGERPRINT`。"""
    fingerprints = _fingerprints(state)
    if wanted:
        item = fingerprints.get(str(wanted))
        if not item:
            raise TypedError(
                "NO_FINGERPRINT",
                f"没有找到 task_id={wanted!r} 的答案指纹；请去掉 `fingerprint` 用最近一条，"
                "或把 cube / measures / time_dimension / 两期区间显式传进来。",
            )
        return item
    task_id = _latest_task_id(fingerprints, state)
    return fingerprints.get(task_id, {}) if task_id else {}


def _resolve_db_name(explicit: str | None, fingerprint: dict) -> str:
    """`db_name` 三级解析（§1.2）：显式 → 指纹 → 当前会话库（含账本兜底）。全空 ⇒ 拒。"""
    if str(explicit or "").strip():
        return str(explicit).strip()
    from_fp = str(fingerprint.get(_FP_DB) or "").strip()
    if from_fp:
        return from_fp
    # 第 3 级**必须**用 report_builder 那份（`configurable.db_name` + 会话账本兜底）：
    # 主 agent 的活由续跑 run 接着干，那条 run 的 configurable 读不到 db_name。
    from agent.tools.report_builder import _current_db_name

    return str(_current_db_name() or "").strip()


# ══════════════════════════════════════════════════════════════════════
# 闸② 判权（§4.5）：工具是 agent 侧本地工具，**没有 REST 层替它判权**
# ══════════════════════════════════════════════════════════════════════
def _caller_can_use_db(db_name: str) -> bool:
    """当前调用者能不能碰 `db_name`（与问数侧同一套授权）。

    **照抄 `middlewares/query_gate.py:359-365` / `tool_filter.py:78` 的约定**，不自己
    发明一套：`resolve_caller()` 返回 `None` 的语义是「**不启用**按用户判权」（内部调用 /
    dev 旁路 / 读用户记录失败）⇒ 放行；返回 `dict` 才逐库判，不通过就是 `False`。
    两个被调函数各自已经把「读盘失败」收敛掉了（`resolve_caller` → `None`、
    `caller_can_access_db` → `True` + warning）⇒ 这里既不需要也不需要写 catch。

    ⚠️ 与方案 §4.5 原话「从 `runtime` 取不到 user ⇒ **fail-closed 拒绝**」**故意不同**
    （文档已按本实现更正）：那句会把 dev34、离线套件、以及任何没有 config 上下文的
    调用**整片**拒掉，而真正的越权（身份真实、账号在、就是没授权）本来就是
    `can_access_db` 的正常 `False` —— 该拒的照拒。工具内部自己 fail-closed 只会造出
    一个「别的工具能查、归因不能查」的孤岛，反而更难排查。
    """
    from agent.auth import runtime as _rt  # 惰性 import：与同文件其它跨界依赖同风格

    user = _rt.resolve_caller("attribution_analyze")
    if user is None:
        return True
    return _rt.caller_can_access_db(user, db_name, "attribution_analyze")


# （`_list_cubes` 的实现见本文件顶部 import 处的别名说明）


def _window_of(pair: Any, fingerprint: dict, which: str) -> Window | None:
    """一期窗口：显式 `[start, end]` → 指纹 `windows.<which>`；都缺 / 形状不对 ⇒ None。"""
    from_fp = fingerprint.get(_FP_WINDOWS)
    candidates = (pair, from_fp.get(which) if isinstance(from_fp, dict) else None)
    for item in candidates:
        if isinstance(item, (list, tuple)) and len(item) == 2 and all(str(x or "").strip() for x in item):
            return Window(str(item[0]).strip(), str(item[1]).strip())
    return None


def _normalize_names(values: Any) -> list[str]:
    """字符串列表 → 去重保序（与 `attribution_runner._normalize_measures` 同口径）。

    ⚠️ **也吃 dict**（取键）：`load_cube_metadata` 的 `measures` / `dimensions` 是
    `{名字: 描述}`，不是名字列表 —— 缺省 `candidate_dimensions = 该 cube 的全部维度`
    就是喂它，只认 list 会让「全部维度」静默变成**空集**，然后报成
    「语义层声明为空」（把工具自己的类型疏忽说成语义层的问题）。
    """
    if isinstance(values, dict):
        values = list(values)
    listed = [str(item).strip() for item in (values or ())] if isinstance(values, (list, tuple)) else []
    return list(dict.fromkeys(item for item in listed if item))


def _resolve_required(*, explicit: dict, fingerprint: dict, task_id: str) -> dict:
    """五样「显式 or 指纹」的东西：cube / measures / time_dimension / 两期窗口。

    **这一步排在读语义层之前**（虽然 cube 名是读元信息的入参）：这五样**一个都不需要
    读盘**就能判全，而「缺哪几样」要**一次报全** —— 先因缺 cube 报 `UNKNOWN_CUBE`、
    模型补上 cube 再来一次才报「还缺窗口」，是白烧一轮 LLM 的往返。

    ⚠️ 缺了给的是 `NO_FINGERPRINT` 而**不是** `UNKNOWN_CUBE`：这两者是不同的处置 ——
    前者让模型补齐参数重试，后者说明语义层里没有这个 cube（重试无意义）。所以下面
    「cube 名非空但盘上读不到」才是 `UNKNOWN_CUBE`，这里为空只是「没说清要算哪个」。
    """
    spec = fingerprint.get(_FP_CUBE_SPEC)
    spec = spec if isinstance(spec, dict) else {}
    resolved = {
        "cube": str(explicit.get("cube") or spec.get("cube") or "").strip(),
        "measures": _normalize_names(explicit.get("measures")) or _normalize_names(spec.get("measures")),
        "time_dimension": str(
            explicit.get("time_dimension") or spec.get("time_dimension") or ""
        ).strip(),
        "baseline": _window_of(explicit.get("baseline"), fingerprint, "baseline"),
        "current": _window_of(explicit.get("current"), fingerprint, "current"),
    }
    missing = [name for name, value in resolved.items() if not value]
    if missing:
        where = (
            f"（已读到 task_id={task_id} 的指纹，但里面没有 {', '.join(missing)}）"
            if fingerprint else "（本轮 state 里没有答案指纹）"
        )
        raise TypedError(
            "NO_FINGERPRINT",
            f"归因需要 {', '.join(missing)}；{where}"
            "请把它们显式传进来：cube / measures / time_dimension / baseline / current"
            "（两期都是半开区间 [start, end)，ISO 日期）。**不要**用「上个月」这类相对说法，"
            "也不要从聊天记录里猜具体日期。",
        )
    return resolved


def _build_requests(
    *,
    db_name: str,
    payload: dict,
    resolved: dict,
    explicit: dict,
    top_n_dimensions: int,
    top_n_values: int,
    max_values_per_dimension: int,
    filters: list[dict],
    budget: int,
    task_id: str,
) -> list[Request]:
    """三道形状/白名单闸 + `Request` 列表（一个度量一条 Request，同一查询组）。

    `payload` 是 `load_cube_metadata` 的返回（**进程内**读的同一份语义层声明）：
    `measures` / `dimensions` / `time_dimensions` 是白名单，`measure_expressions`
    判算子形状（§2.3）；`resolved` 是上面 `_resolve_required` 已经判全的五样。

    本函数里的拒答**全部点名**（`MISSING_TIME_DIMENSION` / `MISSING_DIMENSION` /
    `MISSING_MEASURE` / 形状类 / `INVALID_FILTER`），并把**可用清单**写进载荷的
    附加键（`time_dimensions` / `dimensions` / `measures`）—— 模型据此改一个名字就能重试，
    不用「猜着改一遍、再被拒一遍」。
    """
    cube = resolved["cube"]
    measures = resolved["measures"]
    baseline = resolved["baseline"]
    current = resolved["current"]
    time_dimension = resolved["time_dimension"]
    declared_dims = _normalize_names(payload.get("dimensions"))
    declared_time_dims = _normalize_names(payload.get("time_dimensions"))

    # ── 闸③-1：`time_dimension` 对 **`time_dimensions`** 的键集（§4.5 那张表）──
    # ⚠️ 与 `dimensions` 是**两个平级**列表、不是子集关系：拿 `dimensions` 判会把
    # `work_date`（工时库唯一的时间锚点）判成非法维度。
    if time_dimension not in set(declared_time_dims):
        where = (
            f"cube {cube!r} 没有声明任何时间维度（`time_dimensions` 为空）"
            if not declared_time_dims
            else f"时间锚点 {time_dimension!r} 不在 cube {cube!r} 的时间维度里"
        )
        error = TypedError(
            "MISSING_TIME_DIMENSION",
            f"{where}。归因必须把两期窗口下推成 SQL 谓词（锚点会当过滤列用），"
            "没有合法锚点就只能对**全部历史**做归因 —— 已拒绝。"
            + (f"该 cube 可用的时间维度：{', '.join(declared_time_dims)}。" if declared_time_dims else ""),
        )
        result = error.to_payload()
        result["time_dimensions"] = declared_time_dims
        raise _PayloadError(result)

    # ── 闸③-2：显式给的候选维度对 `dimensions` 的键集 ──
    # 只判**显式**的那一份：缺省那份是从 `declared_dims` 直接来的，天然是子集。
    # 名字写错（用了中文标签 / 物理列名 / 别的 cube 的维度）在编译期会变成引擎的
    # 「Unknown dimension」，但那时错误文本里没有可用清单，模型只能瞎猜着改。
    explicit_dims = _normalize_names(explicit.get("candidate_dimensions"))
    if explicit_dims:
        unknown_dims = [name for name in explicit_dims if name not in set(declared_dims)]
        if unknown_dims:
            error = TypedError(
                "MISSING_DIMENSION",
                f"这些维度不在 cube {cube!r} 的维度声明里：{', '.join(unknown_dims)}。"
                f"该 cube 声明的维度是：{', '.join(declared_dims) or '（空）'}。"
                "要用**语义层里的技术名**（不是中文标签，也不是物理列名）。",
            )
            result = error.to_payload()
            result["dimensions"] = declared_dims
            raise _PayloadError(result)

    # 缺省 = 该 cube 的全部维度（§1.2）。全取多半会撞查询预算 —— 下面会拿可用维度名
    # 回话，让模型挑几个重试（比「你少传了参数」有用）。
    candidates = explicit_dims or declared_dims
    if not candidates:
        raise TypedError("UNKNOWN_CUBE", f"cube {cube!r} 没有可用维度（语义层声明为空）。")
    # 查询预算**先在这里判一次**，只为把可用维度名写进错误文本（内核那道是权威，
    # 本处不改口径、不放松：同一个 `2+2D > budget` 判据）。
    needed = 2 + 2 * len(candidates)
    if needed > budget:
        hint = (
            f"该 cube 声明的维度是：{', '.join(declared_dims)}。请挑 3~5 个业务维度重试"
            if not explicit_dims else "请减少候选维度重试"
        )
        raise TypedError(
            "INVALID_ARGUMENT",
            f"候选维度太多：{len(candidates)} 个维度需要 {needed} 条聚合查询，超出预算 {budget}。{hint}。",
        )

    missing_measures: list[str] = []
    offenders: dict[str, str] = {}
    shape_codes: set[str] = set()
    declared_measures = payload.get("measures") or {}
    expressions = payload.get("measure_expressions") or {}
    for measure in measures:
        if measure not in declared_measures:
            missing_measures.append(measure)
            continue
        shape = classify_measure_expression(expressions.get(measure, ""))
        if shape != "term_wise":
            offenders[measure] = shape
            shape_codes.add(_SHAPE_ERROR_CODE.get(shape, "UNSUPPORTED_MEASURE"))
    if missing_measures or offenders:
        if missing_measures:
            code = "MISSING_MEASURE"
            detail = f"这些度量不在 cube {cube!r} 的度量声明里：{', '.join(missing_measures)}。"
        elif shape_codes == {"UNSUPPORTED_MEASURE"}:
            code = "UNSUPPORTED_MEASURE"
            detail = "这些度量不可加（去重计数类）"
        else:
            code = "NON_TERM_WISE_MEASURE"
            detail = "这些度量不是加性的（比值 / 平均 / 乘积）"
        if offenders:
            detail += "：" + "、".join(
                f"`{name}`（{_SHAPE_LABEL.get(shape, shape)}）" for name, shape in offenders.items()
            ) + "。"
        error = TypedError(
            code,
            f"{detail}M1 只做加性度量（SUM/COUNT 型）；去掉它们后可重试，或换一个可加的指标。"
            "**不会只算支持的那几个** —— 少算的那个会被讲成「没有变化」，比拒答坏得多。",
        )
        result = error.to_payload()
        result["measures"] = dict(offenders, **{name: "not_found" for name in missing_measures})
        raise _PayloadError(result)

    # ── 闸③-3：`filters` 的两个白名单（§4.5 表格末行）──
    # `value` **原样交给引擎**：不拼字符串、不自己转义（转义是 wren 的活 —— 实测 `'` 被
    # wren 按 `''` 处理，自己再转一遍只会把安全值转坏，见 `render_filter_spec` 的记录）。
    # ⚠️ 维度白名单是 `dimensions ∪ time_dimensions`：窗口下推本身就是同一形状的过滤
    # （`work_date:gte:…`，引擎按维度名解析），把 `work_date` 判成非法会让
    # 「只归因 2026 年」这类**合理**请求无法表达（runner 里那条 AND 叠加的日志也是为它写的）。
    allowed_filter_dims = set(declared_dims) | set(declared_time_dims)
    for item in filters:
        operator = str((item or {}).get("operator") or "").strip()
        dimension = str((item or {}).get("dimension") or "").strip()
        if operator not in FILTER_OPERATORS:
            raise TypedError(
                "INVALID_FILTER",
                f"过滤算子 {operator!r} 不在允许集合内：{list(FILTER_OPERATORS)}。",
            )
        if dimension not in allowed_filter_dims:
            raise TypedError(
                "INVALID_FILTER",
                f"过滤维度 {dimension!r} 不在 cube {cube!r} 的声明里。"
                f"可用：{', '.join(sorted(allowed_filter_dims)) or '（空）'}。"
                "`filters.dimension` 用语义层里的维度名（时间维度也可以）。",
            )

    return [
        Request(
            measure=measure,
            baseline=baseline,
            current=current,
            candidate_dimensions=candidates,
            time_dimension=time_dimension,
            cube=cube,
            db_name=db_name,
            filters=filters,
            top_n_dimensions=top_n_dimensions,
            top_n_values=top_n_values,
            max_values_per_dimension=max_values_per_dimension,
            query_budget=budget,
            # 参数透传进 `comparison_metadata`（§2.2）：出问题时要能复算「这次算的是谁」。
            metadata_extra={"fingerprint": task_id or None, "source": "attribution_analyze"},
        )
        for measure in measures
    ]


class _PayloadError(Exception):
    """已经**组装好的**错误载荷（形状 = `TypedError.to_payload()` + 附加键）。

    只用于「错误载荷比 `TypedError` 多带了结构化信息」这一种情况（比如点名是哪几个度量）；
    其余错误一律用 `TypedError` 本身。
    """

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__(str(payload.get("message") or ""))


def _refuse(payload: dict) -> str:
    """错误载荷 → 工具返回值（JSON 文本，形状与内核 `TypedError.to_payload()` 一致）。"""
    return json.dumps(payload, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════
# 工具主路径
# ══════════════════════════════════════════════════════════════════════
async def _attribution_analyze_coro(
    runtime: Annotated[ToolRuntime, InjectedToolArg()],
    db_name: str | None = None,
    cube: str | None = None,
    measures: list[str] | None = None,
    time_dimension: str | None = None,
    baseline: list[str] | None = None,
    current: list[str] | None = None,
    candidate_dimensions: list[str] | None = None,
    filters: list[AttributionFilter] | None = None,
    fingerprint: str | None = None,
    top_n_dimensions: int = _DEFAULT_TOP_DIMS,
    top_n_values: int = _DEFAULT_TOP_VALUES,
    max_values_per_dimension: int = _DEFAULT_MAX_VALUES,
) -> str:
    """解析入参 → 形状闸 → 编译执行 → 逐度量贡献度分解（JSON 文本返回）。"""
    # 闸：总开关（默认关）。**在解析任何入参之前**：关着的时候连库名都不该去算。
    if not attribution_enabled():
        return _refuse(TypedError(
            "ATTRIBUTION_DISABLED",
            f"归因分析未启用（{_ENV_ENABLE} 未设为 {_TRUTHY[0]}/{_TRUTHY[1]}/{_TRUTHY[2]}/{_TRUTHY[3]} 之一）。"
            "这是**按设计拒绝**，不是工具故障：请直接用 run_sql / query_cube 查两期数据对比，"
            "不要重试本工具。",
        ).to_payload())
    if max_values_per_dimension > _MAX_DIMENSION_VALUES:
        return _refuse(TypedError(
            "INVALID_ARGUMENT",
            f"max_values_per_dimension={max_values_per_dimension} 超过硬顶 {_MAX_DIMENSION_VALUES}。",
        ).to_payload())

    try:
        state = runtime.state or {}
    except Exception as e:  # noqa: BLE001  state 不可读 ⇒ 明确报错，不猜指纹
        _logger.warning("[attribution] runtime.state 读取失败: %s", e)
        return _refuse(TypedError("INVALID_ARGUMENT", "无法读取当前对话状态，归因无法确认入参。").to_payload())

    explicit = {
        "cube": cube, "measures": measures, "time_dimension": time_dimension,
        "baseline": baseline, "current": current, "candidate_dimensions": candidate_dimensions,
    }
    try:
        fp = _fingerprint_of(state, str(fingerprint or ""))
        task_id = str(fingerprint or "") or _latest_task_id(_fingerprints(state), state)
        resolved_db = _resolve_db_name(db_name, fp)
        if not resolved_db:
            raise TypedError(
                "UNKNOWN_DB",
                "解析不出要归因的库（显式 db_name / 答案指纹 / 当前会话选中的库三级都空）。"
                "请显式给出 db_name —— **不会猜一个库**：拿错库的语义层去解释另一个库的数字，"
                "比拒答坏得多。",
            )
        # 闸② 判权（§4.5）：**建连接 / 算 project_path 之前** —— 越权时连
        # `resolve_wren_ctx_by_db` 都不该跑（它会读注册表、可能物化语义层版本）。
        if not _caller_can_use_db(resolved_db):
            raise TypedError(
                "DB_NOT_AUTHORIZED",
                f"当前账号无权访问库 {resolved_db!r}，归因已拒绝（与问数同一套授权）。"
                "换一个你有权限的库（`db_name` 显式传）或让管理员授权；**重试同一个库没有用**。",
            )

        # 项目路径与 cube 元信息：进程内读 `<project>/cubes/<cube>/metadata.yml`，
        # 不经 MCP（先例 build_report）—— 所以要的 `expression` / `time_dimensions`
        # 是**同一份声明**，不会与执行侧看到的语义层不一致。
        from agent.utils.wren_call_extract import load_cube_metadata, resolve_wren_ctx_by_db

        project_path, _conn = resolve_wren_ctx_by_db(resolved_db)
        if not project_path:
            raise TypedError(
                "UNKNOWN_DB",
                f"库 {resolved_db!r} 解析不出 wren 语义层项目路径（未建模 / 未接入）。",
            )
        # 五样先判全（**不读盘**）⇒ 缺什么一次报全，别让模型「补一个、再被拒一次」。
        resolved = _resolve_required(explicit=explicit, fingerprint=fp, task_id=task_id)
        cube_name = resolved["cube"]
        # 闸③-a：cube 名对 `cubes/` 的**目录名集合**判成员（§4.5 表格首行）。
        # 必须排在 `load_cube_metadata` 之前：cube 名要拼进文件路径，先判成员
        # 「`cube="../../x"` 越出项目目录读别的 yml」这条路径才不存在。
        available_cubes = _list_cubes(project_path)
        if cube_name not in available_cubes:
            raise TypedError(
                "UNKNOWN_CUBE",
                f"cube {cube_name!r} 不在库 {resolved_db!r} 的语义层里。"
                f"可用的 cube：{', '.join(available_cubes) or '（读不到 cubes/ 目录）'}。"
                "**跨 cube 的多个指标要分次调用**（一次调用只编译一个 cube）。",
            )
        meta = load_cube_metadata(project_path, cube_name)
        if not meta:
            # 目录在、`metadata.yml` 读不出来 / 解析失败 —— 与「名字不在 cubes/ 里」分开报，
            # 因为下一步动作不同（这个是语义库的问题，改名字没用）。
            raise TypedError(
                "UNKNOWN_CUBE",
                f"cube {cube_name!r} 的声明读不出来或解析失败"
                f"（cubes/{cube_name}/metadata.yml 存在但读/解析失败，库 {resolved_db!r}）。",
            )

        budget = _env_int(_ENV_BUDGET, _DEFAULT_BUDGET)
        request_list = _build_requests(
            db_name=resolved_db,
            payload=meta,
            resolved=resolved,
            explicit=explicit,
            top_n_dimensions=top_n_dimensions or _DEFAULT_TOP_DIMS,
            top_n_values=top_n_values or _DEFAULT_TOP_VALUES,
            max_values_per_dimension=max_values_per_dimension,
            filters=[item.model_dump() for item in (filters or [])],
            budget=budget,
            task_id=task_id,
        )

        from agent.utils.attribution_runner import get_runner, run_attribution

        runner = get_runner(
            resolved_db,
            cube=cube_name,
            measures=[request.measure for request in request_list],
            time_dimension=request_list[0].time_dimension or "",
            filters=request_list[0].filters,
        )
        if runner is None:
            raise TypedError(
                "UNKNOWN_DB",
                f"库 {resolved_db!r} 拿不到 wren 项目路径 / 连接（未建模或已下线）。",
            )
        result = await run_attribution(runner, request_list)
    except _PayloadError as e:
        return _refuse(e.payload)
    except TypedError as e:
        return _refuse(e.to_payload())
    except Exception as e:  # noqa: BLE001  工具故障：机读码 + 原始原因（不猜、不美化）
        _logger.warning("[attribution] 归因执行失败", exc_info=True)
        return _refuse({
            "error_type": "attribution_tool_error",
            "code": "INTERNAL_ERROR",
            "message": f"{type(e).__name__}: {e}",
            "period": None, "dimension": None, "columns": [], "row_count": None,
        })

    # 取证：这次到底打了几条 SQL（`attribution_runner` 的 query_log 只进日志与
    # comparison_metadata 不在这里 —— 结果载荷的键集是恒定六键，§4.3）。
    for entry in runner.query_log:
        _logger.info(
            "[attribution] SQL(%s) %s → %d 行（%dms%s）：%s",
            resolved_db, entry["label"], entry["rows"], entry["ms"],
            "，命中缓存" if entry["cached"] else "", entry["sql"],
        )
    return json.dumps(result, ensure_ascii=False)


attribution_analyze_tool = StructuredTool.from_function(
    coroutine=_attribution_analyze_coro,
    name="attribution_analyze",
    description=(
        "把一个指标的**两期差异分解**到各维度上（贡献度分解）："
        "给定 cube、指标、时间锚点与两期区间，返回每个维度取值的增量、占比、"
        "以及可直接复算的下钻条件（结构化 filters）。"
        "**只做贡献度分解，不解释「为什么」** —— 输出里没有、也不许编造任何原因或因果结论："
        "「A 部门贡献了 -80% 的降幅」是数据，「因为 A 部门人走了」不是本工具能给的。"
        "调用前先确认指标与两期区间：用户说「刚才那个数归因一下」时可以省略 "
        "cube/measures/time_dimension/baseline/current，工具会取上一轮问数的答案指纹，"
        "取不到会要求你显式给出 —— **不要**从聊天记录里猜日期。"
        "指标一次传一组（measures 列表，同 cube 多指标成本不变）；跨 cube 的多个指标要**分次调用**。"
        "只有加性指标（SUM/COUNT 型）能算，比值/平均/去重计数会被明确拒答（不会只算一部分）。"
        "候选维度请主动挑 3~5 个业务维度，不要选人名/工号这类高基数字段。"
    ),
    args_schema=AttributionAnalyzeSchema,
)
