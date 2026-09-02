# ============================================================
# NL2SQL 前端发布脚本（一键：本地构建 -> 打包产物 -> 上传 ->
#                         服务器重建 -> 三步重启 -> 验证）
# 运行环境：本机 PowerShell（已配置 SSH 密钥免密；yarn 可用）
# 注意：前端源码含 DLP 加密文件，构建必须在本地环境执行（默认自动 yarn build，
#       若因 DLP 失败，请手动构建后加 -SkipBuild 重跑）
# 用法：  .\release-frontend.ps1
# 参数：  -SkipBuild 跳过本地构建（产物 .next 须已是最新）
# ============================================================
param(
    [string]$LocalRoot   = "D:\code_work_space\llm\huice\008\harness-deep-agents-ui",
    [string]$WorkDir     = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app",
    [string]$PlainConfig = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\next.config.ts",
    [string]$Server      = "192.168.25.64",
    [string]$SshUser     = "weint",
    [string]$AppDir      = "/home/weint/apps/nl2sql/nl2sql-app",
    [switch]$SkipBuild
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

Write-Host "== 1/6 本地生产构建（DLP 解密需本机）==" -ForegroundColor Cyan
if (-not $SkipBuild) {
    Push-Location $LocalRoot
    yarn build
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "yarn build 失败（若为 DLP 加密问题请手动构建后加 -SkipBuild）" }
    Pop-Location
} else {
    Write-Host "   已跳过构建（使用现有 .next）"
}

Write-Host "== 2/6 打包构建产物（不含 node_modules / next.config.ts）==" -ForegroundColor Cyan
$tar = Join-Path $WorkDir "frontend_release.tar"
if (Test-Path $tar) { Remove-Item $tar }
tar -cf $tar --exclude=.next/cache -C $LocalRoot .next public package.json yarn.lock
if ($LASTEXITCODE -ne 0) { throw "打包失败" }
Write-Host "   打包完成：$(([math]::Round((Get-Item $tar).Length/1MB,1))) MB"

Write-Host "== 3/6 上传产物 + 明文 next.config.ts ==" -ForegroundColor Cyan
scp $tar "${SshUser}@${Server}:${AppDir}/frontend/"
if ($LASTEXITCODE -ne 0) { throw "上传失败" }
scp $PlainConfig "${SshUser}@${Server}:${AppDir}/frontend/next.config.ts"
if ($LASTEXITCODE -ne 0) { throw "next.config.ts 上传失败" }

Write-Host "== 4/6 服务器解压 + 校验 BUILD_ID ==" -ForegroundColor Cyan
ssh "${SshUser}@${Server}" "cd ${AppDir}/frontend && tar -xf frontend_release.tar && rm frontend_release.tar && echo BUILD_ID=`$(cat .next/BUILD_ID)"

Write-Host "== 5/6 重建前端镜像 + 三步重启 ==" -ForegroundColor Cyan
ssh "${SshUser}@${Server}" "cd ${AppDir} && docker-compose build frontend 2>&1 | tail -3 && docker-compose stop frontend && docker-compose rm -f frontend && docker-compose up -d frontend"

Write-Host "== 6/6 验证 ==" -ForegroundColor Cyan
Start-Sleep -Seconds 15
ssh "${SshUser}@${Server}" "curl -s -o /dev/null -w 'ui_8080:%{http_code}' --max-time 8 http://127.0.0.1:8080/; echo"

Write-Host ""
Write-Host "OK 前端发布完成。期望：ui_8080:200" -ForegroundColor Green