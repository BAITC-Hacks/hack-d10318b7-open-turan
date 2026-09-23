@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" copy ".env.example" ".env" >nul
if exist "venv\Scripts\python.exe" goto dependencies
py -3.11 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1
if errorlevel 1 goto fallback
py -3.11 -m venv venv
goto created
:fallback
python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1
if errorlevel 1 goto python_missing
python -m venv venv
:created
if errorlevel 1 goto failure
:dependencies
"venv\Scripts\python.exe" launcher.py --check-deps
if not errorlevel 1 goto run
echo Installing local dependencies. The first start needs an internet connection.
"venv\Scripts\python.exe" -m pip install --use-feature=truststore --no-cache-dir --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failure
:run
if /I "%~1"=="--setup" goto setup
echo Meeting Notes: http://127.0.0.1:8000
echo Keep this window open. Press Ctrl+C to stop.
"venv\Scripts\python.exe" launcher.py %*
if errorlevel 1 goto failure
exit /b 0
:setup
"venv\Scripts\python.exe" setup_models.py
if errorlevel 1 goto failure
pause
exit /b 0
:python_missing
echo Python 3.11 or newer is required. Install Python and enable Add to PATH.
pause
exit /b 1
:failure
echo.
echo Startup failed. Read the error above and check README.md.
pause
exit /b 1

