# Handover: TWA Packaging & Play Store Submission

**Next session focus:** Phase 7 — package GrailSweep as a Trusted Web Activity (TWA) and get it ready for Google Play submission.

---

## Status going in

All code-side launch prep is complete. The web app is production-ready: pricing tiers live, scan top-ups working, cold-start fix verified, UI messaging consistent across all surfaces, external rebrand audit clean. No code changes blocking Phase 7.

---

## Phase 7 — TWA Packaging

A Trusted Web Activity is the Android app shell that wraps the GrailSweep PWA. It opens in a fullscreen Chrome instance with no browser UI, looking native, and gets distributed via the Play Store. PWABuilder generates the package; Digital Asset Links proves we own both the website and the app.

### Step 1 — Asset prep

Gather before packaging:

- **App icon**: 512x512 PNG (Play Store listing). PWABuilder may auto-derive smaller sizes from a single high-res source.
- **Screenshots**: minimum 2, recommended 4-8. Phone form factor required, tablet optional. Capture the scanner, results page, collection, and pricing/upgrade screens.
- **Feature graphic**: 1024x500 PNG for the Play Store listing.
- **Store listing copy**: short description (80 chars), full description (4000 chars max), title (30 chars).
- **Privacy policy URL**: already live at grailsweep.com — point to the existing deep-link anchor.

### Step 2 — Lock the package ID

Recommended: `com.grailsweep.app`.

**This is permanent for the lifetime of the app — it cannot be changed without re-listing as a new app, losing all reviews, ratings, and install history.**

Before locking in, quick search Google Play to confirm no existing app uses it.

### Step 3 — PWABuilder packaging

PWABuilder (pwabuilder.com) generates the .aab (Android App Bundle) and .apk from the live PWA. Inputs:

- URL: `https://grailsweep.com`
- Package ID: `com.grailsweep.app` (from Step 2)
- Signing key: PWABuilder generates one if you don't supply your own

### Step 4 — Keystore backup (critical)

The signing keystore PWABuilder generates is what Google uses to verify all future updates come from you.

**If you lose this keystore, the app becomes un-updateable — forever.** Re-listing under a new keystore means losing all reviews, ratings, installs, history.

Backup plan:

- **Primary**: 1Password / Bitwarden / equivalent password manager
- **Secondary**: printed paper copy in a physical safe
- **Tertiary (optional)**: encrypted USB drive in a different physical location

Do this immediately after PWABuilder generates it. Don't wait.

### Step 5 — Digital Asset Links

Host a file at `https://grailsweep.com/.well-known/assetlinks.json` that proves the GrailSweep website and the Android app are associated. PWABuilder generates the contents; we just publish at that path.

Format:

```json
[{
  "relation": ["delegate_permission/common.handle_all_urls"],
  "target": {
    "namespace": "android_app",
    "package_name": "com.grailsweep.app",
    "sha256_cert_fingerprints": ["<from PWABuilder>"]
  }
}]
```

Verification: visit `https://grailsweep.com/.well-known/assetlinks.json` in a browser, confirm it returns valid JSON with the right SHA-256 fingerprint. Without this, the app will show a Chrome address bar inside — defeating the point of a TWA.

### Step 6 — Local sideload testing

Before submitting to Play Store, install the .apk on an Android device:

```
adb install grailsweep.apk
```

Verify:

- App opens fullscreen, no browser chrome visible
- Login / scan / payment flows work end-to-end inside the TWA
- "Get more scans" CTAs launch external Chrome to grailsweep.com (the Stripe redirect behaviour built in Phase 5b)
- Back button behaves correctly
- App appears in the Android app drawer with the correct icon

If the app shows a Chrome address bar at the top → Digital Asset Links is misconfigured. Back to Step 5.

---

## Phase 8 — Play Store Submission

### Step 1 — Google Play Console signup

- Account type: **Personal**
- One-time fee: **$25**
- Identity verification required (passport or driving licence)

### Step 2 — Closed test (mandatory for Personal accounts)

Google requires Personal-account developers to run a closed test with **12+ testers for at least 14 days** before allowing production submission.

The 14-day clock can run in parallel with Phase 7 work — start tester recruitment now.

**Tester recruitment — 12 people needed:**

- Craig's son (Funko collector, already earmarked)
- Business partner
- Family members
- Friends (especially anyone who collects Pokémon / MTG / Yu-Gi-Oh)
- Wider network if local pool isn't enough

Testers need a Google account email. Add them to the closed test track in Play Console; they get an opt-in link, install the app, use it during the test period.

### Step 3 — Production submission

After 14 days of closed testing with 12+ active testers:

- Submit for production review (typically 1-7 days)
- Approval → app live on Google Play Store

---

## Working conventions (carry forward)

- One phase at a time — complete and verify before starting the next
- Recon-only passes before any code changes
- Backup before destructive edits
- "STOP. DO NOT DEPLOY." footer on every Claude Code instruction
- Concise messages, no padding
- When Craig says "do phase X", do phase X then stop
- Surface findings, let Craig decide scope — don't pre-emptively expand

---

## Key identifiers

### Infrastructure

- Domain: `grailsweep.com` (also `.app` and `.co.uk`)
- Modal workspace: `c-a-buckley`
- Modal app: `matchit-api`
- Modal volume: `matchit-data-v2` at `/modal_data`
- Deploy command: `$env:PYTHONIOENCODING="utf-8"; modal deploy matchit_modal.py`

### Stripe

- Webhook URL: `https://grailsweep.com/webhook/stripe`
- Webhook status: enabled, protected by async idempotency dict
- Contact email: `support@grailsweep.com`
- Live price IDs:
  - monthly: `price_1TSXwYFl8aS11HhgulxrZUhR`
  - annual: `price_1TSXyrFl8aS11Hhg2qrU45sS`
  - founder_yearly: `price_1TSXf7Fl8aS11HhgE3lOk3Ma`
  - topup_75: `price_1TSfPyFl8aS11HhgBgUuomas`

### Modal Dicts

- `scan-counters` — live credit balance per user/device
- `stripe-webhook-events` — idempotency tracker for webhook event IDs

### Domain & redirect

- `raredex.co` (legacy) — kept active, Cloudflare Worker 301-redirects all traffic to `grailsweep.com`. Auto-renew should stay on at registrar.

---

## Deferred item — schedule when time permits

### Issue B — no locking on subscriptions.json writes

Pre-existing race condition surfaced during Phase 6b recon. When two writes hit `subscriptions.json` within milliseconds (e.g. parallel webhook deliveries, or a webhook + redemption in the same instant), the second write can silently overwrite the first.

**Currently low priority:**

- Race window is millisecond-scale
- User base small enough that real-world probability is essentially zero
- Fix is straightforward: wrap read-modify-write of `subscriptions.json` in a lock (`threading.Lock` for in-container races, file lock if cross-container races become a concern)
- Estimated effort: 30-45 minutes including verification

**Schedule for:** whenever user growth approaches a level where the race window starts to matter, or as a quiet-half-hour safety improvement before launch. **Not a Phase 7 blocker.**

---

## STOP. DO NOT DEPLOY.

This handover describes pending work, not work in flight. No code changes have been made based on this document.

Phase 7 involves **no code changes to the running app** — it's packaging, asset prep, and Play Console configuration. Phase 8 is submission, not code.

When Phase 7 begins, the first concrete action is **Step 1: asset prep**. Don't skip ahead to PWABuilder before assets are ready — packaging without screenshots/copy means re-doing the package later.