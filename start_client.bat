@echo off
setlocal

where dotnet >nul 2>&1
if errorlevel 1 (
    echo Install the .NET 10 SDK to run SciDevHarness from source.
    if not defined CI pause
    exit /b 1
)

dotnet run --project "%~dp0dotnet\src\SciDev.Desktop\SciDev.Desktop.csproj" -- %*
set "CLIENT_EXIT_CODE=%ERRORLEVEL%"
if not "%CLIENT_EXIT_CODE%"=="0" if not defined CI pause
exit /b %CLIENT_EXIT_CODE%
