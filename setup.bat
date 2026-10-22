@echo off
setlocal
REM Install only into the project environment. Model downloads are explicit.
cd /d "%~dp0"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto install

REM ASR_PYTHON is an optional full path; otherwise prefer supported py launcher versions.
set "BOOTSTRAP=%ASR_PYTHON%"
if defined BOOTSTRAP goto create
for %%V in (3.12 3.11 3.10) do (
    if not defined BOOTSTRAP (
        for /f "delims=" %%P in ('py -%%V -c "import sys; print(sys.executable)" 2^>nul') do set "BOOTSTRAP=%%P"
    )
)
if not defined BOOTSTRAP (
    echo [ERROR] Install Python 3.10-3.12 with the Python launcher:
    echo         https://www.python.org/downloads/
    echo         Or set ASR_PYTHON to the full path of a supported python.exe.
    pause
    exit /b 1
)

:create
"%BOOTSTRAP%" -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The selected interpreter must be Python 3.10-3.12.
    pause
    exit /b 1
)
"%BOOTSTRAP%" -m venv "%~dp0.venv"
if errorlevel 1 goto failed
if not exist "%VENV_PY%" goto failed

:install
"%VENV_PY%" -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Existing .venv must use Python 3.10-3.12.
    pause
    exit /b 1
)
if not exist config.yaml copy /y config.example.yaml config.yaml >nul
if not exist config.yaml goto failed
"%VENV_PY%" --version
"%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto install_general

REM Metadata-verified Windows CPU profile; real-model inference still needs validation.
"%VENV_PY%" -m pip install -r requirements.txt -c constraints\cpu-py312.txt --extra-index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto failed
goto validate

:install_general
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:validate
"%VENV_PY%" -m pip check
if errorlevel 1 goto failed
echo.
echo Dependencies installed into .venv.
echo Download models explicitly when online:
echo   ".venv\Scripts\python.exe" -X utf8 scripts\download_models.py
echo Then double-click run_web.bat. Browser: http://127.0.0.1:8765
pause
exit /b 0

:failed
echo [ERROR] Installation failed. Review the error above and rerun setup.bat.
pause
exit /b 1
