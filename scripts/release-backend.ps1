# ============================================================
# NL2SQL 后端发布脚本（一键：打包 -> 上传 -> 重建 -> 三步重启 -> 验证）
# 运行环境：本机 PowerShell（已配置 SSH 密钥免密，无需输密码）
# 用法：  .\release-backend.ps1
# 参数：  -LocalRoot 本地后端根（默认 D:\code_work_space\llm\nl2sql）
#         -Server / -SshUser / -AppDir 服务器信息
# ============================================================
param(
    [string]$LocalRoot = "D:\code_work_space\llm\nl2sql",
    [string]$WorkDir   = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app",
    [string]$Server    = "192.168.25.64",
    [string]$SshUser   = "weint",
    [string]$AppDir    = "/home/weint/apps/nl2sql/nl2sql-app"
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$tar = Join-Path $WorkDir "backend_release.tar"
if (Test-Path $tar) { Remove-Item $tar }

Write-Host "== 1/5 本地打包（排除大目录/环境变量/探针）==" -ForegroundColor Cyan
tar -cf $tar `
  --exclude=.venv --exclude=.git --exclude=logs --exclude=.langgraph_api `
  --exclude=.idea --exclude=docs --exclude=.tmp --exclude=__pycache__ `
  --exclude=docker --exclude="*.bin" --exclude="*.log" `
  --exclude=.env --exclude=.env.prod --exclude=src/agent/workspace `
  --exclude=src/agent/workspace_manager/workspaces.json `
  -C $LocalRoot .
# ↑ workspaces.json 是运行时注册表（dev 机条目），随 tar 上生产会覆盖服务器
#   注册表（2026-09-08 ee/cpq 工作区消失事故）；治本后注册表住数据卷，此排除
#   为双保险（服务器 backend/ 里的旧残留仍会进镜像，但新代码不再读它）。
if ($LASTEXITCODE -ne 0) { throw "打包失败" }
Write-Host "   打包完成：$(([math]::Round((Get-Item $tar).Length/1MB,1))) MB"

Write-Host "== 2/5 上传到服务器 ==" -ForegroundColor Cyan
scp $tar "${SshUser}@${Server}:${AppDir}/"
if ($LASTEXITCODE -ne 0) { throw "上传失败" }

Write-Host "== 3/5 服务器解压 + 重建镜像 ==" -ForegroundColor Cyan
ssh "${SshUser}@${Server}" "cd ${AppDir} && tar -xf backend_release.tar -C backend && rm backend_release.tar && docker-compose build langgraph-api 2>&1 | tail -3"
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

Write-Host "== 4/5 三步重启（v1 compose 需 stop/rm/up）==" -ForegroundColor Cyan
ssh "${SshUser}@${Server}" "cd ${AppDir} && docker-compose stop langgraph-api && docker-compose rm -f langgraph-api && docker-compose up -d langgraph-api"

Write-Host "== 5/5 等待启动并验证 ==" -ForegroundColor Cyan
Start-Sleep -Seconds 75
ssh "${SshUser}@${Server}" "curl -s -o /dev/null -w 'backend_ok:%{http_code}' --max-time 6 http://127.0.0.1:2026/ok; echo; docker logs nl2sql-app_langgraph-api_1 2>&1 | grep -E 'MCP 工具加载完成|预检通过|预检失败' | tail -4"

Write-Host ""
Write-Host "OK 后端发布完成。期望：backend_ok:200 + 预检通过: N 个 MCP 工具就绪（echarts=18, wrenai_*=27±, dbmcp=2 逐项 breakdown）" -ForegroundColor Green
Write-Host "⚠ 若见「预检失败」或 backend_ok 非 200：关键 MCP server（wrenai_*/dbmcp）加载失败会拒绝启动，按日志排障提示处理" -ForegroundColor Yellow