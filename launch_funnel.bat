@echo off
title Morning Funnel Launcher

REM ============================================================================
REM  Launches the morning funnel (tools\morning_funnel.py) in its own window.
REM  Pass tickers as arguments:   launch_funnel.bat HOLO SPRC ZCMD
REM  With no arguments it uses config\funnel_watchlist.txt plus the live
REM  mention watchlist and swing screener output.
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

start "Morning Funnel" /D "%~dp0" cmd /k "mode con: cols=100 lines=36 & %PYEXE% tools\morning_funnel.py %*"
exit
