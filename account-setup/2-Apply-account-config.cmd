@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0account_setup.ps1" -Action Apply
echo.
echo Keep this window open if BLOCKED was reported.
pause
