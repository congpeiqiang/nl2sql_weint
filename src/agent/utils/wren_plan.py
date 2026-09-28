"""进程内复算「真正下发物理库执行的 SQL」（报告用）。

**要解决的问题**：走 wren 语义层的两条通道，工具返回体里都**没有**真正执行的语句——
- Cube 通道 ``wrenai_<库>_query_cube``：只回 ``{columns, rows, row_count, truncated}``，
  引擎编译出的 SQL 在 ``wren/mcp_server.py`` 内部被丢弃；
- SQL 通道 ``wrenai_<库>_run_sql``：报告记的是**工具入参**，即模型写的语义层 SQL
  （``FROM do_bug`` 这类 MDL 视图），物理库里根本不存在该视图。

所以报告里的「执行 SQL」粘进 MySQL 必然报错。真正能跑的语句是引擎 ``dry_plan``
展开成 CTE 之后、再被连接器追加 LIMIT 的那一条，本模块按同一套 wren API 复算它。

**与 wren CLI 逐字节对拍通过**（2026-09-15，wrenai 0.13.0，60 项含 10 条留档基线）::

    cube: cube_query_to_sql(json.dumps(_build_cube_query(...)), mdl_json)  == wren cube query --sql-only
    物理: engine.dry_plan(sql)                                            == wren dry-plan

对拍同时钉死了线上两处形态：``wren serve mcp`` 的引擎也是用
``<项目>/target/mdl.json`` 建的（``serve_cli.py``），连接字典就是 ``--profile``
展开后的 ``{"datasource": ds, **profile}``（与 ``mcp_tool.wren_conn_dict`` 同形）。

**MDL 文本取 ``<项目>/target/mdl.json``，不要用 ``wren.context.build_json``**：
MCP 的 ``query_cube`` 编译 cube 时用的是 ``json.dumps(build_json(project))``，两者
**只差空白**（解析后逐键相等，已实测）故 SQL 产物一致；而 ``build_json`` 的
``_load_cubes_v2`` 对 ``cubes/*/metadata.yml`` 调 ``read_text()`` 不带 encoding，
Windows 上中文 cube 元数据直接 ``UnicodeDecodeError: 'gbk' codec``——所以走前者。

**旁路性质**：``dry_plan`` 不需要连接器（``_get_connector`` 只在 ``query``/``dry_run``
里懒建），所以复算全程**不开数据库连接**、不产生任何写操作。

**「实际下发」的定义包含调用边界归一**：cube 调用的 ``limit``/``offset`` 在 wren
0.13 下**不可用**（编译进 Cube SQL 的 LIMIT 会被连接器再追加一条 → MySQL 语法错），
平台在工具边界剥离、结果返回后截窗（见 ``strip_cube_window`` 与
``middlewares/sql_approval``）——复算也必须按同一套规则剥离，否则报告里的物理 SQL
会是与线上不一致的那条（拿 limit 去镜像双 LIMIT，线上其实按无 LIMIT 执行）。

**一切失败都 fail-open**：任何异常都返回 ``{}`` / ``""``，调用方当作「没这回事」
（报告退回「查询定义」节），绝不把半截或猜测的 SQL 写进报告。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

# 与 wren/mcp_server.py 同值：连接器实际下发的 limit = min(limit or 1000, 10000) + 1
# （+1 是 _query_with_limit_probe 为判断截断而多取一行）。
DEFAULT_ROW_LIMIT = 1000
MAX_ROW_LIMIT = 10000

# 物理 SQL ≤ 该长度时，除落盘外再内联一份进 check 结果（落盘/读盘失败时的兜底）。
# 取值远小于 MessageSlimmerMiddleware 的 8000 字符截断阈值，保证不会把整条
# check 结果顶到会被落盘替换掉的程度。
PLAN_INLINE_MAX = 3000

_MDL_REL = ("target", "mdl.json")
# 引擎缓存上限（P3-7）。缓存键含「连接指纹」⇒ **键数 ≈ 同时活跃的语义库数**，
# 上限必须 ≥ 库数，否则多库用户会在库之间来回把引擎挤出去、每次都要重建。
# 4 是单库时期的取值，多工作区/多库之后明显偏小（一次 miss 就是一次 `_build_engine`，
# 本地小库实测 1~3ms、冷启动 1.7s，生产模型多的库按注释记约 0.9s）。
# 默认放宽到 16（≈ 4× 当前库数的余量），并用 `WREN_PLAN_CACHE_MAX` 覆盖；
# **代价是内存**（每个引擎常驻），库很多时按实际 RSS 调小。
DEFAULT_CACHE_MAX = 16
_TRAILING_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\s*$", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z一-鿿_-]+")

# wren 私有 API 能力探测结果（None = 还没探过）。探测不过 → 本模块整体停用。
_API_OK: Optional[bool] = None
# 引擎缓存：key = (mdl 路径, mtime_ns, size, 连接指纹) —— 语义库重建后 mtime 变即换引擎
_ENGINE_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


# ── wren 私有 API 能力探测 ────────────────────────────────────────────
def probe_wren_api() -> bool:
    """探测复算依赖的三个 wren 私有入口是否仍可用（结果缓存，只探一次）。

    依赖私有 API 是刻意的取舍：应用内复算比每次多跑一次 MCP ``dry_plan`` 便宜
    （后者要新起 ``wren serve mcp`` 子进程，实测 2.5~4.2s）。代价是 wren 升级
    可能改签名 —— 探测不过就整体停用，报告退回「查询定义」节，
    **宁可没有 SQL，也不给错的 SQL**。
    """
    global _API_OK
    if _API_OK is not None:
        return _API_OK
    ok, why = True, ""
    try:
        import inspect

        from wren.cli import _build_engine
        from wren.cube_cli import _build_cube_query
        from wren_core import cube_query_to_sql

        for name, fn, params in (
            ("_build_engine", _build_engine, ("mdl", "connection_info", "connection_file")),
            (
                "_build_cube_query",
                _build_cube_query,
                ("cube", "measures", "dimensions", "time_dimension", "filters", "limit", "offset"),
            ),
        ):
            got = set(inspect.signature(fn).parameters)
            missing = [p for p in params if p not in got]
            if missing:
                ok, why = False, f"{name} 缺参数 {missing}"
                break
        if ok and not callable(cube_query_to_sql):
            ok, why = False, "cube_query_to_sql 不可调用"
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"{type(e).__name__}: {e}"
    _API_OK = ok
    if ok:
        _logger.info("[wren_plan] wren API 能力探测通过（报告可复算物理 SQL）")
    else:
        _logger.warning(
            "[wren_plan] wren API 能力探测失败（%s）→ 报告不出「执行 SQL（物理）」节", why
        )
    return ok


# ── 小型归一化helper ──────────────────────────────────────────────────
def _as_str_list(v: Any) -> List[str]:
    """把 cube 参数归一成 str 列表。

    MCP 工具 schema 是 ``list[str]``（``",".join(...)`` 前的形态），这里再兜一层
    逗号分隔字符串，避免模型/历史参数形态差异把复算 SQL 拼歪。
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    if isinstance(v, (list, tuple)):
        return [str(s).strip() for s in v if str(s).strip()]
    return [str(v)]


