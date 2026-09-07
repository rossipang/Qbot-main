@echo off
REM Qbot ML 日更：工作日 16:00 收盘后拉当日日K+资金流+主题板 → SQLite
setlocal
cd /d D:\project\Qbot-main
set PYTHONPATH=D:\project\Qbot-main
set PYTHONIOENCODING=utf-8
set LOG=D:\project\Qbot-main\qbot\gui\csv\ml_schedule.log
echo ===== ML DAILY %DATE% %TIME% =====>> %LOG%
D:\anaconda3\envs\Qbot\python.exe -u D:\project\Qbot-main\scripts\refresh_ml_factor_store.py >> %LOG% 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> %LOG%
endlocal
