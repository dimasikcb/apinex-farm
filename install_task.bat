@echo off
chcp 65001 >nul
rem Registers daily task "APInex XP Farm" in Windows Task Scheduler.
rem Change time in /ST (currently 10:05). Runs hidden, only when you are logged in.

set "HERE=%~dp0"
schtasks /Create /TN "APInex XP Farm" /TR "\"%HERE%run_daily.vbs\"" /SC DAILY /ST 10:05 /RL LIMITED /F

if errorlevel 1 (
  echo.
  echo Failed to create the task. Run this file as Administrator.
) else (
  echo.
  echo Done: task "APInex XP Farm" runs daily at 10:05.
  schtasks /Query /TN "APInex XP Farm" /FO LIST
)
pause
