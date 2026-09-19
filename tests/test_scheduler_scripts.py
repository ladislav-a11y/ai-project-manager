"""Static contract checks for the Windows Task Scheduler entrypoints."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


def test_installer_resolves_paths_relative_to_its_own_location() -> None:
    source = (SCRIPTS / "install-scheduler.ps1").read_text(encoding="utf-8")

    assert "$projectRoot = Split-Path -Parent $PSScriptRoot" in source
    assert "Join-Path $PSScriptRoot 'run-ai-project-manager.ps1'" in source
    assert "D:\\orchestrator\\ai-project-manager" not in source


def test_runner_is_relocatable_and_python_is_configurable() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "$projectRoot = Split-Path -Parent $PSScriptRoot" in source
    assert "[string]$PythonExe = $env:AI_PM_PYTHON_EXE" in source
    assert "Get-Command python" in source
    assert "C:\\Users\\Admin" not in source
    assert '"D:/orchestrator/' not in source


def test_runner_builds_project_paths_from_resolved_checkout_parameters() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    # Built from [char] codepoints, not a literal diacritic - see
    # test_runner_preserves_configured_czech_trello_names_as_utf8 for why.
    assert (
        "\"$([char]0x0158)$([char]0x00ED)dic$([char]0x00ED) syst$([char]0x00E9)m\" "
        "= $projectRoot"
    ) in source
    assert "'AI Project Manager' = $projectRoot" in source
    assert "'ai-orchestrator' = $OrchestratorRoot" in source
    assert "'AI Orchestrator' = $OrchestratorRoot" in source
    assert "'Station Agent' = $StationAgentRoot" in source
    assert "$projectPaths | ConvertTo-Json -Compress" in source


def test_runner_seeds_card_project_key_migration_for_the_known_real_production_card() -> None:
    """The live board's pre-existing "P5 - Izolace testovacich Slack
    notifikaci" card has only a P5 priority label and no project identity
    label or title/content trace of which project it belongs to - see
    ai_project_manager.daemon._bootstrap_project_keys. This one-time
    migration entry is what lets a real ``--once`` run resolve it without
    an AI_PM_PROJECT_PATHS exact-title override."""
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "'6a8f0baf1332f1d03b972003' = 'AI Project Manager'" in source
    assert "'6a9537223372a7c011c2f651' = 'Station Agent'" in source
    assert "'6a96f3a589ea0531cfc12958' = 'Station Agent'" in source
    assert "'6a954cb7a0650b2d68cbb51f' = 'AI Project Manager'" in source
    assert "'6a954f060373e6917e0a7291' = 'AI Project Manager'" in source
    assert "$cardProjectKeys | ConvertTo-Json -Compress" in source
    assert "$env:AI_PM_CARD_PROJECT_KEYS = $cardProjectKeys | ConvertTo-Json -Compress" in source


def test_runner_delegates_model_selection_to_providers() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "$env:AI_PM_PROVIDERS = 'groq,antigravity,claude,codex'" in source
    assert "$env:AI_PM_PROVIDER_MODELS = '{}'" in source
    assert "Preserve an operator-configured legacy provider" in source


def test_runner_does_not_hardcode_ai_project_manager_finalize_paths() -> None:
    """AI Project Manager finalization must use the controller's dynamic dirty-path
    scope. A launcher-level per-file allowlist goes stale whenever a legitimate
    task edits a new file (regression: README.md on 2026-09-07)."""
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "$finalizePaths = [ordered]@{" not in source
    assert "$env:AI_ORCHESTRATOR_FINALIZE_PATHS = $finalizePaths" not in source


def test_runner_authorizes_push_remotes_for_ai_project_manager_and_station_agent() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "'AI Project Manager' = 'https://github.com/ladislav-a11y/ai-project-manager.git'" in source
    assert "'Station Agent' = 'https://github.com/ladislav-a11y/station-agent.git'" in source


def test_runner_rejects_retired_providers_via_override() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert (
        "throw \"Gemini je z PM vy$([char]0x0159)azen; "
        "pou$([char]0x017E)ijte jin$([char]0x00E9)ho providera.\""
    ) in source
    assert 'throw "Hermes provider is removed; use a supported provider."' in source


def test_runner_preserves_configured_czech_trello_names_as_utf8() -> None:
    """Windows PowerShell 5.1 parses a BOM-less .ps1 file (this project's
    convention, see AI_PROJECT_PROTOCOL.md SS3) in the system ANSI code page,
    not UTF-8 - a *literal* diacritic in the script source is silently
    misread before the value ever reaches Trello or Python (verified
    incident 2026-09-03: this corrupted TRELLO_INBOX_LIST and silently
    disabled Inbox intake, even though the file's own UTF-8 bytes, read by
    Python here, were always correct). Every functionally significant
    diacritic string must therefore be built at runtime from [char]
    codepoints instead, which this test enforces directly rather than
    trusting the file's on-disk encoding."""
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert '$env:TRELLO_INBOX_LIST = "INBOX / N$([char]0x00E1)pady"' in source
    assert (
        "\"$([char]0x0158)$([char]0x00ED)dic$([char]0x00ED) syst$([char]0x00E9)m\" "
        "= $projectRoot"
    ) in source
    # The two exact-match asserts above already pin every character of both
    # assignments; a reintroduced literal diacritic would fail them directly.
    # These common mojibake markers indicate that UTF-8 was decoded and
    # re-encoded through a legacy Windows code page. Exact Trello/project
    # name matching would then silently stop working.
    assert "Ă" not in source
    assert "Ĺ" not in source


