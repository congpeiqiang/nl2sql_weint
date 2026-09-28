"""Wren 语义库管理 API 路由（随 langgraph API 同进程/同端口提供）。

对标 `docs/wren-semantic-library-management.md` 设计稿，提供语义库（Wren 项目
目录）的管理：列表（含 git 元信息 / 构建状态 / 关联库 / MDL 概览计数）、关联本地
目录、从 git 拉取、删除、重新构建、校验、MDL 概览。

「语义库」本身没有独立实体，本质是磁盘上的 Wren 项目目录，靠 `db_config.json`
里某条库的 `wren_project` 路径字段与数据库配置关联。

热加载（2026-09-19，**重启门槛已消除**）：新增/删除/重新关联语义库后，Wren MCP
工具（`wrenai_*`）**无需重启后端**即可生效——`_invalidate_detector()` 同时失效
`SemanticDbDetector` 缓存并触发 `mcp_tool` 运行期工具注册表的后台对账；关联某个
库的接口再补一次同步加载，把「加载了几个工具 / 失败原因」当场返回。语义库**内容**
更新（git pull + context build）一直不需要重启：每次工具调用新起的 MCP 子进程会
重新读 `target/mdl.json`。响应里的 `requires_restart` 恒为 `false`（保留字段兼容
老前端），详情见 `mcp` 字段。原理见 `agent/middlewares/dynamic_mcp_tools.py`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from agent.utils.offload import offload, offload_long
from api._common import json_response, parse_body, require_user, require_admin
from api.db_config import _ensure_mcp_tools, _scan_wren_projects
from mcp_server.db_mcp_server.db.core.db_config_store import get_store

_logger = logging.getLogger(__name__)


# ── 路径与校验 ──────────────────────────────────────────────
def _workspace_root() -> Path:
    """语义库落地目录（活跃工作区根目录）。与 _scan_wren_projects 一致。"""
    from agent.workspace_manager import get_workspace_manager
    return get_workspace_manager().semantic_dir


def _is_within(path: Path, parent: Path) -> bool:
    """判断 path 是否在 parent 目录内（含相等）。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ── 业务知识：wren v5 规范布局 ──────────────────────────────
# wren 的 knowledge/ 只有 5 个子目录，且**只有 .md 会被消费**：
#   rules/*.md                    → context.load_knowledge_rules()（按文件名排序拼接）
#   sql/*.md                      → memory.markdown.load_query_pairs()（读 front-matter 的 nl+sql）
#   glossary|metrics|caveats/*.md → mcp_server.get_all_knowledge()
# 因此读/写只认这 5 个目录下的 .md —— 写到别处（如 knowledge/glossary.yml）等于没写，
# agent 一个字都读不到。子目录常量与 wren 的 _KNOWLEDGE_SUBDIRS 对齐。
_KNOWLEDGE_CATEGORIES: dict[str, str] = {
    "glossary": "glossary",
    "metrics": "metrics",
    "rules": "rules",
    "sql_patterns": "sql",
    "caveats": "caveats",
}
_KNOWLEDGE_DIRS = frozenset(_KNOWLEDGE_CATEGORIES.values())
# sql 条目走「自然语言问题 + SQL」结构化表单（front-matter 由此渲染）；其余是自由 Markdown
_MARKDOWN_CATEGORIES = ("glossary", "metrics", "rules", "caveats")
# 文件名里不允许出现的字符（含控制字符；跨 Windows/Linux 都安全）
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def _library_dir(project: Path, category: str) -> Path | None:
    """分类名 → 项目内 knowledge/<子目录>；未知分类返回 None。"""
    sub = _KNOWLEDGE_CATEGORIES.get(category)
    return (project / "knowledge" / sub) if sub else None


def _truthy(v: object) -> bool:
    """query 参数 / JSON body 里的布尔归一化：body 可能是 bool，也可能是字符串
    （`"true"`/`"1"`，前端或 curl 传参两种写法都见过）。"""
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_knowledge_file(project: Path, rel_path: str) -> tuple[Path | None, str]:
    """把前端给的相对路径解析成项目内的知识文件，返回 (绝对路径, 规范化相对路径)。

    只接受 `knowledge/<5 分类之一>/<名字>.md`。允许中文/空格/大小写（生产库文件名就是
    `术语表.md`、`报工与工时.md`，必须能原地编辑不改名），拒绝 .. 逃逸、绝对路径、
    反斜杠、二级子目录、非 .md、隐藏文件与非法字符。不合法时路径返回 None。
    """
    raw = str(rel_path or "").strip()
    if not raw or "\\" in raw or raw.startswith("/"):
        return None, raw
    parts = raw.split("/")
    if len(parts) != 3 or parts[0] != "knowledge" or parts[1] not in _KNOWLEDGE_DIRS:
        return None, raw
    fname = parts[2]
    if not fname.endswith(".md") or fname.startswith(".") or _INVALID_FILENAME_CHARS.search(fname):
        return None, raw
    if fname == ".md":
        return None, raw
    safe = f"knowledge/{parts[1]}/{fname}"
    target = (project / safe).resolve()
    if not _is_within(target, project):
        return None, safe
    # 二次确认父目录就是分类目录本身（防软链/大小写差异绕过）
    if target.parent != (project / "knowledge" / parts[1]).resolve():
        return None, safe
    return target, safe


def _read_project_name(project_path: Path) -> str:
    """读 wren_project.yml 的 name 字段；失败回退目录名。"""
    yml = project_path / "wren_project.yml"
    if yml.is_file():
        try:
            import yaml

            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            name = str(data.get("name", "")).strip()
            if name:
                return name
        except Exception as e:  # noqa: BLE001
            _logger.debug("[wren_semantic] 读 %s name 失败: %s", yml, e)
    return project_path.name


# ── git / wren 元信息 ─────────────────────────────────────
def _git_info(project_path: Path) -> dict | None:
    from agent.utils.git_repo import repo_info

    try:
        return repo_info(str(project_path))
    except Exception as e:  # noqa: BLE001
        _logger.debug("[wren_semantic] 读 git 元信息失败: %s", e)
        return None


