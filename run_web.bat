@echo off
setlocal
REM One-click launcher for Classroom ASR Web UI.
REM Usage: double-click run_web.bat
REM        or: run_web.bat --port 8080 --host 0.0.0.0
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] Project Python environment is missing. Run setup.bat first.
    pause
    exit /b 1
)

echo ==============================================
echo  Classroom ASR Web UI
echo  Browser: http://127.0.0.1:8765
echo  Press Ctrl+C to exit
echo ==============================================
echo.

"%PY%" -m asr.web --host 127.0.0.1 --port 8765 %*
set "RESULT=%ERRORLEVEL%"
if errorlevel 1 (
    echo.
    echo ==============================================
    echo  Server failed. If the port is occupied, close its owner yourself
    echo  or choose a different port: run_web.bat --port 8766
    echo ==============================================
)
pause
exit /b %RESULT%
