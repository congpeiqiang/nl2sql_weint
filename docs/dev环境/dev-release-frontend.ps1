<#
.SYNOPSIS
    dev 环境（192.168.25.34）前端发布 —— 本机 build → 64 上建镜像 → 搬进 34。

.DESCRIPTION
    为什么绕这么一圈（三条硬约束）：
      1) 前端构建依赖本机 DLP 解密 → 只能在**本机** yarn build；
      2) 本机没有 docker → 镜像只能在 64 上建（64 有网络，能拉 node 基础镜像）；
      3) 34 不能出外网 → 镜像只能从 64 搬进去。

    流程：本机 build → 打产物包 → scp 到 64 的 **dev 专用目录** → 64 上 docker build
          （tag 带时间戳，绝不动生产的 nl2sql-app_frontend:latest）
          → 34 侧 ssh 64 "docker save" | docker load → 在 34 上换 tag → 重启 frontend + nginx。

    口令：只有第 5 步（34 → 64 那一跳）可能需要。脚本先探这一跳有没有 key，
          有就全程零口令；没有才用本机缓存的口令（第一次问一次，之后不再问）。
          本机 → 64 本来就必须免密（生产发版脚本同要求），所以 1~4 步从不问口令。

    红线：在 64 上只写 /home/weint/apps/nl2sql/dev-build/、只用 nl2sql-dev-frontend:* 标签。

.PARAMETER Server64Password
    64 的登录口令，仅用于本次 save|load 搬运：写成 34 上的临时 askpass 助手，
    用完（无论成败）立即删除。不传则先试 34→64 免密，再退回本机缓存，最后才交互式输入。

.EXAMPLE
    .\dev-release-frontend.ps1
    .\dev-release-frontend.ps1 -SkipBuild              # 复用现有 .next
    .\dev-release-frontend.ps1 -SkipBuild -SkipTransport   # 镜像已在 34 上，只换 tag 重启

.NOTES
    前置：①《发布脚本.md》§1（到 34 免密）；② 本机 yarn 可用；③ 能 ssh 到 64。
    若 64 上还没有 dev-build 目录，本脚本会自动创建（在允许的 /home/weint/apps/nl2sql 树内）。
