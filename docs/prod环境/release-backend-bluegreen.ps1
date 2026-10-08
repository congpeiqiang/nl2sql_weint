# ============================================================
# NL2SQL 后端发布脚本（**蓝绿 / 真零中断**）—— 阶段 2
# 运行环境：本机 PowerShell（已配置 SSH 密钥免密，无需输密码）
# 用法：  .\release-backend-bluegreen.ps1                       # 正向发版（构建 → 起 B → 提升 → 切流到 B → 停 A → 用新镜像起回 A → 切回 A → 停 B）
#         .\release-backend-bluegreen.ps1 -Rollback             # 回退（用旧镜像 ID 重起 A → 切回 → 停 B）
# 参数：  -LocalRoot / -WorkDir / -Server / -SshUser / -AppDir / -ChunkMB / -MaxRetry   —— 同旧脚本
#         -DrainBudget 180   排空预算（秒）
#         -Rollback          回退模式
#         -OldImageId sha256:…  回退用的旧镜像 ID；不给我会去读 /tmp/nl2sql_bluegreen.state
#         -Force             线上 nginx 与仓库模板不一致 / 流量不在 A / 有残留 B 容器时，允许继续
#
# ── 稳态契约：**A 是常驻色，B 只在发版窗口里存在** ──────────────────────
#  发版以「A 跑旧代码」开始、以「A 跑新代码」结束（中途借 B 顶一下流量）。
#  为什么不在结尾把 B 留成活色（那能少一次切换）：① 下一次发版就没有空闲的第二色了
#  （B 自己就是活色），脚本会第二次跑不动；② 全仓的运维习惯、日志 grep、`release-backend.ps1`、
#  监控都认 `nl2sql-app_langgraph-api_1` 这个容器名。所以宁可多发一次切换、把状态收回到原样。
#
# ── 为什么要有这个脚本（相对 release-backend.ps1）────────────────────────
#  `release-backend.ps1` 是「stop 旧容器 → 冷启新容器」，两段**串行** ⇒ 2026-10-05 实测
#  ≈29 秒全量 502（发版时若有 run 在跑，还要先排空，最长叠加 240s）。它**仍可用**，
#  本脚本只是把同一件事换成蓝绿：用户侧 **0 个 5xx**（含 503）。
#
#  ⚠️ 关键时序修正：现有脚本是「**先排空再停**」——排空期间新提交拿到 **503**
#     （`DrainGateMiddleware`）。503 也是 5xx，用户照样看得见。所以本脚本把顺序**倒过来**：
#     **先切流、再排空**。切流之后旧色再也收不到用户请求，它的排空对任何人不可见 ⇒
#     这才是「零 5xx」而不是「把 502 换成 503」。
#
# ── 本轮交付边界（诚实声明）────────────────────────────────────────────
#  零中断**只覆盖后端/API 链路**。前端发版仍走 `release-frontend.ps1`（stop/rm/up
#  frontend），`location /` 会有几秒 502 —— 前端是无状态静态服务，套同一套 overlay 形态
#  即可覆盖，**本轮不做**。
#
# ── 三条铁律（违反任何一条都会踩 2026-10-05 那颗雷）─────────────────────
#  ① 任何 `docker-compose` 调用都必须带 `--no-deps`：compose v1 的 `up -d <svc>` 会把
#     `depends_on` 的依赖一起纳入收敛计划，而本机（compose v1.29.2 + docker 29）的
#     recreate 路径必炸 `KeyError: 'ContainerConfig'` ⇒ 那次把 postgres 打死、停摆 55 分钟。
#  ② overlay 只**新增**服务名，不许出现任何既有服务的键（否则 base 的 config-hash 会变）。
#  ③ 改配置用 `stop && rm -f && up -d`，不要指望 `up -d` 帮你 recreate。
#  对旧色的**停止**用裸 `docker stop/rm <容器名>`（见 bg_steps.sh 的说明）。
#
# ── 残余竞态（本机无法消除，E2E 里要量它）──────────────────────────────
#  `N_JOBS_PER_WORKER` 只在进程 import 期读一次（`langgraph_api/config/__init__.py:380`），
#  没有运行期开关 ⇒ 切流之后旧色**仍持有** 10 个 run 槽，仍可能从**共享**队列里领到刚提交
#  给新色的 run。我们靠「**同一个 ssh 会话内**：连续 3 次 idle 判定 → 立刻 stop」把窗口压到
#  毫秒级（见 bg_steps.sh 的 cutover）。这是设计上已知、已量化、不可根除的一条。
#  ⚠️ **只有第 7→8 步这一次有害**（旧色 A 跑的是**旧代码**，被它领走的 run 会用旧逻辑跑完）。
#     第 10→11 步停 B 时 A/B 是**同一份镜像**，B 就算领到 run 也是在为同一版代码干活。
# ============================================================
param(
    [string]$LocalRoot = "D:\code_work_space\llm\nl2sql",
    [string]$WorkDir   = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app",
    [string]$Server    = "192.168.25.64",
    [string]$SshUser   = "weint",
    [string]$AppDir    = "/home/weint/apps/nl2sql/nl2sql-app",
    [double]$ChunkMB   = 0.5,
    [int]$MaxRetry     = 8,
    [int]$DrainBudget  = 180,
    [switch]$Rollback,
    [string]$OldImageId = "",
    [switch]$Force
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

# ── 常量：与服务端 bg_steps.sh 必须逐字一致 ──
$Proj  = "nl2sql-app"
$ASvc  = "langgraph-api";     $BSvc = "langgraph-api-b"
$ACt   = "${Proj}_${ASvc}_1"; $BCt  = "${Proj}_${BSvc}_1"
# nginx 上游写法是 `<服务名>:<端口>`；用变量拼，避免 `"$ASvc:2026"` 被 PS 当成
# 作用域变量 `$ASvc:2026`（`:` 在变量名里是合法的 scope/drive 分隔符）。
$ABackend = $ASvc + ":2026"
$BBackend = $BSvc + ":2026"
$StepsSh = "/tmp/nl2sql_bg_steps.sh"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# SSH/SCP 公共选项：keepalive 防链路空闲被判死；ConnectTimeout 避免卡握手。
$SshOpt = @("-o","ServerAliveInterval=20","-o","ServerAliveCountMax=6","-o","ConnectTimeout=15")

function Say  ([string]$m) { Write-Host $m -ForegroundColor Cyan }
function Ok   ([string]$m) { Write-Host $m -ForegroundColor Green }
function Warn ([string]$m) { Write-Host $m -ForegroundColor Yellow }
function Die  ([string]$m) { throw $m }

function Scp-UpWithRetry([string]$Local, [string]$Remote) {
    for ($t = 1; $t -le $MaxRetry; $t++) {
        scp @SshOpt $Local "${SshUser}@${Server}:${Remote}"
        if ($LASTEXITCODE -eq 0) { return }
        Warn "  上传 $Remote 第 $t/$MaxRetry 次被重置，3s 后重试…"
        Start-Sleep -Seconds 3
    }
    Die "上传 $Remote 失败（已重试 $MaxRetry 次）"
}

if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null }

