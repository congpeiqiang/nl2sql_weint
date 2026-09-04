"""ProgressBoundaryMiddleware — 确定性推进 write_todos（层2 监督机）。

背景：nl2sql 子智能体里 write_todos 是模型自发工具，实测长查询模型只在
「开头初始化 + 结尾一次全勾」各调一次，中间 5-6 分钟（schema 检索 / run_sql
执行 / 纠错循环）不动 → 前端任务卡冻结在最旧快照。系统提示词 + WRITE_TODOS_PROTOCOL
已有大量纪律文本仍无效 → 纯提示词已证伪，必须确定性兜底。

机制事实（2026-09-04 核实）：
- `write_todos` 实为返回 `Command(update={"todos": ...})` 的普通工具；todos 是
  无 reducer 的 state channel。
- `after_model` 是挂在 model 节点之后的独立 graph 节点，返回值作为 state 更新并入
  （langchain 库 TodoListMiddleware 用同一通道返回错误消息）。因此本中间件可绕过
  模型，确定性写 `{"todos": ...}`。
- after_model 在**本轮工具执行之前**运行 → 看到的是模型刚发出的 tool_calls。

推进规则（保守单向，绝不提前全勾，与系统提示词「todo 纪律铁律」一致）：
1. 本轮 AI 消息已含 write_todos → 跳过（尊重模型自己的更新，不打架）。
2. 按工具名分桶判定本轮是否**首次**到达更后阶段：
   knowledge(get_all_knowledge/get_instructions/list_knowledge)
     < schema(describe_schema/get_mdl/get_context/recall_queries/list_models/...)
     < exec(run_sql/dry_run/query_cube/dry_plan)
   单调记录每线程已到达的最高桶（跨轮保留，防 auto-compress 剪消息后倒退）。
3. schema 里程碑：把当前 in_progress 步 completed、下一未完成步 in_progress
   （若当前已是 schema 阶段本身则不动）。
4. exec 里程碑：说明已越过纯推理步（Subproblem/Query Plan/SQL 生成/性能优化），
   把当前 in_progress 起到「查询执行」语义项之前全部 completed、该项 in_progress；
   找不到「查询执行」语义项则保守前进一步。
5. 只允许 pending→in_progress→completed 单向；从不把最后阶段误勾 completed；
   匹配不中 → 不动（退化为现状，不更糟）。

配套：sync_subagent_todos._extract_subagent_todos 改为**优先读子线程 state.todos**
（authoritative 实时值），使本中间件的确定性更新能立刻镜像到前端。
"""
from __future__ import annotations

import logging
import re
import threading

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

_logger = logging.getLogger(__name__)

# ── 阶段桶 ────────────────────────────────────────────────────
_B_NONE = 0
_B_KNOW = 1  # knowledge loader / 澄清（知识获取）
_B_SCHEMA = 2
_B_EXEC = 3

# 工具名子串 → 桶。wrenai_<库名>_run_sql / dbmcp_run_sql 等带前缀，故用子串包含匹配。
_EXEC_TOOL_SUB = ("run_sql", "query_cube", "dry_run", "dry_plan")
_SCHEMA_TOOL_SUB = (
    "describe_schema", "describe_model", "describe_cube",
    "get_mdl", "list_models", "get_db_info", "get_context",
    "recall_queries", "list_stored_queries", "list_cubes",
    "list_functions", "get_data_source",
)
_KNOW_TOOL_SUB = ("get_all_knowledge", "get_instructions", "list_knowledge")

# todos content 语义别名（规范化后子串匹配）
# 当前 in_progress 已是这些阶段 → schema 里程碑不再推进（避免误跳）
_SCHEMA_PHASE_ALIASES = (
    "schemalinking", "结构链接", "schema链接", "库表结构", "表结构理解", "获取schema",
)
# exec 里程碑的目标项（「查询执行」语义）
_EXEC_PHASE_ALIASES = (
    "查询执行", "执行查询", "执行sql", "sql执行", "执行最终", "正在执行查询",
    "查询运行", "运行查询", "queryexecution", "executequery", "executesql",
)


