@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "VENV_PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo Creating project virtual environment...
    python -m venv "%PROJECT_DIR%.venv"
    if errorlevel 1 goto :error
)

"%VENV_PYTHON%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo Installing project dependencies into .venv...
    "%VENV_PYTHON%" -m pip install -r "%PROJECT_DIR%requirements.txt"
    if errorlevel 1 goto :error
)

"%VENV_PYTHON%" "%PROJECT_DIR%scidev_client.py"
exit /b %errorlevel%

:error
echo SciDevHarness could not prepare its virtual environment.
pause
exit /b 1