# ============================================================
# 服务器侧步骤脚本（上传到 /tmp 后按子命令调用）
#   —— 抽成脚本而不是拼 ssh 内联命令：内联要跨 PowerShell → ssh → bash 三层转义，
#      历史上「`\"` 提前闭合 PS 字符串」那条坑就是这么来的（见 memory
#      `powershell-doublequote-escape-release-step5`）。这里用单引号 here-string，
#      内容对 PS 完全透明。
# ============================================================
$BgStepsSh = @'
#!/usr/bin/env bash
# 蓝绿发版：服务器侧步骤（由 release-backend-bluegreen.ps1 上传到 /tmp 再调用）
#   nl2sql_bg_steps.sh <子命令> [参数…]
#
# 停止旧色**故意**用裸 `docker stop/rm <容器名>` 而不是 `docker-compose stop <服务名>`：
#  ① 少一次 compose 解析（两个文件）⇒ 把「最后一次 idle 判定 → 真的停」的窗口压到毫秒
#     —— 这正是残余竞态（旧色仍可能从共享队列领 run）的缓解手段本身；
#  ② 只可能碰到这一个容器，**物理上**不可能波及 postgres/redis。
set -u

PROJ=nl2sql-app
A_SVC=langgraph-api
B_SVC=langgraph-api-b
A_CT="${PROJ}_${A_SVC}_1"
B_CT="${PROJ}_${B_SVC}_1"
NGINX_CT="${PROJ}_nginx_1"
APP_DIR=/home/weint/apps/nl2sql/nl2sql-app
LOG=/tmp/nl2sql_bg.log
POLL_LOG=/tmp/nl2sql_bg_poll.log
POLL_PID=/tmp/nl2sql_bg_poll.pid
STATE=/tmp/nl2sql_bluegreen.state

cd "$APP_DIR" || { echo "❌ 进不了 $APP_DIR" >&2; exit 1; }

log() { echo "[$(date '+%F %T')] $*"; }
die() { echo "❌ $*" >&2; exit 1; }
# 只用 base（对 A 的操作）；对 B 的操作走 dcb（必须同时给 overlay）
dcb() { docker-compose -f docker-compose.yml -f docker/compose.colorB.yml "$@"; }

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
  # 「旧镜像」= **常驻色 A 此刻真正在跑的**那一份，**不是** `langgraph-api` 这个 tag。
  # ⚠️ 上一次发版一构建完就把 tag 指到新镜像上了 ⇒ 读 tag 会让「旧镜像」等于**新**镜像，
  #    于是回退变成「退到同一版」—— **看起来成功、实际什么都没退**（假回退）。
  #    只有在 A 容器不存在（首次部署）时才退回读 tag。
  a_img=$(docker inspect -f '{{.Image}}' "$A_CT" 2>/dev/null | head -1)
  [ -n "$a_img" ] || a_img=$(docker image inspect -f '{{.Id}}' "${PROJ}_${A_SVC}" 2>/dev/null | head -1)
  echo "OLD_IMAGE_ID=${a_img:-none}"
  echo "A_RUNNING_IMAGE=$(docker inspect -f '{{.Image}}' "$A_CT" 2>/dev/null | head -1)"
  echo "A_JOBS=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" 2>/dev/null | sed -n 's/^N_JOBS_PER_WORKER=//p' | head -1)"
  # ⚠️ **不能**写成 `$(docker inspect … 2>/dev/null || echo no)`：本机 docker 对**不存在**的容器
  #    会往 **stdout 打一个空行**再以非 0 退出 ⇒ 命令替换拿到的是「空行 + no」两行 ⇒
  #    PowerShell 侧 `B_EXISTS=(\S+)` 匹配不到 ⇒ `$bExists` 变空串 ⇒ 稳态（没有残留 B 时）
  #    **每一次发版都被自己的守卫拦死**（2026-10-08 回退演练实撞；正向那次只是因为带了 -Force
  #    才没暴露 —— 当时有残留 B，走了 warn 分支）。先剥空白再给默认值。
  b_id=$(docker inspect -f '{{.Id}}' "$B_CT" 2>/dev/null | tr -d '[:space:]')
  echo "B_EXISTS=${b_id:-no}"
  echo "ENV_PROD=$([ -f .env.prod ] && echo yes || echo no)"
  echo "OVERLAY_REPO=$([ -f docker/compose.colorB.yml ] && echo yes || echo no)"
  echo "DISK_FREE=$(df -h . | tail -1 | awk '{print $4}')"
  # ⚠️ 间距必须用 `[[:space:]]*`：线上那行是 `set $backend_up  "…"`（**两个空格**，
  #    见 docker/nginx.conf:75）—— 写死一个空格会静默匹配不到 ⇒ 打印出空的 LIVE_BACKEND。
  echo "LIVE_BACKEND=$(grep -o 'set \$backend_up[[:space:]]*"[^"]*"' "$APP_DIR/docker/nginx.conf" 2>/dev/null | head -1)"
  echo "REDIS_RUNNING=$(docker ps --format '{{.Names}}' | grep -c 'redis' || true)"
  echo "--- 在跑的容器 ---"
  docker ps --format '{{.Names}}\t{{.Status}}' | sort
  ;;

# ── 4. 把**仓库里这份** ops_drain.py 拷进目标容器（旧镜像里那份是旧逻辑）──
cp_drain)
  ct="${2:?需要容器名}"
  # 色不存在 ⇒ 空转（回退时临时色 B 往往已经被删掉了：稳态就只有 A）
  if ! docker inspect -f '{{.Id}}' "$ct" >/dev/null 2>&1; then
    log "ℹ️ $ct 不存在 —— 跳过 ops_drain 就位（空转）"; exit 0
  fi
  docker cp backend/scripts/ops_drain.py "$ct:/tmp/nl2sql_ops_drain.py" || die "拷 ops_drain 进 $ct 失败"
  docker exec "$ct" python /tmp/nl2sql_ops_drain.py --help >/dev/null 2>&1 \
    || die "$ct 里跑不起来新 ops_drain（import 失败？）"
  log "✅ 新 ops_drain 已就位于 $ct:/tmp/nl2sql_ops_drain.py"
  ;;

