@echo off
REM Qbot ML 周日补训 22:00：仅当本周五未成功周训时才训练
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
echo ===== ML SUNDAY CATCHUP %DATE% %TIME% root=%CD% py=%QBOT_PY% =====>> "%LOG%"
"%QBOT_PY%" -u "%CD%\scripts\train_forward_gbdt.py" --from-store --catchup-missed-friday >> "%LOG%" 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> "%LOG%"
endlocal
