# 创建 Windows 计划任务：每天 09:00 运行 run_daily.bat
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $root "run_daily.bat"
$taskName = "arXiv每日论文"
$time = "09:00"

if (-not (Test-Path $bat)) {
    throw "找不到 $bat"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`""
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "每天 $time 抓取 arXiv 最新论文摘要并发送到邮箱" `
    -Force | Out-Null

Write-Host ""
Write-Host "已创建计划任务：$taskName" -ForegroundColor Green
Write-Host "发送时间：每天 $time（本机时区）"
Write-Host "运行脚本：$bat"
Write-Host ""
Write-Host "查看/修改：打开「任务计划程序」搜索 $taskName"
