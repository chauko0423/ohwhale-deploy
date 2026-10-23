@echo off
cd /d "%~dp0.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0device_test.ps1"
echo.
echo Finished. Keep this window open if an error was reported.
pause