# ── 5. 起空闲第二色（不领 run）──────────────────────────────────
start_b)
  jobs="${2:-0}"
  log "起第二色 B（N_JOBS_PER_WORKER=$jobs）"
  # 先清再起（铁律③）：B 可能已存在（上一次发版中途失败留下的残留），此时 `up -d`
  # 会走 recreate 路径 —— 那正是会炸 KeyError:'ContainerConfig' 的那条路。
  dcb stop "$B_SVC" >/dev/null 2>&1 || true
  dcb rm -f "$B_SVC" >/dev/null 2>&1 || true
  COLOR_B_JOBS="$jobs" dcb up -d --no-deps "$B_SVC" || die "起 B 失败"
  docker ps --format '{{.Names}}' | grep -qx "$B_CT" || die "B 容器没起来"
  code=$(wait_ok "$B_CT" 150) || die "B 的 /ok 未在 150s 内 200（code=$code）—— 常见原因：overlay 的 env_file 找不到、镜像 ID 没替换、预检失败"
  docker logs "$B_CT" 2>&1 | grep -E '预检通过|预检失败|MCP 工具加载完成' | tail -3
  # ── 预检回归闸：判据是「**B 不比正在接流的 A 差**」，**不是**「B 有没有预检失败」──
  #  本机 `.env.prod` 设了 MCP_ALLOW_DEGRADED=1，而 wrenai_chinook 这个 [CRITICAL] 语义库
  #  连不上 ⇒ **A 与 B 的启动日志里都**有「❌❌❌ MCP 启动预检失败」+「降级启动」。
  #  按绝对判据写（`grep -q 预检失败`）会把 **A 自己的常态** 当成 B 的失败 ⇒ 这台机器永远发不了版
  #  （2026-10-08 首次 E2E 就是这么被拦下的）。致命那一档其实已被上面的 wait_ok 盖住：
  #  真正关键 server 缺失时 `start_server.py:329` 会 sys.exit(1)，/ok 永远不会 200。
  #  这里只补一种情况：**A 好、B 坏** —— 新镜像带来的 MCP/语义层回归。
  if docker logs "$B_CT" 2>&1 | grep -q '预检失败'; then
    if ! docker logs "$A_CT" 2>&1 | grep -q '预检失败'; then
      die "B 预检失败而 A 没有 —— 新镜像带来 MCP/语义层回归，拒绝切流（见上方日志）"
    fi
    a_fail=$(docker logs "$A_CT" 2>&1 | sed -n 's/.*非关键 server 缺失（\(.*\)），可用工具.*/\1/p' | tail -1 | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' | sort -u)
    b_fail=$(docker logs "$B_CT" 2>&1 | sed -n 's/.*非关键 server 缺失（\(.*\)），可用工具.*/\1/p' | tail -1 | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' | sort -u)
    new_fail=$(comm -13 <(printf '%s\n' "$a_fail") <(printf '%s\n' "$b_fail") | grep -v '^$' || true)
    if [ -n "$new_fail" ]; then
      die "B 新增了 A 没有的缺失 server（$(echo "$new_fail" | tr '\n' ' ')）—— 新镜像带来回归，拒绝切流"
    fi
    [ -n "$b_fail" ] || log "⚠️ B 的「降级启动」行解析不出 server 名单（日志格式变了？）—— 本次只做了「A 有失败、B 才有」这层判断"
    log "ℹ️ B 与 A 处于**同等**降级状态（缺失: $(printf '%s' "${b_fail:-无}")）—— 不算回归"
  fi
  log "✅ B 就绪（/ok=200，N_JOBS_PER_WORKER=$jobs）"
  echo "B_JOBS_CHECK=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$B_CT" | sed -n 's/^N_JOBS_PER_WORKER=//p' | head -1)"
  ;;

# ── 6. 提升 B（这是给「提升后是否真的领 run」留的显式断言点）─────
promote_b)
  log "提升 B 为正式色（N_JOBS_PER_WORKER=10）"
  dcb stop "$B_SVC" >/dev/null 2>&1 || true
  dcb rm -f "$B_SVC" >/dev/null 2>&1 || true
  COLOR_B_JOBS=10 dcb up -d --no-deps "$B_SVC" || die "提升 B 失败"
  code=$(wait_ok "$B_CT" 150) || die "提升后的 B /ok 未恢复 200（code=$code）"
  log "✅ 提升完成（/ok=200，N_JOBS_PER_WORKER=10）"
  ;;

# ── 7. 就地覆盖 nginx.conf + 校验 + reload（失败自动回滚，绝不留下坏配置）──
nginx_apply)
  f="${2:?需要新 nginx.conf 路径}"
  [ -s "$f" ] || die "未提供 nginx.conf（$f 为空）"
  live="$APP_DIR/docker/nginx.conf"
  bak="/tmp/nginx.conf.bak.$(date +%s)"
  cp -p "$live" "$bak" || die "备份 $live 失败"
  # ⚠️ **必须 `cat >` 保持同一个 inode**：`docker/compose.prod.yml` 把该文件以
  #    「文件级 bind mount」挂进 nginx 容器 /etc/nginx/nginx.conf:ro，`mv`/`sed -i`
  #    会换 inode ⇒ 容器仍读到旧内容（改了却像没改）。
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
  pkill -f 'nl2sql_bg_steps.sh poll_loop' 2>/dev/null || true
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
  # fail-open（2026-10-08 第 11 步 B 里没这份脚本，排空整段被静默跳过，只因当时零 run 才没出事）。
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
  docker logs "$ct" > "/tmp/nl2sql_last_${svc}.log" 2>&1 || true   # rm 会把日志一起带走，先留一份
  docker rm -f "$ct" >/dev/null 2>&1 || log "⚠️ rm $ct 失败（不影响服务，下次 up 会重建）"
  ;;

# ── 9. 收尾断言 ───────────────────────────────────────────────
final_checks)
  echo "--- 两色日志里的 'database is locked' 计数 ---"
  for f in /tmp/nl2sql_last_langgraph-api.log /tmp/nl2sql_last_langgraph-api-b.log; do
    if [ -f "$f" ]; then echo "  $(basename "$f"): $(grep -c 'database is locked' "$f") 条"; else echo "  $(basename "$f"): (无日志)"; fi
  done
  echo "--- 现在在跑的容器（应只剩一个 langgraph-api*）---"
  docker ps --format '{{.Names}}\t{{.Status}}' | grep 'langgraph-api' || true
  ;;
