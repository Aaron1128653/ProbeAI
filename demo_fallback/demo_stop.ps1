# Stops the demo servers by PORT (8000 live, 8001 replay, 8765 TaskBoard), never by program name,
# because other Python work may be running on this machine (CLAUDE.md process-safety rule).
$ports = 8000, 8001, 8765
$listeners = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue
if (-not $listeners) { Write-Host "Nothing is listening on 8000 / 8001 / 8765."; exit 0 }
foreach ($l in $listeners) {
    $proc = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -match "python") {
        Stop-Process -Id $l.OwningProcess -Force
        Write-Host ("Stopped port {0} (pid {1}, {2})" -f $l.LocalPort, $l.OwningProcess, $proc.ProcessName)
    } else {
        Write-Host ("Port {0} is held by '{1}', not Python: left alone." -f $l.LocalPort, $(if ($proc) { $proc.ProcessName } else { "unknown" })) -ForegroundColor Yellow
    }
}
Start-Sleep -Seconds 1
$left = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue
if ($left) { Write-Host ("Still listening: " + (($left | ForEach-Object LocalPort) -join ", ")) -ForegroundColor Yellow } else { Write-Host "All demo ports are free." -ForegroundColor Green }
