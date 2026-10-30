@echo off
rem Starts the tablet bridge. Leave this window open while you play.
cd /d "%~dp0"
py -I bridge\server.py %*
pause
