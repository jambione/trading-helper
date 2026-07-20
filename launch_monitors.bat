@echo off
title Trading Monitors Launcher

REM ============================================================================
REM  Launches trading monitors:
REM    1. Alpaca Flow Monitor   (flow_monitor\main.py)
REM    2. Momentum Monitor      (momentum-monitor\momentum_signal.py)
REM ============================================================================

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

set "FLOW_SIZE=cols=60 lines=32"
set "MOM_SIZE=cols=60 lines=26"

where wt >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto classic

echo  Launching combined monitor window (Flow top, Momentum bottom)...
wt --size 64,72 --title "Trading Monitors" --suppressApplicationTitle -d "%~dp0" cmd /k %PYEXE% -m flow_monitor.main ; split-pane -H --title "Trading Monitors" --suppressApplicationTitle -d "%~dp0momentum-monitor" cmd /k %PYEXE% momentum_signal.py
goto positioned

:classic
echo  Windows Terminal not found - launching two separate windows...
start "Alpaca Flow Monitor" /D "%~dp0" cmd /k "mode con: %FLOW_SIZE% & %PYEXE% -m flow_monitor.main"
start "Momentum Monitor" /D "%~dp0momentum-monitor" cmd /k "mode con: %MOM_SIZE% & %PYEXE% momentum_signal.py"

:positioned
timeout /t 3 >nul
%PYEXE% "%~dp0position_windows.py"

echo.
echo  Monitors launched. This window closes in 5 seconds.
timeout /t 5 >nul
exit
