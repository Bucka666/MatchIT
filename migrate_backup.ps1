# ==============================================================
# MatchIT Migration Backup Script
# Run this on your CURRENT laptop to copy everything to USB
# ==============================================================
#
# USAGE:
#   Right-click this file -> Run with PowerShell
#   OR open PowerShell and run: .\migrate_backup.ps1
#
# USB drive is hardcoded to D: below - change if needed
# ==============================================================

# Prevent robocopy non-zero exit codes from being treated as errors
$ErrorActionPreference = "Continue"

# -- Source paths --
$USB          = "D:"
$ProjectDir   = "C:\Users\c_a_b\Desktop\MatchIT"
$CardsDB      = "C:\Users\c_a_b\My Drive\CardsDB"
$AppData      = "C:\Users\c_a_b\AppData\Local\MatchITv2_ProductMatch_Data"

# -- Destination paths on USB --
# NOTE: Keep BackupRoot SHORT - deep MTG card paths hit Windows 260-char MAX_PATH limit
$BackupRoot   = "$USB\MIBak"
$DestProject  = "$BackupRoot\MatchIT"
$DestCardsDB  = "$BackupRoot\CardsDB"
$DestAppData  = "$BackupRoot\AppData"

# -- Check USB exists --
if (-not (Test-Path "$USB\")) {
    Write-Host ""
    Write-Host "ERROR: USB drive '$USB' not found!" -ForegroundColor Red
    Write-Host "Your available drives:" -ForegroundColor Cyan
    Get-PSDrive -PSProvider FileSystem | Format-Table Name, Root, @{N='Free(GB)';E={[math]::Round($_.Free/1GB,1)}} -AutoSize
    Read-Host "Press Enter to exit"
    exit 1
}

# -- Check USB filesystem type (FAT32 has worse path-length issues than NTFS) --
$usbFS = (Get-Volume -DriveLetter ($USB.TrimEnd(':'))).FileSystem
Write-Host ""
if ($usbFS -eq "FAT32") {
    Write-Host "WARNING: USB is formatted as FAT32." -ForegroundColor Yellow
    Write-Host "FAT32 has a 260-character path limit which can cause errors with deep card folders." -ForegroundColor Yellow
    Write-Host "If you see 'cannot be created' errors, reformat the USB as NTFS and retry." -ForegroundColor Yellow
    Write-Host ""
}

# -- Check free space --
$drive  = Get-PSDrive ($USB.TrimEnd(':'))
$freeGB = [math]::Round($drive.Free / 1GB, 1)

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  MatchIT Migration Backup" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "USB Drive : $USB  ($freeGB GB free, $usbFS)" -ForegroundColor Green
Write-Host "Backup to : $BackupRoot" -ForegroundColor Green
Write-Host ""

if ($freeGB -lt 15) {
    Write-Host "WARNING: Less than 15 GB free on USB." -ForegroundColor Yellow
    Write-Host "CardsDB alone can be 10-20 GB depending on card images." -ForegroundColor Yellow
    $confirm = Read-Host "Continue anyway? (y/n)"
    if ($confirm -ne 'y') { exit 0 }
}

# -- Create backup root --
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
Write-Host ""

# ==============================================
# 1. MATCHIT PROJECT
# ==============================================
Write-Host "[1/4] Copying MatchIT project..." -ForegroundColor Yellow
Write-Host "  From: $ProjectDir" -ForegroundColor Gray
Write-Host "  To:   $DestProject" -ForegroundColor Gray

if (Test-Path $ProjectDir) {
    # Exclude venv dirs (will recreate on new machine) and caches
    # /R:1 /W:1 = 1 retry, 1s wait (default is 1M retries / 30s - causes hanging)
    robocopy $ProjectDir $DestProject /E `
        /XD ".venv" "venv" "__pycache__" ".git" "node_modules" `
             "clean_images" "clean_samples" "ras_images" "db_images" `
        /XF "*.pyc" "*.log" `
        /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS
    Write-Host "  DONE" -ForegroundColor Green
} else {
    Write-Host "  ERROR: $ProjectDir not found!" -ForegroundColor Red
}

# -- Use existing requirements.txt (already in project) --
if (-not (Test-Path "$DestProject\requirements.txt")) {
    Write-Host "  WARNING: requirements.txt not found in backup" -ForegroundColor Yellow
    Write-Host "  Attempting to generate from venv pip..." -ForegroundColor Yellow
    $venvPip = "$ProjectDir\.venv\Scripts\pip.exe"
    if (-not (Test-Path $venvPip)) { $venvPip = "$ProjectDir\venv\Scripts\pip.exe" }
    if (Test-Path $venvPip) {
        $pipOut = & $venvPip freeze 2>&1
        $pipOut | Out-File -FilePath "$DestProject\requirements.txt" -Encoding UTF8
        Write-Host "  requirements.txt generated" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: No pip found in venv - install manually on new machine" -ForegroundColor Yellow
    }
}

Write-Host ""

# ==============================================
# 2. CARDS DATABASE (Google Drive)
# ==============================================
Write-Host "[2/4] Copying CardsDB (may take a while)..." -ForegroundColor Yellow
Write-Host "  From: $CardsDB" -ForegroundColor Gray
Write-Host "  To:   $DestCardsDB" -ForegroundColor Gray

if (Test-Path $CardsDB) {
    robocopy $CardsDB $DestCardsDB /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS
    Write-Host "  DONE" -ForegroundColor Green
} else {
    Write-Host "  WARNING: $CardsDB not found!" -ForegroundColor Yellow
    Write-Host "  Google Drive may not be synced locally. Copy CardsDB manually." -ForegroundColor Yellow
}

Write-Host ""

# ==============================================
# 3. APP DATA (Embeddings + Cache)
# ==============================================
Write-Host "[3/4] Copying embeddings and cache..." -ForegroundColor Yellow
Write-Host "  From: $AppData" -ForegroundColor Gray
Write-Host "  To:   $DestAppData" -ForegroundColor Gray

if (Test-Path $AppData) {
    robocopy $AppData $DestAppData /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS
    Write-Host "  DONE" -ForegroundColor Green
} else {
    Write-Host "  ERROR: $AppData not found!" -ForegroundColor Red
    Write-Host "  Without this you will need to re-embed all 70K+ cards!" -ForegroundColor Red
}

Write-Host ""

# ==============================================
# 4. VERIFY BACKUP
# ==============================================
Write-Host "[4/4] Verifying backup..." -ForegroundColor Yellow
Write-Host ""

$checks = @(
    # -- Core app --
    @{ Name = "app.py (main Flask app)";        Path = "$DestProject\app.py" },
    @{ Name = "api_routes.py (scanner API)";    Path = "$DestProject\api_routes.py" },
    @{ Name = "feature_extractor.py (CLIP)";    Path = "$DestProject\feature_extractor.py" },
    @{ Name = "vertical_loader.py";             Path = "$DestProject\vertical_loader.py" },
    @{ Name = "marketplace.py";                 Path = "$DestProject\marketplace.py" },
    @{ Name = "card_xref.py";                   Path = "$DestProject\card_xref.py" },
    @{ Name = "config.json";                    Path = "$DestProject\config.json" },
    @{ Name = "images.db (local SQLite DB)";    Path = "$DestProject\images.db" },
    @{ Name = "sku_crossrefs.json";             Path = "$DestProject\sku_crossrefs.json" },
    @{ Name = "sku_profiles.json";              Path = "$DestProject\sku_profiles.json" },
    @{ Name = "sku_types.json";                 Path = "$DestProject\sku_types.json" },
    @{ Name = "requirements.txt";               Path = "$DestProject\requirements.txt" },
    # -- Modal deployment --
    @{ Name = "matchit_modal.py (Modal wrapper)";   Path = "$DestProject\matchit_modal.py" },
    @{ Name = "upload_to_modal.py (embeddings)";    Path = "$DestProject\upload_to_modal.py" },
    @{ Name = "upload_profiles.py (profiles)";      Path = "$DestProject\upload_profiles.py" },
    @{ Name = "smart_upload.py";                    Path = "$DestProject\smart_upload.py" },
    # -- Scrapers --
    @{ Name = "scrape_pokemon_tcg.py";          Path = "$DestProject\scrape_pokemon_tcg.py" },
    @{ Name = "scrape_mtg.py";                  Path = "$DestProject\scrape_mtg.py" },
    @{ Name = "scrape_yugioh.py";               Path = "$DestProject\scrape_yugioh.py" },
    @{ Name = "scrape_hardware_images_v2.py";   Path = "$DestProject\scrape_hardware_images_v2.py" },
    # -- Onboarding / tools --
    @{ Name = "onboard_vertical.py";            Path = "$DestProject\onboard_vertical.py" },
    @{ Name = "onboard_skus.py";                Path = "$DestProject\onboard_skus.py" },
    @{ Name = "import_crossrefs.py";            Path = "$DestProject\import_crossrefs.py" },
    @{ Name = "profile_tool.py";                Path = "$DestProject\profile_tool.py" },
    @{ Name = "regression_master.py";           Path = "$DestProject\regression_master.py" },
    @{ Name = "export_clip_onnx.py";            Path = "$DestProject\export_clip_onnx.py" },
    @{ Name = "setup_remotes.py";               Path = "$DestProject\setup_remotes.py" },
    @{ Name = "setup_remotesdb.py";             Path = "$DestProject\setup_remotesdb.py" },
    @{ Name = "setup_hardwaredb.py";            Path = "$DestProject\setup_hardwaredb.py" },
    # -- Startup scripts --
    @{ Name = "Start_MatchIT_DEV.ps1";          Path = "$DestProject\Start_MatchIT_DEV.ps1" },
    @{ Name = "Stop_MatchIT_DEV.ps1";           Path = "$DestProject\Stop_MatchIT_DEV.ps1" },
    @{ Name = "Activate_MatchIT_VENV.ps1";      Path = "$DestProject\Activate_MatchIT_VENV.ps1" },
    # -- Static / UI --
    @{ Name = "scanner.html";                   Path = "$DestProject\static\scanner.html" },
    @{ Name = "landing.html";                   Path = "$DestProject\static\landing.html" },
    @{ Name = "style.css";                      Path = "$DestProject\static\style.css" },
    # -- Verticals --
    @{ Name = "vertical: cards";                Path = "$DestProject\verticals\cards\vertical.json" },
    @{ Name = "vertical: hardware";             Path = "$DestProject\verticals\hardware" },
    @{ Name = "vertical: keys";                 Path = "$DestProject\verticals\keys" },
    @{ Name = "vertical: remotes";              Path = "$DestProject\verticals\remotes" },
    @{ Name = "vertical: kitchen_tools";        Path = "$DestProject\verticals\kitchen_tools" },
    # -- Docs --
    @{ Name = "SETUP_NEW_MACHINE.md";           Path = "$DestProject\SETUP_NEW_MACHINE.md" },
    @{ Name = "Setup_new_laptop.txt";           Path = "$DestProject\Setup_new_laptop.txt" },
    @{ Name = "CLAUDE.md";                      Path = "$DestProject\CLAUDE.md" },
    # -- CardsDB --
    @{ Name = "CardsDB: pokemon";               Path = "$DestCardsDB\pokemon" },
    @{ Name = "CardsDB: mtg";                   Path = "$DestCardsDB\mtg" },
    @{ Name = "CardsDB: yugioh";                Path = "$DestCardsDB\yugioh" },
    # -- Embeddings / AppData --
    @{ Name = "AppData: cards embeddings";      Path = "$DestAppData\cards" },
    @{ Name = "AppData: keys embeddings";       Path = "$DestAppData\keys" }
)

$allGood  = $true
$okCount  = 0
$missList = @()

foreach ($c in $checks) {
    if (Test-Path $c.Path) {
        Write-Host "  [OK]      $($c.Name)" -ForegroundColor Green
        $okCount++
    } else {
        Write-Host "  [MISSING] $($c.Name)" -ForegroundColor Red
        $missList += $c.Name
        $allGood = $false
    }
}

# -- Size summary --
Write-Host ""
Write-Host "---- Backup Sizes ----" -ForegroundColor Cyan
if (Test-Path $DestProject) {
    $projSize = [math]::Round((Get-ChildItem $DestProject -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB, 0)
    Write-Host "  MatchIT project:    ${projSize} MB" -ForegroundColor Gray
}
if (Test-Path $DestCardsDB) {
    $dbSize = [math]::Round((Get-ChildItem $DestCardsDB -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1GB, 1)
    Write-Host "  CardsDB:            ${dbSize} GB" -ForegroundColor Gray
}
if (Test-Path $DestAppData) {
    $adSize = [math]::Round((Get-ChildItem $DestAppData -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB, 0)
    Write-Host "  Embeddings/Cache:   ${adSize} MB" -ForegroundColor Gray
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
if ($allGood) {
    Write-Host "  BACKUP COMPLETE  -  All $okCount items verified OK!" -ForegroundColor Green
} else {
    Write-Host "  BACKUP COMPLETE  -  $okCount OK, $($missList.Count) missing" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Missing items:" -ForegroundColor Yellow
    foreach ($m in $missList) { Write-Host "    - $m" -ForegroundColor Red }
}
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next: Read SETUP_NEW_MACHINE.md on the USB drive" -ForegroundColor Cyan
Write-Host ""
Read-Host "Press Enter to exit"
