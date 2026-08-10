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
- smart_upload.py — uploads embeddings/images.db, images, profiles, thumbnails to Modal volume (state-tracked, incremental)
- incremental_embed.py — adds new card embeddings without full rebuild
- vertical_loader.py — multi-vertical config system
- verticals/cards/vertical.json — cards vertical config
- static/scanner.html — standalone scanner (old, kept for reference)
- templates/match.html — main scanner + photo match page (active)
- templates/landing.html — grailsweep.com home page

## Modal Deployment Commands

Canonical deploy sequence (any code changes) — run all three, in order:
    1. modal run regression_tests.py::test_profile_pipeline   # pre-deploy gate (~30s, SystemExit(1) on failure)
    2. modal deploy matchit_modal.py                          # deploy
    3. modal run matchit_modal.py::warm                       # warm the new container BEFORE real users hit it

Why step 3: every deploy changes the /app image layer, which invalidates the
serve() memory/GPU snapshot, so the first request after a deploy pays the full
cold start (CLIP + embedding cache load, ~11s+). `warm` hits the deployed
serve() endpoint once so that cost lands on us, not the next real user. It also
kicks off the DINOv2 tie-break background preload in that container. Targets the
deployed serve via Function.from_name and hits its *.modal.run URL directly
(bypasses Cloudflare), so it always warms the GPU serve function, not serve_light.

