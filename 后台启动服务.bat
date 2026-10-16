@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 后台启动 - 多厂商网络设备配置备份中心

chcp 936 >nul 2>&1

echo.
echo   ============================================
echo    后台启动服务
echo   ============================================
echo.
echo   服务将在后台运行，不占用本窗口
echo   关闭服务请运行 关闭服务.bat
echo.

if not exist ".venv\Scripts\python.exe" (
    echo   [错误] 运行环境不存在
    echo   请先双击 启动服务.bat 完成首次初始化
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" scripts\daemonctl.py start
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo   ============================================
    echo    服务已在后台运行
    echo   ============================================
    echo.
    echo    关闭本窗口不影响服务
    echo    停止服务  运行 关闭服务.bat
    echo.
    start "" "http://127.0.0.1:8077"
) else if "%RC%"=="2" (
    echo   服务可能启动异常，请查看上方提示与日志文件
) else (
    echo   启动失败，请查看上方提示
)
echo.
timeout /t 8 /nobreak >nul