"""Wren 语义库管理 API 路由（随 langgraph API 同进程/同端口提供）。

对标 `docs/wren-semantic-library-management.md` 设计稿，提供语义库（Wren 项目
目录）的管理：列表（含 git 元信息 / 构建状态 / 关联库 / MDL 概览计数）、关联本地
目录、从 git 拉取、删除、重新构建、校验、MDL 概览。

「语义库」本身没有独立实体，本质是磁盘上的 Wren 项目目录，靠 `db_config.json`
里某条库的 `wren_project` 路径字段与数据库配置关联。

关键约束（重启门槛）：新增/删除语义库后，Wren MCP 工具（`wrenai_*`）要**重启
后端进程**才生效——`_sub_tools` 是进程启动时构建的单例。`SemanticDbDetector`
缓存可 `invalidate()` 即时刷新（前端 semantic 标记），但 server 本身必须重启。
所有写接口在响应里返回 `requires_restart: true` 提示前端。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body
from api.db_config import _scan_wren_projects
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
def _ensure_git_identity(cwd: str) -> None:
    """确保 git 用户信息已配置（用于 commit）。直接设置，已存在则覆盖，不会报错。"""
    from agent.utils import git_repo

    git_repo._run(["config", "--local", "user.name", "NL2SQL Agent"], cwd=cwd)
    git_repo._run(["config", "--local", "user.email", "agent@nl2sql.local"], cwd=cwd)


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
    project_path: Path, target_db: str, overwrite_connection: bool = True
) -> tuple[bool, str]:
    """拉取后的构建链路：生成 connection → 注册 profile → set-profile → context build。

    返回 (ok, 各步骤结果摘要)；ok 仅取决于 context build 是否成功。target/mdl.json
    已存在时跳过构建（拉取的仓库常带构建产物，无需连库重建）。步骤间 best-effort：
    profile add 失败（如已存在同名 profile）不阻断后续 set-profile / build。

    - `overwrite_connection=True`（默认，且给了 target_db）：用 target_db 的本地
      连接信息覆盖生成 connection 文件并注册 profile。
    - `overwrite_connection=False` 或未给 target_db：保留仓库自带连接，直接按
      wren_project.yml 的 profile 构建。
    """
    if (project_path / "target" / "mdl.json").is_file():
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
    try:
        from agent.utils.semantic_db import get_detector

        get_detector().invalidate()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[wren_semantic] detector invalidate 失败: %s", e)


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


# ── 路由 ─────────────────────────────────────────────────
async def list_wren_projects(request: Request):
    items = [_project_detail(p["path"]) for p in _scan_wren_projects()]
    return json_response({"projects": items})


async def associate_local(request: Request):
    """场景 A：关联本地已有目录到某条库配置。"""
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
    return json_response(
        {"ok": True, "project": _project_detail(str(p.resolve())), "requires_restart": True}
    )


async def from_git(request: Request):
    """场景 B（核心）：clone → 定位项目根 → 生成凭据 → 构建 → 关联。"""
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
        return json_response(
            {"error": f"目标目录已存在: {dest}", "project": _project_detail(str(dest))},
            status=409,
        )

    try:
        git_repo.clone_shallow(repo_url, ref, str(dest))
    except RuntimeError as e:
        return json_response({"error": str(e)}, status=500)

    # 定位项目根：规范约定 wren_project.yml 固定在仓库根
    project_root = dest
    if not (project_root / "wren_project.yml").is_file():
        shutil.rmtree(dest, ignore_errors=True)
        return json_response(
            {"error": "仓库根目录缺少 wren_project.yml（语义库需按规范放在仓库根）"}, status=400
        )

    build_note = ""
    try:
        ok, build_note = _build_with_profile(project_root, target_db, overwrite_connection)
    except ValueError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return json_response({"error": str(e)}, status=400)
    if not ok:
        _logger.warning("[wren_semantic] %s 构建未完成: %s", project_root, build_note)

    # 关联
    if target_db:
        get_store().set_wren_project(target_db, str(project_root.resolve()))
        _invalidate_detector()

    return json_response(
        {
            "ok": True,
            "project": _project_detail(str(project_root.resolve())),
            "build_note": build_note,
            "requires_restart": True,
        }
    )


async def delete_project(request: Request):
    """解绑所有关联 + 删除项目目录（不可逆，需前端二次确认）。"""
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
    for db in _associated_dbs(project):
        get_store().set_wren_project(db, "")
    _invalidate_detector()
    # 删除目录（Windows 上 git 文件可能只读，需要 onexc 处理）
    def _on_rm_error(func, path, exc_info):
        import stat
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception as e2:
            _logger.warning("[delete_project] 删除失败 %s: %s", path, e2)

    try:
        shutil.rmtree(project, onexc=_on_rm_error)
    except TypeError:
        # Python < 3.12 用 onerror
        shutil.rmtree(project, onerror=_on_rm_error, ignore_errors=False)
    except Exception as e:
        _logger.warning("[delete_project] rmtree 失败: %s", e)
    # 验证是否删除成功
    if project.exists():
        _logger.warning("[delete_project] 目录仍存在: %s", project)
        return json_response({"ok": True, "name": name, "warning": "目录未能完全删除，可能被进程占用，请手动删除"})
    return json_response({"ok": True, "name": name})


async def build_project(request: Request):
    """重新构建 MDL（context build + memory index，best-effort）。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    ok, out = _run_wren(project, "context", "build")
    if ok:
        # memory index 失败不影响 build 结果（知识库索引可选）
        idx_ok, idx_out = _run_wren(project, "memory", "index", timeout=600)
        tail = ("；memory index 完成" if idx_ok else f"；memory index 失败: {idx_out}")
        return json_response({"ok": True, "message": "context build 完成" + tail})
    return json_response({"ok": False, "message": f"context build 失败: {out}"}, status=400)


