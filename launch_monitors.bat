@echo off
title Trading Monitors Launcher

REM ============================================================================
REM  Launches both trading monitors, each in its own window:
REM    1. Webull L2 Monitor      (webull-l2\l2_signal.py)
REM    2. Momentum Monitor       (momentum-monitor\momentum_signal.py)
REM
REM  Requirements: Python 3.9+. The L2 monitor needs Tesseract-OCR and the
REM  Webull Desktop window visible; the momentum monitor polls the dashboard
REM  feed (trading.jbrasfield.com) and needs no screen scraping.
REM ============================================================================

REM ── locate a Python interpreter ─────────────────────────────────────────────
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYEXE=%~dp0.venv\Scripts\python.exe"
) else if exist "%~dp0venv\Scripts\python.exe" (
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
    echo  Python not found. Install Python 3.9+ and tick "Add to PATH".
    pause
    exit /b 1
)
echo  Using Python: %PYEXE%

REM ── make sure required packages are installed ───────────────────────────────
%PYEXE% -c "import mss, cv2, numpy, pytesseract, rich, plyer" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  Installing monitor packages...
    %PYEXE% -m pip install -r "%~dp0webull-l2\requirements.txt"
)

if not exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    echo  WARNING: Tesseract-OCR not found - both monitors need it.
    echo  Install from https://github.com/UB-Mannheim/tesseract/wiki
)

REM ── window sizes (text columns x lines) — used by the fallback path ────────
set "L2_SIZE=cols=60 lines=32"
set "TV_SIZE=cols=60 lines=26"

REM ── one combined window (Windows Terminal split panes) when available ──────
where wt >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto classic

echo  Launching combined monitor window (L2 top, Momentum bottom)...
wt --size 64,72 --title "Trading Monitors" --suppressApplicationTitle -d "%~dp0webull-l2" cmd /k %PYEXE% l2_signal.py ; split-pane -H --title "Trading Monitors" --suppressApplicationTitle -d "%~dp0momentum-monitor" cmd /k %PYEXE% momentum_signal.py
goto positioned

:classic
echo  Windows Terminal not found - launching two separate windows...
start "Webull L2 Monitor" /D "%~dp0webull-l2" cmd /k "mode con: %L2_SIZE% & %PYEXE% l2_signal.py"
start "Momentum Monitor" /D "%~dp0momentum-monitor" cmd /k "mode con: %TV_SIZE% & %PYEXE% momentum_signal.py"

:positioned
REM ── restore saved size/position (save with save_layout.bat) ────────────────
timeout /t 3 >nul
%PYEXE% "%~dp0position_windows.py"

echo.
echo  Both monitors launched. This window closes in 5 seconds.
timeout /t 5 >nul
exit
