# ============================================================
# MatchIT — Activate VENV (Safe / One Click)
# ============================================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "======================================="
Write-Host " MatchIT — VENV ACTIVATION"
Write-Host "======================================="
Write-Host ""

# --- Project folder (AUTO = current folder)
$PROJECT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path $PROJECT_DIR)) {
    Write-Host "❌ Project folder not found:" -ForegroundColor Red
    Write-Host $PROJECT_DIR
    pause
    exit
}

Set-Location $PROJECT_DIR
Write-Host "📁 Project:" $PROJECT_DIR

# --- VENV path
$VENV_PATH = Join-Path $PROJECT_DIR ".venv"

if (-not (Test-Path $VENV_PATH)) {
    Write-Host ""
    Write-Host "❌ .venv not found here:" -ForegroundColor Red
    Write-Host $VENV_PATH
    Write-Host ""
    Write-Host "Did you create it with:"
    Write-Host "python -m venv .venv"
    pause
    exit
}

# --- Activate
$ACTIVATE = Join-Path $VENV_PATH "Scripts\Activate.ps1"

Write-Host ""
Write-Host "⚡ Activating virtual environment..."
& $ACTIVATE

Write-Host ""
Write-Host "✅ VENV ACTIVE"
Write-Host ""

python -V
Write-Host ""

Write-Host "Installed torch device check:"
python - << 'EOF'
import torch
print("cuda:", torch.cuda.is_available())
print("device:", "cuda" if torch.cuda.is_available() else "cpu")
EOF

Write-Host ""
Write-Host "Ready to run MatchIT 👍"
Write-Host ""
