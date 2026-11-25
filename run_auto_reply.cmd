@echo off
setlocal
cd /d "%~dp0"
set "PATH=%PATH:C:\chromedriver=%"
"%~dp0venv\Scripts\python.exe" "%~dp0auto_reply.py"
endlocal
