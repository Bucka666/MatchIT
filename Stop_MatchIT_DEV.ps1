# ============================
# MatchIT DEV Launcher (Stop)
# ============================

$ErrorActionPreference = "SilentlyContinue"

$PORT = 5000

function Write-Info($msg) { Write-Host "[MatchIT] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[MatchIT] $msg" -ForegroundColor Yellow }

Write-Info "Stopping anything bound to port $PORT ..."

# Find PID using port (netstat)
$lines = netstat -ano | Select-String ":$PORT" | ForEach-Object { $_.Line } | Select-Object -Unique
if (-not $lines) {
  Write-Warn "No process found on port $PORT"
  exit 0
}

$pids = @()
foreach ($l in $lines) {
  $parts = ($l -split "\s+") | Where-Object { $_ -ne "" }
  $pid = $parts[-1]
  if ($pid -match "^\d+$") { $pids += [int]$pid }
}
$pids = $pids | Select-Object -Unique

foreach ($pid in $pids) {
  Write-Info "Killing PID $pid"
  Stop-Process -Id $pid -Force
}

Write-Info "Done."
