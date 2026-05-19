@echo off
title Brasfield Trading — Windows Agent

REM ============================================================================
REM  Brasfield Trading — Windows Local Agent
REM ============================================================================
REM
REM  This agent lets the trading dashboard's Add button (and burst/BUY alerts)
REM  control Webull Desktop and TradingView on this PC.
REM
REM  ── TradingView setup (do this once before running the agent) ─────────────
REM    1. Open Brave or Google Chrome.
REM    2. Go to https://www.tradingview.com/chart/ and load any chart.
REM    3. Right-click the TradingView tab and choose "Pin tab".
REM    4. Drag the pinned tab so it is the FIRST tab in the window
REM       (Ctrl+1 must switch to it). Leave the window open while trading.
REM    5. If you keep TradingView on a different tab number, edit BRAVE_TV_TAB
REM       in your .env file to match (Ctrl+2 = 2, Ctrl+3 = 3, etc.).
REM
REM  ── Webull setup ──────────────────────────────────────────────────────────
REM    Install Webull Desktop if you want auto-add to a Webull watchlist.
REM    If Webull Desktop is not installed, the agent skips the Webull step
REM    and only adds tickers to TradingView.
REM ============================================================================

echo.
echo  ============================================
echo   Brasfield Trading — Windows Local Agent
echo  ============================================
echo.

REM ── Step 1: locate a Python interpreter ──────────────────────────────────────
set "PYEXE="
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYEXE=%~dp0venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set "PYEXE=python"
    ) else (
        where py >nul 2>&1
        if %ERRORLEVEL% EQU 0 set "PYEXE=py -3"
    )
)

if "%PYEXE%"=="" (
    echo  ❌ Python was not found on this PC.
    echo.
    echo  The Windows agent needs Python 3.9 or newer.
    echo  Install it from one of these sources:
    echo.
    echo     • Microsoft Store:  search for "Python 3" and click Get
    echo     • Official site:    https://www.python.org/downloads/windows/
    echo                         IMPORTANT — tick "Add python.exe to PATH"
    echo                         on the first screen of the installer.
    echo.
    echo  After Python is installed, close this window and double-click
    echo  windows_agent.bat again.
    echo.
    pause
    exit /b 1
)

echo  Using Python: %PYEXE%
echo.

REM ── Step 2: make sure required packages are installed ───────────────────────
REM  Quick import check — if it succeeds we skip pip entirely (fast startup).
%PYEXE% -c "import pyautogui, pygetwindow" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  Installing missing Python packages ^(pyautogui, pygetwindow^)...
    %PYEXE% -m pip install --upgrade pip >nul 2>&1
    %PYEXE% -m pip install pyautogui pygetwindow
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo  ❌ Failed to install required packages.
        echo     Try running this manually in a terminal:
        echo         %PYEXE% -m pip install pyautogui pygetwindow
        echo.
        pause
        exit /b 1
    )
    echo  ✅ Packages installed.
    echo.
)

echo  Listening on http://localhost:8889
echo  Keep this window open while using the dashboard.
echo.

%PYEXE% "%~dp0windows_agent.py"

echo.
echo Agent stopped. Press any key to close.
pause >nul
