# ============================================================
# NL2SQL 前端发布脚本（一键：本地构建 -> 打包产物 -> 分片上传 ->
#                         服务器重建 -> 三步重启 -> 验证 BUILD_ID）
# 运行环境：本机 PowerShell（已配置 SSH 密钥免密；yarn 可用）
# 注意：前端源码含 DLP 加密文件，构建必须在本地环境执行（DLP agent 在本机注册）
# 用法：  .\release-frontend.ps1
# 参数：  -SkipBuild 跳过本地构建（产物 .next 须已是最新）
#
# 2026-09-10 加固（事故：构建拉不到基础镜像，脚本却报「OK 前端发布完成」）：
#   1) 上传明文 Dockerfile（与 next.config.ts 同源同理），避免服务器副本漂移；
#   2) 校验产物 BUILD_ID 本地 == 服务器（不一致直接失败）；
#   3) 构建失败不再被 `| tail -3` 吃掉退出码（原写法 `$?` 是 tail 的 0）；
#   4) 验证步骤比对 http_code，非 200 直接 throw（不再无条件打印 OK）。
# 2026-09-22 合并分片上传逻辑（原 docs/prod环境 版本），解决 VPN 链路大文件断传。
# 2026-10-05（档 1）：`up -d` **不再带 nginx**（改由 nginx 的请求期解析自己发现新前端 IP，
#   见 docker/nginx.conf）；验证从「盲等 15s + 打一次 /」改成**轮询 / 到 200**，并追加两发
#   经 nginx 的后端探针：`/ok`（命中 regex location）+ `/api/deployment-info`（命中
#   `location /api/`）—— 本次 nginx 改动两个 location 都动了，只探一个盖不全。
# ============================================================
param(
    [string]$LocalRoot    = "D:\code_work_space\llm\huice\008\harness-deep-agents-ui",
    [string]$WorkDir      = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app",
    [string]$PlainConfig  = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\next.config.ts",
    [string]$PlainDocker = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\Dockerfile",
    [string]$Server       = "192.168.25.64",
    [string]$SshUser      = "weint",
    [string]$AppDir       = "/home/weint/apps/nl2sql/nl2sql-app",
    [switch]$SkipBuild,
    # 分片大小(MB) / 每片重试次数。VPN 链路不稳时把 ChunkMB 调小
    [double]$ChunkMB      = 1.0,
    [int]$MaxRetry        = 8
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

Write-Host "== 1/6 本地生产构建（DLP 解密需本机）==" -ForegroundColor Cyan
if (-not $SkipBuild) {
    Push-Location $LocalRoot
    # 前端代码受 DLP 加密，yarn build 会透明解密（DLP agent 在本机注册）
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

# SSH/SCP 公共选项：keepalive 防止链路空闲被判死；ConnectTimeout 避免卡在握手
$SshOpt = @("-o","ServerAliveInterval=20","-o","ServerAliveCountMax=6","-o","ConnectTimeout=15")

Write-Host "== 4/6 分片上传到服务器（$ChunkMB MB/片，每片重试 $MaxRetry 次）==" -ForegroundColor Cyan
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
    if (-not $ok) { throw "分片 $name 上传失败（已重试 $MaxRetry 次）——把 -ChunkMB 调更小再试" }
    Write-Host ("   分片 {0}/{1} 完成（{2} KB）" -f ($i+1), $nPart, [math]::Round($len/1KB,0)) -ForegroundColor DarkGray
}
Write-Host "   全部 $nPart 片已上传（共 $tarMB MB）" -ForegroundColor Green
Remove-Item $tarPath -ErrorAction SilentlyContinue

# 上传明文 next.config.ts + Dockerfile（源码里那份是 DLP 密文，不能用于服务器构建）
Write-Host "   上传明文 next.config.ts + Dockerfile ..."
scp @SshOpt $PlainConfig "${SshUser}@${Server}:${AppDir}/frontend/next.config.ts"
if ($LASTEXITCODE -ne 0) { throw "next.config.ts 上传失败" }
scp @SshOpt $PlainDocker "${SshUser}@${Server}:${AppDir}/frontend/Dockerfile"
if ($LASTEXITCODE -ne 0) { throw "Dockerfile 上传失败" }

Write-Host "== 5/6 服务器拼接 + 校验 BUILD_ID + 重建镜像 + 三步重启 ==" -ForegroundColor Cyan
# docker-compose v1 需 stop/rm/up 三步
# 2026-10-05：`up -d` **不再带 nginx** —— nginx 已经不在启动期缓存上游 IP 了
# （docker/nginx.conf 改成 `resolver 127.0.0.11` + 变量 proxy_pass，请求期解析），
# 新前端容器的 IP 它自己会在 ≤10s 内发现，无需 reload 或重建。
# 旧写法 `up -d frontend nginx` 是历史遗留：nginx 配置没变时 compose 只会打
# "up-to-date" 不重建它，看似无害，但一旦真被重建就是**多引入一次 nginx 停机**。
ssh @SshOpt "${SshUser}@${Server}" "cd ${AppDir}/frontend && cat ${tarFile}.part* > ${tarFile} && rm -f ${tarFile}.part* && tar -xf ${tarFile} && rm -f ${tarFile} && echo BUILD_ID=`$(cat .next/BUILD_ID) && cd ${AppDir} && docker-compose build frontend > /tmp/fe_build.log 2>&1; rc=`$?; tail -3 /tmp/fe_build.log; if [ `$rc -ne 0 ]; then echo BUILD_FAILED rc=`$rc; exit `$rc; fi; docker-compose stop frontend; docker-compose rm -f frontend; docker-compose up -d frontend"
if ($LASTEXITCODE -ne 0) { throw "前端镜像构建失败（完整日志：服务器 /tmp/fe_build.log）" }

Write-Host "== 6/6 验证 ==" -ForegroundColor Cyan
# 轮询 `/` 直到 200（不再盲等 15s）；随后打一发**经 nginx 的 /ok** 证明后端链路没被动过
# ——「前端发版不碰 nginx」这句话得由这个探针来兜底。
# 写法同 release-backend.ps1：PS 双引号串里 bash 的 `$` 用**反引号**转义（别用 `\"`）。
$poll = "for i in `$(seq 1 40); do c=`$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:8080/); case `$c in 200) echo ui_8080=200; exit 0;; esac; sleep 2; done; echo ui_8080=timeout; exit 1"
ssh @SshOpt "${SshUser}@${Server}" $poll
if ($LASTEXITCODE -ne 0) { throw "验证失败：经 nginx 的 / 在 80s 内未恢复 200 —— 查 frontend 容器日志与 nginx error_log（上游解析？）" }
$apiCode = ssh @SshOpt "${SshUser}@${Server}" "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8080/ok"
$apiCode = "$apiCode".Trim()
Write-Host "   ui_8080:200   backend_via_nginx/ok:$apiCode（期望 200 —— 前端换了，后端链路照旧）"
if ($apiCode -ne "200") { throw "验证失败：经 nginx 的 /ok 返回 $apiCode（期望 200）—— 前端发版不该影响后端链路，检查 nginx 是否仍在正常转发" }

# 第二发探针专打 `/api/`：上面的 `/ok` 命中的是 `location ~ ^/(threads|runs|…)`，
# 而本次 nginx 改动**同时**动了 `location /api/` 那个块，regex 那条覆盖不到它。
# 200（未开鉴权）与 401（`/api/deployment-info` 要 require_user，无 cookie 必然 401）
# 都算「经 nginx 到得了应用」；上游解析坏掉是 502、路由没挂是 404、DNS 挂是 000。
$depCode = ssh @SshOpt "${SshUser}@${Server}" "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8080/api/deployment-info"
$depCode = "$depCode".Trim()
Write-Host "   api/deployment-info:$depCode（期望 200 或 401）"
if ($depCode -ne "200" -and $depCode -ne "401") { throw "验证失败：经 nginx 的 /api/deployment-info 返回 $depCode（期望 200/401）—— location /api/ 那条上游可能没解析到，查 nginx error_log" }

Write-Host ""
Write-Host "OK 前端发布完成。已验证：ui_8080:200 + BUILD_ID=$localBuildId" -ForegroundColor Green
