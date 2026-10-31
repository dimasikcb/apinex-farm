@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Starts the hidden background worker (claims XP once per day).
start "" wscript.exe "watch_hidden.vbs"
echo Background worker started (no window). Logs: logs\
timeout /t 3 >nul
