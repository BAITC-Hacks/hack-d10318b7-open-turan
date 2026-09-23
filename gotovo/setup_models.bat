@echo off
setlocal
where ollama >nul 2>&1
if errorlevel 1 (
    echo Ollama is not installed. Install it from https://ollama.com/download and run it first.
    pause
    exit /b 1
)

echo Downloading the local meeting-summary model qwen2.5:3b.
echo This download may take a while and uses disk space.
ollama pull qwen2.5:3b
if errorlevel 1 (
    echo Model download failed. Check Ollama and the internet connection, then retry.
    pause
    exit /b 1
)

echo Model ready. You can now run start.bat.
pause
endlocal

