# GrailSweep — Set Scheduler Refactor + Source Latency Probe
## Batched Claude Code Instruction Block

**Goal:** Convert the weekly set scheduler into a daily, date-aware, idempotent state machine that runs the full background chain per set off the release calendar; close the `identifier_lookup.json` gap; add accuracy gates; make the on-device index a stage-and-notify step with a single version constant; and add a discovery-augmented, observation-only source-latency probe (pokemontcg.io vs TCGdex-EN) wired into the existing scheduler log.

**Source decision (do not deviate):** pokemontcg.io stays the #1 catalog/image/SKU source. TCGdex-EN is used **only** for detection + latency observation in this block — it does **not** ingest images or synthesise SKUs. Cross-source identity mapping is explicitly deferred until the Pitch Black (17 Jul) probe data justifies it.

---

## GLOBAL CONVENTIONS (apply to every phase)

- **Recon before every edit.** Re-read the live function immediately before changing it; line numbers below are from the last recon and may have drifted.
- **Backup before touching any file** to two locations, named `filename_pre_setscheduler.ext`.
- **Validate before deploy:** `ast.parse` every changed `.py`; `node --check` any changed `.js`.
- **Never** `git reset --hard` with uncommitted changes.
- **Deploy only via PowerShell, run by Craig** — never the Claude Code bash tool:
  `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`
- If behaviour is unchanged after deploy: `modal app stop matchit-api`, then redeploy fresh.
- **Cloudflare Worker is out of scope** — no edits. Worker stays routing/security only.
- After any deploy: **full Cloudflare purge (all pages)**, done by Craig.
- **Scan-path is OUT OF SCOPE.** Do not touch `_increment_scan_counter()` (app.py ~6191-6203) or its 6 call sites; the dual-handler rule does not apply to this change. Recon confirmed the scheduler never touches scan counters — keep it that way.
- **Matching accuracy is an absolute constraint.** No step may let unverified or pre-release images reach either index.
- Work locally in `/home/claude` style scratch first where helpful; final edits land in the repo files named below.

---

## PHASE 0 — Pin the `tcgdexsdk` dependency (do this FIRST, verify before anything else depends on it)

**Why:** `tcgdexsdk==2.3.0` works only because it's in the local venv. It is in **neither** `requirements.txt` nor `pyproject.toml`. A clean Modal image rebuild would drop it — taking down both the new EN probe **and** the existing JP pricing scrape with an `ImportError`.

