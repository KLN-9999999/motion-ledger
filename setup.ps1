$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Api = Join-Path $Root 'apps/api'
$Web = Join-Path $Root 'apps/web'
if (-not (Test-Path (Join-Path $Root '.env'))) { Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env') }

if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Python launcher not found. Install Python 3.12 first.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js not found. Install Node.js 20.17+ or 22.9+ first.' }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw 'npm not found. Reinstall Node.js with npm included.' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
  Write-Warning 'ffmpeg/ffprobe are not on PATH. Video upload and normalization will not work until they are installed.'
}

$VenvPython = Join-Path $Api '.venv/Scripts/python.exe'
if (-not (Test-Path $VenvPython)) { py -3.12 -m venv (Join-Path $Api '.venv') }
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e "${Api}[dev]"
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }

Push-Location $Web
try { npm install; if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' } }
finally { Pop-Location }

Write-Host ''
Write-Host 'MotionLedger dependencies installed.' -ForegroundColor Green
Write-Host 'Copy .env.example to .env and set MODEL_API_KEY for live Meta analysis.'
Write-Host 'Start the app with .\start.ps1'
