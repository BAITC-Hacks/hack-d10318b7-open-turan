@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created local-model settings in .env.
)

if not exist "venv\Scripts\python.exe" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python 3.11 or newer is required. Install Python and enable Add to PATH.
        pause
        exit /b 1
    )
    python -m venv venv
    if errorlevel 1 (
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
)

"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed. Check the internet connection and retry.
    pause
    exit /b 1
)

where ollama >nul 2>&1
if errorlevel 1 (
    echo.
    echo Ollama is not installed. Install it from https://ollama.com/download
    echo Then run: ollama pull qwen2.5:3b
    echo Restart this file after the model download finishes.
    echo.
)

echo.
echo Meeting Notes: http://127.0.0.1:8000
echo Local setup status: http://127.0.0.1:8000/health
echo Press Ctrl+C to stop the server.
echo.
"venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

endlocal