Upload embeddings/images DB (when new cards scraped)
modal run smart_upload.py --db-and-cache
(opt-in and guarded — aborts if the local file has fewer rows than the volume's)
Test email scheduler manually
modal run matchit_modal.py::scheduled_set_check

### GOTCHA — sw.js version bump can be silently skipped by Modal

A bare version-number change in static/sw.js (e.g. grailsweep-v112 -> v113) is the
SAME byte length, and Modal's add_local_dir mount change-detection can miss it —
it reuses the cached /app image layer and your new sw.js NEVER SHIPS. Installed
apps keep serving the old cached /collection etc.

- The fix: change the file's BYTE SIZE too — add or edit a comment line alongside
  the version bump (e.g. a dated `// vNNN — <reason>` line). Then redeploy.
- Verify it actually shipped (don't trust the deploy log). curl the origin
  directly via the Modal-UA bypass (app.py _enforce_cf_proxy() exempts Modal/*
  UAs) with a cache-buster to rule out any HTTP cache:
    curl -s "https://c-a-buckley--matchit-api-serve.modal.run/sw.js?cb=$RANDOM" \
      -H "User-Agent: Modal/verify" | grep CACHE_NAME
  It must show the NEW version.

### Deploy timing is NOT diagnostic (corrected 2026-08-04)

An earlier version of this file said "fast deploy (~15s) == mount cache hit ==
your change may not be in the image." **That inference is wrong and cost a
near-miss.** Measured 2026-08-04: a **12.6s** deploy shipped a changed app.py
correctly, confirmed by the [VERSION] marker. A second, genuinely no-op deploy
of identical content took 14.3s — indistinguishable by timing.

Deploy duration tells you whether layers were rebuilt. It does NOT tell you
whether your change is in the image. The same-byte-length trap above is real,
but timing alone cannot detect it. **Use [VERSION], not the stopwatch.**

### Verifying a deploy actually shipped — the reliable procedure

    1. modal deploy matchit_modal.py --strategy recreate
    2. WAIT past scaledown_window — serve() is 120s (serve_light is 600s).
       Skipping this is the single biggest source of false readings.
    3. modal run matchit_modal.py::warm
    4. modal app logs matchit-api | grep VERSION
    5. compare against:  git rev-parse --short HEAD

[VERSION] is emitted at startup by app.py's _check_preload_integrity(), sourced
from GS_GIT_SHA which modal_config.py bakes in at deploy time (.git is in the
add_local_dir ignore list, so the container cannot derive it itself). It appends
**-dirty** when the working tree had uncommitted changes at deploy — if you see
-dirty on a supposedly clean deploy, the image was built from unclean state.

This is the only direct check. Everything else — cold-start duration, byte-size
deltas, deploy timing — is circumstantial, and two deploys in the week of
2026-08-04 could only be verified that way because every change was on a silent
code path.

### GOTCHA — `modal app logs` replays PRE-deploy container startups

`modal app logs` returns a buffer that can include the startup output of
snapshot-restored containers that predate the deploy. Reading logs immediately
after deploying will happily show you a pre-deploy container's startup lines and
lead you to conclude the change did not ship.

Happened 2026-08-04 and produced a false negative: two container startups in the
buffer both showed the OLD startup sequence, and the change was briefly declared
un-shipped. It had shipped. Waiting past scaledown_window (120s) and re-warming
produced a genuinely fresh container that showed the new markers immediately.

- Symptom: expected new startup lines absent, everything else looks normal.
- Cause: you are reading a container that started BEFORE the deploy.
- Fix: wait out scaledown_window, warm, then read. Confirm with [VERSION].
- Note `modal app logs` fetches only the last ~100 entries, so this buffer is
  small and historical rates cannot be established from it.

### Startup markers — [VERSION] / [PRELOAD-PATH] / [PRELOAD-OK] / [PRELOAD-STALE]

Emitted by app.py `_check_preload_integrity()`, immediately after the three
preloads. Costs 0.1ms — it counts already-loaded in-memory structures and reads
no files.

    [VERSION] <short sha>          the deployed commit; -dirty if built unclean
    [PRELOAD-PATH] <name> -> <path>  (volume) or (IN-IMAGE), one per constant
    [PRELOAD-OK] <counts>          all preloads above their floors
    [PRELOAD-STALE] <detail>       ERROR — container is serving stale/empty data

**Healthy** (all five [PRELOAD-PATH] show `(volume)`):

    sku_game_map=33132  identifier_lookup=162338  pokemon_search_index=24652

**Known-stale signature** (all five show `(IN-IMAGE)`):

    [SKU-GAME]   Preloaded 20236        (healthy 33132)
    [OCR-LOOKUP] Preloaded 162096       (healthy 162338)
    [SEARCH]     search unavailable     (healthy 24652)

What it guards: SET_METADATA_PATH / SKU_GAME_MAP_PATH / IDENTIFIER_LOOKUP_PATH /
MTG_SET_TOTALS_PATH / POKEMON_SEARCH_INDEX_PATH all bind at IMPORT via
`os.path.exists("/modal_data")`. If the mount is invisible at that instant they
bind to the in-image copies under /app, which freeze at the last commit while the
volume copies are updated by the scheduler. The preloads then run at import, so
the wrong binding is baked into the memory snapshot and persists for the
container's whole life. Staleness always takes the form "newest sets missing".

It does NOT crash — a crash-loop from a bad floor is worse than the degradation
being guarded, and this has never been observed to fire. It increments
`preload_stale_containers` in the `matchit-health` modal.Dict. If that counter
ever moves, revisit: crashing so Modal retries onto a fresh container becomes
defensible once the floors are known sound in production.

Floors are FLOORS, not equality — these files grow with every set ingested and an
equality check would fail on every legitimate scheduler update.

**MAINTENANCE — the me5-1 probe must be advanced.** identifier_lookup cannot be
protected by a count floor (stale 162096 vs healthy 162338 is inside any usable
headroom), so it is covered by a content probe for a SKU that exists on the
volume but not in the in-image copy. `_PRELOAD_PROBE_SKU = "me5-1"` in app.py
detects "older than me5", not staleness in general. **When a Pokémon set newer
than me5 ships, advance the probe to a SKU from that set** — otherwise it
silently stops detecting anything, because the in-image copy will by then
contain me5.

### Modal mount ignore list — what still gets uploaded

modal_config.py `add_local_dir("C:/MatchIT", "/app", ignore=[...])` excludes
`*.txt`, `*.md`, `__pycache__`, `*.pyc`, `.git`, `.venv`, `*.log`, eval_rescue,
`config.json.*`, `*_pre_*`, `*_preocr*`, `*.bak*` and several scratch dirs.

Watch out:

- `*.txt` IS ignored, but `.json`, `.csv` and any other extension are NOT — stray
  captures left in C:\MatchIT get uploaded into /app on the next deploy and bloat
  the image. Clean scratch files out of the repo root before deploying.
- The backup-suffix patterns need a LITERAL `_pre_`. Suffixes where a word runs
  straight on from `_pre` (e.g. `_preeragate_`, `_predbretry_`,
  `_prepreloadcheck_`) slip through and DO get uploaded. Same gap exists in
  .gitignore, which is why each new suffix needs its own rule added there.

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

## Cloudflare cache rules (dashboard-only, NOT in this repo)

The cache rule covering /privacy, /terms, /contact and /sitemap.xml must be
set to **"Respect origin TTL"** for BOTH Edge TTL and Browser TTL.

**DO NOT set it to "Ignore cache-control header and use this TTL."** That
setting was the root cause of a week-long "the app never sees updates"
problem during the Aug 2026 iOS resubmission. The origin sends
`max-age=0, must-revalidate` on those routes (see app.py) so device caches
revalidate instead of holding content for 24 hours — Cloudflare overriding
that header defeats the entire mechanism, and every server-side fix appears
to do nothing on device while verifying perfectly clean at the origin.

Symptom to recognise: origin curl shows correct HTML, edge curl shows
correct HTML, yet the installed app still renders old content after a
deploy + purge + relaunch.

This config lives only in the Cloudflare dashboard. If the zone is ever
rebuilt (DNS migration, new account) it must be recreated by hand.

## Diagnostic pill (diagnostics/)

Reusable tool for investigating iOS/WebView caching and platform-branch
issues — i.e. "the server is serving the right thing but the app shows
something else." Injected temporarily, read off the screen, then removed.

Reports as a fixed banner: which CACHE_NAME controls the document, whether
the document is SW-controlled, a count of platform-conditional classes in
the DOM (proving whether new HTML reached the device), whether
gsIsRunningInIOSApp() is true on that page load, and whether a newer SW is
stuck in "waiting".

See diagnostics/README.md for the fields, the reuse procedure, known-good
readings, and the sw.js removal gotcha. Built during the Aug 2026 iOS
resubmission; its design directly exposed the Cloudflare rule documented
above.

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
