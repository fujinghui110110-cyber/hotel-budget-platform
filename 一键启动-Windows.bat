@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 首次使用，正在准备预算系统……
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup_windows.ps1"
  if errorlevel 1 goto failed
)
echo 正在启动预算系统，准备好后会自动打开网页……
".venv\Scripts\python.exe" "scripts\local_server.py" start --open %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动未完成，请查看上面的提示和 logs\server.log。
pause
exit /b 1
