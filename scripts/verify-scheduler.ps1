[CmdletBinding()]
param(
    [string]$TaskName = 'AI Project Manager Scheduler',
    [switch]$WaitForNextTick,
    [int]$TimeoutSeconds = 420
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$schedulerDir = Join-Path $projectRoot 'runtime\scheduler'

if ($TimeoutSeconds -lt 1) {
    throw 'TimeoutSeconds must be at least 1.'
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
$watchdog = Get-CimInstance Win32_Process | Where-Object {
    # The verifier's own PowerShell command line contains the search text.
    # Only a Python process can be the actual watchdog.
    $_.Name -match '^python(w)?\.exe$' -and
    $_.CommandLine -like '*ai_project_manager.watchdog*' -and
    $_.CommandLine -like "*--scheduled-task-name*$TaskName*"
} | Select-Object -First 1
$log = Get-ChildItem -LiteralPath $schedulerDir -Filter 'scheduler-*.log' -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

if ($task.State -ne 'Running') {
    throw "Scheduled Task '$TaskName' is not running (state=$($task.State), result=$($info.LastTaskResult))."
}
if (-not $watchdog) {
    throw "Scheduled Task '$TaskName' has no live watchdog process."
}
if (-not $log) {
    throw "No scheduler transcript exists in $schedulerDir."
}

$tickPattern = 'scheduler tick finished:'
$watchdogStartPattern = '[AI Project Manager] Watchdog online:'
$startReceipt = Select-String -LiteralPath $log.FullName -SimpleMatch $watchdogStartPattern |
    Select-Object -Last 1
if (-not $startReceipt) {
    throw "The live log has no Slack receipt for the current watchdog start: $($log.FullName)"
}
$runStartLine = $startReceipt.LineNumber
$initialTickCount = @(Select-String -LiteralPath $log.FullName -SimpleMatch $tickPattern).Count
if ($initialTickCount -lt 1) {
    throw "The live log has no completed PM tick: $($log.FullName)"
}

if ($WaitForNextTick) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 5
        $tickCount = @(Select-String -LiteralPath $log.FullName -SimpleMatch $tickPattern).Count
    } while ($tickCount -le $initialTickCount -and (Get-Date) -lt $deadline)
    if ($tickCount -le $initialTickCount) {
        throw "No subsequent automatic PM tick appeared within $TimeoutSeconds seconds."
    }
    $sameWatchdog = Get-CimInstance Win32_Process -Filter "ProcessId = $($watchdog.ProcessId)" -ErrorAction SilentlyContinue
    if (-not $sameWatchdog) {
        throw "Watchdog PID $($watchdog.ProcessId) exited while waiting; the later tick is not proven to belong to the verified watchdog."
    }
}
else {
    $tickCount = $initialTickCount
}

$currentRunLines = Get-Content -LiteralPath $log.FullName | Select-Object -Skip ($runStartLine - 1)
$slackDelivered = @($currentRunLines | Select-String -SimpleMatch 'Slack notification delivered (HTTP 200)').Count -gt 0
$slackFailed = @($currentRunLines | Select-String -Pattern 'Slack notification (failed|error)').Count -gt 0
$slackState = if ($slackDelivered) { 'delivered-http-200' } elseif ($slackFailed) { 'failed' } else { 'unverified' }

[pscustomobject]@{
    TaskName = $TaskName
    TaskState = [string]$task.State
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    NextRunTime = $info.NextRunTime
    WatchdogPid = $watchdog.ProcessId
    LogPath = $log.FullName
    SlackState = $slackState
    CompletedTickCount = $tickCount
    SubsequentAutomaticTick = [bool]($WaitForNextTick -and $tickCount -gt $initialTickCount)
} | Format-List
