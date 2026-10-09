# ============================================================
# NL2SQL **dev 环境（192.168.25.34）**后端发布脚本（**蓝绿 / 真零中断**）
# 运行环境：本机 PowerShell（已配 SSH 免密 ⇒ 全程不弹口令）
#
# 用法：  .\dev-release-backend-bluegreen.ps1
#              正向发版：打包 src → 暂存到 src-next → 起空闲 B → 提升 B → 切流到 B
#                        → 排空停 A → **目录轮换** → 起回 A → 切回 A → 停 B
#         .\dev-release-backend-bluegreen.ps1 -Rollback
#              回退：把 B 挂到上一版的 src 目录起临时色 → 切流 → 停 A → 目录轮换回来
#                    → 起回 A → 切回 A → 停 B
# 参数：  -LocalRoot / -WorkDir / -Server / -SshUser / -DevDir / -AppDir / -ChunkMB / -MaxRetry
#         -DrainBudget 180      排空预算（秒），须 < compose 的 stop_grace_period=240s
#         -Rollback             回退模式
#         -PrevSrc <目录名>     回退用的上一版代码目录（不给则读 /tmp/nl2sql_bluegreen_dev.state）
#         -SkipPackage          复用已上传的包（重试时省一次上传）
#         -Force                线上 nginx 与仓库模板不一致 / 流量不在 A / 有残留 B 时允许继续
#
# ── 与 prod 的 `release-backend-bluegreen.ps1` 是**同一套形态**，三处结构性差异 ──
#  ★dev-1 **dev 的代码走 bind mount，不走镜像。** prod 的新代码在镜像里，回退靠旧镜像 ID；
#         dev 的新代码在**目录**里 ⇒ 第二色挂另一份 src（默认 `/mydata/nl2sql/src-next`），
#         发版末段靠**目录轮换**（src → src-prev-<stamp>、src-next → src）把新代码变成常驻色，
#         回退 = 把 `src-prev-<stamp>` 挂回来。**dev 全程不换镜像、不构建。**
#  ★dev-2 **dev 是 compose v2 插件**（容器名连字符 `nl2sql-app-*-1`、命令是 `docker compose`）。
#         三条铁律里的 `--no-deps` 与「overlay 只新增服务名」照旧执行；
#         `KeyError: 'ContainerConfig'` 那条是 prod 的 compose v1 专属。
#  ★dev-3 `env_file` 是 **`.env.dev`**（`DEPLOY_ENV=dev` 在它里面）。照抄 prod 的 `.env.prod`
#         会把 dev 带成 prod 语义。
#
# ── 稳态契约：**A 是常驻色，B 只在发版窗口里存在** ──────────────────────
#  发版以「A 跑旧代码」开始、以「A 跑新代码」结束（中途借 B 顶一下流量）。
#  为什么不在结尾把 B 留成活色：① 下一次发版就没有空闲的第二色了（脚本第二次跑不动）；
#  ② 全仓的运维习惯、日志 grep、`dev-release-backend.ps1` 都认 A 那个容器名。
#
# ── 为什么「切流之后再排空」是这套设计的关键 ────────────────────────────
#  老路是「先排空再停」——排空期间新提交拿到 **503**（`DrainGateMiddleware`）。503 也是 5xx，
#  用户照样看得见。切流之后旧色再也收不到用户请求，排空就不再对任何人可见 ⇒
#  **这才是「零 5xx」而不是「把 502 换成 503」**。
#
# ── 本轮交付边界（诚实声明）────────────────────────────────────────────
#  零中断**只覆盖后端/API 链路**。前端发版仍走 `dev-release-frontend.ps1`，`location /`
#  会有几秒 502 —— 前端是无状态静态服务，套同一套 overlay 形态即可覆盖，**本轮不做**。
#
# ── 三条铁律 ──────────────────────────────────────────────────────────
#  ① 每个 `docker compose` 调用都带 `--no-deps`：不带就会把 `depends_on` 的
#     postgres/redis 一起纳入收敛计划（dev 是 v2，后果比 prod 轻，但没有理由冒这个险）。
#  ② overlay 只**新增**服务名，不许出现任何既有服务的键（否则 base 的 config-hash 会变）。
#  ③ 改配置用 `stop && rm -f && up -d`，不要指望 `up -d` 帮你 recreate。
#  对旧色的**停止**用裸 `docker stop/rm <容器名>`（见 steps 脚本的说明）。
#
# ── 残余竞态（本机无法消除，E2E 里要量它）──────────────────────────────
#  `N_JOBS_PER_WORKER` 只在进程 import 期读一次（`langgraph_api/config/__init__.py:380`），
#  没有运行期开关 ⇒ 切流之后旧色**仍持有** 10 个 run 槽，仍可能从**共享**队列里领到刚提交
#  给新色的 run。我们靠「**同一个 ssh 会话内**：连续 3 次 idle 判定 → 立刻 stop」把窗口压到
#  毫秒级。这是设计上已知、已量化、不可根除的一条。
#  ⚠️ **只有第 8 步这一次有害**（旧色 A 跑的是**旧代码**）；第 12 步停 B 时 A/B 是**同一份代码**。
# ============================================================
[CmdletBinding()]
param(
    [string]$LocalRoot,
    [string]$WorkDir    = "D:\code_work_space\llm\deepseek-workspace\nl2sql-dev",
    [string]$Server     = "192.168.25.34",
    [string]$SshUser    = "weint",
    [string]$DevDir     = "/mydata/nl2sql",
    [string]$AppDir     = "/mydata/nl2sql/nl2sql-app",
    [double]$ChunkMB    = 0.5,
    [int]$MaxRetry      = 8,
    [int]$DrainBudget   = 180,
    [switch]$Rollback,
    [string]$PrevSrc    = "",
    [switch]$SkipPackage,
    [switch]$Force
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

# ── 常量：与服务端 steps 脚本必须逐字一致 ──
$Proj  = "nl2sql-app"
$ASvc  = "langgraph-api";     $BSvc = "langgraph-api-b"
$ACt   = "nl2sql-app-langgraph-api-1";   $BCt = "nl2sql-app-langgraph-api-b-1"
$NCt   = "nl2sql-app-nginx-1"
# nginx 上游写法是 `<服务名>:<端口>`；用变量拼，避免 `"$ASvc:2026"` 被 PS 当成
# 作用域变量 `$ASvc:2026`（`:` 在变量名里是合法的 scope/drive 分隔符）。
$ABackend = $ASvc + ":2026"
$BBackend = $BSvc + ":2026"
$StepsSh   = "/tmp/nl2sql_bg_steps_dev.sh"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Stamp     = Get-Date -Format "yyyyMMdd-HHmmss"

# BatchMode=yes：禁止一切口令交互（没配好 key 就立刻失败，而不是每步弹一次）
# keepalive：防链路空闲被判死；ConnectTimeout：避免卡握手。
$SshOpt = @("-o","BatchMode=yes","-o","ServerAliveInterval=20",
            "-o","ServerAliveCountMax=6","-o","ConnectTimeout=15")
$Target = "${SshUser}@${Server}"

function Say  ([string]$m) { Write-Host $m -ForegroundColor Cyan }
function Ok   ([string]$m) { Write-Host $m -ForegroundColor Green }
function Warn ([string]$m) { Write-Host $m -ForegroundColor Yellow }
function Die  ([string]$m) { throw $m }

function Scp-UpWithRetry([string]$Local, [string]$Remote) {
    for ($t = 1; $t -le $MaxRetry; $t++) {
        scp @SshOpt $Local "${Target}:${Remote}"
        if ($LASTEXITCODE -eq 0) { return }
        Warn "  上传 $Remote 第 $t/$MaxRetry 次被重置，3s 后重试…"
        Start-Sleep -Seconds 3
    }
    Die "上传 $Remote 失败（已重试 $MaxRetry 次）"
}

if (-not $LocalRoot) {
    $cand = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    if (Test-Path (Join-Path $cand "src\agent\main_agent.py")) { $LocalRoot = $cand }
    else { Die "推断不出仓库根（$cand 下没有 src\agent\main_agent.py），请用 -LocalRoot 指定" }
}
if (-not (Test-Path (Join-Path $LocalRoot "src\agent\main_agent.py"))) {
    Die "-LocalRoot 下找不到 src\agent\main_agent.py：$LocalRoot"
}
if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null }