#>
[CmdletBinding()]
param(
    # 前端源码仓库（DPA 密文在本地可解密，所以构建必须在这台机器上做）
    [string]$LocalRoot   = "D:\code_work_space\llm\huice\008\harness-deep-agents-ui",
    [string]$WorkDir     = "D:\code_work_space\llm\deepseek-workspace\nl2sql-dev",
    # 明文副本：UI 源码里那份 next.config.ts 是 DLP 密文，不能用（与生产脚本同一份）
    [string]$PlainConfig = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\next.config.ts",
    [string]$PlainDocker = "D:\code_work_space\llm\deepseek-workspace\nl2sql-app\frontend\Dockerfile",

    [string]$BuildServer = "192.168.25.64",
    [string]$DevServer   = "192.168.25.34",
    [string]$SshUser     = "weint",
    # 64 上的 dev 专用构建目录（必须在 /home/weint/apps/nl2sql 树内）
    [string]$BuildDir64  = "/home/weint/apps/nl2sql/dev-build/frontend",
    [string]$DevDir      = "/mydata/nl2sql",

    [string]$Server64Password,
    [switch]$SkipBuild,
    [switch]$SkipTransport,
    # 复用已有镜像标签（配 -SkipBuild -SkipTransport 时用来重推某个已知版本）
    [string]$ImageTag,
    # 清掉本机缓存的 64 口令后退出
    [switch]$ForgetPassword
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding          = [System.Text.Encoding]::UTF8

# BatchMode=yes：禁止一切口令交互，没配 key 就立刻失败（不再每一步弹口令）
$SshOpt  = @("-o","BatchMode=yes","-o","ServerAliveInterval=20","-o","ConnectTimeout=15")
$T34     = "${SshUser}@${DevServer}"
$T64     = "${SshUser}@${BuildServer}"
$Stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$Img     = if ($ImageTag) { $ImageTag } else { "nl2sql-dev-frontend:$Stamp" }
# dev 的 compose 固定引用这个标签，新镜像最终必须顶到这里
$DevTag  = "nl2sql-app_frontend:latest"
$RollTag = "nl2sql-app_frontend:rollback-$Stamp"

$pwFile34  = "${DevDir}/.tmp/.pw64"
$askFile34 = "${DevDir}/.tmp/.ask64.sh"
$localPw   = Join-Path $WorkDir ".pw64.tmp"
$localAsk  = Join-Path $WorkDir ".ask64.tmp.sh"
# 64 口令的**本机**缓存（只在你这台机器上，不进仓库、不上服务器）：
# 存一次以后就不再问。-ForgetPassword 可清除。
$pwCache   = Join-Path $WorkDir ".pw64.cache"

function Write-Step([string]$Text) { Write-Host "== $Text ==" -ForegroundColor Cyan }

function Invoke-Remote34 {
    param([Parameter(Mandatory)][string]$Command, [switch]$AllowFail)
    $out = & ssh @SshOpt $T34 $Command
    # 退出码回写 $script:RemoteRc：别让调用方读 $LASTEXITCODE（会被后续别的命令覆盖）
    $script:RemoteRc = $LASTEXITCODE
    if (-not $AllowFail -and $script:RemoteRc -ne 0) { throw "34 上命令失败(rc=$($script:RemoteRc))：$Command" }
    return ($out -join "`n")
}
function Invoke-Remote64 {
    param([Parameter(Mandatory)][string]$Command, [switch]$AllowFail)
    $out = & ssh @SshOpt $T64 $Command
    $script:RemoteRc = $LASTEXITCODE
    if (-not $AllowFail -and $script:RemoteRc -ne 0) { throw "64 上命令失败(rc=$($script:RemoteRc))：$Command" }
    return ($out -join "`n")
}

if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Path $WorkDir | Out-Null }

Write-Host "dev 前端发布" -ForegroundColor White
Write-Host "  源码   : $LocalRoot"
Write-Host "  构建机 : $T64 ($BuildDir64)"
Write-Host "  目标   : $T34 ($DevDir)"
Write-Host "  镜像   : $Img  →  $DevTag"
Write-Host ""

# ---------- 口令：缓存优先，一次输入，之后不再问 ----------
if ($ForgetPassword) {
    if (Test-Path $pwCache) { Remove-Item $pwCache -Force; Write-Host "已清除本机缓存的 64 口令：$pwCache" -ForegroundColor Green }
    else { Write-Host "本机没有缓存的口令（$pwCache）" -ForegroundColor DarkGray }
    return
}

# ---------- 连通性：两边都先探一次（BatchMode，没配 key 就立刻说清楚）----------
$p34 = Invoke-Remote34 -AllowFail -Command "hostname; test -d ${DevDir} && echo DEV_OK"
if ($script:RemoteRc -ne 0 -or $p34 -notmatch "DEV_OK") {
    throw "连不上 $T34。先跑一次 .\dev-setup-ssh.ps1 配好免密（只需一次口令）。"
}
$p64 = Invoke-Remote64 -AllowFail -Command "hostname; test -d /home/weint/apps/nl2sql && echo PROD_OK"
if ($script:RemoteRc -ne 0 -or $p64 -notmatch "PROD_OK") {
    throw "连不上 $T64（构建机）。生产发版脚本本来也要求到 64 的免密，请先把这台机器的公钥装到 64 上。"
}

# 记住发布前的镜像，失败也有据可回滚
$prevId = (Invoke-Remote34 -AllowFail -Command "docker images -q $DevTag | head -1").Trim()
Write-Host "  发布前 $DevTag = $(if ($prevId) { $prevId } else { '(无)' })" -ForegroundColor DarkGray
Write-Host ""

