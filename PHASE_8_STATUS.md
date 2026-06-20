# Phase 8 — Google Play Submission Status

**Last updated:** 27 May 2026 (late evening — multiple updates today)
**Phase status:** Closed testing sent for review; awaiting Google approval. A56 smoke test passed. Three production fixes deployed and verified (cardgrade, price alert cancel, mobile affordance).
**Phase 7 status:** Complete (PWABuilder package signed, keystore backed up 4x, sideload tested on Galaxy A56)

---

## 1. Account & app

| Item | Value |
|---|---|
| Play Console account type | Personal |
| Developer account ID | 7483375055861179710 |
| App name | GrailSweep |
| Package name | com.grailsweep.app (locked, permanent) |
| App category | Productivity (no tags) |
| Default language | English (en-GB) |
| App type | App, Free |
| Target audience | 13+ (no under-13 declarations — avoids COPPA / GDPR-K) |
| Identity verification | ✅ Approved 26 May 2026 |
| Android Developer Verification | ✅ Auto-registered (no action needed) |

---

## 2. Release tracks

### Internal testing (LIVE)
- **Release:** 1 (1.0.0)
- **Published:** 27 May 2026 00:22
- **Status:** Available to internal testers, full roll-out
- **AAB:** GrailSweep.aab, 1.6 MB, target SDK 35, API 23+
- **A56 smoke test:** ✅ PASSED 27 May 2026 — package com.grailsweep.app installed and ran cleanly via Play Store TWA path, all features working as expected
- **Testers:** Closed Beta Cohort 1 (3 users — self + business partner + 1)
- **Opt-in URL:** https://play.google.com/apps/internaltest/4701391892198806409

### Closed testing — Alpha (SENT FOR REVIEW)
- **Release:** 1 (1.0.0) — same AAB as internal, added from library
- **Sent for review:** 27 May 2026 (evening)
- **Status:** Awaiting Google approval (expected 24h–3 days)
- **Countries/regions:** United Kingdom only
- **Tester list:** "Closed Beta Cohort 1" (renamed from "in-house testing Grailsweep Android"; rename bundled with the in-flight review)
- **Feedback channel:** support@grailsweep.com
- **Managed publishing:** Off (auto-publishes to closed track on approval)
- **Tester count:** 3 currently; expanding to 15 as confirmations come in
- **Opt-in URL:** Will appear under Testing → Closed testing → Manage track → "How testers join your test" once Google approves

### Production
- **Status:** Gated on closed test completion (12 testers × 14 consecutive days) + billing-compliance resolution
- **Billing approach at production:** TBD (current closed test ships Stripe Custom Tab as-is per Option 1 decision)

---

## 3. Signing keys

| Key | SHA-256 |
|---|---|
| Upload key (PWABuilder, local) | 71:33:4D:D1:08:13:87:C8:B0:9C:AB:F9:04:94:C2:4C:8B:E5:03:AE:02:B9:43:9A:C0:D3:B7:80:C9:83:8A:FA |
| Play app signing key (Google) | 50:B2:FA:6D:CE:B7:8F:BC:F9:5C:E3:CC:F2:4F:66:B9:B3:1A:B3:BE:1D:67:E4:A0:88:E9:33:44:50:F7:40:C1 |

**Both** are live in assetlinks.json (Flask route at app.py:3415-3418, inline JSON).
Verified against Google's Digital Asset Links validator on 27 May — returns both statements cleanly.
A56 smoke test on 27 May confirmed package signing validates end-to-end on real-device Play Store install.

---

## 4. Play Console declarations (all approved & published 27 May)

| Declaration | State |
|---|---|
| Privacy policy | https://www.grailsweep.com/privacy |
| App access | Reviewer code GRAIL-REVW-GOOG provided |
| Ads | None |
| Content rating | All-ages (Productivity utility) |
| Target audience | 13+ |
| News app / COVID / Government / Health / Financial | All No |
| Data safety | 6 data types declared (see section 5) |

---

## 5. Data safety declarations

