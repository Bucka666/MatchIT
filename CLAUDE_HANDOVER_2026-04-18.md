# MatchIT / GrailSweep — Handover Document
**Session date:** 2026-04-18
**Last deploy:** 2026-04-18 ~14:00 UTC
**Scanner status:** WORKING — correctly identifies cards including new Ascended Heroes set
**Known live URL:** https://c-a-buckley--matchit-api-serve.modal.run

---

## ⚠️ CRITICAL INSTRUCTIONS FOR NEXT CLAUDE SESSION

1. **User is Craig.** Owner of MatchIT, a multi-vertical visual product matching platform. Business partner involved. Focus is MatchIT Cards (branded publicly as "GrailSweep").
2. **Main file is `app.py`** — NEVER refer to it as `main.py`. Craig has a separate capture project where `main.py` exists; confusion causes real problems.
3. **DO NOT suggest Craig take breaks.** He decides his own timing. When Claude has made mistakes, own them as Claude's mistakes, not "ours".
4. **DO NOT include markdown backticks inside Python files** meant to be saved directly. Craig has been burned by this before.
5. **Use str_replace for edits, never rewrite whole files unless explicitly asked.**
6. **Craig prefers step-by-step, clearly explained instructions.** He's following Claude's lead for coding/technical.
7. **The interface Craig works in is Claude Code (a separate terminal agent).** Claude in this chat provides instructions that Claude Code executes. Don't try to run bash commands yourself — give Craig the exact command to paste into Claude Code.

---

## 🎯 CURRENT STATE (END OF 2026-04-18 SESSION)

### ✅ Working correctly
- Scanner identifies cards for Pokémon, YGO, MTG
- OCR-first pipeline for YGO/MTG (2-4s warm response)
- Auto-detect TCG from CLIP top match when user hasn't specified
- Confidence gate rejects matches below 0.65
- Guide-frame cropping (88% height, +8% bottom extension)
- Large 3-button TCG selector (Pokémon/YGO/MTG) replacing tiny dropdown
- Card detail modal enlarged (500px max-width, 140x196px card image)
- Quick Grade (Free) shown by default + Run AI Deep Grade (Pro) button
- Deep grade URL fix with 30s timeout
- Session panel (scanned cards) sits above scan controls with box shadow
- Session panel hides when starting a new scan
- Hero video autoplay fallback for iOS Safari
- Shared AudioContext for scanner sound
- Ascended Heroes (`me2pt5`) set recognized via `ASC` → `me2pt5` mapping
- SWSH total fingerprint includes `[swsh11, me2pt5]` for printed total 217
- Impossible OCR values rejected (card num > 400, total > 400)
- Most-common-total picking across multiple OCR reads
- Direct DB lookup loads profile inline so scanner gets prices
- All 135,797 per-folder profiles uploaded to Modal volume

### ❌ Outstanding issues (NOT yet fixed)
1. **Container cycling** — Modal spawns new cold containers for concurrent scans despite `min_containers=1`, `scaledown_window=600`, `buffer_containers=1`. Also Modal preemption can kill warm containers mid-request. Cost vs speed tradeoff — raising `min_containers=2` doubles baseline cost.
2. **Sound on iOS scanner** — Shared AudioContext with resume-before-schedule added, still not firing. Craig to verify hardware mute switch isn't on.
3. **MTG extractor false positives** — e.g., reads "AKIKA 10483" (illustrator credit) as set code `akika-147`. Needs stricter validation: 2-5 uppercase alpha only, reject all-digit or mixed codes.
4. **SWSH ambiguity handling for total=217** — Two sets share this total (swsh11 and me2pt5). Current code returns just the card number when ambiguous. Could be improved by checking CLIP top match's set prefix to resolve ambiguity.
5. **MEE Basic Energies set** — 8 basic energy cards (mee-1 through mee-8), ptcgoCode `MEE`. Released with Mega Evolution series. Parked — pokemontcg.io does NOT have this set indexed yet. Scrydex DOES have it (and is pokemontcg.io's successor, same team). Integration would take significant work for only 8 cards. Best to wait for pokemontcg.io to index it; weekly Monday scheduler will pick up automatically.
6. **Central `sku_profiles.json` not updated** — The file has 332 profiles loaded at runtime but doesn't include me2pt5, me3, or other recent sets. Per-folder profiles in CardsDB are complete. `upload_profiles.py` uploads per-folder profiles to Modal (that's what the scanner actually reads). The central file merge via `onboard_skus.py --profile` hasn't been run recently. Craig's weekly Monday cron job runs scraping but the central merge step may be broken.
7. **Scrydex migration** — pokemontcg.io has migrated image hosting to scrydex.com (URLs in profiles now point to `images.scrydex.com`). The API itself is still pokemontcg.io. Scrydex is the successor — at some point a migration to the Scrydex API may be worthwhile for more current data coverage.
8. **Modal "Direct profile load" inline loading of `sku_profiles_{tcg}.json`** — The code tries to load per-TCG profile files, but Craig's system only has the central `sku_profiles.json`. The fallback works, but the per-TCG file lookup is dead code. Either create per-TCG files or remove the code path.

