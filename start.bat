@echo off
rem ---------------------------------------------------------------
rem  ArkLink one-click launcher (Windows)
rem  Double-click this file. First run creates .venv and installs,
rem  later runs just start the console.
rem  Extra args are passed through, e.g.  start.bat --port 6801
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem 这里必须用 `if not errorlevel 1`，不能用 `%errorlevel%`：
rem 后者在括号块里是「解析时展开」，拿到的永远是上一条命令的旧值。
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [X] Python 3.11+ not found.
  echo     Install from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] creating virtual environment .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [X] failed to create venv
    pause
    exit /b 1
  )
  echo [2/3] installing dependencies ...
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -e .
  if errorlevel 1 (
    echo [!] pip install failed - probably no network.
    echo     If arklink still starts below, the deps were already present.
  )
) else (
  echo [1/3] venv found, skipping install
)

echo [3/3] starting console ...
echo.
".venv\Scripts\python.exe" -m arklink start %*
set "CODE=%errorlevel%"
echo.
if not "%CODE%"=="0" (
  echo [X] exited with code %CODE%
  pause
)
exit /b %CODE%
