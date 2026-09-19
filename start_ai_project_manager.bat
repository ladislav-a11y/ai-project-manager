@echo off
setlocal

rem Persistent launcher for the PM watchdog. This starts the
rem already registered/configured production runner; it does not install a
rem Scheduled Task and it does not use the one-shot --once mode. The Task
rem Scheduler installer is scripts\install-scheduler.ps1.

rem scripts\run-ai-project-manager.ps1 is the single, already-tested place
rem that builds the PM's production configuration: Trello credentials from
rem the DPAPI-protected .secrets\scheduler.clixml (never hardcoded in either
rem script - see test_scheduler_scripts.py), the
rem AI_PM_PYTHON_EXE -> local .venv -> system-python interpreter fallback
rem chain, and the ai_project_manager.watchdog invocation itself. This
rem launcher used to duplicate a second, config-blind copy of that watchdog
rem invocation with none of the credential/env-var setup, so every
rem persistent start failed immediately with a missing Trello credential
rem configuration error. Delegating here instead of
rem re-implementing it keeps exactly one implementation of "how the PM gets
rem its production config" to keep correct and secret-safe.
rem
rem Started via `start` in its own console, detached from whatever invoked
rem this .bat (a Task Scheduler host, a Startup-folder shortcut, an
rem interactive shell): the previous same-console invocation meant closing
rem *that* invoking window sent a CTRL_CLOSE/CTRL_LOGOFF signal straight
rem down the shared console to the whole watchdog+PM process tree, killing
rem it with STATUS_CONTROL_C_EXIT (0xC000013A) even though nothing was
rem actually wrong with the PM itself.
rem
rem The detached console runs scripts\run-persistent-loop.bat rather than
rem run-ai-project-manager.ps1 directly: that plain cmd.exe loop relaunches
rem the PowerShell runner whenever it exits, so a later, unrelated
rem PowerShell console-host crash (see that script's own header comment for
rem the investigated incident) is self-healed instead of leaving the whole
rem PM tree down until a human notices and reruns this .bat by hand.
rem A stop must win any race with a fresh start: clear a stale stop-flag
rem left over from a previous run before launching, so this start is never
rem silently treated as instantly-stopped by an unrelated leftover file.
if exist "%~dp0runtime\pm_stop_requested.flag" del /f /q "%~dp0runtime\pm_stop_requested.flag" >nul 2>&1
start "AI Project Manager" /MIN cmd.exe /c ""%~dp0scripts\run-persistent-loop.bat""
