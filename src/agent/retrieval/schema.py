"""检索索引的骨架：开关、后端选择、落点、条目形状 —— **不含任何 LanceDB 调用**。

## 为什么单独一层

索引是**派生物**（真相源永远是语义库），所以「开关叫什么、用哪个后端、索引放哪、
按什么键失效」这四件事必须全模块唯一一份，不能散到 indexer / search 里各写一遍。

本模块**零第三方依赖、不 import lancedb** —— lancedb 的 import 只许发生在
`backends/lance.py` 内部且是惰性的（方案 R10）：生产镜像在重建之前没有这个包，
import 期炸掉会连带毁掉整个 agent。

## 契约

- **开关**：`NL2SQL_RETRIEVAL` ∈ {`off`(默认) / `fts` / `hybrid`}。默认 off ⇒ 全链与
  今天**逐字一致**。`hybrid` 在运行期嵌入不可用时自动降级成 `fts`（方案 §8.1 的 L1）。
- **后端**：`NL2SQL_RETRIEVAL_BACKEND` ∈ {`lance`(默认) / `jsonl`}。`lance` 装不出来时
  由 `backends.backend_name()` **fail-open 退回** `jsonl`（读侧降级方向 = 今天的全量注入）。
- **落点**：`<workspace>/.retrieval/<项目目录名>/`，**绝不放 wren 项目目录内**。三条
  既有机制都会咬：
  1. 语义库是 **git 版本化的**且会自动提交推送（`语义库更新 <库名> <ts>`）⇒ 索引会被
     commit + push 进 GitLab；
  2. `wren_semantic._local_content_state` 判「有无自建内容」的口径是「**根目录除
     `wren_project.yml` 之外的任何条目都算内容（未知文件也算）**」⇒ 索引会让「接入 Git
     接管」的 pristine 判断失真（多弹确认、甚至被当自建内容备份掉）；
  3. `db_config._scan_wren_projects` 按「一级子目录里有没有 `wren_project.yml`」列语义库
     ⇒ 隐藏目录 `.retrieval` 不会被当成一个库。
- **失效键**：`rev = sha256(target/mdl.json)[:16]`。rev 变了就重建（幂等）。
- **输入只许来自注册表**：`registered_projects()` 读 `db_config.json` 的
  `databases[].wren_project`。**扫目录会把 `<name>.备份-*` 当活库**（生产上就有一个
  151 models 的备份目录躺在同级）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

# ── 开关 ────────────────────────────────────────────────
ENV_SWITCH = "NL2SQL_RETRIEVAL"
MODE_OFF = "off"       # 默认：全链与今天逐字一致
MODE_FTS = "fts"       # 只 FTS 腿（不需要嵌入通道）
MODE_HYBRID = "hybrid"  # FTS + 向量 + RRF（缺嵌入则运行期降级为 fts）
_MODES = (MODE_OFF, MODE_FTS, MODE_HYBRID)

# ── 后端 ────────────────────────────────────────────────
#：`lance` 是默认（2026-09-28 用户拍板：接受改 Dockerfile + 重建镜像）。
#: 选它的理由是**内存与预建索引**，不是 FTS 延迟 —— 实测 5000 条中文语料：
#: 自写腿 18.4ms / gram 索引 37MB / 向量(Python list) 156MB；lance on-disk mmap 不物化。
#: 但入口/返回形状/融合全部与后端无关，`jsonl` 保留为 lancedb 缺席时的降级路径。
ENV_BACKEND = "NL2SQL_RETRIEVAL_BACKEND"
BACKEND_LANCE = "lance"
BACKEND_JSONL = "jsonl"
_BACKENDS = (BACKEND_LANCE, BACKEND_JSONL)

# ── 落点 ────────────────────────────────────────────────
INDEX_DIR_NAME = ".retrieval"   # 工作区下的隐藏目录，见模块 docstring
META_FILENAME = "index.json"    # 元数据：不装 lancedb 也能读
TABLE_NAME = "items"            # lance 的表名
TABLE_DIR_SUFFIX = ".lance"     # lancedb 按表名生成目录：表名 items ⇒ `<index_dir>/items.lance/`

# ── 条目类型（kind → 来源见方案 §5.3）────────────────────
KIND_SCHEMA_TABLE = "schema_table"
KIND_SCHEMA_COLUMN = "schema_column"
KIND_RELATIONSHIP = "relationship"
KIND_VIEW = "view"
KIND_CUBE = "cube"
KIND_MEASURE = "measure"
KIND_DIMENSION = "dimension"
KIND_KNOWLEDGE_RULE = "knowledge_rule"
KIND_KNOWLEDGE_GLOSSARY = "knowledge_glossary"
KIND_KNOWLEDGE_METRIC = "knowledge_metric"
KIND_KNOWLEDGE_CAVEAT = "knowledge_caveat"
KIND_EXAMPLE_SQL = "example_sql"

ALL_KINDS = (
    KIND_SCHEMA_TABLE,
    KIND_SCHEMA_COLUMN,
    KIND_RELATIONSHIP,
    KIND_VIEW,
    KIND_CUBE,
    KIND_MEASURE,
    KIND_DIMENSION,
    KIND_KNOWLEDGE_RULE,
    KIND_KNOWLEDGE_GLOSSARY,
    KIND_KNOWLEDGE_METRIC,
    KIND_KNOWLEDGE_CAVEAT,
    KIND_EXAMPLE_SQL,
)

# knowledge/ 子目录名 → kind（其余知识文件按规则类归）
_KNOWLEDGE_KIND = {
    "rules": KIND_KNOWLEDGE_RULE,
    "glossary": KIND_KNOWLEDGE_GLOSSARY,
    "metrics": KIND_KNOWLEDGE_METRIC,
    "caveats": KIND_KNOWLEDGE_CAVEAT,
}


def knowledge_kind(subdir: str) -> str:
    """knowledge/ 下的一级子目录名 → 条目 kind（认不出按规则类归）。"""
    return _KNOWLEDGE_KIND.get((subdir or "").strip().lower(), KIND_KNOWLEDGE_RULE)


def retrieval_mode() -> str:
    """当前检索模式。未配置/无法识别一律 `off`（默认关 ＝ 今天的行为）。"""
    raw = os.environ.get(ENV_SWITCH, "").strip().lower()
    return raw if raw in _MODES else MODE_OFF


def is_enabled() -> bool:
    """检索薄层是否启用（fts / hybrid 都算启用）。"""
    return retrieval_mode() != MODE_OFF


def vector_enabled() -> bool:
    """是否要用向量腿（仅 hybrid）。运行期嵌入失败时由 search 侧降到纯 FTS。"""
    return retrieval_mode() == MODE_HYBRID


def retrieval_backend() -> str:
    """后端名。未配置/无法识别按 `lance`（默认）；装不出来由 backends 层退回 jsonl。"""
    raw = os.environ.get(ENV_BACKEND, "").strip().lower()
    return raw if raw in _BACKENDS else BACKEND_LANCE


# ── 条目 ────────────────────────────────────────────────
@dataclass
class Item:
    """一条可检索条目。

    `vector` 允许为空 —— 嵌入不可用时照样能建出**纯 FTS 索引**（方案 §8.1 的构建期
    要求），向量稍后 backfill。`meta` 统一序列化成 JSON 字符串落库，避免 LanceDB
    对嵌套 dict 的 schema 推断问题。
    """

    id: str
    kind: str
    text: str
    title: str = ""
    db_name: str = ""
    src_path: str = ""
    rev: str = ""
    meta: dict = field(default_factory=dict)
    vector: list[float] | None = None

    def row(self) -> dict:
        """落库用的扁平行（`meta` → `meta_json`）。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "db_name": self.db_name,
            "src_path": self.src_path,
            "title": self.title,
            "text": self.text,
            "meta_json": json.dumps(self.meta, ensure_ascii=False),
            "rev": self.rev,
            "vector": self.vector,
        }


