# Build FastETS for Windows (x64).
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Venv = Join-Path (Get-Location) ".venv-build"

if (-not (Test-Path $Venv)) {
    & $Python -m venv $Venv
}
& (Join-Path $Venv "Scripts\python.exe") -m pip install --upgrade pip
& (Join-Path $Venv "Scripts\python.exe") -m pip install -r requirements.txt pyinstaller

& (Join-Path $Venv "Scripts\pyinstaller.exe") --noconfirm --clean packaging\FastETS.spec

$Archive = "FastETS-Windows-x64.zip"
if (Test-Path $Archive) { Remove-Item $Archive -Force }
Compress-Archive -Path "dist\FastETS\*" -DestinationPath $Archive

Write-Host "built: $Archive"
