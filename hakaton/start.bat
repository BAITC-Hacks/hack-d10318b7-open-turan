@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example.
    echo Add your OPENAI_API_KEY and ANTHROPIC_API_KEY to .env, then run this file again.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv venv
    ) else (
        py -m venv venv
    )
    if errorlevel 1 (
        echo Could not create the virtual environment. Make sure Python is installed.
        pause
        exit /b 1
    )
)

"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Could not install dependencies.
    pause
    exit /b 1
)

echo.
echo Server: http://127.0.0.1:8000
echo API docs: http://127.0.0.1:8000/docs
echo Press Ctrl+C to stop.
echo.
"venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

endlocal
