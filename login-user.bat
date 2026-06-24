@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m bestrobot login
) else (
  py -3 -m bestrobot login
)
