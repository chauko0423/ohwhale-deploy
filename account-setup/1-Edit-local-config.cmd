@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0account_setup.ps1" -Action Prepare
echo.
echo Save and close both configuration files before running step 2.
pause