---

## 🗂️ KEY FILE LOCATIONS

```
C:\MatchIT\app.py                 # Main Flask app (4000+ lines)
C:\MatchIT\api_routes.py          # Scanner API endpoints (/api/v1/match)
C:\MatchIT\ocr_confirm.py         # PaddleOCR/Google Vision OCR pipeline
C:\MatchIT\matchit_modal.py       # Modal deployment config
C:\MatchIT\upload_profiles.py     # Uploads per-folder profile.json to Modal
C:\MatchIT\upload_to_modal.py     # Uploads embeddings + images to Modal
C:\MatchIT\onboard_skus.py        # Builds central sku_profiles.json from per-folder
C:\MatchIT\scrape_pokemon_tcg.py  # Scrapes pokemontcg.io (--set <id> --resume for single)
C:\MatchIT\set_scheduler.py       # Weekly Monday cron config
C:\MatchIT\templates\match.html   # Scanner UI (2000+ lines)
C:\MatchIT\templates\results.html # Photo match results page
C:\MatchIT\templates\collection.html
C:\MatchIT\templates\base.html    # Nav header
C:\MatchIT\templates\upgrade.html # Pricing tiers
C:\MatchIT\templates\landing.html # Homepage

C:\CardsDB\pokemon\               # Scraped card data (135k+ folders)
  <sku>\front.png, profile.json
C:\CardsDB\mtg\
C:\CardsDB\ygo\

C:\Users\c_a_b\AppData\Local\MatchITv2_ProductMatch_Data\cards\
  images.db                       # Local embeddings SQLite
  image_db\                       # Local cached images
```

**Modal deployment:**
- Workspace: `c-a-buckley`
- App name: `matchit-api`
- Endpoint: `https://c-a-buckley--matchit-api-serve.modal.run`
- Volume: `matchit-data-v2`
- GPU: T4
- Python: 3.11
- DB path on Modal: `/modal_data/MatchITv2_ProductMatch_Data/cards/images.db`
- CardsDB on Modal: `/modal_data/CardsDB`

**Modal container settings (matchit_modal.py):**
```python
@app.function(
    image=image,
    gpu="T4",
    volumes={"/modal_data": vol},
    timeout=300,
    scaledown_window=600,       # 10 min idle before shutdown
    min_containers=1,           # 1 always-warm
    buffer_containers=1,        # 1 extra pre-warmed for bursts
)
```

---

## 🔄 COMMON COMMANDS

