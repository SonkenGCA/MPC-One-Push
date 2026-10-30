@echo off
rem Starts MPC One Push on the MPC One's own screen, with Astra's Push Studio look (the default since 2026-10-29).
rem Leave this window open while you play.
rem Before: close MPC 3 and the tablet bridge, put the MPC in controller mode, open Live with AbletonOSC.
rem Quit: hold SHIFT and press MENU on the MPC, or Ctrl+C here.
rem The original look: start-push-classic.bat. Offline preview of this look: start-push-studio.bat.
cd /d "%~dp0"
.venv\Scripts\python.exe bridge\mpc_push_studio.py --live %*
pause