try {
    # ---------- 1/7 本机构建 ----------
    if ($SkipBuild) {
        Write-Step "1/7 跳过构建（-SkipBuild）"
    }
    else {
        Write-Step "1/7 本机构建（DLP 解密需本机）"
        if (-not (Get-Command yarn -ErrorAction SilentlyContinue)) { throw "本机找不到 yarn" }
        Push-Location $LocalRoot
        try { yarn build; $rc = $LASTEXITCODE } finally { Pop-Location }
        if ($rc -ne 0) { throw "yarn build 失败（DLP 相关就手动构建后加 -SkipBuild 重跑）" }
    }
    $localId = (Get-Content (Join-Path $LocalRoot ".next\BUILD_ID") -Raw).Trim()
    Write-Host "   本地 BUILD_ID = $localId"

    if ($SkipTransport) {
        Write-Step "2-5/7 跳过构建镜像与搬运（-SkipTransport）"
    }
    else {
        # ---------- 2/7 打包产物 ----------
        Write-Step "2/7 打包前端产物"
        $tar = Join-Path $WorkDir "dev_frontend.tar"
        if (Test-Path $tar) { Remove-Item $tar -Force }
        # Dockerfile 的构建上下文需要：package.json / yarn.lock / next.config.ts / .next / public
        & tar -cf $tar --exclude=.next/cache -C $LocalRoot ".next" "public" "package.json" "yarn.lock"
        if ($LASTEXITCODE -ne 0) { throw "打包失败" }
        Write-Host ("   {0} MB" -f [math]::Round((Get-Item $tar).Length / 1MB, 1))

        # ---------- 3/7 上传到 64 的 dev 专用目录 ----------
        Write-Step "3/7 上传到 64 的 dev 专用目录（不碰生产的 frontend/）"
        foreach ($f in @($PlainConfig, $PlainDocker)) {
            if (-not (Test-Path $f)) { throw "缺少明文文件：$f" }
        }
        Invoke-Remote64 -Command "rm -rf $BuildDir64 && mkdir -p $BuildDir64"
        & scp @SshOpt $tar "${T64}:${BuildDir64}/dev_frontend.tar"
        & scp @SshOpt $PlainConfig "${T64}:${BuildDir64}/next.config.ts"
        & scp @SshOpt $PlainDocker "${T64}:${BuildDir64}/Dockerfile"
        if ($LASTEXITCODE -ne 0) { throw "上传 64 失败" }
        Write-Host "   -> $BuildDir64"

        # ---------- 4/7 64 上构建镜像 ----------
        Write-Step "4/7 64 上构建镜像 $Img"
        # 构建输出重定向到文件再取退出码：`docker build | tail` 之后 $? 恒为 tail 的 0，
        # 会掩盖构建失败（2026-09-10 生产事故就是这么报"成功"而 UI 实际打不开的）。
        $b = "set -e; cd $BuildDir64; tar -xf dev_frontend.tar; rm -f dev_frontend.tar; " +
             "if docker build -t $Img . > /tmp/dev_fe_build.log 2>&1; then " +
             "  tail -4 /tmp/dev_fe_build.log; echo IMAGE_ID=`$(docker images -q $Img); " +
             "else tail -25 /tmp/dev_fe_build.log; echo BUILD_FAILED; exit 1; fi"
        $out4 = Invoke-Remote64 -Command $b
        $out4 -split "`n" | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
        if ($out4 -notmatch "IMAGE_ID=\S") { throw "64 上构建失败（完整日志：64:/tmp/dev_fe_build.log）" }

        # ---------- 5/7 搬运（34 发起，64 零落盘） ----------
        Write-Step "5/7 34 侧把镜像拉过去（ssh 64 'docker save' | docker load）"
        # 本机 → 64 早就配了 key（生产发版脚本本来就要求这个），所以 1~4 步从不问口令；
        # 真正需要口令的**只有 34 → 64 这一跳**。所以先探一次这一跳：
        #   有 key → 全程零口令（BatchMode 直接搬，连 askpass 都不建）
        #   没 key → 用本机缓存的口令，第一次问一次，之后不再问（-ForgetPassword 清除）
        $hopKey = (Invoke-Remote34 -AllowFail -Command "ssh -o BatchMode=yes -o ConnectTimeout=8 $T64 'echo HOP_OK' 2>&1 | tail -1") -match "HOP_OK"
        if ($hopKey) {
            Write-Host "   34 → 64 已免密（key 认证），本次不需要 64 口令" -ForegroundColor DarkGray
            $save = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 ${T64} 'docker save $Img'"
        }
        else {
            if (-not $Server64Password) {
                if (Test-Path $pwCache) {
                    $Server64Password = (Get-Content $pwCache -Raw).Trim()
                    Write-Host "   用本机缓存的口令（$pwCache）" -ForegroundColor DarkGray
                }
                else {
                    $sec = Read-Host -AsSecureString "请输入 64 ($BuildServer) 的登录口令（只用于本次镜像搬运；会缓存在本机，下次不再问）"
                    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
                    try   { $Server64Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
                    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
                    if ($Server64Password) {
                        Set-Content -Path $pwCache -Value $Server64Password -NoNewline -Encoding Ascii
                        # 收紧权限：只让当前用户可读（避免同机其他账号捡到）
                        & icacls $pwCache /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
                        Write-Host "   已缓存到 $pwCache（-ForgetPassword 可清除）" -ForegroundColor DarkGray
                    }
                }
            }
            # askpass 助手读一个**独立文件**里的原始口令 —— 不把口令拼进任何 shell 引号里，
            # 避免口令含 $ ' " 等字符时的转义问题。文件结束符必须是 LF（CRLF 会把 \r 带进
            # shebang，报 "bad interpreter"）。
            if ($Server64Password -notmatch '^[\x20-\x7E]+$') {
                throw "口令含非 ASCII 字符，本脚本用 ASCII 写助手文件会写坏。请改用 -Server64Password 传参或改用手工搬运。"
            }
            Set-Content -Path $localPw  -Value ("{0}`n" -f $Server64Password) -NoNewline -Encoding Ascii
            Set-Content -Path $localAsk -Value "#!/bin/sh`nexec cat $pwFile34`n"  -NoNewline -Encoding Ascii

            Invoke-Remote34 -Command "mkdir -p ${DevDir}/.tmp"
            & scp @SshOpt $localPw  "${T34}:${pwFile34}"
            & scp @SshOpt $localAsk "${T34}:${askFile34}"
            if ($LASTEXITCODE -ne 0) { throw "上传 askpass 助手失败" }

            $save = "chmod 700 .tmp/.ask64.sh .tmp/.pw64; " +
                    "export SSH_ASKPASS=${DevDir}/.tmp/.ask64.sh SSH_ASKPASS_REQUIRE=force; " +
                    "ssh -o StrictHostKeyChecking=no -o NumberOfPasswordPrompts=1 " +
                    "    -o PreferredAuthentications=password -o ConnectTimeout=10 ${T64} 'docker save $Img'"
        }
        $p = "set -e; cd ${DevDir}; $save | docker load; " +
             "(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^nl2sql-dev-frontend' | head -3) || true; " +
             "rm -f .tmp/.ask64.sh .tmp/.pw64; echo TRANSPORT_DONE"
        $out5 = Invoke-Remote34 -Command $p
        $out5 -split "`n" | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
        if ($out5 -notmatch "TRANSPORT_DONE") {
            throw "镜像搬运失败（先看 34→64 是否免密：ssh $T34 `"ssh -o BatchMode=yes $T64 hostname`"；没免密就是口令错）"
        }
    }

    # ---------- 6/7 换 tag + 重建容器 ----------
    Write-Step "6/7 换 tag（保留 rollback 标签）→ 重建 frontend → 重启 nginx"
    # ① 先把当前 tag 存成 rollback-<ts>，回滚只要一条 docker tag
    # ② dev 的 compose 固定引用 :latest，新镜像必须顶到 $DevTag
    # ③ nginx 启动时把上游主机名解析成 IP 并缓存 → 前端容器重建后 IP 变了，
    #    不 restart nginx 就是 502（首次部署时踩过）
    # 三处都用 docker inspect 取**完整** sha256：docker images -q 给的是短 ID，
    # 和 .Image 的 sha256:… 直接比会永远不相等（假失败）。
    # 注意字段名不同：容器用 .Image，镜像用 .Id。
    $d = "set -e; cd ${DevDir}/nl2sql-app; " +
         "(docker tag $DevTag $RollTag 2>/dev/null || true); " +
         "docker tag $Img $DevTag; " +
         "docker compose up -d --force-recreate frontend; " +
         "docker compose restart nginx; " +
         "echo RUNNING_IMAGE=`$(docker inspect --format '{{.Image}}' `$(docker compose ps -q frontend)); " +
         "echo LATEST_IMAGE=`$(docker inspect --format '{{.Id}}' $DevTag); " +
         "echo EXPECT_IMAGE=`$(docker inspect --format '{{.Id}}' $Img)"
    $out6 = Invoke-Remote34 -Command $d
    $out6 -split "`n" | Where-Object { $_ -match "^(RUNNING|LATEST|EXPECT)_IMAGE=" } | ForEach-Object {
        Write-Host "   $_" -ForegroundColor DarkGray
    }
    $runId = (($out6 -split "`n" | Where-Object { $_ -match "^RUNNING_IMAGE=" }) -replace "^RUNNING_IMAGE=", "").Trim()
    $expId = (($out6 -split "`n" | Where-Object { $_ -match "^EXPECT_IMAGE=" })  -replace "^EXPECT_IMAGE=", "").Trim()
    # 别写 $null.Trim()：取不到行时 $runId 是空串还好，是 $null 就直接抛
    if (-not $runId -or -not $expId) { throw "取不到镜像 ID（部署命令没按预期输出）" }
    if ($runId -ne $expId) { throw "容器跑的不是新镜像（running=$runId expect=$expId）" }

    # ---------- 7/7 验证 ----------
    Write-Step "7/7 验证"
    Start-Sleep -Seconds 12
    $code = (Invoke-Remote34 -AllowFail -Command "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8080/").Trim()
    Write-Host "   ui_8080 = $code （期望 200）"
    if ($code -ne "200") {
        Invoke-Remote34 -AllowFail -Command "cd ${DevDir}/nl2sql-app && docker compose logs --tail=60 frontend nginx 2>&1 | tail -30" |
            ForEach-Object { Write-Host "   $_" -ForegroundColor DarkYellow }
        throw "验证失败：8080 返回 $code"
    }
    $remoteId = (Invoke-Remote34 -AllowFail -Command "docker exec `$(cd ${DevDir}/nl2sql-app && docker compose ps -q frontend) cat /app/.next/BUILD_ID").Trim()
    Write-Host "   线上 BUILD_ID = $remoteId （本机 $localId）"
    if ($remoteId -and $remoteId -ne $localId) {
        Write-Host "   ⚠ BUILD_ID 与本机不一致：本地构建后又改过代码？或 .next 是旧的" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "OK dev 前端已更新（$Img）" -ForegroundColor Green
    Write-Host "   回滚： ssh $T34 `"cd ${DevDir}/nl2sql-app && docker tag $RollTag $DevTag && docker compose up -d --force-recreate frontend && docker compose restart nginx`"" -ForegroundColor Yellow
}
finally {
    # 清掉**临时**口令文件（本机的两个中转文件 + 34 上的 askpass 助手），
    # 34 上不长期留 64 凭据。注意本机缓存 $pwCache 是**故意留着**的（下次不再问），
    # 要清它用 -ForgetPassword。
    foreach ($f in @($localPw, $localAsk)) {
        if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
    }
    Invoke-Remote34 -AllowFail -Command "rm -f $askFile34 $pwFile34" | Out-Null
}
