@echo off
setlocal
title Exam Assistant
set "ROOT=%~dp0"
set "PYTHON_EXE=%ROOT%python\python.exe"

echo ============================================================
echo  Exam Assistant
echo ============================================================

if exist "%PYTHON_EXE%" goto run
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Run setup.bat first, or install Python 3.10 or newer.
    pause
    exit /b 1
)
set "PYTHON_EXE=python"

:run
echo  Open browser at: http://localhost:7900
echo  Press Ctrl+C to stop.
echo ============================================================
cd /d "%ROOT%"
"%PYTHON_EXE%" "%ROOT%server.py" 7900
pause
