@echo off
setlocal
cd /d "%~dp0"
set "PATH=%PATH:C:\chromedriver=%"

set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%~dp0venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"

if defined PYTHON_EXE (
    "%PYTHON_EXE%" "%~dp0auto_reply.py"
) else (
    python "%~dp0auto_reply.py"
)

endlocal