def test_runner_enables_governed_main_board_inbox_intake() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert '$env:TRELLO_INBOX_LIST = "INBOX / N$([char]0x00E1)pady"' in source
    assert "$env:AI_PM_ENABLE_INBOX = '1'" in source
    assert "personal Inbox" in source


def test_runner_validates_orchestrator_before_importing_credentials() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    validation = source.index("Orchestrator entrypoint is missing")
    credential_import = source.index("Import-Clixml")
    assert validation < credential_import


def test_runner_clears_every_environment_variable_it_sets() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")
    before_finally, cleanup = source.rsplit("finally {", maxsplit=1)

    assigned = {
        line.split("=", maxsplit=1)[0].strip()
        for line in before_finally.splitlines()
        if line.strip().startswith("$env:") and "=" in line
    }
    cleared = {
        line.split("=", maxsplit=1)[0].strip()
        for line in cleanup.splitlines()
        if line.strip().startswith("$env:") and line.strip().endswith("= $null")
    }
    # Provider model configuration is special: it is restored to the caller's
    # inherited value instead of always being cleared.
    assert cleared == assigned - {"$env:AI_PM_PROVIDER_MODELS"}
    assert "$env:AI_PM_PROVIDER_STATE_PATH" in cleanup


def test_runner_preserves_an_explicit_provider_state_path_for_isolated_probe() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "$inheritedProviderStatePath = $env:AI_PM_PROVIDER_STATE_PATH" in source
    assert "can use an isolated file" in source
    assert "$env:AI_PM_PROVIDER_STATE_PATH = $inheritedProviderStatePath" in source


def test_runner_routes_the_persistent_loop_through_the_watchdog() -> None:
    """The persistent (non-`-Once`) loop must be launched via
    ``ai_project_manager.watchdog``, not ``ai_project_manager`` directly -
    otherwise a self-update restart request (RESTART_REQUIRED_EXIT_CODE)
    just looks like a crashed scheduled task instead of getting a
    supervised restart with a fresh interpreter. A single `--once` tick has
    no self-update restart to supervise, so it is launched directly."""
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "'-m', 'ai_project_manager.watchdog'" in source
    assert "'--repo-root', $projectRoot" in source
    assert "'--scheduled-task-name', 'AI Project Manager Scheduler'" in source
    assert "'--mode', 'persistent'" in source
    assert "'--log-path', $logPath" in source
    assert "'-m', 'ai_project_manager', '--once', '--enable-inbox-intake', '--log-level', 'INFO'" in source


