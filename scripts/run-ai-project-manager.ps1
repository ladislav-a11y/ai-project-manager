[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$MaintainOnly,
    [string]$PythonExe = $env:AI_PM_PYTHON_EXE,
    [string]$OrchestratorRoot,
    [string]$StationAgentRoot,
    [int]$PollIntervalSeconds = 300,
    [string]$ScheduledTaskName = 'AI Project Manager Scheduler',
    [string]$ProviderOverride = ''
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
$inheritedProviderStatePath = $env:AI_PM_PROVIDER_STATE_PATH
$inheritedProviderModels = $env:AI_PM_PROVIDER_MODELS

if ($PollIntervalSeconds -lt 1) {
    throw 'PollIntervalSeconds must be at least 1.'
}
if ($Once -and $MaintainOnly) {
    throw 'Once and MaintainOnly are mutually exclusive.'
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

    # Built via [char] codepoints, not a literal diacritic, because Windows
    # PowerShell 5.1 parses a BOM-less .ps1 file (see AI_PROJECT_PROTOCOL.md
    # SS3, "UTF-8 bez BOM") using the system ANSI code page, not UTF-8 - a
    # literal 'a with acute' here is silently misread (its UTF-8 bytes 0xC3
    # 0xA1 decode as two separate CP1250 codepoints, U+0102 and U+02C7)
    # before the value ever reaches Trello or Python. Verified incident
    # (2026-09-03): this corrupted the value so process_inbox's own
    # list-name lookup returned no match, silently skipping Inbox intake.
    $env:TRELLO_INBOX_LIST = "INBOX / N$([char]0x00E1)pady"
    # New, unlabelled Inbox ideas are auto-prepared as isolated projects under
    # this approved workspace root; existing ambiguous project mappings still
    # fail closed.
    $env:AI_PM_PROJECTS_ROOT = $workspaceRoot
    # Inbox intake is enabled after the dedicated P5 governance/intake card
    # was prepared in Připraveno. The intake remains fail-closed on project
    # identity and never touches the personal Inbox.
    $env:AI_PM_ENABLE_INBOX = '1'
    # Ordered failover policy. The PM provider names intentionally remain
    # stable; ``claude`` maps to claude-code. Gemini was retired from PM
    # after its successful audit migration. Keep the provider list explicit;
    # retired providers must not be registered or selected.
    $env:AI_PM_PROVIDERS = 'groq,antigravity,claude,codex'
    if ($ProviderOverride.Trim()) {
        if ($ProviderOverride.Trim().ToLowerInvariant() -eq 'gemini') {
            throw "Gemini je z PM vy$([char]0x0159)azen; pou$([char]0x017E)ijte jin$([char]0x00E9)ho providera."
        }
        if ($ProviderOverride.Trim().ToLowerInvariant() -eq 'hermes') {
            throw "Hermes provider is removed; use a supported provider."
        }
        $env:AI_PM_PROVIDERS = $ProviderOverride.Trim()
    }
    # Preserve an operator-configured legacy provider -> model catalog for
    # backward-compatible state/diagnostics. Production PM dispatch passes
    # only the provider allowlist; AO owns model selection.
    if ([string]::IsNullOrWhiteSpace($inheritedProviderModels)) {
        $env:AI_PM_PROVIDER_MODELS = '{}'
    }
    else {
        $env:AI_PM_PROVIDER_MODELS = $inheritedProviderModels
    }
    $env:AI_PM_POLL_INTERVAL_SECONDS = [string]$PollIntervalSeconds
    # pytest basetemp and the scheduler's own between-tick cleanup must both
    # target the same external, user-writable root outside every checkout -
    # never the checkout itself (see ai_project_manager/test_artifact_paths.py
    # and tests/conftest.py: a .pytest-basetemp-* directory left in the repo
    # tree is exactly the disposable, tool-owned artifact WORKFLOW.md's
    # fail-closed test artifact lifecycle forbids). Cleanup itself still only
    # ever removes direct, expired .pytest-basetemp-* children of this root
    # and only after a scheduler tick has completed.
    $artifactRootCandidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $artifactRootCandidates += Join-Path $env:LOCALAPPDATA 'AIProjectManager\pytest'
    }
    $artifactRootCandidates += Join-Path ([System.IO.Path]::GetTempPath()) 'AIProjectManager\pytest'
    $testArtifactRoot = $null
    foreach ($candidate in $artifactRootCandidates) {
        $probe = $null
        try {
            New-Item -ItemType Directory -Path $candidate -Force -ErrorAction Stop | Out-Null
            $probe = Join-Path $candidate ('.write-probe-' + [guid]::NewGuid().ToString('N'))
            [System.IO.File]::WriteAllText($probe, 'probe')
            Remove-Item -LiteralPath $probe -Force -ErrorAction Stop
            $testArtifactRoot = $candidate
            break
        }
        catch {
            if ($probe -and (Test-Path -LiteralPath $probe)) {
                Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
            }
            continue
        }
    }
    if ($null -eq $testArtifactRoot) {
        throw 'No writable external AI project manager pytest artifact root is available.'
    }
    $env:AI_PM_TEST_ARTIFACT_ROOT = $testArtifactRoot
    $env:AI_PM_ARTIFACT_CLEANUP_ROOT = $testArtifactRoot
    # AO audit and finalization child processes must use the same external
    # pytest root as PM.  Without an explicit basetemp, the managed Windows
    # runtime can redirect pytest into its inaccessible sandbox temp tree.
    $pytestBasetemp = Join-Path $testArtifactRoot ('.pytest-basetemp-pm-' + [guid]::NewGuid().ToString('N'))
    $pytestAddoptsSource = $env:PYTEST_ADDOPTS
    if ($null -eq $pytestAddoptsSource) {
        $pytestAddoptsSource = ''
    }
    $pytestAddopts = [regex]::Replace(
        $pytestAddoptsSource,
        '(?i)(^|\s)--basetemp(?:=\S+|\s+\S+)',
        ' '
    ).Trim()
    $env:PYTEST_ADDOPTS = "$pytestAddopts --basetemp=$pytestBasetemp".Trim()
    $env:AI_PM_ARTIFACT_RETENTION_HOURS = '24'
    $env:AI_ORCHESTRATOR_TIMEOUT_SECONDS = '3600'
    # Used by PM's lifecycle notifier to reuse AO's existing Slack bot token.
    # Keep the path explicit because the scheduler may start with a different
    # current directory than this checkout.
    $env:AI_ORCHESTRATOR_ROOT = $OrchestratorRoot
    # Controller-owned test commands run in a child process.  Git's
    # safe.directory configuration is otherwise scoped to the launcher
    # checkout by the managed runtime, so the child can reject the target
    # repository as dubious ownership even though PM itself can read it.
    $gitSafeDirectories = @(
        $projectRoot, "$projectRoot/*",
        $OrchestratorRoot, "$OrchestratorRoot/*",
        $StationAgentRoot, "$StationAgentRoot/*"
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $env:GIT_CONFIG_COUNT = [string]$gitSafeDirectories.Count
    for ($gitSafeIndex = 0; $gitSafeIndex -lt $gitSafeDirectories.Count; $gitSafeIndex++) {
        Set-Item -Path "Env:GIT_CONFIG_KEY_$gitSafeIndex" -Value 'safe.directory'
        Set-Item -Path "Env:GIT_CONFIG_VALUE_$gitSafeIndex" -Value $gitSafeDirectories[$gitSafeIndex]
    }
    $env:AI_ORCHESTRATOR_CMD = "`"$orchestratorPython`" `"$orchestratorScript`" autonomous --no-commit"
    $finalizeScript = Join-Path $OrchestratorRoot 'finalize.py'
    if (-not (Test-Path -LiteralPath $finalizeScript -PathType Leaf)) {
        throw "Orchestrator finalizer is missing: $finalizeScript"
    }
    # No --test-command here: that would fix the test interpreter to
    # $PythonExe (this PM checkout's own venv) for every registered
    # project's finalization, not just this one. finalize.py's own
    # per-project auto-detection (orchestrator.autonomous._detect_test_command)
    # already resolves each target project's own .venv interpreter from its
    # --project path, so leaving --test-command unset is what generalizes
    # correctly across projects instead of hardcoding today's project here.
    $env:AI_ORCHESTRATOR_FINALIZE_CMD = "`"$orchestratorPython`" `"$finalizeScript`""
    $env:AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES = (@{
        'AI Project Manager' = 'https://github.com/ladislav-a11y/ai-project-manager.git'
        'Station Agent' = 'https://github.com/ladislav-a11y/station-agent.git'
        'AI Orchestrator' = 'https://github.com/ladislav-a11y/ai-orchestrator.git'
        'ai-orchestrator' = 'https://github.com/ladislav-a11y/ai-orchestrator.git'
    } | ConvertTo-Json -Compress)
    # Finalization derives the current-task scope from the persisted dirty-path
    # baseline. This keeps the launcher independent of a stale per-file list;
    # AO still rejects transient artifacts and never stages baseline paths.
    $env:AI_ORCHESTRATOR_SPEC_DIR = Join-Path $projectRoot 'runtime\specs'
    $env:AI_ORCHESTRATOR_OUTBOX_DIR = Join-Path $OrchestratorRoot 'outbox'
    # Preserve an explicitly supplied state path so a guarded diagnostic tick
    # can use an isolated file and cannot mutate production provider health.
    if ([string]::IsNullOrWhiteSpace($inheritedProviderStatePath)) {
        $env:AI_PM_PROVIDER_STATE_PATH = Join-Path $projectRoot 'runtime\provider_state.json'
    }
    else {
        $env:AI_PM_PROVIDER_STATE_PATH = $inheritedProviderStatePath
    }
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
        "$([char]0x0158)$([char]0x00ED)dic$([char]0x00ED) syst$([char]0x00E9)m" = $projectRoot
        'AI Project Manager' = $projectRoot
        'ai-orchestrator' = $OrchestratorRoot
        'AI Orchestrator' = $OrchestratorRoot
        'Station Agent' = $StationAgentRoot
        # This Inbox project has a durable generated checkout. Keep the
        # mapping explicit so a split child cannot be guessed from its title.
        'Bazar Scout + multi-inzerce [Inbox 6a89edf4]' = Join-Path $workspaceRoot 'bazar-scout-multi-inzerce-inbox-6a89edf4'
        # The source Inbox card named an explicit working directory
        # (D:\cw_dekoder) in free text, but the intake ran before
        # inbox_preparation.py learned to honor a declared "Pracovni
        # adresar:" line, so it auto-generated its own checkout instead
        # (D:\orchestrator\cw-dekoder-v1-inbox-6a9b98c8). That work was first
        # moved to D:\cw_dekoder, but that path sits outside the AO sandbox
        # (config.yaml workspace_root, default D:\orchestrator - AO and
        # ClaudeCodeAgent may never touch anything outside it), so it was
        # relocated once more to D:\orchestrator\cw_dekoder (git history
        # preserved). This mapping repoints every remaining subtask
        # (2/11-11/11) of the same Inbox idea there.
        'cw dekoder v1 [Inbox 6a9b98c8]' = 'D:\orchestrator\cw_dekoder'
        # The Gmail agent Inbox card declared its durable working directory
        # explicitly. Keep the split children bound to that checkout rather
        # than allowing a missing project identity to fail closed at dispatch.
        'Gmail agent [Inbox 6aaa9ebe]' = Join-Path $workspaceRoot 'gmail-agent-inbox-6aaa9ebe'
    }
    $env:AI_PM_PROJECT_PATHS = $projectPaths | ConvertTo-Json -Compress
    # Must match ai-orchestrator's own config.yaml workspace_root (default:
    # its parent directory, i.e. this same $workspaceRoot) so PM can reject
    # a declared Inbox working directory outside AO's sandbox at intake
    # instead of only discovering the rejection later, at AO dispatch time.
    $env:AI_ORCHESTRATOR_WORKSPACE_ROOT = $workspaceRoot
    # Persisted as a generated checkout's own local git identity right after
    # git init, so AO's later finalization commit (plain "git commit", no -c
    # override) does not fail with "Author identity unknown" (see incident:
    # card P3.01, cw dekoder v1). Matches the identity already configured by
    # hand in every pre-existing checkout (AI Project Manager, Station
    # Agent, AI Orchestrator).
    $env:AI_PM_GIT_USER_NAME = 'Ladislav Kocandrle'
    $env:AI_PM_GIT_USER_EMAIL = 'ladislav@kocandrle.com'

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
        # The source Inbox card was historically created without its stable
        # project label. Bind its already-split children to the real Station
        # Agent checkout instead of allowing a generated scratch identity.
        '6a96f3a589ea0531cfc12958' = 'Station Agent'
        '6a96541d04fe14b38ccf96db' = 'Station Agent'
        '6a9666115f87e75a5c37742d' = 'Station Agent'
        '6a954cb7a0650b2d68cbb51f' = 'AI Project Manager'
        '6a954f060373e6917e0a7291' = 'AI Project Manager'
        # Gmail agent [Inbox 6aaa9ebe] split children. These immutable card
        # IDs are the one-time migration source; the project_key label then
        # remains durable on each card regardless of title edits.
        '6aab9d2b2a54ccd7876ceee0' = 'Gmail agent [Inbox 6aaa9ebe]'
        '6aab9d2750e83ebbe9d5534c' = 'Gmail agent [Inbox 6aaa9ebe]'
        '6aab9d244e46db0c9b1cbff6' = 'Gmail agent [Inbox 6aaa9ebe]'
        '6aab9d2df934b06f1c8039b3' = 'Gmail agent [Inbox 6aaa9ebe]'
        '6aab9d203791b19da1bd3612' = 'Gmail agent [Inbox 6aaa9ebe]'
        '6aab9d31abc8c640b3068655' = 'Gmail agent [Inbox 6aaa9ebe]'
    }
    $env:AI_PM_CARD_PROJECT_KEYS = $cardProjectKeys | ConvertTo-Json -Compress

    Set-Location -LiteralPath $projectRoot
    if ($MaintainOnly) {
        # Card Contract maintenance is a live Trello repair pass only. It
        # never dispatches a project or touches provider state.
        $arguments = @('-m', 'ai_project_manager', '--maintain-only', '--log-level', 'INFO')
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
    $env:CODEX_HOME = $null
    $env:TRELLO_INBOX_LIST = $null
    $env:AI_PM_ENABLE_INBOX = $null
    $env:AI_PM_PROVIDERS = $null
    $env:AI_PM_PROVIDER_MODELS = $inheritedProviderModels
    $env:PYTEST_ADDOPTS = $null
    $env:AI_PM_POLL_INTERVAL_SECONDS = $null
    $env:AI_PM_TEST_ARTIFACT_ROOT = $null
    $env:AI_PM_ARTIFACT_CLEANUP_ROOT = $null
    $env:AI_PM_ARTIFACT_RETENTION_HOURS = $null
    $env:AI_ORCHESTRATOR_TIMEOUT_SECONDS = $null
    $env:AI_ORCHESTRATOR_CMD = $null
    $env:AI_ORCHESTRATOR_FINALIZE_CMD = $null
    $env:AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES = $null
    $env:AI_ORCHESTRATOR_SPEC_DIR = $null
    $env:AI_ORCHESTRATOR_OUTBOX_DIR = $null
    if ([string]::IsNullOrWhiteSpace($inheritedProviderStatePath)) {
        $env:AI_PM_PROVIDER_STATE_PATH = $null
    }
    else {
        $env:AI_PM_PROVIDER_STATE_PATH = $inheritedProviderStatePath
    }
    $env:AI_PM_PROJECT_PATHS = $null
    $env:AI_PM_PROJECTS_ROOT = $null
    $env:AI_ORCHESTRATOR_ROOT = $null
    $env:AI_ORCHESTRATOR_WORKSPACE_ROOT = $null
    for ($gitSafeIndex = 0; $gitSafeIndex -lt 6; $gitSafeIndex++) {
        Remove-Item -Path "Env:GIT_CONFIG_KEY_$gitSafeIndex" -ErrorAction SilentlyContinue
        Remove-Item -Path "Env:GIT_CONFIG_VALUE_$gitSafeIndex" -ErrorAction SilentlyContinue
    }
    $env:GIT_CONFIG_COUNT = $null
    $env:AI_PM_GIT_USER_NAME = $null
    $env:AI_PM_GIT_USER_EMAIL = $null
    $env:AI_PM_CARD_PROJECT_KEYS = $null
    $credentials = $null
    Stop-Transcript | Out-Null
}
