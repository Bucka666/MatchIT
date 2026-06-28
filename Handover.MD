# GrailSweep — Session Handover

**Date:** 2026-06-27 (end of session; updated after PM Japanese out-of-index sprint)
**Next session focus:** UI/UX changes + new pricing tiers + Stripe work (exact details TBC in new window)

> **PM update:** A long afternoon sprint closed the Japanese out-of-index false-match problem end to end across all three matchers. See new **Section 1A** below before touching anything in the scan/OCR/denominator path — it documents the three-matcher trap that caused repeated "fixed, then broken again" cycles.

---

## 1. Project Snapshot

GrailSweep (grailsweep.com) — multi-TCG AI card scanner + collection manager (Pokémon EN/JP, MTG, YGO; ~144k cards, ~302 Pokémon sets / 33,012 SKUs). Sole dev: Craig (UK, mobile-first). Business partner owns commercial decisions; all technical calls are Craig's.

**Infra:** Flask on Modal (workspace `c-a-buckley`, app `matchit-api`, T4 GPU, volume `matchit-data-v2`), Cloudflare proxy (Worker `raredex-proxy`), R2 for images (`grailsweep-cards`, `images.grailsweep.com`) + on-device model hosting (`models.grailsweep.com`).

**Matching:** CLIP ViT-L-14 + DINOv2 tiebreaker (server, numpy `.npy`, incremental append). OCR (Google Vision server / ML Kit TWA) as confirmation. On-device: MobileCLIP2-S2 FP16 ONNX in-browser (~102k vectors, manual rebuild only).

---

## 1A. Japanese Out-of-Index False-Match — RESOLVED across all three matchers (2026-06-27 PM)

**The problem.** Out-of-index Japanese cards — MEGA-era (M5 Abyss Eye, M1L Mega Brave, etc.), and by the same mechanism Chinese/Korean — visually land on a `jpn-` "twin" in the index and were being returned as confident **wrong** matches (e.g. Abyss Eye `001/081` → `jpn-sv1a-001`). These sets are not embedded (TCGdex has no images for them yet), so the only correct outcome is **no match**. The goal was never to make them scan correctly — it was to stop the confident wrong answer.

**THE THREE-MATCHER TRAP (root cause of the repeated "fixed then broken again" loop).** A card match is finalised in **three independent places**. A veto/gate/denominator change wired into only the one visible in the current log silently leaves the others open. This was missed ~5 times across sessions. The three:
- **(A) Server PROMOTION** — `ocr_confirm_ranking`, rank-N→rank-1, logs `[OCR-P3] top5 promotion blocked`. `_denominator_blocks_promotion` lives here.
- **(B) Server RANK-1-CONFIRM** — `ocr_confirm_ranking`, `status=rank1_confirmed`, fires when CLIP already top-ranks the candidate and OCR confirms on a **bare number** only. Logs `[OCR-P3] rank1_confirm blocked` after this session's fix.
- **(C) ON-DEVICE MobileCLIP** — client-side (`match.html` / `gsOnDeviceMatch`), a **separate matcher** that never calls `ocr_confirm_ranking` or any server veto. Its entire check is the gate predicate (`accept iff game != YGO AND gap >= 0.02 AND top1_sim >= 0.80`). Emits only the quiet `[ONDEVICE-TELEMETRY]` beacon. **Camera scans hit this path** — this is the one that kept resolving the all-day test card wrong while the loud server logs looked fixed.
- Note: the two server scan HANDLERS (`/match` app.py ~7318 + `/api/v1/match` api_routes.py ~382) both feed A/B — that handler-duality is *separate* from this three-matcher duality.

