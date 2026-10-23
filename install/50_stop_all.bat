@echo off
set "PYTHONUTF8=1"
set "OHWHALE_HOME=%~dp0.."
set "PYTHONPATH=%~dp0.."
"%~dp0..\.venv\Scripts\python.exe" -m ohwhale.deploy stop
exit /b %errorlevel%
