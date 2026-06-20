# ============================
# MatchIT DEV Launcher (Start)
# ============================

$ErrorActionPreference = "Stop"

# >>> EDIT THIS PATH <<<
$PROJECT = "C:\Users\c_a_b\Desktop\MatchIT"

# Port/host
$APP_HOST = "127.0.0.1"
$APP_PORT = 5000


# Optional: auto-open browser
$OPEN_BROWSER = $true

# --- Helpers ---
function Write-Info($msg) { Write-Host "[MatchIT] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[MatchIT] $msg" -ForegroundColor Yellow }
function Write-Err ($msg) { Write-Host "[MatchIT] $msg" -ForegroundColor Red }

if (!(Test-Path $PROJECT)) { throw "Project folder not found: $PROJECT" }
Set-Location $PROJECT

if (!(Test-Path ".\app.py")) { throw "app.py not found in: $PROJECT" }

# Activate venv if present (preferred)
if (Test-Path ".\.venv\Scripts\Activate.ps1") {
  Write-Info "Activating venv: .\.venv"
  . .\.venv\Scripts\Activate.ps1
} elseif (Test-Path ".\venv\Scripts\Activate.ps1") {
  Write-Info "Activating venv: .\venv"
  . .\venv\Scripts\Activate.ps1
} else {
  Write-Warn "No venv found (.venv/venv). Using system Python."
}

# Ensure Flask can find app.py
# Use modern Flask CLI invocation: flask --app app run
$env:FLASK_APP = "app.py"
$env:FLASK_ENV = "development"
$env:FLASK_DEBUG = "1"

# Disable caching weirdness in dev (optional)
$env:PYTHONUNBUFFERED = "1"

Write-Info "Starting MatchIT DEV on http://$APP_HOST`:$APP_PORT"

Start-Process "http://$APP_HOST`:$APP_PORT"

flask --app app run --host $APP_HOST --port $APP_PORT


# Run Flask (this keeps the window open so you see logs)
# If Flask isn't installed in this environment, you'll get a clear error.
flask --app app run --host $HOST --port $PORT
