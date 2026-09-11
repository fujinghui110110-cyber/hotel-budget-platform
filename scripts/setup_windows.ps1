$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
function Assert-Success([string]$step) { if ($LASTEXITCODE -ne 0) { throw "$step failed (exit $LASTEXITCODE)" } }
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3.13 -m venv .venv
    Assert-Success 'Python 3.13 environment creation'
}
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
& $python -m pip install -r requirements.txt
Assert-Success 'Dependency installation'
$soffice = @("$env:ProgramFiles\LibreOffice\program\soffice.exe", "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $soffice) {
    $found = Get-Command soffice.exe -ErrorAction SilentlyContinue
    if ($found) { $soffice = $found.Source }
}
if (-not $soffice) { throw 'Install LibreOffice from https://www.libreoffice.org/download/download-libreoffice/ then run this installer again.' }
$env:SOFFICE_BIN = $soffice
& $python scripts\setup_local.py
Assert-Success 'Local initialization'
& $python scripts\local_server.py start --open --port 8768
Assert-Success 'Server startup'