Collected:
- Email address (collected + shared with Stripe and Resend; required; account mgmt + dev comms)
- Purchase history (collected + shared with Stripe; required; account mgmt + dev comms)
- Photos (collected, NOT shared; non-ephemeral; required; app functionality)
- Files and docs (collected, NOT shared; optional; app functionality — CSV imports)
- App interactions (collected, NOT shared; required; app functionality + analytics)
- Device or other IDs (collected, NOT shared; required; fraud prevention/security/compliance)

Security practices:
- All data encrypted in transit (HTTPS via Cloudflare + Modal TLS)
- Account deletion URL: https://www.grailsweep.com/delete-account
- Partial deletion URL: same

**Note:** Photos declared as non-ephemeral because /modal_data/query/ retention is now 30 days (was previously unbounded). Once the 30-day sweep has been running stably for several weeks, the Data safety declaration can be updated to indicate "automatically deleted within 30 days" — this is amendable without re-review.

---

## 6. Reviewer access code

| Field | Value |
|---|---|
| Code | GRAIL-REVW-GOOG |
| Tier | lifetime |
| Email | support@grailsweep.com |
| Expiry | 2027-05-26 (1 year) |
| Device fingerprint limit | 5 |
| Deep grade monthly limit | 999 (effectively unlimited) |
| is_reviewer flag | true |

Stored in /modal_data/subscriptions.json on Modal volume matchit-data-v2.
Verified working via A56 smoke test (Modal logs confirm validate_premium matches, deep grade fires, all Pro features unlock).

---

## 7. Closed beta tester management

### Tester codes generated

15 unique annual subscription codes generated on 27 May 2026 via `generate_tester_codes.py`.

| Field | Value |
|---|---|
| Code format | GRAIL-XXXX-XXXX (matches paying-customer format) |
| Tier | annual |
| Tier label for DMs | Annual Pro |
| Expiry | 2027-05-27 (365 days) |
| Device fingerprint limit | 3 |
| is_tester flag | true |
| is_reviewer flag | false |
| Source | "closed_beta_tester" |
| Email | Empty at generation; populated at redemption |
| Schema fields | Matches `_issue_new_code` output: includes `stripe_subscription_id: null`, `devices: []`, `created_at`/`expires_at` in `.isoformat()` format |

**Backup:** `C:\Users\c_a_b\grailsweep-backups\subscriptions_20260527_173040.json` (3,444 bytes, taken before changes)

**Volume state:** subscriptions.json went from 8 entries → 23 entries on Modal volume matchit-data-v2. All 8 original codes (including GRAIL-REVW-GOOG) confirmed untouched.

**Validation:** GRAIL-5237-C54C tested locally — returns `valid=True`, `tier=annual`, `device_fingerprint_limit=3`, `is_tester=True`. Live Cloudflare endpoint validation confirmed via A56 smoke test.

### Tester recruitment

| Item | Status |
|---|---|
| WhatsApp group | ✅ Set up; existing confirmed testers added |
| Confirmed testers | 3 (self + business partner + 1) |
| Outstanding | 12 more needed (targeting 15 total for dropout buffer) |
| Welcome message | ✅ Drafted (no-link-yet version) |
| Install instructions | ✅ Drafted (placeholder for opt-in URL until Google approves) |
| Per-tester DM template | ✅ Drafted (quick + detailed variants) |
| Tracking CSV | ✅ `tester_codes.csv` in project root, .gitignored |
| Tier to advertise in DMs | ✅ Annual Pro |

### Key rules to remember
- 14-day clock only starts when 12+ testers have **opted in** via the link (not when added to email list)
- If any tester opts out or uninstalls before day 14, clock resets — hence the 15-tester buffer target
- Adding more testers after clock starts is safe and does not reset the clock
- All testers must opt in with the same Gmail account they'll use on their Android device

---

## 8. Code changes shipped this phase

