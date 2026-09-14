@echo off
setlocal EnableExtensions
net session >nul 2>&1
if errorlevel 1 (
  echo Need Administrator. Requesting elevation...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList '%*'"
  exit /b
)
set "PY32=%~dp0..\.python32\python.exe"
if not exist "%PY32%" (
  echo Missing: %PY32%
  pause
  exit /b 1
)
set "PYTHONPATH=%~dp0..;%~dp0..\qbot\engine\trade\easytrader"
cd /d "%~dp0.."
"%PY32%" -u "%~dp0auto_sell_morning.py" %*
set "ERR=%ERRORLEVEL%"
echo.
echo exit_code %ERR%
pause
exit /b %ERR%