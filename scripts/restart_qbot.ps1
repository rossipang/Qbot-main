# 重启 Qbot GUI：先杀旧进程 → 刷新前瞻缓存 → 只启动一个实例
$ErrorActionPreference = 'Continue'
$Root = if ($PSScriptRoot) {
    Split-Path -Parent $PSScriptRoot
} else {
    'E:\projects\Qbot-main'
}

if ($env:QBOT_PYTHON -and (Test-Path -LiteralPath $env:QBOT_PYTHON)) {
    $Python = $env:QBOT_PYTHON
} elseif (Test-Path -LiteralPath 'D:\anaconda3\envs\Qbot\python.exe') {
    $Python = 'D:\anaconda3\envs\Qbot\python.exe'
} elseif (Test-Path -LiteralPath 'D:\miniforge3\envs\Qbot\python.exe') {
    $Python = 'D:\miniforge3\envs\Qbot\python.exe'
} else {
    $Python = 'python'
}

Write-Output "Root=$Root"
Write-Output "Python=$Python"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'main\.py' } |
    ForEach-Object {
        Write-Output ("Stopping PID {0}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 2

$env:PYTHONPATH = $Root
$env:PYTHONIOENCODING = 'utf-8'

$refresh = Join-Path $Root 'scripts\refresh_forward_watch.py'
if (Test-Path -LiteralPath $refresh) {
    Write-Output 'Refreshing forward watch before GUI start...'
    & $Python -u $refresh
    if ($LASTEXITCODE -ne 0) {
        Write-Output ("WARN: forward refresh exit code {0}" -f $LASTEXITCODE)
    }
} else {
    Write-Output ("WARN: refresh script missing: {0}" -f $refresh)
}

$warm = Join-Path $Root 'scripts\warm_sanlianyang.py'
if (Test-Path -LiteralPath $warm) {
    Write-Output 'Warming 三连阳 cache before GUI start (约1分钟)...'
    & $Python -u $warm
    if ($LASTEXITCODE -ne 0) {
        Write-Output ("WARN: 三连阳 warm exit code {0}" -f $LASTEXITCODE)
    }
} else {
    Write-Output ("WARN: warm script missing: {0}" -f $warm)
}

$mainPy = Join-Path $Root 'main.py'
Start-Process -FilePath $Python -ArgumentList $mainPy -WorkingDirectory $Root

Start-Sleep -Seconds 3
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'main\.py' })
Write-Output ("Qbot started: {0} instance(s)" -f $running.Count)
