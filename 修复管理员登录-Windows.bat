@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到运行环境。请把本文件放进原系统文件夹，与启动预算统筹系统-Windows.bat放在一起。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\repair_admin.py
pause