state_write)
  printf 'OLD_IMAGE_ID=%s\nNEW_IMAGE_ID=%s\nLIVE_SERVICE=%s\nTS=%s\n' \
    "${2:-}" "${3:-}" "${4:-}" "$(date -Is)" > "$STATE"
  cat "$STATE"
  ;;
state_read)
  [ -f "$STATE" ] && cat "$STATE" || echo "NO_STATE"
  ;;

# ── 9b. 用**新镜像**把常驻色 A 起回来（B 此刻在接流，A 的冷启对用户不可见）──
#     为什么发版要绕回 A：稳态契约是「A 是常驻色、B 只在发版窗口里存在」（见文件头）。
#     若发完就让 B 常驻，下一次发版就没有「空闲的第二色」可用了（B 自己就是活色），
#     而且 `release-backend.ps1`、运维习惯、日志 grep 全都认 A 的容器名。
start_a_new)
  # 先清再起（铁律③）：容器若已存在，`up -d` 可能走 recreate 路径 —— 那正是会炸
  # KeyError:'ContainerConfig' 的那条路。`rm -f` 之后 up 只会 create。
  docker-compose stop "$A_SVC" >/dev/null 2>&1 || true
  docker-compose rm -f "$A_SVC" >/dev/null 2>&1 || true
  # ⚠️ **故意不带 overlay**：A 的配置必须回到 base 的原样（N_JOBS_PER_WORKER=10），
  #    这样它的 compose config-hash 与「没有蓝绿这回事」时**完全一致**，将来任何
  #    `up -d` 都不会因为 hash 陌生而想 recreate（那会撞 2026-10-05 那颗雷）。
  docker-compose up -d --no-deps --no-build "$A_SVC" || die "用新镜像起 A 失败"
  code=$(wait_ok "$A_CT" 150) || die "A 的 /ok 未在 150s 内恢复 200（code=$code）"
  # ⚠️ 硬闸：A 的 DATABASE_URI 必须与正在接流的 B **逐字相同**。
  #    A 是用**线上 docker-compose.yml**（不带 overlay）起的，所以这条实际在验「服务器的
  #    compose 是否已经是 postgres 版型」。万一它还停在 inmem（`DATABASE_URI: ":memory:"`），
  #    A 会立刻以 10 个槽开始领 run 并把平台元数据写进 pckl —— 那种分裂是静默的。
  #    不一致就**当场把 A 停掉**再喊停（此时流量仍在 B，线上零影响）。
  a_uri=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" | sed -n 's/^DATABASE_URI=//p' | head -1)
  b_uri=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$B_CT" | sed -n 's/^DATABASE_URI=//p' | head -1)
  if [ -z "$a_uri" ] || [ "$a_uri" != "$b_uri" ]; then
    docker stop -t 30 "$A_CT" >/dev/null 2>&1 || true
    die "A 的 DATABASE_URI('${a_uri:-<空>}') 与正在接流的 B('${b_uri:-<空>}') 不一致 ——
     线上 docker-compose.yml 很可能还停在 inmem 版型。**已把 A 停掉**，流量仍在 B，线上不受影响。
     先按手册 §1 核对服务器 compose（要 postgres 版型），再单独重跑本步：bash $0 start_a_new"
  fi
  log "✅ 常驻色 A 已用新镜像起来（/ok=200，DATABASE_URI 与 B 一致）"
  echo "A_JOBS_CHECK=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$A_CT" | sed -n 's/^N_JOBS_PER_WORKER=//p' | head -1)"
  ;;

# ── 清残留的 B（发版中途失败 / 事故处理用；只碰 B 这一个容器）──
rm_b)
  docker stop -t 30 "$B_CT" >/dev/null 2>&1 || true
  docker rm -f "$B_CT" >/dev/null 2>&1 || true
  log "✅ B 容器已清掉（若本来就没有，这步是空转）"
  # ⚠️ **不能**写成 `$(docker inspect … 2>/dev/null || echo no)`：本机 docker 对**不存在**的容器
  #    会往 **stdout 打一个空行**再以非 0 退出 ⇒ 命令替换拿到的是「空行 + no」两行 ⇒
  #    PowerShell 侧 `B_EXISTS=(\S+)` 匹配不到 ⇒ `$bExists` 变空串 ⇒ 稳态（没有残留 B 时）
  #    **每一次发版都被自己的守卫拦死**（2026-10-08 回退演练实撞；正向那次只是因为带了 -Force
  #    才没暴露 —— 当时有残留 B，走了 warn 分支）。先剥空白再给默认值。
  b_id=$(docker inspect -f '{{.Id}}' "$B_CT" 2>/dev/null | tr -d '[:space:]')
  echo "B_EXISTS=${b_id:-no}"
  ;;

# ── 回退路径 ──────────────────────────────────────────────────
retag_old)
  id="${2:?需要旧镜像 ID}"
  docker image inspect -f '{{.Id}}' "$id" >/dev/null 2>&1 || die "找不到镜像 $id（被 prune 掉了？）"
  docker tag "$id" "${PROJ}_${A_SVC}:latest" || die "retag 失败"
  echo "RETAG_OK=$(docker image inspect -f '{{.Id}}' "${PROJ}_${A_SVC}:latest")"
  ;;
start_a_old)   # 用**已 tag 好的旧镜像**起 A，绝不重新构建（--no-build 是硬保证）
  # 先清再起（铁律③）：容器若已存在，`up -d` 可能走 recreate 路径 —— 那正是会炸
  # KeyError:'ContainerConfig' 的那条路。`rm -f` 之后 up 只会 create。
  docker-compose stop "$A_SVC" >/dev/null 2>&1 || true
  docker-compose rm -f "$A_SVC" >/dev/null 2>&1 || true
  docker-compose up -d --no-deps --no-build "$A_SVC" || die "用旧镜像起 A 失败"
  code=$(wait_ok "$A_CT" 150) || die "旧色 A 的 /ok 未恢复 200（code=$code）"
  log "✅ 旧色 A 已起（/ok=200）"
  ;;

*) die "未知子命令：${1:-<空>}" ;;
esac
'@

Say "== 0/12 预检（只读）=="
# nginx 防呆：仓库模板 vs 线上**逐字**比对（把 $backend_up 那一行的值遮掉再比 ——
# 否则蓝绿用过一次之后，线上那行是另一色，永远"不一致"）。不一致 ⇒ 中止，除非 -Force
# —— 不悄悄抹掉别人手改过的东西。
$repoNginx = Join-Path $LocalRoot "docker\nginx.conf"
if (-not (Test-Path $repoNginx)) { Die "找不到 $repoNginx" }
$maskRe   = '(?m)^(\s*set\s+\$backend_up\s+)"[^"]*";'
$repoText = [IO.File]::ReadAllText($repoNginx)
$nHit     = ([regex]::Matches($repoText, $maskRe)).Count
if ($nHit -ne 1) { Die "仓库 nginx.conf 里 `set `$backend_up` 出现 $nHit 次（必须恰好 1 次，替换才有唯一解）" }

