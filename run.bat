@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m bestrobot run
) else (
  py -3 -m bestrobot run
)
