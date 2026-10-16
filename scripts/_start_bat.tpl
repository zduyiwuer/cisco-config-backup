@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 多厂商网络设备配置备份中心

REM 显式切到 GBK 代码页，保证中文在不同系统区域设置下都正常显示
chcp 936 >nul 2>&1

echo.
echo   ============================================
echo    多厂商网络设备配置备份中心
echo   ============================================
echo.

REM ---------- 定位 Python ----------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo   [错误] 未找到 Python
    echo.
    echo   请先安装 Python 3.11 或更高版本，安装时务必勾选
    echo   "Add Python to PATH"，然后重新运行本脚本。
    echo   下载地址 https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM ---------- 创建虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo   [1/3] 首次运行，正在创建运行环境...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo   [错误] 虚拟环境创建失败
        echo   若提示权限不足，请右键本文件选择以管理员身份运行。
        echo.
        pause
        exit /b 1
    )
    echo         运行环境创建完成
) else (
    echo   [1/3] 运行环境已就绪
)

REM ---------- 安装依赖 ----------
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,paramiko,apscheduler,cryptography,openpyxl,multipart,ldap3" >nul 2>&1
if errorlevel 1 (
    echo   [2/3] 正在安装依赖，首次约需 1-2 分钟...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [错误] 依赖安装失败，请检查网络连接
        echo.
        pause
        exit /b 1
    )
) else (
    echo   [2/3] 依赖已就绪
)

REM ---------- 检查端口占用 ----------
set "OCCUPIED="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8077" ^| findstr "LISTENING"') do set "OCCUPIED=%%a"
if defined OCCUPIED (
    echo.
    echo   [提示] 端口 8077 已被进程 !OCCUPIED! 占用
    echo          若服务已在运行，直接访问即可
    echo          确认未运行可执行: taskkill /F /PID !OCCUPIED!
    echo.
)

echo   [3/3] 正在启动服务...
echo.
echo   ============================================
echo    服务地址   http://127.0.0.1:8077
echo    停止服务   按 Ctrl+C 或关闭本窗口
echo   ============================================
echo.

".venv\Scripts\python.exe" run.py --port 8077
set "EXITCODE=%errorlevel%"

echo.
if not "%EXITCODE%"=="0" (
    echo   服务异常退出，错误码 %EXITCODE%
    echo   请查看上方日志排查。
) else (
    echo   服务已停止。
)
echo.
pause