def test_persistent_bat_launcher_shares_config_with_the_runner_script() -> None:
    """``start_ai_project_manager.bat`` is the actual on-machine entrypoint
    for the long-running, watchdog-supervised PM. A prior version invoked
    ``ai_project_manager.watchdog`` directly with none of the production
    configuration (Trello credentials, providers, project paths) that
    ``scripts/run-ai-project-manager.ps1`` loads - every persistent start
    then failed immediately with missing Trello configuration, since
    ``config.load_config`` requires those variables
    (see ai_project_manager/config.py). It must instead delegate to the
    runner script - the single, already-tested place that loads
    ``.secrets/scheduler.clixml`` and resolves the Python interpreter - so
    there is exactly one implementation of "how the PM gets its production
    config" to keep correct."""
    source = (PROJECT_ROOT / "start_ai_project_manager.bat").read_text(encoding="utf-8")

    assert "run-ai-project-manager.ps1" in source
    # The persistent (non-Once) form must be launched, not the single-tick
    # `-Once` form - this .bat is the always-on entrypoint.
    assert "-Once" not in source
    # The .bat must never re-implement credential loading or interpreter
    # resolution itself - that duplication is exactly what silently drifted
    # out of sync and left the persistent start with no configuration.
    assert "Import-Clixml" not in source
    assert ".venv\\Scripts\\python.exe" not in source
    assert "where python" not in source


def test_persistent_bat_launcher_detaches_from_the_invoking_console() -> None:
    """Investigated cause of the observed 0xC000013A (STATUS_CONTROL_C_EXIT)
    crash: the previous version ran the watchdog+PM process tree directly in
    whatever console invoked this .bat (a Task Scheduler host, a
    Startup-folder shortcut, an interactive shell). Closing that invoking
    window sends a CTRL_CLOSE/CTRL_LOGOFF signal down the shared console to
    every process attached to it, terminating the long-running tree even
    though nothing was wrong with the PM itself. Launching via `start` opens
    a new, detached console for the supervisor loop so the invoking window
    can close without killing it."""
    source = (PROJECT_ROOT / "start_ai_project_manager.bat").read_text(encoding="utf-8")

    # The literal command line: `start` launching the supervisor loop in a
    # new, titled console - not merely both substrings appearing somewhere
    # (e.g. inside an explanatory comment).
    assert 'start "AI Project Manager" /MIN cmd.exe /c ""%~dp0scripts\\run-persistent-loop.bat""' in source


def test_persistent_bat_launcher_clears_stale_stop_flag_before_starting() -> None:
    """A stop-flag left over from a previous stop must never make a brand
    new start look like it was instantly stopped (see
    scripts\\run-persistent-loop.bat and scripts\\stop-ai-project-manager.ps1
    for the cooperating halves of this contract)."""
    source = (PROJECT_ROOT / "start_ai_project_manager.bat").read_text(encoding="utf-8")

    assert "runtime\\pm_stop_requested.flag" in source
    assert "del /f /q" in source


def test_persistent_bat_launcher_is_relocatable() -> None:
    source = (PROJECT_ROOT / "start_ai_project_manager.bat").read_text(encoding="utf-8")

    assert "D:\\orchestrator\\ai-project-manager" not in source
    assert "%~dp0" in source


def test_persistent_loop_relaunches_the_runner_and_is_relocatable() -> None:
    """Investigated cause (2026-09-17/18): Windows PowerShell 5.1's console
    host can sporadically die hours into a run with a benign-but-fatal
    "occurred while setting the console window title" Win32 error inside
    run-ai-project-manager.ps1's Start-Transcript call, with no reliable
    PowerShell-level try/catch able to intercept it (it surfaces as an
    uncaught top-level "PS>TerminatingError()"). This plain cmd.exe loop -
    which never calls Start-Transcript itself, and has never exhibited this
    failure class - relaunches the runner whenever it exits instead of
    leaving the whole PM tree down until a human notices."""
    source = (PROJECT_ROOT / "scripts" / "run-persistent-loop.bat").read_text(encoding="utf-8")

    assert "D:\\orchestrator\\ai-project-manager" not in source
    assert "%~dp0" in source
    assert (
        'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File '
        '"%projectRoot%\\scripts\\run-ai-project-manager.ps1"'
    ) in source
    assert ":loop" in source
    assert "goto loop" in source


