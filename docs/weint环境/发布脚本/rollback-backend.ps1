# ============================================================
# NL2SQL 后端回滚脚本（配对 release-backend.ps1 打的 rollback tag）
#
# 用法：  .\rollback-backend.ps1                                # 只列出可回滚的 tag
#         .\rollback-backend.ps1 -Tag rollback-20260923-1530
#
# 原理：  compose v1 的 up 会用「服务对应的项目镜像名」（nl2sql-app_langgraph-api）。
#         把目标 tag 重新 tag 成这个名字，再 up --no-build，即回到旧版本。
#
# 注意：  ① 从**含排空补丁的版本**回滚：在跑的 run 会被排空等完（不丢）；
#         ② 从**旧版本**回滚（旧镜像没有 drain）：仍会中断在跑 run，但线程列表在卷
#            nl2sql_api_meta 里不会丢，只是那一轮 run 断在半路。
# ============================================================
param(
    [string]$Tag     = "",
    [string]$Server  = "192.168.25.64",
    [string]$SshUser = "weint",
    [string]$AppDir  = "/home/weint/apps/nl2sql/nl2sql-app"
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

if (-not $Tag) {
    Write-Host "== 可回滚的镜像 tag ==" -ForegroundColor Cyan
    ssh "${SshUser}@${Server}" "docker images nl2sql-api --format '{{.Repository}}:{{.Tag}}  {{.CreatedSince}}  {{.Size}}'"
    Write-Host ""
    Write-Host "用法：.\rollback-backend.ps1 -Tag rollback-YYYYMMDD-HHMM" -ForegroundColor Yellow
    exit 0
}

Write-Host "== 1/3 确认目标镜像存在 ==" -ForegroundColor Cyan
ssh "${SshUser}@${Server}" "docker image inspect nl2sql-api:${Tag} -f ok >/dev/null"
if ($LASTEXITCODE -ne 0) { throw "镜像 nl2sql-api:${Tag} 不存在" }

Write-Host "== 2/3 回滚（先排空，再切回旧镜像）==" -ForegroundColor Cyan
# P2-1：与 release 同款两步 —— ① 容器内主动排空（让在跑的 run 跑完、新提交拿明确 503），
# ② `stop -t 240` 触发信号式排空。旧镜像（本补丁之前）没有 drain 代码：①会失败、
# 日志里出现 WARN，属预期，②仍按 -t 240 等它自己的收尾。
ssh "${SshUser}@${Server}" "cd ${AppDir} && docker exec nl2sql-app_langgraph-api_1 python /app/scripts/ops_drain.py --budget 210 || echo '   （旧镜像无排空端点，按信号式等待）'"
ssh "${SshUser}@${Server}" "cd ${AppDir} && docker-compose stop -t 240 langgraph-api && docker-compose rm -f langgraph-api && docker tag nl2sql-api:${Tag} nl2sql-app_langgraph-api && docker-compose up -d --no-build langgraph-api"
if ($LASTEXITCODE -ne 0) { throw "回滚失败" }

Write-Host "== 3/3 等待启动并验证 ==" -ForegroundColor Cyan
Start-Sleep -Seconds 75
ssh "${SshUser}@${Server}" "curl -s -o /dev/null -w 'backend_ok:%{http_code}' --max-time 6 http://127.0.0.1:2026/ok; echo"

Write-Host ""
Write-Host "OK 已回滚到 nl2sql-api:${Tag}（期望 backend_ok:200）" -ForegroundColor Green
