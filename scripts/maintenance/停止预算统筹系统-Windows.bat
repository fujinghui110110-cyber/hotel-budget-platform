@echo off
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" "scripts\stop_all.py"
if errorlevel 1 pause
