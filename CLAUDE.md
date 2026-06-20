# GrailSweep — Project Documentation

## Overview
GrailSweep is an AI-powered visual TCG card matching and valuation platform.
- **Live at:** grailsweep.com (via Cloudflare Worker → Modal)
- **Stack:** Flask, SQLite, CLIP ViT-L-14, DINOv2, EasyOCR, Google Vision OCR
- **Hosting:** Modal (workspace: c-a-buckley), T4 GPU
- **Local project:** C:\MatchIT
- **Main app file:** app.py (never main.py)

## Card Database
- ~135,797 cards embedded (Pokémon, MTG, Yu-Gi-Oh)
- CardsDB location: C:\CardsDB
- Embeddings/images on Modal volume: matchit-data-v2
- Volume mount point: /modal_data (NOT /data)

## Key Files
- app.py — main Flask app (web routes, /match, /collection, etc.)
- api_routes.py — API endpoints (/api/v1/match, /api/v1/image)
- matchit_modal.py — Modal deployment (serve, scheduled_set_check)
- set_scheduler.py — weekly new set detection + email notifications
- ocr_confirm.py — EasyOCR set code confirmation for reprint disambiguation
- sync_profiles.py — syncs scraped profile.json files into CardsDB (replaces upload_profiles.py)
- upload_to_modal.py — uploads embeddings/images.db to Modal volume
- incremental_embed.py — adds new card embeddings without full rebuild
- vertical_loader.py — multi-vertical config system
- verticals/cards/vertical.json — cards vertical config
- static/scanner.html — standalone scanner (old, kept for reference)
- templates/match.html — main scanner + photo match page (active)
- templates/landing.html — grailsweep.com home page

## Modal Deployment Commands
Upload embeddings/images DB (when new cards scraped)
modal run upload_to_modal.py
Deploy app (any code changes)
modal deploy matchit_modal.py
Test email scheduler manually
modal run matchit_modal.py::scheduled_set_check

## Pre-deploy regression test
Before every `modal deploy`, run:
    modal run regression_tests.py::test_profile_pipeline
Confirms profile loader + sync pipeline still work end-to-end.
Suite raises SystemExit(1) on any failure. Takes ~30s.
Covers: CardsDB primary path for scheduler-synced sets (me4), pre-existing cards
(base1-1, mtg-hob-110), missing-SKU safety, sync no-op safety, Strategy A2+C
end-to-end loader chain.

## Profile sync (canonical mechanism)

Profiles are synced into CardsDB by sync_profiles.py — invoked
automatically by the Monday scheduler, or manually via:
    modal run sync_profiles.py::run_sync --game pokemon --sets SET_CODE
    modal run sync_profiles.py::run_sync --game pokemon --all-flag
The legacy upload_profiles.py has been removed (broken mount path,
superseded by sync_profiles.py).

## Volume Paths (Modal)

- Embeddings: /modal_data/MatchITv2_ProductMatch_Data/cards/
- CardsDB profiles: /modal_data/CardsDB/
- numpy cache: /modal_data/MatchITv2_ProductMatch_Data/cards/npy_cache
- EasyOCR models: /modal_data/easyocr_models/

## Email
- Sent via Resend (no SMTP, no Gmail credentials)
- Wrapper: email_sender.py, function gs_send_email()
- From / Reply-To: support@grailsweep.com
- Modal secret: resend-api-key
- Set scheduler fires: Monday 1am UTC automatically via Modal cron

## Pricing
- USD→GBP: live rate fetched from frankfurter.app on page load (fallback 0.79)
- EUR→GBP: live rate fetched from same call (fallback 0.86)
- Prices sourced from profile.json files (scraped from TCGPlayer/Cardmarket)

## Pro Features (Gated)
- XLSX export (scExportProGate in match.html)
- Collection manager
- Unlimited scans
- Price alerts
- Live scanner

## OCR Set Confirmation
- Module: ocr_confirm.py
- Crops TCG-specific regions to extract set codes
- Promotes matching result to Rank 1 if found in top 5
- Only ever improves results — falls back to visual ranking if OCR fails
- YGO: bottom-left region, Pokémon: bottom-right, MTG: bottom-left

## Roadmap — Next Vertical: Funko Pop
- Visual matching using CLIP/DINOv2 (no barcode needed — photo upload only)
- Data source: github.com/kennymkchan/funko-pop-data (23K items, MIT license, includes images)
- Pricing via eBay sold listings API (real transaction data — biggest gap in competitor apps)
- Authenticity mark detection — second image upload for signed Pops (PSA/Beckett/JSA cert number via OCR)
- No live scanner needed — photo upload only simplifies build
- Key challenge: visual similarity between Pops in same series — may need item number OCR confirmation similar to TCG set code system
- Estimated: 10–15 evenings full build, 6–9 evenings without auth marks (v1)
- Start after TCG vertical is stable

## Roadmap — App Store & Google Play Submission

**Approach:** Capacitor wrapper around existing GrailSweep web app

**Why:** App Store/Play Store discovery opens GrailSweep to millions of TCG collectors 
searching for card scanner apps. Currently only reachable via direct URL or Google search.
Push notifications would also improve price alert engagement (60-80% open rate vs 20-30% email).

**What needs building (5-7 evenings):**
- Capacitor project setup wrapping grailsweep.com
- Native iOS tab bar replacing web nav pills (Home / Scan / Collection / Pro)
- Push notifications for price alerts (native-only feature, key for Apple approval)
- Splash screen + app icons (all sizes)
- Offline state handling (proper no-connection screen)
- Camera permission declarations (NSCameraUsageDescription)
- App store listing, screenshots, descriptions

**Payment:** Keep Stripe via external link. Under EU/UK DMA rules Apple charges 
5% Core Technology Commission on external purchases. At £2.99/month this is 
preferable to Apple's 15-30% IAP cut.

**Accounts needed:**
- Apple Developer Program: $99/year
- Google Play Developer: $25 one-time

**Apple approval risks:**
- Must have native navigation (not just web nav)
- Must have at least one native-only feature (push notifications solve this)
- Pure webview wrappers rejected under Guideline 4.2 Minimum Functionality
- GrailSweep's AI matching, camera scanning and collection features are strong 
  arguments for genuine app functionality

**Google Play:** Much more lenient — likely to pass with Capacitor wrapper 
and minimal native additions.

**Start after:** Current feature set is stable and subscriber base is growing

## Known Issues / Outstanding
- static/scanner.html is the old standalone scanner, kept for reference only — not the active scanner

## Preferred Ways of Working
- Step-by-step guidance
- Brainstorming
- Data analysis and SQL queries (including subqueries and CTEs)
- Coding and debugging
- Always refer to main app file as app.py, never main.py

## Branding
- Product name: GrailSweep
- Domain: grailsweep.com
- Tagline: "Scan any card. Know its value instantly."
- Colours: purple (#b14dff), cyan (#00e5ff), gold (#ffd700), green (#00ff88)
- Font: Orbitron (display), DM Sans (body)
