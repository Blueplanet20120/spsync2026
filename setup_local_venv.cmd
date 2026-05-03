@echo off
cd /d "%~dp0"
where py >nul 2>&1 && py -3 "tools\setup_local_venv.py" %* && exit /b %ERRORLEVEL%
where python >nul 2>&1 && python "tools\setup_local_venv.py" %* && exit /b %ERRORLEVEL%
echo setup_local_venv: need py or python in PATH >&2
exit /b 127
