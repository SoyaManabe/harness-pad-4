@echo off
rem Windows: runs agentpadctl.py with the Python environment that install.py created.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0agentpadctl.py" %*
exit /b %ERRORLEVEL%
