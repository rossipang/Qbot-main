@echo off
setlocal
cd /d D:\project\Qbot-main
set PYTHONPATH=D:\project\Qbot-main
set PYTHONIOENCODING=utf-8
set LOG=D:\project\Qbot-main\qbot\gui\csv\nightly_forward.log
echo ===== START %DATE% %TIME% =====>> %LOG%
REM ML 日更/周训已改由计划任务：工作日16:05 / 周五22:00 / 周日补训22:00
REM （scripts\install_ml_schedulers.ps1）；此处只刷前瞻与新闻，避免重复重训
D:\anaconda3\envs\Qbot\python.exe -u D:\project\Qbot-main\scripts\refresh_forward_watch.py >> %LOG% 2>&1
D:\anaconda3\envs\Qbot\python.exe -u D:\project\Qbot-main\scripts\refresh_daily_news.py >> %LOG% 2>&1
echo ===== EXIT %ERRORLEVEL% %DATE% %TIME% =====>> %LOG%
endlocal
