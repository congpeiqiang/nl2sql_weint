<#
.SYNOPSIS
    dev 环境（192.168.25.34）后端发布 —— 同步 src/ 并重启容器，不构建镜像。

.DESCRIPTION
    适用：只改了 src/ 下的 Python / prompt / 配置模板（日常 99% 的改动）。
    不适用：改了 pyproject.toml / uv.lock（依赖变了 → 见《发布脚本.md》§4）。

    原理：dev 的 /mydata/nl2sql/src 是 bind 挂载到容器 /app/src 的，
    换掉里面的文件 + 重启容器即可，镜像里的 .venv 不动。

    安全性：替换前会先把线上 src 打包成带时间戳的备份，失败/回滚命令会在结尾打印。
            解压前用 tar 清单校验哨兵文件，校验不过绝不删线上内容。

.EXAMPLE
    .\dev-release-backend.ps1
    .\dev-release-backend.ps1 -LocalRoot D:\code_work_space\llm\nl2sql

.NOTES
    前置：已按《发布脚本.md》§1 配置到 34 的 SSH 免密。
    本脚本不需要任何口令（全程走 SSH key）。
#>
[CmdletBinding()]
param(
    # 本机仓库根（默认按脚本位置自动推断：<repo>\docs\dev环境\ → ..\..）
    [string]$LocalRoot,
    # 本机暂存目录
    [string]$WorkDir   = "D:\code_work_space\llm\deepseek-workspace\nl2sql-dev",
    [string]$Server    = "192.168.25.34",
    [string]$SshUser   = "weint",
    [string]$DevDir    = "/mydata/nl2sql",
    # 已经打过包时跳过打包上传，只用现成的 tar
    [switch]$SkipPackage,
    # 跳过等待 /ok（不推荐）
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding          = [System.Text.Encoding]::UTF8

# BatchMode=yes：禁止一切口令交互。没配好 SSH key 就**立刻失败**，
# 而不是每一步弹一次口令（之前就吃了这个苦：轮询那 20 多次每个都要输一遍）。
$SshOpt = @("-o","BatchMode=yes","-o","ServerAliveInterval=20","-o","ConnectTimeout=15")
$Target = "${SshUser}@${Server}"
$Stamp  = Get-Date -Format "yyyyMMdd-HHmmss"

function Write-Step([string]$Text) { Write-Host "== $Text ==" -ForegroundColor Cyan }

# 远程执行：失败即抛（除非 -AllowFail）
function Invoke-Remote {
    param([Parameter(Mandatory)][string]$Command, [switch]$AllowFail)
    $out = & ssh @SshOpt $Target $Command
    # 退出码单独回写 $script:RemoteRc —— 不要靠调用方读 $LASTEXITCODE：
    # 那是"最后一个本地程序"的退出码，函数里跑过别的命令就会被覆盖。
    $script:RemoteRc = $LASTEXITCODE
    if (-not $AllowFail -and $script:RemoteRc -ne 0) {
        throw "远程命令失败(rc=$($script:RemoteRc))：$Command"
    }
    return ($out -join "`n")
}

# ---------- 0/5 定位仓库根 ----------
if (-not $LocalRoot) {
    $cand = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    if (Test-Path (Join-Path $cand "src\agent\main_agent.py")) { $LocalRoot = $cand }
    else { throw "推断不出仓库根（$cand 下没有 src\agent\main_agent.py），请用 -LocalRoot 指定" }
}
if (-not (Test-Path (Join-Path $LocalRoot "src\agent\main_agent.py"))) {
    throw "-LocalRoot 下找不到 src\agent\main_agent.py：$LocalRoot"
}
if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Path $WorkDir | Out-Null }

Write-Host "dev 后端发布" -ForegroundColor White
Write-Host "  仓库   : $LocalRoot"
Write-Host "  目标   : $Target ($DevDir)"
Write-Host "  时间戳 : $Stamp"
Write-Host ""