def _as_int(v: Any) -> Optional[int]:
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


def effective_probe_limit(limit: Any) -> int:
    """复现 ``wren/mcp_server.py::_query_with_limit_probe`` 的 limit 计算。"""
    n = DEFAULT_ROW_LIMIT if limit is None else _as_int(limit)
    if n is None:
        n = DEFAULT_ROW_LIMIT
    return min(n, MAX_ROW_LIMIT) + 1


def mirror_connector_limit(sql: str, n: int) -> str:
    """复现 ``wren/connector/mysql.py::_apply_limit``（**先去掉尾部空分号**再追 LIMIT）。

    连接器是**无条件**追加的：SQL 若自带尾部 ``LIMIT n``，plan 产物就以 ``LIMIT n``
    结尾，追加后会变成两条 LIMIT（MySQL 语法错）——复算如实镜像，由调用方在注记里
    说明，而不是自作主张「修正」成与线上不一致的语句。

    cube 通道不会走到这条分支：``strip_cube_window`` 已在调用边界剥掉 limit/offset；
    run_sql 通道由 ``sql_approval._normalize_wrenai_limit`` 剥尾部 LIMIT（两者都是
    应用层防御，本函数镜像的是防御之后的语句）。
    """
    return f"{sql.rstrip().rstrip(';').rstrip()}\nLIMIT {n}"


