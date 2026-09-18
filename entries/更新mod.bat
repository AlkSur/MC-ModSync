@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "TARGET=%CD%"
"%~dp0_updater\mcmodsync.exe" sync --target "%TARGET%" %*
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
    echo === 更新完成，请手动启动 PCL2 或 HMCL ===
) else (
    echo === 更新失败，退出码 %RC%，日志见 _updater\logs\ ===
)
echo.%* | findstr /C:"--no-pause" >nul || pause
exit /b %RC%
