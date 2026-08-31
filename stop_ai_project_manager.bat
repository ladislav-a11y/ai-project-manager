@echo off
setlocal

rem Safe operator stop: disable the PM Scheduled Task first, then drain only
rem the PM/watchdog process tree. The task remains registered for later use.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-ai-project-manager.ps1"
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
    echo AI Project Manager stop failed with exit code %exitCode%.
)
exit /b %exitCode%