Write-Host "dev 后端发布（蓝绿）" -ForegroundColor White
Write-Host "  仓库   : $LocalRoot"
Write-Host "  目标   : $Target ($AppDir)"
Write-Host "  时间戳 : $Stamp"
Write-Host ""
# 推的是本机**工作树**（含未提交改动）—— 这正是 dev 的用途，但先让人看清推的是什么
Push-Location $LocalRoot
$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
$dirty  = @(git status --porcelain 2>$null).Count
Pop-Location
Write-Host "  git    : $branch ｜ 未提交改动 $dirty 项" -ForegroundColor DarkGray
if ($dirty -gt 0) { Write-Host "  ⚠ 会把这些未提交改动一起推到 dev" -ForegroundColor Yellow }
Write-Host ""

# ============================================================
# 服务器侧步骤脚本（上传到 /tmp 后按子命令调用）
#   —— 抽成脚本而不是拼 ssh 内联命令：内联要跨 PowerShell → ssh → bash 三层转义，
#      历史上「`\"` 提前闭合 PS 字符串」那条坑就是这么来的。这里用单引号 here-string，
#      内容对 PS 完全透明。
# ============================================================
$BgStepsSh = @'
#!/usr/bin/env bash
# dev 蓝绿发版：服务器侧步骤（由 dev-release-backend-bluegreen.ps1 上传到 /tmp 再调用）
#   nl2sql_bg_steps_dev.sh <子命令> [参数…]
#
# 停止旧色**故意**用裸 `docker stop/rm <容器名>` 而不是 `docker compose stop <服务名>`：
#  ① 少一次 compose 解析（两个文件）⇒ 把「最后一次 idle 判定 → 真的停」的窗口压到毫秒
#     —— 这正是残余竞态（旧色仍可能从共享队列领 run）的缓解手段本身；
#  ② 只可能碰到这一个容器，**物理上**不可能波及 postgres/redis。
set -u

PROJ=nl2sql-app
A_SVC=langgraph-api
B_SVC=langgraph-api-b
A_CT=nl2sql-app-langgraph-api-1
B_CT=nl2sql-app-langgraph-api-b-1
NGINX_CT=nl2sql-app-nginx-1
DEV=/mydata/nl2sql
APP_DIR=/mydata/nl2sql/nl2sql-app
POLL_LOG=/tmp/nl2sql_bg_dev_poll.log
POLL_PID=/tmp/nl2sql_bg_dev_poll.pid
STATE=/tmp/nl2sql_bluegreen_dev.state
OPS_DRAIN=$DEV/.tmp/ops_drain.py
SWAP_SH=$DEV/.tmp/dev_swap_src.sh

cd "$APP_DIR" || { echo "❌ 进不了 $APP_DIR" >&2; exit 1; }

log() { echo "[$(date '+%F %T')] $*"; }
die() { echo "❌ $*" >&2; exit 1; }
# 只用 base（对 A 的操作）；对 B 的操作走 dcb（必须同时给 overlay）
dc()  { docker compose -f docker-compose.yml "$@"; }
dcb() { docker compose -f docker-compose.yml -f docker/compose.colorB.dev.yml "$@"; }

probe_ok() { docker exec "$1" curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
             http://localhost:2026/ok 2>/dev/null || true; }

# 等到容器内 /ok 回 200；成功 echo 200 并 return 0，失败 echo 最后的 code 并 return 1
wait_ok() {
  local ct="$1" max="${2:-120}" i code=""
  for ((i = 0; i * 2 < max; i++)); do
    code=$(probe_ok "$ct")
    [ "$code" = "200" ] && { echo 200; return 0; }
    sleep 2
  done
  echo "${code:-none}"; return 1
}

# 1s 一次的 /ok 轮询（本脚本自身以子命令方式被 nohup 拉起，见 poll_start）
poll_loop() {
  local max="${1:-1800}" i c
  : > "$POLL_LOG"
  for ((i = 0; i < max; i++)); do
    c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:8080/ok 2>/dev/null)
    printf '%s %s\n' "$(date +%s)" "${c:-000}" >> "$POLL_LOG"
    sleep 1
  done
}

case "${1:-}" in

# ── 0. 只读预检 ────────────────────────────────────────────────
preflight)
  echo "A_EXISTS=$(docker inspect -f '{{.Id}}' "$A_CT" 2>/dev/null | tr -d '[:space:]' | sed 's/^$/no/')"
  echo "A_IMAGE=$(docker inspect -f '{{.Image}}' "$A_CT" 2>/dev/null | head -1)"
  echo "A_EDITION=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" 2>/dev/null | sed -n 's/^LANGGRAPH_RUNTIME_EDITION=//p' | head -1)"
  echo "A_JOBS=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" 2>/dev/null | sed -n 's/^N_JOBS_PER_WORKER=//p' | head -1)"
  # ⚠️ 只打**库名**，绝不把 DATABASE_URI 整条打出来（里面有口令）
  echo "A_DB=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" 2>/dev/null | sed -n 's/^DATABASE_URI=//p' | head -1 | sed 's#.*/##')"
  # ⚠️ **不能**写成 `$(docker inspect … 2>/dev/null || echo no)`：docker 对**不存在**的容器
  #    会往 **stdout 打一个空行**再以非 0 退出 ⇒ 命令替换拿到的是「空行 + no」两行 ⇒
  #    PowerShell 侧 `B_EXISTS=(\S+)` 匹配不到 ⇒ `$bExists` 变空串 ⇒ 稳态（没有残留 B 时）
  #    **每一次发版都被自己的守卫拦死**。先剥空白再给默认值。
  b_id=$(docker inspect -f '{{.Id}}' "$B_CT" 2>/dev/null | tr -d '[:space:]')
  echo "B_EXISTS=${b_id:-no}"
  echo "ENV_DEV=$([ -f .env.dev ] && echo yes || echo no)"
  echo "OVERLAY_REPO=$([ -f docker/compose.colorB.dev.yml ] && echo yes || echo no)"
  echo "SWAP_SH=$([ -f "$SWAP_SH" ] && echo yes || echo no)"
  echo "SRC_NEXT=$([ -d "$DEV/src-next" ] && echo yes || echo no)"
  echo "DISK_FREE=$(df -h "$DEV" | tail -1 | awk '{print $4}')"
  # ⚠️ 间距必须用 `[[:space:]]*`：那行是 `set $backend_up  "…"`（**两个空格**）——
  #    写死一个空格会静默匹配不到 ⇒ 打印出空的 LIVE_BACKEND（判据撒谎）。
  echo "LIVE_BACKEND=$(grep -o 'set \$backend_up[[:space:]]*"[^"]*"' "$APP_DIR/docker/nginx.conf" 2>/dev/null | head -1)"
  echo "REDIS_RUNNING=$(docker ps --format '{{.Names}}' | grep -c "redis" || true)"
  echo "--- 在跑的 nl2sql 容器 ---"
  docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'nl2sql' | sort || true
  ;;

# ── 4. 把**仓库里这份** ops_drain.py 拷进目标容器 ──
#  dev 的镜像里有没有这个脚本完全看运气（镜像是从 64 搬来的），
#  所以**永远**从宿主机上这份新的拷进去 —— 判据文本跟着仓库走，不跟着镜像走。
cp_drain)
  ct="${2:?需要容器名}"
  # 色不存在 ⇒ 空转（回退时临时色 B 往往已经被删掉了：稳态就只有 A）
  if ! docker inspect -f '{{.Id}}' "$ct" >/dev/null 2>&1; then
    log "ℹ️ $ct 不存在 —— 跳过 ops_drain 就位（空转）"; exit 0
  fi
  [ -s "$OPS_DRAIN" ] || die "宿主机上没有 $OPS_DRAIN —— 发版脚本第 4 步应当先把它传上来"
  docker cp "$OPS_DRAIN" "$ct:/tmp/nl2sql_ops_drain.py" || die "拷 ops_drain 进 $ct 失败"
  docker exec "$ct" python /tmp/nl2sql_ops_drain.py --help >/dev/null 2>&1 \
    || die "$ct 里跑不起来新 ops_drain（import 失败？）"
  log "✅ 新 ops_drain 已就位于 $ct:/tmp/nl2sql_ops_drain.py"
  ;;

