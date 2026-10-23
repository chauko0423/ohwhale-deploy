@echo off
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "OHWHALE_HOME=%~dp0.."
set "PYTHONPATH=%~dp0.."
"%~dp0..\.venv\Scripts\python.exe" -m ohwhale.deploy start
exit /b %errorlevel%
