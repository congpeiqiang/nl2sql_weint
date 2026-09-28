"""build_report 工具 — P4 报告程序化装配。

背景：nl2sql 子任务完成后，报告生成曾是耗时大头（约 70s）——模型需要在一
次超长 LLM 输出里手工搬运整张数据表、SQL、洞察与 base64 iframe（合计约 5k+
token），且极易在途中漏掉/改写 iframe 导致报告不可交互。

本工具把「数据 + SQL + 洞察 + echarts iframe」从主 agent 自身对话 state
（runtime.state["messages"]）里程序化提取并拼装成 Markdown 报告落盘，模型
只需输出报告标题与解读文本（约几百 token）。数据来源：

- `check_async_task` 最近一次 success 的 ToolMessage：其 content 是 JSON，
  内含格式化的数据表 + 洞察 + SQL（约 1.2k chars）。
- `generate_echarts` 的 ToolMessage（全部，多图按出现顺序去重收集）：
  content 内含可交互 iframe（每张约 3.5k chars，base64 内嵌）。

文件名含时分秒：`{report_name}_{YYYY-MM-DD_HH-mm-ss}.md`（满足「报告生成
包含时分秒」要求），落盘到当前活跃工作区 report/ 目录，返回 VFS 路径。
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

# 业务口径的解析 / 逐字核验与子 agent 侧 `CaliberGateMiddleware` **共用同一份实现**
# （本文件下方以 `_` 前缀再导出同名符号，既有脚本的 import 断言不变）。
from agent.utils.caliber_evidence import (
    CALIBER_MAX_ITEMS as _CALIBER_MAX_ITEMS,
    CaliberVerdict,
    load_knowledge_corpus,
    parse_caliber_block as _parse_caliber_block,
    split_caliber_item as _split_caliber_item,
    strip_caliber_block,
    verify_caliber_entries,
)

_logger = logging.getLogger(__name__)

_RE_IFRAME = re.compile(r"<iframe[\s\S]*?</iframe>", re.IGNORECASE)
_SAFE_FNAME = re.compile(r'[\\/:*?"<>|\r\n]+')
# 全量结果表内嵌上限：超过则只给路径链接（防病理性超大表把报告文件撑爆）
_EMBED_MAX_BYTES = 4 * 1024 * 1024

# 数据结果文本内 ```sql 围栏块（子 agent 常在结果里自带产出 SQL，防报告重复成节）
_SQL_FENCE_RE = re.compile(r"```sql[ \t]*\n?([\s\S]*?)```", re.IGNORECASE)


def _result_already_contains_sql(result_text, sql) -> bool:
    """result_text 是否已含与 sql 同一条 SQL（免报告重复生成「执行 SQL」节）。

    ① 任一 ```sql 围栏块归一化（折叠空白）后 == sql —— 子 agent 把产出 SQL 以多行
    围栏写在结果里时，换行/缩进逐字节不等，归一化后视为同一 SQL（trace d60b
    标量双计数）；② 或 sql 以原文出现在 result_text（保留旧语义防原样重复）。
    """
    if not sql:
        return True
    text = str(result_text or "")
    if not text:
        return False
    norm = lambda s: " ".join(str(s).split())
    for m in _SQL_FENCE_RE.finditer(text):
        if norm(m.group(1)) == norm(sql):
            return True
    return sql in text


def _full_table_section(result_obj) -> str:
    """从 check 结果携带的 full_result_files 读盘拼「完整数据表」一节。

    QueryResultOffloadMiddleware 在 run_sql 边界把大结果表（>50 行）确定性落盘，
    check_progress 已把 VFS 指针汇总到 result.full_result_files。这里**代码读盘**
    （0 模型 token）内嵌到报告正文；VFS `/workspace/` 前缀映射到当前活跃工作区
    磁盘目录（与报告落盘目录同源）。文件缺失 / 超大 / 读取失败时降级只写路径
    链接，不报错。
    """
    if not isinstance(result_obj, dict):
        return ""
    files = result_obj.get("full_result_files")
    if not isinstance(files, list) or not files:
        return ""
    try:
        from agent.workspace_manager import get_workspace_manager

        root = Path(get_workspace_manager().active_workspace)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[build_report] active workspace 读取失败: %s", e)
        return ""
    parts = []
    for fp in files:
        if not isinstance(fp, str) or not fp.startswith("/workspace/"):
            parts.append(f"\n- 全量结果文件：`{fp}`（路径不在工作区，无法内嵌）")
            continue
        rel = fp[len("/workspace/"):]
        disk = root / rel
        try:
            if not disk.is_file():
                parts.append(f"\n- 全量结果文件：`{fp}`（文件缺失，无法内嵌）")
                continue
            size = disk.stat().st_size
            if size > _EMBED_MAX_BYTES:
                parts.append(
                    f"\n- 全量结果文件：`{fp}`（{size} 字节过大，未内嵌，可下载查看）"
                )
                continue
            parts.append(disk.read_text(encoding="utf-8").rstrip())
        except Exception as e:  # noqa: BLE001
            _logger.warning("[build_report] 读全量结果文件失败 %s: %s", fp, e)
            parts.append(f"\n- 全量结果文件：`{fp}`（读取失败，可手动打开查看）")
    if not parts:
        return ""
    return "\n\n".join(parts)


def _wren_plan_sql_section(result_obj) -> str:
    """读回「真正下发物理库执行的 SQL」（check_progress 复算后落盘，附 VFS 指针）。

    与 `_full_table_section` 同一套解析范式（VFS `/workspace/` → 活跃工作区磁盘
    目录）。文件读不到 → 退回 check 结果里的内联 `dialect_sql`；都没有 → 返回
    空串，该节整节不出现（**不伪造**，也绝不拿语义层 SQL 冒充可执行语句）。
    """
    if not isinstance(result_obj, dict):
        return ""
    inline = result_obj.get("dialect_sql")
    inline = inline.strip() if isinstance(inline, str) else ""
    fp = result_obj.get("dialect_sql_file")
    if isinstance(fp, str) and fp.startswith("/workspace/"):
        try:
            from agent.workspace_manager import get_workspace_manager

            root = Path(get_workspace_manager().active_workspace)
            disk = root / fp[len("/workspace/"):]
            if disk.is_file():
                return disk.read_text(encoding="utf-8").rstrip()
            _logger.warning("[build_report] 物理 SQL 文件缺失: %s", fp)
        except Exception as e:  # noqa: BLE001  降级用内联
            _logger.warning("[build_report] 读物理 SQL 文件失败 %s: %s", fp, e)
    return inline


# ── Cube「业务口径」两层渲染 ───────────────────────────────────────────
# 报告原来只给「查询定义（Cube 语义层）」的裸 YAML（cube: xxx / measures: [...]），
# 业务用户看不懂。两层解释叠在它之上：
#   Layer A：LLM 把查询翻成 2-3 句业务语言（可选，失败即跳过）
#   Layer B：模板把 measure/dimension 的技术名换成 cube 元数据里的中文描述
# 两层都 fail-open，最坏情况退回到只有 Layer C（原始定义）——与改动前一致。
# 过滤器运算符 → 中文/数学符号（`genre_name:eq:Rock` → `genre_name = Rock`）
_FILTER_OPS = {
    "eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "ge": "≥",
    "lt": "<", "lte": "≤", "le": "≤", "in": "属于",
    "not_in": "不属于", "contains": "包含", "not_contains": "不包含",
}
# 时间粒度 → 中文（cube 的 granularity 或 time_dimension 尾段）
_GRANULARITY_CN = {
    "day": "按日", "week": "按周", "month": "按月",
    "quarter": "按季度", "year": "按年", "hour": "按小时",
}


def _filter_readable(flt, dimensions_meta: dict) -> str:
    """过滤器表达式 → 人读文本；解析不出就原样返回（绝不猜）。"""
    s = str(flt or "").strip()
    if not s:
        return ""
    parts = s.split(":")
    if len(parts) == 3:
        field, op, value = parts
        field_cn = dimensions_meta.get(field) or field
        op_cn = _FILTER_OPS.get(op.lower(), op)
        return f"{field_cn} {op_cn} {value}"
    if len(parts) == 2:
        field, value = parts
        field_cn = dimensions_meta.get(field) or field
        return f"{field_cn} = {value}"
    return s


def _field_list_readable(names, meta: dict) -> str:
    """字段名列表 →「中文描述 · tech_name」顿号串；无描述时只给技术名。

    分隔符用 ``·`` 而**不是括号**：描述本身常带括号（「工时合计（小时）」），
    再套一层括号就成了 ``工时合计（小时）（total_hours）``——两层括注连排，
    可读性差（生产实测反馈）。
    """
    out = []
    for n in names or []:
        name = str(n)
        desc = (meta or {}).get(name) or ""
        out.append(f"{desc} · {name}" if desc else name)
    return "、".join(out)


# ── 业务口径（报告的「## N. 业务口径」节）────────────────────────────
# 合约：一行一条，三字段用 `|` 分隔 —— `口径项 | 内容 | 出处`。用 `|` 而非嵌套对象
# 与仓内其他工具同形（measures/filters 都是 list[str]），模型出参成本最低；解析不出
# 三段时**降级为单条列表项**，绝不因格式问题丢内容。
#
# 解析（`_parse_caliber_block`）与逐字核验（`_split_caliber_item` /
# `verify_caliber_entries` / `load_knowledge_corpus`）都是顶部从
# `agent.utils.caliber_evidence` 再导出的**同一份实现** —— 子 agent 侧
# `CaliberGateMiddleware` 用它们决定打回重写，本文件用它们决定报告怎么标注。
# 两处共用一份 ⇒ 不会出现「闸门说合规、报告说未核验」的两套账。
# 兜底通道（默认关）：>0 时把语义库 `knowledge/rules/*.md` 原文附到报告末尾。
# 生产实测（2026-09-25 16:34 日志）口径原文本就进了子 agent 上下文——MessageSlimmer
# 知识类免截断对 `get_instructions` 生效（「20049 chars 免截断」），故默认走「模型
# 选条 + 出处标注」；本开关是「宁可啰嗦也不漏」的最后一道保险。
_CALIBER_APPENDIX_MAX_CHARS = int(
    os.environ.get("REPORT_CALIBER_APPENDIX_MAX_CHARS", "0") or "0"
)


def _cell(s) -> str:
    """Markdown 表格单元格：转义竖线、折叠换行（否则整张表被撑散）。"""
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


# SQL 生成来源（`result["sql_origin"]`）→ 报告里那行 blockquote 的措辞。
# 判决本身只由 `agent.utils.process_audit.judge_sql_origin` 产生（确定性、零模型
# 往返），报告侧**只负责翻译**，不参与判断 —— 报告不允许自己编一个来源。
_SQL_ORIGIN_TEXT = {
    # 由 Cube 语义层的具名指标直接编译下发（未经模型手写 SQL）
    "cube_metric": "由 Cube 语义层的具名指标直接编译下发（未经模型手写 SQL）",
    # Cube 出指标主体 + 模型包外层 —— 用户最想区分的那一态
    "cube_metric+llm_outer": "Cube 语义层出指标主体，模型手写外层（混合路径）",
    "llm_from_schema": "模型依据语义库 schema 手写（未使用 Cube 具名指标）",
}
_SQL_ORIGIN_MIXED_TITLE = "执行 SQL（Cube 指标主体 + 模型手写外层，实际下发）"


def _sql_origin_line(result_obj) -> str:
    """一行「SQL 生成来源」（blockquote 单行，`_cell` 保证不撑散排版）。

    **`unknown` / 字段缺席 → 返回空串**：老 check 结果没有这个字段，此时报告与改动
    前逐字一致（不给读不出来源的报告硬安一个来源）。四态里只有三态有文案，第四态
    「说不出来」的正确表达就是不说话。
    """
    if not isinstance(result_obj, dict):
        return ""
    text = _SQL_ORIGIN_TEXT.get(str(result_obj.get("sql_origin") or ""))
    return f"> SQL 生成来源：{_cell(text)}" if text else ""


def _render_business_caliber(entries, verdicts=None) -> str:
    """口径条目 → Markdown 表「口径项 / 内容 / 出处」。

    某条解析不出三段（无 `|` 或字段缺）时降级为列表项，**不丢内容**。返回空串表示
    无可用口径 → 调用方整节跳过（不留空标题）。

    `verdicts`（`utils.caliber_evidence.CaliberVerdict` 列表，与 `entries` 按下标对齐）
    **只在报告侧产生**，决定脚注说什么、不通过的行去哪。缺省 `None` = 没做核验
    （读不到语料 / 该节来自主 agent 传参而非子 agent 块）⇒ 脚注只能声明「未核验」。

    ⚠️ 旧实现此处**无条件**写「口径取自语义库知识库原文…未做推断」。生产 trace
    `9c81d3181f2a72f27cf9d092d7185fab` 的表里 `出处` 全是 `workhour_analysis（Cube）`/
    `v_workhour`/`语义库字段字典`，**一个知识库文件都没有** —— 那句脚注与表内容无关，
    等于报告在撒谎。本函数现在按核验结论分态，且**任何一态都不再出现那句话**。
    """
    # 条目 → 核验结论：用「归一后的条目字符串」做键而不是下标 —— `split_caliber_item`
    # 为 None 的条目（loose）不参与核验，下标会对不上。归一化必须与下面取 `s` 的那步
    # **完全一致**（含 `.strip("|")`），否则 `| a | b | c |` 这种带外框的写法会取不到
    # 结论而被当成「通过」（静默放过，比误杀危险得多）。
    by_item: dict[str, CaliberVerdict] = {}
    for v in verdicts or []:
        by_item.setdefault(str(v.item).strip().strip("|").strip(), v)

    ok_rows: list[tuple[str, str, str]] = []
    bad_rows: list[tuple[str, str, str, str]] = []
    loose: list[str] = []
    for it in entries or []:
        s = str(it or "").strip().strip("|").strip()
        if not s:
            continue
        cells = _split_caliber_item(s)
        if cells is None:
            # 降级成列表项：整行是纯文本（不在表格里）⇒ **不转义竖线**，只折空白。
            # 转义只对表格单元格必要，对列表项反而把它读成 `\|`。
            loose.append(" ".join(s.split()))
            continue
        a, b, c = (_cell(cells[0]), _cell(cells[1]), _cell(cells[2]))
        v = by_item.get(s)
        if v is None or v.ok:
            ok_rows.append((a, b, c))
        else:
            bad_rows.append((a, b, c, _cell(_verdict_label(v))))
    if not ok_rows and not bad_rows and not loose:
        return ""

    n_all = len(ok_rows) + len(bad_rows)
    out: list[str] = []
    if ok_rows:
        out += ["| 口径项 | 内容 | 出处 |", "|---|---|---|"]
        out += [f"| {a} | {b} | {c} |" for a, b, c in ok_rows]
    # 不通过的行**不删**（内容是模型原话，用户需要看到它错在哪），但搬进第二张表并
    # 标出结论 —— 留在原表里加 ⚠️ 会被误读成「这条只是可疑」，单列一表才读得出
    # 「这些不是知识库原文」。
    if bad_rows:
        if out:
            out.append("")
        out += [
            f"**未通过核验的口径（{len(bad_rows)} 条）** —— 以下条目**不是**知识库原文，"
            "请勿据此对外解释口径：",
            "",
            "| 口径项 | 内容（模型自述） | 出处（不成立） | 核验结论 |",
            "|---|---|---|---|",
        ]
        out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in bad_rows]
    # 脚注：**只声明代码能证的事**。没有任何一态会替模型夸口「取自知识库原文」。
    note = _caliber_note(n_all, len(ok_rows), len(bad_rows), bool(verdicts))
    if note:
        out += ["", note]
    if loose:
        if out:
            out.append("")
        out += [f"- {x}" for x in loose]
    return "\n".join(out)


# 核验结论 → 表格里的短标签（长文案留在日志与纠正消息里，表宽有限）。
_VERDICT_LABEL = {
    "source_not_kb_file": "出处不是知识库文件名",
    "content_not_verbatim": "内容非原文（被改写/概括）",
    "ellipsis_fragment_too_short": "省略号片段过短",
    "ellipsis_too_many": "省略号过多",
    "ellipsis_too_much_hidden": "省略过多",
    "segments_not_found": "省略号各段未按序命中",
    "content_empty": "内容为空",
    "item_not_three_fields": "条目格式不足三段",
    "corpus_empty": "读不到语料，无法核验",
}


def _verdict_label(v: CaliberVerdict) -> str:
    """核验结论短标签；出处错但内容确在某个文件里时补上「其实在哪个文件」。"""
    label = _VERDICT_LABEL.get(v.reason or "", v.reason or "未通过")
    if v.reason == "source_not_kb_file" and v.closest:
        label += f"（原文见 {v.closest}）"
    return label


def _caliber_note(n_all: int, n_ok: int, n_bad: int, verified: bool) -> str:
    """本节脚注 —— 四态，措辞严格限定在「代码确实证过的范围」内。

    `verified=False`（未做核验：读不到语料 / 块来自主 agent 传参）时**绝不大意**：
    只能说「出处形如文件名」，不能说「已核验」。
    """
    if not verified:
        if n_all == 0:
            return ""
        return (
            "> ⚠️ 本次**未对口径内容做原文逐字核验**（读不到该库的知识库文件，"
            "或本节口径非子 agent 检索所得）；上表出处为模型自述，仅供参考。"
        )
    if n_bad == 0:
        if n_all == 0:
            return ""
        return (
            f"> ✅ 上表 {n_all} 条口径的`出处`均为知识库真实文件名，"
            "且`内容`与该文件原文**逐字一致**（程序化核验，可 `…` 省略中段）。"
        )
    if n_ok == 0:
        # 这一态没有主表，只有上面那张「未通过核验的口径」表 ⇒ 指向要说清，别写「见上表」。
        return (
            f"> ⚠️ 本节 {n_bad} 条口径**全部未通过**原文核验，"
            "均非知识库原文（见上方「未通过核验的口径」表），**不要据此判断答案口径**。"
        )
    return (
        f"> ⚠️ 本节共 {n_all} 条口径：{n_ok} 条经原文逐字核验通过（见上表），"
        f"{n_bad} 条**未通过**（见下方「未通过核验的口径」表）—— "
        "未通过的条目是模型转述或出处不明，**不是知识库原文**。"
    )




def _current_db_name() -> str:
    """当前库名（configurable.db_name）。取不到 → 会话账本兜底 → 仍取不到返回空串。

    ⚠️ 兜底那一段是 2026-09-26 生产实证倒逼的（trace `eaf1c8b2…`）：报告**永远**由
    同步循环建出来的**续跑 run** 落盘（子 agent 完成后 `runs.create` 触发主 agent
    继续跑），而那条 run 的 `configurable` 是手拼的、只有 user_id（见
    `agent/subagents/sync_subagent_todos.py::_notify_main_agent_to_continue`，已修）
    ⇒ `db_name` 读成空 ⇒ `load_knowledge_corpus("")` 直接 `[]` ⇒ 报告侧**逐字核验
    永远降级**成「未核验」，而同一 run 的子 agent 侧闸门（db_name 由父 run 透传）却有
    语料 —— 就是「闸门说 9/11、报告说未核验」的两套账。

    兜底取 `thread_db` 账本（每个建 run 的请求都会记「本会话用过哪个库」，且写的是
    **钳制之后**的值）：只在**恰好记着一个库**时采用。多库＝拿不准，宁可标「未核验」
    也不能拿错库的语料去判模型不合规——那是比不核验更坏的谎报方向。
    """
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        db = str((cfg.get("configurable") or {}).get("db_name", "") or "")
        if db:
            return db
    except Exception:  # noqa: BLE001
        pass
    try:
        _, tid = _current_identity()
        if not tid:
            return ""
        from agent.auth.grants import dbs_for_thread

        cands = [d for d in dbs_for_thread(tid) if d]
        if len(cands) == 1:
            _logger.info("[build_report] configurable 无 db_name，回退会话账本：库 %s", cands[0])
            return cands[0]
        _logger.info(
            "[build_report] configurable 无 db_name，会话账本记着 %d 个库 → 不猜（标未核验）",
            len(cands),
        )
    except Exception as e:  # noqa: BLE001  兜底失败 = 维持原行为（标未核验）
        _logger.debug("[build_report] 会话账本兜底失败: %s", e)
    return ""


def _caliber_appendix(result_obj) -> str:
    """兜底通道：语义库 `knowledge/rules/*.md` 原文（由 env 开关开启时）。

    唯一不依赖模型自觉的「报告必含业务口径」通道：按当前 db_name 解析 wren 项目目录，
    直接读规则原文附在报告末尾。任何一步失败返回空串（fail-open，报告照出）。
    """
    if _CALIBER_APPENDIX_MAX_CHARS <= 0:
        return ""
    try:
        from agent.utils.wren_call_extract import resolve_wren_ctx_by_db

        db = _current_db_name()
        if not db:
            return ""
        project, _ = resolve_wren_ctx_by_db(db)
        if not project:
            return ""
        rules_dir = Path(str(project)) / "knowledge" / "rules"
        if not rules_dir.is_dir():
            return ""
        budget = _CALIBER_APPENDIX_MAX_CHARS
        parts: list[str] = []
        for f in sorted(rules_dir.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001  单文件读失败不影响其余
                continue
            if not text:
                continue
            if len(text) > budget:
                text = text[:budget].rstrip() + f"\n…（{f.name} 余下省略）"
            parts.append(f"### {f.name}\n\n{text}")
            budget -= len(text)
            if budget <= 0:
                break
        if not parts:
            return ""
        return "\n\n".join(parts)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[build_report] 口径附录读取失败（跳过）: %s", e)
        return ""


def _render_cube_layer_b(cube_args: dict, metadata: dict) -> str:
    """Layer B：模板渲染查询结构（带中文描述）。失败/无内容返回空串。

    输出示例::

        数据模型：sales_analytics（音乐商店销售分析：收入、订单、客单价）
        ├─ 维度：账单国家 · billing_country、音乐流派 · genre_name
        ├─ 度量：总收入 · total_revenue、订单数 · invoice_count
        └─ 筛选：音乐流派 = Rock
    """
    if not isinstance(cube_args, dict) or not cube_args:
        return ""
    cube_name = str(cube_args.get("cube") or "")
    if not cube_name:
        return ""
    meta = metadata if isinstance(metadata, dict) else {}
    cube_desc = str(meta.get("cube_description") or "")
    measures_meta = meta.get("measures") or {}
    dimensions_meta = meta.get("dimensions") or {}

    lines = [f"数据模型：{cube_name}" + (f"（{cube_desc}）" if cube_desc else "")]

    dims = cube_args.get("dimensions")
    if isinstance(dims, list) and dims:
        lines.append("├─ 维度：" + _field_list_readable(dims, dimensions_meta))
    measures = cube_args.get("measures")
    if isinstance(measures, list) and measures:
        lines.append("├─ 度量：" + _field_list_readable(measures, measures_meta))
    filters = cube_args.get("filters")
    if isinstance(filters, list) and filters:
        flist = [x for x in (_filter_readable(f, dimensions_meta) for f in filters) if x]
        if flist:  # 全解析成空串时不留一个光秃秃的「筛选：」标签
            lines.append("├─ 筛选：" + "；".join(flist))

    # 时间：granularity 可能独立给，也可能并进 time_dimension（`invoice_date:month`）
    time_dim = str(cube_args.get("time_dimension") or "")
    granularity = str(cube_args.get("granularity") or "")
    td_field, td_gran = time_dim, ""
    if ":" in time_dim:
        td_field, td_gran = time_dim.split(":", 1)
    gran = granularity or td_gran
    if td_field or gran:
        seg = []
        if td_field:
            seg.append(_field_list_readable([td_field], dimensions_meta))
        if gran:
            gran_cn = _GRANULARITY_CN.get(gran.lower(), gran)
            # 有维度时把粒度括注在后面（`work_date（按月）`），只有粒度时直接给
            seg = [seg[0] + f"（{gran_cn}）"] if seg else [gran_cn]
        lines.append("├─ 时间：" + "".join(seg))

    segments = cube_args.get("segments")
    if isinstance(segments, list) and segments:
        lines.append("├─ 分段：" + "、".join(str(s) for s in segments))
    order_by = cube_args.get("order_by")
    if isinstance(order_by, list) and order_by:
        lines.append("├─ 排序：" + "、".join(str(o) for o in order_by))

    # 末行树形符收尾（把最后一个 ├─ 换成 └─），保持树形可读
    if len(lines) > 1:
        lines[-1] = lines[-1].replace("├─ ", "└─ ", 1)
    return "\n".join(lines)


_CUBE_LAYER_A_PROMPT = """请把下面这次数据查询用 2-3 句中文解释给业务人员听。

要求：
- 用业务语言，不要出现表名、字段名、SQL 术语
- 说明：查了什么对象的数据、按什么口径分组、看了哪些指标
- 若有筛选条件，说明数据范围；若有时间维度，说明周期
- 只输出这段解释本身，不要标题、不要前后缀、不要列表符号

查询信息：
{info}"""


async def _render_cube_layer_a(cube_args: dict, metadata: dict) -> str:
    """Layer A：LLM 把 Cube 查询翻成 2-3 句业务语言。任何失败返回空串（跳过该层）。"""
    if not isinstance(cube_args, dict) or not cube_args:
        return ""
    try:
        from agent.llms.model import create_model

        # 用户身份：模型配置已按用户隔离，必须与主 agent 用同一份配置，
        # 否则会落到全局 store（生产上就是那份过期/不同 key 的配置）。
        # 读法与 ThinkingToggleMiddleware._resolve_overrides 一致：request.runtime.config
        # 恒为空，只能走 langgraph.config.get_config()。
        user_id = None
        try:
            from langgraph.config import get_config as _lg_get_config

            user_id = (_lg_get_config().get("configurable", {}) or {}).get("user_id")
            user_id = str(user_id) if user_id else None
        except Exception:  # noqa: BLE001  取不到就退回全局 store
            pass

        # 短摘要用不着思考链：关掉省时省钱（配置缺失时 create_model 返回 None）
        model = create_model(enable_thinking=False, user_id=user_id)
        if model is None:
            _logger.debug("[build_report] Layer A 跳过：无可用模型配置")
            return ""

        meta = metadata if isinstance(metadata, dict) else {}
        cube_name = str(cube_args.get("cube") or "")
        cube_desc = str(meta.get("cube_description") or "")
        measures_meta = meta.get("measures") or {}
        dimensions_meta = meta.get("dimensions") or {}

        info_lines = [f"数据模型：{cube_name}" + (f"（{cube_desc}）" if cube_desc else "")]
        dims = cube_args.get("dimensions")
        if isinstance(dims, list) and dims:
            info_lines.append("分组维度：" + _field_list_readable(dims, dimensions_meta))
        measures = cube_args.get("measures")
        if isinstance(measures, list) and measures:
            info_lines.append("统计指标：" + _field_list_readable(measures, measures_meta))
        filters = cube_args.get("filters")
        if isinstance(filters, list) and filters:
            flist = [_filter_readable(f, dimensions_meta) for f in filters]
            info_lines.append("筛选条件：" + "；".join(x for x in flist if x))
        gran = str(cube_args.get("granularity") or "")
        time_dim = str(cube_args.get("time_dimension") or "")
        if gran or time_dim:
            info_lines.append("时间维度：" + (gran or time_dim))
        if len(info_lines) <= 1:
            # 只有 cube 名，没别的可解释——省一次 LLM 调用
            return ""

        prompt = _CUBE_LAYER_A_PROMPT.format(info="\n".join(info_lines))
        # model.invoke 是同步的：to_thread 避免阻塞事件循环（本函数在 async 工具里跑）
        import asyncio

        resp = await asyncio.to_thread(model.invoke, prompt)
        content = getattr(resp, "content", None)
        if content is None:
            content = str(resp)
        summary = str(content).strip()
        if len(summary) > 500:
            summary = summary[:500].rstrip() + "…"
        return summary
    except Exception as e:  # noqa: BLE001  报告不能因这一层挂掉
        _logger.warning("[build_report] Layer A 生成失败（跳过）: %s", e)
        return ""


# ── 消息归一化（兼容 dict 与 LangChain BaseMessage）───────────────────
def _msg_name(msg) -> str:
    if isinstance(msg, dict):
        return str(msg.get("name") or msg.get("type") or "")
    return str(getattr(msg, "name", "") or "")


def _msg_content(msg) -> str:
    if isinstance(msg, dict):
        c = msg.get("content", "")
    else:
        c = getattr(msg, "content", "")
    if isinstance(c, str):
        return c
    # content 可能是 list[{type:"text", text:...}]
    if isinstance(c, list):
        parts = []
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text":
                parts.append(str(it.get("text", "")))
            else:
                parts.append(str(it))
        return "\n".join(parts)
    return str(c)


def _find_last_check_result(messages, task_id: str):
    """最近一次 status=success 的 check_async_task 结果。

    check_progress._build_check_command 创建的 ToolMessage **不带 name**（只有
    tool_call_id），因此不能按工具名过滤，改为按内容签名识别：JSON 解析后同时
    具备 status / thread_id / result 字段即视为 check_async_task 结果。

    Returns: (result_obj, result_text) 或 None。result_obj 含 status/thread_id/
    result/result_size 等字段；result_text 是格式化 Markdown（表+洞察+SQL）。
    """
    best = None
    for m in messages:
        if _msg_name(m) not in ("", "tool", "check_async_task"):
            # 非工具消息（user/ai）直接跳过；工具消息继续按内容识别
            role = _msg_name(m)
            if role in ("user", "assistant", "ai", "human", "system"):
                continue
        try:
            obj = json.loads(_msg_content(m))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        # 内容签名：check_async_task 结果必然带这三个键
        if not ({"status", "thread_id", "result"} <= obj.keys()):
            continue
        if obj.get("status") != "success":
            continue
        result_text = obj.get("result")
        if not result_text:
            continue
        if task_id and obj.get("thread_id") and obj.get("thread_id") != task_id:
            continue
        best = (obj, result_text)
    return best


def _find_all_iframes(messages) -> list[str]:
    """全部 generate_echarts 返回的完整 iframe 标签（原样保留，按出现顺序去重）。

    2026-09-08 修复：此前只取最后一张（_find_last_iframe），会话生成两张图时
    报告附录只嵌第 2 张（thread 01a07fd4 堆叠条形图+热力图只剩热力图）。
    去重防同一 iframe 被模型在最终回复里复述时重复嵌入。
    """
    seen: set[str] = set()
    out: list[str] = []
    for msg in messages:
        for m in _RE_IFRAME.finditer(_msg_content(msg)):
            tag = m.group(0)
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


def _current_turn_start(messages) -> int:
    """当前问题轮次的起点：最后一条真实用户消息的下标。

    跳过 [系统自动通知] 注入（子任务超时/完成续跑的系统消息，非用户新问题）——
    超时接管流程里图表生成在通知之后，锚定真实提问才能把它们收进来。
    找不到则回退 0（整个历史，兼容无 human 消息的合成场景）。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict):
            role = m.get("type") or m.get("role")
            text = _msg_content(m)
        else:
            role = getattr(m, "type", "")
            text = _msg_content(m)
        if role in ("human", "user") and not text.lstrip().startswith("[系统自动通知]"):
            return i
    return 0


def _turn_chart_iframes(messages) -> list[str]:
    """当前问题轮次 generate_echarts 产出的全部图表 iframe（按序去重）。

    作用域双重收窄（2026-09-08，用户要求报告只保存当前问题的图）：
    1. 轮次：只扫最后一条真实用户消息之后的片段 → 历史问题的图不进本报告；
    2. 消息角色：只认 tool 结果（图表必然以 generate_echarts 的 ToolMessage
       到达）；AI 文本里的 iframe 复述一律不计——既天然去重本轮复述，也排除
       模型在答复里引用历史轮次图表的边界情况。
    """
    turn = messages[_current_turn_start(messages):]
    tool_msgs = []
    for m in turn:
        if isinstance(m, dict):
            role = m.get("type") or m.get("role")
        else:
            role = getattr(m, "type", "")
        if role in ("tool", "tool_result"):
            tool_msgs.append(m)
    return _find_all_iframes(tool_msgs)


def _current_identity() -> tuple[str, str]:
    """当前 run 的 (user_id, thread_id)。

    身份取自 `langgraph.config.get_config()` 的 configurable —— 外部 run 请求里的
    这两个键已由 `LangfuseMetadataMiddleware` 按登录身份钳制（P1-2），不会被客户端
    伪造；离线脚本/直调场景读不到就返回空串，由调用方兜底。
    """
    try:
        from langgraph.config import get_config

        cfg = get_config().get("configurable", {}) or {}
        return str(cfg.get("user_id") or ""), str(cfg.get("thread_id") or "")
    except Exception:  # noqa: BLE001
        return "", ""


def _thread_tag() -> str:
    """文件名里的会话短标识（同名同秒去重用）。

    读不到会话时用随机 8 hex 兜底 —— **不能返回空串**：空串会让「两个用户同秒出
    同名报告」又回到互相覆盖，而那正是本函数要防的场景。
    """
    _, tid = _current_identity()
    tag = re.sub(r"[^0-9a-zA-Z]", "", tid)[:8]
    return tag or uuid.uuid4().hex[:8]


class BuildReportSchema(BaseModel):
    """build_report 输入。"""

    report_name: str = Field(
        description="报告标题 / 文件名（不含扩展名与时间戳），如「各类型电影数量分布」。"
    )
    analysis: str = Field(
        description="对查询结果的分析解读，Markdown 文本（可用 **加粗**、- 列表、### 小节等）。"
    )
    business_caliber: list[str] = Field(
        default_factory=list,
        description=(
            "（可选）本次结论依据的业务口径，每条一行、三字段用 | 分隔："
            "`口径项 | 内容 | 出处`，如 "
            "`已审核工时 | if_approve = 1 的 work_hour 之和 | rules/报工与工时.md R3`。"
            "**内容必须逐字取自知识库原文**（可 `…` 省略中段，不可改写/概括），"
            "出处必须是知识库里真实存在的文件名（有条目号就一并写），没取到就留空、"
            "**不要编**（表名、视图名 v_*、Cube 名、字段字典都**不是**出处）。"
            "本工具会**逐条做出处 + 内容逐字核验**，未通过的条目在报告里单列并标注。"
            "不传时自动从子任务结果的「业务口径」块提取。"
        ),
    )
    task_id: str = Field(
        default="",
        description="（可选）对应查询的任务 id；不传则自动取最近一次成功的查询结果。",
    )


async def _build_report_coro(
    report_name: str,
    analysis: str,
    task_id: str,
    runtime: Annotated[ToolRuntime, InjectedToolArg()],
    business_caliber: list[str] | None = None,
) -> str:
    try:
        state = runtime.state or {}
        messages = state.get("messages") or []
    except Exception as e:  # noqa: BLE001  state 不可读时给出明确错误
        _logger.warning("[build_report] runtime.state 读取失败: %s", e)
        return "build_report 失败：无法读取当前对话状态。请稍后重试。"

    hit = _find_last_check_result(messages, task_id)
    if not hit:
        return (
            "build_report 失败：未找到可用的查询结果。"
            "请先用 check_async_task 确认子任务已成功完成（status=success）。"
        )
    _obj, result_text = hit
    # 语义层 SQL：check_async_task 已把子线程最后一次 run_sql 附在 result.sql。
    # **注意它是模型写的形态**（引用 MDL 视图如 v_workhour），物理库里跑不了 →
    # 真正可执行的语句另见下面的 plan_sql。
    sql = _obj.get("sql", "") if isinstance(_obj, dict) else ""
    sql = sql.strip() if isinstance(sql, str) else ""
    # Cube 快速通道（wrenai_*_query_cube）不产生 run_sql → check 结果里没有 sql，
    # 只有 cube_query（查询定义）。没有它时本节会整节消失（同日同题实测：run_sql
    # 通道报告 16087 字含 SQL 节，Cube 通道 8450 字零 SQL 字样）。
    cube_query = _obj.get("cube_query", "") if isinstance(_obj, dict) else ""
    cube_query = cube_query.strip() if isinstance(cube_query, str) else ""

    # ── 业务口径：① 主 agent 显式传参 → ② 从子任务结果抽「业务口径」块 → ③ 都无则整节跳过 ──
    # ② 是主路径：口径原文（knowledge/rules/*.md）是**子 agent** 拿到的（MessageSlimmer
    # 知识类免截断保证它进上下文，生产日志实证），主 agent 只看到摘要过的结果 ⇒ 不能只
    # 依赖主 agent 转述。子 agent 最终回复末尾带「## 业务口径」块（契约见系统提示词）。
    _caliber: list[str] = [str(x) for x in (business_caliber or []) if str(x or "").strip()]
    if not _caliber:
        _caliber = _parse_caliber_block(result_text)
    # 逐字核验：**判决只在本处产生**（子 agent 侧 `CaliberGateMiddleware` 只负责打回重写，
    # 不写任何结论行），用的是**同一份磁盘语料**（`utils.caliber_evidence`）⇒ 不会出现
    # 「闸门放过、报告说未核验」的两套账。读不到语料 ⇒ `verdicts=None`，脚注降级为
    # 「未核验」（fail-open，报告照出）。
    #
    # ⚠️ 刻意**不**摘取子 agent 块里的 `> …核验…` 行：中间件不写结论行，能摘到的只可能
    # 是**模型自己写的**自评（「本表口径均已逐字核验」），把它印进报告就是「报告撒谎」
    # 换了个人称。核验结论必须由代码产生、且只由代码产生。
    _verdicts = None
    if _caliber:
        _db = _current_db_name()
        try:
            _corpus = load_knowledge_corpus(_db)
            if _corpus:
                _verdicts = verify_caliber_entries(_caliber, _corpus)
                _bad = sum(1 for v in _verdicts if not v.ok)
                _logger.info(
                    "[build_report] 口径核验：%d/%d 条通过（库 %s）%s",
                    len(_verdicts) - _bad, len(_verdicts), _db or "?",
                    "" if _bad == 0 else "；未通过条目已在报告中单列",
                )
            else:
                _logger.info("[build_report] 读不到知识库语料，口径节标「未核验」（fail-open）")
        except Exception as e:  # noqa: BLE001  核验失败不影响报告产出，降级为未核验
            _logger.warning("[build_report] 口径核验异常（降级为未核验）: %s", e)
            _verdicts = None
    _caliber_md = _render_business_caliber(_caliber, _verdicts)

    # 只收「当前问题」轮次的图表（_turn_chart_iframes：轮次锚定 + 仅 tool 结果），
    # 历史问题生成的 iframe 不进本报告（用户明确要求：报告只保存当前 trace 的图）
    iframes = _turn_chart_iframes(messages)

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_name = _SAFE_FNAME.sub("_", report_name).strip(" ._") or "report"
    # P1-3：文件名原先只到**秒级**且无任何用户/会话维度，两个用户同秒对同名主题
    # 出报告 = 后写的**静默覆盖**前一份（报告目录是全站共享的）。加会话短标识 +
    # 存在性避让，保证「同名同秒」也不互相覆盖。
    _tag = _thread_tag()
    fname = f"{safe_name}_{now}_{_tag}.md"
    try:
        from agent.workspace_manager import get_workspace_manager as _gwm

        _report_dir = _gwm().report_dir
        _n = 2
        while (_report_dir / fname).exists():
            fname = f"{safe_name}_{now}_{_tag}_{_n}.md"
            _n += 1
    except Exception:  # noqa: BLE001  目录不可用时交给下面的写盘分支报错
        pass

    md_parts = [
        f"# {report_name}\n",
        f"> 生成时间：{now}",
        f"> 数据来源：NL2SQL 查询结果",
        f"> 数据截至：{now}（活库数据，可能随时间变动）\n",
        "## 1. 数据结果\n",
        # §1 内联的是子 agent 的**完整答复**，而契约要求该答复末尾就带「业务口径」块 ⇒
        # 不摘掉的话 §2 渲染器会把同一张表再排一遍（2026-09-26 生产实证：同一 11 行出现
        # 两次）。抽块仍从 result_text 原文抽，两者只是渲染分工不同。
        strip_caliber_block(result_text),
    ]
    next_section = 2
    # 大结果全量表内嵌（QueryResultOffload 落盘文件，代码读盘 0 token；无则跳过）
    full_table = _full_table_section(_obj)
    if full_table:
        md_parts += [f"\n## {next_section}. 完整数据表\n", full_table]
        next_section += 1
    # 「业务口径」放在数据与 SQL 之前：先说清口径，再看数。Cube 通道的 Layer A 摘要与本
    # 节同义，故下面在已有本节时不再重复生成（见 Cube 分支的条件）。
    if _caliber_md:
        md_parts += [f"\n## {next_section}. 业务口径\n", _caliber_md]
        next_section += 1
    # 模型最终回复若已含同一条 SQL（原文或围栏块归一化相等）则不重复成节。
    # 注记（sql_note：wren 通道方言提示 / 明细口径注）在跳节时也照样渲染——
    # 否则内嵌 SQL 的 wren/PG 方言没有任何警示，用户直连 MySQL 跑必炸
    # （2026-09-08 INTERVAL '1 year' 实例）。
    # 真正下发目标库执行的语句（check_progress 复算 + 落盘，见 agent/utils/wren_plan）。
    # 上面那条 sql / cube_query 是**语义层**形态（引用 MDL 视图），在物理库里跑不了。
    plan_sql = _wren_plan_sql_section(_obj)
    plan_note = _obj.get("physical_sql_note") if isinstance(_obj, dict) else ""
    if sql:
        _sql_note = _obj.get("sql_note") if isinstance(_obj, dict) else ""
        # 混合路径（Cube 出主体 + 模型包外层）时标题如实演进；其余三态标题一字不动
        # （老结果的标题必须与改动前逐字相同）
        _sql_title = (
            _SQL_ORIGIN_MIXED_TITLE
            if isinstance(_obj, dict)
            and str(_obj.get("sql_origin") or "") == "cube_metric+llm_outer"
            else "执行 SQL"
        )
        if _result_already_contains_sql(str(result_text), sql):
            _logger.info("[build_report] 数据结果已含同 SQL（原文/围栏归一化），跳过追加「执行 SQL」节")
            if _sql_note:
                md_parts += ["", f"> {_sql_note}"]
        else:
            md_parts += [f"\n## {next_section}. {_sql_title}\n", f"```sql\n{sql}\n```"]
            if _sql_note:
                md_parts += ["", f"> {_sql_note}"]
            next_section += 1
        # wren 语义层通道（wrenai_*）：语义层 SQL 之上再给物理 SQL（直连通道
        # dbmcp_* 的 SQL 本身就是目标库方言，没有这一节）
        if plan_sql:
            md_parts += [
                f"\n## {next_section}. 执行 SQL（物理，实际下发）\n",
                f"```sql\n{plan_sql}\n```",
            ]
            if plan_note:
                md_parts += ["", f"> {plan_note}"]
            next_section += 1
        # 来源行放在**分支末尾**：跳节路径（结果里已内嵌 SQL）与正常路径都要有 ——
        # 前者正是「用户看到的那条 SQL 在正文里」的情形，更需要标来源。
        _origin_line = _sql_origin_line(_obj)
        if _origin_line:
            md_parts += ["", _origin_line]
    elif cube_query:
        # Cube 通道：物理 SQL 在前（用户要的是可粘贴执行的那条），语义层查询定义
        # 在后作为「来源」注解。取不到物理 SQL 时只出定义节，标题**不叫「执行 SQL」**
        # ——用「执行 SQL」会暗示一条并不存在的 SQL。
        if plan_sql:
            md_parts += [
                f"\n## {next_section}. 执行 SQL（由 Cube 语义层编译，实际下发）\n",
                f"```sql\n{plan_sql}\n```",
            ]
            if plan_note:
                md_parts += ["", f"> {plan_note}"]
            next_section += 1
        _cube_note = _obj.get("sql_note") if isinstance(_obj, dict) else ""

        # ── Cube「业务口径」两层：先取 cube 元数据里的中文描述 ──
        # 原始定义（cube_args）+ 工具名由 check_progress 透传；工具名在此重新解析
        # 成项目路径（与 check_progress 同一个 resolve_wren_ctx，失败即降级）。
        _cube_args = _obj.get("cube_args") if isinstance(_obj, dict) else None
        _cube_tool = _obj.get("cube_tool") if isinstance(_obj, dict) else ""
        _cube_meta: dict = {}
        if isinstance(_cube_args, dict) and _cube_args and _cube_tool:
            try:
                from agent.utils.wren_call_extract import (
                    load_cube_metadata,
                    resolve_wren_ctx,
                )

                _project, _ = resolve_wren_ctx(str(_cube_tool))
                _cube_meta = load_cube_metadata(
                    _project, str(_cube_args.get("cube") or "")
                )
            except Exception as e:  # noqa: BLE001  拿不到描述就降级展示技术名
                _logger.warning("[build_report] Cube 元数据加载失败: %s", e)

        # Layer A：LLM 业务口径摘要 / Layer B：模板结构（两层各自 fail-open，
        # 都返回空串时本节整体跳过，直接进下面的原始定义节）。
        # 模型已给结构化口径（_caliber_md）时**跳过 Layer A**：否则同一份报告出现两节
        # 「业务口径」，且 Layer A 是 LLM 摘要（有幻觉面）而结构化口径带出处 —— 顺带省
        # 一次 LLM 调用。
        if isinstance(_cube_args, dict) and _cube_args and not _caliber_md:
            _layer_a = await _render_cube_layer_a(_cube_args, _cube_meta)
            if _layer_a:
                md_parts += [f"\n## {next_section}. 业务口径\n", _layer_a]
                next_section += 1
        # Layer B 与口径**无关**（查询结构 = 维度/过滤树，取自 cube 元数据，无 LLM 调用）
        # ⇒ 不能挂在 Layer A 的开关上：挂了就出现「子 agent 给了结构化口径 ⇒ 报告同时
        # 丢掉查询结构」这种静默缺口。
        if isinstance(_cube_args, dict) and _cube_args:
            _layer_b = _render_cube_layer_b(_cube_args, _cube_meta)
            if _layer_b:
                # **必须包代码围栏**：Markdown 里段落内的单个换行会被渲染成空格，
                # 树形文本（├─/└─）会塌成一行（生产实测反馈）。与 Layer C 同款式。
                md_parts += [
                    f"\n## {next_section}. 查询结构\n",
                    f"```\n{_layer_b}\n```",
                ]
                next_section += 1

        # Layer C：原始定义（**始终保留**，两层全挂时报告与改动前一致）
        md_parts += [
            f"\n## {next_section}. 查询定义（Cube 语义层）\n",
            f"```yaml\n{cube_query}\n```",
        ]
        if _cube_note:
            md_parts += ["", f"> {_cube_note}"]
        next_section += 1
        # 纯 Cube 通道也要标来源：这条 SQL 不是模型手写的，报告必须说得出区别
        _origin_line = _sql_origin_line(_obj)
        if _origin_line:
            md_parts += ["", _origin_line]
    md_parts += [f"\n## {next_section}. 分析解读\n", str(analysis).strip()]
    next_section += 1
    if iframes:
        md_parts += [f"\n## {next_section}. 附录：交互式图表\n"]
        for _i, _ifr in enumerate(iframes, 1):
            if len(iframes) > 1:
                # 多图各加小节标题（前端 MarkdownContent 按 iframe 切分原位渲染，
                # 小节标题不影响切分）；单图保持旧版排版不加标题
                md_parts += [f"### 图表 {_i}", _ifr, ""]
            else:
                md_parts += [_ifr]
        md_parts += ["\n> 💡 可交互图表：鼠标悬停查看数值、可缩放。"]
    else:
        md_parts.append(f"\n## {next_section}. 附录\n\n（本次任务未生成交互式图表）")
    next_section += 1
    # 兜底通道（env 开关，默认关）：语义库 `knowledge/rules/*.md` 原文。模型侧没给出
    # 口径时的最后一道保险，放最末尾以免打断正文阅读。
    _caliber_raw = _caliber_appendix(_obj)
    if _caliber_raw:
        md_parts += [f"\n## {next_section}. 附录：语义库口径原文\n", _caliber_raw]

    md = "\n".join(md_parts)

    # ── 落盘：活跃工作区 report/ 目录 ──
    try:
        from agent.workspace_manager import get_workspace_manager

        report_dir = get_workspace_manager().active_workspace / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        dest = report_dir / fname
        dest.write_text(md, encoding="utf-8")
        # P1-3：登记归属（文件名 → 用户/会话）。读接口 /api/reports/{filename} 靠它
        # 判定「这份报告是不是你的」；不登记就只能对所有人放行。
        try:
            from agent.auth.grants import record_report_owner

            _uid, _tid = _current_identity()
            if _uid:
                record_report_owner(fname, _uid, _tid)
        except Exception:  # noqa: BLE001  归属登记失败不影响报告本身
            _logger.debug("[build_report] 归属登记失败", exc_info=True)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[build_report] 写盘失败: %s", e)
        return (
            "build_report 部分失败：报告已拼装但写入文件系统出错"
            f"（{type(e).__name__}: {e}）。可改用 write_file 手动落盘。"
        )

    vfs_path = f"/workspace/report/{fname}"
    _logger.info("[build_report] 报告已生成: %s (%d chars)", vfs_path, len(md))
    return (
        f"报告已生成：{vfs_path}\n"
        f"- 标题：{report_name}\n"
        f"- 生成时间：{now}\n"
        f"- 内容：数据结果"
        + ("、业务口径" if _caliber_md else "")
        + "、分析解读"
        + (f"、内嵌交互式图表×{len(iframes)}" if iframes else "")
        + "\n"
        "请在最终回复中告知用户报告文件路径。"
    )


build_report_tool = StructuredTool.from_function(
    coroutine=_build_report_coro,
    name="build_report",
    description=(
        "把已完成的查询结果程序化装配为 Markdown 报告并写入工作区 report/ 目录。"
        "自动提取最近一次 check_async_task 成功的数据结果（数据表+SQL 或 Cube 查询定义+洞察，"
        "wren 语义层通道另附「真正下发物理库执行」的 SQL，可直接粘贴执行）与"
        " generate_echarts 生成的交互式图表（内嵌 iframe，可交互渲染）。"
        "调用前请确保已用 check_async_task 确认子任务完成、并用 generate_echarts 渲染图表。"
        "业务口径默认自动从子任务结果的「业务口径」块提取（子 agent 手里才有 knowledge/rules/*.md "
        "原文）；若你已知口径而子任务结果里没有，用 business_caliber 显式传入，"
        "每条 `口径项 | 内容 | 出处`，内容逐字取原文、出处写知识库真实文件名"
        "（表名/视图名 v_*/Cube 名/字段字典都不是出处）。"
        "本工具对每条口径做出处 + 内容逐字核验，未通过的条目在报告里单列并标注，"
        "不会谎称「取自知识库原文」。"
        "报告文件名自动包含精确到时分秒的时间戳，无需再用 shell 取时间。"
    ),
    args_schema=BuildReportSchema,
)