# ── 5. 起空闲第二色（不领 run）──────────────────────────────────
#   第二个参数可以是**任意** src 目录 ⇒ 回退时把 B 挂到 src-prev-<stamp> 复用同一套机械
start_b)
  jobs="${2:-0}"
  src="${3:-$DEV/src-next}"
  [ -f "$src/agent/main_agent.py" ] || die "$src 不像一份 src（缺 agent/main_agent.py）"
  log "起第二色 B（N_JOBS_PER_WORKER=$jobs，src=$src）"
  # 先清再起（铁律③）：B 可能已存在（上一次发版中途失败留下的残留），此时 `up -d`
  # 会走 recreate 路径。
  dcb stop "$B_SVC" >/dev/null 2>&1 || true
  dcb rm -f "$B_SVC" >/dev/null 2>&1 || true
  COLOR_B_SRC="$src" COLOR_B_JOBS="$jobs" dcb up -d --no-deps "$B_SVC" || die "起 B 失败"
  docker ps --format '{{.Names}}' | grep -qx "$B_CT" || die "B 容器没起来"
  code=$(wait_ok "$B_CT" 180) || die "B 的 /ok 未在 180s 内 200（code=$code）—— 常见原因：overlay 的 env_file 找不到、compose 还是 inmem 版型、预检失败"
  echo "B_SRC_MOUNTED=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/src"}}{{.Source}}{{end}}{{end}}' "$B_CT")"
  docker logs "$B_CT" 2>&1 | grep -E '预检通过|预检失败|MCP 工具加载完成' | tail -3
  # ── 预检回归闸：判据是「**B 不比正在接流的 A 差**」，**不是**「B 有没有预检失败」──
  #  dev 的 `.env` 设了 MCP_ALLOW_DEGRADED=0，但语义库连不上等情况仍会让 A/B 都出现
  #  「预检失败 + 降级启动」。按绝对判据写（见到「预检失败」就 die）会把 **A 自己的常态**
  #  当成 B 的失败 ⇒ 这台机器永远发不了版。致命那一档已被上面的 wait_ok 盖住：真正关键的
  #  server 缺失时 `start_server.py` 会 sys.exit(1)，/ok 永远不会 200。
  if docker logs "$B_CT" 2>&1 | grep -q '预检失败'; then
    if ! docker logs "$A_CT" 2>&1 | grep -q '预检失败'; then
      die "B 预检失败而 A 没有 —— 新代码带来 MCP/语义层回归，拒绝切流（见上方日志）"
    fi
    a_fail=$(docker logs "$A_CT" 2>&1 | sed -n 's/.*非关键 server 缺失（\(.*\)），可用工具.*/\1/p' | tail -1 | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' | sort -u)
    b_fail=$(docker logs "$B_CT" 2>&1 | sed -n 's/.*非关键 server 缺失（\(.*\)），可用工具.*/\1/p' | tail -1 | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' | sort -u)
    new_fail=$(comm -13 <(printf '%s\n' "$a_fail") <(printf '%s\n' "$b_fail") | grep -v '^$' || true)
    if [ -n "$new_fail" ]; then
      die "B 新增了 A 没有的缺失 server（$(echo "$new_fail" | tr '\n' ' ')）—— 新代码带来回归，拒绝切流"
    fi
    [ -n "$b_fail" ] || log "⚠️ B 的「降级启动」行解析不出 server 名单（日志格式变了？）—— 本次只做了「A 有失败、B 才有」这层判断"
    log "ℹ️ B 与 A 处于**同等**降级状态（缺失: $(printf '%s' "${b_fail:-无}")）—— 不算回归"
  fi
  log "✅ B 就绪（/ok=200，N_JOBS_PER_WORKER=$jobs）"
  echo "B_JOBS_CHECK=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$B_CT" | sed -n 's/^N_JOBS_PER_WORKER=//p' | head -1)"
  ;;

# ── 6. 提升 B（这是给「提升后是否真的领 run」留的显式断言点）─────
promote_b)
  src="${2:-$DEV/src-next}"
  log "提升 B 为正式色（N_JOBS_PER_WORKER=10，src=$src）"
  dcb stop "$B_SVC" >/dev/null 2>&1 || true
  dcb rm -f "$B_SVC" >/dev/null 2>&1 || true
  COLOR_B_SRC="$src" COLOR_B_JOBS=10 dcb up -d --no-deps "$B_SVC" || die "提升 B 失败"
  code=$(wait_ok "$B_CT" 180) || die "提升后的 B /ok 未恢复 200（code=$code）"
  log "✅ 提升完成（/ok=200，N_JOBS_PER_WORKER=10）"
  ;;

# ── 7. 就地覆盖 nginx.conf + 校验 + reload（失败自动回滚，绝不留下坏配置）──
nginx_apply)
  f="${2:?需要新 nginx.conf 路径}"
  [ -s "$f" ] || die "未提供 nginx.conf（$f 为空）"
  live="$APP_DIR/docker/nginx.conf"
  bak="/tmp/nginx.conf.dev.bak.$(date +%s)"
  cp -p "$live" "$bak" || die "备份 $live 失败"
  # ⚠️ **必须 `cat >` 保持同一个 inode**：compose 把该文件以「文件级 bind mount」挂进
  #    nginx 容器 /etc/nginx/nginx.conf:ro，`mv`/`sed -i` 会换 inode ⇒ 容器仍读到旧内容。
  cat "$f" > "$live" || die "就地写入 $live 失败"
  if docker exec "$NGINX_CT" nginx -t 2>&1; then
    docker exec "$NGINX_CT" nginx -s reload || die "nginx reload 失败"
    log "✅ nginx 已 reload（切流生效；reload 是优雅的，在跑的 SSE 不会被掐）"
    echo "NGINX_APPLY=ok bak=$bak"
  else
    cat "$bak" > "$live"          # 同样就地回滚，别用 cp/mv
    die "nginx -t 失败 —— 已就地回滚到备份（$bak），未 reload。请人看上面的 error"
  fi
  ;;

# ── 7b. 轮询 ──────────────────────────────────────────────────
poll_start)
  nohup setsid bash "$0" poll_loop "${2:-1800}" >/dev/null 2>&1 &
  echo $! > "$POLL_PID"
  log "已启动 1s /ok 轮询（pid=$(cat "$POLL_PID")，上限 ${2:-1800}s，日志 $POLL_LOG）"
  ;;
