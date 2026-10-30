@echo off
rem MPC One Push: current Studio interface.
rem Close MPC desktop and other controller apps, then enter USB controller mode.
rem Quit with SHIFT+MENU on the MPC or Ctrl+C here.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setup required: follow the README to create .venv and install requirements.
  pause
  exit /b 1
)
.venv\Scripts\python.exe bridge\mpc_push_studio.py --live %*
pause
