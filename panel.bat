@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Rebuilds panel.html and opens it in the default browser
".venv\Scripts\python.exe" farm.py panel
start "" "panel.html"