# 推的是本机工作树（含未提交改动）—— 这正是 dev 的用途，但先让人看清推的是什么
Push-Location $LocalRoot
$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
$dirty  = @(git status --porcelain 2>$null).Count
Pop-Location
Write-Host "  git    : $branch ｜ 未提交改动 $dirty 项" -ForegroundColor DarkGray
if ($dirty -gt 0) { Write-Host "  ⚠ 会把这些未提交改动一起推到 dev" -ForegroundColor Yellow }
Write-Host ""

# ---------- 连通性（BatchMode，失败即停）----------
$probe = Invoke-Remote -AllowFail -Command "hostname; test -d ${DevDir}/src && echo SRC_DIR_OK"
if ($script:RemoteRc -ne 0 -or $probe -notmatch "SRC_DIR_OK") {
    throw "连不上 $Target（或 ${DevDir}/src 不存在）。先跑一次 .\dev-setup-ssh.ps1 配好免密（只需一次口令）。"
}

# ---------- 1/5 打包 ----------
$tar    = Join-Path $WorkDir "dev_src.tar.gz"
$remote = "${DevDir}/.tmp/dev_src.tar.gz"

if (-not $SkipPackage) {
    Write-Step "1/5 打包本机 src/"
    if (Test-Path $tar) { Remove-Item $tar -Force }
    # 排除项：编译缓存 / 运行时残留 / 工作区注册表（跟过去会覆盖服务器注册表）
    # 同时写 "*/x" 与 "x" 两种形式，兼容不同 tar 的 glob 语义
    $ex = @(
        "--exclude=*/__pycache__", "--exclude=__pycache__",
        "--exclude=*.pyc",
        "--exclude=src/agent/workspace", "--exclude=*/agent/workspace",
        "--exclude=src/agent/workspace-temp", "--exclude=*/agent/workspace-temp",
        "--exclude=src/agent/workspace_manager/workspaces.json", "--exclude=*/workspace_manager/workspaces.json",
        "--exclude=src/.tmp", "--exclude=*/.tmp",
        # model_config.json 是 gitignore 的运行时配置，容器读的是 AGENT_DATA_ROOT
        # （/app/data/shared），src 里这份只是旧种子残留。不排掉的话每次发版都会把它
        # 铺到 /app/src/agent/shared/ 下——一旦 AGENT_DATA_ROOT 没生效就会静默回退读它。
        "--exclude=src/agent/shared/model_config.json", "--exclude=*/shared/model_config.json"
    )
    & tar -czf $tar @ex -C $LocalRoot src
    if ($LASTEXITCODE -ne 0) { throw "打包失败" }
    $mb = [math]::Round((Get-Item $tar).Length / 1MB, 2)
    Write-Host "   $tar ($mb MB)"

    Write-Step "2/5 上传到 $Target"
    Invoke-Remote -Command "mkdir -p ${DevDir}/.tmp" | Out-Null
    & scp @SshOpt $tar "${Target}:${remote}"
    if ($LASTEXITCODE -ne 0) { throw "上传失败" }
    Write-Host "   -> $remote"
}
else {
    Write-Step "1-2/5 跳过打包（-SkipPackage）"
}

# ---------- 3/5 备份 → 校验 → 替换（交给服务端助手脚本做）----------
Write-Step "3/5 备份线上 src → 校验包 → 清空 → 换装"
# 文件操作全部在服务端助手 dev-swap-src.sh 里做，原因是**这里踩过一次大坑**：
# 容器以 root 运行，会在 src 里写出 root 属主的 __pycache__/*.pyc；weint 删不掉它们，
# 于是宿主机上的 `find src -mindepth 1 -delete` 必然中途 Permission denied 退出，
# 而它退出时 src 已经被删了一半 —— 2026-09-21 实际发生：278 个 .py 全没，只剩 root 的 __pycache__。
# 现在的做法：用**应用镜像起一次性 root 容器**做清空+解包（不用 sudo、不用口令），
# 而且先解到暂存区校验通过、再清空真目录，解包失败时线上 src 原封不动。
$script:prev = ".tmp/src-prev-$Stamp.tgz"
$remote3 = "set -e; cd ${DevDir}; " +
           "bash .tmp/dev_swap_src.sh backup $Stamp; " +
           "bash .tmp/dev_swap_src.sh swap $remote; " +
           "rm -f $remote; " +
           "echo BACKUP=$($script:prev)"
