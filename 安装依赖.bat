@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PS_ARGS="

:parse_args
if "%~1"=="" goto :run_script
if /i "%~1"=="--check-only" set "PS_ARGS=%PS_ARGS% -CheckOnly"
if /i "%~1"=="--no-pause" set "PS_ARGS=%PS_ARGS% -NoPause"
shift
goto :parse_args

:run_script
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_dependencies.ps1" %PS_ARGS%
exit /b %errorlevel%