poll_loop) poll_loop "${2:-1800}" ;;
poll_stop)
  pkill -f 'nl2sql_bg_steps_dev.sh poll_loop' 2>/dev/null || true
  [ -f "$POLL_PID" ] && kill "$(cat "$POLL_PID")" 2>/dev/null || true
  rm -f "$POLL_PID"
  sleep 1
  [ -f "$POLL_LOG" ] || { echo "POLL_TOTAL=0"; echo "POLL_NON200=0"; exit 0; }
  echo "POLL_TOTAL=$(wc -l < "$POLL_LOG" | tr -d ' ')"
  echo "POLL_NON200=$(awk '$2 != "200"' "$POLL_LOG" | wc -l | tr -d ' ')"
  awk '$2 != "200" {print "  ⛔ 非200: ts=" $1 " code=" $2}' "$POLL_LOG" | head -20
  echo "POLL_WINDOW=$(head -1 "$POLL_LOG" | cut -d' ' -f1)..$(tail -1 "$POLL_LOG" | cut -d' ' -f1)"
  ;;

# ── 8. 排空 + 连续 idle 确认 + 停色（**同一个 ssh 会话内**做完，窗口压到毫秒）──
cutover)
  svc="${2:?需要服务名}"; ct="${3:?需要容器名}"; budget="${4:-180}"
  # 色不存在 ⇒ 空转（回退时临时色 B 往往已被删掉：稳态就只有 A）
  if ! docker inspect -f '{{.Id}}' "$ct" >/dev/null 2>&1; then
    log "ℹ️ $ct 不存在 —— 无需排空/停止（空转）"; echo "DRAIN_READY=n/a"; exit 0
  fi
  # 排空判据必须**真的能跑起来**：缺文件就**拒绝停容器**。
  # 否则「不知道有没有在跑的 run」会被当成「已排空」然后照停 —— 那正是这套设计要根除的
  # fail-open。
  docker exec "$ct" test -s /tmp/nl2sql_ops_drain.py \
    || die "$ct 存在但里面没有 /tmp/nl2sql_ops_drain.py —— 先跑 bash $0 cp_drain $ct；拒绝在无法判定排空时停容器"
  ready=no
  if docker exec "$ct" python /tmp/nl2sql_ops_drain.py --budget "$budget"; then
    ready=yes
    for i in 1 2 3; do
      if ! docker exec "$ct" python /tmp/nl2sql_ops_drain.py --budget 0 >/dev/null 2>&1; then
        ready=stale; break
      fi
      sleep 2
    done
  fi
  echo "DRAIN_READY=$ready"
  # ready != yes ⇒ 排空**未确认**（预算耗尽仍有 run / 窗口内又活过来）。仍然停，靠的是
  # `docker stop -t 240` + 应用自身的 SIGTERM 协作式排空（NL2SQL_DRAIN_SECS=180）兜底；
  # 但这句必须响亮，不能安静地过去。
  [ "$ready" = "yes" ] || log "⚠️ 排空未确认（DRAIN_READY=$ready）—— 仍按 stop -t 240 停，由应用自身 SIGTERM 排空兜底，请核对上面 ops_drain 的输出"
  docker stop -t 240 "$ct" >/dev/null || die "停 $ct 失败"
  log "✅ 已停 $ct（$svc）"
  docker logs "$ct" > "/tmp/nl2sql_dev_last_${svc}.log" 2>&1 || true   # rm 会把日志一起带走，先留一份
  docker rm -f "$ct" >/dev/null 2>&1 || log "⚠️ rm $ct 失败（不影响服务，下次 up 会重建）"
  ;;

# ── 9. **目录轮换**（dev 特有）：src → <备份名>、<提升目录> → src ──
#    为什么必须在「停 A 之后、起 A 之前」：A 挂着 /mydata/nl2sql/src，只 mv **目录本身**
#    （改个名，不动里面的文件）⇒ 运行中的容器仍指向原来那个 inode，内容一模一样；
#    等 A 停掉再用 base compose 起回来，它才会挂到新的 src。
#    B 此刻挂在 src-next 那个 inode 上，rotate 把它改名成 src —— B 毫发无损。
rotate_src)
  new="${2:?需要要提升进 src 的目录名}"; prevname="${3:?需要当前 src 的新名字}"
  log "目录轮换：src -> $prevname，$new -> src"
  bash "$SWAP_SH" rotate "$new" "$prevname" || die "目录轮换失败（看上面 dev_swap_src.sh 的输出；它失败时会打印恢复命令）"
  echo "SRC_PREV=$prevname"
  echo "SRC_MOUNTED_NOW=$(readlink -f "$DEV/src")"
  ;;

# ── 10. 用 **base compose** 把常驻色 A 起回来（B 在接流，A 的冷启对用户不可见）──
start_a)
  # 先清再起（铁律③）
  dc stop "$A_SVC" >/dev/null 2>&1 || true
  dc rm -f "$A_SVC" >/dev/null 2>&1 || true
  # ⚠️ **故意不带 overlay**：A 的配置必须回到 base 的原样（N_JOBS_PER_WORKER=10），
  #    这样它的 compose config-hash 与「没有蓝绿这回事」时**完全一致**。
  dc up -d --no-deps --no-build "$A_SVC" || die "起 A 失败"
  code=$(wait_ok "$A_CT" 180) || die "A 的 /ok 未在 180s 内恢复 200（code=$code）"
  echo "A_SRC_MOUNTED=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/src"}}{{.Source}}{{end}}{{end}}' "$A_CT")"
  # ⚠️ 硬闸：A 的 DATABASE_URI 必须与正在接流的 B **逐字相同**。
  #    A 是用**线上 docker-compose.yml**（不带 overlay）起的，所以这条实际在验「服务器的
  #    compose 是否已经是 postgres 版型」。万一它还停在 inmem（`DATABASE_URI: ":memory:"`），
  #    A 会立刻以 10 个槽开始领 run 并把平台元数据写进 pckl —— 那种分裂是静默的。
  #    不一致就**当场把 A 停掉**再喊停（此时流量仍在 B，线上零影响）。
  a_uri=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" | sed -n 's/^DATABASE_URI=//p' | head -1)
  b_uri=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$B_CT" | sed -n 's/^DATABASE_URI=//p' | head -1)
  if [ -z "$a_uri" ] || [ "$a_uri" != "$b_uri" ]; then
    docker stop -t 30 "$A_CT" >/dev/null 2>&1 || true
    die "A 的 DATABASE_URI 与正在接流的 B 不一致（A 的库='$(printf '%s' "$a_uri" | sed 's#.*/##')'，B 的库='$(printf '%s' "$b_uri" | sed 's#.*/##')'）——
     线上 docker-compose.yml 很可能还停在 inmem 版型。**已把 A 停掉**，流量仍在 B，线上不受影响。
     先按手册核对服务器 compose（要 postgres 版型），再单独重跑本步：bash $0 start_a"
  fi
  log "✅ 常驻色 A 已起（/ok=200，DATABASE_URI 与 B 一致，src 已轮换）"
  echo "A_JOBS_CHECK=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" | sed -n 's/^N_JOBS_PER_WORKER=//p' | head -1)"
  ;;

# ── 清残留的 B（发版中途失败 / 事故处理用；只碰 B 这一个容器）──
rm_b)
  docker stop -t 30 "$B_CT" >/dev/null 2>&1 || true
  docker rm -f "$B_CT" >/dev/null 2>&1 || true
  log "✅ B 容器已清掉（若本来就没有，这步是空转）"
  b_id=$(docker inspect -f '{{.Id}}' "$B_CT" 2>/dev/null | tr -d '[:space:]')
  echo "B_EXISTS=${b_id:-no}"
  ;;

