@echo off
setlocal
cd /d "%~dp0"

rem Prefer the project's virtual environment
set "PY=.venv\Scripts\python.exe"
if exist "%PY%" goto :run

rem No venv yet: if uv is available, create one and install dependencies
where uv >nul 2>nul
if errorlevel 1 goto :nouv

echo [setup] Creating virtual environment with uv ...
uv venv
if errorlevel 1 goto :nopython
echo [setup] Installing dependencies ...
uv pip install -r requirements.txt
if errorlevel 1 goto :failed
goto :run

:nouv
rem No uv: fall back to system Python
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    goto :run
)
python -c "import sys" >nul 2>nul
if errorlevel 1 goto :nopython
set "PY=python"

:run
if exist "%PY%" (
    "%PY%" run.py %*
) else (
    %PY% run.py %*
)
if errorlevel 1 goto :failed
exit /b 0

:nopython
echo.
echo Python 3.10+ was not found and uv is not installed.
echo Install Python from https://www.python.org/downloads/
echo or install uv: https://docs.astral.sh/uv/
echo.
pause
exit /b 1

:failed
echo.
echo Startup failed. Install dependencies with:
echo   uv pip install -r requirements.txt
echo.
pause
exit /b 1
