# GrailSweep — Session Handover

**Date:** 2026-06-27 (end of session)
**Next session focus:** UI/UX changes + new pricing tiers + Stripe work (exact details TBC in new window)

---

## 1. Project Snapshot

GrailSweep (grailsweep.com) — multi-TCG AI card scanner + collection manager (Pokémon EN/JP, MTG, YGO; ~144k cards, ~302 Pokémon sets / 33,012 SKUs). Sole dev: Craig (UK, mobile-first). Business partner owns commercial decisions; all technical calls are Craig's.

**Infra:** Flask on Modal (workspace `c-a-buckley`, app `matchit-api`, T4 GPU, volume `matchit-data-v2`), Cloudflare proxy (Worker `raredex-proxy`), R2 for images (`grailsweep-cards`, `images.grailsweep.com`) + on-device model hosting (`models.grailsweep.com`).

**Matching:** CLIP ViT-L-14 + DINOv2 tiebreaker (server, numpy `.npy`, incremental append). OCR (Google Vision server / ML Kit TWA) as confirmation. On-device: MobileCLIP2-S2 FP16 ONNX in-browser (~102k vectors, manual rebuild only).

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

### A. JP-in-tiebreak pre-rank exclusion fix — BUILT, NOT YET DEPLOYED

**The bug:** With `jp_mode=en`, Japanese (`jpn-`) SKUs competed during CLIP ranking + DINOv2 tiebreak, only stripped after the fact by `[JP-FILTER]`. The real defect was that `jpn-` rows consumed shortlist/top-K slots (caps of 120 and 20) during ranking, crowding out the correct EN card — so today's bug presents as **wrong EN card promoted**, not empty result.

**What was built (all three files edited, verified `ast.parse` clean):**

- `app.py` — `exclude_jpn=False` param added to `_run_match_paired_two_stage`; `jpn-` skip + `_jp_pre_excluded` counter in both FRONT_INFO and BACK_INFO bucketing loops (before CLIP ranking); `[JP-PRE-FILTER]` log line after both loops. Note: log uses `app.logger.info(...)` not bare `logger.info` — Claude Code corrected this from the instruction block.
- `api_routes.py` — `jp_mode`/`exclude_jpn` threaded into the `/api/v1/match` call site (~337). Existing `[JP-FILTER]` belt-and-braces strip at ~415 left untouched.
- `app.py` — same threading into `/match` (~7689) and `/capture_submit` (~4254, orphaned but wired for consistency).
- `match.html` — upload form JS wired to send `jp_mode` from the existing `_ocrLang` toggle variable. Three FormData objects (`confFd`, `odFd`, `gpuFd`) all updated. `confFd`/`odFd` bypass ranking so `jp_mode` is inert there today but wired for consistency.

**Backups:** `app_pre_jpfix.py`, `api_routes_pre_jpfix.py`, `match_pre_jpfix.html` — both locations.

**DEPLOY STILL NEEDED.** Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py
```

Then full Cloudflare purge. Then verify `[JP-PRE-FILTER]` appears in logs on a JP-card scan in EN mode.

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

1. **Deploy JP-in-tiebreak fix** (see §3A above — built this session, not yet deployed).
2. **swsh4-102 CLIP blind spot** — CLIP returns a different wrong card almost every scan for the 102/203 Zamazenta (Vivid Voltage); OCR rescues server-side but on-device has no OCR backstop. Diagnosis: unstable query-side embedding, likely holo-foil glare. NOT a code fix — measure first. Re-scan on phone now that `[DINO-DIAG]`/`[OCR-GATE]` are live, capture margins, then decide: re-embed vs flag-as-low-confidence.
3. **Add named repro cards as permanent harness fixtures** — `smp-SM210`, `sv4-230`, `base6-10`, `base6-17` not in the 150-card harness. Additive, not blocking.

### B. Release / TWA

4. **Code 8 TWA batch** (closed Alpha) — includes OcrService self-timeout fix. Code 7 currently live, 11/12 testers enrolled.

### C. Prices / Data

5. **me02.5 re-run** — when TCGdex finishes indexing cards 1–99 for Ascended Heroes, re-run `backfill_en_tcgdex_prices.py` to fill the remaining 99 cards.
6. **me01/me02/me03 price audit** — now the `_SETID_MAP` exists, confirm these three sets have populated `prices.tcgplayer` in their profiles. If not, same script with `--set-id me01` etc.

### D. Product / Pricing / Minor

7. **UI/UX changes + new pricing tiers + Stripe work** — next session focus. Exact details TBC.
8. **EN price source preference** (product call for partner) — EN prices come from TCGplayer market (USD-derived); UK users may perceive as low vs eBay UK / Cardmarket. Decide: change source preference, or label as "TCGplayer market (USD-derived)."
9. **results.html:103-104 breakdown field-order inconsistency** (minor display) — breakdown panel uses `market→mid→low`; headline selector uses `market→trend→avg_sell→mid`. Latent; no impact on cards with populated TCGplayer market.
10. **`/api/price_history` request fan-out** (perf, non-urgent) — 182-card collection fires one price-history call per card (~150+ sequential requests on collection load, ~1.1s each). Candidate for a single batched endpoint.
11. **Background thread-leak warnings** (minor) — `Detected N background thread(s) still running after container exit` adds up to 30s to shutdown. Likely background price/stats saves not joining.

### E. Time-Gated (passive — no action until date)

12. **Pitch Black probe data** (release 2026-07-17) — source-latency probe records pokemontcg.io vs TCGdex-EN listing/imaging timestamps. First real test of ±7-day calendar window. Evidence base for deferred TCGdex-image-primary / Scrydex migration decision.

### F. Multi-Game Extension (future, staged)

13. **MTG calendar/probe extension** — Scryfall is fresh, lag barely exists. Extend only where it adds value.
14. **YGO extension** — hardest (weak sources, excluded from on-device). Handle last.
15. **Tier 2 date-discovery agent** — reads announcement pages (Bulbapedia/Serebii/PokéBeach), proposes release dates for Craig's approval. Build ONLY if Tier 1 alerts prove too late. Deliberately deferred.

### G. On-Device Hardening (deferred — low urgency while manual)

16. **Atomic-write fix for `build_ondevice_index.py`** — final outputs written non-atomically. Latent corruption risk on interrupted manual runs.
17. **On-device standalone Modal function + R2 upload script** — no clean entry point for manual rebuild.
18. **Incremental on-device build** — replace full ~102k re-embed with load-existing → embed-only-new → concatenate. Build only if manual full rebuilds become a frequent chore.

---

## 6. Scrydex — Decision Deferred

Scrydex (the commercial successor to pokemontcg.io, same team) has full EN Pokémon pricing including me2pt5, but cheapest plan is $29/month. Deferred until post-launch income exists. TCGdex (free) solved the immediate me2pt5 problem.

No Scrydex key exists anywhere in the codebase, Modal secrets, or local env. If revisited post-launch, it would need a new dedicated Modal secret and integration work. The April handover and scheduler refactor doc already reference it as a planned future migration.

---

## 7. Manual Operations Available

- **JP fix deploy:** `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py` then full Cloudflare purge
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