| Change | File(s) | Purpose |
|---|---|---|
| Reviewer code | subscriptions.json | Google Play reviewer access |
| Device fingerprint limit parameterised | app.py:5962 | Per-code device cap (default 3, reviewer = 5) |
| Delete account/data page | templates/delete_account.html, app.py route + sitemap entry | Google Play Data safety requirement |
| Query image cleanup (startup sweep, 30-day TTL) | matchit_modal.py | Replaces unbounded /modal_data/query/ growth |
| api_routes.py cleanup uncommented | api_routes.py:562-564 | /api/v1/match container-FS cleanup |
| One-shot historical purge | purge_query_images.py | One-time clear of accumulated query/ cruft |
| Play app signing key fingerprint added | app.py:3417 (inline assetlinks.json) | TWA validation for Play Store installs |
| 15 closed beta tester codes | subscriptions.json (Modal volume) | Closed beta tester pool |
| Tester code generator script | generate_tester_codes.py (project root) | Batch code generation for future cohorts |
| tester_codes.csv | project root (gitignored) | Craig's tracking sheet for tester DMs |
| **window._cardGrade deep grade fix** | **templates/results.html:1243** | **`window._cardGrade = d;` added inside `/api/deep_grade` success handler, after `if (d.error) { return; }` guard. Ensures Add-to-Collection saves deep grade when one has been run; auto-grade preserved when deep grade fails or isn't triggered. Verified end-to-end. Single-grade replace model — `grade.method` ("auto" vs "deep") drives badge and bars in collection display + CSV/XLSX export.** |
| **Price alert cancel button** | **templates/collection.html (~line 759 + ~line 220) + new `colCancelAlert` function** | **Static "🔔 Alert set" label converted to functional cancel button. Calls `/api/delete-alert` with `{email, code, sku}` (backend deletes on email+sku composite, so email persistence was added in additive 2-line patch inside `colSetAlert` success handler — stored in `col_alert_emails_v1` localStorage key with prompt fallback for legacy pre-deploy alerts). New CSS class `.col-alert-btn--set` for filled orange state with red hover. Single-click cancel, no confirmation prompt.** |
| **Price alert mobile affordance** | **templates/collection.html (~line 759)** | **Button label changed from "🔔 Alert set" to "🔔 Alert set ✕" so touch users see an always-visible dismiss affordance without needing hover. Verified working on desktop and A56.** |
| **Service worker cache version bumps** | **static/sw.js:2** | **`grailsweep-v5` → `v6` (cardgrade fix), `v6` → `v7` (price alert cancel), `v7` → `v8` (mobile affordance). Each bump forces clients to flush stale JS on next load.** |
| **Unified grading fix (30–31 May 2026)** | **app.py:5528, api_routes.py:563-564, match.html (multiple), sw.js v9→v11** | **Comprehensive multi-surface grading consistency pass. Root causes were a stack of independent bugs: (1) `_rule_based_grade` returning None on multiple match paths → fixed with new `_safe_grade()` wrapper that always returns a populated grade or "Grade unavailable" fallback, applied to all match endpoints (`/match` web, `/api/v1/match` API). (2) Scanner camera path's session-list + button (`scQuickAdd`) had no defensive grade capture → hardened with explicit grade extraction + on-screen toast diagnostic. (3) Detail overlay ⭐ Add button (`gsDetailAddToCollection`) saved cards with no grade field at all → rebuilt with matching defensive guard. (4) `scRunDeepGrade` never persisted deep grade result beyond DOM innerHTML → fixed to write `_gsDetailCard.grade` and sync into `scCards` by SKU, mirroring results.html line 1244 pattern. Final state: every match path returns a grade, every save path includes it, both auto and deep persist across cold starts, all surfaces consistent.** |

All deploys via standard command:
`$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`

Backups for every file modification live in C:\Users\c_a_b\grailsweep-backups\

---

## 9. Backlog (parked, not blocking closed test)