```bash
# Deploy to Modal
modal deploy matchit_modal.py

# Upload per-folder profiles to Modal volume
modal run upload_profiles.py

# Upload embeddings + images to Modal volume
modal run upload_to_modal.py

# Scrape a single set with resume
python scrape_pokemon_tcg.py --set <setid> --resume

# Merge per-folder profiles into central sku_profiles.json
python onboard_skus.py --profile

# Check what's in DB for a SKU
python -c "
import sqlite3
conn = sqlite3.connect('C:/Users/c_a_b/AppData/Local/MatchITv2_ProductMatch_Data/cards/images.db')
count = conn.execute(\"SELECT COUNT(*) FROM images WHERE sku LIKE '<setid>-%'\").fetchone()
print(count[0])
"

# Query pokemontcg.io for a set
curl "https://api.pokemontcg.io/v2/sets?q=name:<setname>"
curl "https://api.pokemontcg.io/v2/sets?q=ptcgoCode:<code>"
```

---

## 📋 TODAY'S WORK (2026-04-18) — WHAT WE CHANGED

### 1. OCR pipeline improvements (`ocr_confirm.py`)
- Added Ascended Heroes to `_PKM_SETCODE_MAP`: `"ASC": "me2pt5"`
- Updated `_SWSH_TOTAL_MAP[217]` to `['swsh11', 'me2pt5']`
- Added impossible value rejection: card num > 400 or total > 400 skipped
- Added `collections.Counter` logic to pick most common total across multiple OCR reads
- Prefer totals in SWSH map when multiple candidates exist
- Multi-region Pokémon OCR read (3 overlapping crops combined + dedup)
- Direct DB lookup now loads profile inline so scanner gets prices
- Added debug logging `[OCR-DB]` for lookup success/failure

### 2. Scanner API (`api_routes.py`)
- Added TCG auto-detect from CLIP top match SKU prefix when user hasn't selected
- `_effective_tcg` calculated before `ocr_confirm_ranking` call
- Prefix matches: `ygo-*` → YUGIOH, `mtg-*` → MTG, `sv*/swsh*/base*/neo*/dp*/bw*/xy*/sm*/me*/pl*/ex*` → POKEMON

### 3. UI template (`match.html`)
- Session panel: `bottom:60px`, `z-index:150`, `max-height:55vh`, added box-shadow
- Session panel hides when `scStart()` is called
- Card detail modal: `max-width:500px`, `max-height:92vh`, `padding:18px`
- Card detail image: `140x196px`, 2px gold border, hover scale
- Added Condition Grade section with Quick Grade (Free) description + Run AI Deep Grade (Pro) button
- Added `window.scRunDeepGrade()` function calling `/api/deep_grade_url`
- Grade section resets on new card open

### 4. Deep grade (`app.py`)
- Increased `urllib.request.urlopen` timeout from 10s → 30s

### 5. Modal config (`matchit_modal.py`)
- Changed `scaledown_window` from 180 → 600 (3 min → 10 min)
- Added `buffer_containers=1` for burst handling

### 6. Profile upload
- Ran `modal run upload_profiles.py` — uploaded 135,797 per-folder profiles to Modal
- Ascended Heroes (me2pt5) now available on Modal

---

## 🔬 DIAGNOSTIC FINDINGS

### Ascended Heroes (me2pt5) — confirmed working
- pokemontcg.io ID: `me2pt5`
- Name: "Ascended Heroes"
- Series: "Mega Evolution"
- ptcgoCode: `ASC`
- printedTotal: 217, total: 295
- Released: 2026-01-30
- 295 images embedded locally, uploaded to Modal
- Profile data: name, rarity, energy_type all present
- **Prices empty** — pokemontcg.io hasn't populated market data yet (too new)
- Scanner correctly identifies as Glastrier, Ascended Heroes #54, me2pt5-54

