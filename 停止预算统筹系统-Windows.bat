@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\stop_all.py"
if errorlevel 1 pause
