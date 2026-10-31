@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Checks whether XP is still claimable and opens the page in YOUR browser if it is
".venv\Scripts\python.exe" farm.py open
timeout /t 4 >nul
