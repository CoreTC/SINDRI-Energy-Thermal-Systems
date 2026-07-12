# Télécharge LibreHardwareMonitorLib.dll depuis les releases officielles.
# Requis pour SINDRI (accès direct aux capteurs hardware via pythonnet).
# Usage : clic-droit → Exécuter avec PowerShell   ou   powershell -File Download-LHM.ps1

$ErrorActionPreference = 'Stop'
$target = Join-Path $PSScriptRoot 'LibreHardwareMonitor'
$zipUrl = 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest/download/LibreHardwareMonitor-net472.zip'

Write-Host "SINDRI - Telechargement LibreHardwareMonitor" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

if (Test-Path (Join-Path $target 'LibreHardwareMonitorLib.dll')) {
    Write-Host "[OK] LibreHardwareMonitorLib.dll deja present : $target" -ForegroundColor Green
    exit 0
}

New-Item -ItemType Directory -Force -Path $target | Out-Null
$tmpZip = Join-Path $env:TEMP "lhm-sindri.zip"
Write-Host "Telechargement depuis GitHub..." -ForegroundColor Yellow
try {
    Invoke-WebRequest -Uri $zipUrl -OutFile $tmpZip -UseBasicParsing
    Write-Host "[OK] ZIP telecharge ($([math]::Round((Get-Item $tmpZip).Length/1MB,1)) MB)" -ForegroundColor Green
} catch {
    Write-Host "[X] Echec du telechargement : $_" -ForegroundColor Red
    Write-Host "    Telecharge manuellement : https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases" -ForegroundColor Yellow
    Write-Host "    Puis extrais LibreHardwareMonitorLib.dll dans $target" -ForegroundColor Yellow
    exit 1
}

Write-Host "Extraction..." -ForegroundColor Yellow
Expand-Archive -Path $tmpZip -DestinationPath $target -Force
Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue

if (Test-Path (Join-Path $target 'LibreHardwareMonitorLib.dll')) {
    Write-Host "[OK] LibreHardwareMonitor installe : $target" -ForegroundColor Green
} else {
    Write-Host "[X] LibreHardwareMonitorLib.dll introuvable apres extraction" -ForegroundColor Red
    exit 1
}
