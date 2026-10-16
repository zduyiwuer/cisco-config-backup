@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 关闭服务 - 多厂商网络设备配置备份中心

chcp 936 >nul 2>&1

echo.
echo   ============================================
echo    关闭服务
echo   ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo   [错误] 运行环境不存在，无需关闭
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" scripts\daemonctl.py stop
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo   服务已停止
) else if "%RC%"=="2" (
    echo   [提示] 端口仍被占用
    echo   若确认不是本服务，可手动结束:
    echo     taskkill /F /PID ^<进程号^>
) else (
    echo   停止失败，请查看上方提示
)
echo.
pause