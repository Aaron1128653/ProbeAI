@echo off
rem Double-click: like start_demo.bat PLUS the LIVE page. LIVE uses the real API (about 5 US cents per run).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0demo_fallback\demo_launch.ps1" -Live
echo.
pause
