@echo off
set "APP_ROOT=%~dp0"
set "PYTHONPATH=%APP_ROOT%src"
if not exist "%APP_ROOT%.venv\Scripts\pythonw.exe" (
  echo Python environment not found. Please install requirements first.
  pause
  exit /b 1
)
start "" "%APP_ROOT%.venv\Scripts\pythonw.exe" "%APP_ROOT%main.py"