### MEE Basic Energies — parked
- Not in pokemontcg.io yet (checked 2026-04-18)
- Scrydex has it and prices (e.g., Basic Psychic Energy #5 is $0.04)
- 8 cards total (mee-1 to mee-8)
- Low priority — basic energies have near-zero market value

### Cold start behavior observed
- Despite `min_containers=1 + buffer_containers=1`, concurrent scans trigger cold starts
- Each cold start reloads CLIP (~30s) + PaddleOCR models
- Modal also sometimes preempts warm containers mid-request (infrastructure)
- Not easily solved without raising `min_containers` to 2+ (cost increases)

---

## 💬 CRAIG'S PREFERENCES & CONTEXT

- Uses Claude for technical/coding work, ChatGPT for personal tasks
- Strong interest in trading cards (Pokémon TCG, MTG, Yu-Gi-Oh)
- Has a business partner; MatchIT has commercial B2B API licensing ambitions
- Family connection to hardware supplier Fitlock Systems (Stockport)
- Laptop: RTX 3050 GPU (~30x faster embeddings than CPU)
- iPhone 16 (wife has iPhone 16 Pro — UI scaling differs)
- Location: Manchester/Stockport area, UK
- Works on MatchIT in `C:\MatchIT\` (username `c_a_b`)
- CardsDB at `C:\CardsDB\`

### Brand info
- Public-facing brand: **GrailSweep** (grailsweep.com)
- Internal/technical name: **MatchIT Cards**
- Other verticals planned: keys (primary launch), hardware, remotes

---

## 🎨 UI/UX NOTES

### Colors / styling
- Primary gradient: `linear-gradient(135deg, #b14dff, #00e5ff)` (purple → cyan)
- Dark background: `rgba(10,8,22,0.75)` and `rgba(26,24,48,0.95)`
- Cyan accent: `--cx-cyan` used for set names
- Gold: `rgba(255,215,0,0.35)` for card image borders
- Green for Free: `rgba(0,255,136,0.15)` border, `#00ff88` text
- Red for errors: `#ff6b6b`

### Font
- Headers use Orbitron monospace
- Body uses system stack

---

## 🚨 THINGS THAT TOOK MULTIPLE ATTEMPTS (DON'T REPEAT)

1. **Modal deployment propagation** — Claude Code sometimes says "redeployed" but the old code is still running. Always verify by checking for new log lines in Modal dashboard before concluding a fix didn't work.

2. **Auto-detect TCG** — Took several iterations. Final working version detects from CLIP's top match SKU prefix in `api_routes.py` before calling `ocr_confirm_ranking`.

3. **Session panel overlay** — Took 3+ attempts. Final fix: `bottom:60px` + `z-index:150` + hide on scan start.

4. **Profile enrichment for OCR direct matches** — Direct DB lookup promoted a SKU to rank 1 but the API profile enrichment step skipped it because it wasn't in original CLIP results. Fix: load profile inline in `_lookup_sku_by_setcode`.

5. **TCG category not reaching OCR** — FormData only appends `category` if TCG button selected. When user scans without selecting, `query_category` was empty string, making OCR try all 3 extractors and match garbage. Fix: auto-detect from CLIP top match.

6. **Ascended Heroes mystery** — Initially thought `me2pt5` was wrong set; card should be `swsh11`. Turned out `me2pt5` IS Ascended Heroes (`ASC` ptcgoCode). Both sets have printedTotal 217 (coincidence). The system was correct all along — the UI just showed empty prices because pokemontcg.io hasn't populated market data yet.

---

## 🗺️ TOMORROW'S PRIORITIES (when Craig is ready)

If Craig asks "what's next":

1. **Test session panel overlay fix** — verify it works on iPhone, no overlap on return
2. **MTG extractor false positive fix** — stricter validation for set codes
3. **Hardware mute check for iOS sound**
4. **Modal container cycling investigation** — check if `min_containers=2` justifies cost
5. **Weekly scheduler audit** — why is central `sku_profiles.json` not getting updated on automated runs

Not urgent / back-burner:
- MEE Basic Energies (wait for pokemontcg.io)
- Scrydex migration evaluation
- Collection API implementation (planned earlier, deprioritized)

---

## ⚙️ SCRATCH NOTES — things Craig mentioned but didn't fully resolve

- "I had these weekly Monday automated scraper/embed runs — things that should have happened 2-3 weeks ago still haven't propagated." → Needs investigation of `scheduled_set_check` function logs in Modal dashboard.
- Sound issue on iOS scanner — suspected hardware mute, not verified.
- Runner preemption errors in Modal logs — may just be occasional infrastructure events.

---

**End of handover.**

The scanner works. Core experience is solid. Remaining items are edge cases and polish.