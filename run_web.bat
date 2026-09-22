@echo off
REM One-click launcher for Classroom ASR Web UI.
REM Usage: double-click run_web.bat
REM        or: run_web.bat --port 8080 --host 0.0.0.0
chcp 65001 >nul
cd /d "%~dp0"

REM Kill any leftover python.exe holding port (previous test runs)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr LISTENING') do (
    taskkill /pid %%a /f >nul 2>&1
)

echo ==============================================
echo  Classroom ASR Web UI
echo  Browser: http://127.0.0.1:8765
echo  Press Ctrl+C to exit
echo ==============================================
echo.

py -3 -m asr.web --host 127.0.0.1 --port 8765 %*
if errorlevel 1 (
    echo.
    echo ==============================================
    echo  FAILED. Try a different port:
    echo    run_web.bat --port 8765
    echo ==============================================
)
pause