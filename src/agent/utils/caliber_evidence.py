"""业务口径的「原文证据」校验：解析 / 归一化 / 逐字比对 / 出处白名单 / 语料加载。

**为什么独立成模块**：两个调用点必须是**同一份实现**，否则「核验时看到的条目」与
「渲染时看到的条目」会漂移：

1. 子 agent 侧 `middlewares/caliber_gate.CaliberGateMiddleware`——在终态答案上核验，
   决定是否打回重写（只「逼」，不下判决）；
2. 主 agent 侧 `tools/report_builder._render_business_caliber`——用**同一份语料**重算
   核验结论，决定报告脚注与分表（判决只在这一处产生）。

## 语料为什么取自磁盘知识库，而不是子 agent state 里的工具返回

这是被事实逼出来的，不是图省事：

- `wren/context.py:713-724` 的 `load_knowledge_rules()` 是 `"\\n\\n".join(parts)`——
  **拼接里没有文件名**。所以拿 `get_instructions` 的返回来当语料，**根本判不出
  「这段文字属于哪个文件」**，也就验不出「引了 A 文件的话、署名 B 文件」这种张冠李戴。
- 知识库的正文本体就在磁盘上（`resolve_wren_ctx_by_db(db_name)` 的 wren 项目目录），
  0 成本、权威、可离线造 fixture 测。
- 知识只有 MCP 一条投递通道（`knowledge/**` 的 read/grep 被文件权限拒，
  见 NL2SQL_SYSTEM_PROMPT.md §十二），所以**逐字命中磁盘原文即反证本次确实取过这份料**
  ——不需要再去 state 里对一遍（且 state 会被 auto-compact / MessageSlimmer 改动，拿它
  当门槛会对真取过的料误判「没取过」，反而制造误打回）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

# ── 块解析（原 report_builder._parse_caliber_block，逐字搬来）────────────
# 合约：一行一条，三字段用 `|` 分隔 —— `口径项 | 内容 | 出处`。
CALIBER_HEADING_RE = re.compile(
    r"^\s*(?:#{1,4}\s*业务口径\s*|\*\*业务口径\*\*)\s*$", re.M
)
CALIBER_MAX_ITEMS = 20

# 知识库子目录。**必须**等于 `api.wren_semantic._KNOWLEDGE_CATEGORIES` 的值集
# （{glossary,metrics,rules,sql,caveats}）——不反向 import api（依赖方向反了），
# 漂移由 `scripts/verify_caliber_evidence.py` 的断言抓。
KNOWLEDGE_DIRS = ("glossary", "metrics", "rules", "sql", "caveats")

# 「本轮取过知识料」的证据口径。**刻意与 message_slimmer 的免截断名单分开**：
# 那份是**上下文预算**口径（3 个后缀，`get_context` 123k 字符绝不能免截断），
# 这份只回答「这次 run 有没有取过料」一个问题。仓内已另有 langfuse_span /
# progress_boundary 两份不同的知识工具表，本集合是第四份，各管各的，不强行统一。
CALIBER_EVIDENCE_TOOL_SUFFIXES = (
    "get_instructions",     # rules/*.md 规则轴（口径主力）
    "get_all_knowledge",    # metrics + glossary + caveats
    "list_knowledge",       # 老 wren 的 get_all_knowledge 替代名
    "get_context",          # JSON，其中 instructions 字段是 rules 原文
    "recall_queries",       # knowledge/sql/*.md 范例
)


def is_caliber_evidence_tool(tool_name: str | None) -> bool:
    """工具名是否属于「知识料取料」（按后缀匹配，兼容 `wrenai_<库名>_` 前缀）。

    只用于**观测**（本轮取过料却没写口径块时打一条日志），不参与任何门槛判定。
    """
    if not tool_name:
        return False
    return str(tool_name).endswith(CALIBER_EVIDENCE_TOOL_SUFFIXES)


def parse_caliber_block(text: str) -> list[str]:
    """从子 agent 最终回复里抽「业务口径」块（标题后的条目行）。

    `check_async_task` 的 result 是**摘要过**的子 agent 最终回复，但两条路径都保留
    尾部（纯文本路径头尾各半；含表格路径把表后正文按剩余预算同样头尾保留），故本块
    放在回复末尾能存活。取**最后一个**同名标题（防正文里引用过同名小节）。

    兼容 `- a | b | c`、`1. a | b | c`、`| a | b | c |` 表格三种写法；抽不到返回
    空列表（调用方退回 Cube 通道 Layer A，或整节跳过）。
    """
    if not text:
        return []
    hits = list(CALIBER_HEADING_RE.finditer(str(text)))
    if not hits:
        return []
    out: list[str] = []
    for line in str(text)[hits[-1].end():].split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):  # 下一个标题 → 本节结束
            break
        item = ""
        if s.startswith("|"):  # 表格形态：表头/分隔行跳过，数据行还原成 `a | b | c`
            cells = [c.strip() for c in s.strip("|").split("|")]
            cells = [c for c in cells if c]
            if not cells or all(set(c) <= set("-: ") for c in cells):
                continue
            if cells[0] in ("口径项", "项目", "口径", "条目"):
                continue
            item = " | ".join(cells)
        elif s.startswith(("-", "*", "•")):
            item = s[1:].strip()
        elif re.match(r"^\d+[.、)]\s*", s):
            item = re.sub(r"^\d+[.、)]\s*", "", s)
        # 无列表符号也不是表格的说明性段落 → 不是条目，跳过（含中间件写的 `> …` 核验行）
        if item:
            out.append(item)
        if len(out) >= CALIBER_MAX_ITEMS:
            break
    return out


_NEXT_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+\S", re.M)


def strip_caliber_block(text: str) -> str:
    """摘掉**最后一个**「业务口径」节（返回新文本；没有该节则原样返回）。

    为什么需要：报告 §1「数据结果」原样内嵌的是子 agent 的完整答复，而契约要求该答复
    **末尾**就附这个块（`parse_caliber_block` 的取块前提）⇒ §1 里已经出现一遍，§2 的
    渲染器又按同一批条目重新排版 ⇒ **同一张表在报告里出现两次**（2026-09-26 生产实证：
    trace `eaf1c8b2…` 的报告 §1 末尾与 §2 是同样的 11 行）。装配层去重比改契约稳：
    抽块仍从**原文**抽（`parse_caliber_block` 不动），只是不再把块内联进 §1。

    边界与 `parse_caliber_block` **同一套规则**：从标题行切到「下一个标题行」为止（块里
    夹的 `> …核验…` 行之类也一并带走）；块后面若还有正文，原样保留。只在接缝处补换行，
    不动文本内部（正文里的空行、代码围栏一律不碰）。
    """
    if not text:
        return ""
    s = str(text)
    hits = list(CALIBER_HEADING_RE.finditer(s))
    if not hits:
        return s
    start = hits[-1].start()
    nxt = _NEXT_HEADING_RE.search(s, hits[-1].end())
    end = nxt.start() if nxt else len(s)
    head = s[:start].rstrip("\n")
    tail = s[end:].lstrip("\n")
    if not head:
        return tail
    return (head + "\n\n" + tail) if tail.strip() else head + "\n"


def split_caliber_item(item) -> tuple[str, str, str] | None:
    """一行 → (口径项, 内容, 出处)；不足三段 / 有空字段 → None（调用方降级为列表项）。

    规则与 `report_builder._render_business_caliber` 原实现逐字一致（≥3 段且前三段
    非空，第 3 段起用空格接回）。核验与渲染共用本函数 ⇒ 不会出现「校验按 A 切、
    渲染按 B 切」。
    """
    s = str(item or "").strip().strip("|").strip()
    if not s:
        return None
    cells = [c.strip() for c in s.split("|")]
    if len(cells) >= 3 and all(cells[:3]):
        return cells[0], cells[1], " ".join(cells[2:])
    return None


# ── 归一化（双侧同一函数）────────────────────────────────────────────
_WS_RE = re.compile(r"\s+")
# 只去反引号/强调符，**不去下划线**：`work_hour`/`if_approve` 是标识符，
# 去掉下划线会造出假匹配（`workhour` 能匹配上 `work hour`）。
_EMPHASIS_RE = re.compile(r"[`*~]")


def normalize_for_match(s: str) -> str:
    """归一化用于逐字比对。

    规则与取舍（宁可误杀不可放过：误杀只多一次重写，放过就是报告撒谎）：

    - **NFKC**：全角 `＝（）` → 半角，否则模型写 `if_approve＝1` 对不上原文。
    - **casefold**：英文标识符大小写差异不算改写。
    - **还原 `\\|` → `|`**：`_cell()` 渲染时会转义竖线，核验侧先还原再比。
    - **去 `` ` `` `*` `~`**：markdown 强调符不构成内容差异。
    - **空白全部删除**（不是折叠成一个空格）：中英混排里空格有无本就随意
      （`if_approve = 1 的 work_hour` vs `if_approve=1的work_hour`），逐字核验
      对齐的是字符序列而非空格。
    - **不删标点、不删竖线**：公式里的 `=`/`+`/`>` 是语义；内容里出现竖线说明
      三字段被写歪了，让它对不上更安全。
    """
    t = unicodedata.normalize("NFKC", str(s or "")).casefold()
    t = t.replace("\\|", "|")
    t = _EMPHASIS_RE.sub("", t)
    return _WS_RE.sub("", t)


# ── 逐字比对 ────────────────────────────────────────────────────────
_ELLIPSIS_RE = re.compile(r"…+|\.{3,}|。{3,}")
_MIN_SEGMENT_CHARS = 8      # 带省略号时每个片段的**归一化**后最短长度
_MAX_SEGMENTS = 4           # 最多 4 段（即最多 3 个省略号）
_MAX_HIDDEN_RATIO = 1.5     # 省掉的字符数 ≤ 1.5 × 实际引到的字符数


def _match_content(q: str, files: list["EvidenceFile"]) -> tuple[bool, str, "EvidenceFile | None"]:
    """归一化后的内容 → (是否逐字命中, 机器码, 命中文件)。命中任一文件即通过。

    **已知并接受的放宽**：单片段（不带省略号）不做最短长度限制 —— `内容` 写 3 个字的
    `未审核`，只要该词确实逐字在原文里，就判通过。理由：那不是**假**（逐字为真），只是
    **弱**（不足以说明口径），而伪证才是本节要拦的东西；加长度门槛会把「口径本来就短」
    误打回，逼模型凑字，反而更糟。带省略号的写法另论（见下）：分片短到能碰巧命中就
    不再是证据，故那里有 `_MIN_SEGMENT_CHARS` 硬线。
    """
    if not q:
        return False, "content_empty", None
    segs = [s for s in _ELLIPSIS_RE.split(q) if s]
    if len(segs) == 1:
        for f in files:
            if q in f.norm_text:
                return True, "verbatim", f
        return False, "content_not_verbatim", None
    if len(segs) > _MAX_SEGMENTS:
        return False, "ellipsis_too_many", None
    if any(len(s) < _MIN_SEGMENT_CHARS for s in segs):
        # 「A…B」两个两字片段能拼出任何东西 ⇒ 带省略号时必须每段够长
        return False, "ellipsis_fragment_too_short", None
    got = sum(len(s) for s in segs)
    if len(q) - got > _MAX_HIDDEN_RATIO * got:
        # 专治「引 32 字、省 200 字」式假引用
        return False, "ellipsis_too_much_hidden", None
    for f in files:
        pos, hit = 0, True
        for s in segs:  # 同一文件内、按**顺序**、不重叠
            i = f.norm_text.find(s, pos)
            if i < 0:
                hit = False
                break
            pos = i + len(s)
        if hit:
            return True, "segmented", f
    return False, "segments_not_found", None


def _closest(q: str, files: list["EvidenceFile"]) -> str:
    """给纠正提示用的「最接近的原文片段」；找不到返回空串。失败一律返回空串。"""
    if not q:
        return ""
    try:
        from difflib import SequenceMatcher

        best_len, best = 0, ""
        for f in files:
            m = SequenceMatcher(None, q, f.norm_text, autojunk=False).find_longest_match()
            if m.size > best_len:
                best_len = m.size
                best = f.norm_text[m.b : m.b + min(m.size + 24, 120)]
            if best_len >= 24:
                break
        return best if best_len >= 6 else ""
    except Exception:  # noqa: BLE001  纯诊断信息，取不到就算了
        return ""


# ── 出处白名单 ──────────────────────────────────────────────────────
# 出处里出现的 `.md` 才算「文件」。`v_workhour` / `workhour_analysis（Cube）` /
# `语义库字段字典` 这类库对象一个 `.md` 都没有 ⇒ 直接判非文件。
#
# 字符类含 CJK 与空格，是为了容纳中文文件名（`rules/报工与工时.md`）与 `a.md R3`
# 这类条目号后缀（结尾必须是 `.md`，故 `R3` 会被回溯掉）。**副作用**：中文连接词会
# 把两个路径粘成一个 token（`rules/a.md 或 metrics/b.md` 整段算一个），解析即失败 ⇒
# fail-closed 打回。这个方向是对的：宁可让模型改成单一名，也不猜「它想引哪个文件」。
# 逗号/顿号分隔的两个真文件仍正常通过（见 `scripts/verify_caliber_evidence.py` t5）。
_SOURCE_MD_RE = re.compile(r"([\w一-鿿][\w一-鿿 ./\\-]*\.md)", re.I)


def _norm_rel(tok: str) -> str:
    """出处写法归一：去绝对路径前缀、`\\`→`/`、只留末两段。

    兼容 `knowledge/rules/x.md` / `rules/x.md` / `x.md` 三种写法。尾部条目号
    （`R3`/`第3条`）天然被正则挡在 `.md` 之外，不参与比对。
    """
    t = str(tok or "").replace("\\", "/").strip()
    t = re.sub(r"^.*?knowledge/", "", t)
    parts = [p for p in t.split("/") if p and p != "."]
    if not parts:
        return ""
    return "/".join(parts[-2:]) if len(parts) >= 2 else parts[0]


def _resolve_source(src: str, files: list["EvidenceFile"]) -> tuple[bool, str, list["EvidenceFile"]]:
    """出处 → (是否可信, 规范路径, 该出处指向的语料文件)。

    出处里出现的**每一个** `.md` 都必须命中磁盘白名单 —— 防「`rules/a.md` 或
    `metrics/b.md`」这种模糊写法蒙过。
    """
    toks = _SOURCE_MD_RE.findall(str(src or ""))
    if not toks:
        return False, "", []
    hit_files: list[EvidenceFile] = []
    rels: list[str] = []
    for t in toks:
        rel = _norm_rel(t)
        rels.append(rel)
        hit = next((f for f in files if f.rel_path == rel or f.name == rel), None)
        if hit is None:
            return False, rel, []
        hit_files.append(hit)
    return True, rels[0], hit_files


# ── 语料加载 ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EvidenceFile:
    """一份知识库文件（相对路径 + 原文本 + 归一化文本）。"""

    rel_path: str          # "rules/报工与工时.md"
    name: str              # "报工与工时.md"
    raw_text: str
    norm_text: str


@dataclass
class CaliberVerdict:
    """一条口径的核验结论。"""

    item: str
    ok: bool = False
    title: str = ""
    content: str = ""
    source: str = ""
    source_ok: bool = False     # 出处是否为知识库真实文件
    content_ok: bool = False    # 内容是否逐字命中（在**出处指向的**文件里）
    hit_file: str = ""          # 内容实际命中的文件（诊断用；出处错时能指出「其实在哪个文件」）
    reason: str = ""            # 机器码（content_* / source_* / item_*）
    reason_human: str = ""      # 给人/给模型看的诊断
    closest: str = ""           # 最接近的原文片段（仅未通过时可能非空）


_MAX_CHARS_PER_FILE = 200_000
_CORPUS_TTL = 60.0
_CORPUS_MAX_ENTRIES = 64
_CORPUS_CACHE: dict[str, tuple[float, list[EvidenceFile]]] = {}
_CORPUS_LOCK = threading.Lock()


def load_knowledge_corpus(db_name: str) -> list[EvidenceFile]:
    """库名 → 该库 wren 项目下 `knowledge/{glossary,metrics,rules,sql,caveats}/*.md` 全文。

    这就是「真实文件名」白名单的来源（动态来自磁盘，不硬编码）。任何一步失败返回
    `[]`（fail-open：调用方降级为「未核验」，绝不因为读不到语料就判模型不合规）。
    """
    db = str(db_name or "").strip()
    if not db:
        return []
    now = time.time()
    with _CORPUS_LOCK:
        cached = _CORPUS_CACHE.get(db)
        if cached and now - cached[0] < _CORPUS_TTL:
            return cached[1]
    files: list[EvidenceFile] = []
    try:
        from agent.utils.wren_call_extract import resolve_wren_ctx_by_db

        project, _ = resolve_wren_ctx_by_db(db)
        if project:
            base = Path(str(project)) / "knowledge"
            for sub in KNOWLEDGE_DIRS:
                d = base / sub
                if not d.is_dir():
                    continue
                for f in sorted(d.glob("*.md")):
                    try:
                        raw = f.read_text(encoding="utf-8").strip()
                    except Exception:  # noqa: BLE001  单文件读失败不影响其余
                        continue
                    if not raw:
                        continue
                    raw = raw[:_MAX_CHARS_PER_FILE]
                    files.append(
                        EvidenceFile(
                            rel_path=f"{sub}/{f.name}",
                            name=f.name,
                            raw_text=raw,
                            norm_text=normalize_for_match(raw),
                        )
                    )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[caliber] 知识库语料加载失败（降级为未核验）: %s", e)
        files = []
    with _CORPUS_LOCK:
        if len(_CORPUS_CACHE) > _CORPUS_MAX_ENTRIES:
            _CORPUS_CACHE.clear()
        _CORPUS_CACHE[db] = (now, files)
    return files


# ── 主入口 ──────────────────────────────────────────────────────────
def verify_caliber_entries(entries, corpus: list[EvidenceFile]) -> list[CaliberVerdict]:
    """逐条核验：出处是否为知识库真实文件 + 内容是否与**该文件**原文逐字一致。

    调用方**必须先判空语料**（空语料 = 无法核验，应降级为「未核验」脚注而不是判不合规）。
    这里对空语料仍返回 `ok=False, reason="corpus_empty"`，是防止调用方漏判后把「读不到
    语料」误当「合规」——方向是安全的（宁可标未通过，不可谎称通过）。
    """
    out: list[CaliberVerdict] = []
    for it in entries or []:
        raw = str(it or "").strip()
        if not raw:
            continue
        v = CaliberVerdict(item=raw)
        if not corpus:
            v.reason, v.reason_human = "corpus_empty", "读不到该库的知识库文件，无法核验"
            out.append(v)
            continue
        parts = split_caliber_item(raw)
        if parts is None:
            v.reason = "item_not_three_fields"
            v.reason_human = "条目不足三段，不是「口径项 | 内容 | 出处」形态，无法核验"
            out.append(v)
            continue
        v.title, v.content, v.source = parts
        v.source_ok, rel, hit_files = _resolve_source(v.source, corpus)
        q = normalize_for_match(v.content)
        # 出处可信 → 只在该出处指向的文件里找内容（这才验得出张冠李戴）；
        # 出处不可信 → 退到全语料找（只为给出「其实这段在哪个文件里」的诊断）
        scope = hit_files if (v.source_ok and hit_files) else corpus
        v.content_ok, c_reason, c_file = _match_content(q, scope)
        v.hit_file = c_file.rel_path if c_file else ""
        v.ok = bool(v.source_ok and v.content_ok)
        v.reason = c_reason
        if not v.source_ok:
            v.reason = "source_not_kb_file"
            v.reason_human = (
                f"出处「{v.source}」不是知识库文件名（应形如 `rules/xxx.md`；"
                "写库对象、表名、视图名（v_*）、Cube 名、字段字典都不算）"
            )
            if v.content_ok and v.hit_file:
                v.reason_human += f"；不过内容本身确实能在 `{v.hit_file}` 里找到"
                v.closest = v.hit_file
        elif not v.content_ok:
            v.reason_human = {
                "content_not_verbatim": f"内容在 `{rel}` 里找不到逐字片段（被改写/概括过）",
                "ellipsis_fragment_too_short": "省略号切出的片段太短（每段至少 8 字）",
                "ellipsis_too_many": "省略号太多（最多 3 个）",
                "ellipsis_too_much_hidden": "省略掉的字数多于引到的字数",
                "segments_not_found": f"省略号各段没能在 `{rel}` 里按顺序找到",
                "content_empty": "内容为空",
            }.get(c_reason, f"内容与 `{rel}` 不一致（{c_reason}）")
            # closest **只**进 verdict / 日志，**不**拼进 reason_human —— reason_human
            # 会被原样贴进给模型的纠正消息，而把知识库原文贴过去等于往模型上下文里
            # 注入它本轮未必取过的料，反而破坏「只能引本次取到的原文」这条语义。
            v.closest = _closest(q, scope)
        out.append(v)
    return out
