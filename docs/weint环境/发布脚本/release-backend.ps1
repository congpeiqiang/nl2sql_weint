# ============================================================
# NL2SQL 后端发布脚本（一键：打包 -> 上传 -> 打回滚tag -> 重建 -> 同步运行期 shared
#                          -> 排空后重启 -> 验证）
# 运行环境：本机 PowerShell（已配置 SSH 密钥免密，无需输密码）
# 用法：  .\release-backend.ps1
# 参数：  -LocalRoot 本地后端根（默认 D:\code_work_space\llm\nl2sql）
#         -Server / -SshUser / -AppDir 服务器信息
#         -DrainTimeout 排空最多等多少秒（默认 240，0 = 不排空、按旧行为硬停）
#         -SkipSharedSync 跳过第 5 步（同步运行期 shared/skills + shared/memory）；应急/排查用
# 回滚：  见同目录 rollback-backend.ps1
#
# P2-1 优雅停机：停容器前先排空（在跑的 run 跑完，新提交拿到明确 503），
# 排空预算由后端 `NL2SQL_DRAIN_SECS`（默认 180）控制，`stop -t` 必须大于它。
# ============================================================
param(
    [string]$LocalRoot = "D:\code_work_space\llm\nl2sql",
    [string]$WorkDir   = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app",
    [string]$Server    = "192.168.25.64",
    [string]$SshUser   = "weint",
    [string]$AppDir    = "/home/weint/apps/nl2sql/nl2sql-app",
    [int]$DrainTimeout = 240,
    [switch]$SkipSharedSync
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$tar = Join-Path $WorkDir "backend_release.tar"
if (Test-Path $tar) { Remove-Item $tar }

Write-Host "== 1/7 本地打包（排除大目录/环境变量/探针）==" -ForegroundColor Cyan
# 注：src/agent/shared/model_config.json 必须排除 —— 2026-09-25 已把它连同其它运行期残留
# （traces.sqlite* / checkpoint/ / trace/ / feedback/）从仓库删掉；真身在
# AGENT_DATA_ROOT/shared/，发版不回退。这条 exclude 保留当兜底：本地跑"未配
# AGENT_DATA_ROOT"的脚本会走回退分支，把模型配置（含 api_key）再生成到仓库里。
# 同理 `--exclude=auth_secret`。
# ⚠️ `--exclude=src/agent/workspace` **匹配不到 `src/agent/workspace-temp/`**：tar 的排除
# 模式匹配到斜杠边界，`workspace-temp` ≠ `workspace`（本机 GNU tar 与 Windows bsdtar 行为一致，
# 2026-09-24 实测）。不补这一条就会把 ~32MB 的本地临时工作区（含语义库草稿 + 嵌套 .git）
# 打进每次发行包；代码里对 `workspace-temp` 的引用为 0 ⇒ 排除它就是原意，不是策略变更。
# 2026-09-25：`workspace-temp` 已从仓库删除 ⇒ 这两条排除项现在都是空转，刻意留着
# （删掉要在两台机器上重新验 tar 行为，不值当）。同时删掉了 `workspaces.json` 的排除项
# —— 工作区注册表机制已不存在（路径钉死 AGENT_DATA_ROOT/workspace）。
# ⚠️ 已知（**未处理，待定**）：`.env.dev`（含 dev 服务器 DB 口令）与 `.claude/` 未被排除，
# 会一起铺进镜像（落点是 /app 下，不影响生产读取：后端按 DEPLOY_ENV 只读 .env.prod）。
# 要不要一并排除属安全策略决定，未擅自改。
tar -cf $tar `
  --exclude=.venv --exclude=.git --exclude=logs --exclude=.langgraph_api `
  --exclude=.idea --exclude=docs --exclude=.tmp --exclude=__pycache__ `
  --exclude=docker --exclude="*.bin" --exclude="*.log" `
  --exclude=.env --exclude=.env.prod --exclude=src/agent/workspace `
  --exclude=src/agent/workspace-temp `
  --exclude=src/agent/shared/model_config.json `
  --exclude=auth_secret `
  -C $LocalRoot .
if ($LASTEXITCODE -ne 0) { throw "打包失败" }
Write-Host "   打包完成：$(([math]::Round((Get-Item $tar).Length/1MB,1))) MB"

Write-Host "== 2/7 上传到服务器 ==" -ForegroundColor Cyan
scp $tar "${SshUser}@${Server}:${AppDir}/"
if ($LASTEXITCODE -ne 0) { throw "上传失败" }

# 必须在 docker-compose build **之前**打 tag：build 会生成新镜像，
# 旧镜像只剩这个 tag 还引用着，否则就变成 dangling 无法回滚。
Write-Host "== 3/7 打回滚 tag（重建前，保住当前镜像）==" -ForegroundColor Cyan
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
Write-Host "   tag 名：nl2sql-api:rollback-${stamp}"
ssh "${SshUser}@${Server}" "cd ${AppDir} && docker tag `$(docker inspect -f '{{.Image}}' nl2sql-app_langgraph-api_1) nl2sql-api:rollback-${stamp} && echo '   已打 tag' && docker images nl2sql-api --format '   {{.Repository}}:{{.Tag}}  {{.CreatedSince}}' | head -6"
if ($LASTEXITCODE -ne 0) {
    Write-Host "   WARN 打回滚 tag 失败（容器未运行？首次部署属正常）—— 继续发布" -ForegroundColor Yellow
}

Write-Host "== 4/7 服务器解压 + 重建镜像 ==" -ForegroundColor Cyan
# ⚠️ 必须**先清空 backend/ 再解压**：`tar -xf` 只覆盖不删除，而镜像构建上下文就是这个目录
#    ⇒ 仓库里删掉的文件会在镜像里「复活」（例：T3 删的 `src/api/workspace.py`、改名前的旧
#    skill 目录），`check_skills_drift` 还会把它们当成合法种子。上一棵树留成
#    `backend_release.tar.prev` 当回滚备份；构建失败可直接重解压它（见 throw 提示）。
ssh "${SshUser}@${Server}" "cd ${AppDir} && rm -f backend_release.tar.prev && mv backend_release.tar backend_release.tar.prev && rm -rf backend && mkdir -p backend && tar -xf backend_release.tar.prev -C backend && docker-compose build langgraph-api 2>&1 | tail -3"
if ($LASTEXITCODE -ne 0) { throw "构建失败（上一棵树在 ${AppDir}/backend_release.tar.prev：cd ${AppDir} && rm -rf backend && mkdir backend && tar -xf backend_release.tar.prev -C backend）" }

Write-Host "== 5/7 同步运行期 shared（skills + memory）到本版 ==" -ForegroundColor Cyan
# 为什么需要这一步：运行期真正生效的是 <AGENT_DATA_ROOT>/shared/{skills,memory}，它只在**目录
#   缺失**时从镜像播种（workspace_manager._seed_data_root_once，`if target.is_dir(): continue`）
#   ⇒ 光发版永远不刷新它，此前只能靠人手工 cp。症状：改了 skill/记忆，发版后线上毫无变化
#   （第 7 步的 drift 体检从此成了这一步的验收：exit 0 才是正常）。
# 安全前提（2026-09-25 核实）：agent 的写权限只有 workspace/{report,tmp,nl2sql_process_data}
#   ⇒ 这两个子树没有运行期写入者，可以整目录**替换**。
# ⛔ 只碰 skills 与 memory 两个子目录：shared/ 下还有 checkpoint / trace / feedback /
#    model_config.json，整树镜像会把运行期数据删掉。
# ⚠️ 必须是替换而非叠加：技能改名/删除时 `cp -a` 只加不删，会把新旧两套都留在线上，
#    模型可能照旧 SOP 走（本次 `nl2sql-*` → `wren-*` 正是这种情况）。
# ⚠️ 位置必须在重启（第 6 步）**之前**：此刻容器还跑着**旧镜像**，容器内 /app/src 里是旧种子，
#    所以种子只能取自宿主机刚解压的 ${AppDir}/backend/src；重启后 memory 才被 import 期读入
#    （main_agent.py:254 的 create_deep_agent 在模块级，memory=[ORCHESTRATOR.md] 随之定型）。
if ($SkipSharedSync) {
    Write-Host "   已跳过（-SkipSharedSync）" -ForegroundColor Yellow
} else {
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    # 探"容器存在"而不是"在运行"：`docker cp` 对已停容器也可用，而 /app/data/shared 在**上次启动**时
    # 就已播种 ⇒ 容器只是停着时同样需要同步。容器压根不存在 = 首部署/全新卷 ⇒ 首次启动会按新镜像播种，跳过。
    ssh "${SshUser}@${Server}" "cd ${AppDir}/backend/src/agent/shared && if ! docker inspect nl2sql-app_langgraph-api_1 >/dev/null 2>&1; then echo '   [shared-sync] 容器不存在（首部署），跳过 —— 首次启动会按新镜像自动播种'; else docker cp skills nl2sql-app_langgraph-api_1:/app/data/shared/skills.new && docker cp memory nl2sql-app_langgraph-api_1:/app/data/shared/memory.new && docker exec nl2sql-app_langgraph-api_1 bash -c 'set -e; cd /app/data/shared; tar -czf /app/data/shared.bak-${ts}.tgz skills memory; rm -rf skills.old memory.old; mv skills skills.old; mv memory memory.old; mv skills.new skills; mv memory.new memory; rm -rf skills.old memory.old; echo `"   [shared-sync] SKILL.md=`$(find skills -name SKILL.md | wc -l) 个, memory=`$(find memory -mindepth 1 -maxdepth 1 | wc -l) 个, 备份 /app/data/shared.bak-${ts}.tgz`"'; fi"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   ERR 替换失败：备份 tar 在 /app/data/shared.bak-${ts}.tgz；/app/data/shared 下留有中间态" -ForegroundColor Red
        Write-Host "        看有哪些目录再选一条（exec 要求容器在运行，容器停着就先 up -d）：" -ForegroundColor Red
        Write-Host "        · 有 skills.old ⇒ 回退：docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app/data/shared && rm -rf skills memory && mv skills.old skills && mv memory.old memory'" -ForegroundColor Red
        Write-Host "        · 只有 skills.new ⇒ 前移：docker exec nl2sql-app_langgraph-api_1 bash -c 'cd /app/data/shared && mv skills.new skills && mv memory.new memory'" -ForegroundColor Red
        throw "运行期 shared 同步失败（尚未重启，线上仍是旧容器）"
    }
}

Write-Host "== 6/7 优雅停机 + 三步重启（v1 compose 需 stop/rm/up）==" -ForegroundColor Cyan
# ① 协作式排空（容器内脚本，不需要任何口令）：新提交立刻拿到 503 + 明确提示，
#    并把"还剩几个后台任务"打出来。失败/超时都不阻断发版（② 仍会兜住）。
if ($DrainTimeout -gt 0) {
    $drainBudget = $DrainTimeout - 30   # 留 30s 余量给 flush + langgraph 自身收尾
    # ⚠️ 必须先在容器里探一下脚本在不在：`/app/src` 与 `scripts/` 都是**烧进镜像**的，
    #    而这一步跑在 ② 重建容器**之前** —— 首个带排空的版本里容器还是旧镜像、没有
    #    ops_drain.py，`python <missing>` 会以 **exit=2** 退出（"can't open file"），
    #    正好和"预算用尽仍有任务"同码 → 会被下面误报成「排空超时：仍有 run 在跑」。
    #    探不到就跳过（echo 返回 0），让 ② 的信号式排空兜底，且不误导操作者。
    ssh "${SshUser}@${Server}" "cd ${AppDir} && if docker exec nl2sql-app_langgraph-api_1 test -f /app/scripts/ops_drain.py; then docker exec nl2sql-app_langgraph-api_1 python /app/scripts/ops_drain.py --budget ${drainBudget}; else echo '   [drain] 跳过协作式排空：当前容器镜像里没有 ops_drain.py（首次带 P2-1 的发版属正常），由 ② 信号式排空兜底'; fi"
    if ($LASTEXITCODE -eq 2) {
        Write-Host "   WARN 排空超时：仍有 run 在跑。继续发版会截断它们（详见上一条日志）；" -ForegroundColor Yellow
        Write-Host "        想再等：重跑本脚本；想放弃：docker exec ... ops_drain.py --undrain" -ForegroundColor Yellow
    } elseif ($LASTEXITCODE -ne 0) {
        Write-Host "   WARN 排空未生效（见上）：将依赖 ② 的信号式排空兜底" -ForegroundColor Yellow
    }
}
# ② 信号式排空（主路径，也覆盖"有人直接 restart"的情况）：`stop -t` 必须 > 服务端预算
#    NL2SQL_DRAIN_SECS(默认 180)，否则 docker 的 SIGKILL 会比排空先到、等待白做。
#    触发链：SIGTERM → uvicorn 停机 → custom_app lifespan 排空 → 之后才是 langgraph 的 5s 窗口。
ssh "${SshUser}@${Server}" "cd ${AppDir} && docker-compose stop -t ${DrainTimeout} langgraph-api && docker-compose rm -f langgraph-api && docker-compose up -d langgraph-api"

Write-Host "== 7/7 等待启动并验证 ==" -ForegroundColor Cyan
Start-Sleep -Seconds 75
ssh "${SshUser}@${Server}" "curl -s -o /dev/null -w 'backend_ok:%{http_code}' --max-time 6 http://127.0.0.1:2026/ok; echo; docker logs nl2sql-app_langgraph-api_1 2>&1 | grep -E 'MCP 工具加载完成|预检通过' | tail -2"

# 运行期 skills 读的是外置 `<AGENT_DATA_ROOT>/shared/skills`，而它只在**目录缺失时**播种
# → 改了仓库里的 skill、镜像更新了，线上却还用旧的那份（**静默不生效**）。
# 第 5 步已做整目录替换，所以这里 exit 0 是**预期**；exit 3 = 那一步没生效（被 -SkipSharedSync
# 跳过 / 容器当时没在跑 / 有人事后手工改了运行期那份）—— 这时才需要人工介入。
ssh "${SshUser}@${Server}" "cd ${AppDir} && docker exec nl2sql-app_langgraph-api_1 python /app/scripts/check_skills_drift.py"
if ($LASTEXITCODE -eq 3) {
    Write-Host "   WARN 外置 skills 与镜像内种子仍漂移（见上清单）：技能类改动此时不生效。" -ForegroundColor Yellow
    Write-Host "        多数情况是第 5 步被跳过或当时容器不在跑；先看 [shared-sync] 那行有没有输出。" -ForegroundColor Yellow
    Write-Host "        手工同步（整目录替换，不要用 cp -a 叠加）：见部署手册 §四" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "OK 后端发布完成。期望：backend_ok:200 + 预检通过: N 个 MCP 工具就绪" -ForegroundColor Green
Write-Host "   回滚：.\rollback-backend.ps1 -Tag rollback-${stamp}" -ForegroundColor Green