# ── Cube 调用窗口（limit/offset）：wren 侧不可用，平台侧截窗 ─────────────
# 唯一真源：工具边界（middlewares/sql_approval）与复算（plan_cube_sql）都走这里，
# 保证「报告里的物理 SQL」==「真正下发的语句」。
def strip_cube_window(args: Any) -> tuple:
    """剥离 cube 调用的 ``limit``/``offset`` → ``(新 args, limit|None, offset|None)``。

    **为什么必须剥**（wren 0.13.0 实测 + 生产 thread 01a0a850 报错坐实）：``query_cube``
    把 limit/offset 直接编译进 Cube SQL（``_build_cube_query``），而
    ``_query_with_limit_probe`` 又把同一个 limit 交给连接器、由 ``_apply_limit``
    **无条件**再追加一条 ``LIMIT {limit+1}``：

    - ``limit=200`` → ``… GROUP BY 1 LIMIT 200`` + ``\\nLIMIT 201`` → MySQL 1064
      （``You have an error in your SQL syntax … near 'LIMIT 201' at line 2``）；
    - ``offset=5``（不带 limit）→ ``… GROUP BY 1 OFFSET 5`` + ``\\nLIMIT 1001``，
      而 MySQL 要求 LIMIT 必须出现在 OFFSET **之前** → 同样必错。

    即这两个参数在 wren 侧不可用。平台改为：调用边界剥离（不传给 wren）、结果返回后
    按 ``(offset, limit)`` 在客户端截窗（``sql_approval._apply_cube_window``）——
    语义等值于 MySQL 的 ``LIMIT n OFFSET m``，上限天然受 wren 的
    ``DEFAULT_ROW_LIMIT=1000`` 探测上限约束（``limit>1000`` 时最多拿到 1000 行）。

    返回**新字典**（不改入参）；非法值按缺失处理（``_as_int``）。
    """
    if not isinstance(args, dict):
        return {}, None, None
    norm = {k: v for k, v in args.items() if k not in ("limit", "offset")}
    return norm, _as_int(args.get("limit")), _as_int(args.get("offset"))


# ── MDL 与引擎 ────────────────────────────────────────────────────────
def _mdl_path(project_path: Any) -> Optional[Path]:
    if not project_path:
        return None
    p = Path(str(project_path)) / _MDL_REL[0] / _MDL_REL[1]
    return p if p.is_file() else None


def _cache_enabled() -> bool:
    return os.environ.get("WREN_PLAN_CACHE", "1") not in ("0", "false", "False")


def cache_max() -> int:
    """引擎缓存上限。`WREN_PLAN_CACHE_MAX` 覆盖，**非数字/≤0 退回默认**（与
    `db/limits.py`、`llm_gate` 同口径：坏值不该被当成「关掉保护」）。"""
    raw = os.environ.get("WREN_PLAN_CACHE_MAX", "")
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CACHE_MAX
    return n if n > 0 else DEFAULT_CACHE_MAX


def _build_engine_for(mdl_path: Path, conn: Dict[str, Any]):
    """建引擎。**不做 stdout/stderr 重定向**——redirect_stdout 是进程级全局替换，
    在服务端会吞掉并发协程的日志输出；这里失败路径本就由 try/except 兜住。"""
    from wren.cli import _build_engine  # noqa: PLC0415

    return _build_engine(
        str(mdl_path),
        json.dumps(conn, ensure_ascii=False, default=str),
        None,
        conn_required=False,
    )


