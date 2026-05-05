# Register the weekly-update.ps1 pipeline as a Windows scheduled task that
# fires every Thursday at 09:00 local time.
#
# Run this ONCE, from an elevated PowerShell prompt:
#
#   powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-task.ps1
#
# To unregister later:
#
#   Unregister-ScheduledTask -TaskName 'planningcheck-decisions-weekly' -Confirm:$false

$TaskName  = 'planningcheck-decisions-weekly'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = Join-Path $ScriptDir 'weekly-update.ps1'

if (-not (Test-Path $ScriptPath)) {
    throw "weekly-update.ps1 not found at $ScriptPath"
}

# 09:00 every Thursday. The task will also run on next wake if the laptop
# was off at the scheduled time.
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Thursday -At 09:00

# Run our PowerShell script. -NoProfile speeds startup; -WindowStyle Hidden
# avoids a console window flashing up.
$Action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

# Settings: catch up after sleep, retry on failure, only run if mains-powered
# (don't drain laptop battery), no time limit.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([System.TimeSpan]::FromHours(2)) `
    -RestartCount 3 `
    -RestartInterval ([System.TimeSpan]::FromMinutes(15))

# Run as the current user, only when logged on. (S4U / "run when not logged
# on" requires admin to register — Interactive doesn't.) On a personal laptop
# this is fine: if you're away for a few days, StartWhenAvailable means the
# task runs as soon as you come back.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# Idempotent: replace any existing task with the same name.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Weekly ACP decisions scrape + classify + push to planningcheck.ie' `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal | Out-Null

Write-Host "Registered: $TaskName"
Write-Host "Next run:   $((Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo).NextRunTime)"
Write-Host ""
Write-Host "To run it now manually (without waiting for Thursday):"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To check status / history:"
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host ""
Write-Host "To unregister:"
Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
