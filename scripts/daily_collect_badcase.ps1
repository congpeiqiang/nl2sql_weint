# 每日 BadCase 采集 → Langfuse Dataset:badcase
# 由 Windows 任务计划（nl2sql-collect-badcase）进程外触发，日志追加到根目录
# server_collect_badcase.log。进程外启动避免随 Claude 会话被杀
# （见记忆 server-run-durability-background-task）。
$ErrorActionPreference = 'Continue'
$root = 'D:\code_work_space\llm\nl2sql'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = "$root\src"
$log = Join-Path $root 'server_collect_badcase.log'
$cmd = Join-Path $root '.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$stamp] begin"
try {
    Push-Location $root
    & $cmd -m agent.eval.collect_badcase --days 1 *>> $log
    $code = $LASTEXITCODE
    Add-Content -Path $log -Value "[$stamp] collect_badcase exit=$code"
    # M6 闭环：紧随采集跑一次真实反馈门禁（好评率报表 + 放量判断），
    # 数据不足时 exit 0（跳过），回归时 exit 1（记录到日志不中断采集任务）。
    & $cmd -m agent.eval.feedback_gate --days 7 *>> $log
    $fgcode = $LASTEXITCODE
    Add-Content -Path $log -Value "[$stamp] feedback_gate exit=$fgcode"
    # P0 闭环：输出待处理 badcase 数量（人工复审提醒）
    & $cmd -m agent.eval.badcase_status summary *>> $log
    Pop-Location
} catch {
    Add-Content -Path $log -Value "[$stamp] ERROR: $($_.Exception.Message)"
}
