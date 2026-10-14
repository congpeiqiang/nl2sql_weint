# ============================================================
# NL2SQL 后端发布脚本（一键：打包 -> 上传 -> 重建 -> 三步重启 -> 验证）
# 运行环境：本机 PowerShell（已配置 SSH 密钥免密，无需输密码）
#
# ⚠️ 2026-10-08 起：常规后端发版请改用 **release-backend-bluegreen.ps1**（蓝绿 / 用户侧 0 个 5xx，
#    说明见 docs/prod环境/蓝绿发布_阶段2发版手册.md）。本脚本**仍然可用**，保留给这三种场景：
#      ① 带**破坏性迁移**的发版（蓝绿的 expand-contract 约束不允许两色同时看到删列/改类型）；
#      ② 就是要一个**停机窗口**（人工确认过的变更窗口）；
#      ③ 蓝绿脚本本身出问题时的退路。
#    代价不变：≈29 秒全量 502（有 run 在跑时 = 排空最长 240s + 冷启）。
#
# 用法：  .\release-backend.ps1
# 参数：  -LocalRoot 本地后端根（默认 D:\code_work_space\llm\nl2sql）
#         -Server / -SshUser / -AppDir 服务器信息
#
# 2026-10-05（档 1：发版不再依赖 nginx 的「IP 复用」）：
#   ① 重启前先走**协作式排空**（容器内 `scripts/ops_drain.py`，自己签 token，无需口令）：
#      在跑的 run 跑完、新提交拿到 503+Retry-After 而不是被腰斩的 502 ⇒ 把「长时间 502」
#      换成长时间 503，**硬中断只剩冷启**。best-effort：脚本不存在/端点不可达/预算用尽
#      都只告警，绝不拦住发版（`stop` 时的信号式排空仍会兜底）。
#   ② 验证改成**经 nginx 打 /ok**（http://127.0.0.1:8080/ok）而非直连 2026 再盲等 75s：
#      既确认冷启完成，又确认 nginx 已解析到新容器 —— 这是 `docker/nginx.conf` 改成
#      「resolver + 变量 proxy_pass」（请求期解析）之后的端到端验收。同时**量出**真实窗口。
#   ③ 本脚本**永远不碰 nginx 容器**：换容器后的新 IP 由 resolver 自己发现，不需要 reload。
# ============================================================
param(
    [string]$LocalRoot = "D:\code_work_space\llm\nl2sql",
    [string]$WorkDir   = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app",
    [string]$Server    = "192.168.25.64",
    [string]$SshUser   = "weint",
    [string]$AppDir    = "/home/weint/apps/nl2sql/nl2sql-app",
    # 分片大小(MB) / 每片重试次数。VPN 链路不稳时把 ChunkMB 调小（连接寿命短
    # 于单片传输时间就必断），链路好时调大减少连接次数。2026-09-18 加：经 VPN
    # 直传 34.9MB 单连接在 ~480KB 处被 reset（28KB/s），无法靠重试整包救回。
    [double]$ChunkMB  = 0.5,
    [int]$MaxRetry    = 8
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$tar = Join-Path $WorkDir "backend_release.tar.gz"
if (Test-Path $tar) { Remove-Item $tar }

Write-Host "== 1/5 本地打包（gzip + 排除大目录/环境变量/探针）==" -ForegroundColor Cyan
# -czf 而非 -cf：34.9MB → 9.2MB（3.8x），链路越差这个杠杆越大。服务器侧解压
# 用 tar -xzf（见 3/5）。19s 本机 CPU 换 3.8 倍传输量，稳赚。
tar -czf $tar `
  --exclude=.venv --exclude=.git --exclude=logs --exclude=.langgraph_api `
  --exclude=.idea --exclude=docs --exclude=.tmp --exclude=__pycache__ `
  --exclude=docker --exclude="*.bin" --exclude="*.log" `
  --exclude=.env --exclude=.env.prod --exclude=.env.dev `
  --exclude=src/agent/workspace-temp `
  --exclude=src/agent/shared/model_config.json `
  --exclude=benchmarks `
  -C $LocalRoot .
# ↑ benchmarks：BIRD Mini-Dev 基准数据（4.1GB，未跟踪、本机评测用，2026-10-14 起）。
#   tar 按文件系统打包不看 git ⇒ 不排它整包进：gzip 后 1522MB、3046 个 0.5MB 分片、
#   上传 1 小时+，且把与运行无关的 4GB 铺进生产 backend/。当天蓝绿发版实测撞到后停跑补排。
# ↑ workspace / workspace-temp：运行时残留，容器读 AGENT_DATA_ROOT，不读这两份。
#   2026-09-25：两个目录都已从仓库删除 ⇒ 这两条排除项是空转兜底（留着的理由是
#   删了要在两台机器上重验 tar 行为，不值当）。
# ↑ workspaces.json 的排除项**已删除**（2026-09-25）：工作区注册表机制不复存在
#   （路径钉死为 AGENT_DATA_ROOT/workspace）⇒ 没有注册表可被覆盖，09-08「dev 机
#   条目覆盖生产」那条事故路径已经不存在了。
# ↑ model_config.json 同理：gitignore 的运行时模型配置，容器读 AGENT_DATA_ROOT
#   （/app/data/shared）那份；本机 src 里这份是 AGENT_DATA_ROOT 外置前的旧种子，
#   带上生产只会铺到 /app/src/agent/shared/ 下当隐患（AGENT_DATA_ROOT 一失效就
#   被静默读走）。2026-09-21 加。
# ↑ .env.dev **2026-10-01 加**：它是**被 git 跟踪**的文件且含**明文真值密钥**，而
#   prod 模式根本不读它（[[deploy-env-flag]]：prod 由 compose 注入，env_loader 叠
#   .env.prod）。此前每次整包发版都把它带进 `backend/.env.dev` ⇒ Dockerfile 的
#   `COPY . .` 让它落进镜像 `/app/.env.dev`（2026-09-30 实测 6704B、时间戳随发版变）。
#   ⚠️ 只挡未来的包：服务器 `backend/.env.dev` 与**已构建镜像里的**旧副本仍在，
#   要清得单独动手（那属于生产操作，需授权）。
if ($LASTEXITCODE -ne 0) { throw "打包失败" }
$tarMB = [math]::Round((Get-Item $tar).Length/1MB,2)
Write-Host "   打包完成：$tarMB MB（gzip 后）"

# SSH/SCP 公共选项：keepalive 防止链路空闲被判死；ConnectTimeout 避免卡在握手。
$SshOpt = @("-o","ServerAliveInterval=20","-o","ServerAliveCountMax=6","-o","ConnectTimeout=15")

Write-Host "== 2/5 上传到服务器（分片 $ChunkMB MB × N，每片独立重试 $MaxRetry 次）==" -ForegroundColor Cyan
$partSize = [int]($ChunkMB * 1MB)
if ($partSize -lt 65536) { throw "ChunkMB 太小（下限 0.0625）" }
$bytes  = [IO.File]::ReadAllBytes($tar)
$nPart  = [math]::Ceiling($bytes.Length / $partSize)
# 清掉上次残留（含旧版脚本可能留在服务器上的整包 tar）
ssh @SshOpt "${SshUser}@${Server}" "rm -f ${AppDir}/backend_release.tar ${AppDir}/backend_release.tar.gz ${AppDir}/backend_release.tar.gz.part*" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "无法连接服务器（清残留失败）——检查 VPN" }

