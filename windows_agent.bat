@echo off
title Brasfield Trading — Windows Agent
echo.
echo  ============================================
echo   Brasfield Trading — Windows Local Agent
echo  ============================================
echo.
echo  This agent lets the trading dashboard's Add
echo  button control Webull Desktop on this PC.
echo.
echo  Listening on http://localhost:8889
echo  Keep this window open while using the dashboard.
echo.

REM Try venv first (if user has set one up), fall back to system Python
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" "%~dp0windows_agent.py"
) else (
    python "%~dp0windows_agent.py"
)

echo.
echo Agent stopped. Press any key to close.
pause >nul
