# Builds dist\MeshScope-<version>-win64.msi
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1 [-Publish]
#   -Publish  also uploads the installer to a GitHub Release (tools\publish_release.py)
param([switch]$Publish)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv\Scripts\python.exe)) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}
$py = Resolve-Path .venv\Scripts\python.exe
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt -r requirements-build.txt

if (-not (Test-Path assets\meshscope.ico)) { & $py tools\make_icon.py }

Write-Host "Running smoke test..."
& $py tests\smoke_test.py
if ($LASTEXITCODE -ne 0) { throw "Smoke test failed" }

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& $py setup.py bdist_msi
if ($LASTEXITCODE -ne 0) { throw "MSI build failed" }

Get-ChildItem dist\*.msi | ForEach-Object { Write-Host "Built $($_.FullName) ($([math]::Round($_.Length/1MB,1)) MB)" }

# An .msi always shows the Windows Installer icon, so also wrap it in a setup .exe with the MeshScope icon
& $py tools\make_setup_exe.py
if ($LASTEXITCODE -ne 0) { throw "Setup exe build failed" }

if ($Publish) {
    & $py tools\publish_release.py
    if ($LASTEXITCODE -ne 0) { throw "Publishing failed" }
}
