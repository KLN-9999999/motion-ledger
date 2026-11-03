$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Api = Join-Path $Root 'apps/api'
$Web = Join-Path $Root 'apps/web'
$VenvPython = Join-Path $Api '.venv/Scripts/python.exe'

if (-not (Test-Path $VenvPython)) { throw 'Backend virtual environment not found. Run .\setup.ps1 first.' }
if (-not (Test-Path (Join-Path $Web 'node_modules'))) { throw 'Frontend dependencies not found. Run .\setup.ps1 first.' }

$ApiCommand = "Set-Location '$Api'; & '.\.venv\Scripts\python.exe' -m uvicorn motionledger.main:app --reload --port 8000"
$WebCommand = "Set-Location '$Web'; npm run dev"
Start-Process powershell -ArgumentList '-NoExit','-ExecutionPolicy','Bypass','-Command',$ApiCommand
Start-Process powershell -ArgumentList '-NoExit','-ExecutionPolicy','Bypass','-Command',$WebCommand
Start-Sleep -Seconds 2
Start-Process 'http://localhost:3000'
Write-Host 'MotionLedger API: http://localhost:8000/docs'
Write-Host 'MotionLedger UI:  http://localhost:3000'