1. Add `tcgdexsdk==2.3.0` to `requirements.txt` (and to whatever the Modal image installs from in `matchit_modal.py` — check the image definition's `pip_install` list and add it there too if the image does not install from `requirements.txt`).
2. Do **not** change any other pins.
3. **Verification (Craig runs in PowerShell after deploy of the image):**
   `modal run matchit_modal.py::<a trivial function>` or a one-off that executes
   `python -c "import tcgdexsdk; print(tcgdexsdk.__version__)"` **on the deployed image**, and confirm it prints `2.3.0`.

**Acceptance:** the deployed Modal image imports `tcgdexsdk` 2.3.0. Nothing else in this block proceeds until this passes.

---

## PHASE 1 — Release calendar file

Create `set_release_calendar.json` on the Modal volume, in the same data dir as `scheduler_log.json`
(`/modal_data/MatchITv2_ProductMatch_Data/cards/`).

**Schema:**
```json
{
  "sets": [
    {
      "game": "pokemon_en",
      "name": "Mega Evolution: Pitch Black",
      "source_ids": { "pokemontcg_io": "me5", "tcgdex_en": null },
      "release_date": "2026-07-17",
      "ondevice_eligible": true,
      "state": "pending",
      "source_latency": {
        "pokemontcg_io": { "first_listed_at": null, "first_imaged_at": null },
        "tcgdex_en":     { "first_listed_at": null, "first_imaged_at": null }
      }
    }
  ],
  "discovered_unlisted": []
}
```

- `source_ids.*` may be `null` if unknown at seed time (TCGdex IDs often differ and aren't known in advance). The probe (Phase 6) backfills the observed id on first match.
- `state` ∈ `pending | detected | catalog_ingested | indexed_server | ondevice_staged | prelive_hold | live`. (`ondevice_staged` and `prelive_hold` are both pre-live holds — eligible sets hold at the former, ineligible at the latter. The scheduler never sets `live` itself; Craig does, manually.)
- `ondevice_eligible`: true for Pokémon + constructed MTG; false for YGO and anything outside on-device coverage.
- **Seeding is manual (Craig).** Craig populates upcoming sets from the public EN/JP calendars. The file is the source of truth for *when*; the sources are the signal for *ready*.
- Add a small loader/saver in `set_scheduler.py` mirroring the existing `_get_scheduler_log_path` / `_save_scheduler_log` pattern (read-modify-write whole file, same data dir). All writes must be append-only on `source_latency` (never overwrite a non-null timestamp).

**Acceptance:** file loads/saves cleanly; a hand-seeded Pitch Black entry round-trips without mangling.

---

## PHASE 2 — Scheduler → daily date-aware state machine

Target: `scheduled_set_check()` (matchit_modal.py ~291) and `run_scheduler()` (set_scheduler.py ~859-1041).

1. **Cron change:** `modal.Cron("0 1 * * 1")` → `modal.Cron("0 1 * * *")` (daily 1am UTC) at matchit_modal.py ~288.
2. **Date-aware frequency note:** daily is the heartbeat. Within a set's release window (release_date − 2 days through release_date + 14 days), that set is eligible for the `getSync` imaging check on every tick (Phase 6); outside the window, `pending` sets only get the cheap `listSync` listing check. (Single daily cron is sufficient — we do not need sub-daily Modal crons; the window just controls how much work each set triggers.)
3. **State machine wrapper:** refactor `run_scheduler()` so each run iterates every non-`live` calendar entry and attempts to advance it **one** transition. Transitions:
   - `pending → detected`: set appears in pokemontcg.io with a non-zero card count.
   - `detected → catalog_ingested`: run the existing catalog pull + image mirror + lookup rebuilds (Phase 3).
   - `catalog_ingested → indexed_server`: **only if** the final-print gate passes (Phase 4); run existing incremental embed.
   - `indexed_server → ondevice_staged`: build on-device vectors and stage (Phase 5). For `ondevice_eligible == false` sets, skip the on-device build and move to the pre-live hold directly (state `prelive_hold`).
   - **PRE-LIVE HOLD — the scheduler stops here for every set and does NOT auto-promote.** The final flip to `live` is always a manual action by Craig (see Phase 4). The scheduler advances each set up to its pre-live hold (`ondevice_staged` for eligible sets, `prelive_hold` for ineligible) and then waits. It must never set `state: "live"` itself. Craig flips the calendar entry to `live` by hand after his manual checks; the scheduler/app simply respect the `live` flag thereafter.
4. **Idempotent + resumable:** each transition must be safe to re-run. A failed embed leaves the set at its prior state to retry next tick; it must never half-write the index. Reuse the existing incremental-embed guarantees (load → embed-new-only → concatenate).
5. **Retain a weekly full reconcile as a safety net** (belt-and-braces for accuracy): keep `rebuild_lookup_files` full-mode reachable and call it once weekly (e.g. Sunday tick) to catch anything the per-set path missed. Do **not** remove it. Daily runs use per-set/delta only.
6. Preserve all existing post-steps that still make sense (FX cache refresh, log save, email, price alerts).

**Acceptance:** on a dry run with a seeded but not-yet-released set, the set stays `pending` and nothing downstream fires. Advancing states is observable in the calendar file and `scheduler_log.json`.

---

## PHASE 3 — Background chain per set (+ close the `identifier_lookup.json` gap)

When a set enters `detected → catalog_ingested`, run the existing pieces in order, scoped to that set:

1. **Catalog pull (CPU / serve_light path):** `PokemonTCGClient` (scrape_pokemon_tcg.py ~288-327) — `get_sets`, `get_cards_for_set`, `build_profile` (~206-251) for image URLs. SKU = pokemontcg.io `card.id` (already `{set}-{number}`, e.g. `sv8-123`), exactly as today. **No source change.**
2. **Image mirror to R2:** reuse `upload_to_r2()` (r2_util.py ~43-56) via the existing writer path used by `register_scraped_cards()` (backfill_scraped_cards.py ~167-173). New cards auto-mirror to `grailsweep-cards` as today. Dedupe stays the application-level `existing_skus` check (line ~99) — unchanged.
3. **Lookup/sidecar rebuilds (per-set):**
   - `sku_game_map.json`, `set_metadata.json` via `rebuild_lookup_files()` (matchit_modal.py delta path ~530-532 / 731-732).
   - `mtg_set_totals.json` via `rebuild_mtg_set_totals()` (app.py ~5765-5768).
   - per-game card-list sidecars via `rebuild_set_card_lists()` (app.py ~5782-5784 / 6023-6028).
   - **GAP CLOSURE:** wire `identifier_lookup.json` regeneration into this per-set rebuild. Its writer is `build_identifier_lookup.py` (path defined app.py ~5495), currently manual-only. Make the scheduler invoke it (delta if it supports one, else full) so the 155,451-key lookup no longer silently goes stale on new sets. Verify the nested-by-game structure is preserved.

**Acceptance:** ingesting a test set updates all five lookup artifacts **including** `identifier_lookup.json`, and new images appear in R2.

---

## PHASE 4 — Accuracy gates

1. **Final-print gate (before `indexed_server`):** index a set only if **both** are true:
   - the set is present in the catalog source with populated card images, **and**
   - `today >= release_date` from the calendar entry.
   This is data-driven and needs no human "is it final?" flag (the catalog sources don't carry leaks). Catalog/prices may go live earlier; only the **index** waits on both conditions.
2. **Pre-live hold — manual regression check by Craig (NO scheduler-run harness):** the scheduler must **not** run any equivalence harness. The only existing harness (`equivalence_harness.py`) works by POSTing real photos to the live `/match` and `/api/v1/match` endpoints — its own header says "identical side effects to a real user scan", i.e. it increments `_increment_scan_counter()` and writes `match_history` on every run. Auto-wiring it would violate the scan-path-out-of-scope rule. Instead:
   - The scheduler holds each set at its pre-live state and **notifies Craig** with the manual checklist.
   - Craig runs `equivalence_harness.py` himself (exactly as he already does for CPU-twin deploys) and, for eligible sets, performs the manual on-device publish, then flips the calendar entry to `live` by hand.
   - **No new scan-path-adjacent code is written in this block.**

   **What this check actually is (important, honest scoping):** the harness diffs against the **fixed 150-image canary set** (real historical photos with known ground truth). It is a **regression guard on existing cards** — it answers *"did adding this set break matching on unrelated, already-known cards?"* It does **not** and **cannot** validate the new set's own cards, because no real photos of not-yet-released cards exist. Do not describe or treat it as new-set validation. New-set-own-correctness is a separate, currently-unaddressed gap (see note below).
3. **Decoupled freshness:** catalog/display transitions and index transitions are independent — a slow embed must never block catalog freshness, and unverified images must never reach the index.

> **Open accuracy gap (named, not silently closed):** nothing in this block validates that the *new set's own cards* are correctly ingested (right image mapped to right SKU, no embedding corruption, no duplicate-SKU collision). The manual canary harness cannot do this. A scan-path-free self-retrieval sanity check would close it (embed each new card's reference image, confirm it returns itself as top-1 against the index) — this is **deferred** and listed in the DEFERRED section. Flagged here so the gap is a known decision, not an oversight.

**Acceptance:** a set whose `release_date` is in the future, even if already listed upstream, cannot reach `indexed_server`. The scheduler never sets `state: "live"` on its own — every set parks at a pre-live hold until Craig flips it manually. No harness is invoked by scheduler code (grep the scheduler/Modal files: zero references to `equivalence_harness`, `_increment_scan_counter`, `match_history`).

---

## PHASE 5 — On-device: stage-and-notify + single version constant

1. **Single version constant:** the on-device version string is currently hardcoded in **two** places — `build_ondevice_index.py:27` and `app.py:3400` (`/api/heartbeat` `index_version`). Introduce **one** constant as the single source of truth and have both read it. (Pick the lowest-risk shared location, e.g. a small module-level constant imported by both, or a tiny config read; document which.) The browser manifest logic in `templates/match.html` (~125) stays as-is — it compares against `meta.json`, which is produced from the same constant.
2. **Stage, do not auto-publish.** When an eligible set reaches `indexed_server`, the scheduler:
   - builds the new MobileCLIP2-S2 512-dim float16 vectors via `build_ondevice_index.py` (outputs `vectors_f16.npy`, `skus.json`, `meta.json` locally — `main()` ~84),
   - sets state `ondevice_staged`,
   - **notifies Craig** (reuse the existing scheduler email path) with: set name, vector count delta, and the exact manual steps (R2 upload to `models.grailsweep.com/gs-ondevice-v1/` + version bump of the single constant).
   - Does **not** upload to R2 and does **not** bump the live version. That stays Craig's manual step, consistent with the manual-publish posture for user-facing artifacts.
3. **No auto-promotion.** When an eligible set's vectors are staged, the scheduler sets `ondevice_staged` and stops. The flip to `live` is Craig's manual action after he (a) runs `equivalence_harness.py` himself, and (b) performs the manual R2 publish + version bump. The scheduler does not run the harness and does not set `live`.

**Acceptance:** an eligible new set produces staged on-device artifacts + a notify email, and the live `gs-ondevice-v1` index/version is untouched until Craig publishes. The two version strings now derive from one constant (grep proves no second literal remains).

---

## PHASE 6 — Source-latency probe (observation-only, discovery-augmented = option 2)

**Pure observation.** Writes timestamps only. Touches **no** images.db, **no** SKU synthesis, **no** embedder, **no** R2. Zero re-embed exposure.

**Per tick, for every calendar entry not yet `live`:**

1. **`first_listed_at` (cheap, every tick):**
   - pokemontcg.io: check whether the set's `source_ids.pokemontcg_io` (or name match if id null) appears in the sets listing.
   - tcgdex_en: `TCGdex(Language.EN)` → `listSync()` returns `SetResume[]` (`id, name, cardCount`). Check whether the entry appears (by `source_ids.tcgdex_en` if set, else fuzzy name match within release_date ± 14 days). On first match where `source_ids.tcgdex_en` was null, **backfill the observed id** into the calendar.
   - On first observation per source, stamp `first_listed_at` (UTC ISO). Never overwrite a non-null value.
2. **`first_imaged_at` (gated — only after that source's `first_listed_at` is set, only while still null, only for sets in the release window):**
   - pokemontcg.io: confirm cards return with `image_url_large` populated (shape already known from `build_profile`).
   - tcgdex_en: `getSync(set_id).cards[0].image` populated (recon confirmed `CardResume.image` is present on the full `Set` object — no per-card detail fetch needed; do **not** call `_fetch_card_detail`, that's pricing-only).
   - Stamp on first populated observation. Append-only.
3. **Discovery augmentation (the option-2 safety net):** from the listing calls you already made, flag any set that appears on **either** source but matches **no** calendar entry. Append to `discovered_unlisted` as `{ source, observed_id, name, cardCount, first_seen_at }` (first-seen only). This catches early/unannounced sets and tells Craig when manual calendar seeding has fallen behind.

**Storage:** all of the above lives in the calendar entry's `source_latency` block (and `discovered_unlisted`), and a per-run summary is appended into the existing `scheduler_log.json` run dict via `_save_scheduler_log()` (set_scheduler.py ~823-840) so it surfaces on the existing admin summary page (`load_scheduler_log()` ~843). No new log file.

**Cost control:** `listSync` once per source per tick (not per set). `getSync` only for in-window sets still missing `first_imaged_at`. Probe runs as a separate observation pass — it is **not** a state transition and must run regardless of state-machine progress.

**Acceptance:** with Pitch Black seeded, the probe records `first_listed_at`/`first_imaged_at` for both sources on the days they actually appear, backfills the TCGdex id, and logs any unlisted discovery — all visible in the calendar file and scheduler log, with nothing written to images.db.

---

## DEPLOYMENT SEQUENCE (Craig, PowerShell)

1. Backups confirmed for every touched file (`*_pre_setscheduler.*`).
2. `ast.parse` / `node --check` all changed files (Claude Code reports clean).
3. `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`
4. Phase 0 verify: confirm `tcgdexsdk 2.3.0` imports on the deployed image.
5. If behaviour looks stale: `modal app stop matchit-api`, then redeploy.
6. Full Cloudflare purge (all pages).
7. Seed `set_release_calendar.json` with Pitch Black (and any other upcoming sets).

---

## TEST MATRIX

- **Dry/no-op:** seeded future-dated set stays `pending`; no ingest, no embed, no index change.
- **Gate:** listed-but-pre-release set cannot pass the final-print gate.
- **Gap closure:** ingesting a test set updates `identifier_lookup.json` alongside the other four artifacts.
- **On-device:** eligible set → staged artifacts + notify email; live index/version untouched.
- **Probe:** timestamps populate per source on real appearance; TCGdex id backfills; discovery list catches an unseeded set; images.db untouched (verify row count unchanged).
- **No auto-promote:** every set parks at a pre-live hold; scheduler never writes `state: "live"`. Grep proves scheduler/Modal code has zero references to `equivalence_harness`, `_increment_scan_counter`, or `match_history`.
- **Manual go-live:** after Craig runs the canary harness himself (+ publishes on-device for eligible sets) and flips the entry to `live`, the set serves normally.
- **Live scanner sanity (mobile-first):** after a set genuinely goes `live`, confirm match behaviour on phone PWA + phone TWA only (never laptop / Microsoft Store). Matching accuracy unchanged.

---

## ROLLBACK

- Restore the `*_pre_setscheduler.*` backups, redeploy, full Cloudflare purge.
- The probe and calendar are additive/observation-only; deleting `set_release_calendar.json` and reverting the cron to `0 1 * * 1` returns to prior weekly behaviour with no data loss to images.db or either index.

---

## DEFERRED (not in this block — decided by Pitch Black probe data)

- TCGdex-EN as a primary **image** source. Requires cross-source identity mapping (set-id alias + number normalisation) validated against the equivalence harness. Build only if the probe shows pokemontcg.io is materially behind on **images** (not just listing) at release. Until then, pokemontcg.io stays #1.
- **New-set self-retrieval sanity check** (scan-path-free, in-process): embed each new card's reference image and confirm top-1 self-retrieval against the index, to catch wrong-image/SKU mappings, embedding corruption, and duplicate-SKU collisions on the new set itself. This is the only thing that would validate the *new set's own* correctness (the canary harness can't). Build as a separate offline check that imports the matching internals directly — no HTTP, no counters, no `match_history`. Deferred to keep this block scan-path-clean and tightly scoped.
- Scrydex as paid emergency fallback.
- Full automation of the on-device publish.
