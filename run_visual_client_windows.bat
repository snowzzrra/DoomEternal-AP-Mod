@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run setup_windows_client.bat first.
    pause
    exit /b 1
)

if not exist "ap_config.json" (
    echo First launch: configure the requested Archipelago, game, and save paths.
    ".venv\Scripts\python.exe" bridge_client.py %*
    exit /b %errorlevel%
)

start "" ".venv\Scripts\pythonw.exe" bridge_client.py %*
