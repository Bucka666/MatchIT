# Handover: Resend Email Migration — Part 3 (updated end of session)

## Status: migration functionally complete; test codes revoked; awaiting 48h monitor + cleanup

Previous session (Part 2) ran end-to-end migration from SMTP to Resend across all five live email paths. All deployed, all verified delivering through Resend. Part 3 session revoked the two outstanding test codes from the Modal volume and added a reusable `/admin/delete_code` endpoint. Two non-migration items remain: passive monitoring (Step 8) and decommissioning the old SMTP infrastructure (Step 9).

---

## What's done

### Resend account (unchanged from Part 2)
- Domain `grailsweep.com` verified (eu-west-1, Ireland)
- DKIM, SPF, DMARC all passing per Microsoft header inspection (`spf=pass dkim=pass dmarc=pass compauth=pass reason=100`)
- API key `grailsweep-modal-prod`, Sending access scope
- Modal secret `resend-api-key` wired into both `@app.function()` decorators in `matchit_modal.py`

### Files created
- `email_sender.py` (project root) — thin Resend wrapper exposing `gs_send_email(to, subject, html, text, reply_to, from_address)`. Includes `print()` calls alongside `logger.info()` for Modal log visibility (see backlog #3 for why).

### Code migrated to Resend
All five live transactional paths now use `gs_send_email()`:

| Path | File:function | Audience | Status |
|---|---|---|---|
| Weekly digest | `set_scheduler.py:_send_email` (line ~413) | Internal (you) | Live, multiple deliveries verified |
| Alert confirmation | `app.py:_send_alert_confirmation` (line ~4207) | Customer | Live, multiple deliveries verified |
| Alert firing | `set_scheduler.py:_send_alert_email` (line ~588) | Customer | Code migrated. Runtime trigger blocked by pre-existing bug — see backlog #2 |
| Subscription codes (GRAIL-) | `app.py:_issue_new_code` (line ~5324) | **Customer (revenue)** | Live, verified via admin test endpoint |
| Top-up codes (TOPUP-) | `app.py:_issue_new_topup_code` (line ~5409) | **Customer (revenue)** | Live, verified via admin test endpoint |

### Code deleted (during Part 2)
- Dead `send_email()` function at `matchit_modal.py:92` (zero callers — confirmed by recon)
- Top-of-file imports removed from `matchit_modal.py`: `smtplib`, `ssl`, `from email.mime.multipart import MIMEMultipart`, `from email.mime.text import MIMEText`
- Diff: 28 lines removed, 0 added. File parses, imports, serves traffic.

### Template redesign (Step 5a-v2 pattern, applied to all customer emails)
Original templates used promotional patterns that triggered Outlook spam classification (didn't actually move SCL — see backlog #1 — but cleaner regardless):
- Removed: emoji in subjects/bodies, neon `#00ff88` price colour, "buy signal" / "sell signal" language, dark `#1a1830` body backgrounds, `linear-gradient` headings, gradient buttons
- Added: white body backgrounds, monospace code displays in `#f6f4fb` panels with `#b14dff` left border, `#7c3aed` solid CTA buttons, clean transactional layout, plain text bodies alongside HTML

### Debug infrastructure (still in place)
- **`/admin/test_email_paths` route** (in `app.py`, after `admin_run_scheduler_dry` around line 3206) — POST endpoint that calls `_issue_new_code` and `_issue_new_topup_code` directly with test parameters. Admin-gated. Used to verify Step 6 without firing Stripe webhooks. **Remove during Step 9.**
- **`print()` calls in `email_sender.py`** — added alongside existing `logger.info()` calls because `logger.info` is silently dropped in Modal. Three call sites: success path, HTTPError path, general exception path. **Keep these — they cost nothing and the underlying logger issue isn't fixed yet.**

### Added during Part 3 session
- **`/admin/delete_code` endpoint** in `app.py` (~line 3207, between `admin_run_scheduler_dry` and `admin_test_email_paths`). Admin-gated via `@admin_required`. Accepts `{"code": "GRAIL-..." | "TOPUP-..."}` JSON body. Returns `ok: True` with full `deleted_entry` on success, 404 on not found, 400 on invalid format. Uses `_load_subs` / `_save_subs` so writes go through `_atomic_write_json`. Logs `[ADMIN] Deleted code ...` via `print()`. Deployed but functionally untested end-to-end (see backlog #6).
- **Backup file:** `app.py.bak_admin_delete_code` (pre-endpoint state).
- **Test codes revoked.** Both `GRAIL-XRGG-QEYA` and `TOPUP-UWMF-G28M` confirmed absent from `/modal_data/subscriptions.json` (verified via `modal volume get`). No longer redeemable.

---

## What's left

### Step 8 — Monitor 48 hours (passive)

Watch the Resend dashboard daily. Things to check:
- **Total volume** — any unexpected spikes?
- **Bounce rate** — should be near zero (these are real customer emails, not bulk sends)
- **Complaint rate** — anyone marking as spam reports back to Resend
- **Modal logs** for `[EMAIL ERROR]`, `[RESEND] HTTPError`, `[RESEND] Failed`, `[TOPUP EMAIL ERROR]` — none expected

If clean after 48h, proceed to Step 9.

If problems appear, diagnose before cleanup.

### Step 9 — Decommission Namecheap SMTP + remove test infrastructure

**Pre-flight grep — confirm nothing still uses SMTP env vars:**

```powershell
Select-String -Path *.py -Pattern "SMTP_HOST|SMTP_USER|SMTP_PASS|smtplib|MIMEText|MIMEMultipart" -Context 0,0
```

Expected: zero matches in `matchit_modal.py` (deleted in Step 7). Some matches expected in `app.py` and `set_scheduler.py` only if cleanup of those imports/env reads hasn't happened yet — review and remove.

**Steps to execute, in order:**

1. **Remove the test endpoint.** Delete the `admin_test_email_paths` route from `app.py` (around line 3206). It was added between `admin_run_scheduler_dry` and `reset_db` per Step 6-test instructions. Use `str_replace` to remove the whole function block including decorators.

2. **Decide on `/admin/delete_code`.** Keep (recommended — see backlog #6 to add a UI) or remove. If keeping, leave it alone in this step.

3. **Remove unused SMTP imports from `app.py` and `set_scheduler.py`.** First grep to confirm they're truly unused after migration, then remove:
   - `app.py`: `import smtplib`, `import ssl`, `from email.mime.text import MIMEText`, `from email.mime.multipart import MIMEMultipart` — only if grep confirms zero remaining usage
   - `set_scheduler.py`: same four imports — only if grep confirms zero remaining usage

4. **Remove SMTP env var reads.** Grep for `os.environ["SMTP_*"]` and `os.environ.get("SMTP_*")` patterns. Should be zero matches after migration. If any remain, remove them.

5. **Deploy.** `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`

6. **Smoke test.** Hit `/admin`, `/match`, `/` and confirm 200 OK. Trigger `/admin/run_scheduler` and confirm a digest email arrives in your inbox.

7. **Decommission the Modal `email-credentials` secret.** Either delete it from Modal dashboard (cleanest) or just leave it unused. If deleting:
   - Modal dashboard → Secrets → `email-credentials` → delete
   - The secret is still referenced in `secrets=[...]` lists in `matchit_modal.py` — those references need removing too, before deletion or Modal will error on next deploy.

8. **Remove backup files** (housekeeping):

```powershell
Remove-Item *.bak_step3, *.bak_step4, *.bak_step5a, *.bak_step5a_v2, *.bak_step5b, *.bak_step6, *.bak_step6test, *.bak_step7, *.bak_debug, *.bak_admin_delete_code
```

9. **Leave Namecheap MX DNS records untouched** — those handle **inbound** mail to support@grailsweep.com. Only outbound SMTP is being decommissioned.

10. **Optionally remove `print()` debug calls in `email_sender.py`.** These are harmless and have proven useful, so I'd leave them. If you want to remove them, restore from `email_sender.py.bak_debug` (which was the pre-debug state).

---

## Discovered during Part 2 — backlog (separate projects)

### 1. Outlook deliverability — new-domain reputation
**Symptom:** All test emails to Outlook addresses (3 separate addresses tested) land in Junk with `SCL: 5`. Identical SCL between aggressive original template and clean redesigned template — confirms content was never the issue.

**Cause:** Brand-new sending domain. Microsoft has no engagement history with `grailsweep.com` → defaults to suspicion → SCL 5 → Junk.

**Authentication is perfect:** SPF, DKIM (both `grailsweep.com` and `amazonses.com`), DMARC, compauth — all `pass`. Headers verified across two test recipients.

**Resolution:** Time. 2-4 weeks of legitimate sending without bounces or complaints. Self-resolves naturally. No code action.

**Resend's separate recommendation:** Switch From address to a sending subdomain (e.g. `mail@send.grailsweep.com` instead of `support@grailsweep.com`). Real best practice, separate small project — DNS change, Resend config change, single `DEFAULT_FROM` constant change in `email_sender.py`. Don't bundle with this migration.

**Also worth doing later:** Once Outlook delivery improves (probably 3-4 weeks), upgrade DMARC from `p=none` to `p=quarantine` for stronger protection.

### 2. `check_price_alerts()` runtime bug
**Symptom:** Migrated `_send_alert_email` (Step 5b) was tested by setting `target_high: £0.01` alerts via the UI on Chandelure, Dromokas Command, Tinkaton, Miltank — multiple cards. Triggered `/admin/run_scheduler` which calls `check_price_alerts()` at `set_scheduler.py:910`. Function ran (no errors logged), but `_send_alert_email` was never invoked. Zero `[RESEND] Sent ... subject='Price alert: ...'` log lines.

**Likely cause:** SKU-to-profile.json price lookup inside `check_price_alerts()` returning None or 0.0 for all tested SKUs, so the threshold comparison never trips. The function reads from `CardsDB/{game}/{sku}/profile.json` per recon.

**Pre-existed this migration** — `check_price_alerts()` body wasn't touched. Migration only swapped the email-send block inside `_send_alert_email`.

**To diagnose next time:** Add `print()` instrumentation inside `check_price_alerts()` and the price-compute helper to see what the function actually returns for a test SKU. ~30 min focused task.

### 3. Modal logger silently dropped
**Symptom:** `logger.info()`, `logger.warning()`, `logger.error()` calls in `set_scheduler.py`, `email_sender.py`, and elsewhere produce zero lines in `modal app logs`. Only `print()` reaches stdout.

**Workaround applied:** Added `print()` alongside `logger.*` in `email_sender.py`. This is why we can see `[RESEND] Sent to ...` lines.

**Real fix (~15 min, backlog):** Configure root logger at app startup. Likely add this near the top of `app.py` and `matchit_modal.py`:

```python
import logging, sys
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    stream=sys.stdout,
    force=True,
)
```

Then audit existing modules that use `logger = logging.getLogger(__name__)` and confirm they work after.

### 4. Stale `MatchITv2_ProductMatch_Data` path reference
**Symptom:** During Step 7 deploy, a single cold-start container hit `PermissionError: [Errno 1] Operation not permitted: '/modal_data/MatchITv2_ProductMatch_Data'` on first `/admin` request. Subsequent requests served fine. Pre-existing intermittent issue.

**Location:** `app.py:81-86` — `get_data_dir()` and `get_images_db_path()`. Still references the pre-rebrand directory name.

**Fix:** Update path constants to match current GrailSweep naming, or just confirm the directory exists once on the volume (whichever is cleaner).

### 5. Lingering background thread on container exit
**Symptom:** `WARNING:modal-client: Detected 1 background thread(s) [WSGI_1] still running after container exit. This will prevent runner shutdown for up to 30 seconds.` Cosmetic, costs a few seconds of container time per scale-down.

**Likely cause:** Flask thread (probably inside `flask_cors` or a `threading.Thread` started somewhere in startup code). Not Resend-related.

**Fix:** Identify the thread, give it a clean shutdown hook, or set `daemon=True` so it doesn't block exit.

---

## Discovered during Part 3 — backlog (separate projects)

### 6. Admin UI button for `/admin/delete_code` endpoint

**Status:** Endpoint exists and is live at `POST /admin/delete_code` (added to `app.py` ~line 3207, between `admin_run_scheduler_dry` and `admin_test_email_paths`). Admin-gated via `@admin_required`. Accepts `{"code": "GRAIL-..."}` or `{"code": "TOPUP-..."}` JSON body. Returns `ok: True` with full `deleted_entry` on success, 404 on not found, 400 on invalid format. Uses `_load_subs` / `_save_subs` so writes go through `_atomic_write_json`. Logs `[ADMIN] Deleted code ...` via `print()`.

**Verified:** Endpoint deployed. End-to-end verification via PowerShell hit a session-cookie issue (302 redirects to `/login` on follow-up requests after a successful login) — not investigated, because the underlying task (revoke two test codes) was completed via direct `modal volume` edit instead. Endpoint is functionally untested in production but logically sound from code review.

**What's needed:** Small UI on the `/admin` HTML page — text input for the code, "Delete" button, confirmation modal (destructive op), result display showing the deleted entry as a receipt. ~15-20 min Claude Code task.

**Why this matters:** Chargebacks, fraud, mis-issued codes during testing. Right now revoking a code requires either fighting PowerShell session cookies or doing a `modal volume get` → edit JSON → `modal volume put` dance with manual JSON validation. A button removes both routes.

**Bonus consideration:** Could extend the same UI to support backlog #7 — i.e. a "Cancel" action that sets `status: "cancelled"` rather than deleting, preserving audit trail.

### 7. `_ssr_subscription` doesn't check status field

**Symptom:** `_ssr_subscription` helper at `app.py:5175` checks only whether a code exists in `subscriptions.json` and reads its `tier`. It does **not** check the `status` field. Result: a `"cancelled"` code still grants `is_premium: True` on SSR-rendered pages until the client-side `validate_premium` call fires and corrects it.

**Real-world impact:** When a real cancellation happens (chargeback, refund, Stripe `customer.subscription.deleted` webhook), the user keeps seeing premium SSR-rendered content on page loads for a brief window. The client-side check eventually catches it, but server-rendered HTML is wrong in the meantime.

**Pre-existed Resend migration** — found during recon for the test code revocation task in Part 3.

**Fix:** Add status check to `_ssr_subscription`. If `status in ("cancelled", "revoked")`, return non-premium. Mirror whatever set of "good" statuses `validate_premium` accepts (currently it only rejects `"cancelled"`, so this needs aligning across both functions for consistency).

**Estimated effort:** ~10 min focused task. Recommended pairing with backlog #6 since they touch the same data model.

---

## Session lessons (carry forward)

- **Don't reach for HTTP scripting when the data lives on a Modal volume.** `modal volume get` → edit → `modal volume put` is faster and uses zero usage budget compared to building auth-gated endpoints. Reserve endpoint-building for things that need to happen repeatedly or from a UI.
- **PowerShell session-cookie auth with Flask through Modal is unreliable** — root cause unknown, possibly Modal's container snapshot/restore interacting with Flask's `SECRET_KEY` between containers. Worth investigating only if multiple future tasks need it; otherwise bypass via volume edits or build proper UI.

---

## Working conventions (carry forward)

- One change at a time, deploy, verify, then next
- Backup before editing existing files (`Copy-Item file.py file.py.bak_stepN`)
- Recon-only first pass before risky integrations
- "STOP. DO NOT DEPLOY." footer on every Claude Code instruction
- Concise messages, no padding
- Modal deploy command: `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`
- **For one-off data fixes on the Modal volume, use `modal volume get` / `modal volume put` rather than building endpoints.**

---

## Key identifiers

- Modal workspace: `c-a-buckley`
- Modal app: `matchit-api`
- Modal volume: `matchit-data-v2` at `/modal_data`
- Resend domain: `grailsweep.com` (verified, eu-west-1)
- Resend API key name: `grailsweep-modal-prod`, scope: Sending access
- Modal secret name: `resend-api-key`, env var: `RESEND_API_KEY`
- Modal secret to remove in Step 9: `email-credentials`
- From address: `GrailSweep <support@grailsweep.com>` (visible) — Resend handles bounce path via `send.grailsweep.com`
- Resend dashboard: https://resend.com/emails
- Admin auth: `@admin_required` decorator at `app.py:600`. Reads `session["is_admin"]`. Set via `POST /login` with form field `password` matching `ADMIN_PASSWORD` env var (or `admin_password` in config.json).
- Subscriptions data: `/modal_data/subscriptions.json` on Modal volume. Loaders: `_load_subs()` line 5084, `_save_subs()` line 5104 → `_atomic_write_json()` line 5033.

---

## Test data status

All previously outstanding test codes have been revoked. No test data currently outstanding on the volume.