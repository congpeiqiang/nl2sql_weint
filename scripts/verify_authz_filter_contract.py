# -*- coding: utf-8 -*-
"""P1-13 授权过滤器「操作符契约」（离线，无需后端 / 数据库 / 网络 / LLM）。

**被测的坑**：`@auth.on` 钩子返回的过滤器是越权的最后一道闸，而 LangGraph 的 inmem
运行时 `_check_filter_match` **对未知操作符静默忽略**：

    {"owner": {"$ne": "bob"}}   ← 看起来是"排除 bob"，实际**不产生任何约束**
                                → 返回 True = 放行（不是拒绝、也不是报错）

写错一个操作符，"不是自己的会话取不到"就变成"谁都取得到"，且**全链路无任何报错**。
本脚本把三件事钉死：

  ① **运行期真相**：拿**当前安装的** matcher 实测 —— 哪些操作符真生效（`$eq`/`$contains`/
     `$or`/`$and`/简写等值）、哪些**静默放行**（dict 值里的未知操作符）、哪些**直接 500**
     （`$or` 元素少于 2 个）。这三条是"我们只能写 `$eq`/`$or`"的理由，不是风格偏好。
  ② **我们只写了允许的那几个**：AST 扫 `src/` 全部 `.py`，把每个 `$` 开头的字符串字面量
     逐个对白名单。将来谁写了个 `$ne`/`$in`，这个脚本立刻红（而不是等生产上越权）。
  ③ **语义矩阵**：把 `owner_filter()` 喂进**真** matcher，断言可见性完全符合预期 ——
     自己 + `legacy` 可见；别人的**以及没有 owner 键的**都不可见（后者正是 P1-5 必须配
     回填脚本的原因）；admin / 内部调用**不返回过滤器**（而不是返回一个"全放行"的过滤器）。
  ④ **`legacy` 只有回填这一个来源**：`is_real_owner("legacy")` 必须为假（不可冒名），
     且全仓 `"legacy"` 字面量只出现在 `auth/ownership.py` 与回填脚本里。

⚠️ **必须在项目 `.venv` 里跑**（`langgraph-runtime-inmem` 0.31.1，与生产同版本）：
       ./.venv/Scripts/python.exe scripts/verify_authz_filter_contract.py     # Windows
       uv run python scripts/verify_authz_filter_contract.py                   # 通用
   换用别处的 python（如系统 site-packages 的 0.14.x）会因**运行时不支持 `$or`** 而误报：
   那种版本下我们的过滤器会让**所有**会话对非管理员不可见（另外一套故障，不是越权）。
   脚本 ⓪ 会先自证运行版本，不符时直接打印上面这行命令。

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import inspect
import os
import pathlib
import re
import sys
import tempfile

os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-filter-"))

# 本脚本带中文与 ⓪①② 这类非 GBK 字符；Windows 控制台默认 GBK 会在 print 时
# UnicodeEncodeError 炸掉（不是断言失败，是整个脚本崩）。这里强制 utf-8 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
for cand in (_ROOT / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

# 我们允许出现在过滤器里的操作符（唯一真源：`auth/ownership.py` 的 owner_filter 只用 $or）。
ALLOWED_OPS = {"$or"}
# matcher 真正**生效**的操作符（② 的判据要区分"生效"与"被忽略"，见 ①）
EFFECTIVE_OPS = {"$or", "$and", "$eq", "$contains"}
# 不再允许任何"无法解析所以跳过"的文件：历史上放行过两个垃圾副本
# （`src/agent/workspace-temp/tmp/extract_svg.py` 与 `src/agent/skills/` 死副本），
# 两者都已在 2026-09-25 单工作区改造中删除 ⇒ 期望值收紧为**零个**（见 ② 的断言）。
# 别再加白名单：那正是"扫不到 = 放行"的漏洞形态。


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def matcher():
    """当前安装版本的过滤器匹配函数（就是生产判权时走的那个）。"""
    from langgraph_runtime_inmem.ops import _check_filter_match

    return _check_filter_match


# ── ⓪ 版本自证 ──────────────────────────────────────────────

def t0_runtime_version() -> bool:
    section("⓪ 运行版本自证（必须在项目 .venv 里跑）")
    from importlib.metadata import version

    src = inspect.getsource(matcher())
    supports_or = '"$or" in filters' in src
    check(supports_or,
          "当前 runtime 的 matcher 支持 `$or`（= 我们的过滤器在这个版本下有意义）",
          f"langgraph-runtime-inmem={version('langgraph-runtime-inmem')}")
    if not supports_or:
        print("\n  ⚠️ 这个 python 不是项目 .venv（版本过旧，matcher 没有 $or 分支）。")
        print("     请改用：./.venv/Scripts/python.exe scripts/verify_authz_filter_contract.py\n")
    return supports_or


# ── ① 运行期真相 ────────────────────────────────────────────

def t1_matcher_truth() -> None:
    section("① 运行期真相：哪些操作符真生效 / 静默放行 / 直接 500")
    m = matcher()
    bob = {"owner": "bob"}
    alice = {"owner": "alice"}

    check(m(alice, {"owner": {"$eq": "alice"}}) is True
          and m(bob, {"owner": {"$eq": "alice"}}) is False,
          "`$eq`（dict 形态）生效")
    check(m(alice, {"owner": "alice"}) is True and m(bob, {"owner": "alice"}) is False,
          "简写等值（直接字符串）生效")
    check(m({"tags": ["a", "b"]}, {"tags": {"$contains": "a"}}) is True
          and m({"tags": ["a"]}, {"tags": {"$contains": "b"}}) is False,
          "`$contains` 生效（值必须是 list，否则 False）")
    check(m(alice, {"$or": [{"owner": "alice"}, {"owner": "legacy"}]}) is True
          and m(bob, {"$or": [{"owner": "alice"}, {"owner": "legacy"}]}) is False,
          "`$or` 生效（我们的 owner_filter 用的就是它）")
    check(m(bob, {"$and": [{"owner": "bob"}, {"owner": {"$eq": "bob"}}]}) is True,
          "`$and` 生效（列出来，虽然我们不用）")

    # —— 危险面：未知操作符**静默忽略 = 放行** ——
    leaked = [
        op for op in ("$ne", "$in", "$nin", "$gt", "$lt", "$exists", "$regex")
        if m(bob, {"owner": {op: "alice"}}) is True
    ]
    check(len(leaked) == 7,
          "**未知操作符在 dict 值里被静默忽略 → 返回 True（放行）** ← 本项存在的理由",
          f"放行的操作符: {leaked}")
    check(m(bob, {"owner": {"$ne": "bob"}}) is True,
          "最典型的误写：`{\"owner\": {\"$ne\": \"bob\"}}` 对 bob 自己的会话**判为可见**")
    check(m(bob, {"owner": {"$eq": "bob", "$ne": "bob"}}) is True,
          "同一 dict 里第二个操作符被丢弃（`next(iter(value))` 只取第一个）→ 多写不报错只是无效")

    # —— 顶层未知操作符（值不是 dict）走"直接等值" → 键不存在 → **全拒** ——
    check(m(bob, {"$ne": "bob"}) is False,
          "顶层未知操作符 → 直接等值匹配一个不存在的键 → **全拒**（另一种故障，非越权）")

    # —— `$or` 元素少于 2：运行时直接 500（不是"当 1 个用"）——
    from starlette.exceptions import HTTPException

    raised = False
    try:
        m(bob, {"$or": [{"owner": "bob"}]})
    except HTTPException as e:
        raised = getattr(e, "status_code", 0) == 500
    check(raised, "`$or` 只有 1 个元素 → HTTPException 500（端点是 500 不是 403）")


# ── ② 我们的过滤器只用了白名单里的操作符 ─────────────────────

# 判据必须**落在过滤器真实的形状上**，否则两头都是假的：
#   · 只看"以 $ 开头的字面量" → 被**分隔符**误伤（`pbkdf2_sha256$iter$salt$hash` 的裸
#     "$"，P1-12 的 users.py），逼着后人放宽白名单 —— 白名单一松，`$ne` 就进来了；
#   · `findall` 全串扫 → 被**散文**误伤（文档字符串里举的 `{"$ne": ...}` 例子）
#     与**无关的 shell 变量**误伤（`"$PATH"`）。
# 真形状 = **字典字面量的键**（过滤器就是 `{键: 条件}`），且过滤操作符全是**小写开头**
# （`$PATH` 那种 shell 变量大写开头）。两条一起卡：既不误伤，也不放过 `$ne`/`$in`。
_OP_KEY_RE = re.compile(r"^\$[a-z][a-z0-9_]*$")


def scan_operator_keys(root: pathlib.Path, exts=("*.py",)) -> tuple[dict[str, list[str]], int, list[str]]:
    """扫 root 下的字典字面量键 → (操作符形态的键 → 位置, 字符串键总数, 无法解析的文件)。

    单独抽成函数是为了能对**构造出来的样本**自测（见 ⑤ 的扫描器自检）——
    一个扫不出违规的扫描器等于没有。
    """
    found: dict[str, list[str]] = {}
    dict_keys_seen = 0
    unparsed: list[str] = []
    for path in sorted(p for ext in exts for p in root.rglob(ext)):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            unparsed.append(rel)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    dict_keys_seen += 1
                    if _OP_KEY_RE.match(key.value):
                        found.setdefault(key.value, []).append(f"{rel}:{key.lineno}")
    return found, dict_keys_seen, unparsed


def t2_ast_whitelist() -> None:
    section("② AST 扫描：全仓只允许写白名单里的操作符")
    # 扫描路径以 `_ROOT` 为基准 → 报出来的位置带 `src/` 前缀，便于直接点开
    src_root = _ROOT / "src"
    raw_found, dict_keys_seen, all_unparsed = scan_operator_keys(src_root)
    found = {k: [f"src/{p}" for p in v] for k, v in raw_found.items()}
    for rel in all_unparsed:
        check(False, f"无法解析 {rel}（语法错误）—— 扫不到就等于没扫")

    check(dict_keys_seen > 0, "扫描确实读到了字典字面量（不是空跑一遍就宣称通过）",
          f"共 {dict_keys_seen} 个字符串键")
    extra = {k: v for k, v in found.items() if k not in ALLOWED_OPS}
    check(not extra, f"过滤器操作符形态的字典键只有白名单 {sorted(ALLOWED_OPS)}",
          f"用到的: {sorted(found)}" + (f" 越界的: {extra}" if extra else ""))
    check(found.get("$or") == ["src/agent/auth/ownership.py:36"],
          "`$or` 只出现在 owner_filter 一处（唯一构造点）",
          str(found.get("$or")))
    # 把"扫不到"显式化：一个都不许逃过扫描（历史那两个垃圾副本已删，白名单清零）。
    check(all_unparsed == [],
          "没有任何文件逃过扫描（全部 .py 都能解析）", str(all_unparsed))

    # 构造合法性：`$or` 的值必须是 list 且 ≥2（少了运行时 500，见 ①）
    from agent.auth.ownership import owner_filter

    f = owner_filter("alice")
    check(list(f.keys()) == ["$or"], "owner_filter 只返回 `$or` 这一个键", str(f))
    check(isinstance(f["$or"], list) and len(f["$or"]) == 2,
          "`$or` 的值是长度 2 的 list（≥2，否则运行时 500）", str(f["$or"]))
    check(all(isinstance(g, dict) for g in f["$or"]), "每个分组都是 dict", str(f["$or"]))

    # 生效操作符与白名单的关系（写下来免得下次有人"顺手加个 $ne"）
    check(ALLOWED_OPS <= EFFECTIVE_OPS,
          "白名单 ⊆ matcher 真正生效的操作符集合",
          f"allowed={sorted(ALLOWED_OPS)} effective={sorted(EFFECTIVE_OPS)}")


# ── ③ 语义矩阵（真 matcher + 真 owner_filter）────────────────

def t3_semantics() -> None:
    section("③ 语义矩阵：owner_filter 喂进真 matcher")
    from agent.auth.ownership import LEGACY_OWNER, owner_filter

    m = matcher()
    f_alice = owner_filter("alice")
    rows = [
        ({"owner": "alice"}, True, "自己的会话"),
        ({"owner": "bob"}, False, "别人的会话"),
        ({"owner": LEGACY_OWNER}, True, "legacy 存量（对所有人可见，与 owned_thread 口径对齐）"),
        ({}, False, "**没有 owner 键** → 谁都看不到（P1-5 必须配回填的原因）"),
        ({"owner": "alice", "db_name": "x"}, True, "多余键不影响（隐式 AND 只判给定的键）"),
        ({"owner": ""}, False, "空归属（写坏的值）不可见"),
    ]
    for metadata, expected, label in rows:
        got = m(metadata, f_alice)
        check(got is expected, f"alice 视角：{label} → {expected}", f"got={got}")

    # 反向：bob 的过滤器看不到 alice 的
    check(m({"owner": "alice"}, owner_filter("bob")) is False,
          "bob 的过滤器看不到 alice 的会话（对称）")

    # admin / 内部：**不返回过滤器**（而不是返回全放行过滤器）
    import asyncio

    from agent.auth.backend import _guard_thread_access
    from agent.auth.ownership import INTERNAL_IDENTITY

    class _Ctx:
        def __init__(self, ident, perms):
            self.user = type("U", (), {"identity": ident})()
            self.permissions = perms

    check(asyncio.run(_guard_thread_access(_Ctx("admin", ("admin",)), {})) is None,
          "admin → 钩子返回 None（不加约束；不是返回一个放行过滤器）")
    check(asyncio.run(_guard_thread_access(_Ctx(INTERNAL_IDENTITY, ()), {})) is None,
          "内部调用（internal）→ 返回 None（子 agent / sync 自己带父 run 的身份）")
    filt = asyncio.run(_guard_thread_access(_Ctx("alice", ()), {}))
    check(filt == owner_filter("alice"), "普通用户 → 返回 owner_filter(自己)", str(filt))


# ── ④ legacy 哨兵只有一个来源 ───────────────────────────────

def t4_legacy_writers() -> None:
    section("④ `legacy` 哨兵：不可冒名 + 运行期只有一个写者（回填）")
    from agent.auth.ownership import LEGACY_OWNER, owner_of, is_real_owner

    check(is_real_owner(LEGACY_OWNER) is False,
          "`is_real_owner('legacy')` 为假 → 任何人不能把自己写成 legacy 混进公共列表")
    check(is_real_owner("internal") is False and is_real_owner("dev") is False
          and is_real_owner("") is False and is_real_owner(None) is False,
          "internal / dev / 空 / None 都不是真实归属人")
    check(is_real_owner("alice") is True, "普通用户 id 是真实归属人")
    check(owner_of({"owner": "legacy"}) == LEGACY_OWNER
          and owner_of({}) == "" and owner_of(None) == "",
          "owner_of 读得出 legacy，缺键返回空串")

    # 运行期写者清点：只看 `src/`（**跑在服务里的代码**）。scripts/ 下的测试脚本
    # 当然要引用这个哨兵来断言行为，把它们算进来就变成自我指涉的假红线。
    # 判据：运行期没有任何一处**自己写** legacy 归属 —— 只有 ownership.py 定义它、
    # 回填脚本（人工执行）浇灌存量。否则"任何人把自己写成 legacy 就能进公共列表"。
    hits: list[str] = []
    for path in sorted((_ROOT / "src").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if "__pycache__" in rel or "workspace-temp" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value == "legacy"
            ):
                hits.append(rel)
    check(set(hits) == {"src/agent/auth/ownership.py"},
          "`src/` 下只有 ownership.py 提到 `legacy`（运行期没有第二个写者 / 没有自封归属）",
          str(sorted(set(hits))))


# ── ⑤ 破坏性负对照 ──────────────────────────────────────────

def t5_negative_control() -> None:
    section("⑤ 负对照（破坏性）：改用未知操作符 → 别人的会话对 alice 可见")
    import agent.auth.ownership as ow

    m = matcher()
    original = ow.owner_filter
    # 复刻"有人顺手写了个 $ne"（语义看似正确：排除自己的？不，这里模拟"排除某人"的写法）
    ow.owner_filter = lambda identity: {"owner": {"$ne": identity}}  # type: ignore[assignment]
    try:
        bad = ow.owner_filter("alice")
        leaked = m({"owner": "bob"}, bad)
        check(leaked is True,
              "**负对照**：未知操作符写法下 → alice 的过滤器对 bob 的会话判为**可见**",
              f"filter={bad} match={leaked}")
        check(m({"owner": "alice"}, ow.owner_filter("alice")) is True,
              "同一写法对自己也可见 → 说明它根本没在做过滤（不是判反了）")
    finally:
        ow.owner_filter = original  # type: ignore[assignment]

    # 扫描器自检：拿构造出来的样本喂给 ② 用的**同一个函数**，它必须能红
    with tempfile.TemporaryDirectory(prefix="nl2sql-opscan-") as td:
        tmp = pathlib.Path(td)
        (tmp / "bad.py").write_text(
            'def f(identity):\n'
            '    """散文里提一句 {"$ne": "x"} 不该被算作违规。"""\n'
            '    env = {"$PATH": "/usr/bin"}\n'          # shell 变量（大写）不算操作符
            '    return {"owner": {"$ne": identity}}\n',  # 真违规
            encoding="utf-8",
        )
        found, seen, _ = scan_operator_keys(tmp)
        check(set(found) == {"$ne"},
              "**扫描器自检**：样本里的 `$ne` 被抓到，而散文里的 `$ne` 与大写的 `$PATH` 都**没有**误报",
              f"found={sorted(found)} seen={seen}")


# ── ⑥ legacy 存量核对口径（回填脚本的分类逻辑）─────────────

def t6_backfill_plan() -> None:
    """P1-13 的另一半：**存量怎么核**。

    实盘核对要在目标环境跑 `scripts/backfill_thread_owner.py`（dry-run，只读不改），
    但那依赖一个正在跑的服务（它经 HTTP 列线程）。这里把它**唯一需要判对的东西**
    —— `plan()` 的分桶——离线钉死：回填脚本判错一个桶，实盘数字就是错的，
    而错的方式是静默的（比如把 orphan 当成 legacy 浇灌出去 = 全员可见）。
    """
    section("⑥ legacy 存量核对口径（回填脚本 plan() 的分桶）")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_backfill_thread_owner", _ROOT / "scripts" / "backfill_thread_owner.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    threads = [
        {"thread_id": "t-alice", "metadata": {"owner": "alice"}},
        {"thread_id": "t-legacy", "metadata": {"owner": "legacy"}},
        {"thread_id": "t-empty", "metadata": {"owner": ""}},
        {"thread_id": "t-missing", "metadata": {}},
        {"thread_id": "t-none", "metadata": {"owner": None}},
        {"thread_id": "t-internal", "metadata": {"owner": "internal"}},
        {"thread_id": "t-dev", "metadata": {"owner": "dev"}},
        {"thread_id": "t-already", "metadata": {"owner": "bob"}},
        {"thread_id": "t-conflict", "metadata": {"owner": "bob"}},
        {"metadata": {"owner": "alice"}},                          # 没有 thread_id → 丢弃
    ]
    existing = {"t-already": "bob", "t-conflict": "carol"}
    b = mod.plan(threads, existing)

    check(sorted(t for t, _ in b["claim"]) == ["t-alice"],
          "真实 owner → claim 桶（写真实 user_id 的归属行）", str(b["claim"]))
    check([t for t, _ in b["legacy"]] == ["t-legacy"] and b["legacy"][0][1] == "legacy",
          "`metadata.owner == legacy` → legacy 桶（浇哨兵，保持全员可见）", str(b["legacy"]))
    check(sorted(b["orphan"]) == ["t-dev", "t-empty", "t-internal", "t-missing", "t-none"],
          "缺失 / 空 / None / internal / dev → orphan 桶（**不写**，两套账本都看不见）",
          str(sorted(b["orphan"])))
    check(b["ok"] == [("t-already", "bob")], "账本与 metadata 一致 → ok（不动）", str(b["ok"]))
    check(b["conflict"] == [("t-conflict", "carol", "bob")],
          "账本≠metadata → conflict（列出人工判断，**不改写**：改写可能夺走别人的会话）",
          str(b["conflict"]))
    check(sum(len(v) for v in b.values()) == 9,
          "10 条输入里 9 条分类、没有 thread_id 的那条被丢弃（不重复计数）",
          f"共 {sum(len(v) for v in b.values())} 条")

    # 回填只写两个桶，且 legacy 是唯一的"多用户可见"来源
    writes = b["claim"] + b["legacy"]
    check(all(uid == "legacy" for _, uid in b["legacy"]) and len(b["legacy"]) <= 1,
          "legacy 桶里每个条目的归属都是哨兵本身（没有借 legacy 之名夹带真实用户）")
    check({uid for _, uid in writes} == {"alice", "legacy"},
          "写入集合 = {真实用户, legacy}（orphan 与 conflict 一律不写）", str(sorted(writes)))

    # 与运行期口径一致：`plan` 的 NON_USER 必须等于 ownership.NON_USER_IDENTITIES
    from agent.auth.ownership import LEGACY_OWNER, NON_USER_IDENTITIES

    # 两份账目必须同口径，但**方向有讲究**：脚本少认一个非用户身份 = 把 internal/dev
    # 之类当成真人写进归属行（凭空造出一个能登录同名的归属）；多认一个只是更保守。
    # 实测脚本比运行期多一个 `""` —— 不可达（`elif owner and ...` 已先把它挡了），放行。
    missing = set(NON_USER_IDENTITIES) - set(mod.NON_USER)
    extra = set(mod.NON_USER) - set(NON_USER_IDENTITIES)
    check(not missing,
          "回填脚本认全了运行期的全部非用户身份（少认一个就会把非用户写成真实归属）",
          f"missing={sorted(missing)}")
    check(extra <= {""},
          "脚本只允许比运行期多认一个空串（不可达的冗余，不是漏判）",
          f"extra={sorted(extra)}")
    check(mod.LEGACY == LEGACY_OWNER,
          "legacy 哨兵字面量两份账目一致（写歪了就成了另一种可见性）",
          f"script={mod.LEGACY} runtime={LEGACY_OWNER}")
    check(isinstance(b["orphan"], list) and b["orphan"],
          "orphan 会被**报告**出来（默认不删）—— 存量核对的输出就是这个桶")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    if t0_runtime_version():
        t1_matcher_truth()
        t2_ast_whitelist()
        t3_semantics()
        t4_legacy_writers()
        t6_backfill_plan()
        t5_negative_control()
    else:
        check(False, "运行版本不支持 `$or`，后续断言无意义（换 .venv 跑）")

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
