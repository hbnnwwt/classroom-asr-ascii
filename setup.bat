@echo off
REM 一键环境搭建：装依赖 + 下载模型（约 2.1 GB，只需联网跑一次）。
REM 用法：双击 setup.bat，完成后双击 run_web.bat 启动服务。
cd /d "%~dp0"

REM --- 1) 找 Python（优先 py 启动器，回退 PATH 里的 python）---
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY (
    python --version >nul 2>&1 && set PY=python
)
if not defined PY (
    echo [ERROR] 未找到 Python。请先安装 Python 3.10+:
    echo         https://www.python.org/downloads/
    echo         安装时勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)

echo 使用 Python:
%PY% --version
echo.

REM --- 2) 安装依赖 ---
echo ==============================================
echo  [1/2] 安装 Python 依赖
echo ==============================================
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)
echo.

REM --- 3) 下载模型（已下载过的会自动校验跳过，可安全重复执行）---
echo ==============================================
echo  [2/2] 下载 ASR 模型（约 2.1 GB，耐心等待）
echo ==============================================
%PY% -X utf8 scripts\download_models.py
if errorlevel 1 (
    echo [ERROR] 模型下载失败，请检查网络后重试。
    pause
    exit /b 1
)

echo.
echo ==============================================
echo  搭建完成！双击 run_web.bat 启动服务，
echo  浏览器访问 http://127.0.0.1:8765
echo ==============================================
pause
