@echo off
REM Resolve Qbot python for home/office. Sets QBOT_PY.
set "QBOT_PY="
if exist "D:\anaconda3\envs\Qbot\python.exe" set "QBOT_PY=D:\anaconda3\envs\Qbot\python.exe"
if not defined QBOT_PY if exist "D:\miniforge3\envs\Qbot\python.exe" set "QBOT_PY=D:\miniforge3\envs\Qbot\python.exe"
if not defined QBOT_PY if exist "E:\miniforge3\envs\Qbot\python.exe" set "QBOT_PY=E:\miniforge3\envs\Qbot\python.exe"
if not defined QBOT_PY if exist "%LOCALAPPDATA%\miniconda3\envs\Qbot\python.exe" set "QBOT_PY=%LOCALAPPDATA%\miniconda3\envs\Qbot\python.exe"
if not defined QBOT_PY (
  where python >nul 2>&1 && for /f "delims=" %%i in ('where python') do (
    set "QBOT_PY=%%i"
    goto :found
  )
)
:found
if not defined QBOT_PY exit /b 1
exit /b 0