| Item | Effort | Notes |
|---|---|---|
| Resend email migration (from pre-Phase-8 backlog) | TBD | **Targeted for Sunday 31 May**; price alert path already on Resend per Modal logs |
| Billing compliance for production submission | TBD | Three options: integrate alt-billing API, hide Stripe in Android build, or switch to Play Billing |
| Update Data safety: Photos ephemeral declaration | 5 min Play Console edit | Once 30-day sweep has stable runtime history |
| Add app tags | 2 min Play Console edit | If Google adds collecting/hobbies tags in a taxonomy update |
| Auto-grade calibration review | Post-closed-test | Audit confirmed full mapping (Gem Mint / Mint / Near Mint / Good / Played / Damaged at app.py:5508-5513 driven by pixel-analysis score from `_rule_based_grade()` at app.py:5456). No code defect, but pixel heuristics can mistake poor photo conditions for poor card conditions. Gather tester signal during closed test before deciding whether to adjust band thresholds. |
| `/api/delete-alert` returns "ok" on no-match | Low priority backend hardening | Endpoint returns `status:"ok"` even when no matching record exists (e.g. wrong/blank email). Means a corrupted localStorage email map could let a backend record linger while the UI shows "no alert." Edge case, low likelihood given current implementation, but worth considering: change endpoint to return `status:"not_found"` when no row deleted, and have frontend surface that as a soft error. |
| YGO export missing name/set | Low priority | Yu-Gi-Oh rows in CSV/XLSX show SKU in the name column and blank in the set column instead of human-readable card name and set. Pokémon and MTG rows show name + set correctly. Likely YGO profiles missing name/set_name fields, or export's lookup falls back to SKU. Investigate after closed test stability. |
| Anthropic API retry observability | Low priority | 30 May log shows occasional `[anthropic._base_client] Retrying request` on deep grade calls. Resolved automatically by the client library, but if frequency increases, consider surfacing a "Deep grade is taking a moment…" UI message to the user instead of a silent spin. |

### Items completed this session (removed from backlog)

| Item | Status |
|---|---|
| Fix window._cardGrade not updating from /api/deep_grade | ✅ DONE 27 May (deployed + verified) |
| Rename "in-house testing Grailsweep Android" list | ✅ DONE 27 May (renamed to "Closed Beta Cohort 1", bundled with in-flight review) |
| Decide tester tier | ✅ DONE (annual, 365-day expiry, advertised as "Annual Pro") |
| In-app price alert cancellation button | ✅ DONE 27 May (deployed + verified on desktop and A56) |
| Mobile affordance for cancel button | ✅ DONE 27 May (✕ glyph added, verified on A56) |
| Auto-grade ordering fix in `/api/v1/match` | ✅ DONE 29 May (verified) |
| Auto-grade collection persistence (cross-surface verification) | ✅ DONE 30–31 May (verified) |
| Deep grade saves from scanner camera path | ✅ DONE 31 May (verified) |

---

## 10. Open questions for next session

- Google approval result for closed testing release (expected within 24h–3 days from 27 May evening)
- Once approved: copy opt-in URL into install-instructions WhatsApp message
- Pre-launch report findings (will auto-generate ~1h after closed test approval, viewable at Test and release → Pre-launch report → Overview; currently still shows "Upload artifacts to generate pre-launch reports" because closed track isn't approved yet)
- Decision on billing compliance approach before production application

---

## 11. Reference URLs

| Page | URL |
|---|---|
| Privacy policy | https://www.grailsweep.com/privacy |
| Terms | https://www.grailsweep.com/terms |
| Delete account | https://www.grailsweep.com/delete-account |
| assetlinks.json | https://www.grailsweep.com/.well-known/assetlinks.json |
| Digital Asset Links validator | https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://www.grailsweep.com&relation=delegate_permission/common.handle_all_urls |
| Internal test opt-in | https://play.google.com/apps/internaltest/4701391892198806409 |
| Closed test opt-in | Will be populated after Google approval |

---

## 12. Immediate next actions (in priority order)

1. **Wait for Google approval email** on closed testing release (still pending, but not blocked by anything)
2. **Recruit remaining 12 testers** — keep populating WhatsApp group as confirmations come in. GRAIL-DD46-F55D is already active with 10 cards — that's one confirmation in the bag.
3. **Today (Sunday 31 May): Resend email migration** — completes the pre-Phase-8 backlog item
4. **Once approved:**
   - Copy opt-in URL into install-instructions message
   - Add all 15 confirmed tester emails to the "Closed Beta Cohort 1" list in Play Console
   - Send WhatsApp install instructions + per-tester DMs with codes
   - Check Pre-launch report → Overview ~1 hour after approval
5. **Monitor opt-in count** in Play Console — when count hits 12, the 14-day clock starts automatically
6. **During closed test window:**
   - Watch for tester feedback on auto-grade accuracy (informs the calibration review backlog item)
   - Start thinking through the billing compliance decision for production submission