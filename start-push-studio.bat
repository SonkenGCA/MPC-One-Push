@echo off
rem Default: offline desktop preview. Pass --live to use the MPC and Live.
rem Stop the original app with SHIFT+MENU before --live; close MPC 3 too.
cd /d "%~dp0"
.venv\Scripts\python.exe bridge\mpc_push_studio.py %*
if errorlevel 1 pause