def _norm(s: str) -> str:
    """规范化用于匹配：去空白 + 小写。"""
    return re.sub(r"\s+", "", s or "").casefold()


def _matches_any(content: str, aliases) -> bool:
    c = _norm(content)
    return any(_norm(a) in c for a in aliases)


def classify_tool(name: str) -> int:
    """按工具名归类阶段桶（不带库名前缀亦可）。返回 0=非阶段工具。"""
    n = _norm(name)
    if not n:
        return _B_NONE
    for sub in _EXEC_TOOL_SUB:
        if _norm(sub) in n:
            return _B_EXEC
    for sub in _SCHEMA_TOOL_SUB:
        if _norm(sub) in n:
            return _B_SCHEMA
    for sub in _KNOW_TOOL_SUB:
        if _norm(sub) in n:
            return _B_KNOW
    return _B_NONE


def advance_todos(todos: list, bucket: int) -> list | None:
    """按到达的阶段桶推进 todos（纯函数，供单测）。

    Args:
        todos: [{"content","status", ...?}, ...]（保留多余字段，仅改 status）
        bucket: 刚到达的里程碑（_B_SCHEMA / _B_EXEC）

    Returns:
        新 todos 列表；无需变化 / 状态异常时返回 None。
    """
    if not todos or bucket not in (_B_SCHEMA, _B_EXEC):
        return None

    n = len(todos)

    # 当前唯一 in_progress 索引（异常态 >1 个 in_progress → 不动）
    in_prog_idx = [i for i in range(n) if _st(todos[i]) == "in_progress"]
    if len(in_prog_idx) == 0:
        # 没有 in_progress：保守只把第一个未完成项点亮（不跳远、不勾它之前的项）
        first_open = next((i for i in range(n) if _st(todos[i]) != "completed"), None)
        if first_open is None:
            return None  # 已全 completed
        new = [dict(t) if isinstance(t, dict) else t for t in todos]
        new[first_open] = _with_status(new[first_open], "in_progress")
        return _changed(new, todos)
    if len(in_prog_idx) > 1:
        return None
    i = in_prog_idx[0]

    if bucket == _B_SCHEMA:
        # 当前已在 schema 阶段本身 → 不动
        if _matches_any(_content_s(todos[i]), _SCHEMA_PHASE_ALIASES):
            return None
        # 否则前进一步：当前 completed，下一个未完成项 in_progress
        j = next((k for k in range(i + 1, n) if _st(todos[k]) != "completed"), None)
        if j is None:
            return None  # 当前已是最后一步，别自作主张
        return _apply(todos, i, j)

    # bucket == _B_EXEC：跳过纯推理步，定位「查询执行」语义项
    target = next(
        (j for j in range(i, n)
         if _matches_any(_content_s(todos[j]), _EXEC_PHASE_ALIASES)),
        None,
    )
    if target == i:
        # 当前 in_progress 项本身就是 exec 目标（已在执行阶段）→ 不动
        return None
    if target is None:
        # 找不到「查询执行」语义项 → 保守前进一步（若后面还有步）
        if i == n - 1:
            return None
        return _apply(todos, i, i + 1)
    # 把 i..target-1（含当前 in_progress）全部 completed，target 置 in_progress
    new = [dict(t) if isinstance(t, dict) else t for t in todos]
    for k in range(i, target):
        new[k] = _with_status(new[k], "completed")
    new[target] = _with_status(new[target], "in_progress")
    return _changed(new, todos)


def _content_s(t) -> str:
    return str(t.get("content", "")) if isinstance(t, dict) else str(getattr(t, "content", ""))


def _st(t) -> str:
    return t.get("status", "pending") if isinstance(t, dict) else getattr(t, "status", "pending")


def _with_status(t, status):
    if isinstance(t, dict):
        out = dict(t)
        out["status"] = status
        return out
    return t


def _apply(todos, done_idx, next_idx):
    """当前 done_idx 步 completed、next_idx 步 in_progress。"""
    new = [dict(t) if isinstance(t, dict) else t for t in todos]
    new[done_idx] = _with_status(new[done_idx], "completed")
    new[next_idx] = _with_status(new[next_idx], "in_progress")
    return _changed(new, todos)


