@echo off
title Save Monitor Layout
REM Arrange the two monitor windows how you like, then double-click this.
REM Their size and position will be restored on every future launch.

set "PYEXE="
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYEXE=%~dp0venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set "PYEXE=python"
    ) else (
        set "PYEXE=py -3"
    )
)

%PYEXE% "%~dp0position_windows.py" save
echo.
pause
