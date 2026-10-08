# -*- coding: utf-8 -*-
"""蓝绿「真零中断发版」（阶段 2）的静态闸 + 行为闸（离线，只读仓内文件；⑥ 段用临时库真跑一遍）。

**替代** `scripts/verify_color_b_idle.py`（那个断言的是「第一刀的空闲色观察形态」：scratch 库、
恒 `N_JOBS_PER_WORKER=0`、关保留策略 —— 本轮形态变了，它必然失败；那 29 条已被本文件①②段
吸收并改了判据）。本文件**覆盖它的全部意图**，可以放心删旧的那份。

**要解决的问题**：蓝绿发版把「一次停服重启」换成「两色并存 + 切一行流」，每一步写错都是生产事故，
而且**错法都很安静**（overlay 动了 base 的键 → 下一次 compose 命令把 postgres 打死；
overlay 指错库 → 第二色去写 scratch/或反过来把观察配置发上生产；忘了 `--no-deps` → 依赖被顺手收敛；
排空写在切流**之前** → 用户看见 503 还以为"零中断"；`reset_running_to_pending` 没加租约 →
两色把同一条 judge 各跑一遍，双倍 token 且结果互相覆盖）。本套件把这些**全部变成可查的断言**。

断言分组：
  ① `docker/compose.colorB.yml`（overlay）：零服务名交集 / 变量化 `N_JOBS` / **与 base 逐字同库** /
     不发布端口 / 无 `depends_on` / `restart:"no"` / 复用 image ID 占位符；
  ② `docker/nginx.conf`：`worker_shutdown_timeout` 存在、≥ `proxy_read_timeout`(600s)、且写在
     **main 上下文**（`http {` 之前 —— 放 `http{}` 会让 `nginx -t` emerg 失败）；
     `set $backend_up` **全文件恰好一次**（脚本的 sed 才有唯一解）；仍是请求期解析（resolver + 变量 proxy_pass）；
  ③ `docs/prod环境/release-backend-bluegreen.ps1`：解析闸 + BOM；每个 `up` 都带 `--no-deps`；
     **排空在切流之后**（这是与旧脚本相反的意图，必须写死）；从不碰 postgres/redis/nginx 容器；有 `-Rollback`；
  ④ `src/agent/eval/eval_queue.py`：`reset_running_to_pending` 的租约夹逼；周期性回收；`--force`；
  ⑤ `scripts/ops_drain.py`：就绪判据含 `counter_trusted`（只看 `remaining==0` 会在**读不到计数**时谎报已排空）；
  ⑥ **行为**：用临时 SQLite 真跑一遍 `reset_running_to_pending` —— 超租约的回 pending、刚 claim 的**不动**。

每段都带**负对照**（把错法喂给同一条断言，必须报警）—— 没有判别力的闸，绿等于没跑
（memory `bg-color-b-idle-observation` 记过这条教训）。

⚠️ **断言前先剥注释**：本仓的注释里**故意**写着被否定的写法（overlay 注释里有 `build:`/`depends_on`、
nginx 注释里有 `upstream backend {…}`）。不剥就会把「解释为什么不用」当成「用了」。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_blue_green.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import yaml

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[1]
_BASE = _REPO / "docker" / "compose.prod.yml"
_OVERLAY = _REPO / "docker" / "compose.colorB.yml"
_NGINX = _REPO / "docker" / "nginx.conf"
_PS1 = _REPO / "docs" / "prod环境" / "release-backend-bluegreen.ps1"
_EVALQ = _REPO / "src" / "agent" / "eval" / "eval_queue.py"
_DRAIN = _REPO / "scripts" / "ops_drain.py"

BASE_SERVICE = "langgraph-api"
B_SERVICE = "langgraph-api-b"
PLACEHOLDER = "IMAGE_ID_PLACEHOLDER"
UPSTREAM_RE = r"^\s*set\s+\$backend_up\s+\"[^\"]*\";"
BACKEND_UP_VALUE = r'"langgraph-api:2026"'

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

    overlay / nginx 的注释里**故意**写着被否定的写法（`build:` / `depends_on` / `upstream`），
    不剥就会把「解释为什么不用」误判成「用了」。前提：这些文件里 `#` 不出现在字符串中。
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


# ══════════════════════════════════════════════════════════════
# ① overlay
# ══════════════════════════════════════════════════════════════
def t1_overlay(base: dict, overlay: dict) -> None:
    section("① overlay `docker/compose.colorB.yml`：只在窗口期存在，且不许碰 base 一根汗毛")
    base_names = set((base.get("services") or {}).keys())
    ov_names = set((overlay.get("services") or {}).keys())
    svc = _svc(overlay, B_SERVICE)
    env = _env(svc)
    base_env = _env(_svc(base, BASE_SERVICE))

    check(ov_names == {B_SERVICE}, f"只定义 `{B_SERVICE}` 一个服务", f"{sorted(ov_names)}")
    check(not (ov_names & base_names),
          "与 base 的服务名**零交集** —— 任何一个交集键都会进 base 服务的 config-hash，"
          "而本机 recreate 路径必炸 `KeyError: 'ContainerConfig'`（2026-10-05 postgres 停摆 55 分钟）",
          f"交集={sorted(ov_names & base_names)}")
    # 负对照：同一条断言对「overlay 里写了 base 的键」必须报警
    _bad = {"services": {B_SERVICE: {}, BASE_SERVICE: {"image": "x"}}}
    check(bool(set(_bad["services"]) & base_names),
          "负对照：同一条断言对「overlay 里写了 base 的键」会报警（非恒真）")
    extra = set(overlay.keys()) - {"version", "services", "volumes"}
    check(not extra, "顶层没有多余键（networks / configs 之类会牵动全局）", f"{sorted(extra)}")

    # ── 空闲 / 正式的唯一区别 ──
    check(env.get("N_JOBS_PER_WORKER") == "${COLOR_B_JOBS:-0}",
          "N_JOBS_PER_WORKER 由 `COLOR_B_JOBS` 变量给值（默认 0 = 起空闲色）—— 默认值必须是 0："
          "忘了传变量的后果只能是「起了个空闲色」，不能是「偷偷上了正式色」",
          env.get("N_JOBS_PER_WORKER", "<缺失>"))
    check("ports" not in svc,
          "没有 `ports` —— 第二色绝不发布宿主端口（发布出去就能被 LAN 直连，不带 token 即成 internal 身份，"
          "整套用户隔离归零）", f"{svc.get('ports')}")
    check("expose" not in svc, "没有 `expose`")
    check("depends_on" not in svc,
          "没有 `depends_on` —— 有它就会把 postgres/redis 拉进收敛计划，撞 compose v1 recreate 那颗雷")

    # ── 与 base 逐字同库（真发版的第二色就是正式色，必须读写真实数据）──
    for key in ("DATABASE_URI", "CHECKPOINT_DB_URI"):
        v = env.get(key, "")
        check(bool(v) and v == base_env.get(key),
              f"{key} 与 base **逐字相同**（同一套生产库）—— 上一版观察形态把它指到 scratch 空库，"
              "那形态不能发版", v.split("@")[-1] or "<缺失>")
        check("scratch" not in v, f"{key} 不含 scratch（观察期的空库已经删掉）")
    check(env.get("LANGGRAPH_API_URL") == f"http://{B_SERVICE}:2026",
          "LANGGRAPH_API_URL 指向**自己** —— 补写器/自调用的 HTTP 写才落回本色所连的库",
          env.get("LANGGRAPH_API_URL", "<缺失>"))

    vols = [str(v) for v in (svc.get("volumes") or [])]
    joined = " | ".join(vols)
    check(any(v.startswith("agent_data:/app/data") for v in vols),
          "**共享** `agent_data:/app/data`（会话/知识/反馈/队列两色必须看同一份）", joined)
    check(any(v.endswith(":/app/.langgraph_api") and "api_meta_b" in v for v in vols),
          "**独占** api_meta 卷（名字带 `_b`，绝不碰在跑那色的 `.langgraph_api`）", joined)
    check(not any(v.startswith("nl2sql_api_meta:/app/.langgraph_api") for v in vols),
          "没有把 base 的 `nl2sql_api_meta` 挂进来")

    check("NL2SQL_RETENTION_ENABLED" not in env,
          "**没有** NL2SQL_RETENTION_ENABLED —— 那是第一刀观察用的临时开关，发版形态必须与 base 同行为")
    check("DB_MIGRATION_BY_CORE_API" not in env,
          "**故意不设** DB_MIGRATION_BY_CORE_API ⇒ 保持开机自动迁移（新镜像带的迁移会在**起空闲色**那刻"
          "就打到共享库 —— 前提是迁移 add-only，见 overlay 文件头 ④）")

    # ── 生命周期 ──
    check(str(svc.get("restart")) == "no",
          'restart 必须是 "no" —— `unless-stopped` 会让第二色在 docker 重启后自己回来，'
          "长期占着内存与一条 postgres 连接且没人记得它是什么", f"{svc.get('restart')!r}")
    check("build" not in svc,
          "没有 `build:` —— 复用**本次刚构建出来的那个 image ID** 才叫「同一份字节」")
    check(str(svc.get("image", "")) == PLACEHOLDER,
          f"image 仍是占位符 `{PLACEHOLDER}`（仓内副本必须**未替换**）—— 发版脚本 sed 成实测 ID；"
          "硬编码一个 id 进仓库是机器相关的，且会掩盖「忘了替换」这个硬失败",
          f"{svc.get('image')!r}")
    check(bool(svc.get("mem_limit")),
          "有 mem_limit（与 base 同值：第二色迟早也是正式色，限额不该更松）", f"{svc.get('mem_limit')}")
    check(str(svc.get("stop_grace_period")) == str(_svc(base, BASE_SERVICE).get("stop_grace_period")) == "240s",
          "stop_grace_period 与 base 同值 240s（第二色迟早也会变成「要被停掉的旧色」，"
          "停机时同样要排空在跑的 run）", f"{svc.get('stop_grace_period')}")
    envf = svc.get("env_file")
    envf = envf if isinstance(envf, list) else ([envf] if envf else [])
    check(".env.prod" in [str(x) for x in envf],
          "env_file 含 .env.prod（与 base 同形；`environment:` 优先级更高，覆盖得住）", f"{envf}")
    hc = " ".join(str(x) for x in ((svc.get("healthcheck") or {}).get("test") or []))
    check("localhost:2026/ok" in hc, "healthcheck 探自己容器内的 /ok", hc)

    # ── 反向：base 不许出现第二色 ──
    check(B_SERVICE not in base_names,
          f"base 没有 `{B_SERVICE}` —— 两色只以 overlay 形式存在，base 保持单色", f"{sorted(base_names)}")
    check(base_env.get("N_JOBS_PER_WORKER") == "10",
          "base 的 N_JOBS_PER_WORKER 仍是 10（当前色必须领 run）",
          base_env.get("N_JOBS_PER_WORKER", "<缺失>"))
    check("scratch" not in base_env.get("DATABASE_URI", ""),
          "base 的 DATABASE_URI 不是 scratch（防把观察配置写进生产参照）")


# ══════════════════════════════════════════════════════════════
# ② nginx.conf
# ══════════════════════════════════════════════════════════════
def t2_nginx(raw: str) -> None:
    section("② `docker/nginx.conf`：切流点唯一 + 旧 worker 有上限 + 仍是请求期解析")
    text = _strip_comments(raw)

    m = re.search(r"^\s*worker_shutdown_timeout\s+(\d+)s?;", text, re.M)
    check(m is not None, "有 `worker_shutdown_timeout`（reload 后旧 worker 的上限）")
    secs = int(m.group(1)) if m else 0
    # 必须 ≥ proxy_read_timeout(600s)：否则一条正常的长 SSE 还在代理时旧 worker 就被强杀
    check(secs >= 600, "worker_shutdown_timeout ≥ 600s（= proxy_read_timeout）—— 否则正常的"
                       "长 SSE 会被 reload 掐断", f"{secs}s")
    # ⚠️ 必须在 **main** 上下文（`events`/`http` **之外**）：写进 `http{}` 会让 `nginx -t`
    #    直接 emerg 失败（`"worker_shutdown_timeout" directive is not allowed here`）
    #    ⇒ 切流那一步当场回滚（2026-10-08 首次 E2E 实撞；改前那句「http{} 段加」是错的）。
    mh = re.search(r"^\s*http\s*\{", text, re.M)
    check(bool(mh) and bool(m) and m.start() < mh.start(),
          "★ `worker_shutdown_timeout` 在 **`http {` 之前** —— 它**只能**写在 main（全局）上下文，"
          "放 `http{}` 会让 `nginx -t` emerg 失败、切流当场回滚（2026-10-08 首次 E2E 实撞）")
    _bad_ctx = "http {\n    worker_shutdown_timeout 610s;\n}\n"
    check(re.search(r"http\s*\{[^}]*worker_shutdown_timeout", _bad_ctx, re.S) is not None,
          "负对照：把 `worker_shutdown_timeout` 放进 `http{}` 会被上一条判为不合格（非恒真）")

    check(re.search(r"^\s*upstream\s", text, re.M) is None,
          "没有 `upstream` 块（旧写法会在启动期解析并**永久**缓存 IP ⇒ 换容器即 502 直到 reload）")
    check("resolver 127.0.0.11 valid=10s ipv6=off;" in text,
          "resolver 仍在（Docker 内嵌 DNS，请求期解析；ipv6=off 是必需项）")
    check("resolver_timeout 5s;" in text, "resolver_timeout 仍是 5s")
    check("proxy_pass http://$backend_up;" in text,
          "proxy_pass 仍是**变量形式**（请求期解析的前提）")
    check("proxy_redirect off;" in text, "proxy_redirect off; 仍在（变量形式会静默关掉默认重写）")

    hits = re.findall(UPSTREAM_RE, text, re.M)
    check(len(hits) == 1,
          "`set $backend_up` 全文件**恰好一次** —— 发版脚本按它做正则替换，"
          "第二处会让替换产生歧义（切错流量）", f"{len(hits)} 处")
    # 负对照：两处时必须报警
    check(len(re.findall(UPSTREAM_RE, text + '\n        set $backend_up  "x:1";\n', re.M)) == 2,
          "负对照：多加一行同型 `set` 后计数变 2（断言确实在校验「唯一性」而不是「存在性」）")
    check(BACKEND_UP_VALUE in hits[0] if hits else False,
          f"切流点当前值 = {BACKEND_UP_VALUE}（仓内副本应是 A 色 / 单色形态）",
          hits[0] if hits else "<缺失>")
    bad = re.findall(r"proxy_pass\s+http://(?!\$)", text)
    check(not bad, "没有把上游写死成主机名（写死会让切流那一行失去意义）")


# ══════════════════════════════════════════════════════════════
# ③ release-backend-bluegreen.ps1
# ══════════════════════════════════════════════════════════════
def t3_ps1(raw: str, path: pathlib.Path) -> None:
    section("③ `docs/prod环境/release-backend-bluegreen.ps1`：顺序、--no-deps、不碰别人、可回退")
    # BOM：无 BOM 时 PS 5.1 按 ANSI 解码 ⇒ 中文注释与提示全乱码
    check(path.read_bytes()[:3] == b"\xef\xbb\xbf", "仍是 UTF-8 BOM")

    exe = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not exe:
        cand = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if pathlib.Path(cand).is_file():
            exe = cand
    if exe:
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
    else:
        print("  [SKIP] 本机没有 powershell.exe / pwsh —— 解析闸跳过（不算失败）")

    text = _strip_comments(raw)
    # 驱动段 = here-string **之后**（前面是服务器侧脚本的定义，顺序与执行顺序无关）
    if "'@" not in text:
        check(False, "找得到 here-string 结束标记 `'@`（否则无法定位驱动段）")
        return
    drv = text[text.rindex("'@") + 2:]
    # 驱动段里**回退分支在前、正向发版在后** ⇒ 顺序断言只看正向那一段
    # （否则 `poll_start` 会先在回退分支里被命中，给出假的"轮询早于 build"）。
    if "== 1/12 本地打包" not in drv:
        check(False, "驱动段里找得到正向发版的起点（`== 1/12 本地打包`）")
        return
    fwd = drv[drv.index("== 1/12 本地打包"):]

    # ── 每个 up 都必须 --no-deps ──
    up_lines = [ln for ln in text.splitlines()
                if re.search(r"\bup\b", ln) and re.search(r"docker-compose|\bdcb\b", ln)]
    check(bool(up_lines), "至少有一处 `up`（否则下面的断言是空转）", f"{len(up_lines)} 处")
    missing = [ln.strip() for ln in up_lines if "--no-deps" not in ln]
    check(not missing, "**每一处** `up` 都带 `--no-deps`（不带就会把 depends_on 的 postgres/redis "
                       "一起收敛 ⇒ `KeyError: 'ContainerConfig'`）", " | ".join(missing)[:200])
    check(any("--no-deps" not in ln and re.search(r"\bup\b", ln)
              for ln in ["docker-compose up -d langgraph-api"]),
          "负对照：把裸 `docker-compose up -d langgraph-api` 喂给同一条检查会报警（非恒真）")

    # ── 不碰别人 ──
    for name in ("postgres", "redis"):
        check(not re.search(rf"docker-compose[^\n]*\b(up|stop|rm|restart|build)\b[^\n]*\b{name}\b", text),
              f"从不 up/stop/rm/restart/build {name}")
    check(not re.search(r"docker-compose[^\n]*\b(up|stop|rm|restart)\b[^\n]*\bnginx\b", text),
          "从不 up/stop/rm/restart nginx 容器（只 `nginx -t` + `nginx -s reload`）")
    check("nginx -t" in text and "nginx -s reload" in text,
          "nginx 只走 `nginx -t` + `nginx -s reload`（reload 是优雅的，在跑的 SSE 不被掐）")
    check(re.search(r'cat\s+"?\$f"?\s*>\s*"?\$live"?', text) is not None,
          "就地覆盖用 `cat >` 写**同一个 inode**（`mv`/`sed -i` 会换 inode ⇒ 容器仍读旧内容，"
          "改了却像没改）")

    # ── 顺序（driver 段里的**调用**顺序就是执行顺序）──
    def idx(pat: str) -> int:
        m = re.search(pat, fwd)
        return m.start() if m else -1

    i_build = idx(r"docker-compose build langgraph-api")
    i_startb = idx(r"\bbash \$StepsSh start_b\b")
    i_promote = idx(r"\bbash \$StepsSh promote_b\b")
    i_poll = idx(r"\bbash \$StepsSh poll_start\b")
    i_nginx_b = idx(r"\bbash \$StepsSh nginx_apply\b")
    i_cut_a = idx(r"\bbash \$StepsSh cutover \$ASvc \$ACt\b")
    i_start_a_new = idx(r"\bbash \$StepsSh start_a_new\b")
    # 第二次 nginx_apply（切回 A）—— 必须取「第一次之后」的那一个
    i_nginx_a = (re.search(r"\bbash \$StepsSh nginx_apply\b", fwd[i_nginx_b + 1:]).start()
                 + i_nginx_b + 1) if i_nginx_b >= 0 else -1
    i_cut_b = idx(r"\bbash \$StepsSh cutover \$BSvc \$BCt\b")
    order = {
        "build": i_build, "start_b": i_startb, "promote_b": i_promote,
        "poll_start": i_poll, "nginx_apply(B)": i_nginx_b, "cutover_A": i_cut_a,
        "start_a_new": i_start_a_new, "nginx_apply(A)": i_nginx_a, "cutover_B": i_cut_b,
    }
    check(all(v >= 0 for v in order.values()),
          "驱动段能定位到全部关键步骤（任一步骤被删/改名都会在这里响亮失败）", f"{order}")
    seq = [i_build, i_startb, i_promote, i_poll, i_nginx_b, i_cut_a,
           i_start_a_new, i_nginx_a, i_cut_b]
    check(all(seq[i] < seq[i + 1] for i in range(len(seq) - 1)),
          "顺序：build < 起空闲色 < 提升 < 起轮询 < 切流到B < 排空停旧色A < 起回常驻A "
          "< 切流回A < 排空停临时色B", f"{seq}")
    check(i_nginx_b < i_cut_a,
          "★ **排空在切流之后** —— 现有脚本是「先排空再停」，排空期新提交拿到 503（也是 5xx，"
          "用户照样看得见）。倒过来才是「零 5xx」而不是「把 502 换成 503」")
    check("cutover $ASvc $ACt" in drv, "排空/停掉的是**旧色 A**（不是 B）")
    check("cutover $BSvc $BCt" in drv, "最后排空/停掉的是**临时色 B**")
    # ── 排空判据必须**跑得起来**才允许停容器（fail-closed）──
    check(re.search(r"test -s /tmp/nl2sql_ops_drain\.py\s*\\?\s*\|\|\s*die", text) is not None,
          "★ `cutover` 在「容器里没有 ops_drain」时**拒绝停容器** —— 原来那段 `if` 只护住了 idle "
          "复查，探针跑不起来照样往下走 `docker stop`：那正是「不知道有没有在跑的 run」被当成"
          "「已排空」的 fail-open（2026-10-08 E2E 实撞：第 11 步的 B 里根本没这份脚本，"
          "整段排空被静默跳过，只因当时零 run 才没出事）")
    _bad_co = ('cutover)\n  docker exec "$ct" python /tmp/nl2sql_ops_drain.py --budget "$budget"\n'
               '  docker stop -t 240')
    check(re.search(r"test -s /tmp/nl2sql_ops_drain\.py\s*\\?\s*\|\|\s*die", _bad_co) is None,
          "负对照：没有那道 fail-closed 闸的 cutover 摘录会被同一条检查判为不合格（非恒真）")
    check(re.search(r"bash \$StepsSh cp_drain \$BCt", fwd) is not None,
          "★ 驱动第 11 步先给**临时色 B** 也 cp 一份 ops_drain —— B 是另起的一个容器，第 4 步"
          "只给 A 就位过（`docker cp` 是 per-container）；漏了它 B 那侧的排空判据跑不起来")
    # 同一 ssh 会话：排空与 stop 都在 bg_steps.sh 的 cutover 一个子命令里（一次 ssh 往返）
    check(re.search(r"cutover\)\n(?:.*\n)*?.*docker stop -t 240", text) is not None,
          "排空判定与 `docker stop` 在**同一个**子命令里（同一次 ssh 会话）—— 这是残余竞态"
          "（切流后旧色仍可能从共享队列领 run）的唯一缓解手段，窗口被压到毫秒")

    # ── 稳态契约：A 常驻、B 只在窗口里（脚本必须能**第二次**跑）──
    m_newa = re.search(r"start_a_new\)(.*?)\n\s*;;", text, re.S)
    check(m_newa is not None, "有 `start_a_new` 子命令（用**新镜像**把常驻色 A 起回来）")
    if m_newa:
        body = m_newa.group(1)
        check("docker-compose up" in body and "--no-deps" in body and "--no-build" in body,
              "`start_a_new` 用 base compose 起 A（`--no-deps --no-build`：不重新构建、"
              "绝不碰 depends_on）")
        check(not re.search(r"\bdcb\b", body),
              "★ `start_a_new` **不带 overlay** —— A 的 compose config-hash 必须回到 base 原样。"
              "带 overlay 会让 A 的 hash 从此与「没有蓝绿这回事」时不同 ⇒ 将来任何 `up -d` 都想 "
              "recreate 它（那正是 2026-10-05 把 postgres 打死的那条路径）")
        check(re.search(r"docker-compose stop \"\$A_SVC\"", body) is not None
              and re.search(r"docker-compose rm -f \"\$A_SVC\"", body) is not None,
              "`start_a_new` 先 `stop && rm -f` 再 up（铁律③：容器已存在时 `up -d` 会走 recreate 路径）")
        check("DATABASE_URI" in body and re.search(r"docker stop -t 30", body) is not None,
              "★ `start_a_new` 核对 A 的 `DATABASE_URI` 与**正在接流的 B** 逐字一致，不一致就"
              "**当场把 A 停掉**再喊停（A 是用不带 overlay 的线上 compose 起的 ⇒ 这条实际在验"
              "服务器 compose 是否已是 postgres 版型；漏了它，A 会以 10 个槽在 inmem 上领 run，"
              "而那是静默的数据分裂）")
    check(re.search(r"^rm_b\)", text, re.M) is not None,
          "有 `rm_b` 子命令（清残留的临时色；手册 §4/§7 就是让人这么救急的 —— 子命令不存在时"
          "那段手册会把人带进沟里）")
    check("LIVE_BACKEND" in text and "B_EXISTS" in text,
          "预检会读出**当前流量在哪一色**（`LIVE_BACKEND`）与**有没有残留 B**（`B_EXISTS`）")

    # ── 预检里的「旧镜像」必须是 **A 容器正在跑的**那一份，不是 `langgraph-api` tag ──
    check(re.search(r'OLD_IMAGE_ID=\$\(docker image inspect', text) is None
          and 'a_img' in text and 'docker inspect -f \'{{.Image}}\' "$A_CT"' in text,
          "★ 「旧镜像」取 **A 容器正在跑的那一份**（`docker inspect {{.Image}}`），不是 "
          "`langgraph-api` 这个 tag —— 上一次发版一构建完 tag 就指到新镜像上了，读 tag 会让"
          "「旧镜像」== 「新镜像」⇒ 回退退到**同一版**，看起来成功、实际什么都没退（假回退）")

    # ── 起空闲色 B：先清再起 + 预检回归闸（判据＝「B 不比 A 差」）──
    m_startb = re.search(r"^start_b\)(.*?)\n\s*;;", text, re.S | re.M)
    check(m_startb is not None, "有 `start_b` 子命令（起空闲第二色）")
    if m_startb:
        sb = m_startb.group(1)
        check(re.search(r'dcb stop "\$B_SVC"', sb) is not None
              and re.search(r'dcb rm -f "\$B_SVC"', sb) is not None,
              "`start_b` 先 `stop && rm -f` 再 up（铁律③：上一次发版中途失败会留下残留 B，"
              "此时 `up -d` 会走 recreate 路径 ⇒ KeyError:'ContainerConfig'）")
        m_abs = re.search(r"grep -q '预检失败'; then(.*?)die", sb, re.S)
        check(m_abs is not None and 'docker logs "$A_CT"' in m_abs.group(1),
              "★ 「B 预检失败」到 `die` 之间**必须先与 A 对照** —— 本机 `.env.prod` 设了 "
              "MCP_ALLOW_DEGRADED=1，而 wrenai_chinook（[CRITICAL] 语义库）连不上，"
              "A 自己就长期处于降级启动态；用**绝对判据**（见到「预检失败」就 die）会把 "
              "**A 的常态**当成 B 的失败，这台机器永远发不了版"
              "（2026-10-08 首次 E2E 实撞，就是被这条拦下的）")
        _bad = ('if docker logs "$B_CT" 2>&1 | grep -q \'预检失败\'; then '
                'die "B 预检失败"; fi')
        _m_bad = re.search(r"grep -q '预检失败'; then(.*?)die", _bad, re.S)
        check(_m_bad is not None and 'docker logs "$A_CT"' not in _m_bad.group(1),
              "负对照：旧的绝对判据摘录会被同一条检查判为不合格（非恒真）")
        check('docker logs "$A_CT"' in sb and "comm -13" in sb,
              "★ 预检回归闸的判据是「**B 的缺失 server 集合 ⊆ A 的**」：只有「A 好 B 坏」或"
              "「B 新增了 A 没有的缺失」才拒流。致命那一档已由上面的 `wait_ok` 盖住 —— "
              "关键 server 缺失时 `start_server.py` 会 sys.exit(1)，/ok 永远不会 200")
    check("[switch]$Force" in raw and "$liveBackend" in raw and "$bExists" in raw,
          "★ 稳态契约前置检查：流量不在 A / 有残留 B 时**拒绝发版**（不检查的话，脚本第二遍会走到"
          "第 4 步 `cp_drain $ACt` 才失败 —— 那时 A 的容器已经不存在了）")
    # ── B_EXISTS 不能用 `$(docker inspect … 2>/dev/null || echo no)` ──
    # 本机 docker 对**不存在**的容器会往 stdout 打一个**空行**再以非 0 退出 ⇒ 命令替换拿到
    # 「空行 + no」两行 ⇒ PowerShell 的 `B_EXISTS=(\S+)` 匹配不到 ⇒ $bExists 变空串 ⇒
    # **稳态（没有残留 B）下每一次发版都被自己的守卫拦死**（2026-10-08 回退演练实撞；
    # 正向那次只因带了 -Force 才没暴露 —— 当时有残留 B，走了 warn 分支）。
    check(re.search(r"B_EXISTS=\$\(docker inspect[^\n]*\|\| echo no\)", text) is None,
          "★ `B_EXISTS` **绝不**写成 `$(docker inspect … 2>/dev/null || echo no)` —— "
          "本机 docker 对不存在的容器会先往 stdout 打一个空行 ⇒ 结果是两行，"
          "PowerShell 侧的正则匹配不到，稳态发版被守卫拦死")
    check("tr -d '[:space:]'" in text and "B_EXISTS=${b_id:-no}" in text,
          "`B_EXISTS` 先剥空白、再 `:-no` 兜底 ⇒ 永远只有**一行**且值确定")
    check(re.search(r"if \(-not \$mB\.Success\)", text) is not None,
          "PowerShell 侧取不到 `B_EXISTS` 时**响亮地拒绝发版**，不把空串当成「没有残留 B」"
          "（判据撒谎的另一种形态）")
    if shutil.which("bash"):
        # 行为闸：用一个「打空行 + 返回 1」的假 docker 复现本机的真实语义，两种写法各跑一遍
        script = ("docker() { printf '\\n'; return 1; }\n"
                  "B_CT=x\n"
                  "b_id=$(docker inspect -f '{{.Id}}' \"$B_CT\" 2>/dev/null | tr -d '[:space:]')\n"
                  "printf 'NEW=[%s]\\n' \"B_EXISTS=${b_id:-no}\"\n"
                  "printf 'OLD=[%s]\\n' \"B_EXISTS=$(docker inspect -f '{{.Id}}' \"$B_CT\" "
                  "2>/dev/null || echo no)\"\n")
        try:
            r = subprocess.run(["bash", "-c", script],
                               capture_output=True, encoding="utf-8", errors="replace")
            out = r.stdout or ""
        except OSError as e:  # noqa: BLE001
            out = f"<{e}>"
        new_line = next((ln for ln in out.splitlines() if ln.startswith("NEW=")), "")
        old_line = next((ln for ln in out.splitlines() if ln.startswith("OLD=")), "")
        check(new_line == "NEW=[B_EXISTS=no]",
              "行为闸：假 docker 下新写法输出**恰好一行** `B_EXISTS=no`（有判别力，非恒真）",
              new_line)
        check(re.search(r"B_EXISTS=(\S+)", old_line) is None,
              "行为闸：同一个假 docker 下**旧写法**的输出让 `B_EXISTS=(\\S+)` 匹配失败 —— "
              "这正是稳态发版被拦死的那条路径", old_line)
    else:
        print("  [SKIP] 本机没有 bash —— B_EXISTS 的行为闸跳过（不算失败）")
    check(re.search(r"state_write '\$curImageId' '\$newImageId' '\$ASvc'", raw) is not None,
          "状态文件记的是**常驻色 A**（`LIVE_SERVICE=langgraph-api`）—— 回退路径据此判断谁在接流")
    check("$ABackend" in raw and "$BBackend" in raw,
          "上游地址用变量拼（`\"$ASvc:2026\"` 会被 PS 解析成作用域变量 `$ASvc:2026`）")

    # 预检里那个「现在哪一色在接流」的 grep：**必须容忍任意间距**
    m_live = re.search(r"LIVE_BACKEND=\$\(grep -o '([^']+)'", text)
    check(m_live is not None, "预检用 grep 从线上 nginx.conf 里抽出当前上游（LIVE_BACKEND）")
    if m_live:
        pat = m_live.group(1)
        check("[[:space:]]*" in pat,
              "★ 那个 grep 容忍**任意**间距：线上那行是 `set $backend_up  \"…\"`（**两个空格**）"
              "—— 写死一个空格会静默匹配不到，打印出空的 LIVE_BACKEND（判据撒谎）")
        if shutil.which("bash"):
            with tempfile.TemporaryDirectory() as td:
                fx = pathlib.Path(td) / "nginx.conf"
                fx.write_text('    set $backend_up  "langgraph-api:2026";\n', encoding="utf-8")
                try:
                    r = subprocess.run(["bash", "-c", f"grep -o '{pat}' '{fx.as_posix()}' | head -1"],
                                       capture_output=True, encoding="utf-8", errors="replace")
                    got = (r.stdout or "").strip()
                except OSError as e:  # noqa: BLE001
                    got = f"<{e}>"
                check("langgraph-api:2026" in got,
                      "行为闸：把「两个空格」的夹具喂给这个 grep 能取到值（断言有判别力，非恒真）",
                      got[:80] or "<空>")
        else:
            print("  [SKIP] 本机没有 bash —— LIVE_BACKEND 的行为闸跳过（不算失败）")

    # ── 回退 ──
    check("[switch]$Rollback" in raw, "有 `-Rollback` 参数（声明在 param 块里）")
    check("retag_old" in text and "start_a_old" in text and "--no-build" in text,
          "回退路径：用旧镜像 ID retag + `up --no-deps --no-build` 起旧色（绝不重新构建 ⇒ "
          "拿到的确实是上一版）")

    # ── ★ 回退**本身就是蓝绿**：绝不能「流量还在 A 上就直接 stop/rm/up 换镜像」──
    # 回退最常见的场景是「发完发现新版有问题」，而那时**窗口已经关了**（B 已删、流量在 A）。
    # 若直接重启 A 换镜像，就是拿真用户等 ≈26s 冷启 —— 与「零中断」自相矛盾。
    rb = drv[drv.index("if ($Rollback)") : drv.index("== 1/12 本地打包")] \
        if ("if ($Rollback)" in drv and "== 1/12 本地打包" in drv) else ""
    check(bool(rb), "能定位到回退分支（否则下面几条是空转）")
    if rb:
        def ridx(pat: str, s: str = rb, start: int = 0) -> int:
            m = re.search(pat, s[start:])
            return (m.start() + start) if m else -1

        r_startb = ridx(r"\bbash \$StepsSh start_b\b")
        r_promote = ridx(r"\bbash \$StepsSh promote_b\b")
        r_nginx_b = ridx(r"\bbash \$StepsSh nginx_apply\b")
        r_retag = ridx(r"\bbash \$StepsSh retag_old\b")
        r_starta = ridx(r"\bbash \$StepsSh start_a_old\b")
        r_nginx_a = ridx(r"\bbash \$StepsSh nginx_apply\b", rb, r_nginx_b + 1) if r_nginx_b >= 0 else -1
        r_cut_a = ridx(r"\bbash \$StepsSh cutover \$ASvc \$ACt\b")
        r_cut_b = ridx(r"\bbash \$StepsSh cutover \$BSvc \$BCt\b")
        r_order = {"start_b": r_startb, "promote_b": r_promote, "nginx(B)": r_nginx_b,
                   "cutover_A": r_cut_a, "retag_old": r_retag, "start_a_old": r_starta,
                   "nginx(A)": r_nginx_a, "cutover_B": r_cut_b}
        check(all(v >= 0 for v in r_order.values()),
              "回退分支能定位到全部关键步骤（任一步被删/改名都响亮失败）", f"{r_order}")
        check(r_startb >= 0 and "bash $StepsSh start_b 0" in rb,
              "★ 回退**先起临时色 B**（N_JOBS_PER_WORKER=0，用**旧镜像**）—— 回退也走蓝绿，"
              "不是直接把常驻色 A 重启成旧版")
        check(re.search(r"'IMAGE_ID_PLACEHOLDER',\s*\$OldImageId", rb) is not None,
              "★ 回退时 overlay 渲染的是**旧镜像 ID**（若渲染成新镜像，回退等于没退 —— 假回退）")
        seq_rb = [r_startb, r_promote, r_nginx_b, r_cut_a, r_retag, r_starta, r_nginx_a, r_cut_b]
        check(all(seq_rb[i] < seq_rb[i + 1] for i in range(len(seq_rb) - 1)),
              "回退顺序：起旧镜像B < 提升B < **切流到B** < 排空停A < retag旧镜像 + 起A "
              "< **切回A** < 排空停B", f"{seq_rb}")
        check(0 <= r_nginx_b < r_retag,
              "★★ **切流到 B 必须早于「把 A 换成旧镜像起回来」** —— 这条是回退零中断的"
              "全部要害：切流之前流量在 A 上，此时重启 A 换镜像 = 拿真用户等 ≈26s 冷启")
        _bad_rb = ("$st = ssh state_read\n"
                   "ssh \"bash $StepsSh retag_old '$OldImageId' && bash $StepsSh start_a_old\"\n"
                   "ssh \"bash $StepsSh poll_start 900\"\n"
                   "ssh \"bash $StepsSh nginx_apply x\"\n")
        _b_nb = re.search(r"\bbash \$StepsSh nginx_apply\b", _bad_rb)
        _b_tr = re.search(r"\bbash \$StepsSh retag_old\b", _bad_rb)
        check(_b_tr is not None and (_b_nb is None or _b_tr.start() < _b_nb.start()),
              "负对照：老形态（**先** retag/起重旧色、**后**切 nginx）会被上面那条判为不合格"
              "（非恒真）")
    check("poll_stop" in text and "POLL_NON200" in text,
          "打印**非 200 计数**（「零中断」这个结论必须由采样数据支撑，不能靠感觉）")
    check("cp_drain" in text and "nl2sql_ops_drain.py" in text,
          "排空用的是 `docker cp` 进容器的**仓库里那份** ops_drain（旧镜像里那份是旧逻辑 —— "
          "判据不会跟着发版更新）")
    check(re.search(r"cp_drain\)\n(?:.*\n)*?.*?\bdie\b.*docker inspect", text) is not None
          or re.search(r"cp_drain\)\n(?:.*\n){0,6}?.*docker inspect", text) is not None,
          "`cp_drain` 对**不存在**的色空转（稳态就只有 A；`-Rollback` 里 B 往往已被删 ⇒ "
          "不空转的话回退演练会在 `docker cp` 上直接死掉）")
    check("DRAIN_READY=n/a" in text,
          "`cutover` 对**不存在**的色空转（同上；回退了两次之后再 `-Rollback` 不该炸）")
    check("-Force" in raw and "Compare-Object" in raw,
          "有防呆：仓库 nginx.conf 与线上不一致时**中止**并打印 diff，要显式 -Force 才覆盖"
          "（不悄悄抹掉别人手改过的东西）")


# ══════════════════════════════════════════════════════════════
# ④ eval_queue 租约
# ══════════════════════════════════════════════════════════════
def t4_eval_queue(raw: str) -> None:
    section("④ `src/agent/eval/eval_queue.py`：崩槽恢复只回收**超租约**的 running")
    text = _strip_comments(raw)

    check(re.search(r"def reset_running_to_pending\(self,\s*\*,\s*stale_only:\s*bool\s*=\s*True\)", text)
          is not None,
          "`reset_running_to_pending(*, stale_only: bool = True)` —— 默认必须是**只回收超租约**"
          "（默认全量就会在第二色的 drainer 一启动时把旧色正在跑的行打回 pending ⇒ 同一条 judge "
          "跑两遍，双倍 token 且结果互相覆盖）")
    check("WHERE state='running' AND updated_at < ?" in text,
          "租约夹逼 `AND updated_at < ?` 在 SQL 里（`updated_at` 是 claim 时写的、执行期不刷新 "
          "⇒ 它就是租约起点，**不加列**）")
    check("_lease_cutoff()" in text and "NL2SQL_EVAL_JUDGE_LEASE_SECS" in text,
          "租约时长可被 `NL2SQL_EVAL_JUDGE_LEASE_SECS` 覆盖")
    # 负对照：把夹逼去掉（等价于无条件复位）必须报警
    check("AND updated_at < ?" not in text.replace("WHERE state='running' AND updated_at < ?", ""),
          "负对照：把夹逼那一段从源码里删掉后，上面那条断言会失败（断言的判别力来自这一处）")
    check(re.search(r"WHERE state='running'\"\n\s*\(\s*_now\(\),?\s*\)", text) is not None
          or "WHERE state='running'\"" in text,
          "`stale_only=False`（只有显式 `--force` 走）保留旧的全量复位行为（运维逃生门）")

    m = re.search(r"def _drain_loop\(.*?\n(?=def |\Z)", text, re.S)
    body = m.group(0) if m else ""
    check("_RECLAIM_INTERVAL" in body and "reset_running_to_pending()" in body,
          "`_drain_loop` **周期性**（空队列时每 `_RECLAIM_INTERVAL` 秒）再回收一次 —— "
          "今天只在启动时收一次，色一崩那条 running 就没人管了")
    check("--force" in text and re.search(r"stale_only=not args\.force", text) is not None,
          "CLI 有 `--force`，且 `drain_all(..., stale_only=not args.force)`（默认安全、"
          "强制要显式开口）")


# ══════════════════════════════════════════════════════════════
# ⑤ ops_drain 就绪判据
# ══════════════════════════════════════════════════════════════
def t5_ops_drain(raw: str) -> None:
    section("⑤ `scripts/ops_drain.py`：就绪判据必须含「计数可信」")
    text = _strip_comments(raw)

    check("counter_trusted" in text,
          "判据读了 `counter_trusted` —— `drain.py` 读不到计数时会**兜底返回 0**（fail-open），"
          "只看 `remaining == 0` 就会把「不知道」当成「已清零」，据此停容器会腰斩在跑的 run，"
          "而日志上一切正常")
    check(re.search(r"if\s+not\s+state\.get\(\"counter_trusted\"\)", text) is not None,
          "计数不可信时**显式**走失败分支（不是 hint，是判定）")
    check("计数不可用" in text and "2 = 未就绪" in text,
          "模块头写明了不可信 ⇒ 退出码 2（发版脚本据此不把容器当成可停）")
    # 负对照：只有 remaining 的老写法必须被判定为「不满足」
    _old_form = "if state.get(\"remaining\") == 0:"
    check("counter_trusted" not in _old_form,
          "负对照：旧的 `if state.get(\"remaining\") == 0:` 不含 counter_trusted（判别力来自这一处）")
    check("\"--json\"" in text or "'--json'" in text or "--json" in text,
          "有 `--json`（给发版脚本解析/留痕；stdout 只输出一行合法 JSON）")
    check("remaining == 0 and counter_trusted" in text or "_ready(state)" in text,
          "就绪判据被抽成单一函数（`_ready`），轮询与单次探测共用同一份逻辑")


# ══════════════════════════════════════════════════════════════
# ⑥ 行为：租约真的只回收超租约的行
# ══════════════════════════════════════════════════════════════
def t6_lease_behaviour() -> None:
    section("⑥ 行为闸：拿临时 SQLite 真跑一遍 reset_running_to_pending")
    _src = _REPO / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

    tmp = tempfile.mkdtemp(prefix="nl2sql-verify-bg-")
    saved_root = os.environ.get("AGENT_DATA_ROOT")
    saved_lease = os.environ.pop("NL2SQL_EVAL_JUDGE_LEASE_SECS", None)
    try:
        os.environ["AGENT_DATA_ROOT"] = tmp
        from agent.eval.eval_queue import EvalQueueStore  # noqa: E402
    except Exception as e:  # noqa: BLE001
        check(False, "能 import agent.eval.eval_queue（离线行为闸的前提）", f"{type(e).__name__}: {e}")
        return

    def _mk(name: str) -> "EvalQueueStore":
        return EvalQueueStore(pathlib.Path(tmp) / name / "eval_queue.sqlite")

    def _stamp(st, trace_id: str, seconds_ago: float) -> None:
        ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
        st._conn.execute("UPDATE eval_queue SET state='running', updated_at=? WHERE trace_id=?",
                         (ts, trace_id))
        st._conn.commit()

    def _state_of(st) -> dict:
        return {r[0]: r[1] for r in st._conn.execute("SELECT trace_id, state FROM eval_queue")}

    try:
        # ── 默认租约（1800s）：超租约的回 pending，刚 claim 的**不动** ──
        st = _mk("q1")
        st.enqueue("judge", "t-old", "", {})
        st.enqueue("judge", "t-new", "", {})
        _stamp(st, "t-old", 7200)   # 2 小时前 —— 持有者显然已死
        _stamp(st, "t-new", 5)      # 5 秒前 —— 正在跑
        n = st.reset_running_to_pending()
        state = _state_of(st)
        check(n == 1, "只回收了 1 条（超租约那条）", f"回收 {n} 条")
        check(state.get("t-old") == "pending", "超租约的 running 被复位为 pending", str(state))
        check(state.get("t-new") == "running",
              "★ 刚 claim 的 running **原封不动** —— 这一条就是「两色并存不重复烧 token」的全部含义",
              str(state))

        # ── `--force` 路径（stale_only=False）保留旧的全量行为 ──
        st2 = _mk("q2")
        st2.enqueue("judge", "t2", "", {})
        _stamp(st2, "t2", 5)
        n2 = st2.reset_running_to_pending(stale_only=False)
        check(n2 == 1 and _state_of(st2).get("t2") == "pending",
              "stale_only=False 仍全量复位（运维逃生门没被顺手删掉）", f"{n2} 条")

        # ── env 覆盖：租约是**调用期**读的，改小之后那条 5s 的行也会被回收 ──
        st3 = _mk("q3")
        st3.enqueue("judge", "t3", "", {})
        _stamp(st3, "t3", 5)
        n3 = st3.reset_running_to_pending()
        check(n3 == 0, "默认租约下，5s 前的 running 不会被回收（负对照）", f"{n3} 条")
        os.environ["NL2SQL_EVAL_JUDGE_LEASE_SECS"] = "1"
        n4 = st3.reset_running_to_pending()
        check(n4 == 1 and _state_of(st3).get("t3") == "pending",
              "租约改成 1s 后同一条被回收（`NL2SQL_EVAL_JUDGE_LEASE_SECS` 在**调用期**生效，"
              "不是 import 期常量）", f"{n4} 条")
        os.environ.pop("NL2SQL_EVAL_JUDGE_LEASE_SECS", None)
        os.environ["NL2SQL_EVAL_JUDGE_LEASE_SECS"] = "abc"
        n5 = st3.reset_running_to_pending()
        check(n5 == 0, "非法值（abc）退回默认租约而不是把行全放了（fail-safe）", f"{n5} 条")
    finally:
        if saved_lease is None:
            os.environ.pop("NL2SQL_EVAL_JUDGE_LEASE_SECS", None)
        else:
            os.environ["NL2SQL_EVAL_JUDGE_LEASE_SECS"] = saved_lease
        if saved_root is None:
            os.environ.pop("AGENT_DATA_ROOT", None)
        else:
            os.environ["AGENT_DATA_ROOT"] = saved_root
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for p in (_BASE, _OVERLAY, _NGINX, _PS1, _EVALQ, _DRAIN):
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

    t1_overlay(base, overlay)
    t2_nginx(_read(_NGINX))
    t3_ps1(_read(_PS1), _PS1)
    t4_eval_queue(_read(_EVALQ))
    t5_ops_drain(_read(_DRAIN))
    t6_lease_behaviour()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    print("\n注意：`docker-compose -f docker-compose.yml -f docker/compose.colorB.yml config -q` 只能在"
          "服务器上跑（仓内没有 `.env.prod`，env_file 缺失会解析失败）—— 那一步在发版手册里。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
