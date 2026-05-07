@echo off
cd /d "%~dp0"
venv\Scripts\python screen_share.py %*
pause
