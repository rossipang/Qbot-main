@echo off
setlocal
cd /d D:\project\Qbot-main
set PYTHONPATH=D:\project\Qbot-main
set PYTHONIOENCODING=utf-8
echo ===== START %DATE% %TIME% =====>> D:\project\Qbot-main\qbot\gui\csv\price_watch_run.log
D:\anaconda3\python.exe -u D:\project\Qbot-main\scripts\price_watch_wechat.py >> D:\project\Qbot-main\qbot\gui\csv\price_watch_run.log 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> D:\project\Qbot-main\qbot\gui\csv\price_watch_run.log
endlocal
