@echo off
setlocal

set "MODEL_TAG=scidev-qwen2.5-coder-7b:q4_k_m"

where ollama >nul 2>&1
if errorlevel 1 (
    echo Ollama was not found on PATH. Install and start Ollama first.
    pause
    exit /b 1
)

ollama show "%MODEL_TAG%" >nul 2>&1
if errorlevel 1 (
    echo The local Qwen model was not found: %MODEL_TAG%
    echo See the Qwen2.5-Coder section in README.md.
    pause
    exit /b 1
)

set "SCIDEV_API_BASE=http://127.0.0.1:11434/v1"
set "SCIDEV_API_KEY=ollama"
set "SCIDEV_MODEL=%MODEL_TAG%"
set "SCIDEV_TEXT_TOOL_CALL_FALLBACK=1"

call "%~dp0start_client.bat"
exit /b %errorlevel%
