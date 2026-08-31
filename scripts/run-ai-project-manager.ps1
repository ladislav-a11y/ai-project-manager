[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$SlackProbe,
    [string]$PythonExe = $env:AI_PM_PYTHON_EXE,
    [string]$OrchestratorRoot,
    [string]$StationAgentRoot,
    [int]$PollIntervalSeconds = 300,
    [string]$ScheduledTaskName = 'AI Project Manager Scheduler'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $OrchestratorRoot) {
    $OrchestratorRoot = Join-Path $workspaceRoot 'ai-orchestrator'
}
if (-not $StationAgentRoot) {
    $StationAgentRoot = Join-Path $workspaceRoot 'station-agent'
}
if (-not $PythonExe) {
    $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) {
        $PythonExe = $localPython
    }
    else {
        $pythonCommand = Get-Command python -CommandType Application -ErrorAction Stop
        if ($pythonCommand.Source -like '*\WindowsApps\python.exe') {
            throw "Python je pouze nefunkcni WindowsApps alias. Vytvor $localPython nebo nastav AI_PM_PYTHON_EXE na skutecny python.exe."
        }
        $PythonExe = $pythonCommand.Source
    }
}
$secretPath = Join-Path $projectRoot '.secrets\scheduler.clixml'
$runtimeDir = Join-Path $projectRoot 'runtime\scheduler'

if ($PollIntervalSeconds -lt 1) {
    throw 'PollIntervalSeconds must be at least 1.'
}

if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
    throw "Scheduler credential file is missing: $secretPath"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable is missing: $PythonExe"
}
$orchestratorPython = Join-Path $OrchestratorRoot '.venv\Scripts\python.exe'
$orchestratorScript = Join-Path $OrchestratorRoot 'orchestrator.py'
if (-not (Test-Path -LiteralPath $orchestratorPython -PathType Leaf)) {
    throw "Orchestrator Python executable is missing: $orchestratorPython"
}
if (-not (Test-Path -LiteralPath $orchestratorScript -PathType Leaf)) {
    throw "Orchestrator entrypoint is missing: $orchestratorScript"
}

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$logPath = Join-Path $runtimeDir ("scheduler-{0:yyyyMMdd}.log" -f (Get-Date))
Start-Transcript -Path $logPath -Append | Out-Null

