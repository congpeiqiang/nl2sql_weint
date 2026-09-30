# -*- coding: utf-8 -*-
"""归因内核（`attribution_core.attribute`）+ 答案指纹（S8）的契约验证（离线，无库 / MCP / 网络 / LLM）。

对象是 `src/agent/utils/attribution_core.py` —— 移植自 Datus 的
`GenericAttributeAnalyzer`（`attribution_utils.py:88-953`），落地设计见
`docs/agent优化记录/归因分析方案.md` §4.2（内核）/ §4.3（取数）/ §4.9（答案指纹）/ §4.7（本脚本）。

**为什么能离线验**：内核与外界只有一个两方法的协议（`fetch_total` / `fetch_grouped`，
都返回「行 dict 列表」），所以换一个桩 Runner 就能把**全部护栏**跑通 —— 不需要真库、
不需要 LLM、可进 CI。把算法错误留在这里，比留到接上真库之后便宜一个数量级。

**本脚本钉住的三处刻意偏离**（方案 §3.1 / §4.3，与参考实现不同，且**不许**被「顺手改回去」）：

1. **截断口径**：物理上限恒为 1001 行（`plan_cube_sql` 写死 `LIMIT 1001`，不接受传参），
   `max_values_per_dimension` **只是告警阈值**。段④的两条断言（1001 行 ⇒ `truncated`；
   `max_values_per_dimension=1` ⇒ **不** `truncated` 且 `values` 一个不少）专门钉死这条。
2. **`values` 不裁**：`per_dimension.<dim>.values` 是本次取回的全部取值（裁它就等于让
   「Σ分组 = 总量」凭空成立、把可加维度误判成不可加）；`top_n_values` 只裁
   `top_dimension_values`。
3. **下钻是结构化 `filters`**：`where_sql` 降级为只读渲染（无父过滤时与参考实现逐字相同）。

⚠️ 只校验**本地代码**：线上跑的是发版镜像里的 `src`，生产对照必须 `docker exec md5sum`。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_attribution.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import json
import os
import pathlib
import re
import sys
import tempfile

# 与仓内 verify 脚本同款：先钉临时根。本脚本自己不写账本，但 M2 段落（⑨~⑫）会碰到
# 工具层与判权（auth.sqlite），所以从一开始就用**强钉**而不是 setdefault。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-attribution-")
os.environ["AGENT_DATA_ROOT"] = _TMP_ROOT

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.utils.attribution_core import (  # noqa: E402
    Request,
    TypedError,
    Window,
    _PHYSICAL_GROUPED_ROW_LIMIT,
    attribute,
)

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ══════════════════════════════════════════════════════════════════════
# 桩 Runner：两个方法，记录调用序列（**与真实实现签名完全一致**）
# ══════════════════════════════════════════════════════════════════════
class StubRunner:
    """脚本化取数：`totals` / `grouped` 按调用顺序弹一条；条目可以是 Exception。"""

    def __init__(self, totals: list, grouped: list) -> None:
        self._totals = list(totals)
        self._grouped = list(grouped)
        self.total_calls = 0
        self.grouped_calls: list[dict] = []

    def fetch_total(self, window: Window) -> list[dict]:
        self.total_calls += 1
        item = self._pop(self._totals, f"total#{self.total_calls}")
        if isinstance(item, Exception):
            raise item
        return item

    def fetch_grouped(self, dimension: str, window: Window, limit: int) -> list[dict]:
        self.grouped_calls.append(
            {"dimension": dimension, "limit": limit, "window": window.to_dict()}
        )
        item = self._pop(self._grouped, f"grouped#{len(self.grouped_calls)} dim={dimension}")
        if isinstance(item, Exception):
            raise item
        return item

    @staticmethod
    def _pop(pool: list, label: str):
        if not pool:
            raise AssertionError(f"桩脚本被多调用了一次：{label}（说明查询条数超出预期）")
        return pool.pop(0)

    def drained(self) -> bool:
        return not self._totals and not self._grouped


def _req(**overrides) -> Request:
    base = dict(
        measure="rev",
        baseline=Window("2026-01-01", "2026-01-08"),
        current=Window("2026-01-08", "2026-01-15"),
        candidate_dimensions=["region"],
    )
    base.update(overrides)
    return Request(**base)


def _codes(payload: dict) -> list[str]:
    return [warning["code"] for warning in payload["warnings"]]


def _deltas(payload: dict, dimension: str) -> list[float]:
    return [value["delta"] for value in payload["per_dimension"][dimension]["values"]]


# ══════════════════════════════════════════════════════════════════════
# ⓪ 夹具自检：桩可构造、主流程跑得通
# ══════════════════════════════════════════════════════════════════════
def s0_fixture_sanity() -> None:
    section("⓪ 夹具自检：桩 Runner + 主流程")
    runner = StubRunner(
        [[{"rev": 30}], [{"rev": 50}]],
        [[{"region": 7, "rev": 30}], [{"region": 7, "rev": 50}]],
    )
    payload = attribute(runner, _req())
    check(payload["total_change"]["delta"] == 20, "★ 主流程跑通：total_change.delta = 20")
    check(
        {"measure", "strategy", "unsupported_reason", "total_change", "dimension_ranking",
         "selected_dimensions", "top_dimension_values", "per_dimension", "warnings",
         "comparison_metadata"} <= set(payload),
        "★ 返回键集含方案 §1.2 的八个字段 + unsupported_reason",
        sorted(payload),
    )
    check(runner.drained(), "负对照：桩脚本被**恰好**消费（多调一次就会 AssertionError）")
    check(
        set(payload["total_change"]) == {"baseline_value", "current_value", "delta", "pct_change"},
        "total_change 键名逐字对齐参考实现",
    )
    check(
        payload["comparison_metadata"]["physical_grouped_row_limit"] == _PHYSICAL_GROUPED_ROW_LIMIT
        == 1001,
        "comparison_metadata 记下物理行数上限 = 1001（截断口径的判据来源）",
    )


# ══════════════════════════════════════════════════════════════════════
# ① 查询序列：2 + 2D，limit = max_values + 1
# ══════════════════════════════════════════════════════════════════════
def s1_query_sequence() -> None:
    section("① 查询序列：`2 + 2D` 条，limit = max_values_per_dimension + 1")
    runner = StubRunner(
        [[{"rev": 30}], [{"rev": 50}]],
        [
            [{"region": 7, "rev": 10}, {"region": None, "rev": 20}],
            [{"region": 7, "rev": 30}, {"region": None, "rev": 20}],
        ],
    )
    payload = attribute(runner, _req(max_values_per_dimension=25))
    check(runner.total_calls == 2, "★ fetch_total 恰 2 次（两期各一次）", runner.total_calls)
    check(len(runner.grouped_calls) == 2, "★ fetch_grouped 恰 2D 次（D=1）", len(runner.grouped_calls))
    check(
        [call["limit"] for call in runner.grouped_calls] == [26, 26],
        "★ limit = 25 + 1（判截断的协议值，照参考实现 `[None, None, 26, 26]` 同构）",
    )
    check(
        [call["window"] for call in runner.grouped_calls]
        == [{"start": "2026-01-01", "end": "2026-01-08"},
            {"start": "2026-01-08", "end": "2026-01-15"}],
        "两期窗口分别落到 baseline / current（半开区间原样透传）",
    )
    check(payload["comparison_metadata"]["queries_executed"] == 4, "queries_executed == 2 + 2D = 4")
    check(runner.drained(), "桩脚本被恰好消费（查询条数不多不少）")
    values = payload["per_dimension"]["region"]["values"]
    check(values[0]["drill_down"]["where_sql"] == "region = 7",
          "非 NULL 取值下钻渲染 `region = 7`（与参考实现逐字相同）",
          values[0]["drill_down"]["where_sql"])
    check(values[1]["drill_down"]["where_sql"] == "region IS NULL",
          "NULL 取值下钻渲染 `region IS NULL`（不是 `= '(null)'`）")

    # D=2：4 条分组查询
    runner2 = StubRunner(
        [[{"rev": 100}], [{"rev": 150}]],
        [
            [{"region": "US", "rev": 100}], [{"region": "US", "rev": 150}],
            [{"channel": "direct", "rev": 100}], [{"channel": "direct", "rev": 150}],
        ],
    )
    payload2 = attribute(runner2, _req(candidate_dimensions=["region", "channel"]))
    check(len(runner2.grouped_calls) == 4, "D=2 ⇒ fetch_grouped 恰 4 次", len(runner2.grouped_calls))
    check(payload2["comparison_metadata"]["queries_executed"] == 6, "D=2 ⇒ queries_executed == 6")
    check(
        [call["dimension"] for call in runner2.grouped_calls] == ["region", "region", "channel", "channel"],
        "维度顺序 = 入参顺序，且同一维两期相邻（顺序执行，不许并发）",
    )


# ══════════════════════════════════════════════════════════════════════
# ② 加性对账：Σdelta == total delta（精确相等）
# ══════════════════════════════════════════════════════════════════════
def s2_additivity_exact() -> None:
    section("② 加性对账：`Σ per_dimension.<dim>.values[].delta == total_change.delta`")
    runner = StubRunner(
        [[{"rev": 100}], [{"rev": 150}]],
        [
            [{"region": "US", "rev": 60}, {"region": "EU", "rev": 40}],
            [{"region": "US", "rev": 90}, {"region": "EU", "rev": 60}],
            [{"channel": "direct", "rev": 100}],
            [{"channel": "direct", "rev": 150}],
        ],
    )
    payload = attribute(runner, _req(candidate_dimensions=["region", "channel"]))
    check(payload["total_change"]["delta"] == 50, "total delta = 150 - 100 = 50")
    for dimension in ("region", "channel"):
        total = sum(_deltas(payload, dimension))
        check(abs(total - 50) < 1e-9,
              f"★ 维度 {dimension}：Σdelta 与总量精确相等（差 {total - 50}）")
    check(payload["per_dimension"]["region"]["non_additive"] is False,
          "负对照：对得上的维度 non_additive=False（不是恒 True）")
    check(payload["per_dimension"]["region"]["reconciliation"]["passed"] is True,
          "reconciliation.passed=True")
    check("non_additive_dimension" not in _codes(payload), "对账通过 ⇒ 没有 non_additive_dimension 警告")
    check(payload["selected_dimensions"] == ["channel", "region"],
          "排序按 score 降序：channel 1.0 > region 0.6", payload["selected_dimensions"])
    first = payload["per_dimension"]["region"]["values"][0]
    check("contribution_pct" in first and abs(first["contribution_pct"] - 60.0) < 1e-9,
          "负对照：总变化非零时 contribution_pct **存在**（与段⑥形成对照）")


# ══════════════════════════════════════════════════════════════════════
# ③ 不可加维度：踢出排名
# ══════════════════════════════════════════════════════════════════════
def s3_non_additive_not_ranked() -> None:
    section("③ 不可加维度：`Σ分组 ≠ 总量` ⇒ 警告 + 不进 dimension_ranking")
    runner = StubRunner(
        [[{"rev": 100}], [{"rev": 100}]],
        [
            [{"region": "US", "rev": 60}, {"region": "EU", "rev": 50}],
            [{"region": "US", "rev": 70}, {"region": "EU", "rev": 40}],
        ],
    )
    payload = attribute(runner, _req())
    detail = payload["per_dimension"]["region"]
    check(detail["non_additive"] is True, "★ non_additive=True（基期分组和 110 ≠ 总量 100）")
    check(abs(detail["reconciliation"]["baseline_residual"] - 10) < 1e-9,
          "reconciliation.baseline_residual == 10", detail["reconciliation"]["baseline_residual"])
    check(detail["reconciliation"]["passed"] is False, "reconciliation.passed == False")
    check("non_additive_dimension" in _codes(payload), "★ 打了 non_additive_dimension 警告")
    check(payload["dimension_ranking"] == [], "★ 不进 dimension_ranking")
    check(payload["selected_dimensions"] == [], "不进 selected_dimensions")
    check(payload["strategy"] == "unsupported"
          and payload["unsupported_reason"]["code"] == "no_additive_dimensions",
          "全维不可加 ⇒ strategy=unsupported / no_additive_dimensions")
    check(len(detail["values"]) == 2, "负对照：不可加维度的取值**仍然给**（只是不给结论）")


# ══════════════════════════════════════════════════════════════════════
# ④ 截断 / 高基数（本仓偏离 1 的正面）
# ══════════════════════════════════════════════════════════════════════
def s4_truncation_and_threshold() -> None:
    section("④ 截断 / 高基数：真截断看物理上限 1001，不看 max_values_per_dimension")
    # 1001 行：V0 是主因（3000 → 100，Δ=-2900），V1..V1000 各 1
    baseline_rows = [{"region": "V0", "rev": 3000}] + [
        {"region": f"V{i}", "rev": 1} for i in range(1, 1001)
    ]
    current_rows = [{"region": "V0", "rev": 100}] + [
        {"region": f"V{i}", "rev": 1} for i in range(1, 1001)
    ]
    runner = StubRunner([[{"rev": 4000}], [{"rev": 1100}]], [baseline_rows, current_rows])
    payload = attribute(runner, _req())
    detail = payload["per_dimension"]["region"]
    check(len(baseline_rows) == 1001, "夹具：两期各返回 1001 行（恰好触到物理上限）")
    check(detail["truncated"] is True, "★ 返回行数 ≥ 1001 ⇒ truncated=True")
    check("high_cardinality_dimension" in _codes(payload), "★ 同打 high_cardinality_dimension")
    check(payload["dimension_ranking"] == [],
          "★ 被截断的维度不进排名（分组和不完整 ⇒ 贡献度不成立）")
    check(abs(detail["values"][0]["delta"] - (-2900)) < 1e-9,
          "★ 主因不被截掉：|Δ| 最大的收缩项排第一（Δ=-2900）",
          detail["values"][0]["delta"])
    check(detail["non_additive"] is False,
          "负对照：本例算术对得上 ⇒ 排除排名的**唯一**原因是 truncated（不是 non_additive）")
    check(abs(payload["per_dimension"]["region"]["score"] - 1.0) < 1e-9,
          "score = max|Δ| / |Δtotal| = 2900 / 2900 = 1.0")
    check(payload["strategy"] == "unsupported"
          and payload["unsupported_reason"]["code"] == "no_additive_dimensions",
          "唯一维度被截断 ⇒ 没有可排名维度 ⇒ strategy=unsupported（不是工具故障）")

    # 负对照：max_values_per_dimension=1 ⇒ **不**截断、取值一个不少，只多一条警告
    runner2 = StubRunner(
        [[{"rev": 100}], [{"rev": 150}]],
        [
            [{"region": "A", "rev": 60}, {"region": "B", "rev": 40}],
            [{"region": "B", "rev": 100}, {"region": "C", "rev": 50}],
        ],
    )
    payload2 = attribute(runner2, _req(max_values_per_dimension=1))
    detail2 = payload2["per_dimension"]["region"]
    check(detail2["truncated"] is False,
          "★ 负对照：阈值=1（3 个取值）**不**标 truncated —— 阈值只告警，不改截断口径")
    check(len(detail2["values"]) == 3, "★ 负对照：`values` 一个不少（3 个取值全在）",
          len(detail2["values"]))
    check("high_cardinality_dimension" in _codes(payload2), "同一条路径仍会打高基数警告")
    check([call["limit"] for call in runner2.grouped_calls] == [2, 2],
          "协议值仍是 max_values + 1 = 2（进不了 SQL，只用于与参考实现同构）")
    check(payload2["dimension_ranking"] == [{"dimension": "region", "score": 1.2,
                                            "non_additive": False, "truncated": False}],
          "★ 负对照的反面：未截断 ⇒ 正常进排名（否则上一段断言可能恒真）",
          payload2["dimension_ranking"])
    check(
        [item["value"] for item in detail2["values"]] == ["A", "B", "C"],
        "排序按 |Δ| 降序（A 与 B 都是 60，稳定序取并集顺序）",
        [item["value"] for item in detail2["values"]],
    )

    # values 不裁：15 个取值 + top_n_values=10
    runner3 = StubRunner(
        [[{"rev": 150}], [{"rev": 300}]],
        [
            [{"region": f"V{i}", "rev": 10} for i in range(15)],
            [{"region": f"V{i}", "rev": 20} for i in range(15)],
        ],
    )
    payload3 = attribute(runner3, _req(top_n_values=10))
    check(len(payload3["per_dimension"]["region"]["values"]) == 15,
          "★ `per_dimension.<dim>.values` 不裁 top_n_values（15 个全在）",
          len(payload3["per_dimension"]["region"]["values"]))
    check(len(payload3["top_dimension_values"]) == 10,
          "★ 只有 `top_dimension_values`（跨维展示摘要）裁到 top_n_values=10",
          len(payload3["top_dimension_values"]))


# ══════════════════════════════════════════════════════════════════════
# ⑤ 取值进出 + NULL ≠ 字符串
# ══════════════════════════════════════════════════════════════════════
def s5_segments_and_null() -> None:
    section("⑤ 进出段 + `NULL` 与字面量 `'(null)'` 是两个成员")
    runner = StubRunner(
        [[{"rev": 40}], [{"rev": 45}]],
        [
            [{"region": "keep", "rev": 20}, {"region": "old", "rev": 10}, {"region": None, "rev": 10}],
            [{"region": "keep", "rev": 25}, {"region": "O'Reilly", "rev": 20}],
        ],
    )
    payload = attribute(runner, _req())
    by_value = {item["value"]: item for item in payload["per_dimension"]["region"]["values"]}
    check(by_value["keep"]["segment_kind"] == "normal", "两期都在 ⇒ normal")
    check(by_value["old"]["segment_kind"] == "exited", "只在基期 ⇒ exited")
    check(by_value["(null)"]["segment_kind"] == "exited", "SQL NULL 只在基期 ⇒ exited")
    check(by_value["O'Reilly"]["segment_kind"] == "entered", "只在当期 ⇒ entered")
    check(
        by_value["O'Reilly"]["drill_down"]["where_sql"] == "region = 'O''Reilly'",
        "★ 单引号按 SQL 规则转义（`O''Reilly`）",
        by_value["O'Reilly"]["drill_down"]["where_sql"],
    )
    check(abs(sum(_deltas(payload, "region")) - 5) < 1e-9,
          "进出项一起参与对账：ΣΔ = 5 = 45 - 40")

    # NULL vs 字面量 '(null)'：同一维里两个成员
    runner2 = StubRunner(
        [[{"rev": 30}], [{"rev": 40}]],
        [
            [{"kind": None, "rev": 20}, {"kind": "(null)", "rev": 10}],
            [{"kind": None, "rev": 30}, {"kind": "(null)", "rev": 10}],
        ],
    )
    payload2 = attribute(runner2, _req(candidate_dimensions=["kind"]))
    items = payload2["per_dimension"]["kind"]["values"]
    check(len(items) == 2, "★ 两个成员**没有**被合并（按取值本身做键，不是按显示文本）", len(items))
    check(items[0]["drill_down"]["filters"][-1] == {"dimension": "kind", "operator": "is_null",
                                                    "value": None},
          "★ SQL NULL ⇒ 结构化过滤走 `is_null`", items[0]["drill_down"]["filters"][-1])
    check(items[0]["drill_down"]["where_sql"] == "kind IS NULL", "…渲染 `kind IS NULL`")
    check(items[1]["drill_down"]["filters"][-1] == {"dimension": "kind", "operator": "eq",
                                                    "value": "(null)"},
          "★ 字面量 '(null)' ⇒ 走 `eq` 且值是**字符串**", items[1]["drill_down"]["filters"][-1])
    check(items[1]["drill_down"]["where_sql"] == "kind = '(null)'", "…渲染 `kind = '(null)'`")
    check(items[0]["value"] == items[1]["value"] == "(null)",
          "两者的 `value` 显示文本相同（所以判据必须看 `filters`，不能看显示值）")

    # 父过滤：drill_down.filters 是「父过滤 + 本维条件」，where_sql 渲染整体
    runner3 = StubRunner(
        [[{"rev": 100}], [{"rev": 150}]],
        [[{"region": "US", "rev": 100}], [{"region": "US", "rev": 150}]],
    )
    payload3 = attribute(runner3, _req(filters=[{"dimension": "if_approve", "operator": "eq",
                                                 "value": 1}]))
    drill = payload3["per_dimension"]["region"]["values"][0]["drill_down"]
    check(len(drill["filters"]) == 2 and drill["filters"][0]["dimension"] == "if_approve",
          "★ 下钻条件保留父过滤（playbook 那条纪律的结构化版本）", drill["filters"])
    check(drill["where_sql"] == "if_approve = 1 AND region = 'US'",
          "`where_sql` 渲染 filters 整体（只读展示）", drill["where_sql"])


# ══════════════════════════════════════════════════════════════════════
# ⑥ 零变化：contribution_pct 整个键缺席
# ══════════════════════════════════════════════════════════════════════
def s6_zero_total_delta() -> None:
    section("⑥ 零变化：`contribution_pct` **整个键缺席**（不是 None / 0 / inf）")
    runner = StubRunner(
        [[{"rev": 0}], [{"rev": 0}]],
        [
            [{"region": "US", "rev": 10}, {"region": "EU", "rev": -10}],
            [{"region": "US", "rev": 20}, {"region": "EU", "rev": -20}],
        ],
    )
    payload = attribute(runner, _req())
    detail = payload["per_dimension"]["region"]
    values = detail["values"]
    check(all("contribution_pct" not in item for item in values),
          "★ 零总变化 ⇒ 每个取值都没有 contribution_pct 键")
    check(not any("inf" in repr(item) for item in values),
          "负对照：整个取值里找不到 inf（除以 0 会被模型讲成「无限大贡献」）")
    check(detail["score"] is None, "score=None（分母是 0，排名时按 0 兜底）")
    check(abs(sum(_deltas(payload, "region"))) < 1e-9, "对冲的两项 Δ 相加为 0")
    check("zero_total_delta_with_component_changes" in _codes(payload),
          "★ 打 zero_total_delta_with_component_changes（提示用绝对增量解读）")
    check(payload["total_change"]["pct_change"] is None, "total_change.pct_change=None")
    check(payload["strategy"] == "term_wise", "零变化不是失败：策略仍是 term_wise")

    # 负对照：总变化非零时该键存在（否则上一条断言可能恒真）
    runner2 = StubRunner(
        [[{"rev": 10}], [{"rev": 30}]],
        [[{"region": "US", "rev": 10}], [{"region": "US", "rev": 30}]],
    )
    payload2 = attribute(runner2, _req())
    check("contribution_pct" in payload2["per_dimension"]["region"]["values"][0],
          "负对照：总变化 20 时 contribution_pct 存在")


# ══════════════════════════════════════════════════════════════════════
# ⑦ 逐维故障隔离
# ══════════════════════════════════════════════════════════════════════
def s7_dimension_isolation() -> None:
    section("⑦ 逐维故障隔离：一维炸掉，其余维度结果完整")
    runner = StubRunner(
        [[{"rev": 100}], [{"rev": 150}]],
        [
            RuntimeError("ambiguous organization column"),
            [{"channel": "direct", "rev": 100}],
            [{"channel": "direct", "rev": 150}],
        ],
    )
    payload = attribute(runner, _req(candidate_dimensions=["orders.organization", "channel"]))
    check("orders.organization" not in payload["per_dimension"], "★ 失败维度不进 per_dimension")
    check("dimension_analysis_failed" in _codes(payload), "★ 打了 dimension_analysis_failed 警告")
    check(payload["warnings"][-1]["code"] == "dimension_analysis_failed",
          "警告落在最后（与参考实现同序）")
    check(payload["selected_dimensions"] == ["channel"], "★ 其余维度照常出结果")
    check(payload["comparison_metadata"]["queries_executed"] == 5,
          "查询计数含失败的那一次：2 + 1（失败即退出该维）+ 2 = 5",
          payload["comparison_metadata"]["queries_executed"])
    check(payload["strategy"] == "term_wise", "有维度成功 ⇒ 策略仍是 term_wise")

    # 负对照：失败维度不出现在 selected_dimensions，也不被算成低分维度
    check("orders.organization" not in payload["selected_dimensions"],
          "负对照：失败维度不在维度排名里（不是「低分维度」）")

    # 维度校验失败（而不是查询异常）同样被隔离
    runner2 = StubRunner(
        [[{"rev": 100}], [{"rev": 150}]],
        [
            [{"region": "A", "rev": 10}, {"region": "A", "rev": 20}],
            [{"kind": "direct", "rev": 100}],
            [{"kind": "direct", "rev": 150}],
        ],
    )
    payload2 = attribute(runner2, _req(candidate_dimensions=["region", "kind"]))
    check("region" not in payload2["per_dimension"], "解析型失败同样只影响该维")
    check(any("DUPLICATE_DIMENSION_KEY" in warning["message"] for warning in payload2["warnings"]),
          "警告正文里带得出类型化错误码（可机读）", payload2["warnings"][-1]["message"][:70])


# ══════════════════════════════════════════════════════════════════════
# ⑧ 错误码（失败要能机读）
# ══════════════════════════════════════════════════════════════════════
def _expect_error(runner: StubRunner, request: Request, code: str, label: str) -> None:
    try:
        attribute(runner, request)
    except TypedError as error:
        check(error.code == code, label, error.code)
    else:
        check(False, label, "居然没抛错")


def s8_error_codes() -> None:
    section("⑧ 错误码：窗口 / 总量 / 度量值 / 重复键 / 入参")

    for start, end, label in (
        ("2026-01-01", "2026-01-01", "空区间"),
        ("2026-01-02", "2026-01-01", "反序"),
        ("not-a-date", "2026-01-01", "非 ISO 日期"),
    ):
        runner = StubRunner([], [])
        _expect_error(
            runner,
            _req(baseline=Window(start, end)),
            "INVALID_TIME_WINDOW",
            f"★ {label} ⇒ INVALID_TIME_WINDOW",
        )
        check(runner.total_calls == 0 and runner.grouped_calls == [],
              f"负对照（{label}）：窗口不合法 ⇒ **一条查询都没发**")

    runner = StubRunner([[{"rev": 10}, {"rev": 20}], [{"rev": 30}]], [])
    try:
        attribute(runner, _req())
    except TypedError as error:
        check(error.code == "MULTI_ROW_TOTAL" and error.period == "baseline",
              "★ 总量返回 2 行 ⇒ MULTI_ROW_TOTAL（带 period）", f"{error.code}/{error.period}")
    else:
        check(False, "★ 总量返回 2 行 ⇒ MULTI_ROW_TOTAL")

    runner = StubRunner([[{"rev": "abc"}], [{"rev": 10}]], [])
    _expect_error(runner, _req(), "NON_NUMERIC_METRIC_VALUE", "★ 总量不是有限数 ⇒ NON_NUMERIC_METRIC_VALUE")

    runner = StubRunner(
        [[{"rev": 100}], [{"rev": 150}]],
        [[{"region": "A", "rev": 100}], [{"region": "A", "rev": None}]],
    )
    payload = attribute(runner, _req())
    check(any("NON_NUMERIC_METRIC_VALUE" in warning["message"] for warning in payload["warnings"]),
          "★ 分组值为 NULL ⇒ 走逐维隔离（不致命），码写进警告正文")

    runner = StubRunner([], [])
    _expect_error(runner, _req(max_values_per_dimension=5000), "INVALID_ARGUMENT",
                  "★ max_values_per_dimension 超硬顶 1000 ⇒ INVALID_ARGUMENT")
    _expect_error(runner, _req(max_values_per_dimension=0), "INVALID_ARGUMENT",
                  "★ max_values_per_dimension=0 ⇒ INVALID_ARGUMENT")
    check(runner.total_calls == 0, "负对照：入参不合法 ⇒ 也没发查询")

    runner = StubRunner([], [])
    try:
        attribute(runner, _req(candidate_dimensions=["a", "b", "c"], query_budget=6))
    except TypedError as error:
        check(error.code == "INVALID_ARGUMENT" and "候选维度太多" in error.message,
              "★ 3 个维度需 8 条 > 预算 6 ⇒ 拒答并回告「候选维度太多」", error.message)
    else:
        check(False, "★ 预算超限应当拒答")
    check(runner.total_calls == 0, "★ 预算闸在**任何查询之前**（fail-closed，不建连接）")

    runner = StubRunner([[{"rev": 10}], [{"rev": 20}]], [])
    payload = attribute(runner, _req(candidate_dimensions=[]))
    check(runner.total_calls == 2 and payload["strategy"] == "unsupported",
          "无候选维度仍先取两期总量（参考实现同序），策略=unsupported")
    check(payload["unsupported_reason"]["code"] == "dimensions_required",
          "unsupported_reason.code == dimensions_required")

    runner2 = StubRunner([[], [{"rev": 10}]], [])
    payload2 = attribute(runner2, _req(candidate_dimensions=[]))
    check("no_data_baseline" in _codes(payload2) and payload2["total_change"]["baseline_value"] == 0,
          "基期 0 行 ⇒ no_data_baseline 且总量按 0")
    runner3 = StubRunner([[{"rev": 0}], [{"rev": 10}]], [])
    payload3 = attribute(runner3, _req(candidate_dimensions=[]))
    check("zero_or_no_data_baseline" in _codes(payload3),
          "基期总量为 0 ⇒ zero_or_no_data_baseline（不区分真 0 与空聚合）")


# ══════════════════════════════════════════════════════════════════════
# S8 答案指纹（改的是既有链路 check_progress + MainAgentState，与内核无代码交集）
# ══════════════════════════════════════════════════════════════════════
def _cube_msg(filters, cube: str = "workhour_analysis") -> dict:
    """造一条含 Cube 调用的 AI 消息（`extract_last_cube_call` 只认 AIMessage + query_cube）。"""
    return {
        "role": "ai",
        "content": "",
        "tool_calls": [{
            "name": "wrenai_demo_query_cube",
            "id": "cube-1",
            "args": {"cube": cube, "measures": ["total_hours"], "filters": filters},
        }],
    }


def s12_answer_fingerprint() -> None:
    section("⑫ S8 答案指纹：字段与 manifest 同名同源；只从「比较上下文」抽两期区间")
    from agent.subagents import check_progress as cp

    # ── 1. 日期序列 → 两期区间：只认两种可判形状，其余一律 None（不猜）──
    check(cp._windows_from_dates(["2026-08-01", "2026-09-01", "2026-10-01"])
          == {"baseline": ["2026-08-01", "2026-09-01"],
              "current": ["2026-09-01", "2026-10-01"]},
          "3 个日期 = 相邻两期（基线结束 = 当期开始，日期只出现一次）")
    check(cp._windows_from_dates(["2026-05-01", "2026-06-01", "2026-08-01", "2026-09-01"])
          == {"baseline": ["2026-05-01", "2026-06-01"],
              "current": ["2026-08-01", "2026-09-01"]},
          "4 个日期 = 两段互不相接的区间")
    check(cp._windows_from_dates(["2026-08-01", "2026-09-01"]) is None,
          "负对照：2 个日期 = 只有一期（没有对照期）⇒ None")
    check(cp._windows_from_dates([]) is None
          and cp._windows_from_dates(
              ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"]) is None,
          "负对照：0 个 / 5 个日期 ⇒ None（>4 分不清哪两对是工期）")
    check(cp._windows_from_dates(["2026-09-01", "2026-08-01", "2026-07-01"]) is None,
          "负对照：倒序（start >= end）⇒ None，**不自行排序**")
    check(cp._windows_from_dates(
              ["2026-01-01", "2026-09-01", "2026-02-01", "2026-03-01"]) is None,
          "负对照：两段重叠（baseline.end > current.start）⇒ None")

    # ── 2. 从 SQL 抽：**只认比较上下文**的日期（裸字面量不算）──
    _one = ("SELECT SUM(work_hour) FROM v_workhour "
            "WHERE work_date >= '2026-08-01' AND work_date < '2026-09-01'")
    check(cp._extract_windows([], _one) is None,
          "负对照：SQL 只有一期（2 个日期）⇒ windows 是 None（不是 [] / {}）")
    _union = ("SELECT SUM(work_hour) AS v FROM v_workhour "
              "WHERE work_date >= '2026-08-01' AND work_date < '2026-09-01' "
              "UNION ALL SELECT SUM(work_hour) FROM v_workhour "
              "WHERE work_date >= '2026-09-01' AND work_date < '2026-10-01'")
    _exp3 = {"baseline": ["2026-08-01", "2026-09-01"],
             "current": ["2026-09-01", "2026-10-01"]}
    check(cp._extract_windows([], _union) == _exp3,
          "两期并列的 SQL（共享边界日期出现两次）⇒ 去重后按相邻两期切分")
    _bare = "SELECT '2026-01-01' AS tag, " + _union
    check(cp._extract_windows([], _bare) == _exp3,
          "裸日期字面量（`SELECT '2026-01-01'`）不参与比较上下文 ⇒ 结论不变")
    _hits = cp._ISO_DATE_RE.findall(_bare)
    _naive = [d for i, d in enumerate(_hits) if d not in _hits[:i]]
    check(len(_naive) == 4 and cp._windows_from_dates(_naive) != _exp3,
          "（负对照前提）不做比较上下文过滤 ⇒ 裸正则去重后正好 4 个日期，"
          "切出的是**另一个**（把裸字面量当基期起的）错区间")
    check(cp._extract_windows([], "SELECT 1") is None,
          "负对照：SQL 里没有日期 ⇒ None")

    # ── 3. Cube 通道：日期在 filters 实参里，走同一道形状闸 ──
    _flt2 = cp._extract_windows(
        [_cube_msg(["work_date:gte:2026-08-01", "work_date:lt:2026-09-01"])], "")
    check(_flt2 is None,
          "负对照：cube filters 只有一期（2 个日期）⇒ None（不硬凑两期）")
    check(cp._extract_windows(
              [_cube_msg(["if_approve:eq:1", "story_count:gte:20"])], "") is None,
          "负对照：cube filters 里没有日期 ⇒ None")
    check(cp._extract_windows(
              [_cube_msg({"work_date": "gte:2026-08-01", "end_date": "lt:2026-09-01",
                          "cmp_date": "gte:2026-10-01"})], "") == _exp3,
          "cube filters（dict 形态）按取值顺序走同一道 3 日期形状闸")
    check(cp._extract_windows([_cube_msg(["work_date:gte:2026-01-01"])], _union) == _exp3,
          "有最终 SQL 时**不**再看 cube filters（SQL 优先，避免两处日期打架）")

    # ── 4. 字段血缘：与磁盘 manifest 同名同源（防两套账）──
    from agent.utils.process_audit import build_process_manifest

    _res = {"sql": _one, "dialect_sql_file": "physical/x.sql"}
    _man = build_process_manifest(
        messages=[], session_thread_id="s", sub_thread_id="t",
        question_id="q", user_question="u", db_name="db",
        status="success", producing_sql=_one, result=_res,
    )
    _fp = cp._build_answer_fingerprint(_res, [], "t")
    _biz = {"db_name", "question_id", "user_question", "final_sql", "final_sql_digest",
            "final_cube_spec", "sql_origin", "physical_sql_file"}
    check(set(_fp) - {"windows"} == _biz,
          "指纹键集 = 8 个业务字段 + 归因专用 windows（逐字按方案 §4.9）")
    check(set(_fp) - {"windows"} <= set(_man),
          "★ 指纹字段名 ⊆ manifest 字段名（两处账本不许漂移）")
    check(_fp["final_sql"] == _man["final_sql"] == _one
          and _fp["final_sql_digest"] == _man["final_sql_digest"] != "",
          "final_sql / digest 与 manifest 同源（同一份 SQL、同一摘要函数）")
    check(_fp["sql_origin"] == _man["sql_origin"] == "llm_from_schema",
          "sql_origin 与 manifest 同源（有 SQL 无 cube 调用 ⇒ llm_from_schema）")
    check(_fp["physical_sql_file"] == _man["physical_sql_file"] == "physical/x.sql",
          "physical_sql_file 与 manifest 同源（同一个指针函数）")
    check(_fp["final_cube_spec"] == _man["final_cube_spec"] == {},
          "非 cube 路径下 final_cube_spec 两边都是空 dict")
    check(_fp["windows"] is None, "单期 SQL ⇒ 指纹里 windows 逐字是 None")
    check(cp._build_answer_fingerprint(
              {**_res, "sql_origin": "cube_metric"}, [], "t")["sql_origin"] == "cube_metric",
          "成功路径已判过的 sql_origin 直接复用（不在指纹里重判一遍）")

    # ── 5. 写回：内部键必须 pop（ToolMessage 是给模型看的），state 更新必须有 ──
    from deepagents.middleware.async_subagents import _build_check_command

    _task = {"task_id": "t1", "agent_name": "nl2sql", "thread_id": "th", "run_id": "r1",
             "status": "success", "created_at": "2026-09-30T00:00:00Z",
             "last_updated_at": "2026-09-30T00:00:00Z"}
    _with_fp = {**_res, "status": "success", cp._FP_KEY: {"db_name": "db"}}
    _cmd = _build_check_command(_with_fp, _task, "call-1")
    _msg = _cmd.update["messages"][0]
    check(cp._FP_KEY not in _msg.content and "db_name" not in _msg.content,
          "内部键已 pop：ToolMessage 正文里没有指纹（否则会被模型当成可用输入）")
    check(_cmd.update.get("answer_fingerprints") == {"t1": {"db_name": "db"}},
          "★ 指纹随 Command.update 按 task_id 键控写回 state")
    check(_cmd.update["async_tasks"]["t1"]["task_id"] == "t1"
          and "async_tasks" in _cmd.update,
          "既有 async_tasks 写回未受影响（同一份 result 两条通道并行）")

    _cmd2 = _build_check_command({**_res, "status": "success"}, _task, "call-2")
    check("answer_fingerprints" not in _cmd2.update,
          "负对照：result 没带指纹 ⇒ update 里不出现 answer_fingerprints（不写空账）")

    # ── 6. state 字段本身（源码闸：导入 main_agent 会构建整张 agent 图，不在此处付这个代价）──
    _ma = (_SRC / "agent" / "main_agent.py").read_text(encoding="utf-8")
    check(bool(re.search(
              r"answer_fingerprints:\s*Annotated\[NotRequired\[dict\[str, dict\]\],\s*"
              r"_merge_dict_by_key\]", _ma)),
          "★ MainAgentState 声明了 answer_fingerprints（否则图会静默丢弃这个 update 键）")
    check("dict(existing or {})" in _ma,
          "reducer 对 None 安全（首写时 channel 还是空的）")


# ══════════════════════════════════════════════════════════════════════
# S2 取数通道（`attribution_runner.WrenCubeRunner`）：编译端真跑、执行端换缝
#
# **为什么能离线验**：把「执行」换成测试缝（`executor=`）后，剩下的全是本模块自己的
# 逻辑 —— 窗口怎么进 `filters`、`2+2D` 的调用序、闸①、行归一、异常收敛。真库只影响
# 「SQL 能不能跑通」，不影响「我们发出去的是不是那条 SQL」。
# 段⑨ 用**假编译器**钉「发出去的参数」（并记录，逐条断言）；段⑩ 用**真编译器**
# （本机有生产活跃库的 checkout 才跑）钉「真产物长什么样」—— 两者互补，缺一不可。
# ══════════════════════════════════════════════════════════════════════
_PROJECT_CHECKOUT = pathlib.Path(r"D:\code_work_space\llm\witops-wrenai")


class _FakePlanner:
    """假编译器：记录每次调用的 args，回一条**可执行、可区分**的只读 SQL。

    入参逐字记下来 —— 本段要验的正是「我们发出去的 args 长什么样」，
    而不是「wren 把 args 编译成了什么」（后者归段⑩）。
    """

    def __init__(self, *, sql_override: str | None = None, fail: bool = False,
                 override_grouped_only: bool = False) -> None:
        self.calls: list[dict] = []
        self._sql_override = sql_override
        self._fail = fail
        # 只对**带维度**的查询下毒：否则总量先撞闸，测不到「逐维隔离」那条路径。
        self._override_grouped_only = override_grouped_only

    def __call__(self, project, conn, args):
        self.calls.append({"project": project, "conn": conn, "args": _deepcopy(args)})
        if self._fail:
            return {}
        dims = list(args.get("dimensions") or [])
        if self._sql_override is not None and not (self._override_grouped_only and not dims):
            return {"cube_sql": self._sql_override, "dialect_sql": self._sql_override,
                    "dialect": "mysql", "limit_appended": False, "dup_limit": False}
        # 把 filters **原样**放进 SQL：这样执行缝能按窗口区分两期，断言也能看到谓词。
        pred = " AND ".join(f"<{spec}>" for spec in (args.get("filters") or [])) or "1=1"
        # measures 是**列表**（段⑬：同一组多度量进同一条 SQL）⇒ 逐列渲染。
        # 单度量时逐字等于老写法（`SUM(work_hour) AS total_hours`）。
        cols = ", ".join(
            f"SUM(work_hour) AS {name}" for name in (args.get("measures") or ["total_hours"])
        )
        head = f"SELECT {cols}"
        if dims:
            head = f"SELECT {dims[0]} AS {dims[0]}, {cols}"
        sql = f"{head} FROM v_workhour WHERE {pred}"
        if dims:
            sql += " GROUP BY 1"
        return {"cube_sql": sql, "dialect_sql": sql + " LIMIT 1001",
                "dialect": "mysql", "limit_appended": True, "dup_limit": False}


def _deepcopy(value):
    import copy

    return copy.deepcopy(value)


def _tail(sql: str, n: int = 200) -> str:
    """只把 SQL 的**尾巴**打进断言附注：真编译产物的前半段是 CTE 展开（几 KB），
    而要看的东西（WHERE / GROUP BY / LIMIT）永远在末尾。"""
    flat = " ".join(str(sql).split())
    return "…" + flat[-n:] if len(flat) > n else flat


def _patch_planner(planner):
    """替换 `wren_plan.plan_cube_sql_checked`（`_compile` 在函数内 import ⇒ 打模块属性即生效）。

    **只打 `_checked` 这条**：runner 走的就是它（失败抛出、带引擎原话）。故意**不**顺手
    把 fail-open 的 `plan_cube_sql` 一起打掉 —— 若哪天有人把 runner 改回 fail-open 那条，
    这里会当场暴露（真编译器在假 project/假 conn 上恒返回 `{}` ⇒ 断言全红）。
    """
    import contextlib

    import agent.utils.wren_plan as wp

    @contextlib.contextmanager
    def _cm():
        original = wp.plan_cube_sql_checked
        wp.plan_cube_sql_checked = planner
        try:
            yield planner
        finally:
            wp.plan_cube_sql_checked = original

    return _cm()


def _s2_request(**overrides) -> Request:
    base = dict(
        measure="total_hours",
        cube="workhour_analysis",
        db_name="witops",
        time_dimension="work_date",
        baseline=Window("2026-01-01", "2026-01-08"),
        current=Window("2026-01-08", "2026-01-15"),
        candidate_dimensions=["dept_level1"],
    )
    base.update(overrides)
    return Request(**base)


def _s2_executor(seen: list[str], *, baseline_rows, current_rows, totals):
    """执行缝：按 SQL 里的窗口谓词区分两期，回 DataFrame（**带 numpy 标量**）。

    刻意返回 numpy 标量而不是 Python 原生 —— 这正是要通过的那道归一（见段⑨-4）。
    """
    import numpy as np
    import pandas as pd

    def _run(sql: str):
        seen.append(sql)
        is_baseline = "2026-01-01" in sql
        grouped = "GROUP BY" in sql
        if not grouped:
            return pd.DataFrame({"total_hours": np.array([totals[0 if is_baseline else 1]], dtype="float64")})
        rows = baseline_rows if is_baseline else current_rows
        return pd.DataFrame({
            "dept_level1": np.array([r[0] for r in rows], dtype=object),
            "total_hours": np.array([r[1] for r in rows], dtype="float64"),
        })

    return _run


def s9_runner_compile_contract() -> None:
    section("⑨ S2 取数通道：编译参数逐字契约 / 2+2D / 闸① / 行归一 / 异常收敛")
    from agent.utils import attribution_runner as ar
    from agent.utils.attribution_core import _normalize_dimension_value

    # ── 0. 夹具自检 ──
    from agent.middlewares.sql_approval import classify_sql

    check(classify_sql("DROP TABLE t")[0] == "write",
          "夹具自检：`classify_sql('DROP TABLE t')` = write（闸① 的判据前提）")
    check(classify_sql("SELECT 1 FROM t WHERE a = 1 LIMIT 1")[0] == "read",
          "夹具自检：正常编译产物 = read（闸① 不误杀）")

    base_rows = [("A", 40.0), ("B", 30.0)]
    cur_rows = [("A", 60.0), ("B", 25.0)]
    seen: list[str] = []
    planner = _FakePlanner()
    runner = ar.WrenCubeRunner(
        "P", {"datasource": "mysql"}, "witops",
        cube="workhour_analysis", measure="total_hours", time_dimension="work_date",
        executor=_s2_executor(seen, baseline_rows=base_rows, current_rows=cur_rows,
                              totals=(70.0, 85.0)),
    )
    with _patch_planner(planner):
        payload = attribute(runner, _s2_request())

    # ── 1. 调用序 `2+2D`，且**顺序**是 total 两期 → 逐维两期 ──
    kinds = [(list(c["args"]["dimensions"]), c["args"]["filters"][0]) for c in planner.calls]
    check(len(planner.calls) == 4, "① 编译 4 次 = `2 + 2D`（D=1）", len(planner.calls))
    check([k[0] for k in kinds] == [[], [], ["dept_level1"], ["dept_level1"]],
          "① 顺序 = 总量(基期) → 总量(当期) → 维度(基期) → 维度(当期)", [k[0] for k in kinds])
    check([k[1] for k in kinds] == ["work_date:gte:2026-01-01", "work_date:gte:2026-01-08",
                                    "work_date:gte:2026-01-01", "work_date:gte:2026-01-08"],
          "① 每条各自带**自己那期**的窗口（两期不串台）", [k[1] for k in kinds])
    check(len(seen) == 4 and runner.queries_executed == 4,
          "① 真正执行 4 条（编译几次就执行几次，无重试/无多余）", len(seen))
    check(payload["comparison_metadata"]["queries_executed"] == 4 == runner.queries_executed,
          "① 内核记账 == runner 记账（`queries_executed` 可交叉核对）")

    # ── 2. args 的**键集**：窗口只走 filters，不发 timeDimensions，不发 limit/offset ──
    for index, call in enumerate(planner.calls):
        args = call["args"]
        check(set(args) == {"cube", "measures", "dimensions", "filters"},
              f"② 第 {index + 1} 次编译的 args 键集恰为 cube/measures/dimensions/filters"
              "（**没有** timeDimensions / limit / offset）", sorted(args))
    check(all(c["args"]["measures"] == ["total_hours"] for c in planner.calls),
          "② measures 恒为 `[measure]`（不带聚合前缀，聚合由 cube 定义承担）")
    check(all(c["args"]["cube"] == "workhour_analysis" for c in planner.calls),
          "② cube 恒为请求里的那个")
    check(planner.calls[0]["args"]["filters"] == ["work_date:gte:2026-01-01", "work_date:lt:2026-01-08"],
          "② 窗口谓词 = `gte start` + `lt end`（**半开** `[start, end)`，不是 lte）",
          planner.calls[0]["args"]["filters"])

    # ── 3. 父过滤在前、窗口在后；`limit` 参数收下但**不进 SQL** ──
    planner_b = _FakePlanner()
    runner_b = ar.WrenCubeRunner(
        "P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
        filters=[{"dimension": "company", "operator": "eq", "value": "X"}],
        executor=_s2_executor([], baseline_rows=base_rows, current_rows=cur_rows, totals=(70.0, 85.0)),
    )
    with _patch_planner(planner_b):
        runner_b.fetch_grouped("dept_level1", Window("2026-01-01", "2026-01-08"), 9999)
    check(planner_b.calls[0]["args"]["filters"] == [
              "company:eq:X", "work_date:gte:2026-01-01", "work_date:lt:2026-01-08"],
          "③ 父过滤在前、窗口两条件在后（顺序即语义：先缩范围再切窗口）",
          planner_b.calls[0]["args"]["filters"])
    planner_c = _FakePlanner()
    runner_c = ar.WrenCubeRunner(
        "P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
        executor=_s2_executor([], baseline_rows=base_rows, current_rows=cur_rows, totals=(70.0, 85.0)),
    )
    with _patch_planner(planner_c):
        runner_c.fetch_grouped("dept_level1", Window("2026-01-01", "2026-01-08"), 3)
        runner_c.fetch_grouped("dept_level1", Window("2026-01-01", "2026-01-08"), 100000)
    check([c["args"] for c in planner_c.calls][0] == [c["args"] for c in planner_c.calls][1],
          "③ `limit` 不同 ⇒ 编译产物**逐字相同**（limit 不下推，物理上限恒 1001）")

    # 父过滤也打时间维度 ⇒ 与窗口按 AND 叠加（是交集、方向变窄），但 metadata 记的是窗口
    # ⇒ 必须留一条 WARNING（否则审计口径对不上）。
    import logging

    planner_d = _FakePlanner()
    runner_d = ar.WrenCubeRunner(
        "P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
        filters=[{"dimension": "work_date", "operator": "gte", "value": "2025-01-01"}],
        executor=_s2_executor([], baseline_rows=base_rows, current_rows=cur_rows, totals=(70.0, 85.0)),
    )
    _records: list[logging.LogRecord] = []
    _handler = logging.Handler()
    _handler.emit = _records.append  # type: ignore[method-assign]
    _log = logging.getLogger("agent.utils.attribution_runner")
    _log.addHandler(_handler)
    _log.setLevel(logging.WARNING)
    try:
        with _patch_planner(planner_d):
            runner_d.fetch_total(Window("2026-01-01", "2026-01-08"))
    finally:
        _log.removeHandler(_handler)
    check(planner_d.calls[0]["args"]["filters"] == [
              "work_date:gte:2025-01-01", "work_date:gte:2026-01-01", "work_date:lt:2026-01-08"],
          "③ 父过滤命中时间维度时 ⇒ 三条条件并存（AND 交集；顺序 = 父 → gte → lt）",
          planner_d.calls[0]["args"]["filters"])
    check(any("AND 叠加" in str(r.getMessage()) for r in _records),
          "③ 同时留 WARNING（否则 `comparison_metadata` 的窗口 ≠ 实际查询范围，审计对不上）")

    # ── 4. 行归一：列名取叶子 + numpy 标量 → Python 原生 ──
    import numpy as np
    import pandas as pd

    normalized = ar._rows_from(pd.DataFrame({
        "v_workhour.dept_level1": np.array(["A"]),
        "total_hours__day": np.array([1.0]),
    }))
    check(list(normalized[0]) == ["dept_level1", "day"],
          "④ 列名取叶子（`column_leaf` 与内核**同一份**，不复制）", list(normalized[0]))
    check(ar._rows_from(None) == [] and ar._rows_from(pd.DataFrame()) == [],
          "④ 空结果 ⇒ `[]`（None 也收敛成 `[]`，不抛）")
    check(ar.column_leaf is __import__("agent.utils.attribution_core", fromlist=["x"]).column_leaf,
          "④ 契约：列名归一与内核共用同一个函数对象（不是同名第二份实现）")

    # ★ 归一的**负对照**：不过这层，整数维度值会被内核悄悄变成字符串。
    np_int = np.array([5])[0]
    check(_normalize_dimension_value(np_int) == "5",
          "★ 负对照：`np.int64(5)` 直接喂内核 ⇒ 被 `str()` 成 `'5'`（`isinstance(np.int64, int)` 为假）")
    check(_normalize_dimension_value(ar._to_python_scalar(np_int)) == 5,
          "★ 正对照：过 `_to_python_scalar` 后 ⇒ 真是 int `5`")
    check(_normalize_dimension_value(ar._to_python_scalar(np.array([3.5])[0])) == 3.5,
          "★ 同理：`np.float64` 必须回 float（否则变 `'3.5'`）")
    # ★ 后果**必须落在可观测处**，否则这段就是自说自话：负对照那两个值会被内核当成
    #   **两个不同的成员**。触发条件是真实存在的 —— pandas 的列只要有 NULL 就会把整列
    #   从 int64 升成 float64（`45` vs `45.0`），而 `int`/`float` 在 Python 的 dict 里
    #   hash 相等 ⇒ 过完归一才合并成一个成员。这条**不是**「模型看到 `"5"`」那类说法：
    #   内核 payload 的 `value` 字段是 `_display_dimension_value` 渲染的**展示字符串**
    #   （`str(value)` / `(null)`），带类型的取值只在 `drill_down.filters[].value` 里。
    check(len({_normalize_dimension_value(np_int), _normalize_dimension_value(5.0)}) == 2,
          "★ 负对照的后果：`'5'` 与 `5.0` 是两个并集成员（同一客户被劈成两行）")
    check(len({_normalize_dimension_value(ar._to_python_scalar(np_int)),
               _normalize_dimension_value(5.0)}) == 1,
          "★ 正对照的后果：`5` 与 `5.0` 同键 ⇒ 两期类型不一致也不会劈成员"
          "（pandas 遇到 NULL 会把整列升成 float64，这是真实触发路径）")
    check(ar._to_python_scalar(None) is None and ar._to_python_scalar("A") == "A",
          "④ 归一不越界：None / str 原样（驱动给的字符串维度值不许被改写）")

    # ── 5. 闸①：编译出的写语句被拒，且**类型化**（总量 → 直接抛；分组 → 计入该维失败）──
    runner_w = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
                                 executor=lambda sql: [{"total_hours": 1.0}])
    with _patch_planner(_FakePlanner(sql_override="DROP TABLE v_workhour")):
        try:
            attribute(runner_w, _s2_request(candidate_dimensions=[]))
            check(False, "⑤ 闸①：写语句必须被拒（总量查询路径）")
        except TypedError as error:
            check(error.code == "INVALID_ARGUMENT" and "写/DDL" in error.message,
                  "⑤ 闸①：写语句 ⇒ `INVALID_ARGUMENT`（不是自创码）", error.code)
    runner_w2 = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
                                  executor=lambda sql: [])
    with _patch_planner(_FakePlanner(sql_override="DROP TABLE v_workhour",
                                     override_grouped_only=True)):
        payload_w = attribute(runner_w2, _s2_request())
    fails = [w for w in payload_w["warnings"] if w["code"] == "dimension_analysis_failed"]
    check(len(fails) == 1 and "INVALID_ARGUMENT" in fails[0]["message"],
          "⑤ 闸①：维度查询路径 ⇒ 计入 `dimension_analysis_failed`（逐维隔离，不炸整场）")

    # ── 6. 异常收敛：编译失败 / 执行异常都不自创码 ──
    runner_f = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
                                 executor=lambda sql: [{"total_hours": 1.0}])
    with _patch_planner(_FakePlanner(fail=True)):
        _expect_error(runner_f, _s2_request(candidate_dimensions=[]), "TOTAL_QUERY_FAILED",
                      "⑥ 编译失败（空 plan）⇒ 内核收敛成 `TOTAL_QUERY_FAILED`")

    # ⑥-b 编译失败必须**带上引擎的原话**（`plan_cube_sql_checked` 的 CubePlanError）。
    # 实测踩过：维度名写成物理列名时 fail-open 那条只回 `{}` ⇒ 错误文本是「编译不出 SQL」，
    # 查一整天。这里用「抛 CubePlanError」的假编译器 + 「回 {}」的假编译器各钉一次。
    from agent.utils.wren_plan import CubePlanError as _CPE  # noqa: PLC0415

    def _raise_plan_error(project, conn, args):
        raise _CPE("Error during planning: Unknown filter dimension 'work_date_raw'")

    runner_f2 = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m",
                                  time_dimension="work_date",
                                  executor=lambda sql: [{"total_hours": 1.0}])
    with _patch_planner(_raise_plan_error):
        try:
            attribute(runner_f2, _s2_request(candidate_dimensions=[]))
            check(False, "⑥-b 编译抛错必须收敛成 TOTAL_QUERY_FAILED")
        except TypedError as error:
            check(error.code == "TOTAL_QUERY_FAILED"
                  and "Unknown filter dimension 'work_date_raw'" in error.message,
                  "⑥-b ★ 引擎原话进了错误文本（S3 的工具要靠它纠正参数）："
                  + error.message[:110], error.code)
    runner_f3 = ar.WrenCubeRunner("P", {"datasource": "mysql"}, "witops", cube="c", measure="m",
                                  time_dimension="work_date",
                                  executor=lambda sql: [{"total_hours": 1.0}])
    with _patch_planner(_FakePlanner(fail=True)):
        try:
            attribute(runner_f3, _s2_request(candidate_dimensions=[]))
            check(False, "⑥-b 空 plan 也必须收敛成 TOTAL_QUERY_FAILED")
        except TypedError as error:
            check("引擎返回了空的物理计划" in error.message,
                  "⑥-b 负对照：`_checked` 不抛却回 `{}` ⇒ 如实说「引擎没给原因」，不许编原因")
    check("plan_cube_sql_checked" in inspect.getsource(ar.WrenCubeRunner._compile)
          and "plan_cube_sql(" not in inspect.getsource(ar.WrenCubeRunner._compile),
          "⑥-b 结构性：`_compile` 走 `_checked`（fail-open 的那条会把错因吞成 `{}`）")

    def _boom_all(sql):
        raise RuntimeError("driver exploded")

    def _boom_grouped(sql):
        # 只炸**分组**查询：总量先炸的话整场就终止了，测不到「逐维隔离」。
        if "GROUP BY" in sql:
            raise RuntimeError("driver exploded")
        return [{"total_hours": 70.0}]

    runner_e = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
                                 executor=_boom_all)
    with _patch_planner(_FakePlanner()):
        _expect_error(runner_e, _s2_request(candidate_dimensions=[]), "TOTAL_QUERY_FAILED",
                      "⑥ 执行异常 ⇒ `TOTAL_QUERY_FAILED`（带 period）")
    runner_e2 = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
                                  executor=_boom_grouped)
    with _patch_planner(_FakePlanner()):
        payload_e = attribute(runner_e2, _s2_request())
    check(any(w["code"] == "dimension_analysis_failed" for w in payload_e["warnings"])
          and payload_e["strategy"] == "unsupported",
          "⑥ 分组执行异常 ⇒ 只该维失败、其余照跑（D=1 时策略=unsupported，因为没维度可排名）")

    # ── 7. 没 time_dimension ⇒ 拒绝（宁可不做，也不对全历史做归因）──
    runner_t = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m", time_dimension=None,
                                 executor=lambda sql: [{"total_hours": 1.0}])
    planner_t = _FakePlanner()
    with _patch_planner(planner_t):
        _expect_error(runner_t, _s2_request(candidate_dimensions=[]), "INVALID_TIME_WINDOW",
                      "⑦ 缺 `time_dimension` ⇒ `INVALID_TIME_WINDOW`（窗口下推不了就不跑）")
        check(planner_t.calls == [],
              "⑦ 而且**一次都没编译**（拒绝发生在编译之前，不留半截产物）")

    # ── 8. 过滤渲染：结构化 → `dim:op[:value]` ──
    check(ar.render_filter_spec({"dimension": "d", "operator": "eq", "value": "A"}) == "d:eq:A",
          "⑧ `eq` ⇒ `d:eq:A`")
    check(ar.render_filter_spec({"dimension": "d", "operator": "eq", "value": "a:b"}) == "d:eq:a:b",
          "⑧ value 含冒号**安全**（wren 侧 `split(':', 2)` 只切两刀）")
    check(ar.render_filter_spec({"dimension": "d", "operator": "is_null"}) == "d:is_null",
          "⑧ `is_null` ⇒ 两段式（不带 value）")
    check(ar.render_filter_spec({"dimension": "d", "operator": "in", "value": ["A", "B"]}) == "d:in:A,B",
          "⑧ `in` ⇒ 逗号连接")
    try:
        ar.render_filter_spec({"dimension": "d", "operator": "like", "value": "%a%"})
        check(False, "⑧ 负对照：枚举外的算子必须拒")
    except TypedError as error:
        check(error.code == "INVALID_FILTER", "⑧ 负对照：`like` 不在 `FILTER_OPERATORS` ⇒ `INVALID_FILTER`")
    try:
        ar.render_filter_spec({"operator": "eq", "value": "A"})
        check(False, "⑧ 负对照：缺 dimension 必须拒")
    except TypedError as error:
        check(error.code == "INVALID_FILTER", "⑧ 负对照：缺 dimension ⇒ `INVALID_FILTER`")

    # ── 9. `get_runner`：解析不出就回 None（不猜库、不回落默认库）──
    import contextlib as _ctx

    import agent.utils.wren_call_extract as wce

    @_ctx.contextmanager
    def _fake_ctx(value):
        original = wce.resolve_wren_ctx_by_db
        wce.resolve_wren_ctx_by_db = lambda db_name: value
        try:
            yield
        finally:
            wce.resolve_wren_ctx_by_db = original

    with _fake_ctx(("", {})):
        check(ar.get_runner("nope", cube="c", measure="m") is None,
              "⑨ `(项目路径, 连接)` 解析不出 ⇒ `None`（由工具层决定回哪个错误码）")
    with _fake_ctx((r"D:\nonexistent", {"datasource": "mysql"})):
        got = ar.get_runner("witops", cube="c", measure="m", time_dimension="work_date")
        check(isinstance(got, ar.WrenCubeRunner), "⑨ 解析得出 ⇒ 建 runner（库名原样带进 metadata）")

    # ── 10. 异步门面把**整段**搬离事件循环（P1-14）──
    import asyncio
    import threading

    thread_seen: list[int] = []

    def _thread_probe(sql: str):
        thread_seen.append(threading.get_ident())
        return [{"total_hours": 1.0}] if "GROUP BY" not in sql else [{"dept_level1": "A", "total_hours": 1.0}]

    runner_o = ar.WrenCubeRunner("P", {}, "witops", cube="c", measure="m", time_dimension="work_date",
                                 executor=_thread_probe)
    with _patch_planner(_FakePlanner()):
        payload_o = asyncio.run(ar.run_attribution(runner_o, _s2_request(candidate_dimensions=[])))
    main_thread = threading.get_ident()
    check(bool(thread_seen) and all(tid != main_thread for tid in thread_seen),
          "⑩ `run_attribution` 把执行搬到工作线程（主循环上不留同步阻塞）")
    check(all(tid == thread_seen[0] for tid in thread_seen),
          "⑩ **同一条**线程跑完 2+2D（不按维度 fan-out，run 槽不被并发吃掉）")
    check(payload_o["comparison_metadata"]["queries_executed"] == 2
          == runner_o.queries_executed,
          "⑩ 门面与内核记账一致（2 条总量）")

    # ── 11. 执行通道的 import 链（**子进程**验，免得把 dbmcp 的 import 副作用带进本进程）──
    _probe = (
        "import sys, os, tempfile;"
        "os.environ['AGENT_DATA_ROOT'] = tempfile.mkdtemp(prefix='probe-');"
        "sys.path.insert(0, r'%s');"
        "from mcp_server.db_mcp_server.db.config import McpSqlConfig;"
        "from mcp_server.db_mcp_server.db.db_server import _build_tool_context, _load_runner_class;"
        "from mcp_server.db_mcp_server.db.sql_runner import RunSqlToolArgs;"
        "assert _load_runner_class('mysql').__name__ == 'MySQLRunner';"
        "assert 'sql' in RunSqlToolArgs.model_fields;"
        "assert callable(McpSqlConfig.from_env) and callable(_build_tool_context);"
        "print('OK')"
    ) % str(_SRC)
    import subprocess

    proc = subprocess.run([sys.executable, "-c", _probe], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    check("OK" in (proc.stdout or ""),
          "⑪ 执行通道的三个 import + `_load_runner_class('mysql')` 在干净进程里可用"
          "（**不连库**，只钉路径没写错）", (proc.stdout or proc.stderr or "").strip()[-160:])


def s10_runner_real_compiler() -> None:
    section("⑩ S2 真编译器：同一份 args 经生产活跃库编译出的 SQL 逐字形状")
    if not (_PROJECT_CHECKOUT / "target" / "mdl.json").exists():
        print("  \033[33mSKIP\033[0m  本机没有活跃库 checkout"
              f"（{_PROJECT_CHECKOUT}）⇒ 跳过真编译段（CI/容器里预期如此）")
        return

    from agent.middlewares.sql_approval import classify_sql
    from agent.utils import attribution_runner as ar
    from agent.utils.wren_plan import plan_cube_sql

    conn = {"datasource": "mysql", "host": "127.0.0.1", "port": 3306,
            "database": "witops", "user": "probe"}

    def _compile(*, dimensions, window, time_dimension):
        # 走 runner 自己的过滤渲染（真实现），只把执行换掉 —— 验的就是「我们的 args」。
        runner = ar.WrenCubeRunner(
            str(_PROJECT_CHECKOUT), conn, "witops",
            cube="workhour_analysis", measure="total_hours",
            time_dimension=time_dimension, executor=lambda sql: [],
        )
        return runner._compile(dimensions=dimensions, window=window)

    sql_total = _compile(dimensions=(), window=Window("2026-08-01", "2026-09-01"),
                         time_dimension="work_date")
    sql_grouped = _compile(dimensions=("dept_level1",),
                           window=Window("2026-08-01", "2026-09-01"), time_dimension="work_date")
    check("work_date >= '2026-08-01'" in sql_total and "work_date < '2026-09-01'" in sql_total,
          "★ 真编译：窗口是**半开**谓词 `>= start AND < end`（不是 `<=`）", _tail(sql_total))
    check("ORDER BY" not in sql_grouped and "GROUP BY 1" in sql_grouped,
          "★ 真编译：`GROUP BY 1` 且**无 ORDER BY**（ORDER BY 会让同一维度取值出多行）",
          _tail(sql_grouped))
    check("work_date__day" not in sql_grouped and "DATE(" not in sql_grouped,
          "★ 真编译：**没有** `DATE(work_date) AS work_date__day`（那条路会把日期塞进 GROUP BY ⇒ 弃用）",
          _tail(sql_grouped))
    check(sql_grouped.rstrip().endswith("LIMIT 1001") and sql_grouped.count("LIMIT") == 1,
          "★ 真编译：物理上限恒 `LIMIT 1001`（= `_PHYSICAL_GROUPED_ROW_LIMIT`，判截断的锚点）")
    check(sql_total.rstrip().endswith("LIMIT 1001") and "GROUP BY" not in sql_total,
          "★ 真编译：合计版无 GROUP BY 且有 LIMIT", _tail(sql_total))

    # ── 注入面实测（方案 §1.2：「注入面按构造关闭」这句话要有证据，不能靠信念）──
    # value 里的单引号由 **wren 自己**按 `''` 转义（不是我们做的），实测四种危险输入
    # 全都「仍是一个字面量」或「编译失败」，**没有找到能越出字面量的形状**。
    # 剩下的两层：闸①（对编译产物再看一眼）+ `FILTER_OPERATORS` 白名单（算子层）。
    _bs = chr(92)  # 反斜杠：用 chr 拼，免得被任何转义层吃掉

    def _sql_for(value: str) -> str:
        from agent.utils.wren_plan import plan_cube_sql

        spec = ar.render_filter_spec({"dimension": "dept_level1", "operator": "eq", "value": value})
        plan = plan_cube_sql(str(_PROJECT_CHECKOUT), conn, {
            "cube": "workhour_analysis", "measures": ["total_hours"],
            "dimensions": ["dept_level1"], "filters": [spec],
        })
        return str((plan or {}).get("dialect_sql") or "")

    _quoted = _sql_for("a' OR 1=1")
    check("= 'a'' OR 1=1'" in _quoted and classify_sql(_quoted)[0] == "read",
          "★ 注入面：value 里的 `'` 由 wren 按 `''` 转义 ⇒ 仍是**一个字面量**"
          "（`a' OR 1=1` 整串被当成部门名，没有越出引号）", _tail(_quoted, 90))
    check(_sql_for("a" + _bs + "' OR 1=1") == "" and _sql_for("a" + _bs) == "",
          "★ 注入面：反斜杠 + 引号 / 末尾单反斜杠 ⇒ 编译**直接失败**（fail-closed，不产出可疑 SQL）")
    _bsed = _sql_for("a" + _bs + _bs + "' OR 1=1")
    check("= 'a" + _bs + _bs + "'' OR 1=1'" in _bsed and classify_sql(_bsed)[0] == "read",
          "★ 注入面：双反斜杠 + 引号 仍是单字面量（MySQL 默认模式与 NO_BACKSLASH_ESCAPES 下都成立）",
          _tail(_bsed, 90))

    # 已知的**保守误杀**：值里出现 `; DROP` 这类文本时，闸① 只看结构、看不穿字面量 ⇒
    # 判 write 并拒。方向是安全的（fail-closed），但错误信息会让人困惑 ⇒ 写进断言里备案。
    _semi_sql = _sql_for("a'; DROP TABLE t; --")
    _semi_verdict = classify_sql(_semi_sql)
    check(_semi_verdict[0] == "write" and "DROP" in str(_semi_verdict[1]),
          "★ 注入面备案：值里含 `; DROP` 时闸① 判 write ⇒ runner 会**拒答**"
          "（保守误杀：实测那个字面量本身是安全的，但方向是安全侧；runner 的注释里写明了"
          "这笔交换换的是什么）", _semi_verdict)

    # 负对照：非法 measure / 非法算子 ⇒ 编译不出来（runner 随后抛普通异常 → 内核收敛）
    check(plan_cube_sql(str(_PROJECT_CHECKOUT), conn,
                        {"cube": "workhour_analysis", "measures": ["nope"],
                         "dimensions": [], "filters": []}) == {},
          "负对照：非法 measure ⇒ `{}`（编译失败，由内核收敛成类型化错误）")
    check(plan_cube_sql(str(_PROJECT_CHECKOUT), conn,
                        {"cube": "workhour_analysis", "measures": ["total_hours"],
                         "dimensions": [], "filters": ["work_date:bogus_op:2026-08-01"]}) == {},
          "负对照：非法算子 ⇒ `{}`（`FILTER_OPERATORS` 是同一份枚举）")


# ══════════════════════════════════════════════════════════════════════
def s13_multi_measure() -> None:
    section("⑬ S2 多度量：一次取数喂多个度量（`2+2D` **不随度量数增长**）")
    import asyncio

    import numpy as np
    import pandas as pd

    from agent.utils import attribution_runner as ar

    measures = ["total_hours", "approved_hours"]
    # 两期行：`(部门, total_hours, approved_hours)`。两个度量都**可加**（各自 Σ分组 == 总量）。
    base_rows = [("A", 40.0, 10.0), ("B", 30.0, 20.0)]
    cur_rows = [("A", 60.0, 22.0), ("B", 25.0, 18.0)]
    totals = {"total_hours": (70.0, 85.0), "approved_hours": (30.0, 40.0)}
    executed: list[str] = []

    def _executor(sql: str):
        """执行缝：一条 SQL 里回**两列**（正是真编译器的产物形状）。"""
        executed.append(sql)
        is_baseline = "2026-01-01" in sql
        if "GROUP BY" not in sql:
            return pd.DataFrame(
                {name: np.array([totals[name][0 if is_baseline else 1]], dtype="float64")
                 for name in measures}
            )
        rows = base_rows if is_baseline else cur_rows
        return pd.DataFrame(
            {
                "dept_level1": np.array([row[0] for row in rows], dtype=object),
                "total_hours": np.array([row[1] for row in rows], dtype="float64"),
                "approved_hours": np.array([row[2] for row in rows], dtype="float64"),
            }
        )

    planner = _FakePlanner()
    runner = ar.WrenCubeRunner(
        "P", {"datasource": "mysql"}, "witops",
        cube="workhour_analysis", measures=measures, time_dimension="work_date",
        executor=_executor,
    )
    with _patch_planner(planner):
        payload = asyncio.run(
            ar.run_attribution(runner, [_s2_request(measure=name) for name in measures])
        )

    # ── 1. 编译带**整组**度量；执行仍只有 2+2D（D=1 ⇒ 4），不是 M×(2+2D) = 8 ──
    check(all(call["args"]["measures"] == measures for call in planner.calls),
          "① 每次编译都带**整组**度量（聚合由 cube 定义承担，多度量 = 多几列）",
          [call["args"]["measures"] for call in planner.calls][:1])
    check(len(planner.calls) == 8 and len(executed) == 4 and runner.queries_executed == 4,
          "★ ① 编译 8 次（M×2+2D，进程内毫秒级）但**执行只有 4 条**"
          "⇒ `queries_executed == 2+2D`，与度量数 M 无关",
          f"编译={len(planner.calls)} 执行={len(executed)}")
    check(sum(1 for entry in runner.query_log if entry["cached"]) == 4
          and len(runner.query_log) == 8,
          "① 记账分得清「调用」与「执行」：query_log 8 条、其中 4 条 `cached=True`",
          runner.query_log[0])

    # ── 2. 返回形状恒定（顶层**不镜像**主度量，读数只有一个地方）──
    check(set(payload) == {"measures", "primary_measure", "per_measure", "warnings",
                           "comparison_metadata", "queries_executed"},
          "② 顶层键集恒为 6 个（单度量也走这个形状）", sorted(payload))
    check(payload["measures"] == measures and payload["primary_measure"] == "total_hours"
          and set(payload["per_measure"]) == set(measures),
          "② `measures` 保序、`primary_measure` = 第一个、`per_measure` 逐度量一份")
    check("total_change" not in payload and "per_dimension" not in payload,
          "② 负对照：结果**不在顶层**（镜像会让 M=1 与 M>1 出现两套读法）")

    # ── 3. 逐度量各算各的（共享取数 ≠ 共享结果）──
    hours = payload["per_measure"]["total_hours"]
    approved = payload["per_measure"]["approved_hours"]
    check(hours["measure"] == "total_hours" and approved["measure"] == "approved_hours",
          "③ 每份结果自己带 measure（不会串味）")
    check(hours["total_change"]["delta"] == 15 and approved["total_change"]["delta"] == 10,
          "★ ③ 两个度量**各自**的 Δ（total_hours 85−70=15、approved_hours 40−30=10）",
          [hours["total_change"]["delta"], approved["total_change"]["delta"]])
    check(hours["strategy"] == approved["strategy"] == "term_wise",
          "③ 两者都可加 ⇒ 都是 `term_wise`（策略逐度量判，不由整组投票）")
    by_value = {
        name: {item["value"]: item["delta"] for item in
               payload["per_measure"][name]["per_dimension"]["dept_level1"]["values"]}
        for name in measures
    }
    check(by_value["total_hours"] == {"A": 20.0, "B": -5.0}
          and by_value["approved_hours"] == {"A": 12.0, "B": -2.0},
          "★ ③ 逐维取值也各算各的（A 两度量都涨、B 两度量都跌，但幅度不同）", by_value)
    # 加性对账（§2.2）在**每个**度量上独立成立 —— 否则「共享取数」会掩盖单度量的不可加。
    check(all(abs(sum(by_value[name].values()) - payload["per_measure"][name]
                   ["total_change"]["delta"]) < 1e-9 for name in measures),
          "★ ③ Σ分维 == 总量 对**每个**度量都成立（对账没被合并掉）")

    # ── 4. 顶层记账：去重后的真实执行数 + 查询组数 ──
    metadata = payload["comparison_metadata"]
    check(payload["queries_executed"] == 4 == metadata["queries_executed"] == runner.queries_executed,
          "④ 顶层三处记账一致且 = 真实执行数（不是 M×(2+2D)）", payload["queries_executed"])
    check(metadata["measures"] == measures and metadata["measure"] == "total_hours",
          "④ metadata 里既有 `measure`（主度量，内核原样）也有 `measures`（整组）")
    check(metadata["query_groups"] == 1,
          "④ 一次调用 = 一个查询组（跨 cube 由模型分次调用，成本 `G×(2+2D)`）")
    check(metadata["candidate_dimensions"] == ["dept_level1"],
          "④ 参数透传仍在（复算用）：candidate_dimensions 等键没被覆盖掉")

    # ── 5. 单度量也走**同一形状**（负对照：`measure=` 老写法）──
    single_runner = ar.WrenCubeRunner(
        "P", {"datasource": "mysql"}, "witops",
        cube="workhour_analysis", measure="total_hours", time_dimension="work_date",
        executor=_executor,
    )
    with _patch_planner(_FakePlanner()):
        single = asyncio.run(ar.run_attribution(single_runner, _s2_request()))
    check(single["measures"] == ["total_hours"] and single["primary_measure"] == "total_hours"
          and single["per_measure"]["total_hours"]["total_change"]["delta"] == 15,
          "⑤ 单度量与多度量**形状完全一致** ⇒ 调用方不必分两种情况写")
    check(single["queries_executed"] == 4 and single_runner.queries_executed == 4
          and not any(entry["cached"] for entry in single_runner.query_log),
          "⑤ 负对照：单度量**没有**缓存命中（每条 SQL 只调一次，`cached` 全 False）",
          [entry["cached"] for entry in single_runner.query_log])

    # ── 6. 顶层 warnings 是**并集、已去重**，且逐条带 measure ──
    # `max_values_per_dimension=1` 让两个度量各产一条**同样**的 `high_cardinality_dimension`
    # （消息里的取值数与阈值都来自同一批行），正是拿来试去重的。
    warn_runner = ar.WrenCubeRunner(
        "P", {"datasource": "mysql"}, "witops",
        cube="workhour_analysis", measures=measures, time_dimension="work_date",
        executor=_executor,
    )
    single_warn_runner = ar.WrenCubeRunner(
        "P", {"datasource": "mysql"}, "witops",
        cube="workhour_analysis", measure="total_hours", time_dimension="work_date",
        executor=_executor,
    )
    with _patch_planner(_FakePlanner()):
        warned = asyncio.run(ar.run_attribution(warn_runner, [
            _s2_request(measure=name, max_values_per_dimension=1) for name in measures
        ]))
        single_warn = asyncio.run(ar.run_attribution(
            single_warn_runner, _s2_request(max_values_per_dimension=1)))
    per_measure_codes = [
        [w["code"] for w in warned["per_measure"][name]["warnings"]] for name in measures
    ]
    check(per_measure_codes == [["high_cardinality_dimension"]] * 2,
          "★ ⑥ 夹具自检：两个度量**各**产一条 high_cardinality_dimension（否则测不到去重）",
          per_measure_codes)
    check(len(warned["warnings"]) == 1 and warned["warnings"][0]["measure"] == "total_hours",
          "★ ⑥ 顶层并集把两条同码同消息的警告**收成一条**（不去重就会把同一件事讲两遍），"
          "且保留了它属于谁", warned["warnings"])
    check(all(w.get("measure") in measures for w in warned["warnings"]),
          "⑥ 顶层 warning 逐条带 `measure`（多度量时不知道警告属于谁 ⇒ 会把 A 的非加性按到 B 头上）")
    check([w["code"] for w in single_warn["warnings"]] == ["high_cardinality_dimension"]
          and set(single_warn["warnings"][0]) == {"code", "dimension", "message", "measure"},
          "⑥ 负对照：单度量的顶层警告 = 内核逐字 + `measure` 一个键（不是另写一套措辞）",
          single_warn["warnings"])

    # ── 7. 同一个度量写两遍 ⇒ 收成一个（不重复编译、不重复计数）──
    dedup_runner = ar.WrenCubeRunner(
        "P", {"datasource": "mysql"}, "witops",
        cube="workhour_analysis", measures=measures, time_dimension="work_date",
        executor=_executor,
    )
    with _patch_planner(_FakePlanner()):
        dedup = asyncio.run(ar.run_attribution(dedup_runner, [
            _s2_request(measure="total_hours"), _s2_request(measure="total_hours"),
            _s2_request(measure="approved_hours"),
        ]))
    check(dedup["measures"] == measures and dedup_runner.queries_executed == 4,
          "⑦ 重复的度量被收成一个（`measures` 无重复项、执行数不变）",
          f"measures={dedup['measures']} 执行={dedup_runner.queries_executed}")

    # ── 8. 入参校验：不是 Request / 空列表 ⇒ 类型化拒绝（不自创码）──
    for bad, label in (
        ([], "空列表"),
        (["total_hours"], "字符串而不是 Request"),
    ):
        try:
            asyncio.run(ar.run_attribution(single_runner, bad))
            check(False, f"⑧ {label} 必须被拒")
        except TypedError as error:
            check(error.code == "INVALID_ARGUMENT",
                  f"⑧ {label} ⇒ `INVALID_ARGUMENT`（不自创码）", error.code)


# ══════════════════════════════════════════════════════════════════════
# S3 工具壳（`agent/tools/attribution_analyze.py`）：形状闸 / 缺省解析 / 开关 / 注册
#
# **端到端也是离线的**：假编译器（`_FakePlanner`）钉「发出去的 args」，执行缝
# （`executor=`）回两期行 —— 于是「入参 → 形状闸 → 编译 → 执行 → 内核 → 载荷」
# 整条链都在这里跑通，不碰真实库、不碰 MCP、不碰 LLM。
#
# ⚠️ §4.5 的闸②（判权）与闸③（cube / 维度名 / 时间锚点 / filters 白名单）在**段⑮**，
# 不在这里 —— 本段覆盖 S3 自己的形状闸（度量表达式）与缺省解析。
# ══════════════════════════════════════════════════════════════════════
_TOOL_PROJECT = pathlib.Path(_TMP_ROOT) / "tool_project"
# 夹具语义库：4 个度量（加性 2 / AVG 1 / DISTINCT 1）+ 2 个维度 + 1 个时间锚点。
# 维度只有 2 个 ⇒ 「缺省 candidate_dimensions = 全部维度」不会撞查询预算（2+2*2=6 ≤ 12），
# 于是「缺省」与「预算超限」是两条各自可测的路径。
_TOOL_CUBE_YML = """
name: demo
description: "夹具立方体"
measures:
  - name: total_hours
    expression: "SUM(work_hour)"
    type: DOUBLE
    description: "工时合计"
  - name: record_count
    expression: "COUNT(*)"
    type: BIGINT
    description: "报工条数"
  - name: avg_hours_per_record
    expression: "AVG(work_hour)"
    type: DOUBLE
    description: "单条平均工时"
  - name: user_count
    expression: "COUNT(DISTINCT user_id)"
    type: BIGINT
    description: "报工人数"
