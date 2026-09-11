@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\local_server.py" start --open --port 8768
if errorlevel 1 pause
