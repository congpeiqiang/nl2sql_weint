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
    [string]$AppDir    = "/home/weint/apps/nl2sql/nl2sql-app",
    # 分片大小(MB) / 每片重试次数。VPN 链路不稳时把 ChunkMB 调小（连接寿命短
    # 于单片传输时间就必断），链路好时调大减少连接次数。2026-09-18 加：经 VPN
    # 直传 34.9MB 单连接在 ~480KB 处被 reset（28KB/s），无法靠重试整包救回。
    [double]$ChunkMB  = 0.5,
    [int]$MaxRetry    = 8
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$tar = Join-Path $WorkDir "backend_release.tar.gz"
if (Test-Path $tar) { Remove-Item $tar }

Write-Host "== 1/5 本地打包（gzip + 排除大目录/环境变量/探针）==" -ForegroundColor Cyan
# -czf 而非 -cf：34.9MB → 9.2MB（3.8x），链路越差这个杠杆越大。服务器侧解压
# 用 tar -xzf（见 3/5）。19s 本机 CPU 换 3.8 倍传输量，稳赚。
tar -czf $tar `
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
$tarMB = [math]::Round((Get-Item $tar).Length/1MB,2)
Write-Host "   打包完成：$tarMB MB（gzip 后）"

# SSH/SCP 公共选项：keepalive 防止链路空闲被判死；ConnectTimeout 避免卡在握手。
$SshOpt = @("-o","ServerAliveInterval=20","-o","ServerAliveCountMax=6","-o","ConnectTimeout=15")

Write-Host "== 2/5 上传到服务器（分片 $ChunkMB MB × N，每片独立重试 $MaxRetry 次）==" -ForegroundColor Cyan
$partSize = [int]($ChunkMB * 1MB)
if ($partSize -lt 65536) { throw "ChunkMB 太小（下限 0.0625）" }
$bytes  = [IO.File]::ReadAllBytes($tar)
$nPart  = [math]::Ceiling($bytes.Length / $partSize)
# 清掉上次残留（含旧版脚本可能留在服务器上的整包 tar）
ssh @SshOpt "${SshUser}@${Server}" "rm -f ${AppDir}/backend_release.tar ${AppDir}/backend_release.tar.gz ${AppDir}/backend_release.tar.gz.part*" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "无法连接服务器（清残留失败）——检查 VPN" }

for ($i = 0; $i -lt $nPart; $i++) {
    $off  = $i * $partSize
    $len  = [math]::Min($partSize, $bytes.Length - $off)
    # 两位补零：cat part* 的字典序 == 数字序，最多 99 片（约 50MB @0.5MB）
    $name = "backend_release.tar.gz.part{0:D2}" -f $i
    $local = Join-Path $WorkDir $name
    $fs = [IO.File]::Create($local); $fs.Write($bytes, $off, $len); $fs.Close()
    $ok = $false
    for ($try = 1; $try -le $MaxRetry; $try++) {
        scp @SshOpt $local "${SshUser}@${Server}:${AppDir}/$name"
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        Write-Host ("   分片 {0}/{1} 第 {2}/{3} 次被重置，3s 后重试…" -f ($i+1), $nPart, $try, $MaxRetry) -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
    Remove-Item $local -ErrorAction SilentlyContinue
    if (-not $ok) { throw "分片 $name 上传失败（已重试 $MaxRetry 次）——把 -ChunkMB 调更小再试" }
    Write-Host ("   分片 {0}/{1} 完成（{2} KB）" -f ($i+1), $nPart, [math]::Round($len/1KB,0)) -ForegroundColor DarkGray
}
Write-Host "   全部 $nPart 片已上传（共 $tarMB MB）" -ForegroundColor Green

Write-Host "== 3/5 服务器拼接 + 解压 + 重建镜像 ==" -ForegroundColor Cyan
ssh @SshOpt "${SshUser}@${Server}" "cd ${AppDir} && cat backend_release.tar.gz.part* > backend_release.tar.gz && rm -f backend_release.tar.gz.part* && tar -xzf backend_release.tar.gz -C backend && rm -f backend_release.tar.gz && docker-compose build langgraph-api 2>&1 | tail -3"
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

Write-Host "== 4/5 三步重启（v1 compose 需 stop/rm/up）==" -ForegroundColor Cyan
ssh @SshOpt "${SshUser}@${Server}" "cd ${AppDir} && docker-compose stop langgraph-api && docker-compose rm -f langgraph-api && docker-compose up -d langgraph-api"

Write-Host "== 5/5 等待启动并验证 ==" -ForegroundColor Cyan
Start-Sleep -Seconds 75
ssh @SshOpt "${SshUser}@${Server}" "curl -s -o /dev/null -w 'backend_ok:%{http_code}' --max-time 6 http://127.0.0.1:2026/ok; echo; docker logs nl2sql-app_langgraph-api_1 2>&1 | grep -E 'MCP 工具加载完成|预检通过|预检失败' | tail -4"

Write-Host ""
Write-Host "OK 后端发布完成。期望：backend_ok:200 + 预检通过: N 个 MCP 工具就绪（echarts=18, wrenai_*=27±, dbmcp=2 逐项 breakdown）" -ForegroundColor Green
Write-Host "⚠ 若见「预检失败」或 backend_ok 非 200：关键 MCP server（wrenai_*/dbmcp）加载失败会拒绝启动，按日志排障提示处理" -ForegroundColor Yellow