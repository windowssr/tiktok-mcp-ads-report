# Create / refresh the conda env for official-tiktok-mcp-client.
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup-conda.ps1
# Optional:
#   .\scripts\setup-conda.ps1 -EnvName tiktok-mcp -Force

param(
    [string]$EnvName = "tiktok-mcp",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$condaCmd = Get-Command conda -ErrorAction SilentlyContinue
if (-not $condaCmd) {
    throw "conda not found. Install Miniconda/Anaconda and ensure conda is on PATH."
}

$pattern = "^" + [regex]::Escape($EnvName) + "\s"
$envExists = conda env list | Select-String -Pattern $pattern

if ($envExists -and $Force) {
    Write-Host "Removing existing env: $EnvName"
    conda env remove -n $EnvName -y
    $envExists = $null
}

if (-not $envExists) {
    Write-Host "Creating conda env: $EnvName (Python 3.11)"
    conda create -n $EnvName python=3.11 pip -y
} else {
    Write-Host "Reusing existing conda env: $EnvName"
}

Write-Host "Installing project into env $EnvName ..."
conda run -n $EnvName python -m pip install -U pip
conda run -n $EnvName python -m pip install -e ".[dev]"

Write-Host ""
Write-Host "Done. Common commands:"
Write-Host "  conda activate $EnvName"
Write-Host "  tiktok-mcp-client --help"
Write-Host ""
Write-Host "Or without activate:"
Write-Host "  conda run -n $EnvName tiktok-mcp-client auth --proxy http://127.0.0.1:7890"