async def validate_project(request: Request):
    """语义库校验：跑 wren context validate，返回错误/告警列表 + MDL 概览。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    ok, out = _run_wren(project, "context", "validate")
    summary = _mdl_summary(project)
    summary["name"] = _read_project_name(project)
    summary["path"] = str(project.resolve())
    return json_response({"ok": ok, "message": out, "summary": summary})


async def summary_project(request: Request):
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)
    summary = _mdl_summary(project)
    summary["name"] = _read_project_name(project)
    summary["path"] = str(project.resolve())
    summary["associated_dbs"] = _associated_dbs(project)
    return json_response({"summary": summary})


# ── 新建语义库流程（create → introspect → generate-models → knowledge → build → push）──

async def create_project(request: Request):
    """Step 1：创建空项目骨架。"""
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
    (dest / "knowledge" / "rules" / "general.md").write_text(rules_general_md(), encoding="utf-8")

    # 关联数据库 → 语义库
    get_store().set_wren_project(db_name, str(dest.resolve()))
    _invalidate_detector()

    return json_response({
        "ok": True,
        "project": _project_detail(str(dest.resolve())),
        "path": str(dest.resolve()),
    })


async def introspect_project(request: Request):
    """Step 2：连接数据库提取表结构。"""
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
        result = introspect_database(cfg)
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
        result = introspect_database(cfg)
    except Exception as e:
        return json_response({"error": f"数据库内省失败: {e}"}, status=500)

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

    from agent.utils.wren_templates import all_templates

    return json_response({"templates": all_templates()})


async def save_knowledge(request: Request):
    """Step 3b：保存业务知识文件。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    data = await parse_body(request)
    files: dict[str, str] = data.get("files", {}) or {}
    if not files:
        return json_response({"error": "files 必填"}, status=400)

    saved: list[str] = []
    for rel_path, content in files.items():
        # 安全：路径必须在项目目录内，且不允许 .. 逃逸
        safe = rel_path.replace("\\", "/").strip("/")
        if ".." in safe or safe.startswith("/"):
            _logger.warning("[wren_semantic] 拒绝非法路径: %s", rel_path)
            continue
        target = (project / safe).resolve()
        if not _is_within(target, project):
            _logger.warning("[wren_semantic] 路径逃逸拒绝: %s", rel_path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        saved.append(safe)

    return json_response({"ok": True, "saved": saved})


async def open_directory(request: Request):
    """Step 3c：在系统资源管理器中打开项目目录。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    import platform
    import subprocess as _sp

    path = str(project.resolve())
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            _sp.run(["open", path], check=True)
        else:
            _sp.run(["xdg-open", path], check=True)
    except Exception as e:
        return json_response({"error": f"打开目录失败: {e}"}, status=500)

    return json_response({"ok": True, "path": path})


async def push_to_git(request: Request):
    """Step 5：推送语义库至 Git 远程仓库。"""
    from agent.utils import git_repo

    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    data = await parse_body(request)
    remote_url = str(data.get("remote_url", "") or "").strip()
    branch = str(data.get("branch", "") or "main").strip()
    tag = str(data.get("tag", "") or "").strip()
    commit_message = str(data.get("commit_message", "") or "初始化语义库").strip()

    if not remote_url:
        return json_response({"error": "remote_url 必填"}, status=400)

    try:
        git_repo.validate_repo_url(remote_url)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)

    cwd = str(project)
    steps: list[str] = []

    try:
        # 若没有 .git，初始化
        if not (project / ".git").exists():
            _logger.info("[push_to_git] git init: %s", cwd)
            ok, out = git_repo._run(["init"], cwd=cwd)
            if not ok:
                return json_response({"error": f"git init 失败: {out}"}, status=500)
            steps.append("git init")
            # git init 创建的默认分支名可能不是目标分支名（如 master vs main），
            # 用 -m 重命名确保后续 push 时分支名匹配
            _logger.info("[push_to_git] git branch -m %s", branch)
            git_repo._run(["branch", "-m", branch], cwd=cwd)
            _logger.info("[push_to_git] git remote add origin %s", remote_url)
            ok, out = git_repo._run(["remote", "add", "origin", remote_url], cwd=cwd)
            if not ok:
                return json_response({"error": f"git remote add 失败: {out}"}, status=500)
            steps.append("remote add origin")
        else:
            _logger.info("[push_to_git] 已有 .git，set-url origin %s", remote_url)
            ok, out = git_repo._run(["remote", "set-url", "origin", remote_url], cwd=cwd)
            if not ok:
                _logger.warning("[push_to_git] set-url 失败: %s", out)

        _logger.info("[push_to_git] _ensure_git_identity")
        _ensure_git_identity(cwd)

        _logger.info("[push_to_git] git add -A")
        ok, out = git_repo._run(["add", "-A"], cwd=cwd)
        if not ok:
            return json_response({"error": f"git add 失败: {out}"}, status=500)
        steps.append("git add -A")

        _logger.info("[push_to_git] git commit -m '%s'", commit_message)
        ok, out = git_repo._run(["commit", "-m", commit_message, "--allow-empty"], cwd=cwd)
        if not ok and "nothing to commit" not in out.lower() and "nothing added" not in out.lower():
            return json_response({"error": f"git commit 失败: {out}"}, status=500)
        steps.append("git commit")

        _logger.info("[push_to_git] git push -u origin %s --force", branch)
        ok, out = git_repo._run(
            ["push", "-u", "origin", branch, "--force"],
            cwd=cwd, timeout=300,
        )
        if not ok:
            _logger.warning("[push_to_git] 首次 push 失败: %s", out)
            # 如果是远程仓库不存在，尝试用 gh CLI 自动创建（仅 GitHub）
            if "repository not found" in out.lower() and "github.com" in remote_url:
                created = _auto_create_github_repo(remote_url)
                if created:
                    steps.append("auto-create remote repo")
                    _logger.info("[push_to_git] 自动创建仓库成功，重试 push")
                    ok, out = git_repo._run(
                        ["push", "-u", "origin", branch, "--force"],
                        cwd=cwd, timeout=300,
                    )
            if not ok:
                git_repo._run(["remote", "set-url", "origin", remote_url], cwd=cwd)
                _logger.info("[push_to_git] 重试 push")
                ok, out = git_repo._run(
                    ["push", "-u", "origin", branch, "--force"],
                    cwd=cwd, timeout=300,
                )
        if not ok:
            return json_response({"error": f"git push 失败: {out}"}, status=500)
        steps.append(f"push to origin/{branch}")

        if tag:
            _logger.info("[push_to_git] git tag %s", tag)
            git_repo._run(["tag", "-f", tag], cwd=cwd)
            ok, out = git_repo._run(["push", "origin", tag, "--force"], cwd=cwd, timeout=120)
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
    """读取语义库的业务知识文件，返回结构化数据供前端表单编辑。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    import yaml as _yaml

    result = {
        "glossary": [],
        "metrics": [],
        "rules": [],
        "sql_patterns": [],
        "caveats": [],
    }

    # glossary.yml
    gf = project / "knowledge" / "glossary.yml"
    if gf.is_file():
        try:
            data = _yaml.safe_load(gf.read_text(encoding="utf-8")) or {}
            result["glossary"] = data.get("terms", []) or []
        except Exception as e:
            _logger.debug("[read_knowledge] glossary.yml 解析失败: %s", e)

    # metrics.yml
    mf = project / "knowledge" / "metrics.yml"
    if mf.is_file():
        try:
            data = _yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
            result["metrics"] = data.get("metrics", []) or []
        except Exception as e:
            _logger.debug("[read_knowledge] metrics.yml 解析失败: %s", e)

    # rules/*.md
    rules_dir = project / "knowledge" / "rules"
    if rules_dir.is_dir():
        for md_file in sorted(rules_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
                result["rules"].append({
                    "name": md_file.stem,
                    "category": "general",
                    "description": content.strip(),
                    "scope": "global",
                    "file": f"knowledge/rules/{md_file.name}",
                })
            except Exception as e:
                _logger.debug("[read_knowledge] rules/%s 解析失败: %s", md_file.name, e)

    # sql/*.yml
    sql_dir = project / "knowledge" / "sql"
    if sql_dir.is_dir():
        for yml_file in sorted(sql_dir.glob("*.yml")):
            try:
                data = _yaml.safe_load(yml_file.read_text(encoding="utf-8")) or {}
                patterns = data.get("patterns", [])
                for p in patterns:
                    p["file"] = f"knowledge/sql/{yml_file.name}"
                    result["sql_patterns"].append(p)
            except Exception as e:
                _logger.debug("[read_knowledge] sql/%s 解析失败: %s", yml_file.name, e)

    # caveats.yml
    cf = project / "knowledge" / "caveats.yml"
    if cf.is_file():
        try:
            data = _yaml.safe_load(cf.read_text(encoding="utf-8")) or {}
            result["caveats"] = data.get("caveats", []) or []
        except Exception as e:
            _logger.debug("[read_knowledge] caveats.yml 解析失败: %s", e)

    return json_response({"ok": True, "knowledge": result})


async def ai_generate_knowledge(request: Request):
    """AI 分析数据库结构，生成业务知识草稿。"""
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
        result = introspect_database(cfg)
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
    """检查语义库的 Git 状态（是否有远程更新）。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    if not (project / ".git").exists():
        return json_response({"ok": True, "is_git": False, "has_updates": False})

    from agent.utils import git_repo

    cwd = str(project)

    # fetch 远程信息
    ok, out = git_repo._run(["fetch", "origin", "--dry-run"], cwd=cwd, timeout=30)
    has_updates = False
    if ok:
        # 比较本地和远程 HEAD
        ok_local, local_head = git_repo._run(["rev-parse", "HEAD"], cwd=cwd)
        ok_remote, remote_head = git_repo._run(["rev-parse", "origin/HEAD"], cwd=cwd)
        if ok_local and ok_remote:
            has_updates = local_head.strip() != remote_head.strip()

    # 检查本地未提交的修改
    ok_status, status_out = git_repo._run(["status", "--porcelain"], cwd=cwd)
    has_local_changes = bool(status_out.strip()) if ok_status else False

    return json_response({
        "ok": True,
        "is_git": True,
        "has_updates": has_updates,
        "has_local_changes": has_local_changes,
        "branch": git_repo.repo_info(str(project)).get("branch", "") if git_repo.repo_info(str(project)) else "",
    })


async def git_pull(request: Request):
    """从远程仓库拉取更新。"""
    name = request.path_params["name"]
    project = _find_project(name)
    if project is None:
        return json_response({"error": f"语义库 '{name}' 不存在"}, status=404)

    if not (project / ".git").exists():
        return json_response({"error": "该语义库不是 Git 仓库"}, status=400)

    from agent.utils import git_repo

    cwd = str(project)

    ok, out = git_repo._run(["pull", "origin"], cwd=cwd, timeout=120)
    if not ok:
        return json_response({"error": f"git pull 失败: {out}"}, status=500)

    return json_response({"ok": True, "message": f"拉取成功: {out}"})


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
    Route("/api/wren-projects/{name}/git-pull", git_pull, methods=["POST"]),
]
