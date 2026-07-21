@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

title DOOM Eternal Archipelago RPC Client

set "DOOM_INPUT="
set "DOOM_ROOT="
set "DOOM_BASE="
set "CONFIG_PATH=%~dp0ap_config.json"

echo.
echo ==========================================
echo  DOOM Eternal Archipelago RPC Client
echo ==========================================
echo.

rem ============================================================
rem Read doom_base_dir from ap_config.json without CMD pipe issues
rem ============================================================

if not exist "%CONFIG_PATH%" goto PromptForPath

set "CONFIG_RESULT=%TEMP%\doom_ap_config_%RANDOM%_%RANDOM%.txt"

if exist "%CONFIG_RESULT%" (
    del /q "%CONFIG_RESULT%" >nul 2>&1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $jsonText = Get-Content -LiteralPath $env:CONFIG_PATH -Raw; $config = ConvertFrom-Json -InputObject $jsonText; $value = [string]$config.doom_base_dir; [System.IO.File]::WriteAllText($env:CONFIG_RESULT, $value) } catch { [System.IO.File]::WriteAllText($env:CONFIG_RESULT, '__INVALID_JSON__') }"

if not exist "%CONFIG_RESULT%" (
    echo.
    echo ERROR: PowerShell did not produce a configuration result.
    echo Config file:
    echo   %CONFIG_PATH%
    echo.
    pause
    exit /b 1
)

set "DOOM_INPUT="
set /p "DOOM_INPUT="<"%CONFIG_RESULT%"

del /q "%CONFIG_RESULT%" >nul 2>&1

if /I "!DOOM_INPUT!"=="__INVALID_JSON__" goto InvalidJson

if not defined DOOM_INPUT (
    echo.
    echo ap_config.json exists, but doom_base_dir is missing or empty.
    echo.
    goto PromptForPath
)

echo Path read from ap_config.json:
echo   !DOOM_INPUT!
echo.

call :ResolveDoomPath "!DOOM_INPUT!"

if defined DOOM_ROOT goto StartClient

echo The doom_base_dir stored in ap_config.json could not be validated:
echo   !DOOM_INPUT!
echo.
goto PromptForPath


rem ============================================================
rem Manual path prompt
rem ============================================================

:PromptForPath
echo Enter the DOOM Eternal installation path.
echo.
echo Valid examples:
echo   D:\SteamLibrary\steamapps\common\DOOMEternal
echo   D:\SteamLibrary\steamapps\common\DOOMEternal\base
echo.
echo Invalid example:
echo   D:\SteamLibrary\steamapps\common\base
echo.

set "DOOM_INPUT="
set /p "DOOM_INPUT=Path: "

if not defined DOOM_INPUT goto Cancelled

call :ResolveDoomPath "!DOOM_INPUT!"

if not defined DOOM_ROOT goto InvalidPath
goto StartClient


rem ============================================================
rem Start client
rem ============================================================

:StartClient
if not exist "%~dp0ap_client.exe" goto MissingClient

echo.
echo DOOM Eternal installation validated.
echo.
echo Game root:
echo   !DOOM_ROOT!
echo.
echo Game base:
echo   !DOOM_BASE!
echo.
echo Starting ap_client.exe...
echo.

"%~dp0ap_client.exe" "!DOOM_ROOT!"

set "CLIENT_EXIT_CODE=!ERRORLEVEL!"

echo.
echo ap_client.exe exited with code !CLIENT_EXIT_CODE!.
echo.

if not "!CLIENT_EXIT_CODE!"=="0" (
    echo The RPC client returned an error.
    echo Copy the messages above when reporting the problem.
    echo.
)

pause
exit /b !CLIENT_EXIT_CODE!


rem ============================================================
rem Resolve either DOOMEternal or DOOMEternal\base
rem ============================================================

:ResolveDoomPath
set "CANDIDATE=%~1"
set "DOOM_ROOT="
set "DOOM_BASE="

if not defined CANDIDATE exit /b 1

rem Normalize to an absolute Windows path.
for %%D in ("!CANDIDATE!") do set "CANDIDATE=%%~fD"

rem Case 1: user selected ...\DOOMEternal
if exist "!CANDIDATE!\DOOMEternalx64vk.exe" (
    if exist "!CANDIDATE!\base\classicwads" (
        set "DOOM_ROOT=!CANDIDATE!"
        set "DOOM_BASE=!CANDIDATE!\base"
        exit /b 0
    )
)

rem Case 2: user selected ...\DOOMEternal\base
if exist "!CANDIDATE!\classicwads" (
    if exist "!CANDIDATE!\..\DOOMEternalx64vk.exe" (
        set "DOOM_BASE=!CANDIDATE!"
        for %%D in ("!CANDIDATE!\..") do set "DOOM_ROOT=%%~fD"
        exit /b 0
    )
)

exit /b 1


rem ============================================================
rem Errors
rem ============================================================

:InvalidJson
echo.
echo ERROR: ap_config.json is invalid JSON.
echo.
echo Use forward slashes:
echo   D:/SteamLibrary/steamapps/common/DOOMEternal/base
echo.
echo Or escaped backslashes:
echo   D:\\SteamLibrary\\steamapps\\common\\DOOMEternal\\base
echo.
pause
exit /b 1

:InvalidPath
echo.
echo ERROR: Could not validate the DOOM Eternal installation.
echo.
echo Expected structure:
echo.
echo   DOOMEternal\
echo     DOOMEternalx64vk.exe
echo     base\
echo       classicwads\
echo.
echo Select either:
echo   ...\DOOMEternal
echo.
echo Or:
echo   ...\DOOMEternal\base
echo.
pause
exit /b 1

:MissingClient
echo.
echo ERROR: ap_client.exe was not found.
echo.
echo Expected:
echo   %~dp0ap_client.exe
echo.
pause
exit /b 1

:Cancelled
echo.
echo Setup cancelled because no path was entered.
echo.
pause
exit /b 1