# ── 收尾断言 ─────────────────────────────────────────────────
final_checks)
  echo "--- 两色日志里的 'database is locked' 计数 ---"
  for f in /tmp/nl2sql_dev_last_langgraph-api.log /tmp/nl2sql_dev_last_langgraph-api-b.log; do
    if [ -f "$f" ]; then echo "  $(basename "$f"): $(grep -c 'database is locked' "$f") 条"; else echo "  $(basename "$f"): (无日志)"; fi
  done
  echo "--- 现在在跑的 nl2sql 容器（应只剩一个 langgraph-api*）---"
  docker ps --format '{{.Names}}\t{{.Status}}' | grep 'langgraph-api' || true
  echo "--- 平台库计数（发版前后应只增不减）---"
  docker exec "${PROJ}-postgres-1" psql -U nl2sql -d nl2sql_platform -tAc \
    "select 'thread=' || count(*) from thread;" 2>/dev/null || echo "  (查不到 —— 平台库还没建？)"
  echo "--- src 现状 ---"
  ls -1d "$DEV"/src "$DEV"/src-next "$DEV"/src-prev-* "$DEV"/src-rolledback-* 2>/dev/null || true
  ;;
state_write)
  printf 'SRC_PREV=%s\nLIVE_SERVICE=%s\nTS=%s\n' \
    "${2:-}" "${3:-$A_SVC}" "$(date -Is)" > "$STATE"
  cat "$STATE"
  ;;
state_read)
  [ -f "$STATE" ] && cat "$STATE" || echo "NO_STATE"
  ;;

*) die "未知子命令：${1:-<空>}" ;;
esac
'@

Say "== 0/12 预检（只读）=="
# nginx 防呆：仓库模板 vs 线上**逐字**比对（把 $backend_up 那一行的值遮掉再比 ——
# 否则蓝绿用过一次之后，线上那行是另一色，永远"不一致"）。不一致 ⇒ 中止，除非 -Force。
$repoNginx = Join-Path $LocalRoot "docker\nginx.conf"
if (-not (Test-Path $repoNginx)) { Die "找不到 $repoNginx" }
$maskRe   = '(?m)^(\s*set\s+\$backend_up\s+)"[^"]*";'
$repoText = [IO.File]::ReadAllText($repoNginx)
$nHit     = ([regex]::Matches($repoText, $maskRe)).Count
if ($nHit -ne 1) { Die "仓库 nginx.conf 里 `set `$backend_up` 出现 $nHit 次（必须恰好 1 次，替换才有唯一解）" }

$liveLocal = Join-Path $WorkDir "nginx.dev.live.conf"
scp @SshOpt "${Target}:${AppDir}/docker/nginx.conf" $liveLocal
if ($LASTEXITCODE -ne 0) { Die "拉取线上 nginx.conf 失败（拿不到就无法做防呆比对，不冒险继续）" }
$liveText   = [IO.File]::ReadAllText($liveLocal)
$repoMasked = ($repoText -replace $maskRe, '$1"<MASKED>";') -split "`r?`n"
$liveMasked = ($liveText -replace $maskRe, '$1"<MASKED>";') -split "`r?`n"
$diff = Compare-Object $repoMasked $liveMasked
if ($diff) {
    if (-not $Force) {
        Warn "线上 docker/nginx.conf 与仓库模板不一致（已遮掉 `$backend_up` 那一行后仍然不同）："
        $diff | Select-Object -First 40 | ForEach-Object { Write-Host ("  {0} {1}" -f $_.SideIndicator, $_.InputObject) -ForegroundColor DarkYellow }
        Die "中止：直接覆盖会抹掉线上手改过的东西。确认无误后加 -Force 重跑（脚本会先备份再改）"
    }
    Warn "线上 nginx.conf 与仓库模板不一致 —— -Force 已给，继续（会先备份）"
} else {
    Ok "  线上 nginx.conf 与仓库模板一致（遮掉切流那行后逐字相同）"
}

$stepsLocal = Join-Path $WorkDir "nl2sql_bg_steps_dev.sh"
# LF：PS here-string 会带上文件本身的 CRLF，直接写成 .sh 会让 bash 在每行尾看到 \r。
[IO.File]::WriteAllText($stepsLocal, ($BgStepsSh -replace "`r`n", "`n"), $Utf8NoBom)
Scp-UpWithRetry $stepsLocal $StepsSh
# 服务端 src 换装助手：每次发布都送一份，保证两端行为一致
$helper = Join-Path $PSScriptRoot "dev-swap-src.sh"
if (-not (Test-Path $helper)) { Die "缺少服务端助手脚本：$helper" }
Scp-UpWithRetry $helper "${DevDir}/.tmp/dev_swap_src.sh"
# 仓库里这份 ops_drain.py 是排空判据的**唯一权威**（镜像里那份可能是旧逻辑）
$opsLocal = Join-Path $LocalRoot "scripts\ops_drain.py"
if (-not (Test-Path $opsLocal)) { Die "找不到 $opsLocal" }
Scp-UpWithRetry $opsLocal "${DevDir}/.tmp/ops_drain.py"

$pfText = (ssh @SshOpt $Target "chmod +x $StepsSh && bash $StepsSh preflight") -join "`n"
if ($LASTEXITCODE -ne 0) { Die "无法连接服务器（预检失败）—— 检查 VPN / SSH 免密" }
Write-Host $pfText

# ── 预检硬闸：3 条「没做到就根本发不了蓝绿」的前提 ────────────────────
$mEdition = [regex]::Match($pfText, 'A_EDITION=(\S*)')
if (-not $mEdition.Success) { Die "预检输出里没有 A_EDITION=… —— 判不了平台存储版型，拒绝发版" }
if ($mEdition.Groups[1].Value -ne "postgres") {
    # ⚠️ 两个坑（本文件在 5.1 上实测踩到，`[Parser]::ParseFile` 直接语法错）：
    #    ① 不能写成 `Die ("…" + "…") -f $x` —— 跨行的括号表达式当**命令实参**会被拒；
    #    ② 换行续接时 `+` **必须留在行尾**：`"aaa"` 换行 `+ "bbb"` 也报「缺少 )」。
    #       所以先拼成变量再传，且用「行尾运算符」体。
    $msg = ("常驻色 A 的 LANGGRAPH_RUNTIME_EDITION='{0}'（不是 postgres）⇒ 平台存储还是 inmem pckl，" +
            "两色会各持一份内存状态，切色 = 用户会话「消失」。先做完《蓝绿发布_dev手册.md》§1 的地基" +
            "（搬镜像 / 建 nl2sql_platform / 改 compose），再回来发版") -f $mEdition.Groups[1].Value
    Die $msg
}
Ok "  平台存储版型：postgres ✓（A 连的库 = $([regex]::Match($pfText, 'A_DB=(\S*)').Groups[1].Value)）"

if ([regex]::Match($pfText, 'REDIS_RUNNING=(\d+)').Groups[1].Value -eq "0") {
    Die "34 上没有 redis 在跑 ⇒ 两色并存时「旧色在跑的 run」的事件发不到新色的订阅者。先起 redis（手册 §1）"
}
Ok "  redis 在跑 ✓"

