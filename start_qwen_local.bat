@echo off
setlocal

if not defined SCIDEV_MODEL set "SCIDEV_MODEL=scidev-qwen2.5-coder-7b:q4_k_m"

where ollama >nul 2>&1
if errorlevel 1 (
    echo Ollama was not found on PATH. Install and start Ollama first.
    pause
    exit /b 1
)

ollama show "%SCIDEV_MODEL%" >nul 2>&1
if errorlevel 1 (
    echo The local Ollama model was not found: %SCIDEV_MODEL%
    echo See the Qwen2.5-Coder section in README.md.
    pause
    exit /b 1
)

set "SCIDEV_API_BASE=http://127.0.0.1:11434/v1"
set "SCIDEV_API_KEY=ollama"
set "SCIDEV_TEXT_TOOL_CALL_FALLBACK=1"
if not defined SCIDEV_TEMPERATURE set "SCIDEV_TEMPERATURE=0"
if not defined SCIDEV_REQUEST_TIMEOUT_SECONDS set "SCIDEV_REQUEST_TIMEOUT_SECONDS=240"
if not defined SCIDEV_STREAMING set "SCIDEV_STREAMING=1"

call "%~dp0start_client.bat" %*
exit /b %errorlevel%
