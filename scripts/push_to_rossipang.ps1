# Push this workspace to https://github.com/rossipang/Qbot-main (private).
# GitHub no longer accepts account passwords for git/API — use a Personal Access Token.
#
# 1) Browser: https://github.com/settings/tokens  → Generate new token (classic)
#    scopes: repo
# 2) Browser: create empty private repo Qbot-main under rossipang (no README)
#    https://github.com/new
# 3) PowerShell:
#    $env:GITHUB_TOKEN = 'ghp_xxxx'
#    .\scripts\push_to_rossipang.ps1

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

# Create private repo if missing
try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$RepoName" -Headers $headers -Method Get | Out-Null
    Write-Host "Repo exists: $Owner/$RepoName"
} catch {
    $body = @{
        name       = $RepoName
        private    = $true
        auto_init  = $false
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

# Push with token in URL once (not stored in gitconfig permanently beyond remote URL without token)
$pushUrl = "https://${Owner}:${Token}@github.com/${Owner}/${RepoName}.git"
git push -u $pushUrl main
git remote set-url origin $originUrl
Write-Host "Done. Clone at home: git clone $originUrl"
Write-Host "Remote origin has NO token saved."