$liveLocal = Join-Path $WorkDir "nginx.live.conf"
scp @SshOpt "${SshUser}@${Server}:${AppDir}/docker/nginx.conf" $liveLocal
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

$stepsLocal = Join-Path $WorkDir "nl2sql_bg_steps.sh"
# LF：PS here-string 会带上文件本身的 CRLF，直接写成 .sh 会让 bash 在每行尾看到 \r。
[IO.File]::WriteAllText($stepsLocal, ($BgStepsSh -replace "`r`n", "`n"), $Utf8NoBom)
Scp-UpWithRetry $stepsLocal $StepsSh
$pfText = (ssh @SshOpt "${SshUser}@${Server}" "chmod +x $StepsSh && bash $StepsSh preflight") -join "`n"
if ($LASTEXITCODE -ne 0) { Die "无法连接服务器（预检失败）—— 检查 VPN" }
Write-Host $pfText
# ⚠️ 变量名**故意不叫** $oldImageId：PowerShell 变量名大小写不敏感，那样会**静默覆盖**
#    `-OldImageId` 参数（回退时用它会拿"当前色"的镜像去当"旧版"，回退到同一个版本 = 假回退）。
$curImageId = ([regex]::Match($pfText, 'OLD_IMAGE_ID=(\S+)')).Groups[1].Value
if (-not $curImageId -or $curImageId -eq "none") { Warn "  取不到当前 langgraph-api 镜像 ID（首次部署？）—— 回退将不可用" }
else { Ok "  当前线上镜像 ID（发版后即成为「旧镜像」）= $curImageId" }

# ── 稳态契约的两条前置检查（不满足就**在动手之前**停）───────────────────
#  契约：A 是常驻色、B 只在发版窗口里存在。所以进发版前必须「流量在 A 且没有残留 B」。
#  这两条把「脚本跑第二遍」这种输入变成**响亮的拒绝**，而不是走到一半才炸
#  （不检查的话，第二遍会在第 4 步 `cp_drain $ACt` 处失败 —— 那时 A 的容器已经不存在了）。
$liveBackend = ([regex]::Match($liveText, '(?m)^\s*set\s+\$backend_up\s+"([^"]*)";')).Groups[1].Value
# 取不到就**响亮地拒绝**，不要把空串当成「没有残留 B」（那正是「判据撒谎」）
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
        Warn "     请先二选一（都不需要重新构建代码）："
        Warn "       · 上一次是正向发版卡住了 →  .\release-backend-bluegreen.ps1 -Rollback       # 切回 A 并停 B"
        Warn "       · 只是留下了一个没接流的空壳 B →  ssh ${SshUser}@${Server} 'bash $StepsSh rm_b'"
        Die "拒绝在非稳态上发版（确认要硬来再加 -Force）"
    }
} else { Ok "  流量在常驻色 A 上（$ABackend）✓" }
if ($bExists -ne "no") {
    if ($Force) { Warn "  ⚠️ 已经有残留的 B 容器（$bExists）（-Force 已给，会被第 5 步覆盖）" }
    else {
        Die "已有残留的 B 容器（$bExists）。先清理：ssh ${SshUser}@${Server} 'bash $StepsSh rm_b'（或加 -Force 让第 5 步重建它）"
    }
} else { Ok "  没有残留的第二色 ✓" }

