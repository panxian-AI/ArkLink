@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title ArkLink 一键部署

echo ================================================================
echo    ArkLink 一键部署
echo ================================================================
echo.
echo   直接回车  = 部署到本机虚拟机（自动装 VirtualBox + Ubuntu + QQ）
echo   输入地址  = 部署到远程 Linux 服务器，例如：
echo                root@1.2.3.4
echo                root@1.2.3.4:2222      （非默认 SSH 端口）
echo.
echo   注意：远程部署要求那台机器是 Ubuntu 22.04/24.04 或 Debian 12，
echo         并且你的 SSH 能登录（密钥或密码都行，会让你在窗口里输）。
echo.
set "TARGET="
set /p TARGET=请输入部署地址（直接回车 = 本机虚拟机）:

rem ------------------------------------------------ 找 Python
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo [X] 没找到 Python 3.11+。
  echo     去 https://www.python.org/downloads/ 安装，勾选 "Add python.exe to PATH" 后重开本文件。
  echo.
  pause
  exit /b 1
)

rem ------------------------------------------------ 首次运行装依赖
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [1/2] 首次运行：创建虚拟环境并安装依赖（约 1 分钟）...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [X] 创建虚拟环境失败。
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -e .
  if errorlevel 1 (
    echo [!] 依赖安装可能失败（网络问题？）——仍然继续尝试启动。
  )
) else (
  echo.
  echo [1/2] 已检测到 .venv，跳过安装
)

echo [2/2] 开始部署...
echo.
if "%TARGET%"=="" (
  ".venv\Scripts\python.exe" -m arklink deploy
) else (
  ".venv\Scripts\python.exe" -m arklink deploy "%TARGET%"
)
set "CODE=%errorlevel%"

echo.
if not "%CODE%"=="0" (
  echo [X] 部署失败（退出码 %CODE%）
  echo.
  echo     下一步可以：
  echo       1) 把上面的报错发给我；
  echo       2) 或运行： .venv\Scripts\python.exe -m arklink env doctor --ai
  echo          （用 DeepSeek 分析原因，需要先准备 API key）
) else (
  echo [OK] 部署完成。
  echo      本机虚拟机：浏览器打开 http://127.0.0.1:6800/ 扫码登录
  echo      远程服务器：浏览器打开 http://服务器地址:6800/ 扫码登录
)
echo.
pause
