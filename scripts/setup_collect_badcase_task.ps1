# 注册每日 BadCase 采集任务（进程外，Task Scheduler）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_collect_badcase_task.ps1
$ErrorActionPreference = 'Stop'
$root = 'D:\code_work_space\llm\nl2sql'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    "-NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\daily_collect_badcase.ps1`""
)
$trigger = New-ScheduledTaskTrigger -Daily -At 2:13AM
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'nl2sql-collect-badcase' -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Write-Output "registered: nl2sql-collect-badcase (daily 02:13)"
