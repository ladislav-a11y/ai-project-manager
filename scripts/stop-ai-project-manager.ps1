[CmdletBinding()]
param(
    [string]$TaskName = 'AI Project Manager Scheduler',
    [int]$WaitSeconds = 10
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path.TrimEnd('\')

if ($WaitSeconds -lt 1) {
    throw 'WaitSeconds must be at least 1.'
}

# Disable before stopping so the repetition trigger cannot immediately launch
# a replacement while the process tree is being drained. Keep the task
# registered: the normal installer can enable/register it again later.
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "Scheduled Task '$TaskName' disabled and stop requested."
}
else {
    Write-Host "Scheduled Task '$TaskName' is not registered."
}

function Get-ProcessRole {
    param([string]$CommandLine)
    if ($CommandLine -like "*ai_project_manager.watchdog*") { return 'Watchdog' }
    if ($CommandLine -like "*run-ai-project-manager.ps1*") { return 'Launcher' }
    if ($CommandLine -like "*-m ai_project_manager*") { return 'PM-child' }
    if ($CommandLine -like "*orchestrator.py*" -or $CommandLine -like "*finalize.py*") { return 'AO-child' }
    return 'Other'
}

function Get-ProjectProcesses {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $projectPattern = [WildcardPattern]::new("*$projectRoot*")
    $ids = [Collections.Generic.HashSet[int]]::new()
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        if (-not $commandLine) { continue }
        $isRunner = $commandLine -like "*run-ai-project-manager.ps1*" -and $projectPattern.IsMatch($commandLine)
        $isWatchdog = $commandLine -like "*ai_project_manager.watchdog*" -and $projectPattern.IsMatch($commandLine)
        $isPm = $commandLine -like "*-m ai_project_manager*" -and $projectPattern.IsMatch($commandLine)
        if ($isRunner -or $isWatchdog -or $isPm) {
            [void]$ids.Add([int]$process.ProcessId)
        }
    }

    # Include only descendants of an identified PM runner/watchdog. This
    # catches the PM child and ai-orchestrator without touching other Python
    # processes owned by the user.
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $processes) {
            if ($ids.Contains([int]$process.ParentProcessId) -and $ids.Add([int]$process.ProcessId)) {
                $changed = $true
            }
        }
    }
    @($processes | Where-Object { $ids.Contains([int]$_.ProcessId) })
}

function Write-ProcessRoleLog {
    param([array]$Processes, [string]$Label)
    if ($Processes.Count -eq 0) {
        Write-Host "$Label`: no PM-owned process found."
        return
    }
    foreach ($process in $Processes) {
        $role = Get-ProcessRole -CommandLine ([string]$process.CommandLine)
        # Re-check against the real, current process table - a PID collected
        # a moment earlier can already be gone; never log a PID as evidence
        # of a running process without this fresh, real-state confirmation.
        $stillRunning = [bool](Get-Process -Id ([int]$process.ProcessId) -ErrorAction SilentlyContinue)
        Write-Host "$Label`: role=$role PID=$($process.ProcessId) parentPID=$($process.ParentProcessId) alive=$stillRunning"
    }
}

$processes = @(Get-ProjectProcesses)
Write-ProcessRoleLog -Processes $processes -Label 'discovered'
$initialDiscovered = $processes
while ($processes.Count -gt 0) {
    # Stop leaves first, then their parents. Avoid -Force initially so normal
    # process cleanup/finally blocks get a chance to run.
    $leafIds = @($processes | Where-Object {
        $processId = [int]$_.ProcessId
        -not ($processes | Where-Object { [int]$_.ParentProcessId -eq $processId })
    } | ForEach-Object { [int]$_.ProcessId })
    if ($leafIds.Count -eq 0) { $leafIds = @($processes | ForEach-Object { [int]$_.ProcessId }) }
    foreach ($processIdToStop in $leafIds) {
        Stop-Process -Id $processIdToStop -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        Start-Sleep -Milliseconds 250
        $processes = @(Get-ProjectProcesses)
    } while ($processes.Count -gt 0 -and (Get-Date) -lt $deadline)
}

if ($processes.Count -gt 0) {
    foreach ($process in $processes) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    $processes = @(Get-ProjectProcesses)
}

if ($processes.Count -gt 0) {
    Write-ProcessRoleLog -Processes $processes -Label 'still-running (termination failed)'
    throw "Could not stop all PM-owned processes: $((@($processes | ForEach-Object ProcessId) -join ', '))"
}

# Report the real, current termination result per originally discovered PID
# (role + a fresh Get-Process check) rather than trusting that "we asked it
# to stop" means it actually stopped.
foreach ($process in $initialDiscovered) {
    $role = Get-ProcessRole -CommandLine ([string]$process.CommandLine)
    $stillRunning = [bool](Get-Process -Id ([int]$process.ProcessId) -ErrorAction SilentlyContinue)
    Write-Host "terminated: role=$role PID=$($process.ProcessId) alive=$stillRunning"
}

Write-Host 'AI Project Manager a watchdog jsou zastavené; jiné Python procesy nebyly cílené.' -ForegroundColor Green