if ($Rollback) {
    Say "== 回退模式（**也是蓝绿**）：用旧镜像起临时色 B → 切流到 B → 把 A 换成旧镜像 → 切回 A → 停 B =="
    # 为什么回退也要走蓝绿：回退最常见的场景是「发完发现新版有问题」，而那时**窗口已经关了**
    # （B 已删、流量在 A）。若直接 `stop/rm/up` 把 A 换成旧镜像，就是拿真用户等 ≈26s 冷启 ——
    # 与「零中断」自相矛盾。所以回退用**同一套 A→B→A 形态**，只是镜像角色对调。
    $st = (ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh state_read") -join "`n"
    $stNewImageId = ([regex]::Match($st, 'NEW_IMAGE_ID=(\S+)')).Groups[1].Value
    if (-not $OldImageId) {
        $OldImageId = ([regex]::Match($st, 'OLD_IMAGE_ID=(\S+)')).Groups[1].Value
        if (-not $OldImageId -or $OldImageId -eq "NO_STATE") {
            Die "没有 -OldImageId，也没有 /tmp/nl2sql_bluegreen.state —— 回退必须有旧镜像 ID，拒绝猜"
        }
        Ok "  从 /tmp/nl2sql_bluegreen.state 取到旧镜像 ID = $OldImageId"
    }
    if (-not $stNewImageId) { $stNewImageId = $curImageId }   # 兜底：被退役的＝当前常驻色那份
    Ok "  本次要退役的镜像（= 现在常驻色 A 跑的那份）= $stNewImageId"

    $overlayText = [IO.File]::ReadAllText((Join-Path $LocalRoot "docker\compose.colorB.yml"))
    if ($overlayText -notmatch 'IMAGE_ID_PLACEHOLDER') { Die "overlay 里没有 IMAGE_ID_PLACEHOLDER（模板被改过？）" }

    Say "== R1 用**旧镜像**起临时色 B（N_JOBS_PER_WORKER=0，不领 run）=="
    $overlayRb = Join-Path $WorkDir "compose.colorB.rollback.yml"
    [IO.File]::WriteAllText($overlayRb, ($overlayText -replace 'IMAGE_ID_PLACEHOLDER', $OldImageId), $Utf8NoBom)
    Scp-UpWithRetry $overlayRb "${AppDir}/docker/compose.colorB.yml"
    Scp-UpWithRetry $stepsLocal $StepsSh
    ssh @SshOpt "${SshUser}@${Server}" "chmod +x $StepsSh && bash $StepsSh cp_drain $ACt"
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh start_b 0"
    if ($LASTEXITCODE -ne 0) { Die "回退用的临时色 B 起不来 —— 线上**零影响**（B 未接流，流量仍在当前色 A）" }

    Say "== R2 提升 B（N_JOBS_PER_WORKER=10）=="
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh promote_b"
    if ($LASTEXITCODE -ne 0) { Die "提升 B 失败 —— 流量仍在 A，线上零影响，可排查后重跑" }

    Say "== R3 ★ 切流到 B（此刻起用户请求全部落**旧镜像**）=="
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh poll_start 1800"
    $rbSwitch = $repoText -replace $maskRe, ('${1}"' + $BBackend + '";')
    $nginxRb = Join-Path $WorkDir "nginx.conf.rollback"
    [IO.File]::WriteAllText($nginxRb, $rbSwitch, $Utf8NoBom)
    Scp-UpWithRetry $nginxRb "${AppDir}/docker/nginx.conf.new"
    $tRb = Get-Date
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh nginx_apply ${AppDir}/docker/nginx.conf.new && rm -f ${AppDir}/docker/nginx.conf.new"
    if ($LASTEXITCODE -ne 0) { Die "切流到回退色失败（nginx 已自动回滚，流量仍在当前色）" }

    Say "== R4 排空并停当前色 A（它跑的是要回退掉的那版）=="
    $rbOutA = (ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh cutover $ASvc $ACt $DrainBudget") -join "`n"
    Write-Host $rbOutA
    $rbRcA = $LASTEXITCODE
    $rbDrainA = ([regex]::Match($rbOutA, 'DRAIN_READY=(\S+)')).Groups[1].Value

    Say "== R5 把常驻色 A 换成**旧镜像**起回来（B 在接流，A 的冷启对用户不可见）=="
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh retag_old '$OldImageId' && bash $StepsSh start_a_old"
    if ($LASTEXITCODE -ne 0) { Die "旧色 A 起不来 —— 流量仍在 B（旧镜像）上，线上不受影响；人工排查后重跑本步" }

    Say "== R6 ★ 切流回 A =="
    $rbBack = $repoText -replace $maskRe, ('${1}"' + $ABackend + '";')
    $nginxRb2 = Join-Path $WorkDir "nginx.conf.rollback2"
    [IO.File]::WriteAllText($nginxRb2, $rbBack, $Utf8NoBom)
    Scp-UpWithRetry $nginxRb2 "${AppDir}/docker/nginx.conf.new"
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh nginx_apply ${AppDir}/docker/nginx.conf.new && rm -f ${AppDir}/docker/nginx.conf.new"
    if ($LASTEXITCODE -ne 0) { Die "切回 A 失败（nginx 已自动回滚，流量仍在 B）" }

    Say "== R7 排空并停临时色 B =="
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh cp_drain $BCt"
    $rbOutB = (ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh cutover $BSvc $BCt $DrainBudget") -join "`n"
    Write-Host $rbOutB
    $rbRcB = $LASTEXITCODE
    $rbDrainB = ([regex]::Match($rbOutB, 'DRAIN_READY=(\S+)')).Groups[1].Value
    $rbSecs = [math]::Round(((Get-Date) - $tRb).TotalSeconds, 1)

    Say "== R8 收尾：停轮询 + 断言 0 个非 200 + 状态换向 =="
    $rbPoll = (ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh poll_stop") -join "`n"
    Write-Host $rbPoll
    $rbNon200 = [int](([regex]::Match($rbPoll, 'POLL_NON200=(\d+)')).Groups[1].Value)
    $rbTotal  = [int](([regex]::Match($rbPoll, 'POLL_TOTAL=(\d+)')).Groups[1].Value)
    # 状态换向：现在「旧」= 刚被退役的那份（$stNewImageId），接流的是回退回来的镜像（$OldImageId）
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh state_write '$stNewImageId' '$OldImageId' '$ASvc'"
    ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh final_checks"
    Write-Host ""
    Say "──────────── 本次回退结论 ────────────"
    Write-Host ("  首次切流→停临时色：{0}s（含 A 的重建）；全程 1s/次 /ok 采样共 {1} 个，非 200 **{2}** 个" -f $rbSecs, $rbTotal, $rbNon200)
    if ($rbNon200 -eq 0) { Ok "  ✅ 零中断回退：两次切换 + 停旧色 + 重建 A 全程没有一个非 200" }
    else { Warn "  ⚠️ 有 $rbNon200 个非 200 —— 逐条看上面的 ts/code，并与两次切换时刻对齐" }
    Write-Host ("  排空确认：A={0}  B={1}" -f $rbDrainA, $rbDrainB) -ForegroundColor Gray
    if ($rbRcA -ne 0) { Warn "  ⚠️ 停 A 那步返回非 0，请核对上面的 DRAIN_READY= 值" }
    if ($rbRcB -ne 0) { Warn "  ⚠️ 停 B 那步返回非 0，请核对上面的 DRAIN_READY= 值" }
    Ok "回退完成：流量在 A（镜像 $OldImageId），临时色 B 已停。**注意**：A 现在跑的是**上一版**代码"
    Write-Host ""
    Write-Host "  想再回到新版本：再跑一次 .\release-backend-bluegreen.ps1（会把当前镜像当「旧镜像」记下）" -ForegroundColor DarkGray
    exit 0
}

# ============================================================
# 正向发版
# ============================================================
$tar = Join-Path $WorkDir "backend_release.tar.gz"
if (Test-Path $tar) { Remove-Item $tar }

Say "== 1/12 本地打包（gzip + 排除大目录/环境变量/探针）=="
# 与 release-backend.ps1 逐字相同：排除项的理由见那个脚本的文件头长注释
# （.env.dev 是**已跟踪且含明文密钥**的文件，绝不能进包；docker/ 不进包 ⇒
#  本脚本另行上传 overlay 与 nginx.conf）。
tar -czf $tar `
  --exclude=.venv --exclude=.git --exclude=logs --exclude=.langgraph_api `
  --exclude=.idea --exclude=docs --exclude=.tmp --exclude=__pycache__ `
  --exclude=docker --exclude="*.bin" --exclude="*.log" `
  --exclude=.env --exclude=.env.prod --exclude=.env.dev `
  --exclude=src/agent/workspace-temp `
  --exclude=src/agent/shared/model_config.json `
  -C $LocalRoot .
if ($LASTEXITCODE -ne 0) { Die "打包失败" }
$tarMB = [math]::Round((Get-Item $tar).Length/1MB, 2)
Ok "   打包完成：$tarMB MB（gzip 后）"

Say "== 2/12 上传到服务器（分片 $ChunkMB MB × N，每片独立重试 $MaxRetry 次）=="
$partSize = [int]($ChunkMB * 1MB)
if ($partSize -lt 65536) { Die "ChunkMB 太小（下限 0.0625）" }
$bytes = [IO.File]::ReadAllBytes($tar)
$nPart = [math]::Ceiling($bytes.Length / $partSize)
ssh @SshOpt "${SshUser}@${Server}" "rm -f ${AppDir}/backend_release.tar ${AppDir}/backend_release.tar.gz ${AppDir}/backend_release.tar.gz.part*" | Out-Null
if ($LASTEXITCODE -ne 0) { Die "无法连接服务器（清残留失败）—— 检查 VPN" }
for ($i = 0; $i -lt $nPart; $i++) {
    $off  = $i * $partSize
    $len  = [math]::Min($partSize, $bytes.Length - $off)
    $name = "backend_release.tar.gz.part{0:D2}" -f $i
    $local = Join-Path $WorkDir $name
    $fs = [IO.File]::Create($local); $fs.Write($bytes, $off, $len); $fs.Close()
    Scp-UpWithRetry $local "${AppDir}/$name"
    Remove-Item $local -ErrorAction SilentlyContinue
    Write-Host ("   分片 {0}/{1} 完成（{2} KB）" -f ($i+1), $nPart, [math]::Round($len/1KB,0)) -ForegroundColor DarkGray
}
Ok "   全部 $nPart 片已上传（共 $tarMB MB）"

Say "== 3/12 服务器拼接 + 解压 + 构建镜像 =="
# 解压前先清掉旧代码目录：tar 解压**不删**「仓库里已删」的文件（2026-09-20 事故：
# 老副本 + 丢 import 的新文件共存，制造出「补丁在、import 不在」的假象）。
# scripts/ 不动（线上可能有仓库里没有的运维脚本）；src/agent/workspace 是运行时数据回退目录，先移出再放回。
ssh @SshOpt "${SshUser}@${Server}" "cd ${AppDir} && cat backend_release.tar.gz.part* > backend_release.tar.gz && rm -f backend_release.tar.gz.part* && rm -rf /tmp/nl2sql_ws_keep && mkdir -p /tmp/nl2sql_ws_keep && if [ -d backend/src/agent/workspace ]; then mv backend/src/agent/workspace /tmp/nl2sql_ws_keep/ ; fi && rm -rf backend/src && tar -xzf backend_release.tar.gz -C backend && if [ -d /tmp/nl2sql_ws_keep/workspace ]; then mv /tmp/nl2sql_ws_keep/workspace backend/src/agent/workspace ; fi && rm -rf /tmp/nl2sql_ws_keep && rm -f backend_release.tar.gz && docker-compose build langgraph-api 2>&1 | tail -3"
if ($LASTEXITCODE -ne 0) { Die "构建失败" }

$newImageId = ((ssh @SshOpt "${SshUser}@${Server}" "docker image inspect -f '{{.Id}}' ${Proj}_${ASvc}") -join "").Trim()
if (-not $newImageId -or $newImageId -notlike "sha256:*") { Die "取不到新镜像 ID（拿到的是 '$newImageId'）" }
Ok "   新镜像 ID = $newImageId"
if ($curImageId -and $newImageId -eq $curImageId) { Warn "   ⚠️ 新镜像 ID 与旧镜像 ID 相同 —— 本次没有实质代码变化？" }

Say "== 4/12 上传 overlay / nginx.conf / 步骤脚本 + 就位 ops_drain =="
# overlay：把 IMAGE_ID_PLACEHOLDER 换成**实测的新镜像 ID**。用 ID 而不是 tag，才叫「同一份字节」
# （tag 会被重指向）。忘了替换的后果是 compose 找不到镜像当场炸 —— 这是**故意**的硬失败。
$overlayRepo = Join-Path $LocalRoot "docker\compose.colorB.yml"
$overlayText = [IO.File]::ReadAllText($overlayRepo)
if ($overlayText -notmatch 'IMAGE_ID_PLACEHOLDER') { Die "overlay 里没有 IMAGE_ID_PLACEHOLDER（模板被改过？）" }
$overlayOut = Join-Path $WorkDir "compose.colorB.yml"
[IO.File]::WriteAllText($overlayOut, ($overlayText -replace 'IMAGE_ID_PLACEHOLDER', $newImageId), $Utf8NoBom)
Scp-UpWithRetry $overlayOut "${AppDir}/docker/compose.colorB.yml"
Scp-UpWithRetry $stepsLocal $StepsSh
ssh @SshOpt "${SshUser}@${Server}" "chmod +x $StepsSh && bash $StepsSh cp_drain $ACt"
if ($LASTEXITCODE -ne 0) { Die "把新 ops_drain 就位到 A 失败（后面排空会用到它）" }
# 现在这一版 ops_drain 也在发版包里（backend/scripts/ops_drain.py），但旧容器里的是旧版本，
# 所以必须先 docker cp 覆盖（见上面的 cp_drain）—— 否则排空判据仍是过时的「只看 remaining」。

Say "== 5/12 起空闲第二色 B（N_JOBS_PER_WORKER=0，不领 run）=="
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh start_b 0"
if ($LASTEXITCODE -ne 0) { Die "起空闲色 B 失败 —— 线上**零影响**（B 未接流），可排查后重跑" }

Say "== 6/12 提升 B 为正式色（N_JOBS_PER_WORKER=10）=="
# 必须**重建**而不是 restart：N_JOBS_PER_WORKER 是 import 期常量，没有运行期开关。
# 先起空闲色再提升，是为了不让**未经验证的新代码**一上来就从共享队列领真实用户的 run。
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh promote_b"
if ($LASTEXITCODE -ne 0) { Die "提升 B 失败 —— 线上仍在 A 上（nginx 未切），可排查后重跑" }

Say "== 7/12 ★ 切流到 B（先起 1s 轮询，再就地覆盖 nginx.conf + nginx -t + reload）=="
# 轮询**故意先于**切流启动：这样切换那一刻本身也在采样窗口内。
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh poll_start 1800"
$switchText = $repoText -replace $maskRe, ('${1}"' + $BBackend + '";')
Write-Host "   切流目标行： set `$backend_up  `"$BBackend`";" -ForegroundColor DarkGray
$nginxNew = Join-Path $WorkDir "nginx.conf.switch"
[IO.File]::WriteAllText($nginxNew, $switchText, $Utf8NoBom)
Scp-UpWithRetry $nginxNew "${AppDir}/docker/nginx.conf.new"
$tSwitch = Get-Date
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh nginx_apply ${AppDir}/docker/nginx.conf.new && rm -f ${AppDir}/docker/nginx.conf.new"
if ($LASTEXITCODE -ne 0) { Die "切流失败（nginx 已自动回滚，流量仍在 A；B 还在跑，可重跑本步）" }
Ok "   ✅ 已切流到 B —— 从这一刻起用户请求全部落 B"

Say "== 8/12 排空旧色 A + 连续 idle 确认 + 停（**同一 ssh 会话内**，窗口压到毫秒）=="
# 这一步之前 A 已经收不到用户请求了，所以它的排空（以及排空期的 503）**没有任何人看得见**。
# 连续 3 次 idle 是对**残余竞态**的缓解：A 仍持有 10 个槽、仍可能从共享队列领到新 run。
# 这条竞态**只在这一步有害**（A 跑的是旧代码）—— 第 11 步停 B 时两色已是同一份镜像，无害。
$cutOutA = (ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh cutover $ASvc $ACt $DrainBudget") -join "`n"
Write-Host $cutOutA
$cutRcA = $LASTEXITCODE
$drainA = ([regex]::Match($cutOutA, 'DRAIN_READY=(\S+)')).Groups[1].Value

Say "== 9/12 用新镜像把常驻色 A 起回来（B 在接流，A 的冷启对用户不可见）=="
# 为什么发版要绕回 A：稳态契约是「A 常驻、B 只在窗口里存在」。留 B 常驻虽然少一次切换，
# 但下一次发版就没有空闲的第二色了（脚本第二次跑不动），且全仓运维习惯都认 A 的容器名。
# ⚠️ A 起来就用 N_JOBS_PER_WORKER=10（base 原样）⇒ 它会立刻从共享队列领 run；这是**安全的**：
#    它跑的镜像与正在接流的 B **逐字节相同**，B 已经把这份代码验证过了。
#    （与「起空闲色 B」不同：B 承载的是**未经任何验证的新代码**，所以必须先从 0 槽起。）
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh start_a_new"
if ($LASTEXITCODE -ne 0) { Die "A 起不来 —— 流量仍在 B 上，**线上不受影响**；修好后重跑本步（bash $StepsSh start_a_new）即可，别从头来" }

Say "== 10/12 ★ 切流回 A（同样是先起轮询已就绪、就地覆盖 + nginx -t + reload）=="
$switchBack = $repoText -replace $maskRe, ('${1}"' + $ABackend + '";')
$nginxBack = Join-Path $WorkDir "nginx.conf.switchback"
[IO.File]::WriteAllText($nginxBack, $switchBack, $Utf8NoBom)
Scp-UpWithRetry $nginxBack "${AppDir}/docker/nginx.conf.new"
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh nginx_apply ${AppDir}/docker/nginx.conf.new && rm -f ${AppDir}/docker/nginx.conf.new"
if ($LASTEXITCODE -ne 0) { Die "切回 A 失败（nginx 已自动回滚，流量仍在 B；A 已跑着新镜像，可重跑本步）" }
Ok "   ✅ 已切回常驻色 A（新镜像）—— 稳态契约恢复"

Say "== 11/12 排空并停临时色 B =="
# 此时 B 已收不到用户请求。B 与 A 是同一份镜像，所以就算它在切流那一瞬又领到一个 run，
# 也只是这个 run 跑在「即将被停的容器」里 —— cutover 会先排空再停，不会腰斩。
# ⚠️ 必须先给 **B** 也就位一份 ops_drain：B 是另起的一个容器，它的镜像里**不一定**有这个
#    脚本（第 4 步只给 A 就位了）。2026-10-08 实撞：B 里没有 ⇒ 排空判据根本跑不起来，
#    整段排空被静默跳过。`cutover` 现在也会在探针跑不起来时**拒绝停容器**（双保险）。
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh cp_drain $BCt"
if ($LASTEXITCODE -ne 0) { Warn "  ⚠️ 给 B 就位 ops_drain 失败 —— 排空判据跑不起来，cutover 会拒绝停容器" }
$cutOutB = (ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh cutover $BSvc $BCt $DrainBudget") -join "`n"
Write-Host $cutOutB
$cutRcB = $LASTEXITCODE
$drainB = ([regex]::Match($cutOutB, 'DRAIN_READY=(\S+)')).Groups[1].Value
$secs = [math]::Round(((Get-Date) - $tSwitch).TotalSeconds, 1)

Say "== 12/12 收尾：停轮询 + 断言 0 个非 200 =="
$pollOut = (ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh poll_stop") -join "`n"
Write-Host $pollOut
$non200 = [int](([regex]::Match($pollOut, 'POLL_NON200=(\d+)')).Groups[1].Value)
$total  = [int](([regex]::Match($pollOut, 'POLL_TOTAL=(\d+)')).Groups[1].Value)
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh state_write '$curImageId' '$newImageId' '$ASvc'"
ssh @SshOpt "${SshUser}@${Server}" "bash $StepsSh final_checks"

Write-Host ""
Say "──────────── 本次发版结论 ────────────"
Write-Host ("  首次切流→停临时色：{0}s（含 A 的重建）；全程 1s/次 /ok 采样共 {1} 个，非 200 **{2}** 个" -f $secs, $total, $non200)
if ($non200 -eq 0) { Ok "  ✅ 零中断：两次切换 + 停旧色 + 重建 A 全程没有一个非 200（旧脚本这里是 ≈29s 全量 502）" }
else { Warn "  ⚠️ 有 $non200 个非 200 —— 逐条看上面的 ts/code，并与两次切换的时刻对齐" }
if ($cutRcA -ne 0) { Warn "  ⚠️ 停 A（旧色）那步返回非 0，请核对上面的 DRAIN_READY= 值" }
if ($cutRcB -ne 0) { Warn "  ⚠️ 停 B（临时色）那步返回非 0，请核对上面的 DRAIN_READY= 值" }
Write-Host ("  排空确认：A={0}  B={1}（stale = 切流后旧色又领到 run —— 残余竞态的观测点）" -f $drainA, $drainB) -ForegroundColor Gray
if ($drainA -eq "stale") {
    Warn "  ⚠️ **第 8 步 DRAIN_READY=stale**：切流之后旧色 A 又从共享队列领到了 run —— 那次 run 跑在**旧代码**上（残余竞态，见文件头）。查 A 的日志定位是哪一条"
}
Write-Host "  旧镜像 ID（回退用）：$curImageId"
Write-Host ""
Write-Host "  回退：.\release-backend-bluegreen.ps1 -Rollback -OldImageId $curImageId" -ForegroundColor Yellow
Write-Host "  收尾：B 的独占卷可以删了（不影响服务，下次发版会重建）：" -ForegroundColor DarkGray
Write-Host "        ssh ${SshUser}@${Server} 'docker volume rm nl2sql-app_nl2sql_api_meta_b'" -ForegroundColor DarkGray
Write-Host "  ⚠️ 前端不在本次范围内：前端发版仍是 stop/rm/up，location / 有几秒 502" -ForegroundColor Yellow