# 助手脚本本体随每次发布送上去，保证和服务端行为一致（几 KB，忽略不计）
$helper = Join-Path $PSScriptRoot "dev-swap-src.sh"
if (-not (Test-Path $helper)) { throw "缺少服务端助手脚本：$helper（应与本脚本同目录）" }
& scp @SshOpt $helper "${Target}:${DevDir}/.tmp/dev_swap_src.sh"
if ($LASTEXITCODE -ne 0) { throw "上传 dev-swap-src.sh 失败" }

try {
    $r3 = Invoke-Remote -Command $remote3
    $r3 -split "`n" | Where-Object { $_ -match "^(包校验|换装|BACKUP=|ERROR)" } | ForEach-Object {
        Write-Host "   $($_.Trim())"
    }
}
catch {
    Write-Host "换装失败。回滚（助手会走同一条 root 容器路径，weint 删不掉的 root 文件也删得掉）：" -ForegroundColor Red
    Write-Host "  ssh $Target `"cd $DevDir && bash .tmp/dev_swap_src.sh restore $($script:prev) && cd nl2sql-app && docker compose restart langgraph-api`"" -ForegroundColor Yellow
    throw
}

# ---------- 4/5 重启 ----------
Write-Step "4/5 重启 langgraph-api（compose v2 插件，不是 docker-compose v1）"
Invoke-Remote -Command "cd ${DevDir}/nl2sql-app && docker compose restart langgraph-api" | Out-Null

# ---------- 5/5 验证 ----------
if ($SkipVerify) {
    Write-Step "5/5 已跳过验证（-SkipVerify）"
}
else {
    Write-Step "5/5 等待 /ok（冷启动约 25~40s，MCP 子进程预检耗时）"
    # 轮询整个在服务端循环，一次 ssh 搞定 —— 之前每个 5 秒一次 ssh，无 key 时就是挨个弹口令
    $poll = "c=000; for i in `$(seq 1 30); do sleep 4; " +
            "c=`$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:2026/ok); " +
            "echo `"[`$i] /ok=`$c`"; [ `"`$c`" = 200 ] && break; done; " +
            "echo FINAL=`$c"
    $out5 = Invoke-Remote -AllowFail -Command $poll
    $out5 -split "`n" | Where-Object { $_.Trim() } | ForEach-Object {
        Write-Host "   $($_.Trim())" -ForegroundColor DarkGray
    }
    $final = (($out5 -split "`n" | Where-Object { $_ -match "^FINAL=" }) -replace "^FINAL=", "").Trim()
    if ($final -ne "200") {
        Write-Host "后端没起来，最近的错误日志：" -ForegroundColor Red
        Invoke-Remote -AllowFail -Command "cd ${DevDir}/nl2sql-app && docker compose logs --tail=300 langgraph-api 2>&1 | grep -E '预检失败|Error|Traceback|未配置任何数据库' | tail -20" |
            ForEach-Object { Write-Host "   $_" -ForegroundColor DarkYellow }
        throw "验证失败：/ok=$final"
    }
    $line = Invoke-Remote -AllowFail -Command "cd ${DevDir}/nl2sql-app && docker compose logs --tail=500 langgraph-api 2>&1 | grep -E '预检通过|预检失败' | tail -1"
    if ($line) { Write-Host "   $($line.Trim())" -ForegroundColor Green }
}

Write-Host ""
Write-Host "OK dev 后端已更新" -ForegroundColor Green
Write-Host "   回滚（恢复本次发布前的 src）：" -ForegroundColor Yellow
Write-Host "     ssh $Target `"cd $DevDir && bash .tmp/dev_swap_src.sh restore $($script:prev) && cd nl2sql-app && docker compose restart langgraph-api`"" -ForegroundColor Yellow
Write-Host "   备份文件在 $DevDir/$($script:prev)（只增不删，需要时手动清理）" -ForegroundColor DarkGray
