@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0account_setup.ps1" -Action Prepare
echo.
echo Setup window closed. You may close this console.
pause
