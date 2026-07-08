@echo off
setlocal
cd /d "%~dp0"

set /p AP_PATH=Path to the Archipelago 0.6.8 folder containing CommonClient.py: 
if not exist "%AP_PATH%\CommonClient.py" (
    echo CommonClient.py was not found in "%AP_PATH%".
    pause
    exit /b 1
)

py -3.11 -m venv .venv
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" -m pip install -r "%AP_PATH%\requirements.txt" -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Client environment created successfully.
echo Run run_visual_client_windows.bat next.
pause
exit /b 0

:failed
echo.
echo Client setup failed. Review the error above.
pause
exit /b 1