def test_persistent_loop_stops_on_the_stop_flag_instead_of_relaunching() -> None:
    """Must check the stop-flag both before launching a fresh runner and
    immediately after the previous one exits - an operator stop mid-run
    (which kills the runner) must not be mistaken for a crash and silently
    undone by an automatic restart."""
    source = (PROJECT_ROOT / "scripts" / "run-persistent-loop.bat").read_text(encoding="utf-8")

    assert source.count('if exist "%stopFlag%"') >= 2
    assert 'set "stopFlag=%projectRoot%\\runtime\\pm_stop_requested.flag"' in source
    assert "exit /b 0" in source


def test_safe_stop_launcher_delegates_to_scoped_stop_script() -> None:
    source = (PROJECT_ROOT / "stop_ai_project_manager.bat").read_text(encoding="utf-8")

    assert '"%~dp0scripts\\stop-ai-project-manager.ps1"' in source
    assert "Stop-Process" not in source


def test_safe_stop_script_disables_task_and_scopes_process_tree() -> None:
    source = (SCRIPTS / "stop-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "Disable-ScheduledTask -TaskName $TaskName" in source
    assert "Stop-ScheduledTask -TaskName $TaskName" in source
    assert "Get-CimInstance Win32_Process" in source
    assert "run-ai-project-manager.ps1" in source
    assert "ai_project_manager.watchdog" in source
    assert "*-m ai_project_manager*" in source


def test_safe_stop_script_writes_stop_flag_before_touching_the_task_or_processes() -> None:
    """scripts\\run-persistent-loop.bat only stops relaunching the runner once
    it observes this file, so it must exist before anything else here can
    even begin draining the tree - otherwise the loop can restart the
    runner in the window between the kill and the flag being written."""
    source = (SCRIPTS / "stop-ai-project-manager.ps1").read_text(encoding="utf-8")

    flag_write = source.index("pm_stop_requested.flag")
    task_disable = source.index("Disable-ScheduledTask -TaskName $TaskName")
    process_kill = source.index("Stop-Process -Id $processIdToStop")
    assert flag_write < task_disable < process_kill


def test_safe_stop_script_also_scopes_the_supervisor_loop() -> None:
    """The self-healing cmd.exe loop (run-persistent-loop.bat) that
    start_ai_project_manager.bat starts must be recognized and torn down
    too, or it would simply relaunch a fresh runner moments after this
    script kills the current one."""
    source = (SCRIPTS / "stop-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "run-persistent-loop.bat" in source


def test_installer_description_uses_configured_interval() -> None:
    source = (SCRIPTS / "install-scheduler.ps1").read_text(encoding="utf-8")

    assert 'every $IntervalMinutes minutes' in source
    assert "every 5 minutes" not in source


def test_installer_registers_enabled_persistent_watchdog_without_starting_it() -> None:
    source = (SCRIPTS / "install-scheduler.ps1").read_text(encoding="utf-8")

    action = source[source.index("$action = New-ScheduledTaskAction"):source.index("$triggers =")]
    assert "-Once" not in action
    assert "-PollIntervalSeconds $pollIntervalSeconds" in action
    assert "New-ScheduledTaskTrigger -AtLogOn" in source
    assert "New-ScheduledTaskTrigger -AtStartup" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in source
    assert "-StartWhenAvailable" in source
    assert "Start-ScheduledTask" not in source
    assert "if (-not $task.Settings.Enabled)" in source
    assert '-PythonExe `"$PythonExe`"' in source


def test_runner_poll_interval_is_explicit_and_validated() -> None:
    source = (SCRIPTS / "run-ai-project-manager.ps1").read_text(encoding="utf-8")

    assert "[int]$PollIntervalSeconds = 300" in source
    assert "$PollIntervalSeconds -lt 1" in source
    assert "$env:AI_PM_POLL_INTERVAL_SECONDS = [string]$PollIntervalSeconds" in source


def test_live_verifier_requires_task_watchdog_log_and_next_tick() -> None:
    source = (SCRIPTS / "verify-scheduler.ps1").read_text(encoding="utf-8")

    assert "Get-ScheduledTask -TaskName $TaskName" in source
    assert "ai_project_manager.watchdog" in source
    assert "scheduler tick finished:" in source
    assert "No subsequent automatic PM tick" in source