# 稳态契约的两条前置检查（不满足就**在动手之前**停）
$liveBackend = ([regex]::Match($liveText, '(?m)^\s*set\s+\$backend_up\s+"([^"]*)";')).Groups[1].Value
$mB = [regex]::Match($pfText, 'B_EXISTS=(\S+)')
if (-not $mB.Success) {
    Die "预检输出里没有可用的 B_EXISTS=… —— 判不了「有没有残留 B」，拒绝发版（预检输出格式变了？）"
}
$bExists = $mB.Groups[1].Value
if ($liveBackend -ne "$ABackend") {
    if ($Force) { Warn "  ⚠️ 线上流量现在在 '$liveBackend'，不在 A（-Force 已给，继续）" }
    else {
        Warn "  ⛔ 线上流量现在在 '$liveBackend'，不在 A（$ABackend）。"
        Warn "     本脚本假设「A 是常驻色、B 只在发版窗口里存在」；当前不满足，说明上一次发版/回退没走完。"
        Warn "     请先二选一（都不需要重新打包）："
        Warn "       · 上一次卡住了      →  .\dev-release-backend-bluegreen.ps1 -Rollback"
        Warn "       · 只留下一个空壳 B  →  ssh $Target 'bash $StepsSh rm_b'"
        Die "拒绝在非稳态上发版（确认要硬来再加 -Force）"
    }
} else { Ok "  流量在常驻色 A 上（$ABackend）✓" }
if ($bExists -ne "no") {
    if ($Force) { Warn "  ⚠️ 已经有残留的 B 容器（$bExists）（-Force 已给，会被第 5 步覆盖）" }
    else { Die "已有残留的 B 容器（$bExists）。先清理：ssh $Target 'bash $StepsSh rm_b'（或加 -Force 让第 5 步重建它）" }
} else { Ok "  没有残留的第二色 ✓" }
if ([regex]::Match($pfText, 'SRC_NEXT=(\S+)').Groups[1].Value -eq "yes") {
    Warn "  上一次发版留下了 $DevDir/src-next —— 第 3 步会把它整个清掉重放（只影响这一个目录）"
}

# ============================================================
# 回退
# ============================================================
if ($Rollback) {
    Say "== 回退模式（**也是蓝绿**）：把上一版 src 挂成临时色 B → 切流到 B → 把 A 换回上一版 → 切回 A → 停 B =="
    # 为什么回退也要走蓝绿：回退最常见的场景是「发完发现新版有问题」，而那时**窗口已经关了**
    # （B 已删、流量在 A）。若直接 stop/rm/up 把 A 换回旧代码，就是拿真用户等一次冷启
    # —— 与「零中断」自相矛盾。所以回退用**同一套 A→B→A 形态**，只是 src 目录角色对调。
    if (-not $PrevSrc) {
        $st = (ssh @SshOpt $Target "bash $StepsSh state_read") -join "`n"
        $PrevSrc = ([regex]::Match($st, 'SRC_PREV=(\S+)')).Groups[1].Value
        if ($PrevSrc -eq "NO_STATE" -or -not $PrevSrc) {
            Die "没有 -PrevSrc，也没有 $STATE —— 回退必须知道上一版代码在哪，拒绝猜。用法：-PrevSrc src-prev-<stamp>"
        }
        Ok "  从 $STATE 取到上一版代码目录 = $PrevSrc"
    }
    $prevPath = "$DevDir/$PrevSrc"
    # ⚠️ 不能写 bash 那套 `cmd \` 换行 || Die …` —— `||` 在 PowerShell 里不是语句分隔符。
    ssh @SshOpt $Target "test -f $prevPath/agent/main_agent.py"
    if ($LASTEXITCODE -ne 0) {
        Die "$prevPath 里没有 agent/main_agent.py —— 目录名写错了？在 34 上 ls -1d $DevDir/src-* 看一眼"
    }
    Ok "  上一版代码目录存在：$prevPath"

    Say "== R1 用**上一版代码**起临时色 B（N_JOBS_PER_WORKER=0，不领 run）=="
    ssh @SshOpt $Target "bash $StepsSh cp_drain $ACt"
    ssh @SshOpt $Target "bash $StepsSh start_b 0 $prevPath"
    if ($LASTEXITCODE -ne 0) { Die "回退用的临时色 B 起不来 —— 线上**零影响**（B 未接流，流量仍在当前色 A）" }

    Say "== R2 提升 B（N_JOBS_PER_WORKER=10）=="
    ssh @SshOpt $Target "bash $StepsSh promote_b $prevPath"
    if ($LASTEXITCODE -ne 0) { Die "提升 B 失败 —— 流量仍在 A，线上零影响，可排查后重跑" }

    Say "== R3 ★ 切流到 B（此刻起用户请求全部落**上一版代码**）=="
    ssh @SshOpt $Target "bash $StepsSh poll_start 1800"
    $rbSwitch = $repoText -replace $maskRe, ('${1}"' + $BBackend + '";')
    $nginxRb = Join-Path $WorkDir "nginx.dev.rollback"
    [IO.File]::WriteAllText($nginxRb, $rbSwitch, $Utf8NoBom)
    Scp-UpWithRetry $nginxRb "${DevDir}/.tmp/nginx.conf.new"
    $tRb = Get-Date
    ssh @SshOpt $Target "bash $StepsSh nginx_apply ${DevDir}/.tmp/nginx.conf.new && rm -f ${DevDir}/.tmp/nginx.conf.new"
    if ($LASTEXITCODE -ne 0) { Die "切流到回退色失败（nginx 已自动回滚，流量仍在当前色）" }

    Say "== R4 排空并停当前色 A（它跑的是要回退掉的那版）=="
    $rbOutA = (ssh @SshOpt $Target "bash $StepsSh cutover $ASvc $ACt $DrainBudget") -join "`n"
    Write-Host $rbOutA
    $rbRcA = $LASTEXITCODE
    $rbDrainA = ([regex]::Match($rbOutA, 'DRAIN_READY=(\S+)')).Groups[1].Value

    Say "== R5 目录轮换：把上一版换回 src（当前这份留档成 src-rolledback-<stamp>）=="
    $rbRot = (ssh @SshOpt $Target "bash $StepsSh rotate_src $PrevSrc src-rolledback-$Stamp") -join "`n"
    Write-Host $rbRot
    if ($LASTEXITCODE -ne 0) { Die "目录轮换失败 —— 流量仍在 B（上一版代码）上，线上不受影响；按上面打印的命令手工恢复后重跑本步" }
    $rbNewPrev = ([regex]::Match($rbRot, 'SRC_PREV=(\S+)')).Groups[1].Value

    Say "== R6 用 base compose 把常驻色 A 起回来（B 在接流，A 的冷启对用户不可见）=="
    ssh @SshOpt $Target "bash $StepsSh start_a"
    if ($LASTEXITCODE -ne 0) { Die "A 起不来 —— 流量仍在 B（上一版代码）上，线上不受影响；修好后重跑本步（bash $StepsSh start_a）即可" }

    Say "== R7 ★ 切流回 A =="
    $rbBack = $repoText -replace $maskRe, ('${1}"' + $ABackend + '";')
    $nginxRb2 = Join-Path $WorkDir "nginx.dev.rollback2"
    [IO.File]::WriteAllText($nginxRb2, $rbBack, $Utf8NoBom)
    Scp-UpWithRetry $nginxRb2 "${DevDir}/.tmp/nginx.conf.new"
    ssh @SshOpt $Target "bash $StepsSh nginx_apply ${DevDir}/.tmp/nginx.conf.new && rm -f ${DevDir}/.tmp/nginx.conf.new"
    if ($LASTEXITCODE -ne 0) { Die "切回 A 失败（nginx 已自动回滚，流量仍在 B）" }

    Say "== R8 排空并停临时色 B =="
    ssh @SshOpt $Target "bash $StepsSh cp_drain $BCt"
    $rbOutB = (ssh @SshOpt $Target "bash $StepsSh cutover $BSvc $BCt $DrainBudget") -join "`n"
    Write-Host $rbOutB
    $rbRcB = $LASTEXITCODE
    $rbDrainB = ([regex]::Match($rbOutB, 'DRAIN_READY=(\S+)')).Groups[1].Value
    $rbSecs = [math]::Round(((Get-Date) - $tRb).TotalSeconds, 1)

    Say "== R9 收尾：停轮询 + 断言 0 个非 200 + 状态换向 =="
    $rbPoll = (ssh @SshOpt $Target "bash $StepsSh poll_stop") -join "`n"
    Write-Host $rbPoll
    $rbNon200 = [int](([regex]::Match($rbPoll, 'POLL_NON200=(\d+)')).Groups[1].Value)
    $rbTotal  = [int](([regex]::Match($rbPoll, 'POLL_TOTAL=(\d+)')).Groups[1].Value)
    # 状态换向：现在「上一版」= 刚被换下去的那份（$rbNewPrev），接流的是回退回来的这一版
    ssh @SshOpt $Target "bash $StepsSh state_write '$rbNewPrev' '$ASvc'"
    ssh @SshOpt $Target "bash $StepsSh final_checks"
    Write-Host ""
    Say "──────────── 本次回退结论 ────────────"
    Write-Host ("  首次切流→停临时色：{0}s（含 A 的重建）；全程 1s/次 /ok 采样共 {1} 个，非 200 **{2}** 个" -f $rbSecs, $rbTotal, $rbNon200)
    if ($rbNon200 -eq 0) { Ok "  ✅ 零中断回退：两次切换 + 停旧色 + 重建 A 全程没有一个非 200" }
    else { Warn "  ⚠️ 有 $rbNon200 个非 200 —— 逐条看上面的 ts/code，并与两次切换时刻对齐" }
    Write-Host ("  排空确认：A={0}  B={1}" -f $rbDrainA, $rbDrainB) -ForegroundColor Gray
    if ($rbRcA -ne 0) { Warn "  ⚠️ 停 A 那步返回非 0，请核对上面的 DRAIN_READY= 值" }
    if ($rbRcB -ne 0) { Warn "  ⚠️ 停 B 那步返回非 0，请核对上面的 DRAIN_READY= 值" }
    Ok "回退完成：流量在 A，src 已是上一版（$PrevSrc），临时色 B 已停。"
    Write-Host ""
    Write-Host "  想再回到新版本：再跑一次 .\dev-release-backend-bluegreen.ps1" -ForegroundColor DarkGray
    Write-Host "  被换下去的那份代码留在 $DevDir/$rbNewPrev（回退一次就能换回来）" -ForegroundColor DarkGray
    exit 0
}

