@echo off
REM Qbot ML 周五周训 22:00：强制从因子库重训 GBDT
setlocal
cd /d D:\project\Qbot-main
set PYTHONPATH=D:\project\Qbot-main
set PYTHONIOENCODING=utf-8
set LOG=D:\project\Qbot-main\qbot\gui\csv\ml_schedule.log
echo ===== ML FRIDAY TRAIN %DATE% %TIME% =====>> %LOG%
D:\anaconda3\envs\Qbot\python.exe -u D:\project\Qbot-main\scripts\train_forward_gbdt.py --from-store >> %LOG% 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> %LOG%
endlocal
