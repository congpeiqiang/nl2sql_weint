# ============================================================
# NL2SQL 前端发布脚本（一键：本地构建 -> 打包产物 -> 上传 ->
#                         服务器重建 -> 三步重启 -> 验证）
# 运行环境：本机 PowerShell（已配置 SSH 密钥免密；yarn 可用）
# 注意：前端源码含 DLP 加密文件，构建必须在本地环境执行（默认自动 yarn build，
#       若因 DLP 失败，请手动构建后加 -SkipBuild 重跑）
# 用法：  .\release-frontend.ps1
# 参数：  -SkipBuild 跳过本地构建（产物 .next 须已是最新）
#
# 2026-09-10 加固（事故：构建拉不到基础镜像，脚本却报「OK 前端发布完成」）：
#   1) 上传明文 Dockerfile（与 next.config.ts 同源同理），避免服务器副本漂移；
#   2) 校验产物 BUILD_ID 本地 == 服务器（不一致直接失败）；
#   3) 构建失败不再被 `| tail -3` 吃掉退出码（原写法 `$?` 是 tail 的 0）；
#   4) 验证步骤比对 http_code，非 200 直接 throw（不再无条件打印 OK）。
# ============================================================
param(
    [string]$LocalRoot   = "D:\code_work_space\llm\huice\008\harness-deep-agents-ui",
    [string]$WorkDir     = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app",
    [string]$PlainConfig = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\next.config.ts",
    [string]$PlainDocker = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\Dockerfile",
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

$localId = (Get-Content (Join-Path $LocalRoot ".next\BUILD_ID") -Raw).Trim()
Write-Host "   本地 BUILD_ID=$localId"

Write-Host "== 2/6 打包构建产物（不含 node_modules / next.config.ts）==" -ForegroundColor Cyan
$tar = Join-Path $WorkDir "frontend_release.tar"
if (Test-Path $tar) { Remove-Item $tar }
tar -cf $tar --exclude=.next/cache -C $LocalRoot .next public package.json yarn.lock
if ($LASTEXITCODE -ne 0) { throw "打包失败" }
Write-Host "   打包完成：$(([math]::Round((Get-Item $tar).Length/1MB,1))) MB"

Write-Host "== 3/6 上传产物 + 明文 next.config.ts / Dockerfile ==" -ForegroundColor Cyan
scp $tar "${SshUser}@${Server}:${AppDir}/frontend/"
if ($LASTEXITCODE -ne 0) { throw "上传失败" }
scp $PlainConfig "${SshUser}@${Server}:${AppDir}/frontend/next.config.ts"
if ($LASTEXITCODE -ne 0) { throw "next.config.ts 上传失败" }
scp $PlainDocker "${SshUser}@${Server}:${AppDir}/frontend/Dockerfile"
if ($LASTEXITCODE -ne 0) { throw "Dockerfile 上传失败" }

Write-Host "== 4/6 服务器解压 + 校验 BUILD_ID ==" -ForegroundColor Cyan
$remoteId = ssh "${SshUser}@${Server}" "cd ${AppDir}/frontend && tar -xf frontend_release.tar && rm frontend_release.tar && cat .next/BUILD_ID"
if ($LASTEXITCODE -ne 0) { throw "服务器解压失败" }
$remoteId = "$remoteId".Trim()
Write-Host "   服务器 BUILD_ID=$remoteId"
if ($remoteId -ne $localId) { throw "BUILD_ID 不一致（本地 $localId / 服务器 $remoteId），产物未更新" }

Write-Host "== 5/6 重建前端镜像 + 三步重启 ==" -ForegroundColor Cyan
# 注意：构建输出重定向到文件再取退出码；不可用 `docker-compose build | tail`（管道后 $? 恒为 tail 的 0，
# 会掩盖构建失败——2026-09-10 就是因此报「成功」而 UI 实际是 000）。
$remoteBuild = 'cd ' + $AppDir + ' && docker-compose build frontend > /tmp/fe_build.log 2>&1; rc=$?; tail -3 /tmp/fe_build.log; if [ $rc -ne 0 ]; then echo "BUILD_FAILED rc=$rc"; exit $rc; fi; docker-compose stop frontend; docker-compose rm -f frontend; docker-compose up -d frontend nginx'
ssh "${SshUser}@${Server}" $remoteBuild
if ($LASTEXITCODE -ne 0) { throw "前端镜像构建失败（完整日志：服务器 /tmp/fe_build.log）" }

Write-Host "== 6/6 验证 ==" -ForegroundColor Cyan
Start-Sleep -Seconds 15
$code = ssh "${SshUser}@${Server}" "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8080/"
$code = "$code".Trim()
Write-Host "   ui_8080:$code（期望 200）"
if ($code -ne "200") { throw "验证失败：8080 返回 $code（期望 200），请检查 frontend/nginx 容器日志" }

Write-Host ""
Write-Host "OK 前端发布完成。已验证：ui_8080:200" -ForegroundColor Green