for ($i = 0; $i -lt $nPart; $i++) {
    $off  = $i * $partSize
    $len  = [math]::Min($partSize, $bytes.Length - $off)
    # 两位补零：cat part* 的字典序 == 数字序，最多 99 片（约 50MB @0.5MB）
    $name = "backend_release.tar.gz.part{0:D2}" -f $i
    $local = Join-Path $WorkDir $name
    $fs = [IO.File]::Create($local); $fs.Write($bytes, $off, $len); $fs.Close()
    $ok = $false
    for ($try = 1; $try -le $MaxRetry; $try++) {
        scp @SshOpt $local "${SshUser}@${Server}:${AppDir}/$name"
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        Write-Host ("   分片 {0}/{1} 第 {2}/{3} 次被重置，3s 后重试…" -f ($i+1), $nPart, $try, $MaxRetry) -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
    Remove-Item $local -ErrorAction SilentlyContinue
    if (-not $ok) { throw "分片 $name 上传失败（已重试 $MaxRetry 次）——把 -ChunkMB 调更小再试" }
    Write-Host ("   分片 {0}/{1} 完成（{2} KB）" -f ($i+1), $nPart, [math]::Round($len/1KB,0)) -ForegroundColor DarkGray
}
Write-Host "   全部 $nPart 片已上传（共 $tarMB MB）" -ForegroundColor Green

Write-Host "== 3/5 服务器拼接 + 解压 + 重建镜像 ==" -ForegroundColor Cyan
# 解压前先清掉服务器上的旧代码目录：tar 解压**不会删除**「仓库里已删/已改名」的文件，
# 于是被删的代码会一直躺在 backend/ 里被烤进镜像。2026-09-20 事故正是被这点放大：
# 一个 09-02 的老副本 + 09-18 丢 import 的 main_agent.py 共存，制造出「补丁文件在、
# import 不在」的假象，白排查一轮（也说明只清 src/ 就够——prompt 在 src/agent/prompt 下）。
# 两点边界：
#   - scripts/ 不动：服务器上可能有仓库里没有的运维脚本（如 cron 调用的），删了不会随包回来。
#   - src/agent/workspace 是 **AGENT_DATA_ROOT 未配置时**的运行时数据回退目录（默认工作区 +
#     db_config.json + feedback SQLite），tar 本来就排除它，故先移出、解压后放回：无论生产
#     是否配了 AGENT_DATA_ROOT 都不会丢数据。
# 拼接完整性闸（2026-10-14 事故后加，与 release-backend-bluegreen.ps1 同款）：上一轮
# 发版进程被杀时其在途 scp 会把残片在本轮清理**之后**落上服务器 ⇒ cat part* 拼出
# 「合法 gzip 流 + 尾部垃圾」，tar rc=2 且炸点在 `rm -rf backend/src` 之后。
# md5 对账放在一切删除动作之前 ⇒ 包不干净就地停住，既有树不动。
$localMd5 = (Get-FileHash -Algorithm MD5 -Path $tar).Hash.ToLower()
ssh @SshOpt "${SshUser}@${Server}" "cd ${AppDir} && cat backend_release.tar.gz.part* > backend_release.tar.gz && rm -f backend_release.tar.gz.part* && echo '${localMd5}  backend_release.tar.gz' | md5sum -c - && rm -rf /tmp/nl2sql_ws_keep && mkdir -p /tmp/nl2sql_ws_keep && if [ -d backend/src/agent/workspace ]; then mv backend/src/agent/workspace /tmp/nl2sql_ws_keep/ ; fi && rm -rf backend/src && tar -xzf backend_release.tar.gz -C backend && if [ -d /tmp/nl2sql_ws_keep/workspace ]; then mv /tmp/nl2sql_ws_keep/workspace backend/src/agent/workspace ; fi && rm -rf /tmp/nl2sql_ws_keep && rm -f backend_release.tar.gz && docker-compose build langgraph-api 2>&1 | tail -3"
if ($LASTEXITCODE -ne 0) { throw "构建失败（含 md5 对账不过 = 服务器上有残片/传输损坏：清 backend_release.tar.gz* 后重跑）" }

Write-Host "== 4/5 先协作式排空，再三步重启（v1 compose 需 stop/rm/up）==" -ForegroundColor Cyan
# 排空必须在 stop 之前：这时进程还在正常接连接，新提交能被**明确拒绝**（503 + Retry-After
# + 中文提示），在跑的 run 也能收尾。反过来若不排空，`stop` 会先关监听 ⇒ 新请求直接
# connection refused（502），然后才用信号式排空等老 run —— 用户看到的是断服而不是「稍后重试」。
# ⚠️ 不判退出码：ops_drain 的 2（预算用尽仍有 run）与 1（端点不可达）都不该中断发版。
$ct = "nl2sql-app_langgraph-api_1"
ssh @SshOpt "${SshUser}@${Server}" "docker exec $ct sh -lc 'test -f /app/scripts/ops_drain.py' 2>/dev/null && docker exec $ct python /app/scripts/ops_drain.py --budget 180 || echo 'SKIP ops_drain（旧镜像或不可达）—— 依赖 stop 时的信号式排空'"

# ⚠️ 铁律：这里的 `up -d` 必须带 `--no-deps`（2026-10-05 事故）。compose v1 的
# `up -d <svc>` 会把 `depends_on` 的依赖**一起纳入收敛计划**；postgres 容器里的
# config-hash 与当前 compose 文件不一致（历史遗留，早于本次改动）⇒ compose 判定要
# recreate 它 ⇒ 撞上 compose v1 + docker 29 的 `KeyError: 'ContainerConfig'`
# （`compose/service.py:1579 get_container_data_volumes`，重建路径上镜像 inspect 没有
# 该键）⇒ 当场把 postgres 打死、平台存储停摆 55 分钟。`--no-deps` 让本步永不触碰依赖。
# 依赖（postgres/redis）带 `restart: unless-stopped`，正常发版时必在跑；若真被停掉，
# 下面的 5/5 健康轮询会立刻失败，而不是被这里静默带过。
$tDown = Get-Date
ssh @SshOpt "${SshUser}@${Server}" "cd ${AppDir} && docker-compose stop langgraph-api && docker-compose rm -f langgraph-api && docker-compose up -d --no-deps langgraph-api"

Write-Host "== 5/5 健康轮询（经 nginx 8080）==" -ForegroundColor Cyan
# 单次 ssh 里跑轮询循环（别再每 2 秒开一次 ssh）。探的是**经 nginx** 的 /ok：
# 既证明后端冷启完成，也证明 nginx 已经解析到**新容器**（上游已是请求期解析）。
# 写法沿用本文件既有约定：PS 双引号串里 bash 的 `$` 用**反引号**转义；别用 `\"`（那个会
# 提前闭合 PS 字符串 —— 2026-09-26 那条教训就是它）。`%{http_code}` 无引号也安全。
$poll = "for i in `$(seq 1 60); do c=`$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:8080/ok); case `$c in 200) echo nginx_ok=200; exit 0;; esac; sleep 2; done; echo nginx_ok=timeout; exit 1"
ssh @SshOpt "${SshUser}@${Server}" $poll
$pollRc = $LASTEXITCODE
$secs = [math]::Round(((Get-Date) - $tDown).TotalSeconds, 1)
Write-Host "   不可用窗口 ≈ $secs s（含排空 + 冷启 —— 这就是用户感知的断服时长）" -ForegroundColor Yellow
ssh @SshOpt "${SshUser}@${Server}" "docker logs nl2sql-app_langgraph-api_1 2>&1 | grep -E 'MCP 工具加载完成|预检通过|预检失败' | tail -4"
if ($pollRc -ne 0) { throw "经 nginx 的 /ok 在 120s 内未恢复 200 —— 查 nginx error_log（上游解析？）与后端预检日志" }

Write-Host ""
Write-Host "OK 后端发布完成。期望：backend_ok:200 + 预检通过: N 个 MCP 工具就绪（echarts=18, wrenai_*=27±, dbmcp=2 逐项 breakdown）" -ForegroundColor Green
Write-Host "⚠ 若见「预检失败」或 backend_ok 非 200：关键 MCP server（wrenai_*/dbmcp）加载失败会拒绝启动，按日志排障提示处理" -ForegroundColor Yellow