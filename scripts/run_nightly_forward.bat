@echo off
setlocal
cd /d D:\project\Qbot-main
set PYTHONPATH=D:\project\Qbot-main
set PYTHONIOENCODING=utf-8
set LOG=D:\project\Qbot-main\qbot\gui\csv\nightly_forward.log
echo ===== START %DATE% %TIME% =====>> %LOG%
D:\anaconda3\envs\Qbot\python.exe -u D:\project\Qbot-main\scripts\refresh_forward_watch.py >> %LOG% 2>&1
D:\anaconda3\envs\Qbot\python.exe -u D:\project\Qbot-main\scripts\refresh_daily_news.py >> %LOG% 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> %LOG%
endlocal