dimensions:
  - name: dept
    expression: "dept"
    type: string
    description: "部门"
  - name: project_name
    expression: "project_name"
    type: string
    description: "项目"
time_dimensions:
  - name: work_date
    expression: "work_date"
    type: date
    description: "报工日期"
    levels: [year, month, day]
"""

# 两期夹具：**逐维值之和 == 总量**（加性对账必须精确通过，否则段⑨的结论被警告污染）。
# 行按**度量名**取数（不是按下标）—— 假编译器按 `args["measures"]` 的**顺序**渲染列，
# 按下标取会在「模型换了度量顺序」时静默串列，那是这套夹具最会咬人的地方。
_TOOL_DATA = {
    "baseline": {
        "total": {"total_hours": 70.0, "record_count": 7.0},
        "dept": [("A", {"total_hours": 40.0, "record_count": 4.0}),
                 ("B", {"total_hours": 30.0, "record_count": 3.0})],
        "project_name": [("P1", {"total_hours": 40.0, "record_count": 4.0}),
                         ("P2", {"total_hours": 30.0, "record_count": 3.0})],
    },
    "current": {
        "total": {"total_hours": 85.0, "record_count": 7.0},
        "dept": [("A", {"total_hours": 60.0, "record_count": 5.0}),
                 ("B", {"total_hours": 25.0, "record_count": 2.0})],
        "project_name": [("P1", {"total_hours": 60.0, "record_count": 5.0}),
                         ("P2", {"total_hours": 25.0, "record_count": 2.0})],
    },
}
# 指纹里的两期（两个窗口半开、首尾相接；`windows` 的形状见 §4.9）。
_TOOL_BASELINE = ["2026-07-01", "2026-08-01"]
_TOOL_CURRENT = ["2026-08-01", "2026-09-01"]


def _write_tool_project() -> pathlib.Path:
    """把夹具语义库写到临时目录（`AGENT_DATA_ROOT` 之外，模拟 workspace 里的项目）。"""
    cube_dir = _TOOL_PROJECT / "cubes" / "demo"
    cube_dir.mkdir(parents=True, exist_ok=True)
    (cube_dir / "metadata.yml").write_text(_TOOL_CUBE_YML, encoding="utf-8")
    return _TOOL_PROJECT


def _tool_conn():
    """wren 形状的连接字典（`datasource` 缺了会「静默产不出 SQL」，段⑨记过这条）。

    本段**用不到真的连上去**：执行缝是注入的（`executor=`），这里只是为了让
    `resolve_wren_ctx_by_db` 的形状完整、`get_runner` 不返回 `None`。
    """
    return {"datasource": "mysql", "host": "h", "port": 3306, "database": "d", "user": "u"}


def _patch_wren_ctx(project: pathlib.Path):
    """把 `resolve_wren_ctx_by_db` 钉到夹具项目（工具与 `get_runner` 都在函数内 import ⇒ 打属性即生效）。"""
    from agent.utils import wren_call_extract as wce

    return _PatchAttr(wce, "resolve_wren_ctx_by_db",
                      lambda *a, **k: (str(project), _tool_conn()))


def _patch_get_runner(seen: list[str]):
    """真 `get_runner` + 注入执行缝：这样工具发出去的 `cube/measures/…` 是**真**走到 runner 的。"""
    from agent.utils import attribution_runner as ar

    real = ar.get_runner

    def _factory(db_name, **kwargs):
        kwargs["executor"] = _tool_executor(seen, list(kwargs.get("measures") or []))
        return real(db_name, **kwargs)

    return _PatchAttr(ar, "get_runner", _factory)


class _EnvFlags:
    """临时设置 env 开关（`with _EnvFlags(NL2SQL_ATTRIBUTION_ENABLE="1"):`）。"""

    def __init__(self, **flags: str) -> None:
        self._flags = flags

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._flags}
        os.environ.update(self._flags)
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class _PatchAttr:
    """临时替换某个模块属性（工具/`get_runner` 都在函数内 `from … import` ⇒ 打属性即生效）。"""

    def __init__(self, obj, name: str, value) -> None:
        self._obj, self._name, self._value = obj, name, value

    def __enter__(self):
        self._saved = getattr(self._obj, self._name)
        setattr(self._obj, self._name, self._value)
        return self._value

    def __exit__(self, *exc):
        setattr(self._obj, self._name, self._saved)
        return False


def _forbid(calls: list[str], label: str):
    """「绝不许被调用」的桩：调用即记一笔（断言它在特定情形下为 0 次）。"""

    def _call(*args, **kwargs):
        calls.append(label)
        raise AssertionError(f"不应被调用：{label}")

    return _call


def _run_tool(state: dict, **kwargs):
    """跑一次工具协程（`runtime` 只需要有 `.state`）。"""
    import asyncio
    from types import SimpleNamespace

    from agent.tools.attribution_analyze import _attribution_analyze_coro

    return asyncio.run(_attribution_analyze_coro(SimpleNamespace(state=state), **kwargs))


def _tool_executor(seen: list[str], measures: list[str]):
    """执行缝：按 SQL 里的窗口谓词与维度名区分，回带 numpy 标量的 DataFrame。

    列名 = 度量名（假编译器的别名），所以内核按叶子名解析得到的就是这次要的那一列
    ——「多度量共用一条 SQL、内核只看自己那列」这条在工具层照样成立。
    """
    import numpy as np
    import pandas as pd

    def _run(sql: str):
        seen.append(sql)
        data = _TOOL_DATA["baseline" if _TOOL_BASELINE[0] in sql else "current"]
        if "GROUP BY" not in sql:
            return pd.DataFrame({m: np.array([data["total"][m]], dtype="float64") for m in measures})
        dim = re.search(r"SELECT\s+(\w+)\s+AS", sql).group(1)
        rows = data[dim]
        frame = {dim: np.array([row[0] for row in rows], dtype=object)}
        for measure in measures:
            frame[measure] = np.array([row[1][measure] for row in rows], dtype="float64")
        return pd.DataFrame(frame)

    return _run


def _fingerprint(**overrides) -> dict:
    """一条「答案指纹」（`check_progress._build_answer_fingerprint` 的子集，形状照 §4.9）。"""
    payload = {
        "db_name": "demo",
        "final_cube_spec": {"cube": "demo", "measures": ["total_hours", "record_count"],
                            "time_dimension": "work_date"},
        "windows": {"baseline": list(_TOOL_BASELINE), "current": list(_TOOL_CURRENT)},
    }
    payload.update(overrides)
    return payload


def _last_payload(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001  工具返回值必是 JSON 文本，解不开就是断言要抓的
        return {"_raw": str(text)[:400]}


def s14_tool_shell() -> None:
    section("⑭ S3 工具壳：形状闸 / 缺省三级解析 / 开关 fail-closed / 注册")
    from agent.tools import attribution_analyze as aa
    from agent.utils import attribution_runner as ar
    from agent.utils import wren_call_extract as wce
    from agent.utils.wren_call_extract import load_cube_metadata

    # ── 1. 形状分类器：样例逐条 + 判定优先级 + 认不出就拒（负对照）──
    for expression, expected, label in (
        ("SUM(work_hour)", "term_wise", "SUM(x)"),
        ("COUNT(*)", "term_wise", "COUNT(*)"),
        ("SUM(CASE WHEN if_approve = 1 THEN work_hour ELSE 0 END)", "term_wise", "SUM(CASE …)"),
        ("SUM(a) - SUM(b)", "term_wise", "两个加性量相减"),
        ("COUNT(*) * 1.0", "term_wise", "常数缩放（**不是**乘积型）"),
        ("AVG(work_hour)", "mix_shift", "AVG"),
        ("SUM(a) / NULLIF(COUNT(*), 0)", "mix_shift", "比值"),
        ("SUM(a) * SUM(b)", "factor_shapley", "两个聚合相乘"),
        ("COUNT(DISTINCT user_id)", "unsupported", "去重计数"),
        ("", "unsupported", "空表达式（不许默认放行）"),
        ("ROUND(SUM(a), 2)", "unsupported", "没认出来的外层函数"),
        ("SUM(a", "unsupported", "括号不配对"),
        ("work_hour", "unsupported", "裸列名（不是聚合）"),
    ):
        got = aa.classify_measure_expression(expression)
        check(got == expected, f"① `{label}` ⇒ `{expected}`", f"{expression!r} → {got}")
    check(aa.classify_measure_expression("SUM(work_hour) / NULLIF(COUNT(DISTINCT user_id), 0)") == "mix_shift",
          "① ★ 优先级：含 `/` **先于** `COUNT(DISTINCT)` 判定 —— `avg_hours_per_user` 是**比值**"
          "（§3.6 的实测分类表这么归它；两条路的将来实现不同，混为一谈会误导 T2）")

    # ── 2. `filters` 的算子枚举取自内核那一份（两套账是本仓的常客）──
    from agent.utils.attribution_core import FILTER_OPERATORS
    from agent.tools.attribution_analyze import AttributionFilter

    enum = AttributionFilter.model_json_schema()["properties"]["operator"].get("enum")
    check(enum == list(FILTER_OPERATORS),
          "② 工具 schema 的算子枚举 == 内核 `FILTER_OPERATORS`（不复制第二份 ⇒ 不会漂移）", enum)

    # ── 3. 工具对象与入参签名（§1.2 逐字对齐）──
    tool = aa.attribution_analyze_tool
    fields = set(aa.AttributionAnalyzeSchema.model_fields)
    expected_fields = {
        "db_name", "cube", "measures", "time_dimension", "baseline", "current",
        "candidate_dimensions", "filters", "fingerprint",
        "top_n_dimensions", "top_n_values", "max_values_per_dimension",
    }
    check(tool.name == "attribution_analyze", "③ 工具名 = `attribution_analyze`", tool.name)
    check(fields == expected_fields, "③ 入参字段集合 == §1.2 的签名", sorted(fields ^ expected_fields))
    check("measure" not in fields and "measures" in fields,
          "③ 是 `measures`（列表）而不是 `measure`（单数）—— 多度量进契约")
    check("runtime" not in fields and "state" not in fields,
          "③ `runtime` 是 `InjectedToolArg`，不出现在给模型看的 schema 里")
    check("不解释" in tool.description and "贡献度分解" in tool.description
          and "不许编造任何原因" in tool.description and "因果结论" in tool.description,
          "③ ★ 工具描述里明写 §1.1 的 T3 红线（**只做贡献度分解、不解释为什么**）"
          "—— 那是模型每次调用都会读到的地方，写在文档里没用"
          "（注意：描述里当然会出现「原因」二字，出现的位置正是**禁止**它）",
          tool.description[:0])

    # ── 4. 接线闸：主 agent 的工具面里真的有它 ──
    main_src = (_ROOT / "src" / "agent" / "main_agent.py").read_text(encoding="utf-8")
    check("from agent.tools.attribution_analyze import attribution_analyze_tool" in main_src,
          "④ 主 agent 源码里有 import 行（接线闸，照 verify_retrieval_wiring 的写法）")
    check("tools=[*mcp_tools, build_report_tool, attribution_analyze_tool]" in main_src,
          "④ 与 `build_report` 同层注册（挂主 agent，不是子 agent —— 指纹在主 state，§4.4）")

    # ── 5. `load_cube_metadata` 的**加法**扩展（既有三键一个字节不动）──
    project = _write_tool_project()
    meta = load_cube_metadata(str(project), "demo")
    check(meta.get("measures") == {
        "total_hours": "工时合计", "record_count": "报工条数",
        "avg_hours_per_record": "单条平均工时", "user_count": "报工人数",
    }, "⑤ 既有键 `measures` 仍是 `{name: 描述字符串}`（换形状会静默打断 report_builder 的口径层）",
          meta.get("measures"))
    check(meta.get("dimensions") == {"dept": "部门", "project_name": "项目"},
          "⑤ 既有键 `dimensions` 形状不变", meta.get("dimensions"))
    check(meta.get("cube_description") == "夹具立方体", "⑤ 既有键 `cube_description` 不变")
    check(meta.get("measure_expressions", {}).get("total_hours") == "SUM(work_hour)"
          and meta["measure_expressions"].get("user_count") == "COUNT(DISTINCT user_id)",
          "⑤ 新增平行键 `measure_expressions`（形状闸的真源，原本被丢掉）",
          meta.get("measure_expressions"))
    check(meta.get("dimension_expressions", {}).get("dept") == "dept",
          "⑤ 新增平行键 `dimension_expressions`")
    check(meta.get("time_dimensions") == {"work_date": {
        "expression": "work_date", "type": "date", "levels": ["year", "month", "day"]}},
        "⑤ 新增平行键 `time_dimensions`（带 type/levels）", meta.get("time_dimensions"))
    check("work_date" not in meta.get("dimensions", {})
          and "work_date" in meta.get("time_dimensions", {}),
          "⑤ ★ 时间维**不在** `dimensions` 里（yml 里是两个平级列表）—— 白名单必须分开取，"
          "否则工时库唯一的锚点会被判成非法维度")

    # ── 6. 真语义库上的分布（本机有生产活跃库的 checkout 才跑）──
    if _PROJECT_CHECKOUT.is_dir():
        import collections

        import yaml

        dist = collections.Counter()
        cubes = _PROJECT_CHECKOUT / "cubes"
        for cube_dir in sorted(p for p in cubes.iterdir() if p.is_dir()):
            doc = yaml.safe_load((cube_dir / "metadata.yml").read_text(encoding="utf-8"))
            for item in doc.get("measures") or []:
                dist[aa.classify_measure_expression(item.get("expression"))] += 1
        total = sum(dist.values())
        check((total, dist["term_wise"], dist["unsupported"], dist["factor_shapley"]) == (88, 57, 9, 0),
              "⑥ ★ 真库 88 个度量上的分布：加性 57 / 不可加 9 / 乘积 0（与 §2.3 更正后的计数一致）",
              f"n={total} {dict(sorted(dist.items()))}")
        check(dist["mix_shift"] == 22,
              "⑥ 比值+AVG = 22（其中 `avg_hours_per_user` 含 `COUNT(DISTINCT)` 但按比值归类 —— "
              "§2.3 原写的「比值 11」把这一条数进了 DISTINCT 那一格，两处计数对不上，已更正）",
              dist["mix_shift"])
    else:
        print(f"  --  跳过段⑥：本机没有 {_PROJECT_CHECKOUT}")

    # ── 7. 开关 fail-closed：关着时**连库名都不解析、不建 runner** ──
    calls: list[str] = []
    with _PatchAttr(ar, "get_runner", _forbid(calls, "get_runner")), \
            _PatchAttr(ar, "run_attribution", _forbid(calls, "run_attribution")), \
            _PatchAttr(aa, "_resolve_db_name", _forbid(calls, "resolve_db_name")):
        off = _last_payload(_run_tool({"answer_fingerprints": {"t1": {"db_name": "witops"}}}))
    check(off.get("code") == "ATTRIBUTION_DISABLED" and calls == [],
          "⑦ ★ 开关默认 `off` ⇒ 回 `ATTRIBUTION_DISABLED`，且**一次解析/构造都没有**"
          "（fail-closed：关着时不碰库名、不建连接）", f"code={off.get('code')} calls={calls}")

    # ── 8. 缺省解析：无指纹 / 库名三级全空 ⇒ 类型化拒绝（且不建 runner）──
    with _EnvFlags(**{aa._ENV_ENABLE: "1"}):
        calls = []
        with _patch_wren_ctx(project), \
                _PatchAttr(aa, "_resolve_db_name", lambda *a, **k: "demo"), \
                _patch_get_runner([]), \
                _PatchAttr(ar, "run_attribution", _forbid(calls, "run_attribution")):
            no_fp = _last_payload(_run_tool({}))
        check(no_fp.get("code") == "NO_FINGERPRINT"
              and all(k in no_fp.get("message", "") for k in ("cube", "measures", "baseline", "current")),
              "⑧ 无指纹且未显式传参 ⇒ `NO_FINGERPRINT`，文本点名缺的是哪几样", no_fp.get("message"))
        check(no_fp.get("message", "").count("本轮 state 里没有答案指纹") == 1,
              "⑧ 错误文本说清「为什么取不到」（本轮没有指纹 ≠ 指纹里缺字段）")
        check(calls == [], "⑧ 负对照：缺指纹时**一条查询都不发**（不猜库、不连库）", calls)

        calls = []
        with _PatchAttr(aa, "_resolve_db_name", lambda *a, **k: ""), \
                _PatchAttr(wce, "load_cube_metadata", _forbid(calls, "load_cube_metadata")), \
                _PatchAttr(wce, "resolve_wren_ctx_by_db", _forbid(calls, "resolve_wren_ctx_by_db")):
            no_db = _last_payload(_run_tool({}, db_name=""))
        check(no_db.get("code") == "UNKNOWN_DB" and calls == [],
              "⑧ 库名三级全空 ⇒ `UNKNOWN_DB`（**不猜一个库**），且连 cube 元信息都不去读",
              no_db.get("code"))

        # cube 名**有**（指纹里写着）、语义层里没有 ⇒ `UNKNOWN_CUBE`。
        # 与上一条分开测：两条路给同一个码会让模型分不清「我该补参数」还是「这库没这个 cube」。
        # 这里走的是**闸③-a**（`.cubes/` 目录名集合，S4）—— 把 dir 里没有的名字拦在拼路径之前。
        calls = []
        with _patch_wren_ctx(project), _patch_get_runner([]), \
                _PatchAttr(ar, "run_attribution", _forbid(calls, "run_attribution")):
            no_cube = _last_payload(_run_tool({"answer_fingerprints": {
                "t1": _fingerprint(final_cube_spec={"cube": "nope", "measures": ["total_hours"],
                                                    "time_dimension": "work_date"})}}))
        check(no_cube.get("code") == "UNKNOWN_CUBE" and calls == []
              and "nope" in no_cube.get("message", "") and "demo" in no_cube.get("message", ""),
              "⑧ cube 名有、语义层里没有 ⇒ `UNKNOWN_CUBE`（点名是哪个 cube + 列出可用名；"
              "重试同一个名字无意义，与 `NO_FINGERPRINT` 的处置完全不同）", no_cube.get("code"))

        # 名字**在** `cubes/` 里、但 `metadata.yml` 读不出来 ⇒ 同一个码、**另一条路**
        # （目录被删了一半 / yml 语法坏）。两条路的下一步动作不同：这条改名字没用。
        calls = []
        with _patch_wren_ctx(project), _patch_get_runner([]), \
                _PatchAttr(wce, "load_cube_metadata", lambda *a, **k: {}), \
                _PatchAttr(ar, "run_attribution", _forbid(calls, "run_attribution")):
            broken = _last_payload(_run_tool({"answer_fingerprints": {"t1": _fingerprint()}}))
        check(broken.get("code") == "UNKNOWN_CUBE" and calls == []
              and "读不出来" in broken.get("message", ""),
              "⑧ 目录在、声明读/解析失败 ⇒ 同码但说清是**语义库的问题**（改 cube 名重试没用）",
              broken.get("message"))

        # 指纹有，但窗口是 `None`（S8 里「问的不是时间对比」就写 None）⇒ 与「没有指纹」
        # 同一个码、但文本要说清缺的是窗口 —— 否则模型不知道自己该补什么。
        calls = []
        hollow = _fingerprint(windows=None)
        with _patch_wren_ctx(project), _patch_get_runner([]), \
                _PatchAttr(ar, "run_attribution", _forbid(calls, "run_attribution")):
            no_win = _last_payload(_run_tool({"answer_fingerprints": {"t1": hollow}}))
        check(no_win.get("message", "").startswith("归因需要 baseline, current"),
              "⑧ ★ `windows: None` 的指纹 ⇒ 只点名缺的是**两期窗口**（cube/measures 都读到了，"
              "不该一起报缺）—— 缺什么报什么，模型才知道补哪个参数", no_win.get("message"))
        check(calls == [], "⑧ 负对照：窗口缺时同样一条 SQL 都不发", calls)

    # ── 9. 端到端（离线）：入参 → 形状闸 → 编译 → 执行 → 内核 → 六键载荷 ──
    executed: list[str] = []
    planner = _FakePlanner()
    with _EnvFlags(**{aa._ENV_ENABLE: "1"}), _patch_planner(planner), \
            _patch_wren_ctx(project), _patch_get_runner(executed):
        # 全部入参都从指纹来（模型只说一句「归因一下」的那条路）。
        payload = _last_payload(_run_tool({"answer_fingerprints": {"t1": _fingerprint()}}))

    check(set(payload) == {"measures", "primary_measure", "per_measure", "warnings",
                           "comparison_metadata", "queries_executed"},
          "⑨ ★ 返回**恒定六键**（§4.3）；SQL 取证进日志与 `comparison_metadata`，不进载荷",
          sorted(payload))
    check(payload.get("measures") == ["total_hours", "record_count"]
          and payload.get("primary_measure") == "total_hours",
          "⑨ 度量列表去重保序、主度量 = 第一个（指纹里的顺序直读）", payload.get("measures"))
    check(set(payload.get("per_measure") or {}) == {"total_hours", "record_count"}
          and "total_hours" not in payload, "⑨ ★ 结果**只在 `per_measure` 下**，顶层不镜像主度量")
    delta = (payload.get("per_measure") or {}).get("total_hours", {}).get("total_change", {})
    check(delta.get("baseline_value") == 70.0 and delta.get("current_value") == 85.0
          and delta.get("delta") == 15.0,
          "⑨ 端到端数字对得上（70 → 85，Δ=15）：真 `get_runner` + 真 runner + 真内核", delta)
    check((payload.get("per_measure") or {}).get("record_count", {})
          .get("total_change", {}).get("delta") == 0.0,
          "⑨ 第二个度量算的是**它自己那列**（record_count 两期都是 7 ⇒ Δ=0）"
          "—— 串列会让这条立刻红")
    check([r["dimension"] for r in (payload.get("per_measure") or {})
           .get("total_hours", {}).get("dimension_ranking", [])] == ["dept", "project_name"],
          "⑨ ★ 缺省 `candidate_dimensions` = 该 cube 声明的**全部维度**（模型没传就全测）")
    warn_codes = {w.get("code") for w in (payload.get("warnings") or [])}
    check("non_additive_dimension" not in warn_codes,
          "⑨ ★ 加性对账对**每个**度量都精确通过（夹具 Σ分组 == 总量 ⇒ 不该有 "
          "`non_additive_dimension`）—— 有它就说明多度量把某一列的对账算错了",
          sorted(warn_codes))
    check(warn_codes == {"zero_total_delta_with_component_changes"}
          and {w.get("measure") for w in payload["warnings"]} == {"record_count"},
          "⑨ ★ 唯一的提醒是 `record_count`「总量没变（7→7）但分量此消彼长」，且**只标在它身上** "
          "—— total_hours 涨了 15，不该跟着报同一条（多度量里警告串味就会这样误导）",
          payload.get("warnings"))
    check((payload.get("per_measure") or {}).get("total_hours", {}).get("warnings") == [],
          "⑨ 负对照：`total_hours` 自己那份 warnings 是**空的**（内层保持内核逐字，不做并集）")
    meta_out = payload.get("comparison_metadata") or {}
    check(meta_out.get("fingerprint") == "t1" and meta_out.get("source") == "attribution_analyze",
          "⑨ 参数透传：`comparison_metadata` 里能看出这次算的是**哪条指纹**（可复算）")
    check(meta_out.get("queries_executed") == 6 and payload.get("queries_executed") == 6,
          "⑨ ★ `queries_executed == 2+2D = 6`（D=2）—— 与度量数 M=2 **无关**", meta_out.get("queries_executed"))
    check(len(executed) == 6 and len(planner.calls) == 12,
          "⑨ ★ 真发出去 6 条、进程内编译 12 次（M×(2+2D)）：多度量**共用取数**，"
          "执行层按编译产物去重", f"执行={len(executed)} 编译={len(planner.calls)}")
    check(all(call["args"]["measures"] == ["total_hours", "record_count"] for call in planner.calls),
          "⑨ 每次都带**整组**度量（同一 cube 的多个度量一次调完，不是分次调用）",
          planner.calls[0]["args"]["measures"] if planner.calls else None)
    check(all("2026-07-01" in sql or "2026-08-01" in sql for sql in executed),
          "⑨ 窗口进了 SQL 谓词（两期各自的半开区间，不是「上个月」这类相对说法）")

    # ── 10. 负对照：一票否决 / 缺失度量 / 查询预算 / 硬顶 ──
    def _veto(**kwargs):
        """跑一次**显式入参**的调用，返回 (载荷, 编译次数) —— 形状闸的各种拒法。"""
        calls_local: list[str] = []
        local_planner = _FakePlanner()
        base = dict(db_name="demo", cube="demo", time_dimension="work_date",
                    baseline=list(_TOOL_BASELINE), current=list(_TOOL_CURRENT),
                    candidate_dimensions=["dept"])
        base.update(kwargs)
        with _EnvFlags(**{aa._ENV_ENABLE: "1"}), _patch_planner(local_planner), \
                _patch_wren_ctx(project), _patch_get_runner(calls_local):
            out = _last_payload(_run_tool({}, **base))
        return out, local_planner.calls

    with _EnvFlags(**{aa._ENV_ENABLE: "1"}):
        veto, compiles = _veto(measures=["total_hours", "avg_hours_per_record"])
    check(veto.get("code") == "NON_TERM_WISE_MEASURE" and compiles == [],
          "⑩ ★ 一票否决：`[加性, AVG]` ⇒ 整调用被拒，且**一次编译都没发生**"
          "（只算支持的那几个、把 AVG 静默丢掉更坏）", f"code={veto.get('code')} 编译={len(compiles)}")
    check(veto.get("measures") == {"avg_hours_per_record": "mix_shift"},
          "⑩ 错误载荷**点名**是哪个度量、什么形状（模型据此去掉它重试）", veto.get("measures"))
    check("mix_shift" not in str(veto.get("message")) and "比值" in str(veto.get("message")),
          "⑩ 错误文本给人话标签（「比值型」）而不是内部枚举名（`mix_shift`）", veto.get("message"))

    with _EnvFlags(**{aa._ENV_ENABLE: "1"}):
        unsupported, compiles = _veto(measures=["user_count"])
    check(unsupported.get("code") == "UNSUPPORTED_MEASURE"
          and unsupported.get("measures") == {"user_count": "unsupported"},
          "⑩ 全是不可加形状 ⇒ 用 `UNSUPPORTED_MEASURE`（与「非加性」分开：模型的重试方式不同）",
          unsupported.get("code"))

    with _EnvFlags(**{aa._ENV_ENABLE: "1"}):
        bogus, compiles = _veto(measures=["not_a_measure"])
    check(bogus.get("code") == "MISSING_MEASURE"
          and bogus.get("measures") == {"not_a_measure": "not_found"},
          "⑩ 名字不在 cube 声明里 ⇒ `MISSING_MEASURE`（不按「不可加」报 —— 那是两回事）",
          bogus.get("code"))

    with _EnvFlags(**{aa._ENV_ENABLE: "1", aa._ENV_BUDGET: "4"}):
        over, compiles = _veto(measures=["total_hours"], candidate_dimensions=None)
    check(over.get("code") == "INVALID_ARGUMENT" and "dept" in over.get("message", "")
          and "project_name" in over.get("message", ""),
          "⑩ ★ 查询预算超限（D=2 ⇒ 2+2D=6 > 4）⇒ 拒，并把**可用维度名**列出来让模型挑 —— "
          "「你少传了参数」那种错误文本模型没法据此重试", over.get("message"))
    check("需要 6 条聚合查询" in over.get("message", "")
          and "超出预算 4" in over.get("message", ""),
          "⑩ 预算错误文本带上「需要几条 / 预算几条」（否则模型不知道要砍到几个维度）")

    with _EnvFlags(**{aa._ENV_ENABLE: "1"}):
        hard, compiles = _veto(measures=["total_hours"], max_values_per_dimension=1001)
    check(hard.get("code") == "INVALID_ARGUMENT" and compiles == [],
          "⑩ 单维度取值数超硬顶 1000 ⇒ 在碰 state / 建 runner **之前**就拒",
          hard.get("code"))

    # 开关的读法本身（`_env_int` 的失败方向：乱填 ⇒ 回默认，不静默变 0）
    with _EnvFlags(**{aa._ENV_BUDGET: "十"}):
        check(aa._env_int(aa._ENV_BUDGET, 12) == 12,
              "⑩ `NL2SQL_ATTRIBUTION_QUERY_BUDGET=十` ⇒ 回默认 12（不是 0 —— 那会让所有调用被拒）")
    with _EnvFlags(**{aa._ENV_BUDGET: "0"}):
        check(aa._env_int(aa._ENV_BUDGET, 12) == 12, "⑩ `=0` 同样回默认（0 没有合理语义）")
    with _EnvFlags(**{aa._ENV_ENABLE: "TRUE "}):
        check(aa.attribution_enabled() is True, "⑩ 开关读法：`\"TRUE \"` 也算开（strip + lower）")
    with _EnvFlags(**{aa._ENV_ENABLE: "enabled"}):
        check(aa.attribution_enabled() is False,
              "⑩ 负对照：不认识的写法（`enabled`）一律当**关**（fail-closed 的方向）")


# ══════════════════════════════════════════════════════════════════════
# S4 三道闸（方案 §4.5）：本工具不在任何 SQL 中间件的名单里，闸只能建在工具内部
#
# 三条各自独立、缺一不可：
#   ① 只读硬闸 —— 在 runner（`_compile_checked`），本段验的是**接线**（编译产物真会过闸），
#      判据本身（`classify_sql`）在段⑨ 已断言；
#   ② 判权 —— 在工具内、**建连接之前**；
#   ③ 入参白名单 —— 四个名字各自对**正确的集合**判成员（`time_dimension` 对
#      `time_dimensions`，不是 `dimensions`），filters 判算子枚举与维度名。
#
# 每一条都配负对照：拒的时候**一次编译都不能发生**（说明它拦在前面，而不是事后）。
# ══════════════════════════════════════════════════════════════════════
# 路径穿越探针：cube 名会被拼进文件路径（`cubes/<cube>/metadata.yml`）。这个目录**故意**
# 带一份合法声明 —— 若闸③-a 不在拼路径之前拦，「`../../…`」是能真的读到它的。
_TRAVERSAL_CUBE = "../../attribution_traversal_probe"


def s15_three_gates() -> None:
    section("⑮ S4 三道闸：只读硬闸接线 / 判权（不启用≠拒）/ cube·维度·时间锚点·filters 白名单")
    from agent.auth import runtime as art
    from agent.tools import attribution_analyze as aa
    from agent.utils import attribution_runner as ar
    from agent.utils import wren_call_extract as wce

    project = _write_tool_project()

    # ── 1. 闸①：只读硬闸的**接线**（判据本身在段⑨：`classify_sql('DROP TABLE t')` = write）──
    # 用假编译器**故意**产出一条写语句：先证它确实产得出来（负对照），再证 `_compile_checked` 拒。
    write_sql = "DELETE FROM do_work_hour_detail_extend"
    gate1_runner = ar.WrenCubeRunner(
        "P", {"datasource": "mysql"}, "demo", cube="demo",
        measures=["total_hours"], time_dimension="work_date", executor=lambda sql: [],
    )
    window = Window(*_TOOL_BASELINE)
    with _patch_planner(_FakePlanner(sql_override=write_sql)):
        leaked = gate1_runner._compile(dimensions=("dept",), window=window)
        try:
            gate1_runner._compile_checked(dimensions=("dept",), window=window)
            check(False, "⑮ 闸①：写语句必须被拒")
        except TypedError as error:
            check(error.code == "INVALID_ARGUMENT" and "只读" in error.message,
                  "⑮ ★ 闸① 接线：编译产物过 `classify_sql` 判 write ⇒ 拒答（`INVALID_ARGUMENT`）",
                  error.code)
    check(leaked == write_sql,
          "⑮ 闸① 的负对照：这条写语句**确实**由 `_compile` 产出（否则上面那条是空断言）",
          _tail(leaked, 60))

    # ── 2. 闸② 判权：三种身份各自的结果（★ 不启用 ≠ 拒）──
    with _PatchAttr(art, "resolve_caller", lambda *a, **k: None):
        check(aa._caller_can_use_db("demo") is True,
              "⑮ ★ 闸②：`resolve_caller` 回 `None`（内部调用 / dev / 读用户记录失败）⇒ **放行** —— "
              "与 `query_gate` / `tool_filter` 同一条约定；在这里 fail-closed 会把 dev34 与"
              "离线套件整片拒掉，而真正的越权走的是下面那条正常 False")
    grants: list[tuple] = []

    def _grant(user, db, component=None):
        grants.append((user, db, component))
        return db == "demo"

    with _PatchAttr(art, "resolve_caller", lambda *a, **k: {"user_id": "u1", "is_admin": False}), \
            _PatchAttr(art, "caller_can_access_db", _grant):
        check(aa._caller_can_use_db("demo") is True and aa._caller_can_use_db("other") is False,
              "⑮ ★ 闸②：身份真实 ⇒ 逐库判（有授权的库放行、没授权的拒）")
    check(grants and grants[0] == ({"user_id": "u1", "is_admin": False}, "demo",
                                  "attribution_analyze"),
          "⑮ 闸② 交给授权判定的就是 `resolve_caller` 的 user 与 §1.2 解析后的库名"
          "（component 写成本工具名，日志可追溯）", grants[:1])

    # 端到端越权：**一次 project_path / 连接都不能算出来**（§4.5 最后一行那句）。
    calls: list[str] = []
    with _EnvFlags(**{aa._ENV_ENABLE: "1"}), \
            _PatchAttr(aa, "_resolve_db_name", lambda *a, **k: "other"), \
            _PatchAttr(art, "resolve_caller", lambda *a, **k: {"user_id": "u1"}), \
            _PatchAttr(art, "caller_can_access_db", lambda *a, **k: False), \
            _PatchAttr(wce, "resolve_wren_ctx_by_db", _forbid(calls, "resolve_wren_ctx_by_db")), \
            _PatchAttr(ar, "get_runner", _forbid(calls, "get_runner")):
        denied = _last_payload(_run_tool({}))
    check(denied.get("code") == "DB_NOT_AUTHORIZED" and calls == [],
          "⑮ ★ 端到端越权 ⇒ `DB_NOT_AUTHORIZED`，且**语义层项目路径与连接一次都没算**"
          "（闸② 的位置就是这条断言的判据）",
          f"code={denied.get('code')} calls={calls}")
    check("重试同一个库没有用" in denied.get("message", ""),
          "⑮ 越权文本写明**下一步动作**（换库 / 找管理员）—— 与「重试也没用」的其它码同一条纪律",
          denied.get("message"))

    # ── 3. 闸③：四个名字各自对正确的集合判成员 ──
    def _veto3(**kwargs):
        """闸③ 的拒法：跑一次**显式入参**调用，返回 (载荷, 编译器调用表)。"""
        local_planner = _FakePlanner()
        base = dict(db_name="demo", cube="demo", time_dimension="work_date",
                    baseline=list(_TOOL_BASELINE), current=list(_TOOL_CURRENT),
                    candidate_dimensions=["dept"], measures=["total_hours"])
        base.update(kwargs)
        with _EnvFlags(**{aa._ENV_ENABLE: "1"}), _patch_planner(local_planner), \
                _patch_wren_ctx(project), _patch_get_runner([]):
            out = _last_payload(_run_tool({}, **base))
        return out, local_planner.calls

    # 3-a cube 名：`cubes/` 的目录名集合（夹具项目里只有 `demo`）
    cube_bad, compiles = _veto3(cube="workhour_analysis")
    check(cube_bad.get("code") == "UNKNOWN_CUBE" and compiles == []
          and "demo" in cube_bad.get("message", ""),
          "⑮ ★ 闸③-a：cube 名不在 `cubes/` 里 ⇒ `UNKNOWN_CUBE` 并列出**可用 cube 名**，"
          "且一次编译都没发生（跨 cube 的指标要分次调用）", cube_bad.get("message"))

    # 3-a′ 路径穿越的负对照：探针目录里**真的**有一份合法声明 ⇒ 这条闸不是空转的
    probe = pathlib.Path(_TMP_ROOT) / "attribution_traversal_probe"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / "metadata.yml").write_text(_TOOL_CUBE_YML, encoding="utf-8")
    traversal, compiles = _veto3(cube=_TRAVERSAL_CUBE)
    check((probe / "metadata.yml").is_file() and traversal.get("code") == "UNKNOWN_CUBE"
          and compiles == [],
          "⑮ ★ 负对照：`cube=\"../../…\"` 指向的目录里**真有**一份可读声明，仍然被拒 —— "
          "先判目录名成员，拼路径的越界读按构造不存在",
          traversal.get("code"))

    # 3-b 维度名：`dimensions` 的键集
    dim_bad, compiles = _veto3(candidate_dimensions=["部门"])
    check(dim_bad.get("code") == "MISSING_DIMENSION" and compiles == []
          and dim_bad.get("dimensions") == ["dept", "project_name"],
          "⑮ ★ 闸③-b：维度名不在声明里（用了中文标签）⇒ `MISSING_DIMENSION` + 可用维度清单",
          dim_bad.get("dimensions"))

    # 3-c 时间锚点：**`time_dimensions`** 的键集（不是 `dimensions`）
    time_bad, compiles = _veto3(time_dimension="work_month")
    check(time_bad.get("code") == "MISSING_TIME_DIMENSION" and compiles == []
          and time_bad.get("time_dimensions") == ["work_date"],
          "⑮ ★ 闸③-c：锚点不在 `time_dimensions` 里 ⇒ `MISSING_TIME_DIMENSION` + 可用锚点清单",
          time_bad.get("time_dimensions"))
    cross, compiles = _veto3(time_dimension="dept")
    check(cross.get("code") == "MISSING_TIME_DIMENSION" and compiles == [],
          "⑮ ★ 闸③-c′：`dept` 是**真维度但不是时间维度** ⇒ 同样被拒 —— "
          "两个列表不平级混用的话，这条会被放行到编译期才炸（而 `work_date` 会被误判成非法）",
          cross.get("code"))

    # 3-d filters：`operator` 限枚举（★ pydantic 的 `json_schema_extra` 只给模型看，**不校验**）
    loose = aa.AttributionFilter(dimension="dept", operator="like", value="A%")
    check(loose.operator == "like",
          "⑮ ★ 前置事实：`operator=\"like\"` **构造得出来** —— `json_schema_extra` 的 enum 是给模型看的，"
          "不是校验 ⇒ 闸③-d 是唯一拦它的地方", loose.operator)
    op_bad, compiles = _veto3(filters=[loose])
    check(op_bad.get("code") == "INVALID_FILTER" and compiles == []
          and all(name in op_bad.get("message", "") for name in aa.FILTER_OPERATORS),
          "⑮ ★ 闸③-d：算子不在枚举内 ⇒ `INVALID_FILTER` 并把允许集合报出来", op_bad.get("message"))

    filter_dim_bad, compiles = _veto3(
        filters=[aa.AttributionFilter(dimension="not_a_dim", operator="eq", value="A")])
    check(filter_dim_bad.get("code") == "INVALID_FILTER" and compiles == []
          and "dept" in filter_dim_bad.get("message", "")
          and "work_date" in filter_dim_bad.get("message", ""),
          "⑮ ★ 闸③-d：`filters.dimension` 不在白名单 ⇒ `INVALID_FILTER`；"
          "白名单 = `dimensions ∪ time_dimensions`（窗口下推本身就是同一个形状的过滤，"
          "把时间维度判成非法会让「只归因某一年」无法表达）", filter_dim_bad.get("message"))

    # 3-d′ value **原样**交给引擎：不拼接、不转义（转义是 wren 的活，段⑩ 已实测）
    hostile = "A' OR 1=1 --"
    passed, calls_f = _veto3(
        filters=[aa.AttributionFilter(dimension="dept", operator="eq", value=hostile)])
    specs = [spec for call in calls_f for spec in (call["args"].get("filters") or [])]
    check(f"dept:eq:{hostile}" in specs,
          "⑮ ★ 闸③-d′：带引号/注释符的 value **逐字**进编译器的 `filters`（我们不改写、不转义）—— "
          "真正的兜底是闸① 与 wren 自己的转义，不是在这里做字符串过滤",
          specs[:2])

    # ── 4. 结构性：两道闸的**位置**也是契约（顺序错了，闸就从「拦」变成「事后解释」）──
    source = inspect.getsource(aa._attribution_analyze_coro)
    check(source.index("_caller_can_use_db(resolved_db)")
          < source.index("resolve_wren_ctx_by_db(resolved_db)"),
          "⑮ ★ 结构性：闸② 在源码里排在 `resolve_wren_ctx_by_db` **之前**")
    check(source.index("_list_cubes(project_path)")
          < source.index("load_cube_metadata(project_path, cube_name)"),
          "⑮ ★ 结构性：cube 白名单判定排在读 `metadata.yml` **之前**（先判成员、再拼路径）")
    check(all(name in source for name in ("_caller_can_use_db", "_list_cubes")),
          "⑮ 结构性：三道闸都在工具内部 —— 没有任何中间件按工具名后缀匹配得上 "
          "`attribution_analyze`（§4.5 的出发点）")


# ══════════════════════════════════════════════════════════════════════
# ⑯ S5：免截断 + 提示词（跨文件契约：改名/搬文件都会让 S5 静默失效）
# ══════════════════════════════════════════════════════════════════════
def s16_s5_exemption_and_prompt() -> None:
    section("⑯ S5 免截断与提示词")
    from agent.middlewares.message_slimmer import (
        MessageSlimmerMiddleware,
        _ATTRIBUTION_EXEMPT_TOOL_SUFFIXES,
        _is_attribution_tool,
    )
    from agent.tools import attribution_analyze as aa

    tool_name = aa.attribution_analyze_tool.name
    check(tool_name == "attribution_analyze"
          and _is_attribution_tool(tool_name),
          "⑯ ★ 免截断白名单匹配工具**真名**（从 `StructuredTool.name` 取，不是抄一份字面量）"
          " —— 改名会让豁免静默失效，这条钉住两边同源")

    # 真跑中间件：嵌套 JSON 超阈值 ⇒ 不落盘；负对照 ⇒ 落盘
    import json as _json
    from langchain_core.messages import ToolMessage
    from types import SimpleNamespace

    payload = _json.dumps(
        {"per_measure": {"m": {"per_dimension": {
            f"d{i}": {"values": [{"value": f"v{j}", "delta": -j} for j in range(60)]}
            for i in range(3)}}},
         "warnings": []},
        ensure_ascii=False,
    )
    # 撑到阈值以上（结构真实即可，不追求精确体积）
    payload = payload + (" " * max(0, 9000 - len(payload)))
    check(len(payload) > 8000, "⑯ 夹具载荷 > 8000（截断阈值）", len(payload))

    class _Backend:
        def __init__(self) -> None:
            self.writes: dict[str, str] = {}

        def write(self, path, content):
            self.writes[path] = content
            return SimpleNamespace(error=None)

        async def awrite(self, path, content):
            return self.write(path, content)

    def _run(name: str):
        be = _Backend()
        mw = MessageSlimmerMiddleware(backend=be, max_chars_before_truncate=8000)
        msg = ToolMessage(content=payload, name=name, tool_call_id="c1")
        return mw._process_tool_message_sync(msg, []), be

    kept, be_kept = _run(tool_name)
    dropped, be_dropped = _run("read_file")
    check(kept.content == payload and be_kept.writes == {},
          "⑯ 归因结果免落盘（截断后是半个 JSON，模型会照着被砍断的嵌套对象读错数字）")
    check(dropped.content != payload and len(be_dropped.writes) == 1,
          "⑯ 负对照：同内容非归因工具仍落盘 ⇒ 差异只由工具名决定")

    # 提示词：工具挂在**主 agent**（§4.4），玩法就必须写进**主 agent** 的提示词
    main_src = (_SRC / "agent/main_agent.py").read_text(encoding="utf-8")
    sub_src = (_SRC / "agent/graphs/nl2sql_agent.py").read_text(encoding="utf-8")
    check("attribution_analyze_tool" in main_src,
          "⑯ 工具注册在**主 agent** 的 tools 列表里（§4.4 的落地处）")
    check("attribution_analyze" not in sub_src,
          "⑯ ★ 子 agent 工具面里**没有**本工具 ⇒ 把 playbook 写进 "
          "`NL2SQL_SYSTEM_PROMPT.md` 是无效的（那是子 agent 的提示词，它调不到这个工具）")

    main_prompt = (_SRC / "agent/prompt/MAIN_AGENT_PROMPT.md").read_text(encoding="utf-8")
    check("## 归因分析 playbook（attribution_analyze）" in main_prompt,
          "⑯ ★ playbook 落在 `MAIN_AGENT_PROMPT.md`（Langfuse 名 `main_system_prompt`）")
    check("不解释" in main_prompt and "因果" in main_prompt,
          "⑯ playbook 首条即 T3 红线：只做贡献度分解、不许编造因果")
    check("分次调用" in main_prompt and "跨 cube" in main_prompt,
          "⑯ playbook 写明跨 cube 分次调用（一次调用只编译一个 cube）")
    check("不要从聊天记录里猜" in main_prompt,
          "⑯ playbook 禁止从聊天记录猜日期/指标名（幻觉的温床）")
    for code in ("non_additive_dimension", "high_cardinality_dimension",
                 "dimension_analysis_failed", "unequal_windows"):
        check(code in main_prompt, f"⑯ playbook 逐条点名 `{code}` 的处置方式")
    check("dimension_ranking" in main_prompt and "不是低分维度" in main_prompt,
          "⑯ playbook 写明「没进 `dimension_ranking` 的维度不是低分维度」（防静默降级被转述成结论）")
    check("ATTRIBUTION_DISABLED" in main_prompt and "不要重试" in main_prompt,
          "⑯ playbook 说明开关关闭是**按设计的拒绝**、直接换 run_sql，不要重试")
    # 这条是**防提示词撒谎**：`strategy=unsupported` 的成因是 `no_additive_dimensions`（内核实证），
    # 不是「比值/平均」—— 后者在工具层就被 `NON_TERM_WISE_MEASURE` 整调用拒了。
    check("unsupported_reason" in main_prompt and "no_additive_dimensions" in main_prompt,
          "⑯ ★ playbook 说的 `unsupported` 成因与内核一致（`unsupported_reason`/`no_additive_dimensions`，"
          "不是「非加性指标」那条 —— 那条走的是提前拒答）")
    check("NON_TERM_WISE_MEASURE" in main_prompt and "不会只算" in main_prompt,
          "⑯ playbook 写明不可加指标是整调用拒答、不是少算几个")

    sub_prompt = (_SRC / "agent/prompt/NL2SQL_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    check("attribution_analyze" not in sub_prompt,
          "⑯ ★ 子 agent 提示词不提本工具（提了就是教它调一个不在工具面里的东西）")


# ══════════════════════════════════════════════════════════════════════
# ⑰ 生产事故回归（trace 476c977f13d460e5f9a3e9b3e9f8c268，2026-09-30）
#    `_attribution_analyze_coro() missing 1 required positional argument: 'runtime'`
# ══════════════════════════════════════════════════════════════════════
def s17_runtime_injection() -> None:
    section("⑰ runtime 注入必须对**校验层**可见（真 ToolNode 回归）")
    from typing import Annotated as _Ann

    from langchain.tools import ToolRuntime
    from langchain_core.messages import AIMessage
    from langchain_core.tools import InjectedToolArg
    from langchain_core.tools.base import _is_injected_arg_type
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    from agent.tools import attribution_analyze as aa

    tool = aa.attribution_analyze_tool

    # ① 权威断言。注入要过两层，两层解析方式**不同**：ToolNode 用 `get_type_hints`
    #    （能解析字符串），而 `StructuredTool._injected_args_keys` 用**未解析**的
    #    `signature(fn).parameters[k].annotation` ⇒ future-annotations 会让它恒 False。
    check("runtime" in tool._injected_args_keys,
          "⑰ ★ `_injected_args_keys` 含 `runtime` ⇒ 校验阶段不会把它当普通字段丢掉"
          "（判据取工具对象本身，不读源码文本）")
    _ann = _Ann[ToolRuntime, InjectedToolArg()]
    check(_is_injected_arg_type(_ann) is True
          and _is_injected_arg_type("Annotated[ToolRuntime, InjectedToolArg()]") is False,
          "⑰ ★ 机制对照：同一标注，**对象**形状判 True、**字符串**形状判 False "
          "⇒ 本文件的 `from __future__ import annotations` 就是事故的充分原因")
    _future = [
        node for node in ast.walk(ast.parse(pathlib.Path(aa.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
    ]
    check(not _future,
          "⑰ 本文件不再带 future-annotations（**AST 判据**，不是子串匹配 —— 模块说明里"
          "会写到这行 import 本身；上一条才是权威判据）")

    def _call_via_toolnode(args: dict) -> str:
        """把工具挂进最小 ToolNode 真跑一次 —— 注入只在这条路径上发生。

        只带 `coroutine` 的 `StructuredTool` 不支持同步调用 ⇒ 必须走 `ainvoke`。
        """
        g = StateGraph(MessagesState)
        g.add_node("tools", ToolNode([tool]))
        g.add_edge(START, "tools")
        g.add_edge("tools", END)
        app = g.compile()
        try:
            out = asyncio.run(app.ainvoke({"messages": [AIMessage(content="", tool_calls=[
                {"name": tool.name, "args": args, "id": "call_1", "type": "tool_call"}])]}))
        except Exception as e:  # noqa: BLE001
            # ToolNode 默认把工具内的异常**重新抛出**（`_default_handle_tool_errors`），
            # 于是装配层故障会直接把整个脚本打断。这里收回成可读文本 —— 断言照样红，
            # 但后面的段还能跑完。（生产上它表现为 TOOL 级 ERROR，不是崩进程。）
            return f"!!{type(e).__name__}: {e}"
        content = out["messages"][-1].content
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    def _code_of(text: str) -> str | None:
        try:
            return json.loads(text).get("code")
        except Exception:  # noqa: BLE001  非 JSON 说明崩在了装配层，交给断言去报
            return None

    # 生产那一轮的真实入参（逐字取自 trace 的 tool_call args）
    prod_args = {
        "cube": "workhour_analysis", "db_name": "witops", "measures": ["total_hours"],
        "time_dimension": "work_date",
        "baseline": ["2026-08-01", "2026-09-01"], "current": ["2026-09-01", "2026-10-01"],
    }
    # 本脚本宣称离线：工具链路的上游（env_loader）会顺手把 LangSmith 打开，这里关掉它，
    # 免得跑真图时出网（⑰ 是最后一个段，置为 false 不影响别的段）。
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"

    saved = os.environ.pop(aa._ENV_ENABLE, None)
    try:
        off = _call_via_toolnode(prod_args)
    finally:
        if saved is not None:
            os.environ[aa._ENV_ENABLE] = saved
    check(_code_of(off) == "ATTRIBUTION_DISABLED"
          and "missing 1 required positional argument" not in off,
          "⑰ ★ 关着开关走真 ToolNode：按设计拒答（`ATTRIBUTION_DISABLED`），"
          "而不是在装配层抛「缺少 runtime」—— 生产上那一轮的死法",
          f"code={_code_of(off)}")

    # 开开关 + 故意不存在的库：**必须越过 `runtime.state` 那一行**才可能拿到下面这两个码，
    # 所以这条同时证明了「注进来的 runtime 真的可读」。
    os.environ[aa._ENV_ENABLE] = "on"
    try:
        on = _call_via_toolnode({**prod_args, "db_name": "__verify_no_such_db__"})
    finally:
        os.environ.pop(aa._ENV_ENABLE, None)
        if saved is not None:
            os.environ[aa._ENV_ENABLE] = saved
    check(_code_of(on) in ("UNKNOWN_DB", "DB_NOT_AUTHORIZED")
          and "无法读取当前对话状态" not in on,
          "⑰ ★ 开着开关走真 ToolNode（库名故意不存在）：拿到的是**判权之后的**码 ⇒ "
          "`runtime.state` 真的读到了（没注入会退化成「无法读取当前对话状态」）",
          f"code={_code_of(on)}")


# ══════════════════════════════════════════════════════════════════════
def main() -> None:
    s0_fixture_sanity()
    s1_query_sequence()
    s2_additivity_exact()
    s3_non_additive_not_ranked()
    s4_truncation_and_threshold()
    s5_segments_and_null()
    s6_zero_total_delta()
    s7_dimension_isolation()
    s8_error_codes()
    s9_runner_compile_contract()
    s10_runner_real_compiler()
    s12_answer_fingerprint()
    s13_multi_measure()
    s14_tool_shell()
    s15_three_gates()
    s16_s5_exemption_and_prompt()
    s17_runtime_injection()

    print()
    passed = sum(1 for ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过" + ("" if passed == len(results) else "   ← 有失败"))
    if passed == len(results):
        print(f"（落盘根：{_TMP_ROOT}）")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
