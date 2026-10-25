@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "TARGET=%CD%"
set "MCS_UPDATE_ATTEMPTS=0"
:run_updater
"%~dp0_updater\mcmodsync.exe" sync --target "%TARGET%" %*
set "RC=%errorlevel%"
if "%RC%"=="42" goto install_updater
goto finish
:install_updater
set /a MCS_UPDATE_ATTEMPTS+=1 >nul
if %MCS_UPDATE_ATTEMPTS% GTR 2 (
    echo 更新器反复请求重启，已停止，请联系服主。
    set "RC=1"
    goto finish
)
"%~dp0_updater\self-update\mcmodsync.exe" apply-update --target "%TARGET%" --no-pause
set "RC=%errorlevel%"
if "%RC%"=="0" goto run_updater
:finish
echo.
if "%RC%"=="0" (
    echo === 更新完成，请手动启动 PCL2 或 HMCL ===
) else (
    echo === 更新失败，退出码 %RC%，日志见 _updater\logs\ ===
)
echo.%* | findstr /C:"--no-pause" >nul || pause
exit /b %RC%
