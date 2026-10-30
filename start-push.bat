@echo off
rem MPC One Push: current Studio interface.
rem Close MPC desktop and other controller apps, then enter USB controller mode.
rem Quit with SHIFT+MENU on the MPC or Ctrl+C here.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment for MPC One Push...
  py -3 -m venv .venv
  if errorlevel 1 goto setup_error
)
.venv\Scripts\python.exe -c "import PIL" >nul 2>&1
if errorlevel 1 (
  echo Installing the required Pillow package...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto setup_error
)
.venv\Scripts\python.exe bridge\mpc_push_studio.py --live %*
pause
exit /b
:setup_error
echo Setup could not finish. Install Python 3 with the Python launcher enabled,
echo check your internet connection, then run start-push.bat again.
echo See README for manual setup instructions.
pause
exit /b 1
