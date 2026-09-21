<#
.SYNOPSIS
    dev 环境一次性准备：给本机装一把 SSH key 并装到 34 上，之后发布脚本全程不再问口令。

.DESCRIPTION
    为什么需要：发布脚本要反复 ssh/scp 到 34（上传、换装、轮询、看日志），
    没有 key 的话**每一步都会弹一次口令**（后端脚本光轮询就有 20 多次）。

    做的事：
      1) 没有默认 key（~/.ssh/id_ed25519）就生成一把（空口令）；
      2) 把公钥追加到 34 的 ~/.ssh/authorized_keys（这一步要输一次 34 的口令）；
      3) 用 BatchMode 验证免密登录真的生效。

    幂等：重复执行只会跳过已存在的 key、去重后不再追加同一把公钥。

.EXAMPLE
    .\dev-setup-ssh.ps1
#>
[CmdletBinding()]
param(
    [string]$Server  = "192.168.25.34",
    [string]$SshUser = "weint",
    [string]$KeyPath = (Join-Path $env:USERPROFILE ".ssh\id_ed25519")
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding          = [System.Text.Encoding]::UTF8
$Target = "${SshUser}@${Server}"

Write-Host "dev 环境 SSH 免密准备 —— 目标 $Target" -ForegroundColor White
Write-Host ""

# ---------- 1) key ----------
if (Test-Path $KeyPath) {
    Write-Host "已存在私钥：$KeyPath（复用）"
}
else {
    Write-Host "生成新私钥：$KeyPath"
    & ssh-keygen -t ed25519 -f $KeyPath -N '""' -C "nl2sql-dev-$env:COMPUTERNAME"
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen 失败（是不是已经有同名文件了？）" }
}
$pub = "$KeyPath.pub"
if (-not (Test-Path $pub)) { throw "找不到公钥 $pub" }
$pubText = (Get-Content $pub -Raw).Trim()
# 公钥必须是单行 "ssh-ed25519 <base64> [注释]"：多行残留/字段缺失会让下面的指纹打印
# 和远端 grep 出怪事（grep 用的正是这一整行）。宁在这一步明确报错，也别拿坏 key 去装。
if ($pubText -match "[\r\n]") { throw "公钥文件里有多行内容（$pub）——不是一把干净的 key，先查一下" }
$parts = $pubText.Split(' ')
if ($parts.Count -lt 2 -or $parts[0] -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-)') {
    throw "公钥格式不对（$pub）：第一段应是 ssh-ed25519 / ssh-rsa / ecdsa-…"
}
if ($parts[1].Length -lt 24) { throw "公钥数据段过短（$($parts[1].Length) 字符），$pub 可能被截断" }
Write-Host "公钥指纹：$($parts[1].Substring(0, 24))..."
Write-Host ""

# ---------- 2) 装到 34（这里会问一次口令）----------
Write-Host "把公钥装到 34（这次需要输一次 34 的口令）..." -ForegroundColor Yellow
$install = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && " +
           "chmod 600 ~/.ssh/authorized_keys && " +
           "(grep -qxF '$pubText' ~/.ssh/authorized_keys || echo '$pubText' >> ~/.ssh/authorized_keys) && " +
           "echo INSTALLED && wc -l < ~/.ssh/authorized_keys"
& ssh -o StrictHostKeyChecking=accept-new -o NumberOfPasswordPrompts=1 $Target $install
if ($LASTEXITCODE -ne 0) { throw "装公钥失败（口令错？34 不可达？）" }
Write-Host ""

# ---------- 3) 验证免密 ----------
# BatchMode=yes 禁止一切交互 => 只有 key 认证成功才会返回 0
Write-Host "验证免密登录（BatchMode，不会有任何提示）..."
$probe = & ssh -o BatchMode=yes -o ConnectTimeout=10 $Target "hostname; test -d /mydata/nl2sql && echo DEV_DIR_OK"
if ($LASTEXITCODE -ne 0 -or ($probe -join "`n") -notmatch "DEV_DIR_OK") {
    throw "免密没生效。检查 34 上 ~/.ssh 权限（700）与 authorized_keys（600），以及它是否禁用了 PubkeyAuthentication。"
}
Write-Host ($probe -join "`n") -ForegroundColor DarkGray
Write-Host ""
Write-Host "OK 以后 ssh/scp 到 $Target 都不再问口令了。 " -ForegroundColor Green
Write-Host "   现在可以跑 .\dev-release-backend.ps1 / .\dev-release-frontend.ps1" -ForegroundColor Green