function ConvertFrom-ProtectedString {
    param([Security.SecureString]$Value)
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

try {
    $credentials = Import-Clixml -LiteralPath $secretPath
    $env:TRELLO_KEY = ConvertFrom-ProtectedString $credentials.TrelloKey
    $env:TRELLO_TOKEN = ConvertFrom-ProtectedString $credentials.TrelloToken
    $env:TRELLO_BOARD_ID = ConvertFrom-ProtectedString $credentials.TrelloBoardId
    $env:SLACK_WEBHOOK_URL = ConvertFrom-ProtectedString $credentials.SlackWebhookUrl
    $env:AI_PM_SLACK_ENABLED = '1'

    # The locally installed Codex CLI does not reliably derive the Windows
    # home directory from the service/launcher environment.  Give it the
    # explicit per-user home so it can load its authenticated config without
    # hard-coding an account path or exposing credentials.
    if (-not $env:CODEX_HOME) {
        if (-not $env:USERPROFILE) {
            throw 'USERPROFILE is required to configure the local Codex CLI home.'
        }
        $env:CODEX_HOME = Join-Path $env:USERPROFILE '.codex'
    }

    $env:TRELLO_INBOX_LIST = 'INBOX / Nápady'
    # Inbox intake is enabled after the dedicated P5 governance/intake card
    # was prepared in Připraveno. The intake remains fail-closed on project
    # identity and never touches the personal Inbox.
    $env:AI_PM_ENABLE_INBOX = '1'
    # Ordered failover policy: Hermes is preferred when available, but is
    # hard-wired in ai-orchestrator to the Nous free model only.  The PM
    # provider names intentionally remain stable; ``hermes`` maps to the
    # production Hermes agent, while ``claude`` maps to claude-code.
    $env:AI_PM_PROVIDERS = 'hermes,gemini,antigravity,claude,codex'
    $env:AI_PM_PROVIDER_MODELS = (@{
        'hermes' = @('upstage/solar-pro4:free')
        'gemini' = @('gemini-2.5-flash')
        'claude' = @('claude-opus-4-1', 'claude-sonnet-4')
        'antigravity' = @('gemini-2.5-pro')
        # The ChatGPT-account Codex CLI rejects the generic gpt-5.6 alias;
        # use the concrete model configured for this installation.
        'codex' = @('gpt-5.6-luna')
    } | ConvertTo-Json -Compress)
    $env:AI_PM_POLL_INTERVAL_SECONDS = [string]$PollIntervalSeconds
    $env:AI_ORCHESTRATOR_TIMEOUT_SECONDS = '3600'
    $env:AI_ORCHESTRATOR_CMD = "`"$orchestratorPython`" `"$orchestratorScript`" autonomous --no-commit"
    $finalizeScript = Join-Path $OrchestratorRoot 'finalize.py'
    if (-not (Test-Path -LiteralPath $finalizeScript -PathType Leaf)) {
        throw "Orchestrator finalizer is missing: $finalizeScript"
    }
    $finalizeTestCommand = "$PythonExe -B -m pytest -q -p no:cacheprovider"
    $env:AI_ORCHESTRATOR_FINALIZE_CMD = "`"$orchestratorPython`" `"$finalizeScript`" --test-command `"$finalizeTestCommand`""
    $env:AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES = (@{
        'AI Project Manager' = 'https://github.com/ladislav-a11y/ai-project-manager.git'
    } | ConvertTo-Json -Compress)
    $finalizePaths = [ordered]@{
        'AI Project Manager' = @(
            'ai_project_manager/cli.py',
            'ai_project_manager/config.py',
            'ai_project_manager/daemon.py',
            'ai_project_manager/dod_validator.py',
            'ai_project_manager/inbox.py',
            'ai_project_manager/inbox_preparation.py',
            'ai_project_manager/orchestrator_runner.py',
            'ai_project_manager/trello_sync.py',
            'scripts/run-ai-project-manager.ps1',
            'scripts/verify_once_resolves_project_path.py',
            'tests/test_cli.py',
            'tests/test_config.py',
            'tests/test_daemon.py',
            'tests/test_dod_validator.py',
            'tests/test_handoff_e2e.py',
            'tests/test_inbox.py',
            'tests/test_orchestrator_runner.py',
            'tests/test_recovery_e2e.py',
            'tests/test_trello_sync.py'
        )
    }
    $env:AI_ORCHESTRATOR_FINALIZE_PATHS = $finalizePaths | ConvertTo-Json -Compress
    $env:AI_ORCHESTRATOR_SPEC_DIR = Join-Path $projectRoot 'runtime\specs'
    $env:AI_ORCHESTRATOR_OUTBOX_DIR = Join-Path $OrchestratorRoot 'outbox'
    $env:AI_PM_PROVIDER_STATE_PATH = Join-Path $projectRoot 'runtime\provider_state.json'
    # Keyed by each project's stable identity - either a "project_key"
    # Trello label on the card (recommended: works even when the card's
    # title never mentions the project at all, e.g. a generic work item
    # like "P5 - Izolace testovacich Slack notifikaci") or, as a legacy
    # fallback, an identity phrase that happens to appear in the title -
    # never the exact current card title, so a re-prioritized or reworded
    # card (title edits, priority changes) keeps resolving to the same
    # checkout without needing this map updated. See
    # ai_project_manager.orchestrator_runner.resolve_project_path.
    $projectPaths = [ordered]@{
        'Řídicí systém' = $projectRoot
        'AI Project Manager' = $projectRoot
        'ai-orchestrator' = $OrchestratorRoot
        'AI Orchestrator' = $OrchestratorRoot
        'Station Agent' = $StationAgentRoot
    }
    $env:AI_PM_PROJECT_PATHS = $projectPaths | ConvertTo-Json -Compress

    # One-time migration for real production cards created before the
    # project_key label existed: the board's pre-existing cards carry
    # only P0-P5 priority labels, no project identity label, and their
    # titles/descriptions often name no project at all (e.g. this one),
    # so there is no content signal to derive identity from - keyed by
    # immutable Trello card ID captured from the production board. Once applied
    # the card keeps its project_key label from then on regardless of
    # future title edits, so this entry only ever matters until that
    # first successful migration run. See
    # ai_project_manager.daemon._bootstrap_project_keys.
    $cardProjectKeys = [ordered]@{
        '6a8f0baf1332f1d03b972003' = 'AI Project Manager'
        '6a9537223372a7c011c2f651' = 'Station Agent'
        '6a954cb7a0650b2d68cbb51f' = 'AI Project Manager'
        '6a954f060373e6917e0a7291' = 'AI Project Manager'
    }
    $env:AI_PM_CARD_PROJECT_KEYS = $cardProjectKeys | ConvertTo-Json -Compress

    Set-Location -LiteralPath $projectRoot
    if ($SlackProbe) {
        # Isolated delivery proof: use the same protected production config,
        # but do not load Trello configuration in Python, run a PM tick, or
        # release the scheduler from HOLD. notify() returns a failing process
        # status unless Slack itself acknowledges the POST with HTTP 200.
        $arguments = @('-m', 'ai_project_manager', '--slack-probe', '--log-level', 'INFO')
        & $PythonExe @arguments
    }
    elseif ($Once) {
        # A single tick is already a safe, self-contained process (see
        # ai_project_manager/cli.py) - no supervising restart is needed.
        $arguments = @('-m', 'ai_project_manager', '--once', '--enable-inbox-intake', '--log-level', 'INFO')
        & $PythonExe @arguments
    }
    else {
        # Route the persistent loop through the supervising watchdog instead
        # of invoking ai_project_manager directly: when the PM detects its
        # own code changed underneath it (self_update.py) it exits with a
        # dedicated restart code rather than restarting itself, and only a
        # separate parent process picking up a fresh interpreter can safely
        # relaunch it. Without the watchdog here, that exit would just look
        # like a crashed scheduled task instead of a supervised restart. See
        # ai_project_manager/watchdog.py. $PythonExe is resolved above from
        # AI_PM_PYTHON_EXE / a local .venv / a real (non-WindowsApps-stub)
        # system python, so the watchdog itself runs the same way whether or
        # not a .venv is present on this machine.
        if ($ScheduledTaskName -eq 'AI Project Manager Scheduler') {
            $arguments = @(
                '-m', 'ai_project_manager.watchdog',
                '--repo-root', $projectRoot,
                '--scheduled-task-name', 'AI Project Manager Scheduler'
            )
        }
        else {
            $arguments = @(
                '-m', 'ai_project_manager.watchdog',
                '--repo-root', $projectRoot,
                '--scheduled-task-name', $ScheduledTaskName
            )
        }
        $arguments += @(
            '--mode', 'persistent',
            '--log-path', $logPath,
            '--log-level', 'INFO',
            '--', '--enable-inbox-intake', '--log-level', 'INFO'
        )
        & $PythonExe @arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "AI Project Manager exited with code $LASTEXITCODE"
    }
}
finally {
    $env:TRELLO_KEY = $null
    $env:TRELLO_TOKEN = $null
    $env:TRELLO_BOARD_ID = $null
    $env:SLACK_WEBHOOK_URL = $null
    $env:CODEX_HOME = $null
    $env:AI_PM_SLACK_ENABLED = $null
    $env:TRELLO_INBOX_LIST = $null
    $env:AI_PM_ENABLE_INBOX = $null
    $env:AI_PM_PROVIDERS = $null
    $env:AI_PM_PROVIDER_MODELS = $null
    $env:AI_PM_POLL_INTERVAL_SECONDS = $null
    $env:AI_ORCHESTRATOR_TIMEOUT_SECONDS = $null
    $env:AI_ORCHESTRATOR_CMD = $null
    $env:AI_ORCHESTRATOR_FINALIZE_CMD = $null
    $env:AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES = $null
    $env:AI_ORCHESTRATOR_FINALIZE_PATHS = $null
    $env:AI_ORCHESTRATOR_SPEC_DIR = $null
    $env:AI_ORCHESTRATOR_OUTBOX_DIR = $null
    $env:AI_PM_PROVIDER_STATE_PATH = $null
    $env:AI_PM_PROJECT_PATHS = $null
    $env:AI_PM_CARD_PROJECT_KEYS = $null
    $credentials = $null
    Stop-Transcript | Out-Null
}
