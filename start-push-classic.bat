@echo off
rem Starts MPC One Push with the original (pre-Studio) look. start-push.bat is the default. Leave this window open while you play.
rem Before: close MPC 3 and the tablet bridge, put the MPC in controller mode, open Live with AbletonOSC.
rem Quit: hold SHIFT and press MENU on the MPC, or Ctrl+C here.
cd /d "%~dp0"
.venv\Scripts\python.exe bridge\mpc_push.py %*
pause
