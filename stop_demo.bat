@echo off
rem Double-click: stops the demo servers (by port). Safe to run any time.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0demo_fallback\demo_stop.ps1"
echo.
pause