# ============================================================
# 正向发版
# ============================================================
$tar = Join-Path $WorkDir "dev_bg_src.tar.gz"
$remote = "${DevDir}/.tmp/dev_bg_src.tar.gz"

if (-not $SkipPackage) {
    Say "== 1/12 本地打包 src/（工作树，含未提交改动）=="
    if (Test-Path $tar) { Remove-Item $tar -Force }
    # 排除清单与 dev-release-backend.ps1 **逐字相同**（理由见那个脚本的注释）
    $ex = @(
        "--exclude=*/__pycache__", "--exclude=__pycache__",
        "--exclude=*.pyc",
        "--exclude=src/agent/workspace", "--exclude=*/agent/workspace",
        "--exclude=src/agent/workspace-temp", "--exclude=*/agent/workspace-temp",
        "--exclude=src/.tmp", "--exclude=*/.tmp",
        # model_config.json 是 gitignore 的运行时配置，容器读的是 AGENT_DATA_ROOT
        # （/app/data/shared），src 里这份只是旧种子残留。
        "--exclude=src/agent/shared/model_config.json", "--exclude=*/shared/model_config.json"
    )
    & tar -czf $tar @ex -C $LocalRoot src
    if ($LASTEXITCODE -ne 0) { Die "打包失败" }
    $tarMB = [math]::Round((Get-Item $tar).Length/1MB, 2)
    Ok "   打包完成：$tarMB MB（gzip 后）"

    Say "== 2/12 上传到服务器（分片 $ChunkMB MB × N，每片独立重试 $MaxRetry 次）=="
    $partSize = [int]($ChunkMB * 1MB)
    if ($partSize -lt 65536) { Die "ChunkMB 太小（下限 0.0625）" }
    $bytes = [IO.File]::ReadAllBytes($tar)
    $nPart = [math]::Ceiling($bytes.Length / $partSize)
    ssh @SshOpt $Target "mkdir -p ${DevDir}/.tmp && rm -f ${DevDir}/.tmp/dev_bg_src.tar.gz ${DevDir}/.tmp/dev_bg_src.tar.gz.part*" | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "无法连接服务器（清残留失败）—— 检查 VPN" }
    for ($i = 0; $i -lt $nPart; $i++) {
        $off  = $i * $partSize
        $len  = [math]::Min($partSize, $bytes.Length - $off)
        $name = "dev_bg_src.tar.gz.part{0:D2}" -f $i
        $lp   = Join-Path $WorkDir $name
        $fs = [IO.File]::Create($lp); $fs.Write($bytes, $off, $len); $fs.Close()
        Scp-UpWithRetry $lp "${DevDir}/.tmp/$name"
        Remove-Item $lp -ErrorAction SilentlyContinue
        Write-Host ("   分片 {0}/{1} 完成（{2} KB）" -f ($i+1), $nPart, [math]::Round($len/1KB,0)) -ForegroundColor DarkGray
    }
    Ok "   全部 $nPart 片已上传（共 $tarMB MB）"
} else {
    Say "== 1-2/12 跳过打包上传（-SkipPackage，复用 ${DevDir}/.tmp/dev_bg_src.tar.gz）=="
}

Say "== 3/12 服务器拼接 → 校验 → **暂存到 src-next**（绝不动正在接流的 src）=="
# ⚠️ 这里必须用 `stage` 而不是 `swap`：swap 会清空 /mydata/nl2sql/src，而那正是**正在接流的
#    A 容器**挂着的目录 —— 换了等于在用户眼皮底下换代码，而且新代码还没被验证过。
#    stage 把它放到旁边的 src-next，等 B 验证过、A 停下来之后再 rotate 上去。
$remote3 = "set -e; cd ${DevDir}; " +
           "cat .tmp/dev_bg_src.tar.gz.part* > .tmp/dev_bg_src.tar.gz && rm -f .tmp/dev_bg_src.tar.gz.part* && " +
           "bash .tmp/dev_swap_src.sh stage .tmp/dev_bg_src.tar.gz ${DevDir}/src-next; " +
           "rm -f .tmp/dev_bg_src.tar.gz"
$r3 = (ssh @SshOpt $Target $remote3) -join "`n"
Write-Host $r3
if ($LASTEXITCODE -ne 0) { Die "暂存 src-next 失败（看上面的 ERROR 行；src 未被改动，线上零影响）" }

Say "== 4/12 上传 overlay + 就位 ops_drain =="
Scp-UpWithRetry (Join-Path $LocalRoot "docker\compose.colorB.dev.yml") "${AppDir}/docker/compose.colorB.dev.yml"
ssh @SshOpt $Target "chmod +x $StepsSh && bash $StepsSh cp_drain $ACt"
if ($LASTEXITCODE -ne 0) { Die "把新 ops_drain 就位到 A 失败（后面排空会用到它）" }

Say "== 5/12 起空闲第二色 B（N_JOBS_PER_WORKER=0，挂 src-next，不领 run）=="
ssh @SshOpt $Target "bash $StepsSh start_b 0 ${DevDir}/src-next"
if ($LASTEXITCODE -ne 0) { Die "起空闲色 B 失败 —— 线上**零影响**（B 未接流），可排查后重跑" }

