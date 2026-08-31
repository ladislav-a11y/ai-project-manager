[CmdletBinding()]
param(
    [string]$TaskName = 'AI Project Manager Scheduler',
    [int]$IntervalMinutes = 5,
    [string]$PythonExe = $env:AI_PM_PYTHON_EXE
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$userId = "$env:USERDOMAIN\$env:USERNAME"
$runnerPath = Join-Path $PSScriptRoot 'run-ai-project-manager.ps1'
$powerShellExe = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source

if ($IntervalMinutes -lt 1) {
    throw 'IntervalMinutes must be at least 1.'
}
$pollIntervalSeconds = $IntervalMinutes * 60

if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Runner script is missing: $runnerPath"
}
if (-not $PythonExe) {
    $PythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable is missing: $PythonExe"
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

$action = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runnerPath`" -PythonExe `"$PythonExe`" -PollIntervalSeconds $pollIntervalSeconds -ScheduledTaskName `"$TaskName`"" `
    -WorkingDirectory $projectRoot
$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn -User $userId
    New-ScheduledTaskTrigger -AtStartup
)
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
# This action is the persistent watchdog. A finite execution limit would
# terminate it without a replacement because logon/startup are the only
# triggers; the watchdog itself owns the periodic PM ticks.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $triggers `
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
        $adminStep = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -TaskName `"$TaskName`" -IntervalMinutes $IntervalMinutes -PythonExe `"$PythonExe`""
        throw "Task Scheduler registration was denied (0x80070005). Open PowerShell with 'Run as administrator' and run exactly: $adminStep"
    }
    throw
}

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
if (-not $task.Settings.Enabled) {
    throw "Scheduled Task '$taskName' was registered but is disabled."
}

Write-Host ''
Write-Host 'AI Project Manager Scheduler byl nainstalovan a povolen; nebyl spusten.' -ForegroundColor Green
Write-Host "Stav: $($task.State)"
Write-Host "Posledni spusteni: $($info.LastRunTime)"
Write-Host "Dalsi spusteni: $($info.NextRunTime)"
Write-Host "Python: $PythonExe"