def item_id(db_name: str, kind: str, src_path: str) -> str:
    """稳定 id（重建幂等：同一条语料永远同一个 id）。"""
    raw = f"{db_name}\x00{kind}\x00{src_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ── 路径解析 ────────────────────────────────────────────
def workspace_root() -> Path:
    """工作区根（`<AGENT_DATA_ROOT>/workspace`，单值钉死）。"""
    try:
        from agent.workspace_manager import get_workspace_manager

        return get_workspace_manager().active_workspace
    except Exception as e:  # noqa: BLE001 —— 解析不出来时退回仓库内默认，绝不让检索拖垮调用方
        _log.debug("[retrieval] WorkspaceManager 不可用，回退默认工作区: %s", e)
        root = os.environ.get("AGENT_DATA_ROOT", "").strip()
        if root:
            return Path(root) / "workspace"
        return Path(__file__).resolve().parents[1] / "workspace"


def index_dir_for(project: Path) -> Path:
    """某 wren 项目的索引目录（工作区下、按项目目录名分桶）。"""
    return workspace_root() / INDEX_DIR_NAME / Path(project).name


def table_dir(index_dir: Path) -> Path:
    """lance 后端的数据集目录（`<index_dir>/items.lance/`）。

    **单点定义**：目录名由 lancedb 按表名生成（表名 + `.lance`），两侧（写侧的 ready
    判定、读侧的版本提示）自己拼一遍就会拼错 —— 实施期就这么错过一次
    （写成 `<index_dir>/items`，于是 `ready()` 恒 False、`_hint()` 恒空）。
    """
    return Path(index_dir) / (TABLE_NAME + TABLE_DIR_SUFFIX)


def project_rev(project: Path) -> str:
    """索引失效键：`target/mdl.json` 的内容哈希（读不到返回空串 ＝ 不可索引）。"""
    mdl = Path(project) / "target" / "mdl.json"
    try:
        return hashlib.sha256(mdl.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def registered_projects() -> list[dict]:
    """注册表里的活跃库：[{db_name, project, project_name}]。

    **索引输入只许来自这里**（方案 R8）：工作区里躺着 `<name>.备份-<ts>`，
    扫目录会把备份当活库建索引。失败一律返回空表（fail-open：没索引 ⇒ 回落全量注入）。
    """
    try:
        from agent.workspace_manager import get_workspace_manager

        cfg_path = get_workspace_manager().db_config_path
        data = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] 读注册表失败，视为没有可索引的库: %s", e)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for cfg in data.get("databases") or []:
        if not isinstance(cfg, dict):
            continue
        wp = str(cfg.get("wren_project") or "").strip()
        if not wp:
            continue
        project = Path(wp)
        if not project.is_dir() or str(project) in seen:
            continue
        seen.add(str(project))
        out.append(
            {
                "db_name": str(cfg.get("name") or ""),
                "project": project,
                "project_name": project.name,
            }
        )
    return out


# ── 元数据 ──────────────────────────────────────────────
def meta_path(index_dir: Path) -> Path:
    return Path(index_dir) / META_FILENAME


def read_meta(index_dir: Path) -> dict:
    """读索引元数据（读不到返回空 dict）。"""
    try:
        return json.loads(meta_path(index_dir).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_meta(index_dir: Path, meta: dict) -> None:
    """写索引元数据（原子替换：先写临时文件再 rename）。"""
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    tmp = meta_path(index_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(meta_path(index_dir))