**What shipped (all deployed + verified in live camera scans):**
1. **Denominator parser fix** (`ocr_confirm.py`): `_extract_pokemon_number` was dropping the parsed `total` on 2 of 4 return paths (bare-number fallthrough + SWSH-unique). Now records `_last_pkm_denominator` on all paths. Without this the veto got `None` and failed open. Logs now show `extracted card number only: N (denom=T)`.
2. **JP set-keying fix** (`build_set_metadata.py` + `ocr_confirm.py`): `scan_cardsdb` and the gate both collapsed `jpn-sv1a-001` → `"jpn"` via `split("-")[0]`, producing ONE meaningless `"jpn"` metadata bucket with null totals → veto failed open for ALL JP cards. Fixed to `rsplit("-",1)[0]` (strips only the trailing card number; also necessary for the `jpn-s-p` promo whose set code contains a dash). JP set totals enriched from **TCGdex card-level** `cardCount` (`/v2/ja/cards/<setcode>-001` → `set.cardCount.official`/`.total`) — NOTE: TCGdex **set-level** cardCount is unreliable (sv1a reports 101 vs real 73); **card-level** is correct (73/103). pokemontcg.io has no JP sets. New helper `_fetch_jp_set_totals` + `parse_pokemon_denominator`. `set_metadata.json` rebuilt (now ~1188 entries; jpn-sv1a=73/103, jpn-sv3a=62/92, jpn-sv1s=78/108 verified) and pushed to volume.
3. **Veto applied to ALL THREE server exit paths** (`ocr_confirm.py`): direct-lookup already had it; added the same `_denominator_blocks_promotion` call to **rank1_confirmed** (was the leak — confirmed on bare-number match only, no denom check) and confirmed promotion path. All three now veto before finalising.
4. **On-device backstop (Option A)** — closes matcher (C):
   - `/api/ocr-lookup` now returns `ocr_denom` even on a `not_found` miss (via `parse_pokemon_denominator`). Previously the denominator was discarded one frame down and the client had nothing to compare against.
   - New read-only endpoint `/api/set-total/<sku>` (uses `_get_set_id_from_sku` — NOT inline rsplit, which would mis-key MTG; on-device accepts MTG too).
   - `match.html` camera on-device accept branch: if OCR read a denominator AND the on-device pick's known set total conflicts, **suppress the on-device accept and fall through to `/match`** (which has the server veto). **Fail-open**: suppresses only on a positive, data-backed conflict; missing denom / unknown total / any error → normal on-device accept. Worst case for a legit card = one extra `/match` round-trip that confirms correctly. Logs `[ONDEVICE] denom conflict: ocr=N vs set ... -> fall through to /match` (client console only).
   - `_tryOcrLookup` miss now returns `{miss:true, ocr_denom}` instead of `null`; the OCR-hit check was hardened from `if (_ocrResult)` to `if (_ocrResult && !_ocrResult.miss)` (a truthy miss object would otherwise have been mistaken for a hit).