Say "== 6/12 提升 B 为正式色（N_JOBS_PER_WORKER=10）=="
# 必须**重建**而不是 restart：N_JOBS_PER_WORKER 是 import 期常量，没有运行期开关。
# 先起空闲色再提升，是为了不让**未经验证的新代码**一上来就从共享队列领真实用户的 run。
ssh @SshOpt $Target "bash $StepsSh promote_b ${DevDir}/src-next"
if ($LASTEXITCODE -ne 0) { Die "提升 B 失败 —— 线上仍在 A 上（nginx 未切），可排查后重跑" }

Say "== 7/12 ★ 切流到 B（先起 1s 轮询，再就地覆盖 nginx.conf + nginx -t + reload）=="
ssh @SshOpt $Target "bash $StepsSh poll_start 1800"
$switchText = $repoText -replace $maskRe, ('${1}"' + $BBackend + '";')
Write-Host "   切流目标行： set `$backend_up  `"$BBackend`";" -ForegroundColor DarkGray
$nginxNew = Join-Path $WorkDir "nginx.dev.switch"
[IO.File]::WriteAllText($nginxNew, $switchText, $Utf8NoBom)
Scp-UpWithRetry $nginxNew "${DevDir}/.tmp/nginx.conf.new"
$tSwitch = Get-Date
ssh @SshOpt $Target "bash $StepsSh nginx_apply ${DevDir}/.tmp/nginx.conf.new && rm -f ${DevDir}/.tmp/nginx.conf.new"
if ($LASTEXITCODE -ne 0) { Die "切流失败（nginx 已自动回滚，流量仍在 A；B 还在跑，可重跑本步）" }
Ok "   ✅ 已切流到 B —— 从这一刻起用户请求全部落 B"

Say "== 8/12 排空旧色 A + 连续 idle 确认 + 停（**同一 ssh 会话内**，窗口压到毫秒）=="
$cutOutA = (ssh @SshOpt $Target "bash $StepsSh cutover $ASvc $ACt $DrainBudget") -join "`n"
Write-Host $cutOutA
$cutRcA = $LASTEXITCODE
$drainA = ([regex]::Match($cutOutA, 'DRAIN_READY=(\S+)')).Groups[1].Value

Say "== 9/12 ★ 目录轮换：src -> src-prev-$Stamp，src-next -> src =="
# A 已经停了，此刻把新代码从 src-next 提升成 src；旧代码留档在 src-prev-<stamp>，回退要用。
# B 此刻挂在 src-next 那个 inode 上 —— rotate 只改目录名，B 毫发无损。
$rotOut = (ssh @SshOpt $Target "bash $StepsSh rotate_src src-next src-prev-$Stamp") -join "`n"
Write-Host $rotOut
if ($LASTEXITCODE -ne 0) { Die "目录轮换失败 —— 流量仍在 B 上，线上不受影响；按上面打印的恢复命令处理" }

Say "== 10/12 用 base compose 把常驻色 A 起回来（B 在接流，A 的冷启对用户不可见）=="
ssh @SshOpt $Target "bash $StepsSh start_a"
if ($LASTEXITCODE -ne 0) { Die "A 起不来 —— 流量仍在 B 上，**线上不受影响**；修好后重跑本步（bash $StepsSh start_a）即可，别从头来" }

Say "== 11/12 ★ 切流回 A =="
$switchBack = $repoText -replace $maskRe, ('${1}"' + $ABackend + '";')
$nginxBack = Join-Path $WorkDir "nginx.dev.switchback"
[IO.File]::WriteAllText($nginxBack, $switchBack, $Utf8NoBom)
Scp-UpWithRetry $nginxBack "${DevDir}/.tmp/nginx.conf.new"
ssh @SshOpt $Target "bash $StepsSh nginx_apply ${DevDir}/.tmp/nginx.conf.new && rm -f ${DevDir}/.tmp/nginx.conf.new"
if ($LASTEXITCODE -ne 0) { Die "切回 A 失败（nginx 已自动回滚，流量仍在 B；A 已跑着新代码，可重跑本步）" }
Ok "   ✅ 已切回常驻色 A（新代码）—— 稳态契约恢复"

Say "== 12/12 排空并停临时色 B + 收尾断言 =="
# ⚠️ 必须先给 **B** 也就位一份 ops_drain：B 是另起的一个容器，`docker cp` 是 per-container。
ssh @SshOpt $Target "bash $StepsSh cp_drain $BCt"
if ($LASTEXITCODE -ne 0) { Warn "  ⚠️ 给 B 就位 ops_drain 失败 —— 排空判据跑不起来，cutover 会拒绝停容器" }
$cutOutB = (ssh @SshOpt $Target "bash $StepsSh cutover $BSvc $BCt $DrainBudget") -join "`n"
Write-Host $cutOutB
$cutRcB = $LASTEXITCODE
$drainB = ([regex]::Match($cutOutB, 'DRAIN_READY=(\S+)')).Groups[1].Value
$secs = [math]::Round(((Get-Date) - $tSwitch).TotalSeconds, 1)

$pollOut = (ssh @SshOpt $Target "bash $StepsSh poll_stop") -join "`n"
Write-Host $pollOut
$non200 = [int](([regex]::Match($pollOut, 'POLL_NON200=(\d+)')).Groups[1].Value)
$total  = [int](([regex]::Match($pollOut, 'POLL_TOTAL=(\d+)')).Groups[1].Value)
ssh @SshOpt $Target "bash $StepsSh state_write 'src-prev-$Stamp' '$ASvc'"
ssh @SshOpt $Target "bash $StepsSh final_checks"

Write-Host ""
Say "──────────── 本次发版结论 ────────────"
Write-Host ("  首次切流→停临时色：{0}s（含 A 的重建）；全程 1s/次 /ok 采样共 {1} 个，非 200 **{2}** 个" -f $secs, $total, $non200)
if ($non200 -eq 0) { Ok "  ✅ 零中断：两次切换 + 停旧色 + 重建 A 全程没有一个非 200（老路 dev-release-backend.ps1 是一次冷启中断）" }
else { Warn "  ⚠️ 有 $non200 个非 200 —— 逐条看上面的 ts/code，并与两次切换的时刻对齐" }
if ($cutRcA -ne 0) { Warn "  ⚠️ 停 A（旧色）那步返回非 0，请核对上面的 DRAIN_READY= 值" }
if ($cutRcB -ne 0) { Warn "  ⚠️ 停 B（临时色）那步返回非 0，请核对上面的 DRAIN_READY= 值" }
Write-Host ("  排空确认：A={0}  B={1}（stale = 切流后旧色又领到 run —— 残余竞态的观测点）" -f $drainA, $drainB) -ForegroundColor Gray
if ($drainA -eq "stale") {
    Warn "  ⚠️ **第 8 步 DRAIN_READY=stale**：切流之后旧色 A 又从共享队列领到了 run —— 那次 run 跑在**旧代码**上（残余竞态）。查 A 的日志（/tmp/nl2sql_dev_last_langgraph-api.log）定位是哪一条"
}
Write-Host "  回退用：上一版代码目录 = src-prev-$Stamp（在 $DevDir 下）"
Write-Host ""
Write-Host "  回退：.\dev-release-backend-bluegreen.ps1 -Rollback" -ForegroundColor Yellow
Write-Host "  ⚠️ 前端不在本次范围内：前端发版仍走 .\dev-release-frontend.ps1，location / 有几秒 502" -ForegroundColor Yellow
