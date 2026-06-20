# Handover: Post-Resend Migration — Part 4

## Status: four backlog items closed in Part 3 session (19 May 2026). Remaining work below, renumbered for fresh start.

Part 3 session closed: `_ssr_subscription` status-check bug, admin UI for code cancellation (soft-cancel via new `/admin/cancel_code` endpoint), `MatchITv2_ProductMatch_Data` cold-start `PermissionError` race (try/except around naked `os.makedirs` calls), and the silent-Modal-logger problem (new `log_config.py` module installs `basicConfig(stream=sys.stdout, force=True)` at three entry points). All deployed, verified in Modal logs, no regressions. Modal logger fix is the biggest unlock — every future `logger.*` call now reaches Modal logs, scheduler runs are visible for the first time in weeks, and CLIP startup chatter is observable.

Resend migration (from Part 3) remains in passive monitoring phase. No issues observed in the 48-hour window so far. Outlook deliverability is still SCL 5 (new-domain reputation, self-resolves over weeks).

---

## Remaining tasks — ordered easiest/quickest first

### 1. Monitor Resend 48h (passive — Step 8 of original migration)

Watch Resend dashboard daily. Look for:
- Total volume spikes
- Bounce rate (should be near zero)
- Complaint rate
- Modal logs for `[EMAIL ERROR]`, `[RESEND] HTTPError`, `[RESEND] Failed`, `[TOPUP EMAIL ERROR]`

If clean after 48h from Part 3 completion (around 21 May 2026), proceed to task 2.

### 2. Wait out Outlook reputation (passive, weeks)

