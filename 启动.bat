@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"

%PY% -c "import sys" >nul 2>nul
if errorlevel 1 goto :nopython

%PY% run.py %*
if errorlevel 1 goto :failed
exit /b 0

:nopython
echo.
echo Python 3.10+ was not found.
echo Install Python from https://www.python.org/downloads/
echo and enable "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1

:failed
echo.
echo Startup failed. Install dependencies with:
echo   python -m pip install -r requirements.txt
echo.
pause
exit /b 1