def _engine_for(mdl_path: Path, conn: Dict[str, Any]):
    """按 (mdl 指纹, 连接指纹) 取/建引擎。语义库重建 → mtime 变 → 自动换新引擎。"""
    st = mdl_path.stat()
    conn_fp = hashlib.sha1(
        json.dumps(conn, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:12]
    key = (str(mdl_path), st.st_mtime_ns, st.st_size, conn_fp)
    if not _cache_enabled():
        return _build_engine_for(mdl_path, conn)
    with _CACHE_LOCK:
        eng = _ENGINE_CACHE.get(key)
        if eng is None:
            # 持锁构建：并发同键只建一次（~0.9s），不同键互相等一下可接受。
            # ⚠️ 这里是**串行**的：N 个库的首次查询同时到达 ⇒ 墙钟 ≈ N × 建引擎耗时
            # （本地 4 个库实测并发到达的墙钟 15ms ≈ 串行和）。改成「按键加锁、锁外构建」
            # 能让不同键并行，但 wren 的 `_build_engine` 是否线程安全**没有证据**
            # （可能碰进程级全局，如数据源注册）⇒ 不做，除非先在生产量出这个串行是可观测瓶颈。
            eng = _build_engine_for(mdl_path, conn)
            _ENGINE_CACHE[key] = eng
            limit = cache_max()
            while len(_ENGINE_CACHE) > limit:
                _ENGINE_CACHE.popitem(last=False)
        else:
            _ENGINE_CACHE.move_to_end(key)
    return eng


def _finish(
    mdl_path: Path,
    conn: Dict[str, Any],
    semantic_sql: str,
    *,
    cube_sql: str = "",
    limit: Any = None,
) -> Dict[str, Any]:
    """语义层/ cube SQL → 实际下发语句（plan + 连接器 LIMIT）。"""
    engine = _engine_for(mdl_path, conn)
    plan_sql = engine.dry_plan(semantic_sql)
    if not isinstance(plan_sql, str) or not plan_sql.strip():
        return {}
    n = effective_probe_limit(limit)
    ds = getattr(engine, "data_source", None)
    dialect = str(getattr(ds, "value", None) or ds or "").strip().lower()
    return {
        "cube_sql": cube_sql or "",
        "dialect_sql": mirror_connector_limit(plan_sql, n),
        "dialect": dialect,
        "limit_appended": n,
        "dup_limit": bool(_TRAILING_LIMIT_RE.search(plan_sql.rstrip())),
    }


# ── 对外：复算 ────────────────────────────────────────────────────────
class CubePlanError(ValueError):
    """Cube 定义编译不出可执行 SQL（原因已本地化，可直接展示给操作者）。

    ``plan_cube_sql``（报告侧）把它连同一切异常一起吞掉 → ``{}``；交互式试算
    （标注页「按口径试算」）需要把**为什么**告诉人，走 ``plan_cube_sql_checked``。
    """


def _cube_sql_plan(project_path: Any, conn: Any, args: Any) -> Dict[str, Any]:
    """``plan_cube_sql`` 的真实实现，失败**抛出**（不吞）。

    检查顺序刻意「引擎可用 → 定义完整 → 配置就绪 → 编译」：最常见的错（缺 cube /
    写错 measure）先报，再报环境问题（连接缺失、mdl 找不到），最后才是引擎编译错
    （拼错的 measure/dimension 会在这里以引擎原话冒出来）——试算端点按这个顺序给
    提示，操作者才知道该改定义还是该找运维。
    """
    if not probe_wren_api():
        raise CubePlanError("wren 引擎不可用（当前环境未安装/未启用语义层）")
    if not isinstance(args, dict):
        raise CubePlanError("查询定义必须是对象")
    norm, window_limit, window_offset = strip_cube_window(args)
    cube = norm.get("cube")
    measures = _as_str_list(norm.get("measures"))
    if not cube:
        raise CubePlanError("查询定义缺少 cube 名")
    if not measures:
        raise CubePlanError("查询定义缺少 measures（Cube 查询至少要有一个度量）")
    if not isinstance(conn, dict) or not conn.get("datasource"):
        raise CubePlanError("拿不到该库的连接配置（db_config 缺失）")
    mdl_path = _mdl_path(project_path)
    if mdl_path is None:
        raise CubePlanError(f"语义库项目里找不到 mdl.json（project={project_path or '未配置'}）")

    from wren.cube_cli import _build_cube_query  # noqa: PLC0415
    from wren_core import cube_query_to_sql  # noqa: PLC0415

    # 逐字镜像 wren/mcp_server.py::query_cube 的参数映射（limit/offset 传 None：
    # 线上由 sql_approval 剥掉，传过去就是那条必错的 LIMIT/OFFSET）
    cube_query = _build_cube_query(
        str(cube),
        ",".join(measures),
        ",".join(_as_str_list(norm.get("dimensions"))),
        norm.get("time_dimension") or None,
        _as_str_list(norm.get("filters")),
        None,
        None,
    )
    cube_sql = cube_query_to_sql(
        json.dumps(cube_query, ensure_ascii=False),
        mdl_path.read_text(encoding="utf-8"),
    )
    if not isinstance(cube_sql, str) or not cube_sql.strip():
        raise CubePlanError("引擎没有编译出 SQL（定义里可能没有有效度量/维度）")
    plan = _finish(mdl_path, conn, cube_sql, cube_sql=cube_sql, limit=None)
    if not plan:
        raise CubePlanError("物理 SQL 生成失败（MDL 与连接不匹配？）")
    plan["window_limit"] = window_limit
    plan["window_offset"] = window_offset
    return plan


def plan_cube_sql(project_path: Any, conn: Any, args: Any) -> Dict[str, Any]:
    """Cube 查询定义 → ``{"cube_sql", "dialect_sql", "dialect", ...}``；失败返回 ``{}``。

    ``cube_sql`` 是引擎编译出的**语义层中间 SQL**（引用 MDL 模型名，仍不能直连
    MySQL），随结果一起回传仅作审计与「编译前长什么样」的对照；报告展示的
    ``dialect_sql`` 才是可执行的那条。

    ``args`` 里的 ``limit``/``offset`` 先经 ``strip_cube_window`` 剥离（线上工具边界
    同样剥离、改在结果集截窗）→ 复算产物与实际下发逐字一致；被剥掉的值以
    ``window_limit``/``window_offset`` 回传，供报告注记说明「窗口是平台侧截的」。

    fail-open：任何失败都返回 ``{}``（调用方按「没这回事」处理）。需要知道失败原因
    的交互式场景走 ``plan_cube_sql_checked``。
    """
    try:
        return _cube_sql_plan(project_path, conn, args)
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.debug("[wren_plan] cube 物理 SQL 复算失败: %s", e)
        return {}


def plan_cube_sql_checked(project_path: Any, conn: Any, args: Any) -> Dict[str, Any]:
    """同 ``plan_cube_sql``，但把失败原因**抛成** ``CubePlanError``（供试算端点回报）。

    引擎自己的报错（未知 measure/dimension、MDL 解析失败…）原样包进异常消息——那
    正是「口径拼写检查」的价值所在，吞掉就没了。
    """
    try:
        return _cube_sql_plan(project_path, conn, args)
    except CubePlanError:
        raise
    except Exception as e:  # noqa: BLE001  引擎异常 → 本地化后抛出
        raise CubePlanError(f"{type(e).__name__}: {e}") from e


def plan_run_sql(project_path: Any, conn: Any, sql: Any, limit: Any = None) -> Dict[str, Any]:
    """语义层 SQL（模型写的那条）→ 实际下发语句；失败返回 ``{}``。"""
    try:
        if not probe_wren_api():
            return {}
        sql = str(sql or "").strip()
        if not sql:
            return {}
        mdl_path = _mdl_path(project_path)
        if mdl_path is None or not isinstance(conn, dict) or not conn.get("datasource"):
            return {}
        return _finish(mdl_path, conn, sql, limit=limit)
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.debug("[wren_plan] run_sql 物理 SQL 复算失败: %s", e)
        return {}


# ── 对外：落盘 ────────────────────────────────────────────────────────
def write_plan_file(root: Any, session_id: Any, kind: str, name: Any, sql: str) -> str:
    """把全量物理 SQL 落盘，返回 VFS 指针 ``/workspace/…``；失败返回 ``""``。

    为什么走文件：实测物理 SQL 3.5~10.9 KB，内联进 check 结果会顶破
    ``MessageSlimmerMiddleware`` 的 8000 字符阈值 → 整条 check 结果（连带
    ``full_result_files`` 指针）被落盘替换成 1000 字符预览。落盘后 check 结果
    只带指针（~百字节），report_builder 读盘内嵌，报告正文本身是文件、不进 state。

    文件名取 SQL 指纹 → 同一条 SQL 反复 check 幂等，不堆文件。
    """
    try:
        sql = str(sql or "").strip()
        if not sql or not root or not session_id:
            return ""
        digest = hashlib.sha1(sql.encode("utf-8")).hexdigest()[:10]
        safe = (_SAFE_NAME_RE.sub("_", str(name or kind or "plan")).strip("_")[:40]
                or str(kind or "plan"))
        rel = Path("nl2sql_process_data") / str(session_id) / "wren_plan" / f"{safe}_{digest}.sql"
        disk = Path(str(root)) / rel
        if not disk.is_file():
            disk.parent.mkdir(parents=True, exist_ok=True)
            disk.write_text(sql + "\n", encoding="utf-8")
        return "/workspace/" + rel.as_posix()
    except Exception as e:  # noqa: BLE001  fail-open
        _logger.debug("[wren_plan] 物理 SQL 落盘失败: %s", e)
        return ""
