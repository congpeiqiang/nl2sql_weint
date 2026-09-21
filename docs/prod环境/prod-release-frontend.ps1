# ============================================================
# NL2SQL 生产环境（192.168.25.64）前端发布脚本
# 功能：本地 yarn build -> 打包产物 -> 分片上传 -> 服务器解压+重建镜像+三步重启 -> 验证 BUILD_ID
# 用法：.\prod-release-frontend.ps1 [-SkipBuild]
# 前置：本机已配好到 64 的 SSH 密钥（免密登录）；yarn 可用
# 注意：前端代码受 DLP 加密保护，构建必须在本地环境执行
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

Write-Host "== 1/6 本地构建（DLP 解密需本机）==" -ForegroundColor Cyan
if (-not $SkipBuild) {
    Push-Location $LocalRoot
    # 前端代码受 DLP 加密，yarn build 会解密（DLP agent 在本机注册）
    # 构建失败时 yarn.lock 可能被修改，需还原
    yarn build
    if ($LASTEXITCODE -ne 0) {
        git checkout -- yarn.lock 2>$null
        Pop-Location
        throw "yarn build 失败（若为 DLP 加密问题请手动构建后加 -SkipBuild）"
    }
    Pop-Location
} else {
    Write-Host "   已跳过构建（使用现有 .next）"
}

Write-Host "== 2/6 提取 BUILD_ID ==" -ForegroundColor Cyan
$buildIdFile = Join-Path $LocalRoot ".next\BUILD_ID"
if (-not (Test-Path $buildIdFile)) { throw "找不到 .next/BUILD_ID——先构建" }
$localBuildId = (Get-Content $buildIdFile -Raw).Trim()
Write-Host "   本地 BUILD_ID: $localBuildId" -ForegroundColor Green

Write-Host "== 3/6 打包构建产物（不含 node_modules / next.config.ts）==" -ForegroundColor Cyan
$tarFile = "frontend_release.tar"
$tarPath = Join-Path $WorkDir $tarFile
if (Test-Path $tarPath) { Remove-Item $tarPath }
tar -cf $tarPath --exclude=.next/cache -C $LocalRoot .next public package.json yarn.lock
if ($LASTEXITCODE -ne 0) { throw "打包失败" }
$tarMB = [math]::Round((Get-Item $tarPath).Length/1MB,2)
Write-Host "   打包完成：$tarMB MB"

Write-Host "== 4/6 分片上传到服务器 ==" -ForegroundColor Cyan
$SshOpt = @("-o","ServerAliveInterval=20","-o","ServerAliveCountMax=6","-o","ConnectTimeout=15")
$ChunkMB = 1.0  # 前端产物较大，用 1MB 分片
$MaxRetry = 8
$partSize = [int]($ChunkMB * 1MB)
$bytes  = [IO.File]::ReadAllBytes($tarPath)
$nPart  = [math]::Ceiling($bytes.Length / $partSize)

# 清掉上次残留
ssh @SshOpt "${SshUser}@${Server}" "rm -f ${AppDir}/frontend/${tarFile} ${AppDir}/frontend/${tarFile}.part*" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "无法连接服务器——检查 VPN" }

for ($i = 0; $i -lt $nPart; $i++) {
    $off  = $i * $partSize
    $len  = [math]::Min($partSize, $bytes.Length - $off)
    $name = "${tarFile}.part{0:D2}" -f $i
    $local = Join-Path $WorkDir $name
    $fs = [IO.File]::Create($local); $fs.Write($bytes, $off, $len); $fs.Close()
    $ok = $false
    for ($try = 1; $try -le $MaxRetry; $try++) {
        scp @SshOpt $local "${SshUser}@${Server}:${AppDir}/frontend/$name"
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        Write-Host ("   分片 {0}/{1} 第 {2}/{3} 次被重置，3s 后重试…" -f ($i+1), $nPart, $try, $MaxRetry) -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
    Remove-Item $local -ErrorAction SilentlyContinue
    if (-not $ok) { throw "分片 $name 上传失败（已重试 $MaxRetry 次）——把 ChunkMB 调更小再试" }
    Write-Host ("   分片 {0}/{1} 完成（{2} KB）" -f ($i+1), $nPart, [math]::Round($len/1KB,0)) -ForegroundColor DarkGray
}
Write-Host "   全部 $nPart 片已上传（共 $tarMB MB）" -ForegroundColor Green
Remove-Item $tarPath -ErrorAction SilentlyContinue

# 上传明文 next.config.ts（源码里那份是 DLP 密文，不能用于服务器构建）
Write-Host "   上传明文 next.config.ts ..."
scp @SshOpt $PlainConfig "${SshUser}@${Server}:${AppDir}/frontend/next.config.ts"
if ($LASTEXITCODE -ne 0) { throw "next.config.ts 上传失败" }

Write-Host "== 5/6 服务器拼接 + 解压 + 重建镜像 + 三步重启 ==" -ForegroundColor Cyan
# docker-compose v1 需 stop/rm/up 三步
ssh @SshOpt "${SshUser}@${Server}" "cd ${AppDir}/frontend && cat ${tarFile}.part* > ${tarFile} && rm -f ${tarFile}.part* && tar -xf ${tarFile} && rm -f ${tarFile} && echo BUILD_ID=`$(cat .next/BUILD_ID) && cd ${AppDir} && docker-compose build frontend 2>&1 | tail -3 && docker-compose stop frontend && docker-compose rm -f frontend && docker-compose up -d frontend"
if ($LASTEXITCODE -ne 0) { throw "构建或重启失败" }

Write-Host "== 6/6 验证 ==" -ForegroundColor Cyan
Start-Sleep -Seconds 15
ssh @SshOpt "${SshUser}@${Server}" "curl -s -o /dev/null -w 'ui_8080:%{http_code}' --max-time 8 http://127.0.0.1:8080/; echo; docker exec nl2sql-app_frontend_1 cat /app/.next/BUILD_ID"

Write-Host ""
Write-Host "OK 前端发布完成。期望：ui_8080:200 + BUILD_ID=$localBuildId" -ForegroundColor Green
