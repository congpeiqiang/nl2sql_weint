"""离线实验 API —— 把 run_experiment 包装为后台任务 + 状态轮询。

覆盖「前端离线测试」需求：数据集（Langfuse badcase/goodcase）、prompt 版本
（Langfuse label）、skill 版本（git ref）、语义库版本（git ref）四维度 A/B，
每臂独立指定（arm = {name, prompt_label?, skill_ref?, semantic_ref?}，缺省走默认）。

路由：
    GET  /api/experiment/datasets                数据集及条数
    GET  /api/experiment/prompt-labels?name=<n>  Langfuse prompt 可用 labels
    GET  /api/experiment/skill-refs              skill git 仓库 tags/branches/HEAD
    GET  /api/experiment/semantic-refs?db=X      语义库 git refs
    POST /api/experiment/runs                    提交实验（后台执行，返回 stamp）
    GET  /api/experiment/runs                    历史 run 列表
    GET  /api/experiment/runs/{stamp}            状态 + 结果（manifest/gate/逐条明细）

后台执行：POST 建 stamp + 初始 status 文件后 asyncio.create_task 跑
_run_orchestrator（asyncio.to_thread 包阻塞逻辑，不卡事件循环）；进度经
on_progress 回调写 {stamp}.status.json，前端轮询读取。cancel 不在 MVP
（subprocess 树取消复杂）；超时由 GET 侧标记 interrupted 兜底。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

_RUN_DIR_NAME = "experiment_runs"
_RUN_TIMEOUT = 7200  # 单 run 超时（秒），超时未刷新 → interrupted
_RUN_TASKS: dict[str, asyncio.Task] = {}

# 参与实验的 Langfuse prompt 名（label A/B 入口）
_PROMPT_NAMES = ("main_system_prompt", "nl2sql_system_prompt")


# ── 内部工具 ───────────────────────────────────────────────


def _run_dir() -> Path:
    from agent.workspace_manager import get_workspace_manager

    return get_workspace_manager().active_workspace / "eval" / _RUN_DIR_NAME


# 北京时区（UTC+8，无 DST）：历史 run 标题时间戳用业务本地时间，避免 0 时区观感
_CST = timezone(timedelta(hours=8))


def _stamp() -> str:
    return datetime.now(_CST).strftime("%Y%m%dT%H%M%S")


def _read_status(stamp: str) -> dict | None:
    p = _run_dir() / f"{stamp}.status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _logger.warning("[experiment] 读 status %s 失败: %s", stamp, e)
        return None


def _write_status(stamp: str, data: dict) -> None:
    p = _run_dir() / f"{stamp}.status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _git_refs(repo_path: Path) -> dict:
    """列 git 仓库 tags / branches / HEAD 短 sha（供 skill / 语义库版本选择）。"""
    def _run(*args: str) -> list[str]:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo_path), *args],
                capture_output=True, text=True, timeout=60,
            )
            return [ln for ln in r.stdout.strip().splitlines() if ln.strip()] if r.returncode == 0 else []
        except Exception:  # noqa: BLE001
            return []

    head = ""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            head = r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return {
        "tags": _run("tag"),
        "branches": _run("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"),
        "head": head,
    }


def _trace_url(trace_id: str) -> str:
    """trace 详情链接（Langfuse UI 追踪页）。"""
    base = os.environ.get("LANGFUSE_BASE_URL", "").rstrip("/")
    pid = os.environ.get("LANGFUSE_PROJECT_ID", "")
    if not base or not pid or not trace_id:
        return ""
    return f"{base}/project/{pid}/traces/{trace_id}"


# ── 元数据枚举 ─────────────────────────────────────────────


async def list_datasets(request: Request):
    """Langfuse 全部数据集及 item 数（不限于 badcase/goodcase）。

    count 取 v4 dataset_items.list 的 meta.total_items（datasets.list 元数据不带
    item 数），每数据集一次查询；读取失败标记 -1。
    """
    from agent.trace.langfuse_client import get_client

    client = get_client()
    out = []
    try:
        ds_resp = client.api.datasets.list(limit=100)
        for d in (ds_resp.data or []):
            name = getattr(d, "name", "") or ""
            if not name:
                continue
            count = -1
            try:
                items = client.api.dataset_items.list(dataset_name=name, page=1, limit=1)
                meta = items.meta if hasattr(items, "meta") else None
                count = int(
                    meta.get("total_items")
                    if isinstance(meta, dict)
                    else (getattr(meta, "total_items", -1) if meta else -1)
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[experiment] 数据集 %s item 数读取失败: %s", name, e)
            out.append({"name": name, "count": count})
    except Exception as e:  # noqa: BLE001
        _logger.warning("[experiment] 列数据集失败: %s", e)
    out.sort(key=lambda x: x["name"])
    return json_response({"datasets": out})


async def prompt_labels(request: Request):
    """列出参与实验的 Langfuse prompt 可用 labels（label A/B 入口）。"""
    name = (request.query_params.get("name") or "").strip()
    names = [name] if name else list(_PROMPT_NAMES)
    from agent.trace.langfuse_client import get_client

    client = get_client()
    out = []
    for n in names:
        try:
            resp = client.api.prompts.list(name=n, limit=100)
            metas = resp.data or []
            labels = sorted({lb for m in metas for lb in (m.labels or [])})
            total_versions = sum(len(m.versions or []) for m in metas)
            out.append({"name": n, "labels": labels, "versions": total_versions, "found": len(metas)})
        except Exception as e:  # noqa: BLE001
            _logger.warning("[experiment] prompt %s 读取失败: %s", n, e)
            out.append({"name": n, "error": str(e)})
    return json_response({"prompts": out})


async def skill_refs(request: Request):
    """skill 版本 git refs（tags/branches/HEAD）。

    GitLab 为真源：`git ls-remote` 直读 origin（TTL 缓存，推 tag 后 ≤30s 出现，无需
    服务器手工 git fetch）。skill tag 命名硬约定 `skills/`|`skills-` 前缀——排除语义
    执行版 v1~v7 等非 skill tag，无需再逐个 ref 查树内容。开发机代码在 git 检出内
    → 额外并入本地已打/已推 refs（离线可用）；生产容器代码目录无 .git → 纯远程。
    origin 取 `SKILLS_GIT_REMOTE`（未配置且无本地仓库 → 空 + note）。
    """
    from agent.utils.skills_versioning import (
        _default_skills_base,
        effective_origin,
        enclosing_repo,
        remote_refs,
        tag_like_skill,
    )

    base = _default_skills_base()
    enc = enclosing_repo(base)
    repo_root = enc[0] if enc else None
    local = _git_refs(Path(repo_root)) if repo_root else None
    origin = effective_origin(repo_root)
    remote = remote_refs(origin) if origin else None

    tags: list[str] = []
    _seen: set[str] = set()
    for src in (remote, local):
        if not src:
            continue
        for t in src.get("tags") or []:
            if tag_like_skill(t) and t not in _seen:
                tags.append(t)
                _seen.add(t)

    branches: list[str] = []
    _seen_b: set[str] = set()
    for src in (remote, local):
        if not src:
            continue
        for b in src.get("branches") or []:
            if b not in _seen_b:
                branches.append(b)
                _seen_b.add(b)
    head = (local or {}).get("head", "") or ""

    if not tags and not branches and not head:
        # 远程可达的仓库必有 ≥1 个分支，正常情况不会走到这；走到只有两种：
        # 远程不可达/未配置（remote=None），或远程仓库空到无任何 ref（note2）。
        if remote is None:
            note = (
                "无法连接 GitLab（SKILLS_GIT_REMOTE 未配置或网络不可达），暂无可用 "
                "skill 版本；配置远程源后重试"
            )
        else:
            note = (
                "GitLab 当前无可选 skill 版本；commit 并推送 skills/ 或 skills- 前缀的 "
                "tag 后下拉自动出现（≤30s），无需在服务器手工 git fetch"
            )
        return json_response({"tags": [], "branches": [], "head": "", "note": note})
    return json_response({"tags": tags, "branches": branches, "head": head})


async def semantic_refs(request: Request):
    """指定库的语义库 git refs（tags/branches/HEAD）。

    版本真源 = 该语义库仓库（正在服务的项目目录）自己的 origin：`git ls-remote` 直读
    （TTL 缓存，语义库在设置里 push 新 tag 后 ≤30s 下拉自动出现，无需服务器手工
    fetch）。本地仓库 refs 并入（dev / 已 fetch 的版本）。本地目录未绑 git → 只出本地
    refs（通常为空）+ note。
    """
    db = (request.query_params.get("db") or "").strip()
    if not db:
        return json_response({"error": "db 必填（如 ?db=chinook_aliyun）"}, status=400)
    from agent.utils.semantic_db import get_detector
    from agent.utils import git_repo
    from agent.utils.skills_versioning import remote_refs

    path = get_detector().project_path_for(db)
    if not path:
        return json_response({"error": f"数据库 {db} 未建模（无语义库项目）"}, status=404)
    repo = Path(path)
    local = _git_refs(repo)
    origin = ""
    try:
        ok, out = git_repo._run(["remote", "get-url", "origin"], cwd=str(repo), timeout=15)
        origin = out.strip() if ok else ""
    except Exception:  # noqa: BLE001
        origin = ""
    remote = remote_refs(origin) if origin else None

    tags: list[str] = []
    _seen: set[str] = set()
    for src in (remote, local):
        if not src:
            continue
        for t in src.get("tags") or []:
            if t not in _seen:
                tags.append(t)
                _seen.add(t)
    branches: list[str] = []
    _seen_b: set[str] = set()
    for src in (remote, local):
        if not src:
            continue
        for b in src.get("branches") or []:
            if b not in _seen_b:
                branches.append(b)
                _seen_b.add(b)
    head = (local or {}).get("head", "") or ""

    if not tags and not branches and not head:
        # git 仓库必有 ≥1 个分支，走到这只剩两种：本地目录未绑 git / 远程不可达且本地无 ref
        if origin:
            note = (
                "无法连接该语义库的远程仓库（网络不可达或未授权），本地亦无已 fetch 的 "
                "git refs；修复远程后推 tag ≤30s 自动出现"
            )
        else:
            note = (
                "该语义库是本地目录（未绑定 Git 远程），暂无可用版本；先在「语义库管理」"
                "里推送到 Git 再选版本"
            )
        return json_response({"tags": [], "branches": [], "head": "", "note": note})
    return json_response({"tags": tags, "branches": branches, "head": head})


# ── 实验编排 ───────────────────────────────────────────────


async def create_run(request: Request):
    """提交离线实验：body {datasets, dataset_limit?, arms[], judge?, threshold?}。"""
    body = await parse_body(request)
    datasets = body.get("datasets") or ["badcase"]
    if isinstance(datasets, str):
        datasets = [datasets]
    datasets = [str(d) for d in datasets if d]
    if not datasets:
        return json_response({"error": "datasets 不能为空"}, status=400)

    raw_arms = body.get("arms")
    if not isinstance(raw_arms, list) or not raw_arms:
        return json_response({"error": "arms 必填（每臂 {name, prompt_label?, skill_ref?, semantic_ref?}）"}, status=400)
    norm_arms: list[dict] = []
    for i, a in enumerate(raw_arms):
        if not isinstance(a, dict):
            continue
        norm_arms.append(
            {
                "name": str(a.get("name") or f"arm{i}"),
                "prompt_label": str(a.get("prompt_label") or ""),
                "skill_ref": str(a.get("skill_ref") or ""),
                "semantic_ref": str(a.get("semantic_ref") or ""),
            }
        )
    if not norm_arms:
        return json_response({"error": "arms 为空"}, status=400)
    names = [a["name"] for a in norm_arms]
    if len(set(names)) != len(names):
        return json_response({"error": "arm 名重复（name 需唯一）"}, status=400)

    stamp = _stamp()
    status = {
        "stamp": stamp,
        "status": "running",
        "stage": "queued",
        "progress": {"total": len(norm_arms), "done": 0, "current": ""},
        "arms": norm_arms,
        "request": {
            "datasets": datasets,
            "dataset_limit": int(body.get("dataset_limit") or 0),
            "judge": bool(body.get("judge")),
            "threshold": float(body.get("threshold") or 0.05),
        },
        "error": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": "",
    }
    _write_status(stamp, status)
    task = asyncio.create_task(_execute_run(stamp, body))
    _RUN_TASKS[stamp] = task
    return json_response(
        {"ok": True, "stamp": stamp, "status_url": f"/api/experiment/runs/{stamp}"}
    )


def _preflight_arms(arms: list[dict]) -> str:
    """逐臂物化 skill_ref 与 semantic_ref；返回错误描述，全过 → 空串。

    物化失败（tag 不存在 / GitLab 不可达 / 名字不合前缀 / 库未建模或未绑 git）→
    显式报错，不让 worker 静默退化跑「当前版本」得出看似成功实则错误的 A/B 结果。
    预检同时把物化目录备好，worker 子进程走 marker 快路径零网络。

    semantic_ref 形态：`db=ref`（UI）或 `db=ref,db2=ref2`（多库）；无 `=` 的裸 ref
    运行时本来就不生效（override 解析跳过），预检同样跳过。
    """
    from agent.utils.skills_versioning import materialize_skills_ref
    from agent.utils.semantic_db import materialize_semantic_ref

    for a in arms:
        if not isinstance(a, dict):
            continue
        ref = str(a.get("skill_ref") or "").strip()
        if ref and materialize_skills_ref(ref) is None:
            return (
                f"skill_ref <{ref}> 物化失败：tag 不存在 / GitLab 不可达 / "
                "名字不含 skills/ 或 skills- 前缀，请检查后重试"
            )
        sem = str(a.get("semantic_ref") or "").strip()
        for part in sem.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            db, sref = (s.strip() for s in part.split("=", 1))
            if not db or not sref:
                continue
            if materialize_semantic_ref(db, sref) is None:
                return (
                    f"semantic_ref <{part}> 物化失败：库「{db}」未建模 / 无语义库项目，"
                    "或该 ref 本地与远程都不存在（本地目录未绑 git 无法取版本），请检查后重试"
                )
    return ""


async def _execute_run(stamp: str, body: dict) -> None:
    """后台执行：skill_ref 预检 → 装载查询集 → orchestrator（spawn worker）→ 状态落盘。"""
    import agent.eval.run_experiment as rex

    def on_progress(prog: dict) -> None:
        cur = _read_status(stamp) or {}
        cur["stage"] = prog.get("stage", cur.get("stage", "running_arms"))
        cur["progress"] = prog
        _write_status(stamp, cur)

    try:
        datasets = body.get("datasets") or ["badcase"]
        if isinstance(datasets, str):
            datasets = [datasets]
        datasets = [str(d) for d in datasets if d] or ["badcase"]
        # manifest/history 展示用：单数据集 = 原名；多数据集 = 逗号连接
        dataset_label = ",".join(datasets) if len(datasets) > 1 else datasets[0]
        run_dir = _run_dir() / stamp
        run_dir.mkdir(parents=True, exist_ok=True)
        out_dir = run_dir / "out"
        arms_file = run_dir / "arms.json"
        arms_file.write_text(json.dumps(body.get("arms") or [], ensure_ascii=False), encoding="utf-8")

        ns = SimpleNamespace(
            queries="",  # 纯 dataset 装载（前端数据集多选）；--queries 文件模式暂不开放
            dataset=dataset_label,
            datasets=datasets,
            dataset_limit=int(body.get("dataset_limit") or 0),
            from_badcase=False,
            from_badcase_limit=0,
            badcase_status=body.get("badcase_status") or "",
            out_dir=str(out_dir),
            labels=[],
            semantic=[],
            arms=str(arms_file),
            run_name="",
            judge=bool(body.get("judge")),
            threshold=float(body.get("threshold") or 0.05),
            timeout=int(body.get("timeout") or 1800),
        )

        cur = _read_status(stamp) or {}
        cur["stage"] = "preflight"
        _write_status(stamp, cur)

        # ── skill_ref / semantic_ref 预检（物化含网络浅克隆，放线程不卡事件循环）──
        pre_err = await asyncio.to_thread(_preflight_arms, body.get("arms") or [])
        if pre_err:
            cur = _read_status(stamp) or {}
            cur["status"] = "error"
            cur["stage"] = "failed"
            cur["error"] = pre_err
            cur["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write_status(stamp, cur)
            return

        cur = _read_status(stamp) or {}
        cur["stage"] = "loading_dataset"
        _write_status(stamp, cur)

        ns.queries_path = await asyncio.to_thread(rex._prepare_queries, ns)
        rc = await asyncio.to_thread(rex._run_orchestrator, ns, on_progress, stamp)
        cur = _read_status(stamp) or {}
        if rc == 0:
            cur["status"] = "done"
            cur["stage"] = "complete"
        else:
            cur["status"] = "error"
            cur["stage"] = "failed"
            cur["error"] = f"orchestrator 退出码 {rc}"
        cur["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_status(stamp, cur)
    except Exception as e:  # noqa: BLE001
        _logger.error("[experiment] run %s 异常: %s", stamp, e)
        cur = _read_status(stamp) or {}
        cur["status"] = "error"
        cur["stage"] = "failed"
        cur["error"] = f"{type(e).__name__}: {e}"
        cur["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_status(stamp, cur)
    finally:
        _RUN_TASKS.pop(stamp, None)


async def delete_run(request: Request):
    """删除历史 run：manifest / status / 明细目录。running 中的拒绝。"""
    stamp = request.path_params["stamp"]
    task = _RUN_TASKS.get(stamp)
    if task is not None and not task.done():
        return json_response({"error": f"run {stamp} 仍在运行，不能删除"}, status=409)
    st = _read_status(stamp)
    if st and st.get("status") == "running":
        # 进程重启后 _RUN_TASKS 为空但 status 残留 running（僵尸 run）——按 running 拒绝，
        # 避免后台任务（若仍在）写回重建已删文件。
        return json_response({"error": f"run {stamp} 状态为 running，不能删除"}, status=409)

    d = _run_dir()
    mf = d / f"run_{stamp}.json"
    sf = d / f"{stamp}.status.json"
    run_dir = d / stamp
    if not (mf.exists() or sf.exists() or run_dir.exists()):
        return json_response({"error": f"run {stamp} 不存在"}, status=404)

    for p in (mf, sf):
        try:
            p.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[experiment] 删除 %s 失败: %s", p.name, e)
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    _logger.info("[experiment] 删除历史 run %s", stamp)
    return json_response({"ok": True, "deleted": stamp})


async def list_runs(request: Request):
    """历史 run 列表（倒序）。

    manifest（run_*.json）是 orchestrator 完成才落盘，running 阶段只有
    {stamp}.status.json——必须同时扫 status 文件，否则运行中的 run 会从历史
    列表消失，无法点回看进度。
    """
    d = _run_dir()
    runs: dict[str, dict] = {}
    if d.exists():
        # 完成/失败：manifest 有完整信息（gate/arms/dataset）
        for mf in d.glob("run_*.json"):
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
                stamp = str(manifest.get("stamp") or mf.stem[len("run_"):])
                st = _read_status(stamp) or {}
                runs[stamp] = {
                    "stamp": stamp,
                    "status": st.get("status", "done"),
                    "stage": st.get("stage", ""),
                    "dataset": manifest.get("dataset", ""),
                    "arms": manifest.get("arms", []),
                    "gate": manifest.get("gate"),
                    "started_at": st.get("started_at", ""),
                    "finished_at": st.get("finished_at", ""),
                }
            except Exception as e:  # noqa: BLE001
                _logger.warning("[experiment] 读 manifest %s 失败: %s", mf.name, e)
        # running/中断：只有 status.json（manifest 未落盘），gate 未知
        for sf in d.glob("*.status.json"):
            stamp = sf.name[: -len(".status.json")]
            if stamp in runs:
                continue
            try:
                st = json.loads(sf.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                _logger.warning("[experiment] 读 status %s 失败: %s", sf.name, e)
                continue
            req = st.get("request") or {}
            ds = req.get("datasets") or []
            runs[stamp] = {
                "stamp": stamp,
                "status": st.get("status", "running"),
                "stage": st.get("stage", ""),
                "dataset": ",".join(ds) if isinstance(ds, list) else str(ds or ""),
                "arms": st.get("arms", []),
                "gate": None,
                "started_at": st.get("started_at", ""),
                "finished_at": st.get("finished_at", ""),
            }
    ordered = sorted(runs.values(), key=lambda r: r["stamp"], reverse=True)
    return json_response({"runs": ordered})


async def get_run(request: Request):
    """run 状态 + 结果（manifest / gate / 逐条明细）。"""
    stamp = request.path_params["stamp"]
    d = _run_dir()
    st = _read_status(stamp)

    # 超时标记（running 且超过 _RUN_TIMEOUT 未刷新 → interrupted）
    if st and st.get("status") == "running":
        try:
            started = datetime.fromisoformat(str(st.get("started_at", "")))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - started).total_seconds() > _RUN_TIMEOUT:
                st["status"] = "interrupted"
                st["stage"] = "failed"
                st["error"] = "超过超时未完成，标记为 interrupted"
                _write_status(stamp, st)
        except Exception:  # noqa: BLE001
            pass

    manifest: dict | None = None
    mf = d / f"run_{stamp}.json"
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[experiment] 读 manifest %s 失败: %s", stamp, e)

    if st is None and manifest is None:
        return json_response({"error": f"run {stamp} 不存在"}, status=404)

    items: dict[str, list[dict]] = {}
    gate = (manifest or {}).get("gate")
    if manifest:
        for arm in manifest.get("arms", []):
            n = arm.get("name") if isinstance(arm, dict) else arm
            p = d / stamp / "out" / f"exp_{n}.jsonl"
            if p.exists():
                items[str(n)] = [
                    json.loads(line)
                    for line in p.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
        # trace_url 补充（Langfuse 详情链接）
        for recs in items.values():
            for r in recs:
                tid = r.get("trace_id", "")
                r["trace_url"] = _trace_url(tid) if tid else ""

    return json_response(
        {
            "stamp": stamp,
            "status": (st or {}).get("status", "done"),
            "stage": (st or {}).get("stage", ""),
            "arms": (st or {}).get("arms") or (manifest or {}).get("arms", []),
            "progress": (st or {}).get("progress"),
            "error": (st or {}).get("error", ""),
            "started_at": (st or {}).get("started_at", ""),
            "finished_at": (st or {}).get("finished_at", ""),
            "manifest": manifest,
            "items": items,
            "gate": gate,
        }
    )


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/experiment/datasets", list_datasets, methods=["GET"]),
    Route("/api/experiment/prompt-labels", prompt_labels, methods=["GET"]),
    Route("/api/experiment/skill-refs", skill_refs, methods=["GET"]),
    Route("/api/experiment/semantic-refs", semantic_refs, methods=["GET"]),
    Route("/api/experiment/runs", create_run, methods=["POST"]),
    Route("/api/experiment/runs", list_runs, methods=["GET"]),
    Route("/api/experiment/runs/{stamp}", get_run, methods=["GET"]),
    Route("/api/experiment/runs/{stamp}", delete_run, methods=["DELETE"]),
]
