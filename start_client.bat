@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "VENV_PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" "%PROJECT_DIR%scripts\bootstrap.py" %*
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        python "%PROJECT_DIR%scripts\bootstrap.py" %*
    ) else (
        where py >nul 2>&1
        if errorlevel 1 goto :python_missing
        py -3 "%PROJECT_DIR%scripts\bootstrap.py" %*
    )
)

set "CLIENT_EXIT_CODE=%ERRORLEVEL%"
if not "%CLIENT_EXIT_CODE%"=="0" if not defined CI pause
exit /b %CLIENT_EXIT_CODE%

:python_missing
echo Python 3.12, 3.13, or 3.14 is required to start SciDevHarness.
set "CLIENT_EXIT_CODE=1"
if not defined CI pause
exit /b 1
