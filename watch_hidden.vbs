' Hidden background worker: keeps running, claims XP once per day.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\admin\WorkBuddy AI\2026-10-27-21-39-31\apinex-farm"
sh.Run """.venv\Scripts\pythonw.exe"" ""farm.py"" watch", 0, False
