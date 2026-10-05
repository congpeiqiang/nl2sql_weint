# -*- coding: utf-8 -*-
"""档 1「发版不再依赖 nginx 的 IP 复用」验证（离线，无需后端/数据库/网络）。

**要解决的问题**：2026-10-05 一次后端发版实测 ≈29 秒全量 502。归因出两条：
  ① nginx 的上游是**启动期解析一次、IP 永久缓存**（`upstream backend { server
     langgraph-api:2026; }`）⇒ 容器重建换 IP 后 nginx 一直打旧 IP，**永久 502 直到手工
     reload**。那次没炸只是因为 Docker 把同一个 IP 又分给了新容器 —— 靠运气；同一条路
     也直接封死蓝绿/滚动。反过来的坑同样存在：旧写法下后端不在，nginx 干脆起不来。
  ② 发版是裸 `docker stop`：进程先关监听 ⇒ 新请求 `connection refused`（502），之后才是
     信号式排空。P2-1 早就实现了**协作式排空**（`POST /api/admin/drain`，拒绝新 run 时给
     503 + Retry-After），但权威脚本 `release-backend.ps1` 从没接线。

档 1 的交付（**不含**零中断 —— 那是档 2 蓝绿，前置是 `DATABASE_URI` 从 `:memory:`
迁到 compose 内的 postgres，否则 `.langgraph_api` 每容器一份 pckl 会分裂线程元数据）：
上游改成**请求期解析**；后端发版**先排空**、再用**经 nginx** 的探针量窗口；前端发版
**不再碰 nginx**。

本脚本是这套改动的**静态闸** —— 只读仓内文件、不连网、不改任何东西：
  ① `docker/nginx.conf`：无 `upstream` 块、`resolver` 在 `http` 块、三个 `proxy_pass`
     全是**变量形式**、`proxy_redirect off` 在位；附**负对照**证明断言非恒真。
  ② `release-backend.ps1`：排空在 `stop` **之前**、探针**经 nginx**、不再盲等 75s、
     **永不触碰 nginx 容器**。
  ③ `release-frontend.ps1`：只 `up -d frontend`、不重建 nginx、两发经 nginx 的探针齐全。
  ④ 两个 `.ps1` 过 PowerShell 解析闸 —— 双引号串里 `"` 的转义符是**反引号**，`\"` 会
     提前闭合字符串（2026-09-26 这条坑让 release 脚本第 5 步整段没跑过，见 memory
     `powershell-doublequote-escape-release-step5`）。
  ⑤ 两个 `.ps1` 仍是 **UTF-8 BOM**（无 BOM 时 PS 5.1 按 ANSI 解码，中文注释与提示全乱码）。

⚠️ **断言前先剥注释**（`_strip_comments`，各段断言都走它）：本仓注释里**故意**写着被
否定的旧写法 —— `nginx.conf` 的注释里有 `upstream backend { … }`，前端脚本的注释里有
`up -d frontend nginx`。不剥注释就会把「解释为什么要删」误判成「还留着」。前提是这两个
文件（及其它 `.ps1`）的**字符串里不含 `#`**；若哪天有了，下面的**正向**断言会响亮地失败
（不会静默放过），改 `_strip_comments` 即可。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_release_zero_downtime.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[1]
_NGINX = _REPO / "docker" / "nginx.conf"
_BACKEND_PS = _REPO / "docs" / "prod环境" / "release-backend.ps1"
_FRONTEND_PS = _REPO / "docs" / "prod环境" / "release-frontend.ps1"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(text: str, marker: str = "#") -> str:
    """剥掉行内注释（`#` 到行尾）。

    本仓注释里**故意**保留被否定的旧写法，不剥就会把「解释为什么删掉」当成「还留着」。
    """
    out = []
    for ln in text.splitlines():
        i = ln.find(marker)
        out.append(ln if i < 0 else ln[:i])
    return "\n".join(out)


def _nginx_action(text: str) -> str:
    """返回命中的「动 nginx 容器」命令原文（没有则空串）。"""
    for pat in (
        # docker/docker-compose stop|restart|rm -f|up -d … nginx
        r"(?im)^[^\n]*\bdocker(?:-compose)?\s+(?:stop|restart|rm(?:\s+-f)?|up\s+-d)\b[^\n]*\bnginx\b[^\n]*$",
        # 直接给 nginx 发信号（reload 会重读配置、并不重建容器，但发版脚本不该有它）
        r"(?im)\bnginx\s+-s\s+(?:reload|stop|quit)\b",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return ""


# ── ① nginx.conf ────────────────────────────────────────────
def t1_nginx_conf() -> None:
    section("① docker/nginx.conf：上游改成请求期解析")
    raw = _read(_NGINX)
    nginx = _strip_comments(raw)

    check("upstream " not in nginx, "没有 `upstream` 块 —— 启动期解析已移除")
    # 负对照：同一条断言对**旧写法**必须报警。少了它，「文件里根本没这词」也会让上面通过。
    _old_form = "http {\n    upstream backend {\n        server langgraph-api:2026;\n    }\n}"
    check("upstream " in _old_form, "负对照：同一条断言对旧写法会报警（非恒真）")

    m = re.search(r"(?m)^\s*resolver\s+([^;]+);", nginx)
    spec = m.group(1).strip() if m else ""
    check(bool(m), "http 块内有 `resolver`", spec)
    check("127.0.0.11" in spec, "指 Docker 内嵌 DNS（compose 默认的用户自定义网络满足前提）")
    check("valid=" in spec, "带 `valid=` —— 覆盖 Docker 对服务名固定的 600s TTL")
    check("ipv6=off" in spec, "带 `ipv6=off` —— Docker 内嵌 DNS 对 AAAA 处理不当（否则报 could not be resolved）")
    check(bool(re.search(r"(?m)^\s*resolver_timeout\s+5s\s*;", nginx)),
          "`resolver_timeout 5s`（默认 30s：DNS 挂掉时请求会挂 30s 才 502）")

    i_server = nginx.find("server {")
    i_res = nginx.find("resolver ")
    check(i_server > 0 and 0 <= i_res < i_server,
          "`resolver` 落在 `http` 块（`server {` 之前）—— 每个 worker 一份缓存的前提",
          f"resolver@{i_res} server@{i_server}")

    i_set_b = nginx.find("set $backend_up")
    i_set_f = nginx.find("set $frontend_up")
    check(i_set_b > i_server, "`set $backend_up` 在 `server` 块内（三个 location 继承、每请求求值）")
    check(i_set_f > i_server, "`set $frontend_up` 在 `server` 块内")
    check('"langgraph-api:2026"' in nginx, "后端上游**带端口**（只写 host 会默认走 80）")
    check('"frontend:3000"' in nginx, "前端上游带端口")

    passes = re.findall(r"(?m)^\s*proxy_pass\s+([^;]+);", nginx)
    check(len(passes) == 3, "三个 location 的 proxy_pass 都还在", f"{passes}")
    check(bool(passes) and all(p.startswith("http://$") for p in passes),
          "全部是**变量形式** —— 漏掉任何一处就退回启动期解析（那处会永久 502）", f"{passes}")
    check(nginx.count("proxy_pass http://$backend_up;") == 2,
          "两处后端 location 用 `$backend_up`（`location /api/` 与 LangGraph 原生路由）")
    check(nginx.count("proxy_pass http://$frontend_up;") == 1, "前端 location 用 `$frontend_up`")
    check(not re.search(r"proxy_pass\s+http://\$\w+\s*\$", nginx),
          "**没有**给 proxy_pass 补 URI（`$request_uri`/`$uri$is_args$args`）—— 无 URI 时静态"
          "与变量形式走同一条分支（转发 unparsed_uri），补上反而切到 URI-as-is 分支 = 回归")
    check("proxy_redirect off;" in nginx,
          "显式 `proxy_redirect off` —— 变量形式下该默认行为被 nginx 静默关闭，写出来免得后人误会")
    check("location /api/" in nginx and "location / {" in nginx, "`/api/` 与 `/` 两个 location 都还在")


# ── ② release-backend.ps1 ───────────────────────────────────
def t2_backend_script() -> None:
    section("② release-backend.ps1：先排空 → 再停 → 经 nginx 探针 → 不碰 nginx")
    ps = _strip_comments(_read(_BACKEND_PS))

    i_drain = ps.find("ops_drain.py")
    i_stop = ps.find("docker-compose stop langgraph-api")
    check(i_drain >= 0, "接了协作式排空（`scripts/ops_drain.py`，容器内跑、自签 token、无需口令）")
    check(i_stop >= 0, "三步重启里有 `docker-compose stop langgraph-api`")
    check(0 <= i_drain < i_stop,
          "**排空在 stop 之前** —— 顺序反了就是先关监听（502）再排空，接了等于白接",
          f"drain@{i_drain} stop@{i_stop}")
    check("ops_drain.py --budget 180" in ps,
          "排空带预算（`--budget 180` < compose `stop_grace_period=240s`，留出冷启余量）")
    check("SKIP ops_drain" in ps,
          "旧镜像/端点不可达时只告警不拦发版（best-effort，`stop` 的信号式排空仍兜底）")
    check("docker-compose rm -f langgraph-api" in ps,
          "保留 `rm -f`（compose v1 对已存在容器直接 `up -d` 会 KeyError 崩）")

    check("127.0.0.1:8080/ok" in ps,
          "健康探针**经 nginx**（同时证明冷启完成 + nginx 已解析到新容器 —— 变量 proxy_pass 的端到端验收）")
    check("nginx_ok=200" in ps, "轮询断言的是 200，不是「打了一次就算过」")
    check("Start-Sleep -Seconds 75" not in ps, "不再盲等 75s（改成服务端轮询到 200）")

    hit = _nginx_action(ps)
    check(hit == "", "**永不触碰 nginx 容器** —— 换容器后的新 IP 由 resolver 自己发现，不需要 reload", hit)
    check("不可用窗口" in ps, "把真实中断窗口量出来并打印（不再靠感觉）")


# ── ③ release-frontend.ps1 ──────────────────────────────────
def t3_frontend_script() -> None:
    section("③ release-frontend.ps1：不重建 nginx + 两发经 nginx 的探针")
    ps = _strip_comments(_read(_FRONTEND_PS))

    check("docker-compose up -d frontend" in ps, "只 `up -d frontend`")
    check("up -d frontend nginx" not in ps,
          "**不再** `up -d frontend nginx` —— 配置没变时 compose 只打 up-to-date，一旦真被重建"
          "就是多引入一次 nginx 停机")
    hit = _nginx_action(ps)
    check(hit == "", "前端发版不触碰 nginx 容器", hit)
    check("docker-compose stop frontend" in ps and "docker-compose rm -f frontend" in ps,
          "仍是 stop/rm/up 三步（compose v1 要求）")
    check("127.0.0.1:8080/" in ps, "轮询 `/` 到 200（不再是盲等 15s + 打一次）")
    check("127.0.0.1:8080/ok" in ps, "探针一：`/ok` —— 证明后端链路没被动过")
    check("127.0.0.1:8080/api/deployment-info" in ps,
          "探针二：`/api/…` —— `/ok` 命中 regex location，覆盖不到 `location /api/` 那个块")
    check('"401"' in ps and '-ne "200"' in ps,
          "`/api/` 探针接受 200/401（未开鉴权 / 需登录都算到得了应用；502=上游没解析到，404=路由没挂）")


# ── ④ PowerShell 解析闸 ─────────────────────────────────────
def t4_parse_gate() -> None:
    section("④ 两个 .ps1 过 PowerShell 解析闸")
    exe = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not exe:
        cand = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if pathlib.Path(cand).is_file():
            exe = cand
    if not exe:
        print("  [SKIP] 本机没有 powershell.exe / pwsh —— 解析闸跳过（不算失败）。"
              "改过脚本的人在 Windows 上必须补跑。")
        return

    for p in (_BACKEND_PS, _FRONTEND_PS):
        # 用 [Parser]::ParseFile 而不是「跑一遍」：语法错误才是要抓的（`\"` 提前闭合字符串
        # 会留下一个语法无效的脚本，而 PS 只有真执行到那一行才报错 —— 那可能在下一次发版）。
        cmd = (
            "$e=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{p}',[ref]$null,[ref]$e);"
            'if($e){$e | ForEach-Object {$_.Message}} else {"PARSE_OK"}'
        )
        try:
            r = subprocess.run([exe, "-NoProfile", "-Command", cmd],
                               capture_output=True, encoding="utf-8", errors="replace")
        except OSError as e:  # noqa: BLE001 —— 起不来就 SKIP，别把环境问题当代码问题
            print(f"  [SKIP] 调不起 {exe}：{e}")
            return
        out = (r.stdout or "").strip()
        check(out == "PARSE_OK", f"{p.name} 解析通过", out[:300])
        if out != "PARSE_OK":
            print("     ↑ 检查双引号串里的 `\\\"`（应为**反引号**转义）")


# ── ⑤ BOM 闸 ────────────────────────────────────────────────
def t5_bom_gate() -> None:
    section("⑤ 两个 .ps1 仍是 UTF-8 BOM")
    for p in (_BACKEND_PS, _FRONTEND_PS):
        head = p.read_bytes()[:3]
        check(head == b"\xef\xbb\xbf",
              f"{p.name} 带 UTF-8 BOM（无 BOM 时 PS 5.1 按 ANSI 解码，中文全乱）",
              f"实际={head.hex()}")


def main() -> int:
    for p in (_NGINX, _BACKEND_PS, _FRONTEND_PS):
        if not p.is_file():
            print(f"{NG} 缺文件：{p}")
            return 1

    t1_nginx_conf()
    t2_backend_script()
    t3_frontend_script()
    t4_parse_gate()
    t5_bom_gate()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
