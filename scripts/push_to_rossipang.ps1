# Retry push when GitHub network works.
# Usage:
#   $env:GITHUB_TOKEN = 'ghp_xxx'
#   .\scripts\push_to_rossipang.ps1

param(
    [string]$RepoName = "Qbot-main",
    [string]$Owner = "rossipang",
    [string]$Token = $env:GITHUB_TOKEN
)

$ErrorActionPreference = "Stop"
if (-not $Token) {
    Write-Error "Set env GITHUB_TOKEN to a GitHub PAT (repo scope). Account password will not work."
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${Owner}:${Token}"))
$headers = @{
    Authorization = "Basic $pair"
    Accept        = "application/vnd.github+json"
    "User-Agent"  = "Qbot-sync"
}

try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName" -Headers $headers -Method Get | Out-Null
    Write-Host "Repo exists: $Owner/$RepoName"
} catch {
    $body = @{
        name        = $RepoName
        private     = $true
        auto_init   = $false
        description = "Personal Qbot fork for home/office sync (not upstream)"
    } | ConvertTo-Json
    Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Headers $headers -Method Post -Body $body -ContentType "application/json"
    Write-Host "Created private repo $Owner/$RepoName"
}

$originUrl = "https://github.com/$Owner/$RepoName.git"
$remotes = git remote
if ($remotes -match '^origin$') {
    git remote set-url origin $originUrl
} else {
    git remote add origin $originUrl
}

$env:GIT_HTTP_LOW_SPEED_LIMIT = "1000"
$env:GIT_HTTP_LOW_SPEED_TIME = "600"
$pushUrl = "https://${Owner}:${Token}@github.com/${Owner}/${RepoName}.git"
git push -u $pushUrl main
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
git remote set-url origin $originUrl
Write-Host "Done. Home: git clone $originUrl"
Write-Host "Remote origin has NO token saved. Revoke this PAT if it was pasted in chat."
