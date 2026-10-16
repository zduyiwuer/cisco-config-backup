@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 配置开机自启 - 多厂商网络设备配置备份中心

chcp 936 >nul 2>&1

echo.
echo   ============================================
echo    配置开机自启
echo   ============================================
echo.
echo   创建一个登录时自动启动的计划任务。
echo.

set "TASK_NAME=多厂商网络设备配置备份中心"

REM ---------- 权限检查 ----------
net session >nul 2>&1
if errorlevel 1 (
    echo   [提示] 需要管理员权限
    echo.
    echo   请右键本文件，选择 以管理员身份运行
    echo.
    pause
    exit /b 1
)

REM ---------- 定位运行环境 ----------
set "PYEXE=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYEXE%" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    echo   [错误] 未找到运行环境
    echo   请先双击 启动服务.bat 完成首次初始化
    echo.
    pause
    exit /b 1
)

REM ---------- 创建计划任务 ----------
echo   正在创建计划任务 "%TASK_NAME%"...
schtasks /Create /TN "%TASK_NAME%" /TR "\"%PYEXE%\" \"%~dp0run.py\" --port 8077" /SC ONLOGON /RL HIGHEST /F >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 计划任务创建失败
    echo   请确认以管理员身份运行
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================
echo    配置完成
echo   ============================================
echo.
echo   开机登录后会自动启动服务
echo.
echo   取消开机自启执行：
echo     schtasks /Delete /TN "%TASK_NAME%" /F
echo.

REM ---------- 立即启动一次 ----------
echo   正在启动服务...
echo.
echo   ============================================
echo    服务地址   http://127.0.0.1:8077
echo   ============================================
echo.

start "" "%PYEXE%" "%~dp0run.py" --port 8077
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8077

echo   浏览器已打开。如未自动打开请手动访问上面的地址
echo.
pause