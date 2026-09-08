@echo off
REM Qbot ML 日更：工作日 16:05 收盘后拉当日日K+资金流+主题板 → SQLite
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%
set PYTHONIOENCODING=utf-8
set LOG=%CD%\qbot\gui\csv\ml_schedule.log
call "%~dp0_ml_resolve_python.bat"
if errorlevel 1 (
  echo [ERR] no Qbot python >> "%LOG%"
  exit /b 1
)
echo ===== ML DAILY %DATE% %TIME% root=%CD% py=%QBOT_PY% =====>> "%LOG%"
"%QBOT_PY%" -u "%CD%\scripts\refresh_ml_factor_store.py" >> "%LOG%" 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> "%LOG%"
endlocal
