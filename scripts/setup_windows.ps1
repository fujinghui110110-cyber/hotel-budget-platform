$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$manifest = Get-Content (Join-Path $PSScriptRoot 'windows_dependencies.json') -Raw | ConvertFrom-Json
$offline = Join-Path (Get-Location) 'offline\windows'

function Assert-Success([string]$step) {
    if ($LASTEXITCODE -ne 0) { throw "$step failed (exit $LASTEXITCODE)" }
}
function Get-Installer($item) {
    New-Item -ItemType Directory -Force -Path $offline | Out-Null
    $path = Join-Path $offline $item.filename
    if (-not (Test-Path $path)) {
        Write-Host "Downloading official installer: $($item.filename)"
        $partial = "$path.partial"
        Invoke-WebRequest -UseBasicParsing -Uri $item.url -OutFile $partial
        if ((Get-FileHash $partial -Algorithm SHA256).Hash -ne $item.sha256) {
            Remove-Item $partial
            throw 'Installer checksum mismatch. Download rejected.'
        }
        Move-Item $partial $path
    }
    if ((Get-FileHash $path -Algorithm SHA256).Hash -ne $item.sha256) {
        throw "Installer checksum mismatch: $path. Remove this file and retry."
    }
    return $path
}
function Find-Python {
    $candidates = @("$env:LOCALAPPDATA\Programs\Python\Python313\python.exe", "$env:ProgramFiles\Python313\python.exe")
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        try {
            $detected = & py.exe -3.13 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { $candidates += $detected }
        } catch { }
    }
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($found -and $found.Source -notlike '*WindowsApps*') { $candidates += $found.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,13) and sys.maxsize > 2**32 else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}
function Find-LibreOffice {
    $paths = @($env:SOFFICE_BIN, "$env:ProgramFiles\LibreOffice\program\soffice.exe", "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe")
    $found = Get-Command soffice.exe -ErrorAction SilentlyContinue
    if ($found) { $paths += $found.Source }
    foreach ($path in $paths) { if ($path -and (Test-Path $path)) { return $path } }
    return $null
}
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
    throw 'This package supports Windows x64 only.'
}
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    $basePython = Find-Python
    if (-not $basePython) {
        $installer = Get-Installer $manifest.python
        Write-Host 'Installing Python 3.13 for this Windows user...'
        $process = Start-Process -FilePath $installer -ArgumentList '/passive InstallAllUsers=0 PrependPath=0 Include_launcher=1 Include_test=0' -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) { throw "Python installation failed: $($process.ExitCode)" }
        $basePython = Find-Python
        if (-not $basePython) { throw 'Python installation completed but Python 3.13 x64 was not found.' }
    }
    & $basePython -m venv .venv
    Assert-Success 'Python environment creation'
}
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
& $python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,13) else 1)'
Assert-Success 'Existing Python environment compatibility check'
$wheels = Join-Path $offline 'wheels'
if (Test-Path (Join-Path $wheels 'sha256.json')) {
    $hashes = Get-Content (Join-Path $wheels 'sha256.json') -Raw | ConvertFrom-Json
    foreach ($entry in $hashes.PSObject.Properties) {
        $wheel = Join-Path $wheels $entry.Name
        if (-not (Test-Path $wheel) -or (Get-FileHash $wheel -Algorithm SHA256).Hash -ne $entry.Value) { throw "Wheel missing or checksum mismatch: $($entry.Name)" }
    }
    & $python -m pip install --no-index --find-links $wheels -r requirements.txt
} else {
    & $python -m pip install -r requirements.txt
}
Assert-Success 'Dependency installation'
$soffice = Find-LibreOffice
if (-not $soffice) {
    $installer = Get-Installer $manifest.libreoffice
    Write-Host 'Installing LibreOffice. Windows may request administrator approval.'
    $approval = Read-Host 'LibreOffice requires Windows administrator approval. Install now? Type YES to continue'
  if ($approval -ne 'YES') { throw 'LibreOffice installation cancelled. Install manually and run setup again.' }
  $process = Start-Process -FilePath 'msiexec.exe' -Verb RunAs -ArgumentList @('/i', "`"$installer`"", '/passive', '/norestart') -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010)) { throw "LibreOffice installation failed: $($process.ExitCode)" }
    $soffice = Find-LibreOffice
    if (-not $soffice) { throw 'LibreOffice installation completed but soffice.exe was not found.' }
}
# Bundle the tunnel client so first public-link generation needs no software download.
$cloudflared = Join-Path (Get-Location) '.runtime\bin\cloudflared.exe'
if (-not (Test-Path $cloudflared)) {
    $tunnelInstaller = Get-Installer $manifest.cloudflared
    New-Item -ItemType Directory -Force -Path (Split-Path $cloudflared) | Out-Null
    Copy-Item $tunnelInstaller $cloudflared
}
$env:SOFFICE_BIN = $soffice
& $python scripts\setup_local.py
Assert-Success 'Local initialization'
Write-Host 'Setup complete. The one-click launcher will now open the system.'