No code action. SPF, DKIM, DMARC all passing — domain is brand new and Microsoft has no engagement history. Self-resolves in 2–4 weeks of legitimate sending. Once Outlook delivery improves:
- Upgrade DMARC from `p=none` to `p=quarantine`
- Consider switching From address to `mail@send.grailsweep.com` (Resend's recommendation, separate mini-project — DNS change, Resend config change, single `DEFAULT_FROM` constant change in `email_sender.py`)

### 3. Legacy-code status field is ignored (~5 min)

`_ssr_subscription`'s fast path returns `is_premium: True` for any code in `CFG.get("premium_codes", [])` without checking that entry's `status` in `subscriptions.json`. Found while testing the Part 3 status-check fix — `GRAIL-XAJI-0Y6D` is both `tier=legacy` (in config) and has a status field in subs. The new `/admin/cancel_code` endpoint also explicitly rejects legacy codes for the same reason.

Today this is theoretical (you'd never normally cancel a legacy code), but if it ever happens the legacy code stays premium forever. Fix: move the legacy fast-path *after* the status check, or have legacy codes also honour their `subscriptions.json` status if present.

Worth pairing with task 4 — same alignment exercise across the codebase.

### 4. Align `validate_premium` to require `status == "active"` (~20 min including audit)

Currently `validate_premium` accepts anything that isn't `"cancelled"`, including `"unredeemed"`. `resolve_tier_from_code` in `db.py` already requires `"active"` for the scan-gating path. Aligning gives one canonical rule across the codebase.

**Blocker:** audit what `"unredeemed"` means in the issuance flow first — there's 1 live entry in `subscriptions.json` (`TOPUP-UWMF-G28M`, but actually the unredeemed is a different one) that would change behaviour. Quick recon: grep for where `"unredeemed"` is written, and confirm the issuance flow's intent (is this status meant to grant premium access pre-redemption, or block it until redeemed?).

### 5. Diagnose `check_price_alerts()` (~30 min — now easier with logger fix)

Pre-existed Part 2 migration. Symptom: when test alerts are set (`target_high: £0.01` on multiple cards) and `/admin/run_scheduler` triggers `check_price_alerts()` at `set_scheduler.py:910`, no errors are logged but `_send_alert_email` is never invoked.

**Likely cause:** SKU-to-profile.json price lookup inside `check_price_alerts()` returning None or 0.0 for tested SKUs, so the threshold comparison never trips. The function reads from `CardsDB/{game}/{sku}/profile.json`.

**Now easier to diagnose:** the Modal logger fix from Part 3 means existing `logger.info` calls inside `set_scheduler.py` are now visible. Run a dry scheduler trigger with test alerts active and watch for what `check_price_alerts()` is actually doing. If existing logger calls don't expose enough, add `logger.info("[ALERT-DEBUG] sku=%s price=%s threshold=%s match=%s", ...)` calls and re-run.

### 6. Decommission SMTP infrastructure (~30–45 min, multi-step)

Blocked by task 1 — only safe to run once Resend has logged a clean 48 hours of production traffic.

**Pre-flight grep** — confirm nothing still uses SMTP env vars:
```powershell
Select-String -Path *.py -Pattern "SMTP_HOST|SMTP_USER|SMTP_PASS|smtplib|MIMEText|MIMEMultipart" -Context 0,0
```

**Steps, in order:**
1. Remove the `admin_test_email_paths` route from `app.py` (~line 3206, between `admin_run_scheduler_dry` and `reset_db`).
2. Keep `/admin/delete_code` and `/admin/cancel_code`. Both are useful — `cancel_code` got a UI in Part 3, `delete_code` is still curl-only.
3. Remove unused SMTP imports from `app.py` and `set_scheduler.py`:
   - `import smtplib`, `import ssl`, `from email.mime.text import MIMEText`, `from email.mime.multipart import MIMEMultipart` — only if grep confirms zero remaining usage
4. Remove SMTP env var reads. Grep for `os.environ["SMTP_*"]` and `os.environ.get("SMTP_*")`.
5. Deploy: `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`
6. Smoke test: `/admin`, `/match`, `/` should all 200. Trigger `/admin/run_scheduler` and confirm a digest email arrives.
7. Decommission the Modal `email-credentials` secret:
   - First remove references in `secrets=[...]` lists in `matchit_modal.py` (otherwise Modal errors on deploy)
   - Then delete from Modal dashboard → Secrets → `email-credentials` → delete
8. **Leave Namecheap MX DNS records untouched** — those handle inbound mail to `support@grailsweep.com`. Only outbound SMTP is being decommissioned.
9. Remove backup files (housekeeping). Current backups on disk after Part 3:
   ```powershell
   Remove-Item *.bak_ssr_status, *.bak_admin_cancel, *.bak_makedirs_race, *.bak_logger_fix, *.bak_admin_delete_code
   ```
   (Plus all earlier `*.bak_stepN` backups if not already cleared.)

### 7. Cancelled subscribers still see their collection on SSR (~10 min — product decision needed first)

Observed during Part 3 Task A verification: `[SSR-COLLECTION] code=... cards=2` rendered for a cancelled user. The premium gate is closed (no save/export), but their saved card list still loads on page render via the `_ssr_col` prefetch in `collection()` (~app.py:4226–4228).

**Product question:** gate the collection prefetch behind premium status on SSR, or accept that read-only view-after-cancellation is fine?

Technical fix is straightforward (add `if not _is_prem: skip _ssr_col`), but the answer is commercial. Decide before implementing.

### 8. Clean up `print()` workarounds now that logger works (~20–30 min)

After the Part 3 logger fix, every `print(..., flush=True)` paired with an identical `logger.*` call is redundant. Most visible example: `email_sender.py:67-79` has three paired calls (success, HTTPError, general exception).

**Two-stage approach recommended:**

Stage A — remove obvious duplicates only:
- `email_sender.py:67-79` — three paired pairs. Remove the `print()` lines; keep the `logger.*` calls.

Stage B — optional consistency pass (style preference, not a bug):
- `app.py` has many `print()` calls (`[PATH]`, `[ADMIN]`, `[SSR-SUBS]`, `[SSR-PREMIUM]`, `[CACHE]`, `[STARTUP]`, `[DINO-TIEBREAK]`, etc) that could become `logger.info()` for consistency. No functional difference, but unifies the log style.
- `matchit_modal.py` has `[WARMUP]` prints, same story.

Stage A first, verify no regression in 24h, then Stage B if desired.

---

## Working conventions (carry forward)

- One change at a time, deploy, verify, then next
- Backup before editing existing files (`Copy-Item file.py file.py.bak_stepN`)
- Recon-only first pass before risky integrations
- "STOP. DO NOT DEPLOY." footer on every Claude Code instruction
- Concise messages, no padding
- Modal deploy command: `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`
- For one-off data fixes on the Modal volume, use `modal volume get` / `modal volume put` rather than building endpoints
- `modal volume put` refuses to overwrite by default — use `--force` flag for revert/replace flows

---

## Key identifiers

- Modal workspace: `c-a-buckley`
- Modal app: `matchit-api`
- Modal volume: `matchit-data-v2` at `/modal_data`
- Subscriptions data: `/modal_data/subscriptions.json` on Modal volume. Loaders: `_load_subs()` line 5084, `_save_subs()` line 5104 → `_atomic_write_json()` line 5033.
- Premium tiers: `_PREMIUM_TIERS = {"monthly", "annual", "lifetime", "legacy", "founder_yearly"}` at app.py:4189
- Status writers: `_extend_subscription()` (active) at app.py:5596, `_cancel_subscription()` (cancelled) at app.py:5626, new admin endpoint at app.py:3222
- Admin auth: `@admin_required` decorator at `app.py:600`. Reads `session["is_admin"]`. Set via `POST /login`.
- Resend domain: `grailsweep.com` (verified, eu-west-1)
- Resend API key name: `grailsweep-modal-prod`, scope: Sending access
- Modal secret name: `resend-api-key`, env var: `RESEND_API_KEY`
- Modal secret to remove in task 6: `email-credentials`
- From address: `GrailSweep <support@grailsweep.com>` (visible) — Resend handles bounce path via `send.grailsweep.com`
- Resend dashboard: https://resend.com/emails
- Logger config module: `log_config.py` at project root. `configure_logging()` calls `basicConfig(level=INFO, stream=sys.stdout, force=True)`. Called from `matchit_modal.py` (top), `app.py` (just before `app = Flask(__name__)`), and `set_scheduler.py` (top, outside `__main__` guard).

---

## Test data status

All test codes from Part 2 and Part 3 testing have been revoked or reverted. `subscriptions.json` is in clean production state.

---

## Open question for tomorrow's session

If task 5 (`check_price_alerts()` diagnosis) reveals the price lookup is broken and not just a test-data issue, this may surface as a real customer-facing bug — alerts being silently non-functional in production. Worth scoping size of impact before deciding whether to escalate priority above the easier tasks 3 and 4.