def _mdl_summary(project_path: Path) -> dict:
    """读 target/mdl.json 概览。未构建返回 built=False 与全 0 计数。"""
    mdl = project_path / "target" / "mdl.json"
    if not mdl.is_file():
        return {"built": False, "models": 0, "views": 0, "relationships": 0, "cubes": 0}
    try:
        data = json.loads(mdl.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.warning("[wren_semantic] 读 %s 失败: %s", mdl, e)
        return {"built": False, "models": 0, "views": 0, "relationships": 0, "cubes": 0}
    return {
        "built": True,
        "models": len(data.get("models", []) or []),
        "views": len(data.get("views", []) or []),
        "relationships": len(data.get("relationships", []) or []),
        "cubes": len(data.get("cubes", []) or []),
        "data_source": str(data.get("dataSource", "") or ""),
    }


def _associated_dbs(project_path: Path) -> list[str]:
    """返回 wren_project 指向该目录的所有 DBConfig.name。"""
    names: list[str] = []
    target = str(project_path.resolve())
    for cfg in get_store().list_configs(masked=True):
        wp = str(cfg.get("wren_project", "") or "")
        if not wp:
            continue
        try:
            if str(Path(wp).resolve()) == target:
                names.append(str(cfg.get("name", "")))
        except OSError:
            if os.path.abspath(wp) == target:
                names.append(str(cfg.get("name", "")))
    return names


def _project_detail(path: str) -> dict:
    """由磁盘路径生成一条语义库列表项（含 MDL 概览计数，供前端列表直接展示）。"""
    p = Path(path)
    mdl = _mdl_summary(p)
    return {
        "path": str(p.resolve()),
        "name": p.name,                       # 目录名（下拉沿用）
        "project_name": _read_project_name(p),  # wren_project.yml 的 name
        "source": "git" if (p / ".git").exists() else "local",
        "git": _git_info(p),
        "built": mdl["built"],
        "models": mdl["models"],
        "views": mdl["views"],
        "relationships": mdl["relationships"],
        "cubes": mdl["cubes"],
        "data_source": mdl.get("data_source", ""),
        "associated_dbs": _associated_dbs(p),
    }


async def _project_detail_async(path: str) -> dict:
    """`_project_detail` 的**异步入口**（P1-14）。

    `_project_detail` 自己有两个重活，都是「`async def` 里直接调 = 挂在共用事件循环上」：
      · `_git_info` → `git_repo.repo_info`：**子进程**（Windows 上起进程更贵）；
      · `_mdl_summary` → 读 `target/mdl.json`：大语义库能到 MB 级（151 模型），JSON 解析几十毫秒。
    本文件里凡在 `async def` 响应体里拼 `_project_detail(...)` 的地方一律改走这里。

    走长任务池（`offload_long`）：子进程 + 大文件解析，不该和每请求的短调用抢默认池。
    """
    return await offload_long(_project_detail, path)


def _find_project(name: str) -> Path | None:
    """按目录名或 wren_project.yml 的 name 定位项目目录。"""
    for item in _scan_wren_projects():
        p = Path(item["path"])
        if p.name == name or _read_project_name(p) == name:
            return p
    return None


# ── wren CLI ──────────────────────────────────────────────
def _wren_bin() -> str:
    from agent.settings.setting import settings

    return (settings.WREN_BIN_PATH or "").strip() or "wren"


def _run_wren(project_path: Path, *args: str, timeout: int = 180) -> tuple[bool, str]:
    """在项目目录内执行 wren 命令，返回 (ok, 合并后的输出)。"""
    bin_path = _wren_bin()
    try:
        proc = subprocess.run(
            [bin_path, *args],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False, f"wren CLI 未找到（WREN_BIN_PATH={bin_path}）"
    except subprocess.TimeoutExpired:
        return False, f"wren {' '.join(args)} 超时（>{timeout}s）"
    except Exception as e:  # noqa: BLE001
        return False, f"wren 执行失败: {e}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    merged = "\n".join(x for x in (out, err) if x)
    return proc.returncode == 0, (merged or f"退出码 {proc.returncode}")


# ── GitHub 自动建库 ────────────────────────────────────────
def _auto_create_github_repo(remote_url: str) -> bool:
    """从 remote_url 提取 owner/repo，尝试用 gh CLI 或 GitHub API 创建私有仓库。"""
    import re
    m = re.search(r"github\.com[/:]([^/]+)/([^/.]+?)(?:\.git)?/?$", remote_url)
    if not m:
        return False
    owner, repo = m.group(1), m.group(2)

    # 方案 1：gh CLI
    try:
        proc = subprocess.run(
            ["gh", "repo", "create", f"{owner}/{repo}", "--private", "--source=.", "--push"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            _logger.info("[auto_create_github_repo] gh 已创建 %s/%s", owner, repo)
            return True
        _logger.debug("[auto_create_github_repo] gh 失败: %s", proc.stderr.strip())
    except FileNotFoundError:
        _logger.debug("[auto_create_github_repo] gh CLI 未安装，尝试 GitHub API")
    except Exception as e:
        _logger.debug("[auto_create_github_repo] gh 异常: %s", e)

    # 方案 2：从 git credential 获取 token，调用 GitHub API
    try:
        token = _get_github_token(remote_url)
        if not token:
            _logger.warning("[auto_create_github_repo] 未找到 GitHub token")
            return False
        import requests
        resp = requests.post(
            "https://api.github.com/user/repos",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"name": repo, "private": True, "auto_init": False},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            _logger.info("[auto_create_github_repo] API 已创建 %s/%s", owner, repo)
            return True
        _logger.warning("[auto_create_github_repo] API 失败 %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        _logger.warning("[auto_create_github_repo] API 异常: %s", e)
    return False


def _get_github_token(remote_url: str) -> str:
    """尝试从 git credential helper 获取 GitHub token/password。"""
    import re
    m = re.search(r"https://([^@]+@)?github\.com", remote_url)
    if not m:
        return ""
    # 从 URL 提取内嵌 token（如 https://TOKEN@github.com/...）
    if m.group(1):
        return m.group(1).rstrip("@")
    # 尝试 git credential fill（非交互模式，避免弹窗）
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n",
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            env=env,
        )
        if proc.returncode == 0:
            for line in proc.stdout.split("\n"):
                if line.startswith("password="):
                    return line[9:].strip()
    except Exception:
        pass
    return ""


# ── 凭据：生成 connection 文件 ────────────────────────────
async def _ensure_git_identity(cwd: str) -> None:
    """确保 git 用户信息已配置（用于 commit）。直接设置，已存在则覆盖，不会报错。

    P1-14：内部是两次 git 子进程，整段一次搬进长任务池（`async` 里不许直接跑 git）。
    """
    from agent.utils import git_repo

    def _apply() -> None:
        git_repo._run(["config", "--local", "user.name", "NL2SQL Agent"], cwd=cwd)
        git_repo._run(["config", "--local", "user.email", "agent@nl2sql.local"], cwd=cwd)

    await offload_long(_apply)


def _write_connection_file(project_path: Path, target_db: str) -> str:
    """用 target_db 的已解密连接信息生成 config/connection_<db_type>.json。

    wren CLI 只读明文 connection；git 仓库内的 connection 文件常含占位或不应
    携带真实凭据，故用本地 db_config.json 的加密配置解密后覆盖生成。
    """
    try:
        cfg = get_store().get(target_db)
    except KeyError:
        raise ValueError(f"数据库 '{target_db}' 不存在")
    props = {
        "host": cfg.host,
        "port": cfg.port,
        "database": cfg.database,
        "user": cfg.user,
        "password": cfg.password,
    }
    payload = {
        "datasource": cfg.db_type,
        "properties": {k: v for k, v in props.items() if v is not None},
    }
    config_dir = project_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    fname = f"connection_{cfg.db_type}.json"
    (config_dir / fname).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(config_dir / fname)


def _profile_name(project_path: Path, target_db: str) -> str:
    """确定 wren profile 名：wren_project.yml 的 profile 字段优先，否则用 target_db。

    `wren context set-profile <name>` 会把 profile 名写回 wren_project.yml，后续
    `wren context build` / `wren serve mcp` 都按此名解析连接。若仓库已带 profile
    字段，须沿用其名注册（否则 build 解析不到连接）。
    """
    yml = project_path / "wren_project.yml"
    profile = ""
    if yml.is_file():
        try:
            import yaml

            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            profile = str(data.get("profile", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            _logger.debug("[wren_semantic] 读 profile 失败: %s", e)
    if not profile:
        profile = re.sub(r"\W+", "_", target_db or "").strip("_") or "default"
    return profile


def _build_with_profile(
    project_path: Path, target_db: str, overwrite_connection: bool = True,
    force_build: bool = False,
) -> tuple[bool, str]:
    """拉取后的构建链路：生成 connection → 注册 profile → set-profile → context build。

    返回 (ok, 各步骤结果摘要)；ok 仅取决于 context build 是否成功。target/mdl.json
    已存在时跳过构建（拉取的仓库常带构建产物，无需连库重建）；`force_build=True`
    时不做这个跳过——「接管已有仓库」（`_adopt_git_into`）必须让构建产物与 profile
    对本机关联库成立，跳过会留下仓库原作者的数据源。步骤间 best-effort：
    profile add 失败（如已存在同名 profile）不阻断后续 set-profile / build。

    - `overwrite_connection=True`（默认，且给了 target_db）：用 target_db 的本地
      连接信息覆盖生成 connection 文件并注册 profile。
    - `overwrite_connection=False` 或未给 target_db：保留仓库自带连接，直接按
      wren_project.yml 的 profile 构建。
    """
    if not force_build and (project_path / "target" / "mdl.json").is_file():
        return True, "已存在 target/mdl.json，跳过构建"

    profile = _profile_name(project_path, target_db)
    steps: list[str] = []
    if target_db and overwrite_connection:
        conn_file = _write_connection_file(project_path, target_db)  # 可能抛 ValueError
        steps.append(f"生成 {Path(conn_file).name}")
        ok_add, out_add = _run_wren(
            project_path, "profile", "add", profile, "--from-file", conn_file
        )
        steps.append("profile add " + ("ok" if ok_add else f"失败({out_add})"))
        ok_set, out_set = _run_wren(project_path, "context", "set-profile", profile)
        steps.append("set-profile " + ("ok" if ok_set else f"失败({out_set})"))
    else:
        steps.append("保留仓库自带连接，跳过凭据覆盖")

    ok_build, out_build = _run_wren(project_path, "context", "build")
    steps.append("context build " + ("ok" if ok_build else f"失败({out_build})"))
    return ok_build, "；".join(steps)


def _invalidate_detector() -> None:
    """失效「发现类」缓存（detector + db_name 归一化 + 语义库版本物化 + 显示名），
    并触发运行期 MCP 工具注册表的后台对账（免重启）。

    语义库的新增/删除/改名/重新关联都走这里。对账按 **server 指纹**决定是否重新
    加载，所以**内容更新（git pull + context build）不会触发重载**（没必要：每次
    工具调用新起的子进程都重读 `target/mdl.json`）。对账在守护线程里跑：加载完成
    才换上新条目，因此调用方**没有**「工具短暂消失」的空窗。
    """
    try:
        from agent.utils.semantic_db import invalidate_db_discovery_caches

        invalidate_db_discovery_caches()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[wren_semantic] 发现类缓存失效失败: %s", e)
    # Langfuse 显示名（wrenai 项目标题）按语义库算，改名后要重算；纯显示，无安全影响
    try:
        from agent.trace.langfuse_client import reset_wrenai_display_cache

        reset_wrenai_display_cache()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[wren_semantic] wrenai 显示名缓存失效失败: %s", e)
    try:
        from agent.tools.mcp_tool import reload_sub_entries_in_background

        reload_sub_entries_in_background()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[wren_semantic] MCP 工具注册表对账触发失败: %s", e)


async def _sync_mcp_tools_many(db_names) -> dict:
    """批量把若干库的语义工具同步装进运行期注册表 → ``{库名: 结果}``。

    顺序执行（每个库都要起一次 MCP 子进程，并发起反而抢资源）；单个库失败不影响
    其余，结果里如实带回 ``status`` / ``error``。
    """
    out: dict = {}
    for db in db_names:
        out[db] = await _ensure_mcp_tools(db)
    return out


def _fallback_generate(tables, foreign_keys, scope):
    """基于表结构的简单生成（LLM 不可用时的回退）。"""
    generated = {}
    if "glossary" in scope:
        generated["glossary"] = [
            {
                "name": t.name,
                "definition": t.comment or f"{t.name} 表",
                "synonyms": [],
                "related_tables": [t.name],
            }
            for t in tables
        ]
    if "metrics" in scope:
        generated["metrics"] = []
    if "rules" in scope:
        generated["rules"] = [
            {
                "name": "通用查询规则",
                "category": "general",
                "description": "查询时默认使用最新数据，除非用户指定时间范围。",
                "scope": "global",
            }
        ]
    if "sql_patterns" in scope:
        generated["sql_patterns"] = []
    return generated


# ── 「接入 Git」：给已建好的本地语义库接上远程仓库 ─────────────
# 用户诉求（2026-09-20）：「语义库创建后，能不能就支持更新 git 上的仓库，因为有些
# 时候 git 上已经有对应的语义库了」。缺口：新建出来的库没有 .git → 卡片上没有
# 「更新」按钮、git_pull 直接 400；Git 导入又因同名目录被 409 挡住 → 只能删库重来
# （连带解绑数据库）。这里补上反向路径：把已有远程仓库的内容**接管**进一个本地目录。
_ADOPT_TIME_FMT = "%Y%m%d-%H%M%S"
# 回报给前端的内容清单截断长度：列几个够用户认出是什么，其余只给数量
_ADOPT_LIST_LIMIT = 5


class _AdoptError(Exception):
    """接管流程的用户可见结果：带 HTTP 状态 + 附加字段（由端点原样回给前端）。

    `status=200` 是刻意为之的特例，专给「业务上可确认」的结果用（本地有自建内容 /
    同名目录已存在）：前端 `handle()` 把**任何**非 2xx 压成一句 Error，附加字段
    （`code` / `local_files`）会被丢掉，对话框就没法据此渲染确认勾选。与
    `git_repo.pull_ref` 用 `blocked` 字段表达「可确认的拒绝」是同一套做法。
    """

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _rmtree_force(path: Path) -> None:
    """尽力删除目录：Windows 上 git 对象文件是只读的，rmtree 需先改权限。

    best-effort（失败只告警不抛）：调用方一般已达成主要目的，留一个残留目录不该让
    整个操作报错。`delete_project` 的目录删除走同一套处理。
    """
    def _on_rm_error(func, target, exc_info):  # noqa: ANN001
        import stat

        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except Exception as e2:  # noqa: BLE001
            _logger.warning("[wren_semantic] 删除失败 %s: %s", target, e2)

    if not path.exists():
        return
    try:
        shutil.rmtree(path, onexc=_on_rm_error)
    except TypeError:
        # Python < 3.12 用 onerror
        shutil.rmtree(path, onerror=_on_rm_error, ignore_errors=False)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[wren_semantic] rmtree 失败 %s: %s", path, e)


def _local_content_state(project: Path) -> dict:
    """判定本地目录是「刚建出来的空骨架」还是「有自建内容」。

    返回 ``{"files": [...], "pristine": bool, "built": bool}``；`files` 是接管会
    覆盖掉的自建内容（项目内相对路径，posix 风格，已排序）。

    判据宁严勿松——判错两个方向的代价不对等：多问一次只是多点一下确认，少问一次会
    把人家自己写的东西换掉。故：
      - 根目录除 `wren_project.yml` 之外的任何条目 → 内容（未知文件也算）；
      - `models/` `views/` `cubes/` 下任何文件（`.gitkeep` 除外）→ 内容；
      - `knowledge/` 下正文与 wren_templates 任一模板**逐字相同**的文件 → 不算内容
        （新建时生成的模板/示例，本就该被仓库版本替换）；
      - `target/` 下任何文件（含 `mdl.json`）→ 内容（已构建，产物可再生但可能白建）；
      - `config/connection_*.json` → 不算内容（本地凭据，构建时按 db_config 重新生成，
        且通常被仓库 .gitignore 忽略）。
    """
    from agent.utils import wren_templates as tpl

    template_bodies = {
        body.strip()
        for body in (
            tpl.knowledge_yml(),
            tpl.rules_general_md(),
            tpl.glossary_template(),
            tpl.metrics_template(),
            tpl.sql_template(),
            tpl.caveats_template(),
        )
        if body and body.strip()
    }
    if not project.is_dir():
        return {"files": [], "pristine": True, "built": False}

    content: list[str] = []
    built = False
    for cur, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d != ".git"]
        cur_path = Path(cur)
        rel_dir = cur_path.relative_to(project)
        for fname in files:
            rel = fname if str(rel_dir) == "." else f"{rel_dir.as_posix()}/{fname}"
            top = rel.split("/", 1)[0]
            if rel == "wren_project.yml":
                continue  # 新建时生成，会被仓库版本替换
            if top == "config" and fname.startswith("connection_"):
                continue  # 本地凭据，构建时重新生成
            if top == "target" and fname == "mdl.json":
                built = True
            if fname == ".gitkeep":
                continue
            if top == "knowledge":
                try:
                    body = (cur_path / fname).read_text(encoding="utf-8").strip()
                except (OSError, UnicodeDecodeError):
                    body = ""
                if body and body in template_bodies:
                    continue  # 新建时生成的模板/示例
            content.append(rel)
    return {"files": sorted(content), "pristine": not content, "built": built}


def _adopt_backup_dir(root: Path, name: str, stamp: str) -> Path:
    """备份目录路径：`<workspace>/<name>.备份-<ts>`（同秒重复时补序号）。

    命名含「备份」是刻意的——`db_config._scan_wren_projects` 会跳过名字含
    `备份`/`backup` 的目录，备份因此不会被当成语义库列进前端下拉。
    """
    backup = root / f"{name}.备份-{stamp}"
    n = 0
    while backup.exists():
        n += 1
        backup = root / f"{name}.备份-{stamp}-{n}"
    return backup


# ── 变更语义库：按库串行 + 「暂存副本 → 原子换入」 ──────────────
# 为什么（2026-09-24 P2-10② 审计）：`target/mdl.json` 是**别人正在读**的文件——每次
# 工具调用新起的 `wren serve mcp` 子进程都直接打开它（见 `_invalidate_detector` 的
# 说明）。而 wren 的 `context build` 是**原地截断重写**、`git checkout` 也就地改工作树，
# 于是读者可能在窗口里打开「半写的 JSON」或「根本不存在的文件」；wren 加载器对后者是
# **硬失败**：
#     Error: project found at <path> but target/mdl.json missing.
# 生产上表现为该库的工具全部加载失败、甚至**重启后容器起不来**（wit-mdl 事故）。
#
# 做法（与 `_adopt_git_into` 同族）：所有写入都在**暂存副本**里做完，成功了才安装；
# 失败则什么都不动，线上目录一个字节都没被碰过（构建失败不再可能让一个原本可用的
# 语义库停在「没有 mdl.json」的状态）。安装方式按「变更的是不是产物文件」分两种：
#
#   · **整目录交换**（`_swap_in`，给 git 拉取用）：拉取改的是一整棵工作树，没法逐文件
#     原子替换，只能连目录一起换。两次同盘 `rename` 之间目录名短暂空缺 —— 实测
#     （Windows 紧循环轮询）一次连续 ~5ms 的缺口，Linux 上远小于此（仅目录项更新）。
#   · **单文件原子替换**（`_replace_target_mdl`，给构建用）：构建的产物就
#     `target/mdl.json` 一个文件，`os.replace` 本身是原子的 ⇒ **零窗口**。
#
# 暂存目录 = `<项目父目录>/.backups/.<tag>-<ts>/project`：同盘才能 rename；`.backups`
# 是隐藏目录、且它的一级子目录里没有 `wren_project.yml`，而 `_scan_wren_projects` 只扫
# 一级子目录 ⇒ 它不会被当成一个语义库列进前端（与 `_adopt_git_into` 同一约定）。
_PROJECT_LOCKS: dict[str, asyncio.Lock] = {}

# 产物替换的重试参数（只对 Windows 的「目标被打开」有意义，见 `_replace_target_mdl`）：
# 8 次、线性退避 20ms → 最坏 ~0.7s；Linux 上第一次就成功，等于零成本。
_REPLACE_RETRIES = 8
_REPLACE_RETRY_SLEEP = 0.02


def _project_lock(key: str) -> asyncio.Lock:
    """按**项目目录**取锁：同一语义库的写操作串行，不同库互不阻塞。

    只在事件循环线程上调用（各端点入口）⇒ 这个 dict 不需要再加锁。
    同库并发命中时的预期行为是「后到的等前一个做完」，而不是两个 `context build`
    同时写同一个 `target/`、两个 `pull` 同时改同一棵工作树（后者还会各自往
    `~/.wren/profiles.yml` 这类全局单文件里写）。键用解析后的路径 ⇒ 换工作区/改名
    不会串到别的库上。
    """
    lock = _PROJECT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROJECT_LOCKS[key] = lock
    return lock


def _new_staging_dir(project: Path, tag: str) -> Path:
    """本次变更的暂存根目录（同盘、隐藏、不会被当成语义库）。**取名即占位**。

    直接 mkdir 出目录（而不是只算个名字）：名字靠时间戳，同秒内的两次调用只有靠
    「目录已存在」才能发现撞车 —— 不占位的话两次会拿到同一个名字。父目录（`.backups`）
    随之建出，`shutil.copytree` 会把内容拷进它的下级。
    """
    from datetime import datetime

    stamp = datetime.now().strftime(_ADOPT_TIME_FMT)
    base = project.parent / ".backups"
    staging = base / f".{tag}-{stamp}"
    n = 0
    while True:
        try:
            staging.mkdir(parents=True)
            return staging
        except FileExistsError:
            n += 1
            staging = base / f".{tag}-{stamp}-{n}"


def _swap_in(staging: Path, project: Path) -> str:
    """把暂存副本原子换入项目位置，返回被挪走的旧目录（备份）。

    两次同盘 `rename`（与 `_adopt_git_into` 同一套顺序）：第一次失败 = 现场没动过；
    第二次失败 = 把备份 rename 回来再抛。备份沿用 `<名>.备份-<ts>` 命名，`_scan_wren_
    projects` 会跳过它——它只在两次 rename 之间短暂存在，调用方随后就删。
    """
    from datetime import datetime

    backup = _adopt_backup_dir(
        project.parent, project.name, datetime.now().strftime(_ADOPT_TIME_FMT)
    )
    os.rename(project, backup)
    try:
        os.rename(staging, project)
    except OSError as e:
        try:
            os.rename(backup, project)  # 回滚：旧目录回到原处
        except OSError as e2:
            raise OSError(
                f"{e}；且回滚失败，原内容仍在 {backup}，请手动改回 {project.name}"
            ) from e2
        raise OSError(f"换入失败，已回滚到变更前状态：{e}") from e
    return str(backup)


async def _stage_and_swap(
    project: Path,
    tag: str,
    prepare: Callable[[Path], Awaitable[tuple[bool, Any]]],
) -> tuple[bool, Any]:
    """「暂存副本 → 在副本里变更 → 原子换入」骨架（「更新」与「重建」共用）。

    `prepare(副本目录)` 是真正的变更动作（拉取 / 构建），返回 `(ok, 载荷)`：

    - `ok=False`：**不换入**，副本丢弃，把载荷原样交回调用方（端点的错误文案与改动前
      完全一致）；线上目录保持变更前的样子，可以继续服务。
    - `ok=True`：把副本换入项目位置，载荷交回调用方。

    载荷约定：`prepare` 自己失败时给 `{"message": ...}`（端点直接展示）；暂存/换入这类
    环境性失败由本函数给 `{"message": ..., "staging_error": True}`。
    """
    staging_root = _new_staging_dir(project, tag)
    staging = staging_root / "project"
    try:
        await offload_long(shutil.copytree, project, staging)
    except OSError as e:
        await offload_long(_rmtree_force, staging_root)
        return False, {
            "message": f"暂存副本失败：{e}（未换入：线上目录保持原样）",
            "staging_error": True,
        }
    try:
        ok, payload = await prepare(staging)
    except BaseException:
        await offload_long(_rmtree_force, staging_root)
        raise
    if not ok:
        await offload_long(_rmtree_force, staging_root)
        return False, payload
    try:
        backup = await offload_long(_swap_in, staging, project)
    except OSError as e:
        await offload_long(_rmtree_force, staging_root)
        return False, {"message": f"换入失败：{e}（未换入：线上目录保持原样）",
                       "staging_error": True}
    # 副本已被 rename 走（staging 不存在了），这里只兜残留目录
    await offload_long(_rmtree_force, staging_root)
    if backup:
        # 变更前的目录不留：更新前的状态在 git 里可复现（本地未提交改动按 `pull_ref`
        # 的规则要么被拦住、要么已 stash 进随副本一起换入的 `.git`）
        await offload_long(_rmtree_force, Path(backup))
    return True, payload


def _replace_target_mdl(staged_project: Path, project: Path) -> None:
    """把暂存副本里构建出的 `target/mdl.json` **原子替换**进线上目录。

    `os.replace` 是同盘单文件 rename ⇒ 原子：读者要么打开旧文件、要么打开新文件，
    **不存在**「文件不存在」的时刻（这正是整目录交换做不到的，见段注释）。构建路径上
    唯一的写入点就是 `context.py:827-832 save_target`，所以只换这一个文件就够；线上
    `target/` 缺了才补建（首次构建某个从未构建过的库）。

    **Windows 上的重试**：目标文件被别的进程打开着时，Windows 的 rename 会直接失败
    （`WinError 5`；实测紧循环读该文件时必现）。Linux 没这回事（rename 对被打开的目标
    无条件成功），而生产跑在 Linux 上 —— 所以这里不额外退让：失败就重试几次，仍失败则
    报错。每次失败线上文件都没动，重试之间读者照常读到旧内容，重试本身不引入窗口。
    """
    src = staged_project / "target" / "mdl.json"
    if not src.is_file():
        raise OSError(f"构建没有产出 {src}")
    dst_dir = project / "target"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "mdl.json"
    last: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # Windows：目标被打开
            last = e
            time.sleep(_REPLACE_RETRY_SLEEP * (attempt + 1))
    raise OSError(f"替换 {dst.name} 失败（重试 {_REPLACE_RETRIES} 次）：{last}") from last


async def _stage_build_replace(
    project: Path,
    tag: str,
    prepare: Callable[[Path], Awaitable[tuple[bool, Any]]],
) -> tuple[bool, Any]:
    """「暂存副本 → 在副本里构建 → 原子替换产物」骨架（「重建」专用）。

    与 `_stage_and_swap` 的区别只在**安装方式**：这里不换目录，只把副本里的产物文件
    原子替换过去，因此**没有目录空缺窗口**（构建是最常被点的动作，收益最大），也不动
    `.git`、不动源文件、不产生备份目录 ⇒ 更快（拷贝时连 `.git`/`target` 都不用拷）。

    语义与 `_stage_and_swap` 完全一致：`prepare` 返回 `ok=False` = 什么都不换、载荷原样
    交回；`ok=True` = 产物已替换进线上目录、载荷交回；`prepare` 抛异常或失败 ⇒ 只在
    「替换产物」这一步才开始改线上目录，之前的失败现场一律未动。
    """
    staging_root = _new_staging_dir(project, tag)
    staging = staging_root / "project"
    try:
        # `.git` 与 `target` 都不需要：构建只读源文件、只写 target/mdl.json。
        # 少拷 `.git` 让这一步从「按库大小几十~几百 MB」降到「源文件几十 KB」。
        await offload_long(
            shutil.copytree,
            project,
            staging,
            ignore=shutil.ignore_patterns(".git", "target"),
        )
    except OSError as e:
        await offload_long(_rmtree_force, staging_root)
        return False, {
            "message": f"暂存副本失败：{e}（未换入：线上目录保持原样）",
            "staging_error": True,
        }
    try:
        ok, payload = await prepare(staging)
    except BaseException:
        await offload_long(_rmtree_force, staging_root)
        raise
    if not ok:
        await offload_long(_rmtree_force, staging_root)
        return False, payload
    try:
        await offload_long(_replace_target_mdl, staging, project)
    except OSError as e:
        await offload_long(_rmtree_force, staging_root)
        return False, {
            "message": f"换入失败：{e}（未换入：线上目录保持原样）",
            "staging_error": True,
        }
    await offload_long(_rmtree_force, staging_root)
    return True, payload


async def _adopt_git_into(
    project: Path,
    repo_url: str,
    ref: str = "",
    *,
    discard_local: bool = False,
    target_db: str = "",
    build: bool = True,
    overwrite_connection: bool = True,
) -> dict:
    """把远程仓库内容接管进**已存在**的本地语义库目录（`from_git` / `git_adopt` 共用）。

    顺序刻意排成「先 clone 到保险的位置，再动本地目录」——clone 成功之前不碰本地半分，
    交换用两次同盘 `rename`（原子），第二次失败就把备份 rename 回来。任何失败都保证
    本地目录完好（这是「用户点一下就把自己写的知识换掉」的场景，不能有中间态）。

    为什么不用「本地 git init + fetch + checkout -f」就地接管（沙箱实测过）：远程没有
    的本地文件会作为**未跟踪残留**留下来，一起被 build 烤进 MDL → 得到「远程+本地」
    混合语义库，比直接覆盖更难发现。干净 clone + 整目录交换没有这个中间态。

    关联天然保住：`_associated_dbs` 按 `wren_project` 解析后的**路径**匹配，而交换是
    同名同盘的 rename，路径字符串不变 → 库关联一条都不会掉，无需重挂。

    构建（`build=True`）在**交换之前**、对着 clone 出来的暂存目录做：这样换入的目录
    一开始就带着 `target/mdl.json`，读者永远看不到「接管到一半、还没有构建产物」的
    语义库（与 `_stage_and_swap` 同一理由，见上面的段注释）。

    成功返回载荷；用户可见的失败抛 `_AdoptError`（带状态与附加字段）。
    整个流程持有该项目的写锁（`_project_lock`）：并发的「更新」/「重建」不会插进来。
    """
    async with _project_lock(str(project.resolve())):
        return await _adopt_git_into_locked(
            project,
            repo_url,
            ref,
            discard_local=discard_local,
            target_db=target_db,
            build=build,
            overwrite_connection=overwrite_connection,
        )


async def _adopt_git_into_locked(
    project: Path,
    repo_url: str,
    ref: str = "",
    *,
    discard_local: bool = False,
    target_db: str = "",
    build: bool = True,
    overwrite_connection: bool = True,
) -> dict:
    """`_adopt_git_into` 的实体（调用方必须已经持有 `_project_lock`）。"""
    from datetime import datetime

    from agent.utils import git_repo

    try:
        git_repo.validate_repo_url(repo_url)
    except ValueError as e:
        raise _AdoptError(str(e), status=400) from e
    if target_db:
        try:
            get_store().get(target_db)  # 先校验，别等目录换完了才发现库不存在
        except KeyError as e:
            raise _AdoptError(f"数据库 '{target_db}' 不存在", status=404) from e

    # P1-14：`_local_content_state` 要遍历 knowledge/ 下每个 .md **逐个读内容**（拿来和新
    # 建模板比对），文件数随语义库增长 → 长任务池
    state = await offload_long(_local_content_state, project)
    if not state["pristine"] and not discard_local:
        shown = "、".join(state["files"][:_ADOPT_LIST_LIMIT])
        suffix = "" if len(state["files"]) <= _ADOPT_LIST_LIMIT else f" 等 {len(state['files'])} 个文件"
        raise _AdoptError(
            f"本地语义库有自建内容（{shown}{suffix}）：接管会用仓库内容整体替换它们"
            f"（原目录会备份到 workspace 下，可按提示找回）",
            status=200, code="local_content", pristine=False,
            local_files=state["files"], built=state["built"],
        )

    root = _workspace_root()
    stamp = datetime.now().strftime(_ADOPT_TIME_FMT)
    # 暂存目录放在 workspace 内（`.backups/` 这一层没有 wren_project.yml，且是隐藏
    # 目录，克隆过程中不会被 _scan_wren_projects 当成一个语义库列出来）；同盘才能
    # 用 rename 落地，跨盘整目录拷贝既不原子又要拷两遍。
    staging = root / ".backups" / f".adopt-{stamp}"
    n = 0
    while staging.exists():
        n += 1
        staging = root / ".backups" / f".adopt-{stamp}-{n}"
    clone_dir = staging / "project"

    try:
        staging.mkdir(parents=True, exist_ok=True)
        # P1-14：浅克隆（网络 + 子进程，timeout 300）进长任务池；下面的清理同理
        # （暂存目录里是一整个克隆，含 .git，删它并非毫秒级）。
        await offload_long(git_repo.clone_shallow, repo_url, ref, str(clone_dir))
    except (RuntimeError, OSError) as e:
        await offload_long(_rmtree_force, staging)
        raise _AdoptError(f"克隆失败：{e}", status=500) from e

    if not (clone_dir / "wren_project.yml").is_file():
        await offload_long(_rmtree_force, staging)
        raise _AdoptError(
            "仓库根目录缺少 wren_project.yml（语义库需按规范放在仓库根）", status=400
        )

    # 关联库先算：它按解析后的**路径**匹配（交换前后路径不变），而构建要用它挑 profile
    # 的连接 —— 因此必须在交换之前算出来，构建也就要在交换之前做。
    dbs = list(_associated_dbs(project))
    warnings: list[str] = []

    # ── 构建（在暂存目录里做）──────────────────────────────
    build_note = ""
    if build:
        db_for_build = target_db or (dbs[0] if dbs else "")
        try:
            # 接管路径强制真构建（force_build）：接管的目标是「拿这个仓库 + 本地库配置
            # 跑起来」，若仓库恰好带了 target/mdl.json 就跳过构建，profile 也不会注册，
            # 子进程按仓库原作者的 profile 解析连接 → 就是 aliyun-chinook 那种
            # 「条目在册、0 工具」。跳过构建只在「从零导入」路径保留。
            # P1-14：同 from_git —— 整条 wren 构建链路进长任务池
            # 对 clone 出来的暂存目录构建（不是 project）：换入即带着 target/mdl.json
            ok_build, build_note = await offload_long(
                _build_with_profile, clone_dir, db_for_build, overwrite_connection,
                force_build=True,
            )
        except ValueError as e:
            build_note = f"构建未执行：{e}"
            warnings.append(build_note)
        else:
            if not ok_build:
                warnings.append(f"构建未完成：{build_note}")
    else:
        build_note = "按要求跳过构建（源文件已就位，点「构建」后生效）"

    # ── 原子换入 ──────────────────────────────────────────
    backup = _adopt_backup_dir(root, project.name, stamp)
    try:
        os.rename(project, backup)
    except OSError as e:
        await offload_long(_rmtree_force, staging)
        raise _AdoptError(f"本地目录备份失败（{project} → {backup}）：{e}", status=500) from e
    try:
        os.rename(clone_dir, project)
    except OSError as e:
        try:
            os.rename(backup, project)  # 回滚：本地目录回到原处
        except OSError as e2:
            raise _AdoptError(
                f"接管失败且回滚失败：{e}；本地内容仍在 {backup}，请手动改回 {project.name}",
                status=500, backup_dir=str(backup),
            ) from e2
        await offload_long(_rmtree_force, staging)
        raise _AdoptError(f"接管失败，已回滚到接管前状态：{e}", status=500) from e
    await offload_long(_rmtree_force, staging)

    backup_dir = ""
    if state["pristine"]:
        # 空骨架内容全部可再生（yml / connection / 模板），不留没有价值的备份
        await offload_long(_rmtree_force, backup)
    else:
        backup_dir = str(backup)

    # ── 关联 + 工具热装 ───────────────────────────────────
    if target_db:
        get_store().set_wren_project(target_db, str(project.resolve()))
        if target_db not in dbs:
            dbs.append(target_db)
    elif not dbs:
        warnings.append(
            "该语义库尚未关联任何数据库：内容已接管，但工具不会被任何库使用；"
            "请到「数据库配置」把 wren_project 指向它"
        )
    _invalidate_detector()

    # 仓库自带构建产物时提示 data_source 与关联库不一致（不改判，但要让人看见）
    src = str((await offload_long(_mdl_summary, project)).get("data_source", "") or "")
    if src and dbs and "跳过构建" not in build_note:
        mismatched: list[str] = []
        for db in dbs:
            try:
                dtype = str(get_store().get(db).db_type or "")
            except KeyError:
                continue
            if dtype and dtype != src:
                mismatched.append(f"{db}({dtype})")
        if mismatched:
            warnings.append(
                f"仓库自带构建产物 data_source={src}，与关联库 {'、'.join(mismatched)} 的"
                f"类型不一致；查询报连接错误时点「构建」按本地库重建"
            )

    mcp = await _sync_mcp_tools_many(dbs) if dbs else None
    return {
        "ok": True,
        "project": await _project_detail_async(str(project.resolve())),
        "backup_dir": backup_dir,
        "build_note": build_note,
        "mcp": mcp,
        "associated_dbs": dbs,
        "warnings": warnings,
        "requires_restart": False,
    }


# ── P1 授权辅助 ──────────────────────────────────────────
def _project_visible(project_path: Path, allowed_dbs: set[str]) -> bool:
    """语义库是否对用户可见：至少一个关联库在用户的 visible_dbs 中。"""
    assoc = _associated_dbs(project_path)
    if not assoc:
        # 未关联任何库 → 只对管理员可见（admin 的 allowed_dbs = 全量已配置库）
        return False
    return bool(set(assoc) & allowed_dbs)


def _require_project_access(request: Request, project_path: Path) -> None:
    """校验用户有权访问该语义库（至少一个关联库可见），否则 403。"""
    user = require_user(request)
    if user.get("is_admin"):
        return
    from agent.auth.grants import visible_dbs
    allowed = visible_dbs(user)
    if not _project_visible(project_path, allowed):
        from starlette.exceptions import HTTPException
        raise HTTPException(status_code=403, detail="无权访问该语义库")


# ── 路由 ─────────────────────────────────────────────────
async def list_wren_projects(request: Request):
    user = require_user(request)
    from agent.auth.grants import visible_dbs
    allowed = visible_dbs(user)
    is_admin = bool(user.get("is_admin"))

    # P1-14：`_project_detail` 每个库要跑 `repo_info`（4 次 git 子进程）+ 读 MDL，
    # N 个库就是 4N 次子进程 —— 整段一次线程切换跑完（前端进语义库页面就调它）。
    def _list_details() -> list[dict]:
        return [
            _project_detail(p["path"])
            for p in _scan_wren_projects()
            if is_admin or _project_visible(Path(p["path"]), allowed)
        ]

    items = await offload_long(_list_details)
    return json_response({"projects": items})


async def associate_local(request: Request):
    """场景 A：关联本地已有目录到某条库配置。"""
    require_admin(request)
    data = await parse_body(request)
    path = str(data.get("path", "") or "").strip()
    target_db = str(data.get("target_db", "") or "").strip()
    if not path or not target_db:
        return json_response({"error": "path 与 target_db 必填"}, status=400)
    p = Path(path)
    if not p.is_dir() or not (p / "wren_project.yml").is_file():
        return json_response({"error": "目录不存在或缺少 wren_project.yml"}, status=400)
    if not _is_within(p, _workspace_root()):
        return json_response(
            {"error": f"目录必须在 workspace 内: {_workspace_root()}"}, status=400
        )
    try:
        get_store().get(target_db)  # 校验目标库存在
    except KeyError:
        return json_response({"error": f"数据库 '{target_db}' 不存在"}, status=404)
    if not get_store().set_wren_project(target_db, str(p.resolve())):
        return json_response({"error": f"数据库 '{target_db}' 不存在"}, status=404)
    _invalidate_detector()
    # 同步把该库的 wrenai 工具装进运行期注册表：响应里就能看到工具数与失败原因
    mcp = await _ensure_mcp_tools(target_db)
    return json_response(
        {
            "ok": True,
            "project": await _project_detail_async(str(p.resolve())),
            "requires_restart": False,
            "mcp": mcp,
        }
    )


async def from_git(request: Request):
    """场景 B（核心）：clone → 定位项目根 → 生成凭据 → 构建 → 关联。

    `replace_existing`（body，缺省 false）：目标目录已存在时，缺省回 200 +
    `code="exists"` 让前端弹确认（不删用户任何东西）；确认后带 true 重试 → 走
    `_adopt_git_into` 接管（备份本地 → 干净 clone → 整目录交换）。已是 Git 仓库的
    同名目录拒绝接管（它有「更新」按钮）。
    """
    require_admin(request)
    from agent.utils import git_repo

    data = await parse_body(request)
    repo_url = str(data.get("repo_url", "") or "")
    ref = str(data.get("ref", "") or "")
    project_name = str(data.get("project_name", "") or "").strip()
    target_db = str(data.get("target_db", "") or "").strip()
    overwrite_connection = bool(data.get("overwrite_connection", True))

    if not repo_url:
        return json_response({"error": "repo_url 必填"}, status=400)
    try:
        git_repo.validate_repo_url(repo_url)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)

    # 项目名：显式指定 > 仓库名（去 .git 后缀）
    if not project_name:
        project_name = repo_url.rstrip("/").rsplit("/", 1)[-1]
        if project_name.endswith(".git"):
            project_name = project_name[:-4]
    project_name = project_name or "wren_project"
    # 目录名 sanitize：只保留安全字符，避免路径逃逸
    safe_name = "".join(c for c in project_name if c.isalnum() or c in "-_") or "wren_project"
    dest = _workspace_root() / safe_name
    if dest.exists():
        # 同名目录已存在：不再直接 409 让用户先去删库（生产痛点：有人先在平台「新建」
        # 出了库，之后才发现 git 上早就有对应语义库），改为「可接管」——复用
        # _adopt_git_into（备份本地 + 干净 clone + 整目录交换）。未确认时回 200 +
        # code="exists"（前端据此弹确认），确认即带 replace_existing=true 重试。
        if not _truthy(data.get("replace_existing")):
            state = await offload_long(_local_content_state, dest)  # P1-14：逐个读文件
            detail = (
                "当前是空骨架，无自建内容" if state["pristine"]
                else f"其中 {len(state['files'])} 个自建文件会先备份再替换"
            )
            return json_response({
                "ok": False,
                "code": "exists",
                "adoptable": True,
                "pristine": state["pristine"],
                "local_files": state["files"],
                "error": (
                    f"已存在同名语义库「{safe_name}」（{detail}）：将用仓库内容整体替换它；"
                    f"确认后带 replace_existing=true 重试"
                ),
                "project": await _project_detail_async(str(dest)),
            })
        if (dest / ".git").exists():
            # 已经是 Git 仓库 → 它有「更新」按钮，别用导入的覆盖语义把人家历史换掉
            return json_response({
                "error": f"「{safe_name}」已是 Git 仓库，请用卡片上的「更新」按钮",
                "code": "is_git",
            }, status=400)
        try:
            return json_response({
                **await _adopt_git_into(
                    dest, repo_url, ref,
                    discard_local=True,  # 走到这里用户已在前端确认「替换」，不再二次确认
                    target_db=target_db,
                    build=True,
                    overwrite_connection=overwrite_connection,
                ),
                "replaced_existing": True,
            })
        except _AdoptError as e:
            return json_response({"ok": False, "error": str(e), **e.extra}, status=e.status)

    try:
        # P1-14：浅克隆（网络 + 子进程，timeout 300）进长任务池
        await offload_long(git_repo.clone_shallow, repo_url, ref, str(dest))
    except RuntimeError as e:
        return json_response({"error": str(e)}, status=500)

    # 定位项目根：规范约定 wren_project.yml 固定在仓库根
    project_root = dest
    if not (project_root / "wren_project.yml").is_file():
        await offload_long(shutil.rmtree, dest, ignore_errors=True)
        return json_response(
            {"error": "仓库根目录缺少 wren_project.yml（语义库需按规范放在仓库根）"}, status=400
        )

    build_note = ""
    try:
        # P1-14：内部串了 3~4 次 wren 子进程（最长 180s×3），整条链路进长任务池
        ok, build_note = await offload_long(
            _build_with_profile, project_root, target_db, overwrite_connection
        )
    except ValueError as e:
        await offload_long(shutil.rmtree, dest, ignore_errors=True)
        return json_response({"error": str(e)}, status=400)
    if not ok:
        _logger.warning("[wren_semantic] %s 构建未完成: %s", project_root, build_note)

    # 关联
    mcp = None
    if target_db:
        get_store().set_wren_project(target_db, str(project_root.resolve()))
        _invalidate_detector()
        mcp = await _ensure_mcp_tools(target_db)  # 工具随即可用，无需重启

    return json_response(
        {
            "ok": True,
            "project": await _project_detail_async(str(project_root.resolve())),
            "build_note": build_note,
            "requires_restart": False,
            "mcp": mcp,
        }
    )


async def delete_project(request: Request):
    """解绑所有关联 + 删除项目目录（不可逆，需前端二次确认）。"""
    require_admin(request)
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    # 安全：只删 workspace 内目录，且 wren_project.yml.name 或目录名匹配
    if not _is_within(project, _workspace_root()):
        return json_response({"error": "仅支持删除 workspace 内的语义库"}, status=403)
    project_name = _read_project_name(project)
    if project.name != name and project_name != name:
        return json_response({"error": "项目名与请求 name 不一致，拒绝删除"}, status=400)

    # 解绑
    unbound = list(_associated_dbs(project))
    for db in unbound:
        get_store().set_wren_project(db, "")
    _invalidate_detector()
    # 这些库的语义工具立即下线（免重启）：模型清单里不再出现，存量调用由
    # DynamicMCPToolsMiddleware 挡掉，不会执行指向已删目录的陈旧实例
    try:
        from agent.tools.mcp_tool import invalidate_sub_entries

        for db in unbound:
            invalidate_sub_entries(db)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[wren_semantic] MCP 工具下线失败: %s", e)
    # 删除目录（Windows 上 git 文件可能只读，需要 onexc 处理）。
    # P1-14：整个语义库（含 .git）的删除放线程 —— 目录大时是秒级，卡在循环上会让
    # 所有会话一起等。
    await offload_long(_rmtree_force, project)
    # 验证是否删除成功
    if project.exists():
        _logger.warning("[delete_project] 目录仍存在: %s", project)
        return json_response({"ok": True, "name": name, "warning": "目录未能完全删除，可能被进程占用，请手动删除"})
    return json_response({"ok": True, "name": name})


async def _rebuild_swapped(project: Path) -> tuple[bool, str]:
    """「重建」的核心（含项目写锁）：在暂存副本里构建，成功后原子替换产物。

    单独抽成函数是为了能被**直接测**（`scripts/verify_semantic_staging_swap.py` 用假
    wren 二进制驱动真实文件系统）：端点只做鉴权 + 取项目 + 拼响应。
    """
    async def _prepare(staging: Path) -> tuple[bool, Any]:
        # P1-14：wren CLI 是**子进程**且最长 180s/600s（评估报告 §3.3 的首位）。走长任务池，
        # 构建期间其他人的 SSE / 子任务不再跟着一起卡住。
        ok_b, out = await offload_long(_run_wren, staging, "context", "build")
        if not ok_b:
            return False, {"message": f"context build 失败: {out}（未换入：线上目录保持原样）"}
        return True, {"message": ""}

    async with _project_lock(str(project.resolve())):
        ok, payload = await _stage_build_replace(project, "build", _prepare)
    return ok, str(payload.get("message", "构建失败"))


async def build_project(request: Request):
    """重新构建 MDL（context build + memory index，best-effort）。

    `context build` 在**暂存副本**里做，成功后把产物 `target/mdl.json` **原子替换**进
    线上目录（`_stage_build_replace`）：wren 的构建是原地截断重写 `target/mdl.json`，
    而那个文件正被每次工具调用新起的 MCP 子进程直接打开 —— 就地构建会让它们读到半写
    的 JSON；构建中途失败/被杀更会让整个库停在「没有 mdl.json」，wren 加载器对这种
    情况是**硬失败**（工具全挂、重启后容器起不来）。新写法下，构建失败连旧目录都不动
    （库照常服务），成功则是单文件 `os.replace` ⇒ 读者连「文件短暂不存在」都看不到。
    """
    require_admin(request)
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    ok, message = await _rebuild_swapped(project)
    if not ok:
        return json_response({"ok": False, "message": message}, status=400)

    # memory index 在替换**之后**、对线上目录跑：索引落在 `~/.wren/memory`（全局、按项目
    # 路径认），对着暂存目录建会认到一个随即消失的路径；它不写项目目录，不影响替换的
    # 原子性。失败不影响 build 结果（知识库索引可选）。
    idx_ok, idx_out = await offload_long(_run_wren, project, "memory", "index", timeout=600)
    tail = ("；memory index 完成" if idx_ok else f"；memory index 失败: {idx_out}")
    # 关联库的工具重装一遍（免重启）：语义库**内容**更新本就不需要重载，但若该库
    # 此前加载失败（如建库时还没有 MDL），条目会卡在失败态，build 是重试的时机
    mcp = await _sync_mcp_tools_many(_associated_dbs(project))
    return json_response(
        {"ok": True, "message": "context build 完成" + tail, "mcp": mcp}
    )


async def validate_project(request: Request):
    """语义库校验：跑 wren context validate，返回错误/告警列表 + MDL 概览。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    _require_project_access(request, project)
    ok, out = await offload_long(_run_wren, project, "context", "validate")  # P1-14：子进程
    # P1-14：`_mdl_summary` 解析 target/mdl.json —— 151 模型的生产库是 MB 级 JSON
    summary = await offload_long(_mdl_summary, project)
    summary["name"] = _read_project_name(project)
    summary["path"] = str(project.resolve())
    return json_response({"ok": ok, "message": out, "summary": summary})


async def summary_project(request: Request):
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    _require_project_access(request, project)

    # P1-14：mdl.json（MB 级）+ wren_project.yml + db_config 三次读盘，一次线程切换搬走
    def _load_summary() -> dict:
        s = _mdl_summary(project)
        s["name"] = _read_project_name(project)
        s["path"] = str(project.resolve())
        s["associated_dbs"] = _associated_dbs(project)
        return s

    return json_response({"summary": await offload_long(_load_summary)})


# ── 新建语义库流程（create → introspect → generate-models → knowledge → build → push）──

async def create_project(request: Request):
    """Step 1：创建空项目骨架。"""
    require_admin(request)
    data = await parse_body(request)
    project_name = str(data.get("project_name", "") or "").strip()
    db_name = str(data.get("db_name", "") or "").strip()
    description = str(data.get("description", "") or "").strip()

    if not project_name:
        return json_response({"error": "project_name 必填"}, status=400)
    if not db_name:
        return json_response({"error": "db_name 必填"}, status=400)

    # 校验目标库存在
    try:
        cfg = get_store().get(db_name)
    except KeyError:
        return json_response({"error": f"数据库 '{db_name}' 不存在"}, status=404)

    # 校验项目名不冲突
    safe_name = "".join(c for c in project_name if c.isalnum() or c in "-_") or "wren_project"
    dest = _workspace_root() / safe_name
    if dest.exists():
        return json_response(
            {"error": f"目标目录已存在: {dest}"}, status=409,
        )

    # P1-14：整段是「建目录树 + 写 4 个文件 + 写 connection + 写 db_config（AES 加密 +
    # 临时文件 + os.replace 原子落盘）」，全在磁盘上 → 一次线程切换全搬走。
    # `_invalidate_detector()` 是**进程内缓存**操作（不能搬：搬了别的线程看不到失效）。
    def _materialize() -> None:
        # 创建目录结构
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "models").mkdir(exist_ok=True)
        (dest / "views").mkdir(exist_ok=True)
        (dest / "cubes").mkdir(exist_ok=True)
        (dest / "target").mkdir(exist_ok=True)
        for sub in ("glossary", "metrics", "rules", "sql", "caveats"):
            (dest / "knowledge" / sub).mkdir(parents=True, exist_ok=True)

        # 生成 wren_project.yml
        import yaml

        wren_yml = {
            "schema_version": 5,
            "name": project_name,
            "version": "1.0",
            "catalog": "wren",
            "schema": "public",
            "data_source": cfg.db_type,
        }
        if description:
            wren_yml["properties"] = {"description": description}
        (dest / "wren_project.yml").write_text(
            yaml.dump(wren_yml, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        # 生成 connection 文件
        _write_connection_file(dest, db_name)

        # 生成 knowledge 模板
        from agent.utils.wren_templates import knowledge_yml as _k_yml, rules_general_md

        (dest / "knowledge" / "knowledge.yml").write_text(_k_yml(), encoding="utf-8")
        (dest / "knowledge" / "rules" / "general.md").write_text(
            rules_general_md(), encoding="utf-8"
        )

        # 关联数据库 → 语义库
        get_store().set_wren_project(db_name, str(dest.resolve()))

    await offload(_materialize)
    _invalidate_detector()
    # 这里**不做**同步加载：目录刚建、models/ 还是空的，此时加载工具既无意义（还没
    # 人会查）又可能耗掉重试超时。工具在 introspect → build 之后由 build_project
    # 的同步加载（或后台对账）装进注册表。

    return json_response({
        "ok": True,
        "project": await _project_detail_async(str(dest.resolve())),
        "path": str(dest.resolve()),
        "requires_restart": False,
    })


async def introspect_project(request: Request):
    """Step 2：连接数据库提取表结构。"""
    require_admin(request)
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    data = await parse_body(request)
    db_name = str(data.get("db_name", "") or "").strip()

    # 从 connection 文件或 wren_project.yml 推导目标库
    if not db_name:
        db_name = _read_project_name(project)
        # 尝试从关联库反查
        for cfg_item in get_store().list_configs(masked=False):
            wp = str(cfg_item.get("wren_project", "") or "")
            try:
                if str(Path(wp).resolve()) == str(project.resolve()):
                    db_name = str(cfg_item.get("name", ""))
                    break
            except OSError:
                pass

    if not db_name:
        return json_response({"error": "无法确定目标数据库，请显式传入 db_name"}, status=400)

    try:
        cfg = get_store().get(db_name)
    except KeyError:
        return json_response({"error": f"数据库 '{db_name}' 不存在"}, status=404)

    # 内省
    from agent.utils.db_introspect import introspect_database

    try:
        # P1-14：同步驱动内省（ClickHouse 走 requests.get timeout=30，MySQL/PG 走同步
        # 驱动），可能 30s×N 张表 —— 进长任务池
        result = await offload_long(introspect_database, cfg)
    except Exception as e:
        _logger.exception("[wren_semantic] 内省失败: %s", e)
        return json_response({"error": f"数据库内省失败: {e}"}, status=500)

    return json_response({
        "ok": True,
        "db_name": db_name,
        "db_type": cfg.db_type,
        "tables": [
            {
                "name": t.name,
                "comment": t.comment,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type,
                        "wren_type": c.wren_type,
                        "nullable": c.nullable,
                        "comment": c.comment,
                        "is_primary_key": c.is_primary_key,
                    }
                    for c in t.columns
                ],
                "primary_key": t.primary_key,
                "column_count": len(t.columns),
            }
            for t in result.tables
        ],
        "foreign_keys": [
            {
                "source_table": fk.source_table,
                "source_column": fk.source_column,
                "target_table": fk.target_table,
                "target_column": fk.target_column,
            }
            for fk in result.foreign_keys
        ],
    })


async def generate_models(request: Request):
    """Step 2b：按选中表生成 models/*.yml 和 relationships.yml。"""
    require_admin(request)
    import yaml as _yaml

    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    data = await parse_body(request)
    selected_tables: list[str] = data.get("selected_tables", []) or []
    include_relationships = bool(data.get("include_relationships", True))
    db_name = str(data.get("db_name", "") or "").strip()

    if not selected_tables:
        return json_response({"error": "selected_tables 必填，至少选一张表"}, status=400)

    # 需要内省结果来生成模型 — 调内省
    if not db_name:
        for cfg_item in get_store().list_configs(masked=False):
            wp = str(cfg_item.get("wren_project", "") or "")
            try:
                if str(Path(wp).resolve()) == str(project.resolve()):
                    db_name = str(cfg_item.get("name", ""))
                    break
            except OSError:
                pass
    if not db_name:
        return json_response({"error": "无法确定目标数据库，请显式传入 db_name"}, status=400)

    try:
        cfg = get_store().get(db_name)
    except KeyError:
        return json_response({"error": f"数据库 '{db_name}' 不存在"}, status=404)

    from agent.utils.db_introspect import introspect_database

    try:
        result = await offload_long(introspect_database, cfg)  # P1-14：同步驱动内省
    except Exception as e:
        return json_response({"error": f"数据库内省失败: {e}"}, status=500)

    # P1-14：选 N 张表就写 N 个 models/<表>/metadata.yml（每个都要建目录 + 序列化 YAML 落盘），
    # 外加 relationships.yml —— 勾「全选」时是几百次写，整段搬线程（纯文件 I/O，长任务池）。
    def _write_models() -> tuple[int, int]:
        # 构建表名 → TableInfo 映射
        table_map = {t.name: t for t in result.tables}
        models_generated = 0
        for tbl_name in selected_tables:
            t = table_map.get(tbl_name)
            if t is None:
                _logger.warning("[wren_semantic] 表 '%s' 不在内省结果中，跳过", tbl_name)
                continue
            model = {
                "name": tbl_name.lower(),
                "table_reference": {"table": tbl_name},
                "columns": [
                    {
                        "name": c.name,
                        "type": c.wren_type,
                        "is_calculated": False,
                        "not_null": not c.nullable,
                        "is_primary_key": c.is_primary_key,
                        **({"properties": {"description": c.comment}} if c.comment else {}),
                    }
                    for c in t.columns
                ],
                "cached": False,
            }
            if t.primary_key:
                model["primary_key"] = t.primary_key
            if t.comment:
                model["properties"] = {"description": t.comment}

            model_dir = project / "models" / tbl_name.lower()
            model_dir.mkdir(parents=True, exist_ok=True)
            (model_dir / "metadata.yml").write_text(
                f"# {tbl_name} table\n" +
                _yaml.dump(model, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            models_generated += 1

        # 生成 relationships.yml
        relationships_generated = 0
        if include_relationships:
            seen_pairs: set[tuple[str, str]] = set()
            relationships: list[dict] = []
            for fk in result.foreign_keys:
                if fk.source_table not in selected_tables and fk.target_table not in selected_tables:
                    continue
                src = fk.source_table.lower()
                tgt = fk.target_table.lower()
                if src == tgt:
                    continue
                key = tuple(sorted([src, tgt]))  # type: ignore[assignment]
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                relationships.append({
                    "name": f"{src}_{tgt}",
                    "models": [src, tgt],
                    "join_type": "MANY_TO_ONE",
                    "condition": f"{src}.{fk.source_column} = {tgt}.{fk.target_column}",
                })
            (project / "relationships.yml").write_text(
                "# Auto-generated from database foreign keys\n" +
                _yaml.dump({"relationships": relationships}, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            relationships_generated = len(relationships)
        return models_generated, relationships_generated

    # 写的是构建的**输入**（models/*.yml）：拿项目的写锁，避免与「更新/重建」的
    # 暂存整目录拷贝交叉（那样会把半写的 model 文件烤进换入的副本）
    async with _project_lock(str(project.resolve())):
        models_generated, relationships_generated = await offload_long(_write_models)

    return json_response({
        "ok": True,
        "generated": {"models": models_generated, "relationships": relationships_generated},
    })


async def knowledge_template(request: Request):
    """Step 3：获取业务知识模板。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    _require_project_access(request, project)

    from agent.utils.wren_templates import all_templates

    return json_response({"templates": all_templates()})


async def save_knowledge(request: Request):
    """Step 3b：保存业务知识（wren 规范布局，见 _resolve_knowledge_file）。

    body：
      files:     {"knowledge/glossary/术语表.md": "正文"}   Markdown 分类写原文
      sql_pairs: {"knowledge/sql/未报工名单.md": {"nl","sql",["datasource","tags","body"]}}
                 —— 由 wren 自己的 render_query_markdown 渲染 front-matter，保证与
                 load_query_pairs/parse_query_markdown 往返一致
      deletes:   ["knowledge/caveats/旧陷阱.md"]            删除（幂等）

    返回 saved / deleted / rejected；rejected 是非法的路径（前端据此报错，不静默吞掉）。
    """
    require_admin(request)
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    data = await parse_body(request)
    files = data.get("files") or {}
    sql_pairs = data.get("sql_pairs") or {}
    deletes = data.get("deletes") or []
    if not isinstance(files, dict) or not isinstance(sql_pairs, dict) or not isinstance(deletes, list):
        return json_response({"error": "files / sql_pairs 需为对象，deletes 需为数组"}, status=400)
    if not files and not sql_pairs and not deletes:
        return json_response({"error": "files / sql_pairs / deletes 至少给一项"}, status=400)

    # P1-14：这一整段是 N 个文件的「读旧 front-matter → 渲染 → mkdir → 写入」/「删除」，
    # 每保存一次知识就跑一遍，文件数随语义库增长（网络盘/绑定挂载更慢）→ 一次线程切换
    # 整段搬走。纯路径 + 文件 I/O，不读请求上下文，故走长任务池。
    def _apply() -> tuple[list[str], list[str], list[str]]:
        from wren.memory.markdown import parse_query_markdown, render_query_markdown

        saved: list[str] = []
        deleted: list[str] = []
        rejected: list[str] = []

        def _reject(rel_path: object, why: str) -> None:
            _logger.warning("[wren_semantic] 拒绝知识文件 %s: %s", rel_path, why)
            rejected.append(str(rel_path))

        # 1) 自由 Markdown 分类：原文落盘
        for rel_path, content in files.items():
            target, safe = _resolve_knowledge_file(project, rel_path)
            if target is None:
                _reject(rel_path, "路径非法（只允许 knowledge/<分类>/<名>.md）")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(content), encoding="utf-8")
                saved.append(safe)
            except OSError as e:
                _reject(safe, f"写入失败: {e}")

        # 2) SQL 条目：结构化字段 → wren 渲染器生成 front-matter
        for rel_path, pair in sql_pairs.items():
            target, safe = _resolve_knowledge_file(project, rel_path)
            if target is None:
                _reject(rel_path, "路径非法（只允许 knowledge/<分类>/<名>.md）")
                continue
            if not isinstance(pair, dict):
                _reject(safe, "sql_pairs 的值需为对象")
                continue
            nl = str(pair.get("nl") or "").strip()
            sql = str(pair.get("sql") or "").strip()
            if not nl or not sql:
                # 没有 nl+sql 的 sql/*.md 不会被 wren 读取 → 拒写，不留垃圾文件
                _reject(safe, "nl 与 sql 均不能为空")
                continue

            # source / created_at 沿用文件已有值（重存不改出处），新文件默认 user
            old: dict = {}
            if target.is_file():
                try:
                    old = parse_query_markdown(target)
                except Exception as e:
                    _logger.debug("[save_knowledge] %s 旧 front-matter 解析失败: %s", safe, e)

            def _field(key: str, fallback=None):
                val = pair[key] if key in pair else fallback
                if isinstance(val, str):
                    val = val.strip()
                return val or None

            raw_tags = pair["tags"] if "tags" in pair else old.get("tags")
            if isinstance(raw_tags, (list, tuple)):
                tags = [str(t).strip() for t in raw_tags if str(t).strip()]
            elif isinstance(raw_tags, str):
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
            else:
                tags = []
            body = pair["body"] if "body" in pair else old.get("_body")

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    render_query_markdown(
                        nl,
                        sql,
                        datasource=_field("datasource", old.get("datasource")),
                        tags=tags or None,
                        source=_field("source", old.get("source")) or "user",
                        created_at=_field("created_at", old.get("created_at")),
                        body=str(body).strip() if body else None,
                    ),
                    encoding="utf-8",
                )
                saved.append(safe)
            except OSError as e:
                _reject(safe, f"写入失败: {e}")

        # 3) 删除（幂等：文件已不存在视为完成，不计入 deleted）
        for rel_path in deletes:
            target, safe = _resolve_knowledge_file(project, rel_path)
            if target is None:
                _reject(rel_path, "路径非法（只允许 knowledge/<分类>/<名>.md）")
                continue
            try:
                if target.is_file():
                    target.unlink()
                    deleted.append(safe)
            except OSError as e:
                _reject(safe, f"删除失败: {e}")

        return saved, deleted, rejected

    # 同 generate_models：knowledge/*.md 是构建输入，拿项目写锁避免被整目录拷贝逮到半截
    async with _project_lock(str(project.resolve())):
        saved, deleted, rejected = await offload_long(_apply)

    if rejected and not saved and not deleted:
        return json_response(
            {"error": "没有可写入的文件，路径或内容不合法", "rejected": rejected}, status=400
        )

    _logger.info("[save_knowledge] %s: 写入 %d / 删除 %d / 拒绝 %d", name, len(saved), len(deleted), len(rejected))
    return json_response({"ok": True, "saved": saved, "deleted": deleted, "rejected": rejected})


async def open_directory(request: Request):
    """Step 3c：在系统资源管理器中打开项目目录。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    _require_project_access(request, project)

    import platform
    import subprocess as _sp

    path = str(project.resolve())
    system = platform.system()

    def _open() -> None:
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            _sp.run(["open", path], check=True)
        else:
            _sp.run(["xdg-open", path], check=True)

    try:
        # P1-14：`open`/`xdg-open` 是子进程（无桌面环境的容器里可能一直等到超时），
        # 放线程跑，别让「打开目录」把事件循环钉住
        await offload_long(_open)
    except Exception as e:
        return json_response({"error": f"打开目录失败: {e}"}, status=500)

    return json_response({"ok": True, "path": path})


async def push_to_git(request: Request):
    """Step 5：推送语义库至 Git 远程仓库。"""
    require_admin(request)
    from agent.utils import git_repo

    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    data = await parse_body(request)
    remote_url = str(data.get("remote_url", "") or "").strip()
    branch = str(data.get("branch", "") or "main").strip()
    tag = str(data.get("tag", "") or "").strip()
    commit_message = str(data.get("commit_message", "") or "").strip()
    if not commit_message:
        # 前端提交信息留空时自动生成（含语义库名 + 时间），不再默认固定文案
        from datetime import datetime, timezone

        commit_message = "语义库更新 %s %s" % (
            name,
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
    # 是否强制覆盖远端（force push）。默认非强制：远端已有不同历史时普通推送会被
    # git 拒绝（rejected / non-fast-forward），需要用户在前端显式勾选强制覆盖。
    push_flags = ["--force"] if _truthy(data.get("force", False)) else []

    if not remote_url:
        return json_response({"error": "remote_url 必填"}, status=400)

    try:
        git_repo.validate_repo_url(remote_url)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)

    cwd = str(project)
    steps: list[str] = []
    # P1-14：整条链路（init → add → commit → push → tag）都是**阻塞 git 子进程**，
    # push 的 timeout 是 300s、tag 是 120s，且失败还会重试 2 次 —— 直接跑就是把这个
    # 用户的一次推送变成全站最长十几分钟的停摆（§3.1：10 个 job 共用一个事件循环、
    # 阻塞检测被 LANGGRAPH_ALLOW_BLOCKING 关掉，卡住是静默的）。故一律走
    # `git_repo.run_async`（→ 长任务池）。
    is_force = bool(push_flags)

    try:
        # 若没有 .git，初始化
        if not (project / ".git").exists():
            _logger.info("[push_to_git] git init: %s", cwd)
            ok, out = await git_repo.run_async(["init"], cwd=cwd)
            if not ok:
                return json_response({"error": f"git init 失败: {out}"}, status=500)
            steps.append("git init")
            # git init 创建的默认分支名可能不是目标分支名（如 master vs main），
            # 用 -m 重命名确保后续 push 时分支名匹配
            _logger.info("[push_to_git] git branch -m %s", branch)
            await git_repo.run_async(["branch", "-m", branch], cwd=cwd)
            _logger.info("[push_to_git] git remote add origin %s", remote_url)
            ok, out = await git_repo.run_async(
                ["remote", "add", "origin", remote_url], cwd=cwd
            )
            if not ok:
                return json_response({"error": f"git remote add 失败: {out}"}, status=500)
            steps.append("remote add origin")
        else:
            # 已有 .git（可能是上次半途失败残留）：origin 可能未配置（如 remote add
            # 被 dubious ownership / 断网打断），分支名也可能不是目标（如 master vs main）。
            # set-url 失败则改 add，再强制把当前分支重命名为目标分支，确保后续 push 走对分支。
            _logger.info("[push_to_git] 已有 .git，配置 origin %s", remote_url)
            ok, out = await git_repo.run_async(
                ["remote", "set-url", "origin", remote_url], cwd=cwd
            )
            if not ok:
                _logger.info("[push_to_git] set-url 失败（origin 未配置?）: %s", out)
                ok, out = await git_repo.run_async(
                    ["remote", "add", "origin", remote_url], cwd=cwd
                )
                if not ok:
                    return json_response({"error": f"git remote add 失败: {out}"}, status=500)
            _logger.info("[push_to_git] git branch -M %s", branch)
            ok, out = await git_repo.run_async(["branch", "-M", branch], cwd=cwd)
            if not ok:
                return json_response({"error": f"git branch 重命名为 {branch} 失败: {out}"}, status=500)
            steps.append("配置 origin + 重命名分支")

        _logger.info("[push_to_git] _ensure_git_identity")
        await _ensure_git_identity(cwd)

        _logger.info("[push_to_git] git add -A")
        ok, out = await git_repo.run_async(["add", "-A"], cwd=cwd)
        if not ok:
            return json_response({"error": f"git add 失败: {out}"}, status=500)
        steps.append("git add -A")

        _logger.info("[push_to_git] git commit -m '%s'", commit_message)
        ok, out = await git_repo.run_async(
            ["commit", "-m", commit_message, "--allow-empty"], cwd=cwd
        )
        if not ok and "nothing to commit" not in out.lower() and "nothing added" not in out.lower():
            return json_response({"error": f"git commit 失败: {out}"}, status=500)
        steps.append("git commit")

        _logger.info(
            "[push_to_git] git push -u origin %s%s", branch, " --force" if is_force else ""
        )
        ok, out = await git_repo.run_async(
            ["push", "-u", "origin", branch, *push_flags],
            cwd=cwd, timeout=300,
        )
        if not ok:
            _logger.warning("[push_to_git] 首次 push 失败: %s", out)
            # 非强制推送被远端拒绝（历史分叉/需拉取）→ 明确引导勾选强制覆盖，不再盲目重试
            low = out.lower()
            if not is_force and (
                "rejected" in low or "non-fast-forward" in low or "fetch first" in low
            ):
                return json_response(
                    {"error":
                        f"远端分支 '{branch}' 已有不同历史，普通推送被拒绝。\n"
                        "如需覆盖远端内容，请勾选推送弹窗中的「强制覆盖远端（force push）」。\n"
                        f"git 输出: {out}"},
                    status=409,
                )
            # 如果是远程仓库不存在，尝试用 gh CLI 自动创建（仅 GitHub）
            if "repository not found" in out.lower() and "github.com" in remote_url:
                # P1-14：内部是 `gh`/GitHub API 的网络调用，同样进长任务池
                created = await offload_long(_auto_create_github_repo, remote_url)
                if created:
                    steps.append("auto-create remote repo")
                    _logger.info("[push_to_git] 自动创建仓库成功，重试 push")
                    ok, out = await git_repo.run_async(
                        ["push", "-u", "origin", branch, *push_flags],
                        cwd=cwd, timeout=300,
                    )
            if not ok:
                await git_repo.run_async(
                    ["remote", "set-url", "origin", remote_url], cwd=cwd
                )
                _logger.info("[push_to_git] 重试 push")
                ok, out = await git_repo.run_async(
                    ["push", "-u", "origin", branch, *push_flags],
                    cwd=cwd, timeout=300,
                )
        if not ok:
            return json_response({"error": f"git push 失败: {out}"}, status=500)
        steps.append(f"push to origin/{branch}")

        if tag:
            _logger.info("[push_to_git] git tag %s", tag)
            await git_repo.run_async(["tag", "-f", tag], cwd=cwd)
            ok, out = await git_repo.run_async(
                ["push", "origin", tag, *push_flags], cwd=cwd, timeout=120
            )
            if ok:
                steps.append(f"tag {tag}")
            else:
                steps.append(f"tag push 失败: {out}")
                _logger.warning("[push_to_git] tag push 失败: %s", out)

        _logger.info("[push_to_git] 完成: %s", steps)
        return json_response({"ok": True, "message": "；".join(steps)})
    except Exception as e:
        _logger.exception("[push_to_git] 未预期异常: %s", e)
        return json_response({"error": f"推送失败: {e}"}, status=500)


async def read_knowledge(request: Request):
    """读取语义库业务知识：按 wren 规范遍历 knowledge/ 下 5 个子目录，逐文件返回。

    每个条目 = 一个 `.md` 文件（这是 wren 唯一会消费的形态）：
      glossary/metrics/rules/caveats → {name, file, content}（自由 Markdown 正文）
      sql_patterns                   → {name, file, nl, sql, [datasource, tags, body, ...]}
    """
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    _require_project_access(request, project)

    from wren.memory.markdown import parse_query_markdown

    # P1-14：这段要遍历 5 个分类目录、**逐个读 .md 全文**（成熟语义库几百个文件，
    # 网络盘/绑定挂载上能到几十~几百毫秒），且每次打开知识页都跑 → 一次线程切换整段搬走。
    # 纯路径计算 + 文本解析，不读请求上下文（故用长任务池，不占默认池）。
    def _collect() -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {cat: [] for cat in _KNOWLEDGE_CATEGORIES}

        for category in _MARKDOWN_CATEGORIES:
            d = _library_dir(project, category)
            if not d or not d.is_dir():
                continue
            for md_file in sorted(d.glob("*.md")):
                if md_file.name.startswith("."):
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as e:
                    _logger.warning("[read_knowledge] %s 读取失败: %s", md_file, e)
                    continue
                result[category].append({
                    "name": md_file.stem,
                    "file": f"knowledge/{d.name}/{md_file.name}",
                    "content": content,
                })

        sql_dir = _library_dir(project, "sql_patterns")
        if sql_dir and sql_dir.is_dir():
            for md_file in sorted(sql_dir.glob("*.md")):
                if md_file.name.startswith("."):
                    continue
                try:
                    fm = parse_query_markdown(md_file)
                except (OSError, UnicodeDecodeError) as e:
                    _logger.warning("[read_knowledge] %s 解析失败: %s", md_file, e)
                    continue
                nl, sql = fm.get("nl"), fm.get("sql")
                if not nl or not sql:
                    # 与 load_query_pairs 同判据：缺 nl+sql 的 md 不是知识条目（wren 会跳过）
                    continue
                item: dict = {
                    "name": md_file.stem,
                    "file": f"knowledge/sql/{md_file.name}",
                    "nl": str(nl),
                    "sql": str(sql),
                    "source": str(fm.get("source", "user")),
                    "body": str(fm.get("_body", "") or ""),
                }
                if fm.get("datasource"):
                    item["datasource"] = str(fm["datasource"])
                if fm.get("tags"):
                    raw_tags = fm["tags"]
                    item["tags"] = (
                        [str(t) for t in raw_tags]
                        if isinstance(raw_tags, (list, tuple))
                        else [str(raw_tags)]
                    )
                if fm.get("created_at"):
                    item["created_at"] = str(fm["created_at"])
                result["sql_patterns"].append(item)
        return result

    result = await offload_long(_collect)

    return json_response({"ok": True, "knowledge": result})


async def ai_generate_knowledge(request: Request):
    """AI 分析数据库结构，生成业务知识草稿。"""
    require_admin(request)
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    data = await parse_body(request)
    scope = data.get("scope", ["glossary", "metrics", "rules", "sql_patterns"])
    industry = str(data.get("industry", "") or "").strip()
    notes = str(data.get("notes", "") or "").strip()

    # 找到关联数据库
    db_name = ""
    store = get_store()
    for cfg_item in store.list_configs(masked=False):
        wp = str(cfg_item.get("wren_project", "") or "")
        try:
            if str(Path(wp).resolve()) == str(project.resolve()):
                db_name = str(cfg_item.get("name", ""))
                break
        except OSError:
            pass
    if not db_name:
        db_name = _read_project_name(project)

    if not db_name:
        return json_response({"error": "无法确定目标数据库"}, status=400)

    try:
        cfg = store.get(db_name)
    except KeyError:
        return json_response({"error": f"数据库 '{db_name}' 不存在"}, status=404)

    # 内省
    from agent.utils.db_introspect import introspect_database
    try:
        result = await offload_long(introspect_database, cfg)  # P1-14：同步驱动内省
    except Exception as e:
        return json_response({"error": f"数据库内省失败: {e}"}, status=500)

    # 构建表结构描述
    tables_info = []
    for t in result.tables:
        cols = ", ".join(f"{c.name} ({c.wren_type})" for c in t.columns[:10])
        comment = f" -- {t.comment}" if t.comment else ""
        tables_info.append(f"- {t.name}{comment}: {cols}")

    fk_info = []
    for fk in result.foreign_keys:
        fk_info.append(f"- {fk.source_table}.{fk.source_column} → {fk.target_table}.{fk.target_column}")

    # 构造 prompt 并调用 LLM
    try:
        from agent.llm.llm_factory import get_llm
        llm = get_llm()
    except Exception:
        # 如果 LLM 不可用，返回基于表结构的简单生成
        generated = _fallback_generate(result.tables, result.foreign_keys, scope)
        return json_response({"ok": True, "generated": generated, "mode": "fallback"})

    prompt = f"""你是一个数据分析专家。根据以下数据库结构，生成语义库的业务知识。

数据库类型: {cfg.db_type}
数据库名: {db_name}
{"行业: " + industry if industry else ""}
{"补充说明: " + notes if notes else ""}

## 表结构
{chr(10).join(tables_info)}

## 外键关系
{chr(10).join(fk_info) if fk_info else "无"}

请生成以下内容的 JSON（只返回 JSON，不要 markdown 代码块）：
{{
  "glossary": [
    {{"name": "术语名", "definition": "定义", "synonyms": ["同义词1"], "related_tables": ["表名"]}}
  ],
  "metrics": [
    {{"name": "metric_name", "display_name": "展示名", "type": "count_distinct", "expression": "SQL表达式", "description": "描述"}}
  ],
  "rules": [
    {{"name": "规则名", "category": "general", "description": "规则描述", "scope": "global"}}
  ],
  "sql_patterns": [
    {{"name": "模式名", "questions": ["用户可能的问题"], "template": "SQL模板", "parameters": []}}
  ]
}}

要求：
- glossary: 每个表至少1条，重要列也要生成（基于列名推断业务含义）
- metrics: 生成3-5个常用业务指标
- rules: 生成2-3条通用业务规则（如数据时效性、空值处理）
- sql_patterns: 生成2-3个常见查询模式
"""

    try:
        import json as _json
        response = await llm.ainvoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)
        # 清理可能的 markdown 代码块包裹
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        generated = _json.loads(content)
        # 过滤 scope
        generated = {k: v for k, v in generated.items() if k in scope}
        return json_response({"ok": True, "generated": generated, "mode": "ai"})
    except Exception as e:
        _logger.exception("[ai_generate] LLM 调用失败: %s", e)
        generated = _fallback_generate(result.tables, result.foreign_keys, scope)
        return json_response({"ok": True, "generated": generated, "mode": "fallback", "error": str(e)})


async def git_status(request: Request):
    """检查语义库的 Git 状态（是否有远程更新 / 本地未提交改动）。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    _require_project_access(request, project)

    if not (project / ".git").exists():
        # 非 Git 项目：把「接管会覆盖掉哪些本地内容」一并回给前端，「接入 Git」对话框
        # 据此在**点按钮之前**就把要覆盖的东西摆出来（2026-09-15 的教训：别让用户点
        # 了才吃一句拒绝）。pristine=True 时前端连确认都不用弹。
        state = await offload_long(_local_content_state, project)  # P1-14：逐个读文件
        return json_response({
            "ok": True,
            "is_git": False,
            "has_updates": False,
            "adopt_local_files": state["files"],
            "adopt_pristine": state["pristine"],
            "adopt_built": state["built"],
        })

    from agent.utils import git_repo

    cwd = str(project)

    # P1-14：这一串是 6~8 次 git 子进程（`repo_info` 自己就 4 次、`ls-remote` 走网络
    # timeout 30s、`local_changes` 还要 diff），整段合成一次线程切换跑完。前端语义库
    # 卡片是进页面就查的，挂在循环上等于每次刷新都把别人拖一下。
    def _collect() -> dict:
        info = git_repo.repo_info(cwd) or {}
        return {
            "info": info,
            "local_head": git_repo._rev_parse(cwd, "HEAD"),
            # 远程比对不用 origin/HEAD：浅克隆 / 按 tag 建的仓库没有这个符号引用
            # （2026-09-09 事故：has_updates 恒 False）。改为 ls-remote 实时取远程分支 sha。
            "remote": git_repo.list_remote_refs(cwd),
            "status": git_repo._run(["status", "--porcelain"], cwd=cwd),
            # 未提交改动的**具体文件**（与 pull_ref 护栏同判据）：前端「更新」对话框据此
            # 列出是哪几个文件挡着。注意 has_local_changes（含未跟踪文件）比这个宽 ——
            # 未跟踪文件不拦更新，别用前者决定是否放行（2026-09-15 生产：保存知识只写盘
            # 不提交 → 工作树常年脏，用户只看到一句看不懂的拒绝）。
            "changes": git_repo.local_changes(cwd),
        }

    snap = await offload_long(_collect)
    info = snap["info"]
    branch = info.get("branch", "") or ""
    remote = snap["remote"]
    remote_branch = (
        branch
        if any(b["name"] == branch for b in remote["branches"])
        else remote["default_branch"]
    )
    remote_sha = next(
        (b["sha"] for b in remote["branches"] if b["name"] == remote_branch), ""
    )
    has_updates = bool(
        snap["local_head"] and remote_sha and snap["local_head"] != remote_sha
    )

    ok_status, status_out = snap["status"]
    has_local_changes = bool(status_out.strip()) if ok_status else False
    changes = snap["changes"]

    return json_response({
        "ok": True,
        "is_git": True,
        "has_updates": has_updates,
        "has_local_changes": has_local_changes,
        "local_changes": changes["blocking"],
        "generated_changes": changes["generated"],
        "branch": branch,
        "commit": info.get("commit", ""),
        "remote_branch": remote_branch,
        "remote_commit": remote_sha[:7] if remote_sha else "",
        "default_branch": remote["default_branch"],
    })


async def git_refs(request: Request):
    """列出语义库远程分支/tag（供前端「更新」对话框选择）。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    _require_project_access(request, project)

    if not (project / ".git").exists():
        return json_response({
            "ok": True, "is_git": False,
            "default_branch": "", "branches": [], "tags": [],
        })

    from agent.utils import git_repo

    # P1-14：`ls-remote` 走网络（timeout 30s）+ `repo_info` 4 次子进程 → 一次线程切换
    def _collect() -> dict:
        return {
            "refs": git_repo.list_remote_refs(str(project)),
            "info": git_repo.repo_info(str(project)) or {},
        }

    snap = await offload_long(_collect)
    return json_response({
        "ok": True,
        "is_git": True,
        "current_branch": snap["info"].get("branch", ""),
        "current_commit": snap["info"].get("commit", ""),
        **snap["refs"],
    })


async def git_pull(request: Request):
    """把语义库更新到远程指定 ref（分支/tag）；不传 ref → 远程默认分支最新。

    ref 来源：query `?ref=<分支或tag>` 优先，其次 JSON body `{"ref": "..."}`。
    传分支 → 重置本地同名分支到远程（并修复被 tag 锁死的 refspec）；传 tag →
    detached 检出该 tag。核心逻辑在 git_repo.pull_ref（含本地改动/未推送提交护栏）。

    `discard_local`（query `?discard_local=1` 或 body `{"discard_local": true}`）：
    用户在前端显式确认「放弃本地改动」时传 true —— 后端先 `git stash` 备份本地改动
    再更新，返回 `stash_ref`（可找回），而不是静默覆盖。缺省 false = 保持原有的
    拒绝行为。生产背景：平台「保存知识」只写盘不提交（wren_semantic.save_knowledge），
    所以工作树常常是脏的，只给一句拒绝提示会让更新按钮变成死路。

    拉取在**暂存副本**里做（`_stage_and_swap`），成功后整目录原子换入：正在服务的那份
    目录在换入前一个字节都不会被改（既不会被 checkout 改工作树，也不会有「拉下来还没
    构建」的中间态被读者撞见）。另外，换入前若发现副本里没有 `target/mdl.json`，就地
    补一次 `context build` —— 前端是「先 pull 再单独发一次构建请求」，那第二个请求失败/
    被关页面打断时，语义库会停在「有源文件、没构建产物」的状态，而 wren 加载器对缺
    `target/mdl.json` 是**硬失败**（该库工具全挂、重启后容器都起不来）。
    """
    require_admin(request)
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    if not (project / ".git").exists():
        return json_response({"error": "该语义库不是 Git 仓库"}, status=400)

    ref = (request.query_params.get("ref") or "").strip()
    discard_local = _truthy(request.query_params.get("discard_local"))
    if not ref or not discard_local:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001  无 body / 非 JSON
            body = None
        if isinstance(body, dict):
            ref = ref or str(body.get("ref") or "").strip()
            discard_local = discard_local or _truthy(body.get("discard_local"))

    ok, payload = await _pull_swapped(project, ref, discard_local)
    if not ok:
        return json_response({"error": payload.get("message", "更新失败")}, status=400)
    if payload.get("changed"):
        _invalidate_detector()  # 拉取可能改到 wren_project.yml，刷新项目探测缓存
    return json_response(payload)


async def _pull_swapped(project: Path, ref: str, discard_local: bool) -> tuple[bool, dict]:
    """「更新」的核心（含项目写锁）：在暂存副本里拉取，成功后原子换入。

    抽出来是为了能被直接测（同 `_rebuild_swapped`）。返回值 = `(ok, pull_ref 的结果载荷)`
    —— 成功时载荷原样回给前端（`changed` / `stash_ref` / `message` 都在里面）。
    """
    from agent.utils import git_repo

    async def _prepare(staging: Path) -> tuple[bool, Any]:
        # P1-14：fetch/checkout/stash 全是网络 + 子进程（pull_ref 内部 timeout 120s，
        # 且失败会再做一次 stash）→ 进长任务池
        result = await offload_long(
            git_repo.pull_ref, str(staging), ref, discard_local=discard_local
        )
        if not result.get("ok"):
            # 副本丢弃即可：线上目录没被动过，所以文案里说清「未换入」
            result["message"] = (
                str(result.get("message") or "更新失败") + "（未换入：线上目录保持更新前状态）"
            )
            return False, result
        # 换入前兜底：副本里没有构建产物就地补一次（只 build，不动连接/profile——
        # 更新本就不该换掉本地关联库的连接凭据）
        if not (staging / "target" / "mdl.json").is_file():
            ok_build, out_build = await offload_long(_run_wren, staging, "context", "build")
            if ok_build:
                result["message"] = f"{result.get('message', '')}；已补构建 target/mdl.json"
            else:
                result["message"] = (
                    f"{result.get('message', '')}；⚠️ 更新后该库没有 target/mdl.json 且补构建失败："
                    f"{out_build}——请点「构建」重试，否则该库的查询会失败"
                )
        return True, result

    async with _project_lock(str(project.resolve())):
        ok, payload = await _stage_and_swap(project, "pull", _prepare)
    return ok, payload


async def git_adopt(request: Request):
    """把**已建好的**本地语义库接上远程仓库并拉取内容（「接入 Git」入口）。

    body：`{repo_url(必填), ref(可选，分支/tag), discard_local(默认 false),
    build(默认 true), target_db(可选)}`。

    与 `from_git`（从零 clone 出一个新库）的区别：这里的目录**已经存在**（通常是
    「新建」出来的空骨架）——接管 = 备份本地 + 干净 clone + 整目录交换，之后该库就有
    `.git` 了，走普通的「更新」（`git_pull`）。两条入口共用 `_adopt_git_into`。

    护栏：只接管 workspace 内的目录（与 delete 同法）；已是 Git 仓库直接 400（它有
    「更新」按钮，不该再走覆盖语义）；本地有自建内容时回 200 + `code="local_content"`
    等用户确认（前端据此渲染确认勾选），**确认前不动任何文件**。
    """
    require_admin(request)
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    if not _is_within(project, _workspace_root()):
        return json_response({"error": "仅支持接管 workspace 内的语义库"}, status=403)
    if (project / ".git").exists():
        return json_response(
            {"error": "该语义库已是 Git 仓库，请用卡片上的「更新」按钮"}, status=400
        )

    data = await parse_body(request)
    repo_url = str(data.get("repo_url", "") or "").strip()
    if not repo_url:
        return json_response({"error": "repo_url 必填"}, status=400)

    try:
        result = await _adopt_git_into(
            project,
            repo_url,
            str(data.get("ref", "") or "").strip(),
            discard_local=_truthy(data.get("discard_local")),
            target_db=str(data.get("target_db", "") or "").strip(),
            build=_truthy(data.get("build", True)),
        )
    except _AdoptError as e:
        return json_response({"ok": False, "error": str(e), **e.extra}, status=e.status)
    return json_response(result)


async def get_git_ssh_key(request: Request):
    """返回后端用于 ssh:// git 推送的 SSH 公钥。

    供前端「复制 SSH 公钥」配置到 GitLab/码云等账号的 SSH Keys 页。
    密钥持久化在 AGENT_DATA_ROOT/.ssh（重建容器不丢），公钥随推送不变，
    同一账号所有仓库通用，无需每加一个仓库配一次。
    """
    # P1：未登录即可读（顺带触发 ensure_ssh_key 在服务器上生成密钥）。
    # 公钥本身不是机密（就是要给人贴到 GitLab 的），所以门槛取「登录即可」而非
    # 管理员——语义库只读浏览对已授权用户是开放的，不该让复制公钥变成 admin 专属。
    from api._common import require_user
    require_user(request)

    from agent.utils import git_repo

    priv = git_repo.ensure_ssh_key()
    pub = priv.with_suffix(".pub")
    try:
        pubkey = pub.read_text(encoding="utf-8").strip()
    except OSError as e:
        return json_response({"error": f"读取 SSH 公钥失败: {e}"}, status=500)
    if not pubkey:
        return json_response({"error": "SSH 公钥为空（需先在容器内生成密钥）"}, status=500)

    return json_response({"ok": True, "pubkey": pubkey, "path": str(pub)})


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/wren-projects", list_wren_projects, methods=["GET"]),
    Route("/api/wren-projects/create", create_project, methods=["POST"]),
    Route("/api/wren-projects/local", associate_local, methods=["POST"]),
    Route("/api/wren-projects/from-git", from_git, methods=["POST"]),
    Route("/api/wren-projects/{name}", delete_project, methods=["DELETE"]),
    Route("/api/wren-projects/{name}/introspect", introspect_project, methods=["POST"]),
    Route("/api/wren-projects/{name}/generate-models", generate_models, methods=["POST"]),
    Route("/api/wren-projects/{name}/knowledge/template", knowledge_template, methods=["GET"]),
    Route("/api/wren-projects/{name}/knowledge/save", save_knowledge, methods=["POST"]),
    Route("/api/wren-projects/{name}/open-directory", open_directory, methods=["POST"]),
    Route("/api/wren-projects/{name}/push", push_to_git, methods=["POST"]),
    Route("/api/wren-projects/{name}/build", build_project, methods=["POST"]),
    Route("/api/wren-projects/{name}/validate", validate_project, methods=["POST"]),
    Route("/api/wren-projects/{name}/summary", summary_project, methods=["GET"]),
    Route("/api/wren-projects/{name}/knowledge/read", read_knowledge, methods=["GET"]),
    Route("/api/wren-projects/{name}/knowledge/ai-generate", ai_generate_knowledge, methods=["POST"]),
    Route("/api/wren-projects/{name}/git-status", git_status, methods=["GET"]),
    Route("/api/wren-projects/{name}/git-refs", git_refs, methods=["GET"]),
    Route("/api/wren-projects/{name}/git-pull", git_pull, methods=["POST"]),
    Route("/api/wren-projects/{name}/git-adopt", git_adopt, methods=["POST"]),
    Route("/api/git-ssh-key", get_git_ssh_key, methods=["GET"]),
]
