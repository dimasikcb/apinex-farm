@echo off
chcp 65001 >nul
rem Stops the background worker.
wmic process where "name='pythonw.exe' and commandline like '%%farm.py%%'" call terminate >nul 2>&1
echo Stopped (if it was running).
timeout /t 2 >nul
