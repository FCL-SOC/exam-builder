@echo off
setlocal
title Exam Assistant Setup
set "ROOT=%~dp0"
set "PYTHON_DIR=%ROOT%python"

echo ============================================================
echo  Exam Assistant Setup
echo  Run this once on a new machine, then use start.bat
echo ============================================================
echo.

if exist "%PYTHON_DIR%\python.exe" (
    echo Portable Python is already installed. Run start.bat.
    pause
    exit /b 0
)

echo Downloading portable Python 3.11 ...
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile '%ROOT%py_embed.zip' -UseBasicParsing"
if errorlevel 1 (
    echo [ERROR] Download failed. Alternative: install Python 3.10 or newer from python.org, then run start.bat.
    pause
    exit /b 1
)
powershell -NoProfile -Command "Expand-Archive -Path '%ROOT%py_embed.zip' -DestinationPath '%PYTHON_DIR%' -Force"
del "%ROOT%py_embed.zip" 2>nul

echo.
echo Setup complete. No packages are needed. Run start.bat to launch.
pause
