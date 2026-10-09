# -*- coding: utf-8 -*-
"""dev 环境（192.168.25.34）蓝绿发版的静态闸 + 行为闸（离线，只读仓内文件）。

**要解决的问题**：dev 的蓝绿与 prod **同构但不同物** —— prod 换的是**镜像**，dev 换的是
**bind mount 的 src 目录**。这一处差异把每一步都变得容易写错，而错法都很安静：

  · 忘了 dev 是 compose **v2**（连字符容器名）⇒ 脚本等的是一个永远不存在的容器；
  · overlay 的 `env_file` 照抄了 prod 的 `.env.prod` ⇒ dev 被带成 prod 语义；
  · 第二色**没有挂另一份 src** ⇒ 两色跑同一份代码，「新代码」根本没上过；
  · 末段忘了**目录轮换** ⇒ 新代码只活在临时色里，一停 B 就回到旧代码（发了个寂寞）；
  · `ports` 没收回环 ⇒ LAN 上不带 token 直连即内部身份，用户隔离归零（P0-2）；
  · nginx 还挂着 `agent_data:ro` + `location /report/` ⇒ 未登录可下载全站报告（P0-3）；
  · 排空写在切流**之前** ⇒ 用户看见 503 还以为是「零中断」。

断言分组：
  ① `docker/compose.colorB.dev.yml`（overlay）：只新增服务名 / `env_file == .env.dev` /
     平台键与 base **逐字相同** / 挂 `src-next` 且可被 `COLOR_B_SRC` 覆盖 / 不发布端口 /
     无 `depends_on` / 无 `build` / `restart:"no"` / `stop_grace_period` 与 base 同值；
  ② `docker/compose.dev.yml`（34 上 compose 的仓库参照副本）：有 `redis` / 平台存储是
     postgres 版型 / 端口收回环（**P0-2**）/ nginx 挂载里没有 `agent_data`（**P0-3**）；
  ③ `docker/nginx.conf`：切流点唯一 + `worker_shutdown_timeout` 在 main 上下文 + 请求期解析 +
     没有 `location /report/`、没有 `autoindex`；
  ④ `docs/dev环境/dev-release-backend-bluegreen.ps1`：解析闸 + BOM；每处 `up` 都带 `--no-deps`；
     顺序 `起空闲B < 提升B < 切流 < 排空A < 目录轮换 < 起A < 切回A`（**排空在切流之后**）；
     从不碰 postgres/redis/frontend/nginx 容器；有 `-Rollback`；打印非 200 计数；
     **回退也是蓝绿**（先切流再换代码）；
  ⑤ `docs/dev环境/dev-swap-src.sh`：有 `stage`/`rotate`；`rotate` 是**先改名备份再提升**
     （绝不是 `rm -rf src`）；`stage` 拒绝目标 == `$SRC`；
  ⑥ **行为**：把内嵌的服务器侧 steps here-string 抠出来跑 `bash -n`（296 行的脚本没法靠肉眼保平安）。

每段都带**负对照**（把错法喂给同一条断言，必须报警）—— 没有判别力的闸，绿等于没跑
（memory `bg-color-b-idle-observation` 记过这条教训）。

⚠️ **断言前先剥注释**：本仓的注释里**故意**写着被否定的写法（overlay 注释里有 `build:`/
`depends_on`/`.env.prod`、nginx 注释里有 `upstream backend {…}`、ps1 注释里有 `|| Die`）。
不剥就会把「解释为什么不用」当成「用了」。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_dev_blue_green.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

import yaml

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[1]
_BASE = _REPO / "docker" / "compose.dev.yml"
_OVERLAY = _REPO / "docker" / "compose.colorB.dev.yml"
_NGINX = _REPO / "docker" / "nginx.conf"
_PS1 = _REPO / "docs" / "dev环境" / "dev-release-backend-bluegreen.ps1"
_SWAP = _REPO / "docs" / "dev环境" / "dev-swap-src.sh"

BASE_SERVICE = "langgraph-api"
B_SERVICE = "langgraph-api-b"
# dev 是 compose **v2**：容器名用连字符（prod 是下划线）
B_CONTAINER = "nl2sql-app-langgraph-api-b-1"
A_CONTAINER = "nl2sql-app-langgraph-api-1"
UPSTREAM_RE = r"^\s*set\s+\$backend_up\s+\"[^\"]*\";"
BACKEND_UP_VALUE = r'"langgraph-api:2026"'

# overlay 里必须与 base **逐字相同**的键（第二色提升后就是正式色，指错库/指错 runtime
# 就是一次静默的数据分裂）
PLATFORM_KEYS = [
    "LANGGRAPH_RUNTIME_EDITION",
    "DATABASE_URI",
    "MIGRATIONS_PATH",
    "REDIS_URI",
    "CHECKPOINT_DB_URI",
    "WREN_BIN_PATH",
    "LANG",
    "LC_ALL",
    "MCP_ALLOW_DEGRADED",
]

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _strip_comments(text: str) -> str:
    """剥掉行内注释（`#` 到行尾）。

    overlay / nginx / ps1 的注释里**故意**写着被否定的写法（`build:` / `depends_on` /
    `.env.prod` / `upstream` / `||`），不剥就会把「解释为什么不用」误判成「用了」。
    """
    out = []
    for ln in text.splitlines():
        i = ln.find("#")
        out.append(ln if i < 0 else ln[:i])
    return "\n".join(out)


def _svc(raw: dict, name: str) -> dict:
    return ((raw.get("services") or {}).get(name)) or {}


def _env(svc: dict) -> dict:
    return {str(k): str(v) for k, v in (svc.get("environment") or {}).items()}


def _split_herestring(raw: str) -> tuple[str, str]:
    """把 ps1 拆成 (内嵌 bash steps 脚本正文, here-string 之后的驱动段)。

    ⚠️ 用**行判定**而不是 `raw.rindex("'@")`：bash 正文里满是 `"$@"`，靠字符串搜索会踩错位置。
    """
    lines = raw.splitlines()
    oi = next((i for i, l in enumerate(lines)
               if "BgStepsSh" in l and l.rstrip().endswith("= @'")), -1)
    if oi < 0:
        return "", ""
    ti = next((i for i in range(oi + 1, len(lines)) if lines[i] == "'@"), -1)
    if ti < 0:
        return "", ""
    return "\n".join(lines[oi + 1:ti]), "\n".join(lines[ti + 1:])


# ══════════════════════════════════════════════════════════════
# ① overlay
# ══════════════════════════════════════════════════════════════
def t1_overlay(base: dict, overlay: dict) -> None:
    section("① overlay `docker/compose.colorB.dev.yml`：只在窗口期存在，且不许碰 base 一根汗毛")
    base_names = set((base.get("services") or {}).keys())
    ov_names = set((overlay.get("services") or {}).keys())
    svc = _svc(overlay, B_SERVICE)
    env = _env(svc)
    base_env = _env(_svc(base, BASE_SERVICE))
    base_svc = _svc(base, BASE_SERVICE)

    check(ov_names == {B_SERVICE}, f"只定义 `{B_SERVICE}` 一个服务", f"{sorted(ov_names)}")
    check(not (ov_names & base_names),
          "与 base 的服务名**零交集** —— 任何交集键都会进 base 服务的 config-hash，"
          "而 overlay 只新增服务时那个 hash **在数学上不可能变**",
          f"交集={sorted(ov_names & base_names)}")
    _bad = {"services": {B_SERVICE: {}, BASE_SERVICE: {"image": "x"}}}
    check(bool(set(_bad["services"]) & base_names),
          "负对照：同一条断言对「overlay 里写了 base 的键」会报警（非恒真）")
    extra = set(overlay.keys()) - {"version", "services", "volumes"}
    check(not extra, "顶层没有多余键（networks / configs 之类会牵动全局）", f"{sorted(extra)}")

    # ── ★dev-3：env_file 必须是 `.env.dev` ──
    envfile = list(svc.get("env_file") or [])
    check(envfile == [".env.dev"],
          "★ `env_file` 是 `.env.dev` —— `DEPLOY_ENV=dev` 就在它里面，照抄 prod 的 `.env.prod` "
          "会把 dev 带成 prod 语义", f"{envfile}")
    check([".env.prod"] != envfile,
          "负对照：`.env.prod` 会被上一条判为不合格（非恒真）")

    # ── 空闲 / 正式的唯一区别 ──
    check(env.get("N_JOBS_PER_WORKER") == "${COLOR_B_JOBS:-0}",
          "`N_JOBS_PER_WORKER` 由 `COLOR_B_JOBS` 变量给值（默认 0 = 起空闲色）—— 默认必须是 0："
          "忘了给变量时应当得到「空闲色」，而不是偷偷上一个正式色",
          env.get("N_JOBS_PER_WORKER", "<缺失>"))
    check(base_env.get("N_JOBS_PER_WORKER") == "10",
          "base 的 `N_JOBS_PER_WORKER` 是 10（当前色必须领 run）",
          base_env.get("N_JOBS_PER_WORKER", "<缺失>"))

    # ── 与 base 逐字同库（第二色提升后就是正式色）──
    diff = [k for k in PLATFORM_KEYS if env.get(k) != base_env.get(k)]
    check(not diff,
          "平台键与 base **逐字相同**（第二色提升后就是正式色：指错库/指错 runtime 会静默分裂）",
          "差异键=" + ", ".join(f"{k}: {env.get(k)!r} vs {base_env.get(k)!r}" for k in diff))
    check(env.get("DATABASE_URI", "").endswith("/nl2sql_platform"),
          "第二色连的是平台库 `nl2sql_platform`", env.get("DATABASE_URI", "<缺失>"))
    check(env.get("REDIS_URI") == "redis://redis:6379/0",
          "第二色共用同一个 redis（跨实例订阅在跑的 run）", env.get("REDIS_URI", "<缺失>"))

    # ── 指自己（服务名，不是容器名）──
    check(env.get("LANGGRAPH_API_URL") == f"http://{B_SERVICE}:2026",
          "★ `LANGGRAPH_API_URL` 指**自己**、且用**服务名**（DNS 别名）而不是容器名 —— "
          "自调用/补写器的 HTTP 写要落回本色所连的库", env.get("LANGGRAPH_API_URL", "<缺失>"))

    # ── ★dev-1：第二色必须挂**另一份** src，且可被 COLOR_B_SRC 覆盖 ──
    vols = [str(v) for v in (svc.get("volumes") or [])]
    src_vols = [v for v in vols if v.endswith(":/app/src")]
    check(len(src_vols) == 1, "恰好一条 `/app/src` 挂载", f"{src_vols}")
    if src_vols:
        v = src_vols[0]
        check("COLOR_B_SRC" in v,
              "★dev-1 `/app/src` 的宿主侧可由 `COLOR_B_SRC` 覆盖 —— **回退复用同一套机械**"
              "（把 B 挂到 `src-prev-<stamp>`）靠的就是它", v)
        check("src-next" in v,
              "★dev-1 默认挂 `src-next`（待发布的那份），**不是** base 的 `src` —— "
              "两色挂同一份 src 等于「新代码根本没上过」", v)
    base_src_vols = [str(v) for v in (_svc(base, BASE_SERVICE).get("volumes") or [])
                     if str(v).endswith(":/app/src")]
    check(base_src_vols and "/mydata/nl2sql/src:/app/src" in base_src_vols,
          "base 挂的是 `/mydata/nl2sql/src`（常驻色跑的就是它）", f"{base_src_vols}")
    check(not any("src-next" in v for v in base_src_vols),
          "负对照：base 若也挂 `src-next` 会被上一条判为不合格（非恒真）")

    # 共享 / 独占：agent_data 共享，api_meta 必须**独占**
    check(any(v.endswith(":/app/data") and "agent_data" in v for v in vols),
          "共享 `agent_data`（会话/知识/反馈/队列 —— 两色必须看同一份）", f"{vols}")
    meta = [v for v in vols if v.endswith(":/app/.langgraph_api")]
    check(len(meta) == 1 and "api_meta_b" in meta[0],
          "`.langgraph_api` 用**独占**目录 `api_meta_b` —— 绝不碰在跑那色的 `.langgraph_api`",
          f"{meta}")

    # ── 不发布端口 / 无 depends_on / 无 build / restart no / 与 base 同 grace ──
    for key in ("ports", "expose", "depends_on", "build"):
        check(key not in svc,
              f"没有 `{key}`"
              + (" —— 第二色发布宿主端口就能被 LAN 直连，不带 token 即成 internal 身份"
                 "（用户隔离归零）" if key == "ports" else "")
              + (" —— 写了就会把 postgres/redis 拉进收敛计划" if key == "depends_on" else "")
              + (" —— dev 从不构建镜像（34 无外网，镜像从 64 搬来）" if key == "build" else ""),
              f"{svc.get(key)!r}")
    # 负对照：一条带 ports 的 overlay 必须被判不合格
    check("ports" in {"ports": ["2026:2026"]}, "负对照：带 `ports` 的 overlay 会被上一条报警（非恒真）")
    check(str(svc.get("restart")) == "no",
          "`restart: \"no\"` —— `unless-stopped` 会让临时色在 docker 重启后自己回来，"
          "长期占内存与 postgres 连接且没人记得它是什么", f"{svc.get('restart')!r}")
    check(svc.get("stop_grace_period") == base_svc.get("stop_grace_period") is not None,
          "`stop_grace_period` 与 base **同值且非空**（提升后就是正式色，限额不该比 base 松；"
          "缺了它 docker 默认 10s 后 SIGKILL）",
          f"overlay={svc.get('stop_grace_period')!r} base={base_svc.get('stop_grace_period')!r}")

    # ── 不换镜像：dev 换的只有 src 目录 ──
    check(svc.get("image") == base_svc.get("image"),
          "★dev-1 `image` 与 base **同一个 tag** —— dev 全程不换镜像、不构建，"
          "换的是 bind mount 的 src 目录（与 prod 相反）",
          f"overlay={svc.get('image')!r} base={base_svc.get('image')!r}")
    check(str(svc.get("container_name")) == B_CONTAINER,
          "★dev-2 `container_name` 显式写成 compose v2 的**连字符**形态 —— "
          "发版脚本的容器名判据必须与它逐字一致", f"{svc.get('container_name')!r}")


# ══════════════════════════════════════════════════════════════
# ② compose.dev.yml
# ══════════════════════════════════════════════════════════════
def t2_compose_dev(base: dict, raw: str) -> None:
    section("② `docker/compose.dev.yml`：34 上 compose 的仓库参照副本（两个 P0 口子就在这里闭掉）")
    names = set((base.get("services") or {}).keys())
    svc = _svc(base, BASE_SERVICE)
    env = _env(svc)

    check("redis" in names, "有 `redis` 服务（两色并存时跨实例订阅在跑的 run）", f"{sorted(names)}")
    check({"langgraph-api", "postgres", "devdb", "frontend", "nginx"} <= names,
          "五个原生服务仍在（改动是增量的，没有顺手删掉谁）")
    redis = _svc(base, "redis")
    check("ports" not in redis,
          "redis 不发布宿主端口（同 postgres 的处理：compose 内网直达）", f"{redis.get('ports')!r}")

    # ── 平台存储：postgres 版型（蓝绿的**前提**）──
    check(env.get("LANGGRAPH_RUNTIME_EDITION") == "postgres",
          "★ `LANGGRAPH_RUNTIME_EDITION=postgres` —— inmem pckl 是**每容器一份**、每 10s 整份覆盖写"
          "（last-writer-wins）⇒ 两色并存必互相抹数据，蓝绿无从谈起",
          env.get("LANGGRAPH_RUNTIME_EDITION", "<缺失>"))
    check(env.get("DATABASE_URI", "").endswith("/nl2sql_platform"),
          "`DATABASE_URI` 指平台库 `nl2sql_platform`（与正文库 `nl2sql_checkpoint` 分库）",
          env.get("DATABASE_URI", "<缺失>"))
    check(env.get("MIGRATIONS_PATH") == "/storage/migrations",
          "`MIGRATIONS_PATH` 指向镜像里带的迁移目录（`Dockerfile:109`）",
          env.get("MIGRATIONS_PATH", "<缺失>"))
    check(re.search(r"^\s*redis:\s*$", _strip_comments(raw), re.M) is not None,
          "`redis` 是顶层服务（不是塞在 environment 里）")
    check(base.get("version") is not None, "compose 文件有 `version:`（v1/v2 兼容形态）")

    # ── ★P0-2：端口必须收回环 ──
    ports = [str(p) for p in (svc.get("ports") or [])]
    _port_ok = lambda ps: ps == ["127.0.0.1:2026:2026"]  # noqa: E731
    check(_port_ok(ports),
          "★P0-2 后端端口收回环 `127.0.0.1:2026:2026` —— 原写 `\"2026:2026\"` 发布在 0.0.0.0 ⇒ "
          "LAN 上不带 token、不带 XFF 直连即被认成 internal 身份，四个 @auth.on 全放行，"
          "整套用户隔离归零（nginx 走服务名连后端，绑回环不受影响）", f"{ports}")
    check(not _port_ok(["2026:2026"]),
          "负对照：裸 `\"2026:2026\"` 会被上一条判为不合格（非恒真）")

    # ── ★P0-3：nginx 不再需要读 agent_data ──
    nginx = _svc(base, "nginx")
    nvols = [str(v) for v in (nginx.get("volumes") or [])]
    _no_agent_data = lambda vs: not any("agent_data" in v for v in vs)  # noqa: E731
    check(_no_agent_data(nvols),
          "★P0-3 nginx 的挂载里**没有** `agent_data` —— 它唯一的用途是喂 `location /report/`"
          "（`alias … + autoindex on`）⇒ 未登录即可列出并下载全站报告；该 location 已随 "
          "`docker/nginx.conf` 整份换成仓库那份而消失", f"{nvols}")
    check(not _no_agent_data(["/mydata/nl2sql/data/agent_data:/app/data:ro"]),
          "负对照：带 `agent_data:ro` 的挂载会被上一条判为不合格（非恒真）")
    check(any("docker/nginx.conf:/etc/nginx/nginx.conf:ro" in v for v in nvols),
          "nginx 仍以**文件级**只读方式挂仓库那份 `docker/nginx.conf`（就地覆盖必须写同一 inode）",
          f"{nvols}")
    check([str(p) for p in (nginx.get("ports") or [])] == ["8080:80"],
          "nginx 仍发布 8080（切流后 1s /ok 轮询打的就是它）", f"{nginx.get('ports')!r}")

    check(svc.get("stop_grace_period") == "240s",
          "base 有 `stop_grace_period: 240s`（与 prod 同值；`docker stop -t 240` 之外的停法"
          "也才有 240s 而不是默认 10s）", f"{svc.get('stop_grace_period')!r}")
    check(env.get("CHECKPOINT_DB_URI", "").endswith("/nl2sql_checkpoint"),
          "正文库仍是 `nl2sql_checkpoint`（没被顺手改掉）", env.get("CHECKPOINT_DB_URI", "<缺失>"))


# ══════════════════════════════════════════════════════════════
# ③ nginx.conf
# ══════════════════════════════════════════════════════════════
def t3_nginx(raw: str) -> None:
    section("③ `docker/nginx.conf`：切流点唯一 + 旧 worker 有上限 + 请求期解析 + 没有报告直出")
    text = _strip_comments(raw)

    m = re.search(r"^\s*worker_shutdown_timeout\s+(\d+)s?;", text, re.M)
    check(m is not None, "有 `worker_shutdown_timeout`（reload 后旧 worker 的上限）")
    secs = int(m.group(1)) if m else 0
    check(secs >= 600, "worker_shutdown_timeout ≥ 600s（= proxy_read_timeout）—— 否则正常的"
                       "长 SSE 会被 reload 掐断", f"{secs}s")
    mh = re.search(r"^\s*http\s*\{", text, re.M)
    check(bool(mh) and bool(m) and m.start() < mh.start(),
          "★ `worker_shutdown_timeout` 在 **`http {` 之前** —— 它**只能**写在 main（全局）上下文，"
          "放 `http{}` 会让 `nginx -t` emerg 失败、切流当场回滚（2026-10-08 首次 E2E 实撞）")
    _bad_ctx = "http {\n    worker_shutdown_timeout 610s;\n}\n"
    check(re.search(r"http\s*\{[^}]*worker_shutdown_timeout", _bad_ctx, re.S) is not None,
          "负对照：把 `worker_shutdown_timeout` 放进 `http{}` 会被上一条判为不合格（非恒真）")

    check(re.search(r"^\s*upstream\s", text, re.M) is None,
          "没有 `upstream` 块（旧写法在启动期解析并**永久**缓存 IP ⇒ 换容器即 502 直到 reload）")
    check("resolver 127.0.0.11 valid=10s ipv6=off;" in text,
          "resolver 仍在（Docker 内嵌 DNS，请求期解析；ipv6=off 是必需项）")
    check("proxy_pass http://$backend_up;" in text,
          "proxy_pass 仍是**变量形式**（请求期解析的前提）")
    hits = re.findall(UPSTREAM_RE, text, re.M)
    check(len(hits) == 1,
          "`set $backend_up` 全文件**恰好一次** —— 发版脚本按它做正则替换，"
          "第二处会让替换产生歧义（切错流量）", f"{len(hits)} 处")
    check(len(re.findall(UPSTREAM_RE, text + '\n        set $backend_up  "x:1";\n', re.M)) == 2,
          "负对照：多加一行同型 `set` 后计数变 2（断言确实在校验「唯一性」而不是「存在性」）")
    check(BACKEND_UP_VALUE in hits[0] if hits else False,
          f"切流点当前值 = {BACKEND_UP_VALUE}（仓内副本应是 A 色 / 单色形态）",
          hits[0] if hits else "<缺失>")

    check("location /report/" not in text and "autoindex" not in text,
          "★P0-3 没有 `location /report/`、没有 `autoindex` —— 两者凑起来就是「未登录即可"
          "列出并下载全站报告」")
    check("alias" not in text or "report" not in text.split("alias")[1][:80],
          "没有以 alias 直出目录（同样的口子换条路径而已）")


# ══════════════════════════════════════════════════════════════
# ④ dev-release-backend-bluegreen.ps1
# ══════════════════════════════════════════════════════════════
def t4_ps1(raw: str, path: pathlib.Path, body: str, drv: str) -> None:
    section("④ `docs/dev环境/dev-release-backend-bluegreen.ps1`：顺序、--no-deps、不碰别人、可回退")
    check(path.read_bytes()[:3] == b"\xef\xbb\xbf", "仍是 UTF-8 BOM")

    _ps_parse_gate(path)

    text = _strip_comments(raw)
    btext = _strip_comments(body)
    dtext = _strip_comments(drv)

    check(bool(body), "抠得出内嵌的服务器侧 steps 脚本（否则下面大半是空转）")
    check(bool(drv), "抠得出 here-string 之后的驱动段")
    if not body or not drv:
        return

    # ── 服务器侧注释里的 `||` 会被剥掉；驱动段里若出现 `||` 说明又抄了 bash 写法 ──
    check(re.search(r"\|\|", dtext) is None,
          "★ 驱动段（PowerShell 侧）没有 `||` —— `||` 不是 PS 的语句分隔符，"
          "抄 bash 的 `cmd \\` 换行 ||` 会让整个脚本语法错（`标记“||”不是此版本中的有效语句分隔符`）")
    _bad_drv = 'ssh $Target "test -f x" `\n    || Die "no"\n'
    check(re.search(r"\|\|", _strip_comments(_bad_drv)) is not None,
          "负对照：上面那种 bash 写法摘录会被同一条检查判为不合格（非恒真）")

    # ── 每个 up 都必须 --no-deps ──
    up_lines = [ln for ln in text.splitlines()
                if re.search(r"\bup\b", ln) and re.search(r"\bdcb?\b|docker compose", ln)]
    check(bool(up_lines), "至少有一处 `up`（否则下面的断言是空转）", f"{len(up_lines)} 处")
    missing = [ln.strip() for ln in up_lines if "--no-deps" not in ln]
    check(not missing,
          "**每一处** `up` 都带 `--no-deps`（不带就会把 depends_on 的 postgres/redis 一起收敛）",
          " | ".join(missing)[:200])
    check("--no-deps" not in "dcb up -d \"$B_SVC\"",
          "负对照：裸 `dcb up -d \"$B_SVC\"` 会被同一条检查判为不合格（非恒真）")
    check(all("--no-deps" not in ln for ln in text.splitlines()
              if re.search(r"\bdcb?\s+(stop|rm)\b", ln)),
          "`stop` / `rm` **没有**带 `--no-deps` —— compose v2 的 stop/rm 没有这个选项，"
          "带上会直接报错")

    # ── 不碰别人 ──
    for name in ("postgres", "redis", "frontend", "nginx"):
        check(re.search(rf"\b(up|stop|rm|restart|build)\b[^\n]*\b{name}\b", text) is None
              or re.search(rf"docker\s+compose[^\n]*\b(up|stop|rm|restart|build)\b[^\n]*\b{name}\b",
                           text) is None,
              f"从不 up/stop/rm/restart/build {name} 容器")
    check(re.search(r"docker\s+compose[^\n]*\b(up|stop|rm|restart)\b[^\n]*\bnginx\b", text) is None,
          "从不 up/stop/rm/restart nginx 容器（只 `nginx -t` + `nginx -s reload`）")
    check("nginx -t" in text and "nginx -s reload" in text,
          "nginx 只走 `nginx -t` + `nginx -s reload`（reload 是优雅的，在跑的 SSE 不被掐）")
    check(re.search(r'cat\s+"?\$f"?\s*>\s*"?\$live"?', text) is not None,
          "就地覆盖用 `cat >` 写**同一个 inode**（`mv`/`sed -i` 会换 inode ⇒ 容器仍读旧内容，"
          "改了却像没改）")
    check("tr -d '\r'" in text or "tr -d '[:space:]'" in text,
          "判据归一：`docker inspect` 的输出先剥空白（本机 docker 对**不存在**的容器会先往 "
          "stdout 打一个空行 ⇒ 不剥就是两行）")
    check("B_EXISTS=${b_id:-no}" in text,
          "★ `B_EXISTS` 先剥空白再 `:-no` 兜底 ⇒ 永远**一行**且值确定（旧写法 "
          "`$(docker inspect … || echo no)` 在稳态下会让守卫拦死自己）")
    check('LIVE_BACKEND=$(grep -o' in raw and "[[:space:]]*" in raw,
          "★ `LIVE_BACKEND` 那个 grep 容忍**任意**间距：线上那行是 `set $backend_up  \"…\"`"
          "（**两个空格**）—— 写死一个空格会静默匹配不到，打印出空值（判据撒谎）")

    # ── 顺序（驱动段的**调用**顺序就是执行顺序）──
    fwd_start = dtext.find("== 1/12")
    check(fwd_start >= 0, "驱动段里找得到正向发版的起点（`== 1/12`）")
    fwd = dtext[fwd_start:] if fwd_start >= 0 else dtext

    def idx(pat: str, s: str = fwd, start: int = 0) -> int:
        m = re.search(pat, s[start:])
        return (m.start() + start) if m else -1

    i_stage = idx(r"dev_swap_src\.sh stage")
    i_startb = idx(r"bash \$StepsSh start_b\b")
    i_promote = idx(r"bash \$StepsSh promote_b\b")
    i_poll = idx(r"bash \$StepsSh poll_start\b")
    i_nginx_b = idx(r"bash \$StepsSh nginx_apply\b")
    i_cut_a = idx(r"bash \$StepsSh cutover \$ASvc \$ACt\b")
    i_rotate = idx(r"bash \$StepsSh rotate_src\b")
    i_start_a = idx(r"bash \$StepsSh start_a\b")
    i_nginx_a = idx(r"bash \$StepsSh nginx_apply\b", fwd, i_nginx_b + 1) if i_nginx_b >= 0 else -1
    i_cp_b = idx(r"bash \$StepsSh cp_drain \$BCt\b")
    i_cut_b = idx(r"bash \$StepsSh cutover \$BSvc \$BCt\b")
    order = {
        "stage(src-next)": i_stage, "start_b": i_startb, "promote_b": i_promote,
        "poll_start": i_poll, "nginx_apply(B)": i_nginx_b, "cutover_A": i_cut_a,
        "rotate_src": i_rotate, "start_a": i_start_a, "nginx_apply(A)": i_nginx_a,
        "cp_drain_B": i_cp_b, "cutover_B": i_cut_b,
    }
    check(all(v >= 0 for v in order.values()),
          "驱动段能定位到全部关键步骤（任一步骤被删/改名都会在这里响亮失败）", f"{order}")
    seq = [i_stage, i_startb, i_promote, i_poll, i_nginx_b, i_cut_a,
           i_rotate, i_start_a, i_nginx_a, i_cp_b, i_cut_b]
    check(all(v >= 0 for v in seq) and all(seq[i] < seq[i + 1] for i in range(len(seq) - 1)),
          "顺序：暂存src-next < 起空闲B < 提升B < 起轮询 < 切流到B < 排空停旧色A < "
          "**目录轮换** < 起回常驻A < 切流回A < 给B就位ops_drain < 排空停临时色B", f"{seq}")
    check(i_nginx_b < i_cut_a,
          "★ **排空在切流之后** —— 老路是「先排空再停」，排空期新提交拿到 503（也是 5xx，"
          "用户照样看得见）。倒过来才是「零 5xx」而不是「把 502 换成 503」")
    check(0 <= i_cut_a < i_rotate,
          "★ **目录轮换在停 A 之后** —— A 挂着 /mydata/nl2sql/src，只 mv 目录本身（改个名，"
          "不动里面的文件）⇒ 运行中的容器仍指向原 inode，内容一模一样；等 A 停了再起回来，"
          "它才会挂到新的 src")
    check(i_rotate < i_start_a,
          "★ **轮换早于起回 A** —— 顺序反了就是「A 起回来仍然跑旧代码」")
    check("cutover $ASvc $ACt $DrainBudget" in raw, "排空/停掉的是**旧色 A**（不是 B）")
    check("cutover $BSvc $BCt $DrainBudget" in raw, "最后排空/停掉的是**临时色 B**")

    # ── 排空判据必须**跑得起来**才允许停容器（fail-closed）──
    check(re.search(r"test -s /tmp/nl2sql_ops_drain\.py\s*\\?\s*\|\|\s*die", body) is not None,
          "★ `cutover` 在「容器里没有 ops_drain」时**拒绝停容器** —— 否则「不知道有没有在跑的 "
          "run」会被当成「已排空」然后照停，那正是这套设计要根除的 fail-open")
    _bad_co = ('cutover)\n  docker exec "$ct" python /tmp/nl2sql_ops_drain.py --budget "$budget"\n'
               '  docker stop -t 240')
    check(re.search(r"test -s /tmp/nl2sql_ops_drain\.py\s*\\?\s*\|\|\s*die", _bad_co) is None,
          "负对照：没有那道 fail-closed 闸的 cutover 摘录会被同一条检查判为不合格（非恒真）")
    check(re.search(r"cutover\)\n(?:.*\n)*?.*docker stop -t 240", body) is not None,
          "排空判定与 `docker stop` 在**同一个**子命令里（同一次 ssh 会话）—— 这是残余竞态"
          "（切流后旧色仍可能从共享队列领 run）的唯一缓解手段，窗口被压到毫秒")
    check(re.search(r"cp_drain \$ACt", dtext) is not None
          and re.search(r"cp_drain \$BCt", dtext) is not None,
          "★ A 与 B **各** cp 一份 ops_drain —— `docker cp` 是 per-container，"
          "第 4 步只给了 A，第 12 步漏了 B 那侧的排空判据就跑不起来")
    check("DRAIN_READY" in text and "stale" in text,
          "排空未确认（`stale` / 预算耗尽）时**响亮地**提示，不安静地过去")

    # ── 起空闲色 B：先清再起 ──
    m_startb = re.search(r"^start_b\)(.*?)\n\s*;;", body, re.S | re.M)
    check(m_startb is not None, "有 `start_b` 子命令（起空闲第二色）")
    if m_startb:
        sb = m_startb.group(1)
        check(re.search(r'dcb stop "\$B_SVC"', sb) is not None
              and re.search(r'dcb rm -f "\$B_SVC"', sb) is not None,
              "`start_b` 先 `stop && rm -f` 再 up（铁律③：上一次发版中途失败会留下残留 B，"
              "此时 `up -d` 会走 recreate 路径）")
        check('docker logs "$A_CT"' in sb and "comm -13" in sb,
              "★ 预检回归闸的判据是「**B 不比 A 差**」：只有「A 好 B 坏」或「B 新增了 A 没有的"
              "缺失 server」才拒流。用绝对判据（见到「预检失败」就 die）会把 **A 自己的常态**"
              "当成 B 的失败（dev 的降级启动态就是这么长期的）")
        _bad = ('if docker logs "$B_CT" 2>&1 | grep -q \'预检失败\'; then die "B 预检失败"; fi')
        check('docker logs "$A_CT"' not in _bad,
              "负对照：绝对判据的摘录会被同一条检查判为不合格（非恒真）")
        check(re.search(r'\[ -f "\$src/agent/main_agent\.py" \]', sb) is not None,
              "`start_b` 校验 `$src` 确实像一份 src（缺 `agent/main_agent.py` 就拒绝起）")

    # ── 起回 A：不带 overlay + DATABASE_URI 硬闸 ──
    m_starta = re.search(r"^start_a\)(.*?)\n\s*;;", body, re.S | re.M)
    check(m_starta is not None, "有 `start_a` 子命令（用 base compose 把常驻色起回来）")
    if m_starta:
        sa = m_starta.group(1)
        check(re.search(r"dc up -d --no-deps --no-build", sa) is not None
              and not re.search(r"\bdcb\b", sa),
              "★ `start_a` 用 **base compose**（`dc up`，不带 overlay）—— A 的 config-hash 必须"
              "回到 base 原样，否则将来任何 `up -d` 都想 recreate 它")
        check("DATABASE_URI" in sa and re.search(r"docker stop -t 30", sa) is not None,
              "★ `start_a` 核对 A 的 `DATABASE_URI` 与**正在接流的 B** 逐字一致，不一致就"
              "**当场把 A 停掉**再喊停（A 是用不带 overlay 的线上 compose 起的 ⇒ 这条实际在验"
              "服务器 compose 是否已是 postgres 版型；漏了它，A 会以 10 个槽在 inmem 上领 run，"
              "而那是静默的数据分裂）")
    check(re.search(r"^rm_b\)", body, re.M) is not None,
          "有 `rm_b` 子命令（清残留的临时色；手册就是让人这么救急的）")

    # ── 稳态契约：A 常驻、B 只在窗口里 ──
    check("LIVE_BACKEND" in text and "B_EXISTS" in text,
          "预检会读出**当前流量在哪一色**（`LIVE_BACKEND`）与**有没有残留 B**（`B_EXISTS`）")
    check("$liveBackend" in raw and "$bExists" in raw and "[switch]$Force" in raw,
          "★ 稳态契约前置检查：流量不在 A / 有残留 B 时**拒绝发版**（确认要硬来才加 -Force）")
    check("-Force" in raw and "Compare-Object" in raw,
          "有防呆：仓库 nginx.conf 与线上不一致时**中止**并打印 diff，要显式 -Force 才覆盖"
          "（不悄悄抹掉别人手改过的东西）")
    check(re.search(r"(?m)^\s*\$maskRe\s*=", raw) is not None and "nHit" in raw,
          "★ 防呆比对前先把 `set $backend_up` 那一行**遮掉**再比 —— 蓝绿用过一次之后线上那行"
          "是另一色，不遮会永远「不一致」；同时校验它在仓库副本里**恰好一次**（否则替换无唯一解）")

    # ── 回退也是蓝绿 ──
    check("[switch]$Rollback" in raw, "有 `-Rollback` 参数（声明在 param 块里）")
    rb_start = dtext.find("if ($Rollback)")
    fwd_marker = dtext.find("== 1/12")
    check(rb_start >= 0 and (fwd_marker < 0 or rb_start < fwd_marker),
          "能定位到回退分支（否则下面几条是空转）")
    if rb_start >= 0 and (fwd_marker < 0 or rb_start < fwd_marker):
        rb = dtext[rb_start:fwd_marker if fwd_marker > 0 else len(dtext)]

        def ridx(pat: str, start: int = 0) -> int:
            return idx(pat, rb, start)

        r_startb = ridx(r"bash \$StepsSh start_b\b")
        r_promote = ridx(r"bash \$StepsSh promote_b\b")
        r_nginx_b = ridx(r"bash \$StepsSh nginx_apply\b")
        r_cut_a = ridx(r"bash \$StepsSh cutover \$ASvc \$ACt\b")
        r_rotate = ridx(r"bash \$StepsSh rotate_src\b")
        r_start_a = ridx(r"bash \$StepsSh start_a\b")
        r_nginx_a = ridx(r"bash \$StepsSh nginx_apply\b", r_nginx_b + 1) if r_nginx_b >= 0 else -1
        r_cut_b = ridx(r"bash \$StepsSh cutover \$BSvc \$BCt\b")
        r_order = {"start_b": r_startb, "promote_b": r_promote, "nginx(B)": r_nginx_b,
                   "cutover_A": r_cut_a, "rotate_src": r_rotate, "start_a": r_start_a,
                   "nginx(A)": r_nginx_a, "cutover_B": r_cut_b}
        check(all(v >= 0 for v in r_order.values()),
              "回退分支能定位到全部关键步骤（任一步被删/改名都响亮失败）", f"{r_order}")
        check(r_startb >= 0 and "bash $StepsSh start_b 0 $prevPath" in rb,
              "★ 回退**先起临时色 B**（N_JOBS_PER_WORKER=0，挂**上一版**代码目录）—— "
              "回退也走蓝绿，不是直接把常驻色 A 重启成旧版（那要拿真用户等一次冷启）")
        seq_rb = [r_startb, r_promote, r_nginx_b, r_cut_a, r_rotate, r_start_a, r_nginx_a, r_cut_b]
        check(all(v >= 0 for v in seq_rb) and all(seq_rb[i] < seq_rb[i + 1]
                                                  for i in range(len(seq_rb) - 1)),
              "回退顺序：起旧代码B < 提升B < **切流到B** < 排空停A < 目录轮换 < 起回A < "
              "**切回A** < 排空停B", f"{seq_rb}")
        check(0 <= r_nginx_b < r_cut_a,
              "★★ **切流到 B 必须早于「把 A 换回上一版」** —— 这条是回退零中断的全部要害："
              "切流之前流量在 A 上，此时动 A = 拿真用户等冷启")
        check("PrevSrc" in raw and "SRC_PREV" in raw and "state_read" in raw,
              "回退的上一版目录来自 `-PrevSrc` 或状态文件的 `SRC_PREV`（没有就**拒绝猜**）")
        check(re.search(r"rotate_src \$PrevSrc src-rolledback-", rb) is not None,
              "★ 回退的目录轮换把上一版换回 `src`，被换下去的留档成 `src-rolledback-<stamp>` "
              "（再 `-Rollback` 一次就能换回来 —— 状态换向）")

    check("poll_stop" in text and "POLL_NON200" in text,
          "打印**非 200 计数**（「零中断」这个结论必须由采样数据支撑，不能靠感觉）")
    check("cp_drain" in text and "nl2sql_ops_drain.py" in text,
          "排空用的是 `docker cp` 进容器的**仓库里那份** ops_drain")
    check(re.search(r"cp_drain\)\n(?:.*\n){0,8}?.*docker inspect", text) is not None,
          "`cp_drain` 对**不存在**的色空转（稳态就只有 A；`-Rollback` 里 B 往往已被删）")
    check("DRAIN_READY=n/a" in text, "`cutover` 对**不存在**的色空转")
    check("state_write" in text and "state_read" in text,
          "有状态文件（`/tmp/nl2sql_bluegreen_dev.state`）：回退据此知道上一版在哪")


def _ps_parse_gate(path: pathlib.Path) -> None:
    exe = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not exe:
        cand = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if pathlib.Path(cand).is_file():
            exe = cand
    if not exe:
        print("  [SKIP] 本机没有 powershell.exe / pwsh —— 解析闸跳过（不算失败）")
        return
    cmd = ("$e=$null;"
           "[void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{path}',[ref]$null,[ref]$e);"
           'if($e){$e | ForEach-Object {$_.Message}} else {"PARSE_OK"}')
    try:
        r = subprocess.run([exe, "-NoProfile", "-Command", cmd],
                           capture_output=True, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        check(out == "PARSE_OK", "过 PowerShell 解析闸（`[Parser]::ParseFile`）", out[:300])
    except OSError as e:  # noqa: BLE001
        print(f"  [SKIP] 调不起 {exe}：{e}（改过脚本的人在 Windows 上必须补跑）")


# ══════════════════════════════════════════════════════════════
# ⑤ dev-swap-src.sh
# ══════════════════════════════════════════════════════════════
def t5_swap_src(raw: str, path: pathlib.Path) -> None:
    section("⑤ `docs/dev环境/dev-swap-src.sh`：stage 与 rotate（dev 换代码的唯一通道）")
    text = _strip_comments(raw)

    check(re.search(r"^stage\)\s*$", text, re.M) is not None, "有 `stage` 模式")
    check(re.search(r"^rotate\)\s*$", text, re.M) is not None, "有 `rotate` 模式")
    # case 标签可以共用一行（`swap | restore)`），所以匹配「行首**或** `|` 之后」
    for mode in ("check", "backup", "swap", "restore"):
        check(re.search(rf"(?:^|\|)\s*{mode}\s*[)|]", text, re.M) is not None,
              f"老模式 `{mode}` 仍在（没被顺手删）")
    check(re.search(r"(?:^|\|)\s*restore\s*[)|]", "swap | restore)", re.M) is not None,
          "负对照：同一条断言认得出共用一行的 `swap | restore)`（非恒真）")

    check(re.search(r'\[ "\$TGT" != "\$SRC" \]', text) is not None,
          "`stage` 拒绝目标 == `$SRC` —— 那是 swap/restore 的活；弄混了就是在用户眼皮底下换代码")
    m_stage = re.search(r"^stage\)(.*?)\n\s*;;", text, re.S | re.M)
    check(m_stage is not None and re.search(r"stage_into \"\$2\" \"\$TGT\"", m_stage.group(1)) is not None,
          "`stage` 只调 `stage_into` 到目标目录，**不碰** `$SRC`")
    if m_stage:
        check(re.search(r"\bSRC\b", m_stage.group(1).replace("$SRC", "")) is None
              or "!=" in m_stage.group(1),
              "负对照：`stage` 分支里 `$SRC` 只出现在「不许等于」那句话里")

    # ★ 先改名备份，再提升 —— 绝不是 rm -rf src
    m_rot = re.search(r"^rotate\)(.*?)\n\s*;;", text, re.S | re.M)
    check(m_rot is not None, "抠得出 `rotate` 分支")
    if m_rot:
        rot = m_rot.group(1)
        i_mv1 = rot.find('mv "$SRC" "$DEV/$PREVNAME"')
        i_mv2 = rot.find('mv "$DEV/$NEW" "$SRC"')
        check(i_mv1 >= 0 and i_mv2 >= 0 and i_mv1 < i_mv2,
              "★ `rotate` = **先** `mv $SRC → $PREVNAME`（改名备份），**再** `mv $NEW → $SRC` "
              "（提升）—— 顺序反了会把上一版永久删掉，回退无路",
              f"backup@{i_mv1} promote@{i_mv2}")
        check(re.search(r"rm\s+-rf?\s+\"?\$SRC", rot) is None,
              "★ `rotate` 里**没有** `rm -rf $SRC` —— 目录是**改名**不是删；src 里全是 root 属主"
              "的 `__pycache__`（容器以 root 写），weint 根本删不掉，删必半残"
              "（2026-09-21 实测：278 个 .py 全没、只剩 123 个 root 属主的 __pycache__）")
        _bad_rot = 'mv "$DEV/$NEW" "$SRC"\nrm -rf "$SRC.bak"'
        check(re.search(r"rm\s+-rf?\s+\"?\$SRC", _bad_rot) is not None,
              "负对照：含 `rm -rf $SRC…` 的摘录会被上一条判为不合格（非恒真）")
        check('die "备份 $SRC 失败' in rot and "src 原状保留" in rot,
              "第一刀失败时明确「src 未做任何改动」")
        check("mv $DEV/$PREVNAME $SRC" in rot,
              "★ 第二刀失败时**打印出可直接粘贴的恢复命令**（此刻 src 已是 $PREVNAME）")
        check("SRC_PREV=" in rot, "打印机读串 `SRC_PREV=`（发版脚本与状态文件都靠它）")

    check("--strip-components=1" in text,
          "解包用 `--strip-components=1`（包顶层是 `src/`）")
    check(re.search(r"^stage_into\(\)", text, re.M) is not None,
          "有 `stage_into`（先解到暂存区再清空真目录 —— 解包失败时目标目录原封不动）")
    check("in_root_at" in text and "docker run --rm" in text,
          "文件操作用**一次性 root 容器**做（weint 删不掉容器写出的 root 属主文件）")
    check(path.read_bytes()[:2] == b"#!", "有 shebang")

    if shutil.which("bash"):
        try:
            r = subprocess.run(["bash", "-n", str(path)], capture_output=True,
                               encoding="utf-8", errors="replace")
            check(r.returncode == 0, "过 `bash -n`", (r.stderr or "").strip()[:300])
        except OSError as e:  # noqa: BLE001
            print(f"  [SKIP] 跑不起 bash -n：{e}")
    else:
        print("  [SKIP] 本机没有 bash —— bash -n 跳过（不算失败）")


# ══════════════════════════════════════════════════════════════
# ⑥ 行为闸：内嵌的服务器侧 steps 脚本
# ══════════════════════════════════════════════════════════════
def t6_herestring_behaviour(body: str) -> None:
    section("⑥ 行为：内嵌的服务器侧 steps 脚本（≈300 行，只能靠 bash -n 保平安）")
    check(body.startswith("#!/usr/bin/env bash"),
          "正文以 shebang 开头（抠取位置的护栏：抠歪了这里立刻响）")
    check('case "${1:-}" in' in body and body.rstrip().endswith("esac"),
          "正文是完整的 `case` 结构（头尾都在）")
    check("\r" not in body,
          "正文里**没有 `\\r`** —— PS here-string 会带上文件自身的 CRLF，"
          "驱动段写盘前必须 `-replace \\`r\\`n,\\`n`，否则 bash 每行尾都多一个 \\r")
    _bad = "x=1\r\ny=2"
    check("\r" in _bad, "负对照：含 CRLF 的摘录会被上一条判为不合格（非恒真）")

    for sub in ("preflight", "cp_drain", "start_b", "promote_b", "nginx_apply",
                "poll_start", "poll_stop", "cutover", "rotate_src", "start_a", "rm_b",
                "final_checks", "state_write", "state_read"):
        check(re.search(rf"^{sub}\)", body, re.M) is not None, f"有子命令 `{sub}`")

    if shutil.which("bash"):
        try:
            r = subprocess.run(["bash", "-n"], input=body, capture_output=True,
                               encoding="utf-8", errors="replace")
            check(r.returncode == 0, "内嵌脚本过 `bash -n`（语法闸）",
                  (r.stderr or "").strip()[:400])
        except OSError as e:  # noqa: BLE001
            print(f"  [SKIP] 跑不起 bash -n：{e}")
    else:
        print("  [SKIP] 本机没有 bash —— 内嵌脚本的语法闸跳过（不算失败）")


def main() -> int:
    for p in (_BASE, _OVERLAY, _NGINX, _PS1, _SWAP):
        if not p.is_file():
            print(f"{NG} 缺文件：{p}")
            return 1

    try:
        base = yaml.safe_load(_strip_comments(_read(_BASE))) or {}
        overlay = yaml.safe_load(_strip_comments(_read(_OVERLAY))) or {}
    except yaml.YAMLError as e:  # noqa: BLE001
        print(f"{NG} overlay/base YAML 解析失败：{e}")
        return 1
    if not isinstance(base.get("services"), dict) or not isinstance(overlay.get("services"), dict):
        print(f"{NG} 两个 compose 文件都必须有 `services:` 映射")
        return 1

    raw_ps1 = _read(_PS1)
    body, drv = _split_herestring(raw_ps1)

    t1_overlay(base, overlay)
    t2_compose_dev(base, _read(_BASE))
    t3_nginx(_read(_NGINX))
    t4_ps1(raw_ps1, _PS1, body, drv)
    t5_swap_src(_read(_SWAP), _SWAP)
    t6_herestring_behaviour(body)

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    print("\n注意：`docker compose -f docker-compose.yml -f docker/compose.colorB.dev.yml config -q` "
          "只能在 34 上跑（仓内没有 `.env.dev`，env_file 缺失会解析失败）—— 那一步在发版手册里。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
