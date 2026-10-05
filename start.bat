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
cd /d "%ROOT%"

rem The Claude connector starts too, once setup-connector.bat has installed its packages.
"%PYTHON_EXE%" -c "import mcp, jsonschema, uvicorn" >nul 2>nul
if errorlevel 1 goto app
if not defined EXAM_SERVER set "EXAM_SERVER=http://127.0.0.1:7900"
if not defined EDITOR_URL set "EDITOR_URL=http://%COMPUTERNAME%:7900/"
if not defined PUBLIC_URL set "PUBLIC_URL=http://%COMPUTERNAME%:7901"
set "PORT=7901"
start "Exam Assistant - Claude connector" /min "%PYTHON_EXE%" "%ROOT%connector\server.py"
echo  Claude connector:  http://%COMPUTERNAME%:7901  - minimised in its own window

:app
echo  Press Ctrl+C to stop.
echo ============================================================
"%PYTHON_EXE%" "%ROOT%server.py" 7900
pause
