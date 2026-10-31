@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Opens a browser window. Sign in with Google; the window closes by itself.
".venv\Scripts\python.exe" farm.py login
echo.
echo Exit code: %errorlevel%  (0 = login saved)
pause
