@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo 尚未安装 OP 端环境。请先按 docs\OP端使用教程.md 完成一次性安装。
  echo python -m venv .venv
  echo .venv\Scripts\python.exe -m pip install -e .
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m mcmodsync menu %*
if errorlevel 1 (
  echo OP 端启动失败，请查看上方提示。
  pause
  exit /b 1
)
