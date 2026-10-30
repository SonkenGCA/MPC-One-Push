@echo off
rem Auto-start: runs MPC One Push whenever the MPC One is connected and Live is open (no window).
rem It writes what it does to logs\autostart.txt. Stop it in Task Manager (pythonw.exe).
rem "start-autostart.bat --check" only says what it would do right now.
cd /d "%~dp0"
if "%1"=="--check" (
  .venv\Scripts\python.exe bridge\autostart.py --check
  pause
  exit /b
)
start "" .venv\Scripts\pythonw.exe bridge\autostart.py
