#Requires -Version 5.1
<#
.SYNOPSIS
  Register Qbot ML scheduled tasks (current user).

.DESCRIPTION
  - Weekdays 16:05  daily factor refresh
  - Friday   22:00  weekly GBDT train
  - Sunday   22:00  catch-up if Friday train missed

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_ml_schedulers.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_ml_schedulers.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    # scripts/ 的上一级即为仓库根；兼容家里 D:\project 与公司 E:\projects
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$tasks = @(
    @{
        Name = "QbotML_DailyRefresh"
        Bat  = Join-Path $ProjectRoot "scripts\run_ml_daily_refresh.bat"
        CreateArgs = "/SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:05"
    },
    @{
        Name = "QbotML_FridayTrain"
        Bat  = Join-Path $ProjectRoot "scripts\run_ml_friday_train.bat"
        CreateArgs = "/SC WEEKLY /D FRI /ST 22:00"
    },
    @{
        Name = "QbotML_SundayCatchup"
        Bat  = Join-Path $ProjectRoot "scripts\run_ml_sunday_catchup.bat"
        CreateArgs = "/SC WEEKLY /D SUN /ST 22:00"
    }
)

function Remove-QbotMlTask([string]$name) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    cmd.exe /c "schtasks /Query /TN `"$name`" >nul 2>&1"
    if ($LASTEXITCODE -eq 0) {
        cmd.exe /c "schtasks /Delete /TN `"$name`" /F >nul 2>&1"
        Write-Host "Removed $name"
    }
    $ErrorActionPreference = $prev
}

foreach ($t in $tasks) {
    Remove-QbotMlTask $t.Name
}

if ($Uninstall) {
    Write-Host "Uninstalled Qbot ML scheduled tasks."
    exit 0
}

foreach ($t in $tasks) {
    if (-not (Test-Path -LiteralPath $t.Bat)) {
        throw "Missing bat: $($t.Bat)"
    }
    $tr = 'cmd.exe /c "' + $t.Bat + '"'
    $argLine = '/Create /TN "' + $t.Name + '" /TR "' + $tr + '" ' + $t.CreateArgs + ' /RL LIMITED /F'
    Write-Host (">> schtasks " + $argLine)
    $p = Start-Process -FilePath "schtasks.exe" -ArgumentList $argLine -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0) {
        throw ("schtasks failed for " + $t.Name + " exit=" + $p.ExitCode)
    }
    Write-Host ("Registered " + $t.Name)
}

Write-Host ""
Write-Host "Done. Verify: schtasks /Query /TN QbotML_DailyRefresh"
$logPath = Join-Path $ProjectRoot "qbot\gui\csv\ml_schedule.log"
Write-Host ("Logs: " + $logPath)
Write-Host "Sunday 22:00 catches up only if Friday weekly train was missed."
