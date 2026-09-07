@echo off
REM Qbot ML 周日补训 22:00：仅当本周五未成功周训时才训练
setlocal
cd /d D:\project\Qbot-main
set PYTHONPATH=D:\project\Qbot-main
set PYTHONIOENCODING=utf-8
set LOG=D:\project\Qbot-main\qbot\gui\csv\ml_schedule.log
echo ===== ML SUNDAY CATCHUP %DATE% %TIME% =====>> %LOG%
D:\anaconda3\envs\Qbot\python.exe -u D:\project\Qbot-main\scripts\train_forward_gbdt.py --from-store --catchup-missed-friday >> %LOG% 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> %LOG%
endlocal