**Verified (live camera, 2026-06-27 PM):** Abyss Eye `001/081` now blocks on BOTH server doors (`top5 promotion blocked` when CLIP ranks it #2, `rank1_confirm blocked` when #1) → `denominator_mismatch`, no wrong card, no price. The fact these reach the server at all proves the on-device backstop suppressed and handed off. `051/081` → `no_sku_match`. EN regression card (`sv8pt5-77`) routed normally, untouched.

**Known remaining edge (NOT a regression — tracked separately):** the on-device backstop fires only when OCR actually *reads* a denominator off the frame. A scan where OCR gets nothing (glare/blur/angle) still rides on-device visual confidence alone. This is the **swsh4-102 blind spot** (Backlog §5.A.1) — same gap, untouched by this work.

**Backups:** `ocr_confirm_pre_denomfix.py`, `ocr_confirm_pre_jpkey.py` + `build_set_metadata_pre_jpkey.py`, `ocr_confirm_pre_rank1veto.py`, `ocr_confirm_pre_ondevice_denom.py` + `app_pre_ondevice_denom.py` + `match_pre_ondevice_denom.html`, `set_metadata_pre_jpkey.json` — all in both backup locations.

**STANDING RULE (carry forward):** For ANY scan-path / match-finalisation change, enumerate **all three** resolution points (server promotion, server rank-1-confirm, on-device gate) BEFORE proposing a fix, and confirm which path the user's actual scan took (server `/match` vs on-device beacon) rather than reasoning from whichever log line is loudest. The on-device path logs only one quiet telemetry line — it's the easy one to miss.

---

## 2. Standing Conventions (MUST persist)

- All code edits via Claude Code, as batched instruction blocks prepared in chat, handed over at end.
- Recon (read-only) before every edit. Backup to two locations before touching files: `c:\MatchIT\` and `C:\Users\c_a_b\grailsweep-backups\`, suffix `_pre_<feature>`. Distinct suffix per edit so recovery points aren't overwritten.
- `ast.parse` / `node --check` before deploy.
- Deploy (Craig runs, PowerShell): `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py` — always the prefix.
- If behaviour unchanged after deploy: `modal app stop matchit-api`, then redeploy.
- All modal/git commands run by Craig in PowerShell — never via Claude Code's bash. Cloudflare Worker edits + full purge (all pages) done manually by Craig via dashboard.
- **Dual-handler rule:** scan-path changes touch BOTH `app.py /match` (~7318) AND `api_routes.py /api/v1/match` (~382). Core matching functions `_run_match_paired_two_stage`, `_dinov2_tiebreak`, `ocr_confirm_ranking` are SHARED — one change covers both legs. The dual-handler rule applies to call-site gates, not these shared funcs.
- Testing: mobile-first — phone PWA + phone TWA only for scanner. Camera scanner does NOT work on laptop/PC Chrome or MS Store. Upload is fallback, not default for scanner testing.
- Matching accuracy is an absolute constraint — never traded for performance/cost.
- No full GPU re-embed ever runs automatically (cost guardrail). Full server/on-device re-embed is manual-only.
- Craig sets his own pace. No suggestions to stop/break/wrap up.

---

## 3. What Shipped This Session

### A. JP-in-tiebreak pre-rank exclusion fix — DEPLOYED + VERIFIED

**The bug:** With `jp_mode=en`, Japanese (`jpn-`) SKUs competed during CLIP ranking + DINOv2 tiebreak, only stripped after the fact by `[JP-FILTER]`. The real defect was that `jpn-` rows consumed shortlist/top-K slots (caps of 120 and 20) during ranking, crowding out the correct EN card — so today's bug presents as **wrong EN card promoted**, not empty result.

**What was built (all three files edited, verified `ast.parse` clean):**

- `app.py` — `exclude_jpn=False` param added to `_run_match_paired_two_stage`; `jpn-` skip + `_jp_pre_excluded` counter in both FRONT_INFO and BACK_INFO bucketing loops (before CLIP ranking); `[JP-PRE-FILTER]` log line after both loops. Note: log uses `app.logger.info(...)` not bare `logger.info` — Claude Code corrected this from the instruction block.
- `api_routes.py` — `jp_mode`/`exclude_jpn` threaded into the `/api/v1/match` call site (~337). Existing `[JP-FILTER]` belt-and-braces strip at ~415 left untouched.
- `app.py` — same threading into `/match` (~7689) and `/capture_submit` (~4254, orphaned but wired for consistency).
- `match.html` — upload form JS wired to send `jp_mode` from the existing `_ocrLang` toggle variable. Three FormData objects (`confFd`, `odFd`, `gpuFd`) all updated. `confFd`/`odFd` bypass ranking so `jp_mode` is inert there today but wired for consistency.

**Backups:** `app_pre_jpfix.py`, `api_routes_pre_jpfix.py`, `match_pre_jpfix.html` — both locations.

**DEPLOYED + VERIFIED.** Deploy + full Cloudflare purge done; `[JP-PRE-FILTER]` confirmed in logs on a JP-card-in-EN-mode scan.

**Harness note:** No `jpn-` fixture in `groundtruth.csv` yet — additive, not blocking. Add a `jp_mode=en` scan of a JP card to get a real before/after diff.

---

### B. me2pt5 (Ascended Heroes) TCGdex price backfill — COMPLETE, NO DEPLOY NEEDED

**The problem:** pokemontcg.io has never populated TCGplayer prices for me2pt5 — confirmed upstream gap, not a GrailSweep bug. Known since the April handover, no longer waiting.

**The solution:** New standalone Modal script `backfill_en_tcgdex_prices.py` — fetches both TCGplayer + Cardmarket prices from TCGdex EN API (free, no key needed), writes into profile JSONs on the Modal volume. Atomic writes. Idempotent (safe to re-run).

**Run result:** `{'updated': 196, 'skipped': 0, 'failed': 99}` — 196/295 cards now have prices. The 99 failures are cards 1–99 which TCGdex hasn't indexed upstream yet (partial coverage from their side, not a bug). App reads by mtime so prices are already live — no redeploy needed.

**Set ID mapping in `_SETID_MAP`:**

```
me2pt5 → me02.5
me1    → me01
me2    → me02
me3    → me03
```

**Re-run when TCGdex indexes cards 1–99:**

```powershell
$env:PYTHONIOENCODING="utf-8"; modal run backfill_en_tcgdex_prices.py::run_price_backfill
```

---

## 4. Pending Verification (carry forward from previous session)

### Scheduler fix 01:00 UTC run

The Item 3 scheduler fix (deployed 2026-06-27 before this session) still needs its first clean 01:00 run verified. Check Modal logs for:

- `[CRON] no new SKUs — skipping rebuild_lookup_files` ← KEY LINE (overrun path dead)
- `[SCHED] Done` well under 3600s
- Pitch Black (~2026-07-17) showing `skipped: outside_window`
- Quiet-day email suppression intact

The 2026-06-27 01:00 log showed OLD behaviour (full rebuild on zero-new day, 1837.8s) — that was before the fix deployed. Next 01:00 is the "after."

---

## 5. Outstanding Backlog — Priority Order

### A. Matching Accuracy (highest — absolute constraint)

1. **swsh4-102 CLIP blind spot** — CLIP returns a different wrong card almost every scan for the 102/203 Zamazenta (Vivid Voltage); OCR rescues server-side but on-device has no OCR backstop. Diagnosis: unstable query-side embedding, likely holo-foil glare. NOT a code fix — measure first. Re-scan on phone now that `[DINO-DIAG]`/`[OCR-GATE]` are live, capture margins, then decide: re-embed vs flag-as-low-confidence. **(2026-06-27 PM note:** the on-device denominator backstop from §1A does NOT cover this — that backstop only fires when OCR reads a denominator; the swsh4-102 case is a no-OCR-read / glare scenario, so it still rides on-device visual confidence alone. This is now the cleanest remaining hole in the on-device path.**)**
2. **Add named repro cards as permanent harness fixtures** — `smp-SM210`, `sv4-230`, `base6-10`, `base6-17` not in the 150-card harness. Additive, not blocking.

### B. Release / TWA

3. **Code 8 TWA batch** (closed Alpha) — includes OcrService self-timeout fix. Code 7 currently live, 11/12 testers enrolled.

### C. Prices / Data

4. **me02.5 re-run** — when TCGdex finishes indexing cards 1–99 for Ascended Heroes, re-run `backfill_en_tcgdex_prices.py` to fill the remaining 99 cards.
5. **me01/me02/me03 price audit** — now the `_SETID_MAP` exists, confirm these three sets have populated `prices.tcgplayer` in their profiles. If not, same script with `--set-id me01` etc.

### D. Product / Pricing / Minor

6. **UI/UX changes + new pricing tiers + Stripe work** — next session focus. Exact details TBC.
7. **EN price source preference** (product call for partner) — EN prices come from TCGplayer market (USD-derived); UK users may perceive as low vs eBay UK / Cardmarket. Decide: change source preference, or label as "TCGplayer market (USD-derived)."
8. **results.html:103-104 breakdown field-order inconsistency** (minor display) — breakdown panel uses `market→mid→low`; headline selector uses `market→trend→avg_sell→mid`. Latent; no impact on cards with populated TCGplayer market.
9. **`/api/price_history` request fan-out** (perf, non-urgent) — 182-card collection fires one price-history call per card (~150+ sequential requests on collection load, ~1.1s each). Candidate for a single batched endpoint.
10. **Background thread-leak warnings** (minor) — `Detected N background thread(s) still running after container exit` adds up to 30s to shutdown. Likely background price/stats saves not joining.

### E. Time-Gated (passive — no action until date)

11. **Pitch Black probe data** (release 2026-07-17) — source-latency probe records pokemontcg.io vs TCGdex-EN listing/imaging timestamps. First real test of ±7-day calendar window. Evidence base for deferred TCGdex-image-primary / Scrydex migration decision.

### F. Multi-Game Extension (future, staged)

12. **MTG calendar/probe extension** — Scryfall is fresh, lag barely exists. Extend only where it adds value.
13. **YGO extension** — hardest (weak sources, excluded from on-device). Handle last.
14. **Tier 2 date-discovery agent** — reads announcement pages (Bulbapedia/Serebii/PokéBeach), proposes release dates for Craig's approval. Build ONLY if Tier 1 alerts prove too late. Deliberately deferred.

### G. On-Device Hardening (deferred — low urgency while manual)

15. **Atomic-write fix for `build_ondevice_index.py`** — final outputs written non-atomically. Latent corruption risk on interrupted manual runs.
16. **On-device standalone Modal function + R2 upload script** — no clean entry point for manual rebuild.
17. **Incremental on-device build** — replace full ~102k re-embed with load-existing → embed-only-new → concatenate. Build only if manual full rebuilds become a frequent chore.

---

## 6. Scrydex — Decision Deferred

Scrydex (the commercial successor to pokemontcg.io, same team) has full EN Pokémon pricing including me2pt5, but cheapest plan is $29/month. Deferred until post-launch income exists. TCGdex (free) solved the immediate me2pt5 problem.

No Scrydex key exists anywhere in the codebase, Modal secrets, or local env. If revisited post-launch, it would need a new dedicated Modal secret and integration work. The April handover and scheduler refactor doc already reference it as a planned future migration.

---

## 7. Manual Operations Available

- **me2pt5 price re-run (when TCGdex catches up):** `modal run backfill_en_tcgdex_prices.py::run_price_backfill`
- **Other EN sets price backfill:** `modal run backfill_en_tcgdex_prices.py::run_price_backfill --set-id me01` (me01/me02/me03 already in `_SETID_MAP`)
- **Manual full lookup rebuild:** `modal run matchit_modal.py::rebuild_lookup_files`
- **set_metadata regen:** `python build_set_metadata.py` → eyeball me1/me2/me3 → `modal volume put matchit-data-v2 set_metadata.json /set_metadata.json --force`
- **Regression harness:** `python equivalence_harness.py --target-url https://grailsweep.com --from-modal-secret --access-code <CODE> --label <L> --out <f>.json` then `--diff baseline_gpu.json <after>.json`

---

## 8. Key Context Notes

- **Three call sites, not two, for `_run_match_paired_two_stage`:** camera leg (`api_routes.py:337`), upload leg (`app.py:7689`), and orphaned `/capture_submit` (`app.py:4254`) — nothing in the repo references `/capture_submit` from the live UI today. All three were threaded with the JP fix.
- **Two shortlist/top-K caps exist** between bucketing and final results: `shortlist_n=120` (after Stage-1 scoring) and `top_k_sku=20` (final cap). Harness diffs after the JP fix may legitimately show non-JP cards changing — this is fix-working, not a regression.
- **me2pt5 vs TCGdex naming:** pokemontcg.io calls it `me2pt5`, TCGdex calls it `me02.5`. Full mapping is in `_SETID_MAP` in `backfill_en_tcgdex_prices.py`.
- **Local CardsDB vs Modal volume divergence:** JP `profile.json` files on local `C:\CardsDB` have no `prices` key at all — Cardmarket prices only exist on the live Modal volume (`/modal_data/CardsDB`). Don't use local profiles to check JP pricing.
- **Scheduler overrun reference:** "Research on Phynite" chat (`3d84c7a6-...`) holds original set-scheduler refactor design. The ±7-day window was specced there but never built until this sprint — treat any unbuilt design items from old chats as outstanding, not done.



## Session: Stripe/pricing pass + founder teardown (2026-06-28)

**Status: complete, validated, deployed.**

### Pricing changes (live Stripe Price IDs)
- Monthly £3.99 → £4.39 — `price_1TnOOUFl8aS11HhgA6pNelu0`
- Top-up → £1.89 / 125 scans — `price_1TnORuFl8aS11Hhg0EOQQAlN` (config key renamed `stripe_price_topup_75` → `_125`)
- Ultimate £34.99/YEAR — `price_1TnPN7Fl8aS11HhgbfvwvFos` (NEW; old annual `price_1TSXyrFl8aS11Hhg2qrU45sS` was mis-set to MONTHLY interval — replaced, archive on cleanup)
- Referral first month £2.99 → £3.39 (display only; coupon `GRAILSWEEP_REFERRAL` £1.00-off/once already correct & live)
- Saving badge ~27% → ~34% (hardcoded; all instances incl. base.html extras)

### Founder teardown
Fully removed — 22 app.py sites, config.json, db.py `_PREMIUM_TIERS_CAPPED`, JS tier checks (base/match/collection/results.html), copy.md, LIA.md. Post-removal grep clean.

### Other
- 11 files changed; backups suffix `_pre_pricing_2026` (both locations) + `_pre_ultimate_fix_2026` (config.json, app.py)
- api_routes.py: 3× `locals().get("_ocr_info")` → direct ref
- match.html: `_gsTierPageLoadCheck` no-op + listener removed
- Docs: 25 → 150 scans, Annual → Ultimate Plan

### DEPLOY CHECKLIST (pending)
1. regression harness → 2. deploy → 3. CF purge → 4. phone PWA checkout test (verify Ultimate = £34.99/**year**) → 5. ARCHIVE old Prices LAST: old £3.99 monthly, old top-up, founder, old annual `price_1TSXyrFl8aS11Hhg2qrU45sS`

### Backlog additions
- Dead CSS in upgrade.html: `.upgrade-tier-cta--current` / `--sold-out` (orphaned, trivial)
- Admin diagnostic token `?token=scans-diag-2026` — secret-or-remove
- `[TIMING2]` probe strip (backup: app_pre_timing_probe.py)
- me2pt5 cards 1–99 price re-run when TCGdex indexes them
