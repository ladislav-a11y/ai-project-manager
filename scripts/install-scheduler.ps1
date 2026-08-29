[CmdletBinding()]
param(
    [string]$TaskName = 'AI Project Manager Scheduler',
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$userId = "$env:USERDOMAIN\$env:USERNAME"
$runnerPath = Join-Path $PSScriptRoot 'run-ai-project-manager.ps1'

if ($IntervalMinutes -lt 1) {
    throw 'IntervalMinutes must be at least 1.'
}
$pollIntervalSeconds = $IntervalMinutes * 60

if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Runner script is missing: $runnerPath"
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -File `"$runnerPath`" -PollIntervalSeconds $pollIntervalSeconds -ScheduledTaskName `"$TaskName`"" `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "AI Project Manager persistent watchdog (PM tick every $IntervalMinutes minutes): Trello -> provider failover -> Trello." `
        -Force | Out-Null
}
catch {
    # NativeErrorCode is not populated consistently by the ScheduledTasks
    # module. HRESULT 0x80070005 may only be present in FullyQualifiedErrorId
    # (as it is on Windows 11), so cover all three representations.
    $accessDenied = (
        $_.Exception.NativeErrorCode -eq 5 -or
        $_.Exception.HResult -eq -2147024891 -or
        $_.FullyQualifiedErrorId -match '0x80070005'
    )
    if ($accessDenied) {
        $adminStep = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -TaskName `"$TaskName`" -IntervalMinutes $IntervalMinutes"
        throw "Task Scheduler registration was denied (0x80070005). Open PowerShell with 'Run as administrator' and run exactly: $adminStep"
    }
    throw
}

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 8
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName

$watchdog = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and
    $_.CommandLine -like '*ai_project_manager.watchdog*' -and
    $_.CommandLine -like "*--scheduled-task-name*$taskName*"
} | Select-Object -First 1

if ($task.State -ne 'Running') {
    throw "Scheduled Task '$taskName' was registered but is not running (state=$($task.State), result=$($info.LastTaskResult))."
}
if (-not $watchdog) {
    throw "Scheduled Task '$taskName' is running but its watchdog process was not found. Inspect runtime\scheduler and LastTaskResult=$($info.LastTaskResult)."
}

Write-Host ''
Write-Host 'AI Project Manager Scheduler byl nainstalovan a spusten.' -ForegroundColor Green
Write-Host "Stav: $($task.State)"
Write-Host "Posledni spusteni: $($info.LastRunTime)"
Write-Host "Dalsi spusteni: $($info.NextRunTime)"
Write-Host "Watchdog PID: $($watchdog.ProcessId)"
