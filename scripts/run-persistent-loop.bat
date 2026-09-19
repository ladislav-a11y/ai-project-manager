@echo off
setlocal

rem Self-healing supervisor for the persistent PM launcher.
rem
rem Investigated cause (2026-09-17/18): Windows PowerShell 5.1's console
rem host can sporadically die hours into a run with a benign-but-fatal
rem "The Win32 internal error ... occurred while setting the console window
rem title" error, deep inside run-ai-project-manager.ps1's Start-Transcript
rem call, once its minimized console is disrupted (session lock/RDP
rem disconnect and similar). That crash has no PowerShell-level try/catch
rem that can reliably intercept it (it surfaces as an uncaught top-level
rem "PS>TerminatingError()"), so the fix lives one process up: this plain
rem cmd.exe loop - which never calls Start-Transcript or touches
rem $Host.UI.RawUI at all, and has never exhibited this failure class in
rem the incident logs - notices whenever run-ai-project-manager.ps1 exits,
rem for any reason, and relaunches it, instead of leaving the whole PM tree
rem down until a human happens to notice.
rem
rem Cooperates with stop_ai_project_manager.bat: that script (via
rem scripts\stop-ai-project-manager.ps1) writes the stop-flag file below
rem as its very first action, before it kills anything. This loop checks
rem for that flag both before launching and immediately after the child
rem exits, so an intentional stop is never mistaken for a crash and
rem silently undone by an automatic restart.

set "projectRoot=%~dp0.."
set "stopFlag=%projectRoot%\runtime\pm_stop_requested.flag"

:loop
if exist "%stopFlag%" (
    del /f /q "%stopFlag%" >nul 2>&1
    exit /b 0
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%projectRoot%\scripts\run-ai-project-manager.ps1"

if exist "%stopFlag%" (
    del /f /q "%stopFlag%" >nul 2>&1
    exit /b 0
)

rem Brief pause so a run that fails immediately (e.g. a real, persistent
rem configuration error) cannot spin the loop as a tight, log-flooding
rem crash loop; a transient session-console failure hours into a healthy
rem run is unaffected by this small delay.
timeout /t 10 /nobreak >nul
goto loop
