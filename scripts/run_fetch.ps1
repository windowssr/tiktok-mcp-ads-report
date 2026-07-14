# Launch the interactive TikTok ads fetch menu with conda env.
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\run_fetch.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\run_fetch.ps1 -ProxyPort 7890
#   powershell -ExecutionPolicy Bypass -File .\scripts\run_fetch.ps1 -Once last_7_days

param(
    [string]$EnvName = "tiktok-mcp",
    [string]$ProxyPort = "7890",
    [string]$Once = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$proxy = "http://127.0.0.1:$ProxyPort"
$argsList = @(
    "run", "-n", $EnvName, "--cwd", $RepoRoot,
    "python", "fetch_ads.py",
    "--proxy", $proxy
)

if ($Once) {
    $argsList += @("--once", $Once, "--only-spend")
}

Write-Host "Proxy: $proxy"
Write-Host "Env:   $EnvName"
Write-Host ""
& conda @argsList