def _changed(new, todos):
    """若状态无实质变化返回 None，否则返回 new。"""
    if len(new) != len(todos):
        return new
    same = True
    for a, b in zip(new, todos):
        if _st(a) != _st(b):
            same = False
            break
    return None if same else new


# ── 单调桶跟踪（按线程）───────────────────────────────────────
_BUCKETS: dict[str, int] = {}
_BUCKETS_LOCK = threading.Lock()
_BUCKETS_MAX = 2000


def _seen_bucket(tid: str, derived: int) -> int:
    """记录线程已见最高桶，返回需触发推进的里程碑桶（仅 schema/exec）。

    KNOW 桶只作单调记录不触发（knowledge 阶段无 write_todos 写）。并发安全。
    """
    global _BUCKETS
    with _BUCKETS_LOCK:
        prev = _BUCKETS.get(tid, 0)
        if derived <= prev:
            return 0
        new_bucket = max(prev, derived)
        if len(_BUCKETS) > _BUCKETS_MAX:
            _BUCKETS.clear()
        _BUCKETS[tid] = new_bucket
        if new_bucket >= _B_SCHEMA:
            return new_bucket
        return 0


def _runtime_thread_id(runtime) -> str:
    """从 after_model 的 runtime 提取 thread_id；取不到返回 ""（调用方跳过，勿跨线程串扰）。"""
    try:
        ei = getattr(runtime, "execution_info", None)
        if ei is not None:
            tid = getattr(ei, "thread_id", "") or getattr(ei, "thread", "")
            if tid:
                return str(tid)
        ctx = getattr(runtime, "context", None)
        if ctx is not None:
            tid = getattr(ctx, "thread_id", "")
            if tid:
                return str(tid)
    except Exception:  # noqa: BLE001
        pass
    return ""


class ProgressBoundaryMiddleware(AgentMiddleware):
    """after_model 确定性推进 todo 的监督机（层2）。

    只读模型已发出的工具调用做里程碑判定；本轮模型自己已写 write_todos 时跳过。
    """

    def _after(self, state, runtime) -> dict | None:
        tid = _runtime_thread_id(runtime)
        if not tid:
            return None  # 取不到线程标识 → 宁可不动，避免跨 run 串扰

        try:
            todos = list((state or {}).get("todos") or [])
        except Exception:  # noqa: BLE001
            return None
        if not todos:
            return None  # 策略 B 可跳过 write_todos → 无列表可推进

        # 本轮模型是否已自己写 write_todos → 尊重模型
        try:
            msgs = (state or {}).get("messages") or []
            last_ai = next(
                (m for m in reversed(msgs) if isinstance(m, AIMessage)), None
            )
        except Exception:  # noqa: BLE001
            last_ai = None
        if last_ai is not None:
            tcs = last_ai.tool_calls or []
            if any((tc.get("name") or "") == "write_todos" for tc in tcs):
                return None
        else:
            tcs = []

        # 本轮工具调用 → 判定是否首次到达更高阶段桶
        derived = 0
        for tc in tcs:
            bucket = classify_tool(tc.get("name") or "")
            if bucket > derived:
                derived = bucket
        if derived <= _B_NONE:
            return None

        milestone = _seen_bucket(tid, derived)
        if milestone == 0:
            return None  # 非新里程碑（如纠错循环里重跑 schema/run_sql）

        advanced = advance_todos(todos, milestone)
        if advanced is None:
            return None
        # 同步进度文件（供 sync 端耗时/step_history 连续）
        try:
            from agent.subagents.track_progress import record_todos_progress
            record_todos_progress(tid, advanced)
        except Exception:  # noqa: BLE001
            pass
        _logger.info(
            "[Boundary] 线程 %s 到达桶 %d，确定性推进 todos %d 项",
            tid[:8], milestone, len(advanced),
        )
        return {"todos": advanced}

    def after_model(self, state, runtime):
        return self._after(state, runtime)

    async def aafter_model(self, state, runtime):
        return self._after(state, runtime)
