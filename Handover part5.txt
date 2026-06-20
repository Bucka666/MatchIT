# Handover: Post-Resend Migration — Part 5

## Status: six items closed in Part 4 session (21–22 May 2026). Three items remain. Renumbered for fresh start.

Part 4 session closed: Resend 48h monitoring confirmation, full SMTP infrastructure decommission (route + dead imports + Modal secret + backups), Task 8 Stage A (redundant `print()` cleanup in `email_sender.py`), legacy-code status check alignment in `_ssr_subscription` AND `validate_premium`, and `validate_premium` updated to require explicit `status == "active"`. Six deploys, every one verified clean with smoke tests + scheduler digest confirmation.

Also part-closed: Task 5 (`check_price_alerts()` diagnosis). Two of three bugs fixed and deployed (Deploys A + B). Third bug fully diagnosed with canonical fix algorithm identified, but not yet implemented — deferred to next session as it's the most complex of the three. Details below.

---

## Remaining tasks — ordered easiest/quickest first

### 1. Wait out Outlook reputation (passive, weeks)

No code action. SPF, DKIM, DMARC all passing — domain is brand new and Microsoft has no engagement history. Self-resolves in 2–4 weeks of legitimate sending. Once Outlook delivery improves:
- Upgrade DMARC from `p=none` to `p=quarantine`
- Consider switching From address to `mail@send.grailsweep.com` (Resend's recommendation, separate mini-project — DNS change, Resend config change, single `DEFAULT_FROM` constant change in `email_sender.py`)

### 2. Cancelled subscribers still see their collection on SSR (~10 min — product decision needed first)

Observed during Part 3 Task A verification: `[SSR-COLLECTION] code=... cards=2` rendered for a cancelled user. The premium gate is closed (no save/export), but their saved card list still loads on page render via the `_ssr_col` prefetch in `collection()` (~app.py:4226–4228).

**Product question:** gate the collection prefetch behind premium status on SSR, or accept that read-only view-after-cancellation is fine?

Technical fix is straightforward (add `if not _is_prem: skip _ssr_col`), but the answer is commercial. Decide before implementing.

### 3. Fix `check_price_alerts()` price-resolution algorithm — Deploy C (~30–45 min)

**This is Task 5 Deploy C — the remaining bug after Part 4 Deploys A + B.**

The function now successfully loads alerts from the volume (Deploy A) and resolves SKU directories with correct lowercase game prefixes (Deploy B). But it still fails silently because the price-resolution block at `set_scheduler.py:697-720` assumes a flat `prices["gbp"]` / `prices["usd"]` structure that doesn't exist on disk.

**Actual profile.json structure (confirmed via `modal volume get`):**

```json
"prices": {
  "tcgplayer": {
    "reverseHolofoil": {"low": 0.09, "mid": 0.25, "high": 4.0, "market": 0.22},
    "holofoil":        {"low": 0.01, "mid": 0.19, "high": 123.45, "market": 0.17}
  },
  "cardmarket": {
    "avg_sell": 0.06, "low": 0.02, "trend": 0.05, "avg_1d": 0.03, "avg_7d": 0.05, "avg_30d": 0.05
  }
}
```

Note: pricing is in USD (tcgplayer) and EUR (cardmarket), NOT GBP. Currency is implicit in the marketplace.

**Two distinct structures to handle:**
- `tcgplayer.{foiltype}.{pricepoint}` — nested dict
- `cardmarket.{pricepoint}` — flat number at source level

**Canonical algorithm to mirror — lives in `templates/results.html:1149-1168` as JavaScript:**

```javascript
var best = 0;
var prices = window._rawPrices || {};
Object.keys(prices).forEach(function(src) {
  if (src.toLowerCase().indexOf('ebay') >= 0) return;
  if (src.toLowerCase().indexOf('amazon') >= 0) return;
  var sdata = prices[src];
  if (!sdata || typeof sdata !== 'object') return;
  Object.keys(sdata).forEach(function(variant) {
    var vdata = sdata[variant];
    var pr = 0;
    if (vdata && typeof vdata === 'object')
      pr = vdata.market || vdata.trend || vdata.avg_sell || vdata.mid || 0;
    else if (typeof vdata === 'number') pr = vdata;
    if (pr > 0) {
      var mult = src.toLowerCase().indexOf('cardmarket') >= 0 ? EUR_RATE : USD_RATE;
      var gbp = pr * mult;
      if (gbp > best) best = gbp;
    }
  });
});
return Math.round(best * 100) / 100;
```

**Python translation needed in `check_price_alerts()`:**

Replace the existing block at `set_scheduler.py:697-720` (everything from `# Compute best GBP price...` through the second `if gbp is None: continue`). Replacement should:

1. Define FX rates as constants at top of function (or module): `USD_RATE = 0.79`, `EUR_RATE = 0.86` — matches template's fallback defaults
2. Iterate `prices.items()`, skip sources containing `'ebay'` or `'amazon'` (case-insensitive)
3. Per source dict, iterate its values:
   - If value is dict: `pr = vdata.get("market") or vdata.get("trend") or vdata.get("avg_sell") or vdata.get("mid") or 0`
   - If value is number (int/float): `pr = vdata`
   - Otherwise skip
4. If `pr > 0`: apply EUR rate to `cardmarket` source, USD rate to all others
5. Track maximum across all sources/variants — best-of-all-prices, not first-match
6. Add `logger.info(f"[ALERT-PRICE] sku={sku} gbp={gbp}")` BEFORE the threshold comparison so future debugging is trivial

**FX rate decision:**
Use hardcoded `USD_RATE = 0.79`, `EUR_RATE = 0.86` constants. Don't fetch live rates — scheduler runs shouldn't depend on `frankfurter.app` API availability, and FX drift is well below the noise floor of typical alert thresholds.

**Test data ready and verified on the Modal volume:**

```json
{
  "email": "grailtest2026@outlook.com",
  "code": "GRAIL-XAJI-0Y6D",
  "sku": "sv3-85",
  "card_name": "Togekiss",
  "target_low": 0.2,
  "target_high": 0.15,
  "current_gbp": 0.16,
  "created": "2026-05-22T16:54:16.105278",
  "triggered": false
}
```

Both thresholds are crossed by current price £0.16 (above target_high £0.15, below target_low £0.20). Triggering `/admin/run_scheduler` after deploy should fire a `"low"` alert (`elif` order means low wins).

Hand-walking the algorithm for Togekiss confirms `tcgplayer.reverseHolofoil.market = 0.22 USD * 0.79 ≈ 0.17 GBP` is the maximum. Result `0.17` > target_high `0.15` → fires high. Result `0.17` > target_low `0.20`? No, 0.17 < 0.20 → fires low. Either way, fires.

**Verification command after deploy:**

```powershell
modal app logs matchit-api --since 5m 2>&1 | Select-String -Pattern "\[ALERT-PRICE\]|\[ALERTS\] Triggering|\[ALERTS\] Alert email sent"
```

Expected sequence:
1. `[ALERT-PRICE] sku=sv3-85 gbp=0.17` (or thereabouts)
2. `[ALERTS] Triggering low alert for sv3-85 @ £0.17`
3. `[ALERTS] Alert email sent to grailtest2026@outlook.com for sv3-85`

If all three appear, Task 5 closes. Check inbox at `grailtest2026@outlook.com` for the alert email — may go to junk (Outlook reputation, see Task 1).

**Things to check during recon before edit:**
- Has anyone changed the price structure on disk since this handover? `modal volume get matchit-data-v2 /CardsDB/pokemon/sv3-85/profile.json` and confirm.
- Do MTG and Yu-Gi-Oh profiles have the same nested structure? `modal volume get matchit-data-v2 /CardsDB/mtg/<some-sku>/profile.json` — worth a quick eyeball before assuming the algorithm works for all three games.
- Is the alert still there with `triggered: false`? `modal volume get matchit-data-v2 /price_alerts.json ./check.json --force`

**Worth keeping after Task 5 closes:** the `[ALERT-PRICE]` log line stays — Stage B (Task 8 from old handover) optional cleanup would otherwise potentially remove it as a `print()`. It's already a `logger.info`, so it's fine, but document the intent.

---

## What was closed in Part 4

For completeness, items removed from the backlog:

- **Task 1 (Resend 48h monitoring):** confirmed clean
- **Task 6 (SMTP decommission, multi-step):** all five sub-steps done — route removed, dead imports + CFG entries removed, `email-credentials` references removed from matchit_modal.py, Modal secret deleted, all backup files cleared. DNS MX records untouched per plan.
- **Task 3 (legacy-code status check):** fixed in BOTH `_ssr_subscription` AND `validate_premium` — turned out to be the same bug class in two places.
- **Task 4 (align `validate_premium` to `== "active"`):** done. Confirmed `"unredeemed"` is only ever written by `_issue_new_topup_code` (app.py:5617) for topup codes — never subscription codes — so the alignment is safe.
- **Task 5 Deploy A (alerts file persistence):** `price_alerts.json` now lives at `/modal_data/price_alerts.json` on the volume. Verified persistent across container scaledown.
- **Task 5 Deploy B (game prefix case):** lowercase `mtg`/`yugioh`/`pokemon` strings now used in `check_price_alerts()` to match volume layout.
- **Task 8 Stage A (print() cleanup):** three redundant `print()` calls removed from `email_sender.py`. Verified `logger.*` calls reach Modal logs cleanly.

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
- When verifying logger output reaches Modal logs, use the scheduler digest as a known-good signal (`[RESEND] Sent` from `email_sender.py`)
- For session-pacing: when user says "do task X", do task X then stop. Don't auto-extend into "and while we're at it..." without checking in.

---

## Key identifiers

- Modal workspace: `c-a-buckley`
- Modal app: `matchit-api`
- Modal volume: `matchit-data-v2` at `/modal_data`
- Subscriptions data: `/modal_data/subscriptions.json` on Modal volume. Loaders: `_load_subs()` line 5084, `_save_subs()` line 5104 → `_atomic_write_json()` line 5033.
- Premium tiers: `_PREMIUM_TIERS = {"monthly", "annual", "lifetime", "legacy", "founder_yearly"}` at app.py:4189
- Status writers: `_extend_subscription()` (active) at app.py:5596, `_cancel_subscription()` (cancelled) at app.py:5626, admin endpoint at app.py:3222
- Admin auth: `@admin_required` decorator at `app.py:600`. Reads `session["is_admin"]`. Set via `POST /login`.
- Resend domain: `grailsweep.com` (verified, eu-west-1)
- Resend API key name: `grailsweep-modal-prod`, scope: Sending access
- Modal secret name: `resend-api-key`, env var: `RESEND_API_KEY`
- From address: `GrailSweep <support@grailsweep.com>` (visible) — Resend handles bounce path via `send.grailsweep.com`
- Resend dashboard: https://resend.com/emails
- Logger config module: `log_config.py` at project root. `configure_logging()` calls `basicConfig(level=INFO, stream=sys.stdout, force=True)`. Called from `matchit_modal.py` (top), `app.py` (just before `app = Flask(__name__)`), and `set_scheduler.py` (top, outside `__main__` guard).
- Price alerts file: `/modal_data/price_alerts.json` on Modal volume (new in Part 4 Deploy A)
- CardsDB layout: `/modal_data/CardsDB/{mtg|pokemon|yugioh}/{sku}/profile.json` — lowercase game directories
- FX reference: `templates/results.html:1141-1144` fetches live rates from `frankfurter.app`. Fallback defaults `USD_RATE = 0.79`, `EUR_RATE = 0.86`.

---

## Test data status

`subscriptions.json` is clean. The Togekiss price alert (`sv3-85`, see Task 3 above) is intentionally retained on the volume — it's the test fixture for Deploy C verification. Once that deploy fires the alert successfully, the alert will either auto-remove (one-shot) or be marked `triggered: true` depending on the function's logic at line 738.

---

## Open question for tomorrow's session

If Deploy C reveals that MTG or Yu-Gi-Oh profiles use a DIFFERENT price structure than Pokémon (entirely possible given different source APIs), the algorithm may need per-game branching or a more defensive iteration approach. Worth checking one MTG and one Yu-Gi-Oh profile.json before assuming the Pokémon-derived algorithm works universally.