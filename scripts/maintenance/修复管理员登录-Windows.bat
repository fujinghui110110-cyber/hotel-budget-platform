@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo 未找到运行环境。请先双击系统根目录的一键启动-Windows.bat。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\repair_admin.py
pause
