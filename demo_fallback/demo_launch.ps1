# One-click start for the demo (D17 allows launchers outside probe/ and web/).
#   demo_launch.ps1            -> TaskBoard (8765) + the labelled REPLAY page (8001). Costs nothing, needs no API key.
#   demo_launch.ps1 -Live      -> also the LIVE page (8000): REAL API calls, about 5 US cents per run, needs .env with the key.
#   demo_launch.ps1 -NoBrowser -> start the servers only (used by tests).
# Stop everything with demo_stop.ps1 (it stops by PORT, never by program name).
param([switch]$Live, [switch]$NoBrowser)

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Cannot find $py" -ForegroundColor Red; exit 1 }

function Test-Listening($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }
function Wait-Http($url, $seconds = 30) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { Start-Sleep -Milliseconds 400 }
    }
    return $false
}

$wanted = @(8765, 8001) + $(if ($Live) { 8000 } else { @() })
$busy = $wanted | Where-Object { Test-Listening $_ }
if ($busy) {
    Write-Host ("Port(s) already in use: " + ($busy -join ", ") + ". Run demo_stop first (or it is already running).") -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting TaskBoard (the app being tested) on http://127.0.0.1:8765/ ..."
Start-Process $py -ArgumentList "-m", "uvicorn", "demo_app.server:app", "--port", "8765", "--log-level", "warning" -WorkingDirectory $root -WindowStyle Minimized
if (-not (Wait-Http "http://127.0.0.1:8765/")) { Write-Host "TaskBoard did not start." -ForegroundColor Red; exit 1 }

Write-Host "Starting the REPLAY page (a recording of a real run, no API calls) on http://127.0.0.1:8001/ ..."
Start-Process $py -ArgumentList "demo_fallback\start_replay.py" -WorkingDirectory $root -WindowStyle Minimized
if (-not (Wait-Http "http://127.0.0.1:8001/api/status")) { Write-Host "REPLAY page did not start." -ForegroundColor Red; exit 1 }

if ($Live) {
    Write-Host "Starting the LIVE page on http://127.0.0.1:8000/ (REAL API calls) ..." -ForegroundColor Yellow
    $env:PROBE_LLM_MODE = "record"
    $env:PROBE_WEB_YES_SPEND = "1"
    Start-Process $py -ArgumentList "-m", "uvicorn", "web.server:app", "--port", "8000", "--log-level", "warning" -WorkingDirectory $root -WindowStyle Minimized
    if (-not (Wait-Http "http://127.0.0.1:8000/api/status")) { Write-Host "LIVE page did not start (is ANTHROPIC_API_KEY in .env?)." -ForegroundColor Red; exit 1 }
}

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8765/"
    Start-Process "http://127.0.0.1:8001/"
    if ($Live) { Start-Process "http://127.0.0.1:8000/" }
}

Write-Host ""
Write-Host "Ready:" -ForegroundColor Green
Write-Host "  TaskBoard (the app being tested)   http://127.0.0.1:8765/            (clean copy: http://127.0.0.1:8765/?bugs=off)"
Write-Host "  REPLAY (recording, free)           http://127.0.0.1:8001/            -> in 'Advanced' type /__reset, then Run test"
if ($Live) { Write-Host "  LIVE (real API, ~5 cents a run)    http://127.0.0.1:8000/            -> in 'Advanced' type /__reset, URL http://127.0.0.1:8765/, then Run test" }
Write-Host "Stop everything with demo_stop.bat."
