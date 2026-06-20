# GrailSweep Session Handover — 31 May 2026

**Session status:** All planned tasks complete. Deploy verified (sw.js v13).
**Phase 8 status:** Closed testing sent for review — awaiting Google approval. All backlog items from today closed.

---

## 1. Tasks completed this session

### 1. Resend migration confirmed complete + CLAUDE.md cleanup
- Recon confirmed zero SMTP/smtplib code anywhere in project files
- `/admin/test_email_paths` dead route removed from app.py (was marked TEMPORARY — Resend verification only)
- CLAUDE.md email section rewritten to reflect current state (Resend, `email_sender.py`, `gs_send_email()`, `resend-api-key` Modal secret)
- CLAUDE.md Known Issues cleaned up: removed stale Gmail price alert line, removed stale sw.js cache name line (was matchit-v2, now grailsweep-v11+), reworded scanner.html bullet
- Backup: `app_pre_email_cleanup.py`

### 2. Billing compliance — Option A locked
- Decision: Keep Stripe + add Google Play Billing alongside (User Choice Billing)
- Users will see both options at checkout on Android; Google takes 15% on subscriptions (10% service + 5% billing), 25% on one-time purchases (20% + 5%) from June 30 2026 in UK
- Implementation effort: 3–5 days, requires Play Billing Library integration in TWA — deferred until after closed test
- Staging strategy recommended: UK production first, expand regions after billing integration complete

### 3. Android pricing locked
All prices match web except top-up, where Google's 25% cut made the margin unacceptable at £1.79:

| Product | Web price | Android price | Notes |
|---|---|---|---|
| Monthly | £3.99 | £3.99 | Accept margin hit |
| Annual | £34.99 | £34.99 | Accept margin hit |
| Founder | £19.99 | £19.99 | Accept margin hit |
| Referral monthly | £2.99 | £2.99 | Accept margin hit |
| Top-up 75 scans | £1.79 | **£1.99** | Adjusted — nets ~£1.49 vs £1.56 Stripe |

### 4. Collection export — Number column added (sw.js v11 → v12)
Root cause: `card_number` was never stored in any of the three Add-to-Collection save paths, so it was absent from the collection XLSX entirely. The YGO export bug as logged was a misdiagnosis — no YGO-specific code branch exists; the missing field affected all TCGs equally.

Five changes across three files:
| File | Change |
|---|---|
| results.html:1177 | Added `card_number: r1.profile.card_number \|\| ''` to `resCardData` |
| results.html:1288 | Added `card_number: _rc.card_number \|\| ''` to `colAddCard` item |
| match.html:1855 | Added `card_number: c.num \|\| ''` to `scQuickAdd` newItem |
| match.html:2024 | Added `card_number: c.num \|\| ''` to `gsDetailAddToCollection` newItem |
| collection.html:858/860 | Added 'Number' header and `i.card_number \|\| ''` row value |

Existing collection items without the field export blank gracefully (not `undefined`).
Verified: Number column present and populated for all TCGs on new scans.

### 5. `/api/delete-alert` no-match hardening (sw.js v12 → v13)
- Backend (app.py:4459): now compares list length before/after filter — returns `{"status": "not_found"}` when no matching email+sku record existed instead of silent `"ok"`
- Frontend (collection.html:1373): added `else if (d.status === 'not_found')` branch — still cleans up both localStorage keys (`col_alerts_v1` and `col_alert_emails_v1`) and calls `colRender()` so UI heals itself even when backend had no record
- Normal cancel verified working on A56
- Backups: `app_pre_delete_alert_hardening.py`, `collection_pre_delete_alert_hardening.html`

---

## 2. sw.js cache history this phase

| Version | Change |
|---|---|
| v8 | Mobile affordance (Phase 8 earlier) |
| v9–v11 | Unified grading fix |
| v12 | Number column export fix |
| v13 | delete-alert hardening |

---

## 3. Backlog (parked — nothing actionable now)

| Item | Effort | Notes |
|---|---|---|
| Billing compliance implementation (Option A) | 3–5 days | Play Billing Library + User Choice Billing enrolment. Deferred until after closed test. UK production first, then expand. |
| Auto-grade calibration | Post-closed-test | No code defect. Pixel heuristics can mistake bad photo lighting for poor card condition. Ask testers directly: *"Scan a card you know the condition of — does the grade feel about right, too high, or too low?"* Err strict rather than generous if adjustment needed — aligns with PSA/BGS conservatism. |
| Update Data safety: Photos ephemeral declaration | 5 min Play Console edit | Once 30-day query image sweep has stable runtime history (several weeks) |
| Add app tags | 2 min Play Console edit | Conditional on Google adding collecting/hobbies tags to taxonomy |
| Anthropic API retry observability | Low priority | Occasional retrying request in logs, self-resolving. Only act if frequency increases — consider "Deep grade is taking a moment…" UI message |
| YGO export name/set (original backlog item) | Closed/reframed | Recon showed no YGO-specific bug. If a YGO card shows SKU in name column it means that specific card's profile returned empty from match — a data issue for that card, not a code bug. Monitor during closed test. |

---

## 4. Closed testing status (unchanged from Phase 8 handover)

- Sent for review 27 May (evening) — awaiting Google approval (expected 24h–3 days from then)
- Once approved: copy opt-in URL into WhatsApp install instructions, add tester emails to Play Console, send per-tester DMs
- 14-day clock starts when 12+ testers have opted in via the link
- 3 confirmed testers, 12 more needed (15 total target for dropout buffer)
- GRAIL-DD46-F55D already active with 10 cards — one confirmation in the bag

---

## 5. Immediate next actions (in priority order)

1. **Wait for Google approval email** — still pending
2. **Recruit remaining 12 testers** — keep populating WhatsApp group
3. **Once approved:**
   - Copy opt-in URL into install instructions message
   - Add all 15 confirmed tester emails to Closed Beta Cohort 1 in Play Console
   - Send WhatsApp install instructions + per-tester DMs with codes
   - Check Pre-launch report ~1 hour after approval
4. **During closed test:** Ask testers the grade accuracy question (see backlog above)
5. **Post-closed-test:** Start billing compliance implementation before production submission

---

## 6. Persistent technical reference (unchanged)

- **Project root:** C:\MatchIT | **Deploy:** `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`
- **Modal:** workspace c-a-buckley, app matchit-api, volume matchit-data-v2 at /modal_data
- **Deploy quirk:** If behaviour unchanged after successful deploy, `modal app stop matchit-api` then redeploy fresh
- **All code changes** go through Claude Code (chat prepares instructions; Claude Code executes locally)
- **Key files:** matchit_modal.py, app.py, api_routes.py, ocr_confirm.py, set_scheduler.py, log_config.py, email_sender.py
- **Stripe live price IDs:** monthly price_1TSXwYFl8aS11HhgulxrZUhR, annual price_1TSXyrFl8aS11Hhg2qrU45sS, founder_yearly price_1TSXf7Fl8aS11HhgE3lOk3Ma, topup_75 price_1TSfPyFl8aS11HhgBgUuomas
- **Android top-up price:** £1.99 (web remains £1.79)
- **Scan storage:** subscriptions.json on Modal volume; scan counters in Modal Dict "scan-counters"; top-up credits in Modal Dict
- **Email:** Resend only, via `gs_send_email()` in `email_sender.py`, Modal secret `resend-api-key`, from/reply-to support@grailsweep.com
- **sw.js current cache:** grailsweep-v13