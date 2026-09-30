# -*- coding: utf-8 -*-
"""归因取数通道（S2）：把内核的两次方法调用落成 wren cube 的**编译 + 执行**。

**它是什么**：`attribution_core.AttributionRunner` 协议的真实实现 —— 内核要两样东西
（不带维度的期间总量、`GROUP BY dim` 的分组值），本模块负责「把这件事变成 SQL、
跑出结果、把结果归一成行 dict 列表」。算法、对账、排名全在内核里，本模块**不算任何数**。

**它不是什么**：不建因果（方案 §1.1 的 T3 红线）、不改口径、不判权（那要 `user`，
见下面「三道闸的分工」）、不落盘任何产物。

## 三个反直觉点（实施时最容易走偏的地方，都已被实测钉死）

① **窗口走 `filters`，不走 `time_dimension`。**
   cube 查询的常规写法是给 `time_dimension`（CLI 规格 `name:granularity[:start,end]`），
   但归因**不能**用那条路：本机用生产活跃库的 checkout 编译实测（`total_hours` ×
   `dept_level1`，窗口 2026-08）——
   - 走 `filters`（本模块）：`… FROM v_workhour WHERE work_date >= '2026-08-01'
     AND work_date < '2026-09-01' GROUP BY 1` —— 半开区间逐字兑现，且**没有 ORDER BY**；
   - 走 `time_dimension: "work_date:day:2026-08-01,2026-09-01"`：SQL 变成
     `SELECT DATE(work_date) AS work_date__day, dept_level1, SUM(…) … GROUP BY 1, 2 ORDER BY 1`
     —— **日期被塞进 GROUP BY**，同一个维度取值会出**多行**（每天一行）。
     内核按 `dict[维度值 → 度量值]` 解析，多行必然撞键 ⇒ `DUPLICATE_DIMENSION_KEY`。
   ⇒ 本模块**一个 `timeDimensions` 都不发**：`time_dimension` 只当**过滤目标**用。
   两期各带自己的 `gte`/`lt`，语义是 OSI 半开区间 `[start, end)`（方案 §1.2）。
   ⚠️ 而且 `time_dimension` 的值必须是 **cube 的维度名**（`time_dimensions[].name`），
   **不是物理列名**：传物理列名时编译报 `Unknown filter dimension '…' in cube '…'`
   （夹具实测栽过一次）。生产的 cube 里两者多数同名，所以这条只在真出问题时才暴露 ——
   工具层的白名单也必须按 `time_dimensions` 的键集取（方案 §4.4）。

② **`limit` 进不了 SQL，照传不误。**
   内核按参考实现把 `max_values_per_dimension + 1` 传给 `fetch_grouped`，语义是
   **判截断**而非分页。本仓兑现不了它：`wren_plan._finish` 对 cube 查询固定
   `append LIMIT 1001`（`effective_probe_limit(None)`），不接受传参（方案 §3.1）。
   ⇒ 本模块**收下这个参数但不用它**，判截断由内核看「返回行数是否触到 1001」完成。
   **不要**为了让它生效去改 `wren_plan`（方案 §4.3 方框明写）。

③ **整段 2+2D 必须在同一个线程里顺序跑，且那条线程不能是事件循环线程。**
   10 个 dbmcp 引擎的 `run_sql` 全是「async 外壳 + 同步实心」（`offload.py` 的模块
   docstring 记着这条），所以「顺序跑 12 条查询」= 12 次同步阻塞。跑在主循环上会把
   全站串起来（P1-14）。⇒ 异步门面 `run_attribution()` 用 `offload()` 把**整段**
   搬到工作线程；在内核线程里每次取数用 `asyncio.run()` 驱动引擎（工作线程没有运行的
   循环，合法）。不并发（方案 §3.4①：run 槽全局 10，不许按维度 fan-out）。

## 多度量：**一次取数喂多个度量**（查询组，方案 §2.1）

一份报告 / 一个问题经常牵涉**多个指标**（「工时和报工条数为什么都降了」）。多度量**不是**
多花几条查询 —— `plan_cube_sql` 的 `measures` 本来就是**列表**，`SELECT d, SUM(a), SUM(b) … GROUP BY 1`
一条就够。所以本模块：

- runner 收 `measures=[…]`，`_compile` 把**整组度量**编译进同一条 SQL；
- **内核一行不改**：它是单度量的（`Request.measure`），本模块对每个度量各跑一次
  `attribute(runner, Request(measure=m, …))`；同一批行里其余度量的列被内核
  `_resolve_column`（先整名匹配、再叶子名匹配，命中恰一个才采纳）**天然忽略**；
- 取数在**执行层**按 SQL 去重（`self._cache`）：M 个度量 ⇒ 编译 M×(2+2D) 次（进程内、毫秒级）、
  **执行仍只有 2+2D 次**。`queries_executed` 数的是**执行**，所以它恒 `2+2D`、与 M 无关
  （方案 §2.1 的成本公式 `G × (2+2D)`，G = 查询组数）。

**缓存生命周期 = runner 实例**（即一次调用），**不跨调用复用**：同一条 SQL 在毫秒级窗口里
执行两次本来就是重复劳动，而跨请求缓存会引入「读到旧的数」这类静默错数。
**键就是编译出的物理 SQL 本身** —— 两期窗口不同、维度不同 ⇒ SQL 不同 ⇒ 不会串台。

⚠️ **一次调用只有一个查询组**：`plan_cube_sql` 的 `args["cube"]` 是**单值**（不是列表），
编译契约本身表达不了跨 cube；`time_dimension` 也钉在 runner 上。跨 cube / 多时间锚点的问题
由**模型分次调用**，每次调用各自 `2+2D`、各自过预算（方案 §2.1 / §3.5）。

## 三道闸的分工（方案 §4.5）

`attribution_analyze` 不在任何现成中间件的名单里（那些按工具名后缀匹配），所以闸必须
建在自己身上。本模块只实现**闸① 只读硬闸** —— 因为它是唯一需要「编译产物 + 连接」
才能做的一道，而这两样只有这里同时握着；放在取数层意味着**任何**调用方都绕不过它。

- **闸①（在本模块）**：编译出的物理 SQL 过 `classify_sql`，`verdict == "write"` 直接拒。
  纵深防御 —— SQL 由我们自己编译，理论上必是 `SELECT`；代价只有一次正则，但它是
  「语义层被改坏 / 引擎行为变化」时的唯一兜底。
- **闸②（判权）/ 闸③（入参白名单）不在本模块**：两者都要 `user` 与 `db_name` 的上下文，
  属于工具层（已落地：`attribution_analyze._caller_can_use_db` / `_build_requests` / `_list_cubes`）。
  本模块的 `conn` 由 `get_runner` 解析，**闸②必须发生在它之前**
  （越权时连 `project_path` 都不该算出来）—— 这个顺序由工具层保证，段⑮ 有断言钉住。

## 编译失败要读**原因**

取编译结果走 `plan_cube_sql_checked`（失败抛 `CubePlanError`），**不是** fail-open 的
`plan_cube_sql`（失败恒 `{}`）。差别全在错误文本上：「filter 维度名写成物理列名」这类错
在 fail-open 那条路上只剩一句「编译不出 SQL」，本轮为此耗掉一整个诊断脚本；`_checked`
把引擎原话带进 `RuntimeError`，再由内核收敛成 `TOTAL_QUERY_FAILED` —— 那是 S3 的工具
反馈给模型、让它自己改参数的**唯一**信号。**不要**在错误文本里替引擎猜原因（比如写
「filter 请用 cube 维度名」）：维度名与列名同名时它会把对的写法说成错的。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Sequence

from agent.utils.attribution_core import (
    FILTER_OPERATORS,
    Request,
    TypedError,
    Window,
    attribute,
    column_leaf,
)

_logger = logging.getLogger(__name__)

# 窗口下推用的算子：半开区间只用到这两个（[start, end) ⇒ `>= start AND < end`）。
_WINDOW_OPERATORS = ("gte", "lt")

# 闸①：写/DDL 判定值（`sql_approval.classify_sql` 的三态之一）。
_WRITE_VERDICT = "write"


def _normalize_measures(measures: Sequence[str] | None, measure: str | None) -> list[str]:
    """`measures` / `measure` 两种写法 → 去重保序的度量列表。

    - 两个都给时 ``measures`` 胜出（新写法优先）；
    - 去重：同一个度量写两遍没有意义，而重复编译出的 SQL 一模一样 ⇒ 只会让
      「度量数 × 查询数」的账对不上；
    - 全空 ⇒ 空列表（编译时由引擎报「没有度量」，**本模块不自己造错误码**）。
    """
    listed = [str(item).strip() for item in (measures or ())]
    if not any(listed) and str(measure or "").strip():
        listed = [str(measure).strip()]
    return list(dict.fromkeys(item for item in listed if item))


# ══════════════════════════════════════════════════════════════════════
# 取数实现
# ══════════════════════════════════════════════════════════════════════
class WrenCubeRunner:
    """`AttributionRunner` 的 wren cube 实现：编译 → 闸① → 执行 → 行归一。

    ``executor`` 是**测试缝**（S6 离线用，生产不传）：给一个 ``(sql) -> 行`` 的可调用
    就跳过真数据库，其余路径（编译、闸①、列归一、2+2D 计数）全部照跑 —— 否则离线套件
    只能测到桩，测不到「真编译器产出的 SQL 长什么样」。

    ``conn`` 必须是 **wren 形状**（``{datasource, host, port, database, user[, password]}``，
    即 `resolve_wren_ctx_by_db` / `wren_conn_dict` 给的那种）—— 它只供**编译**用；
    执行走的是另一条独立通道（`McpSqlConfig.from_env(db_name)`，见 `_dbmcp_execute`）。
    少 ``datasource`` 的症状是「编译静默产不出 SQL」，不是报错，所以 `_compile` 的
    异常文本里专门提这个键。

    ``measures`` 是**查询组**里的度量列表（见模块 docstring「多度量」）：一次调用一个 cube、
    一个时间锚点、一对窗口，度量全进同一条 SQL。``measure``（单数）是**兼容旧调用方的写法**，
    等价于 ``measures=[measure]``；两个都给时 ``measures`` 胜出。
    """

    def __init__(
        self,
        project_path: str,
        conn: dict,
        db_name: str,
        *,
        cube: str,
        measure: str | None = None,
        measures: Sequence[str] = (),
        time_dimension: str | None = None,
        filters: Sequence[dict] = (),
        executor: Callable[[str], Any] | None = None,
    ) -> None:
        self._project_path = str(project_path or "")
        self._conn = conn if isinstance(conn, dict) else {}
        self._db_name = str(db_name or "")
        self._cube = str(cube or "")
        self._measures = _normalize_measures(measures, measure)
        # 主度量（第一个）—— 错误文本、`_label` 之外的展示都取它。
        self._measure = self._measures[0] if self._measures else ""
        # 只当**过滤目标**用，不进 `timeDimensions`（见模块 docstring 反直觉点 ①）。
        self._time_dimension = str(time_dimension or "")
        self._base_filters = [dict(item) for item in (filters or ())]
        self._executor = executor
        # 每条取数的记账（label / 行数 / 耗时 / SQL / 是否命中缓存）：给 S6 断言与排障用。
        # 上限天然是 M×(2+2D)，不会长。
        self._query_log: list[dict] = []
        # **执行层**缓存：键 = 编译出的物理 SQL。多度量共用同一条 SQL ⇒ 只真跑一次
        # （模块 docstring「多度量」）。生命周期 = 本实例，不跨调用复用。
        self._cache: dict[str, list[dict]] = {}

    # ── 给内核看的两个方法 ──────────────────────────────────────────
    def fetch_total(self, window: Window) -> list[dict]:
        """不带维度的聚合（两期各一次）。"""
        return self._run(dimensions=(), window=window)

    def fetch_grouped(self, dimension: str, window: Window, limit: int) -> list[dict]:
        """``GROUP BY dimension`` 的聚合。

        ``limit`` **刻意不用**：本仓的物理上限写死 1001（模块 docstring 反直觉点 ②）。
        收下它只为与内核/参考实现同构；判截断由内核看返回行数完成。
        """
        del limit  # 有意未使用，理由见 docstring
        return self._run(dimensions=(dimension,), window=window)

    # ── 记账（给 S6 与排障）────────────────────────────────────────
    @property
    def query_log(self) -> list[dict]:
        """本次 runner 生命周期内的**每一次取数调用**，按先后顺序。

        条目里有 ``cached`` 标志（多度量共用同一条 SQL 时第二轮是命中缓存的），
        所以 ``len(query_log)`` 在 M>1 时会大于 ``queries_executed``。
        """
        return [dict(entry) for entry in self._query_log]

    @property
    def queries_executed(self) -> int:
        """**真正发到数据库**的条数（命中缓存的不算）。

        S2 的完成判据是它等于 ``2 + 2D``；多度量**不改变**这个数（模块 docstring
        「多度量」），所以它同时是「度量数没把成本乘起来」的唯一判据。
        """
        return sum(1 for entry in self._query_log if not entry["cached"])

    # ── 主路径 ────────────────────────────────────────────────────
    def _run(self, *, dimensions: Sequence[str], window: Window) -> list[dict]:
        """一条取数：编译（含闸①）→ 缓存查执行 → 行归一 → 记账。

        **缓存查的是执行、不是编译**：编译每次都做（进程内毫秒级），这样
        `query_log` 里能看见「哪几个度量共用了一条 SQL」，而真跑的次数仍由
        `queries_executed` 如实反映。判截断、对账要的是**行**，两者都不受影响。
        """
        label = self._label(dimensions=dimensions, window=window)
        sql = self._compile_checked(dimensions=dimensions, window=window)
        hit = sql in self._cache
        started = time.monotonic()
        if hit:
            rows = self._cache[sql]
        else:
            rows = self._execute(sql)
            self._cache[sql] = rows
        self._query_log.append(
            {
                "label": label,
                "measures": list(self._measures),
                "rows": len(rows),
                "ms": int((time.monotonic() - started) * 1000),
                "sql": sql,
                "cached": hit,
            }
        )
        _logger.info(
            "[归因取数] %s → %d 行（%dms%s）",
            label,
            len(rows),
            self._query_log[-1]["ms"],
            "，命中缓存" if hit else "",
        )
        # 浅拷贝列表：缓存那份是给**后续**调用复用的，别让上层（内核或测试）拿到同一个
        # list 对象 —— 一旦有人 append/排序，缓存就被悄悄改了。
        return list(rows)

    def _label(self, *, dimensions: Sequence[str], window: Window) -> str:
        """取数标签。

        **不用 `total@baseline` / `dim=部门@current`**：协议的 `Window` 不带期名
        （内核只传窗口），凭空起个 `baseline`/`current` 的名字会让日志在「两期窗口写反」
        这种事故里**指错方向**。标签只写事实：取的是哪个窗口。
        """
        span = f"{window.start}~{window.end}"
        if not dimensions:
            return f"total@{span}"
        return f"dim={'+'.join(dimensions)}@{span}"

    def _compile_checked(self, *, dimensions: Sequence[str], window: Window) -> str:
        """编译物理 SQL 并过闸①（只读）。返回**待执行**的物理 SQL。"""
        sql = self._compile(dimensions=dimensions, window=window)
        from agent.middlewares.sql_approval import classify_sql  # 惰性：避免 import 环

        verdict, detail = classify_sql(sql)
        if verdict == _WRITE_VERDICT:
            # 类型化拒绝：这不是「工具故障」，是「按设计拒绝」（方案 §2.2 末行）。
            raise TypedError(
                "INVALID_ARGUMENT",
                f"归因只允许只读查询；编译出的语句被判为写/DDL（{detail or '未知'}），已拒绝执行。",
                dimension=dimensions[0] if dimensions else None,
            )
        return sql

    def _compile(self, *, dimensions: Sequence[str], window: Window) -> str:
        """cube 查询定义 → 物理 SQL（`plan_cube_sql_checked` 只编译、不执行）。

        **走 `_checked` 而不是 fail-open 的那条**：`plan_cube_sql` 失败恒返回 `{}`，
        把「filter 维度名写成了**物理列名**」这类错误变成一句「编译不出 SQL」——
        实测在这上面耗掉一整个诊断脚本。这里要的是引擎的原话（`CubePlanError` 已把
        未知 measure/dimension、MDL 不匹配等本地化），因为它就是 S3 那个工具反馈给
        模型的唯一信号。

        ``limit``/``offset`` **刻意不进 args**：`plan_cube_sql` 内部的
        `strip_cube_window` 会把它们剥出来只当 `window_limit` 返还（`wren_plan.py:311`），
        传了反而制造「两条 LIMIT」的假象（`plan_cube_sql` docstring 记过这条）。

        ``measures`` 发**整组**：聚合由 cube 定义承担，多度量只是多几列（模块 docstring
        「多度量」）；把度量名当聚合前缀写进 args 会编译失败。
        """
        from agent.utils.wren_plan import CubePlanError, plan_cube_sql_checked

        args: dict[str, Any] = {
            "cube": self._cube,
            "measures": list(self._measures),
            "dimensions": list(dimensions),
            "filters": self._compile_filters(window),
        }
        reason = ""
        try:
            plan = plan_cube_sql_checked(self._project_path, self._conn, args)
        except CubePlanError as e:
            plan, reason = {}, str(e)
        sql = str((plan or {}).get("dialect_sql") or "")
        if not sql.strip():
            # 让内核把异常收敛成类型化错误（总量查询 → TOTAL_QUERY_FAILED、
            # 分组查询 → DIMENSION_QUERY_FAILED + dimension_analysis_failed）。
            # 这里**不自创错误码**：方案 §2.2 那张码表是完整集合，S6 段⑧逐条断言它。
            hint = ""
            if "datasource" not in self._conn:
                # 实测踩过：把 `McpSqlConfig.config`（{host,port,database,user,password}）
                # 直接当 wren 连接用 ⇒ 编译器**不报错**、只是产不出 SQL。少一个键要查半天，
                # 所以把它写进错误文本。正确来源是 `resolve_wren_ctx_by_db` / `wren_conn_dict`。
                hint = "；连接字典缺 `datasource` 键（wren 连接必须是 " \
                       "{datasource,host,port,database,user[,…]}，不能直接用 McpSqlConfig.config）"
            elif not reason:
                # `_checked` 不抛却回 {} ⇒ 只有「plan 里没有 dialect_sql」（`_finish` 的
                # 空 plan 分支），引擎没给原话，如实说不知道。
                reason = "编译器没有给出原因（引擎返回了空的物理计划）"
            # ⚠️ 报错文本里**不要**写「filter 维度请用 cube 维度名」这类猜测：维度名与
            # 物理列名同名时（生产多数如此）它会把对的写法说成错的。引擎原话已经够准。
            raise RuntimeError(
                "cube 定义编译不出可执行 SQL"
                f"（cube={self._cube!r} measures={list(self._measures)!r}"
                f" dimensions={list(dimensions)!r} time_dimension={self._time_dimension!r}"
                f"{hint}；原因：{reason}）"
            )
        return sql

    def _compile_filters(self, window: Window) -> list[str]:
        """父过滤 + 窗口的**半开**两条件 → `dim:op:value` 字符串列表。

        半开 `[start, end)` 由算子选择兑现：`gte start` + `lt end`。**不要**改成
        `lte end` —— 那会把 end 当天整天算进来，两期边界重叠一天（`unequal_windows`
        之外还会静默重复计数）。
        """
        specs = [render_filter_spec(item) for item in self._base_filters]
        if self._time_dimension and any(
            str((item or {}).get("dimension") or "") == self._time_dimension
            for item in self._base_filters
        ):
            # 父过滤里已有一条作用在时间维度上：它与窗口**是 AND**（交集），方向上是变窄
            # 所以不算错数 —— 但 `comparison_metadata` 记的是窗口，实际查的是交集，
            # 两者不再等价。审计口径要对得上，所以必须留下这一行。
            _logger.warning(
                "[归因取数] 父过滤里已有作用在 %s 上的条件，与两期窗口按 AND 叠加"
                "（实际范围是交集，可能窄于 metadata 记的窗口）：%s",
                self._time_dimension,
                [item for item in self._base_filters
                 if str((item or {}).get("dimension") or "") == self._time_dimension],
            )
        if not self._time_dimension:
            # 没有时间锚点 ⇒ 窗口下推不了 ⇒ 等于「对全部历史做归因」。宁可不做。
            raise TypedError(
                "INVALID_TIME_WINDOW",
                "归因需要 time_dimension 才能把两期窗口下推到 SQL；"
                "缺失时无法把结果限定在 baseline/current 窗口内，已拒绝。",
            )
        specs.extend(
            f"{self._time_dimension}:{operator}:{self._window_bound(window, operator)}"
            for operator in _WINDOW_OPERATORS
        )
        return specs

    @staticmethod
    def _window_bound(window: Window, operator: str) -> str:
        return window.start if operator == "gte" else window.end

    # ── 执行 ──────────────────────────────────────────────────────
    def _execute(self, sql: str) -> list[dict]:
        """执行一条物理 SQL → 行 dict 列表（列名已取叶子）。"""
        result = self._executor(sql) if self._executor is not None else self._dbmcp_execute(sql)
        return _rows_from(result)

    def _dbmcp_execute(self, sql: str) -> Any:
        """默认执行通道：复用 dbmcp 的 runner（与 `api/feedback_annotation._run_preview` 同源）。

        **不 import `api.feedback_annotation`**：方向是反的（`api` 依赖 `agent`），
        引进来会造出环。所以这里重写那两行 `asyncio.run(runner.run_sql(...))` ——

        - `_run_engine_blocking` 的注释解释了为什么必须 `asyncio.run` 而不是 `await`：
          引擎的 `run_sql` 是 async 外壳 + 同步实心，`await` 只会把查询挂在循环上。
        - 本函数**运行在内核线程**（`run_attribution` 已把整段 offload 出去），
          那条线程没有运行中的事件循环 ⇒ `asyncio.run` 合法。
        - 连接**每条查询新建**（`MySQLRunner.__init__` 只存参数，`pymysql.connect` 在
          `run_sql` 里），与 `_run_preview` 一致：不持有跨请求的长连接，也就没有
          「连接被掐断后全站报 OperationalError」那类状态。代价是 `2+2D` 条 = 12 次
          建连（同库、会话级 statement timeout 也要重设一次）—— 换来的是**无连接态**，
          在「12 条顺序查询」这种短促场景里这笔交换划算。
        - **不调 `_apply_default_limit` / 不 `split_sql_statements`**：我们发出去的 SQL
          已经是编译产物（自带 `LIMIT 1001`、单语句），再走一遍改写只会让 `query_log`
          里那条 SQL 不再等于真正执行的语句 —— 而那条 SQL 是取证用的。要改也是改
          `plan_cube_sql` 的上限，不是在这里二次加工。
        """
        from mcp_server.db_mcp_server.db.config import McpSqlConfig
        from mcp_server.db_mcp_server.db.db_server import (
            _build_tool_context,
            _load_runner_class,
        )
        from mcp_server.db_mcp_server.db.sql_runner import RunSqlToolArgs

        cfg = McpSqlConfig.from_env(self._db_name)
        # 与 `_run_preview` 同样把「importlib 首次导入 + 构造驱动」一并放线程外：
        # 本函数已经在工作线程里，所以直接同步调即可。
        runner = _load_runner_class(cfg.db_type)(**cfg.config)
        context = _build_tool_context()
        return asyncio.run(runner.run_sql(RunSqlToolArgs(sql=sql), context))


# ══════════════════════════════════════════════════════════════════════
# 结构化过滤 → wren 的 `dim:op:value` 规格
# ══════════════════════════════════════════════════════════════════════
def render_filter_spec(item: dict) -> str:
    """结构化过滤项 → wren `_parse_filter` 认的 `dim:op[:value]` 字符串。

    `_parse_filter` 用 ``spec.split(":", 2)`` 切，所以 **value 里带冒号是安全的**
    （maxsplit=2 把它留在一个片段里）。两条已知边界，都在下面显式处理：

    - `is_null` / `is_not_null` **不带 value**（写 `dim:is_null`）；带了也忽略 ——
      参考实现的 `_parse_filter` 只认两段式。
    - `in` / `not_in` 用逗号连接 —— **值里含逗号的成员无法表达**（wren 侧按逗号切）。
      这里**不改写、不转义**（转了才会造出「看起来对、实际查别的值」的静默错数），
      只打一条 WARNING，让它在日志里可见。

    `operator` 限 `FILTER_OPERATORS`（内核同一份枚举），不在枚举内直接拒 ——
    注入面按构造关闭的一半（方案 §1.2）。

    value 的转义**不是我们做的**：实测（`verify_attribution.py` 段⑩，用生产活跃库的
    真编译器）wren 把 `'` 按 `''` 转义，四种危险输入分别是 ——
      `a' OR 1=1` ⇒ `'a'' OR 1=1'`（一个字面量，越不出去）；`a\\' OR 1=1` 与末尾单
      反斜杠 ⇒ **编译直接失败**；`a\\\\' OR 1=1` ⇒ 仍是单字面量。**没有找到能越出
      字面量的形状**，而 `; DROP` 这类文本会被闸①（保守地）判成写语句而拒答 ——
    安全的那个方向，代价是极罕见的值会被误拒，错误码是 `INVALID_ARGUMENT`。
    由此：**不要**在这里加「转义 / 过滤 value」的逻辑（我们自己转一遍只会与 wren 打架，
    把安全值转坏），要改就改 `FILTER_OPERATORS` 或闸① 的判据。
    """
    dimension = str((item or {}).get("dimension") or "").strip()
    operator = str((item or {}).get("operator") or "").strip()
    if not dimension:
        raise TypedError("INVALID_FILTER", f"过滤项缺少 dimension：{item!r}")
    if operator not in FILTER_OPERATORS:
        raise TypedError(
            "INVALID_FILTER",
            f"过滤算子 {operator!r} 不在允许集合内：{list(FILTER_OPERATORS)}",
        )
    if operator in ("is_null", "is_not_null"):
        return f"{dimension}:{operator}"

    value = (item or {}).get("value")
    if operator in ("in", "not_in"):
        members = list(value) if isinstance(value, (list, tuple, set)) else [value]
        rendered = [str(member) for member in members]
        if any("," in member for member in rendered):
            _logger.warning(
                "[归因取数] 过滤 %s 的成员里含逗号，wren 侧按逗号切分 ⇒ 该条件无法逐字表达：%s",
                dimension,
                rendered,
            )
        return f"{dimension}:{operator}:{','.join(rendered)}"
    return f"{dimension}:{operator}:{value}"


# ══════════════════════════════════════════════════════════════════════
# 结果行归一
# ══════════════════════════════════════════════════════════════════════
def _rows_from(result: Any) -> list[dict]:
    """驱动层结果 → 行 dict 列表：列名取叶子、标量转 Python 原生。

    「叶子名」必须用内核那一份 `column_leaf`（**import，不复制**）：编译出的别名与
    内核期望的裸名一旦各有一套规则，就会漂移成「列找不到」而没人知道为什么。
    """
    if result is None:
        return []
    records = _to_records(result)
    rows: list[dict] = []
    for record in records:
        rows.append(
            {column_leaf(str(column)): _to_python_scalar(value) for column, value in record.items()}
        )
    return rows


def _to_records(result: Any) -> list[dict]:
    """DataFrame / 行迭代 → `list[dict]`。"""
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        # pandas DataFrame：`to_dict("records")` 是唯一不依赖列序的取法
        records = to_dict(orient="records")
        return list(records) if isinstance(records, list) else []
    if isinstance(result, dict):
        return [result]
    return [dict(row) for row in result]


def _to_python_scalar(value: Any) -> Any:
    """numpy 标量 → Python 原生（其余原样，**不猜**）。

    **为什么必须转**：`df.to_dict("records")` 给的是 `np.int64` / `np.float64`，而内核
    `_normalize_dimension_value` 靠 `isinstance(value, int)` 判整数 —— `np.int64`
    **不是** `int` ⇒ 它会一路掉到最后的 `str(value)`，把整数维度值变成字符串 `"5"`。
    后果落在**并集成员的身份**上：pandas 的列只要出现 NULL 就会把整列从 int64 升成
    float64（`45` vs `45.0`），两期就这么撞上不一致的类型；`int`/`float` 在 Python
    的 dict 里 hash 相等（`5` 与 `5.0` 同键），可 `"5"` 与 `5.0` 不等 ⇒ 同一个客户
    被劈成 `entered` + `exited` 两行、Σδ 看着还对得上。归一之后这半边就关死了。
    （**不要**把它说成「模型看到 `"5"` 所以 `value == 5` 不成立」：payload 的 `value`
    字段是内核 `_display_dimension_value` 渲染的展示字符串，本来就是 `str(value)`；
    带类型的取值在 `drill_down.filters[].value` 里。）

    **为什么只转 numpy、不猜别的**：numpy 标量是**我们自己**引入的中间层，转它天经地义；
    驱动给什么类型就是什么类型，**一个都不猜**。真库实测（Chinook，MySQL 8 + pymysql）：
    `YEAR(InvoiceDate)` 和 `CAST(YEAR(InvoiceDate) AS CHAR)` 分别回 `int 2021` 与
    `str "2021"` —— 两者都**原样**进 payload。一旦开始按值猜（`int("2021")`），
    `"0005"` 这类合法的字符串成员就会被改写成 `5`，那才是静默错数。
    """
    if value is None or isinstance(value, (str, bytes)):
        return value
    item = getattr(value, "item", None)  # numpy 标量有 `.item()`；str/Decimal/date 没有
    if callable(item):
        try:
            return item()
        except Exception:  # noqa: BLE001 —— 转不动就原样交给内核判
            return value
    return value


# ══════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════
def get_runner(
    db_name: str,
    *,
    cube: str,
    measure: str | None = None,
    measures: Sequence[str] = (),
    time_dimension: str | None = None,
    filters: Sequence[dict] = (),
    executor: Callable[[str], Any] | None = None,
) -> WrenCubeRunner | None:
    """库名 → `WrenCubeRunner`；`(项目路径, 连接字典)` 解析不出 ⇒ ``None``。

    ``measures`` 是查询组里的度量列表（`measure` 单数是兼容写法，见 `WrenCubeRunner`）。

    ``db_name → (project_path, conn)`` 用 `resolve_wren_ctx_by_db`（正向孪生：
    交互式场景手里只有库名），它内部先 `normalize_db_name` 归一、再
    `discover()` + `project_path_for()`（含 `WREN_SEMANTIC_OVERRIDE` 版本物化），
    与 `plan_cube_sql` 定方言、`McpSqlConfig` 建连接**共用同一份 ctx** ⇒ 方言不会错位。

    ⚠️ **绝不按目录名拼路径**：生产注册表里 `name` 是 `witops`，工作区目录却是
    `witops_wrenai`（同级还有 `*.备份-*`）⇒ `f"{db_name}_semantic"` 这类写法会指到
    错的地方**且不报错**（方案 §4.3）。

    返回 ``None`` 只表示「解析不出这个库」，由工具层决定回 `UNKNOWN_DB`（三级解析的
    最后一级全空）还是别的码 —— 本函数**不猜库、不回落默认库**。
    """
    from agent.utils.wren_call_extract import resolve_wren_ctx_by_db

    project, conn = resolve_wren_ctx_by_db(db_name)
    if not project or not conn:
        _logger.warning("[归因取数] 库 %r 解析不出 (项目路径, 连接) ⇒ 不建 runner", db_name)
        return None
    return WrenCubeRunner(
        project,
        conn,
        db_name,
        cube=cube,
        measure=measure,
        measures=measures,
        time_dimension=time_dimension,
        filters=filters,
        executor=executor,
    )


async def run_attribution(
    runner: WrenCubeRunner, request: Request | Sequence[Request]
) -> dict:
    """`attribute()` 的异步门面：**整段**取数一次搬离事件循环（P1-14）。

    为什么整段而不是每条：`2+2D` 条是**顺序**依赖同一条引擎路径的（方案 §3.4①
    明令不许按维度 fan-out），一条条 offload 只会把「12 次阻塞」换成「12 次调度」，
    而中间任何一次让出控制权都只是多一次线程往返。搬一次，语义最清楚。
    **多度量也在这同一趟里**：M 个度量顺序跑完，进程内编译 M 次、真跑仍只有 `2+2D` 条
    （模块 docstring「多度量」）。

    门面放在本模块而不是工具层：**取证通道的线程纪律应该跟通道本身住在一起**，
    否则第二个调用方（比如将来的批处理/评测）会照着 `attribute()` 直接同步调。

    入参收**一个 `Request` 或一串 `Request`**（同一 runner、同一查询组），返回形状**恒定**：

    ```python
    {
      "measures": ["total_hours", "record_count"],   # 去重保序，= 入参顺序
      "primary_measure": "total_hours",              # = measures[0]
      "per_measure": {  # 逐字 = 内核 `attribute()` 的返回（单度量对齐参考实现在这里）
          "total_hours":   {total_change, strategy, dimension_ranking, selected_dimensions,
                            top_dimension_values, per_dimension, warnings, comparison_metadata},
          "record_count":  {...},
      },
      "warnings": [ {**warning, "measure": "…"}, … ],  # 各度量的**并集**（按 code+message 去重）
      "comparison_metadata": {...},                   # 主度量那份 + measures/queries_executed/query_groups
      "queries_executed": 2 + 2 * D,                   # **去重后的真实执行条数**，与 M 无关
    }
    ```

    三处刻意的不对称，都已想清楚：

    1. **结果一律挂在 `per_measure` 下**，顶层**不镜像**主度量。镜像会让 M=1 与 M>1 出现
       两套读法（模型要判「这次到底看哪个」），而多度量时顶层镜像必然与 `per_measure` 重复。
       代价是单度量多一层嵌套 —— 换来「永远只有一个地方读数」。
    2. **顶层 `warnings` 是并集、且逐条带 `measure`**：多度量时不知道警告属于哪条度量，
       模型会把 A 的「非加性」按到 B 头上（§2.2 打 `non_additive_dimension` 的维度是要被
       踢出排名的）。内层那份保持内核逐字输出。
    3. **顶层 `queries_executed` 数的是「真正发出去的 SQL」，内层那份数的是「内核尝试了几次」**
       —— 两者在多度量下都不会被 M 乘起来（已去重），但**维度编译失败时会差一次**：
       内核是 `_fetch` 之前就 `queries_executed += 1`（段⑨ 那条 `2+1+2 = 5` 就是它），
       而 runner 在编译失败时**根本没发查询**、不进记账。审计取顶层
       （「这次调用到底打了几条 SQL」才是它要回答的问题）；内层保持内核逐字。
    """
    from agent.utils.offload import offload

    requests = _requests_of(request)
    return await offload(_attribute_all, runner, requests)


def _requests_of(request: Request | Sequence[Request]) -> list[Request]:
    """一个 `Request` 或一串 → 去重保序的列表（去重键 = 度量名）。

    同一个度量写两遍只会编译出同一条 SQL、跑出同一份行，最后在后一个覆盖前一个 ——
    与其让它悄悄发生，不如在这里就按名字收成一个，`measures` 里也就不会出现重复项。
    """
    items = [request] if isinstance(request, Request) else list(request or ())
    seen: dict[str, Request] = {}
    for item in items:
        if not isinstance(item, Request):
            raise TypedError("INVALID_ARGUMENT", f"归因入参必须是 Request，收到 {type(item).__name__}。")
        seen.setdefault(item.measure, item)
    if not seen:
        raise TypedError("INVALID_ARGUMENT", "归因至少需要一个 Request（一个度量）。")
    return list(seen.values())


def _attribute_all(runner: WrenCubeRunner, requests: Sequence[Request]) -> dict:
    """**在工作线程里**顺序跑完每个度量，再把结果装配成统一形状。

    多度量**共用取数**靠 runner 的执行层缓存（模块 docstring「多度量」），
    本函数因此只是「循环 + 拼装」，没有任何取数逻辑 —— 内核与 runner 的职责都不动。
    """
    per_measure: dict[str, dict] = {}
    for request in requests:
        per_measure[request.measure] = attribute(runner, request)

    primary = requests[0].measure
    metadata = dict(per_measure[primary].get("comparison_metadata") or {})
    metadata["measures"] = [request.measure for request in requests]
    # 顶层记账 = **真实执行条数**（去重后的）；跨组由模型分次调用，见模块 docstring。
    metadata["queries_executed"] = runner.queries_executed
    metadata["query_groups"] = len(
        {
            (item.baseline.start, item.baseline.end, item.current.start, item.current.end)
            for item in requests
        }
    )
    return {
        "measures": [request.measure for request in requests],
        "primary_measure": primary,
        "per_measure": per_measure,
        "warnings": _merge_warnings(requests, per_measure),
        "comparison_metadata": metadata,
        "queries_executed": runner.queries_executed,
    }


def _merge_warnings(requests: Sequence[Request], per_measure: dict[str, dict]) -> list[dict]:
    """各度量的警告 → 并集（按 `code` + `message` 去重、保序），逐条标注 `measure`。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for request in requests:
        for warning in per_measure[request.measure].get("warnings") or ():
            key = json.dumps(
                [warning.get("code"), warning.get("message")],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append({**warning, "measure": request.measure})
    return merged



__all__ = [
    "WrenCubeRunner",
    "get_runner",
    "render_filter_spec",
    "run_attribution",
]
