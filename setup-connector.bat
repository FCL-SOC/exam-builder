@echo off
setlocal
title Exam Assistant - Claude connector setup
set "ROOT=%~dp0"
set "PYTHON_DIR=%ROOT%python"
set "PYTHON_EXE=%PYTHON_DIR%\python.exe"

echo ============================================================
echo  Exam Assistant - Claude connector setup
echo  Run this once on the server, after setup.bat.
echo  It needs an internet connection to download packages.
echo ============================================================
echo.

if exist "%PYTHON_EXE%" goto portable
where python >nul 2>nul
if errorlevel 1 goto nopython
set "PYTHON_EXE=python"
goto install

:portable
rem The portable Python ships without pip and ignores installed packages until "import site" is switched on.
for %%f in ("%PYTHON_DIR%\python3*._pth") do (
    powershell -NoProfile -Command "(Get-Content '%%~f') -replace '^#\s*import site', 'import site' | Set-Content '%%~f'"
)
if exist "%PYTHON_DIR%\Scripts\pip.exe" goto install
echo Adding pip to the portable Python ...
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%ROOT%get-pip.py' -UseBasicParsing"
if errorlevel 1 goto failed
"%PYTHON_EXE%" "%ROOT%get-pip.py" --no-warn-script-location
if errorlevel 1 goto failed
del "%ROOT%get-pip.py" 2>nul

:install
echo Installing the connector's packages ...
"%PYTHON_EXE%" -m pip install --no-warn-script-location --upgrade -r "%ROOT%connector\requirements.txt"
if errorlevel 1 goto failed
"%PYTHON_EXE%" -c "import mcp, jsonschema, uvicorn"
if errorlevel 1 goto failed

echo.
echo Allowing the connector through Windows Firewall (port 7901) ...
netsh advfirewall firewall show rule name="Exam Assistant - Claude connector" >nul 2>nul
if not errorlevel 1 goto done
netsh advfirewall firewall add rule name="Exam Assistant - Claude connector" dir=in action=allow protocol=TCP localport=7901 >nul 2>nul
if errorlevel 1 (
    echo  Couldn't add the firewall rule: this needs an administrator.
    echo  Ask IT to allow inbound TCP port 7901, or run this file again with "Run as administrator".
)

:done
echo.
echo Setup complete. start.bat now starts the Claude connector as well.
echo Teachers install connector\desktop-extension\exam-assistant.mcpb in Claude Desktop.
pause
exit /b 0

:nopython
echo [ERROR] Python not found. Run setup.bat first, or install Python 3.10 or newer.
pause
exit /b 1

:failed
echo.
echo [ERROR] Setup didn't finish. Check the internet connection and run setup-connector.bat again.
echo         Exam Assistant itself is unaffected and still works without the connector.
pause
exit /b 1
