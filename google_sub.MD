# Handover: Google Play Store Submission — GrailSweep

## Status: fresh phase, nothing started yet. All previous backlog (Parts 1–5) closed except passive Outlook reputation wait.

This handover starts the path from "live PWA at grailsweep.com" to "GrailSweep installable from the Google Play Store on Android." No Google developer account exists yet, no packaging has been done, no assets prepared.

---

## Tool recommendation: PWABuilder

**Use PWABuilder (pwabuilder.com), Microsoft's web UI built on top of Google's Bubblewrap.**

### Why PWABuilder over the alternatives

Three viable tools exist for getting a PWA into the Play Store. Here's the comparison, scored against GrailSweep's actual situation:

| Tool | What it does | Pros | Cons | Fit for GrailSweep |
|---|---|---|---|---|
| **PWABuilder** | Web UI that wraps your PWA as a TWA (Trusted Web Activity) using Bubblewrap under the hood. You enter a URL, it generates a `.aab` file for upload. | No local install. No Java/Android SDK headaches. Lighthouse audit built in. Microsoft maintains the UI, Google maintains the engine. | Web-based, so if pwabuilder.com is down on submission day, you're blocked (rare). | **Best fit.** Matches the working conventions in this project — minimum fuss, web tooling, no local toolchain to maintain. |
| **Bubblewrap CLI** | Google's official Node CLI for the same job. PWABuilder uses this internally. | Full control. Reproducible builds. Same output as PWABuilder. | Requires Node, JDK 17+, Android SDK, and careful keystore management on your local machine. CLI is fiddly with Java path issues. | Overkill. Same result as PWABuilder but more moving parts to maintain. |
| **Capacitor** | Wraps your web app in a native Android shell with its own runtime. You write/build a native app that loads your web code. | Access to true native APIs (deep Bluetooth, AR, etc.) that browsers don't expose. Same shell can target iOS. | You rebuild and resubmit to the Play Store for every code change (no instant web update). Larger APK. More maintenance. | **Wrong tool for this job.** GrailSweep is already a working PWA using web APIs (camera via `getUserMedia`, Stripe via redirect, etc.). No native API is needed. |

### What this means in practice

A TWA built by PWABuilder is essentially Chrome running your PWA in full-screen mode, signed and packaged as an Android app. Web changes you deploy to grailsweep.com appear in the TWA instantly — no app store resubmission required for content updates. You only resubmit when you bump the app version (icon change, manifest change, target SDK update, etc.).

This is the same approach used by Twitter Lite, Google Maps Go, and most PWA-first apps on the Play Store.

---

## Up-front decision: Personal vs Organization developer account

This decision sets the path for everything that follows. Make it before signing up.

| | Personal account | Organization account |
|---|---|---|
| Cost | $25 one-time | $25 one-time |
| ID required | Government photo ID | DUNS number + business registration + verified business website |
| **12-tester / 14-day closed test required before public release** | **Yes** | No |
| Suitable if | You're publishing solo, no formal business entity | You have a registered business (Ltd, LLC, etc.) |

**The 12-tester rule is the headline gotcha.** Google introduced this in late 2023 for personal accounts to combat low-quality apps. You need 12 real people running your app via a closed test for 14 continuous days before Google will let you publish publicly. They must each open and use the app during that window. This adds two weeks minimum to your timeline if going personal.

**Two paths to consider:**

- **Personal account, start lining up 12 testers now.** Beta tester list candidates from memory: Craig's son (Funko collector, has been earmarked as a beta tester anyway), Craig's business partner, family/friends. Realistically 12 is doable but needs organising.
- **Organization account via the business arrangement with your partner.** Requires getting a DUNS number first (free at upgrade.dnb.com, takes 1–30 business days). If the business is registered, this is the cleaner path long-term — no tester requirement, more credibility, and necessary anyway for any future financial app concerns.

**Open question for early in the new session:** which path? Recommend deciding before any further action.

---

## Stripe billing compliance — read this before anything else

This is the biggest non-obvious risk for GrailSweep specifically and needs settling before submission.

Google Play's Payments Policy requires apps to use **Google Play Billing** for in-app digital purchases (subscriptions, virtual currency, scan top-ups, etc.). Google takes 15–30% on those transactions.

GrailSweep currently uses **Stripe direct** for:
- Monthly subscription (£3.99) — price_1TSXwYFl8aS11HhgulxrZUhR
- Annual subscription (£34.99) — price_1TSXyrFl8aS11Hhg2qrU45sS
- Founder yearly (£19.99 cap 100) — price_1TSXf7Fl8aS11HhgE3lOk3Ma
- Top-up packs (£1.79 / 75 scans) — price_1TSfPyFl8aS11HhgBgUuomas

Inside a TWA, those Stripe CTAs on grailsweep.com render inside the Android app. Google's enforcement on this has tightened.

**Three viable approaches, in order of safety:**

1. **EU users via the Digital Markets Act exception.** Stripe is permitted for digital purchases in the EU. GrailSweep is UK-based (post-Brexit, not EU) — so this exception does NOT cover UK users. Limited scope.
2. **In-app: information only, redirect to web for purchase.** The Android app shows tier info but the "Subscribe" button opens grailsweep.com in the user's browser (not in the TWA). Stripe runs in the user's real Chrome, separate session, payment completes there, account links back via login. Compliant under current policy. Loses some conversion but stays safe.
3. **Add Google Play Billing alongside Stripe.** Detect platform; serve Play Billing on Android, Stripe on web. Most defensive but means 15–30% margin loss on Android subscribers and significant integration work (Modal backend would need Play Billing webhook handling alongside the existing Stripe one).

**Recommendation:** Approach 2 for v1.0 launch. Approach 3 as a follow-up if Android revenue justifies the integration cost. Approach 1 ignored — UK userbase doesn't benefit.

This decision affects what the app looks like at submission time. Worth settling before packaging.

---

## Phased plan

The work splits into seven phases, easiest first. Each phase has a clear gate; finish one before starting the next.

### Phase 1 — PWA readiness audit (no signup, no cost)

Before paying $25 and committing to Google's process, confirm GrailSweep actually qualifies as a "good PWA" by Google's measure.

**Acceptance criteria for TWA:**
- HTTPS only ✅ (grailsweep.com via Cloudflare, confirmed)
- Valid web app manifest with: `name`, `short_name`, `start_url`, `display: standalone` or `fullscreen`, `theme_color`, `background_color`, `icons` array (must include 512×512 PNG and 192×192 PNG)
- Registered service worker
- Lighthouse PWA audit score ≥ 80 (Google's stated minimum for TWA)

**Action items:**

1. Open Chrome DevTools on grailsweep.com → Application tab → Manifest. Confirm all required fields present. Pay particular attention to the icon sizes — TWA needs the 512×512 specifically for the Play Store splash screen.
2. Application tab → Service Workers. Confirm one is registered and running.
3. Lighthouse tab → run audit in "Mobile" mode, "Progressive Web App" category. Target score ≥ 80. Fix any failures before proceeding.
4. Note any gaps. If the manifest is missing fields, fix in `static/manifest.json` (or wherever it lives — recon needed) and deploy before phase 4.

**Output of phase 1:** confidence that grailsweep.com will pass PWABuilder's audit cleanly.

### Phase 2 — Asset preparation (can do in parallel with phase 3)

Google Play requires a specific set of graphics and copy. Prepare these before signup so you're not stalled waiting for design work.

**Required graphics:**

| Asset | Spec | Purpose |
|---|---|---|
| App icon | 512×512 PNG, 32-bit, no transparency | Store listing |
| Feature graphic | 1024×500 PNG/JPEG | Top of store listing |
| Phone screenshots | Min 2, max 8. 16:9 or 9:16, 1080p+ recommended | Store listing carousel |
| 7-inch tablet screenshots | Optional but recommended | Tablet listing |
| 10-inch tablet screenshots | Optional but recommended | Tablet listing |
| Promo video | Optional, YouTube URL | Top of store listing |

**Required copy:**

| Field | Limit | Purpose |
|---|---|---|
| App name | 30 chars | "GrailSweep" |
| Short description | 80 chars | First line user sees |
| Full description | 4000 chars | The full pitch |
| Category | One of Google's preset categories — likely "Shopping" or "Lifestyle" | Browse classification |
| Content rating | Filled via Google's questionnaire | Age rating |
| Privacy policy URL | Must be publicly accessible | https://grailsweep.com/privacy (already exists from Phase 4 legal work) |

**Required data:**

- Data Safety form: declare what user data the app collects, why, and how it's handled. Will need to list: email addresses (subscriptions), payment data (handled by Stripe — third party), photos/camera (scanning), device identifiers (analytics if any). Be honest and conservative; Google audits this.
- Target audience: pick age range. Card collecting skews adult but is family-safe; "Designed for 13+" is probably right.
- Government apps / news / health declarations: all "no" for GrailSweep.

### Phase 3 — Google Play Console signup

Once the personal-vs-organization decision is made:

1. Go to https://play.google.com/console
2. Sign in with a Google account dedicated to GrailSweep (don't reuse personal Gmail — create a fresh one or use a business Workspace account). Must have 2-Step Verification enabled.
3. Pay the $25 one-time fee.
4. Complete identity verification (ID upload).
5. For organization: provide DUNS, business registration, verified website ownership (via Search Console — already verified from the earlier sitemap.xml/robots.txt work).
6. Wait 24–48 hours for activation.

**No app submission happens at this stage.** Just account creation.

### Phase 4 — Package the PWA with PWABuilder

This is the actual wrapping step. After this, you have a `.aab` file ready to upload.

1. Go to https://www.pwabuilder.com
2. Enter `https://grailsweep.com` and click Start.
3. PWABuilder runs an audit and shows a PWA Score. If score is below ~80, it'll tell you exactly what to fix. Loop back to Phase 1 if needed.
4. Click "Package for Stores" → "Android".
5. Configure the Android package:
   - **Package ID**: a reverse-DNS identifier like `com.grailsweep.app` — this is permanent and globally unique, can never be changed once submitted. Choose carefully.
   - **App name**: GrailSweep
   - **Launcher name**: GrailSweep (what shows under the icon on the home screen)
   - **Theme color** and **Background color**: pull from the existing brand
   - **Display mode**: `standalone` (recommended for TWA)
   - **Notifications**: enable if you want push later, leave off for v1
   - **Signing key**: choose "Use mine" if you've already generated one, otherwise let PWABuilder generate. **Critical: download and back up the keystore file and credentials. Lose this and you can never update the app — Google requires the same signing key for every version.**
6. Click Generate. Download the zip.
7. Inside the zip: `app-release-signed.aab` (the upload file), `app-release-signed.apk` (for local install testing), `signing-key-info.txt` (keystore details — back this up immediately to multiple locations, treat like a password), `assetlinks.json` (next phase needs this).

**Test locally before uploading:** install the `.apk` on an Android device via USB (`adb install app-release-signed.apk`) and confirm it launches GrailSweep, the camera scanner works, login works, premium gating works. Better to find issues here than via a Play Store rejection.

### Phase 5 — Digital Asset Links setup

Without this step, the TWA will show a Chrome address bar at the top of the app (it falls back to "Custom Tab" mode). Setting this up properly gives you full-screen, no-browser-UI experience.

1. Inside the PWABuilder zip, open `assetlinks.json`. It looks like:
   ```json
   [{
     "relation": ["delegate_permission/common.handle_all_urls"],
     "target": {
       "namespace": "android_app",
       "package_name": "com.grailsweep.app",
       "sha256_cert_fingerprints": ["AB:CD:EF:..."]
     }
   }]
   ```
2. Host this file at `https://grailsweep.com/.well-known/assetlinks.json` — exactly that path, exactly that filename. Add a route in Flask or a Cloudflare Worker rule. Must be served as `Content-Type: application/json` with no redirects.
3. Verify by visiting the URL directly in a browser.
4. Verify by running Google's tester: `https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://grailsweep.com&relation=delegate_permission/common.handle_all_urls`

### Phase 6 — Closed testing track (mandatory for personal accounts)

If personal account: this is the 12-tester / 14-day phase.

1. In Play Console: Testing → Closed testing → Create track
2. Upload the `.aab` from Phase 4
3. Add a tester list (email addresses or a Google Group). Need ≥12 distinct testers.
4. Each tester gets an opt-in link, must install via the Play Store, and use the app at least once. Google tracks this.
5. Wait 14 calendar days minimum from the day all 12 are confirmed active.
6. Address any tester feedback / crash reports during this window.

If organization account: this phase is **optional but strongly recommended**. Even without the mandatory requirement, running a 1–2 week closed test surfaces real-world issues (Android version compatibility, screen sizes, etc.) before public exposure.

### Phase 7 — Production submission

1. Play Console → Production → Create new release
2. Upload the `.aab` (or promote from closed testing)
3. Fill in release notes ("Initial public release of GrailSweep")
4. Roll out → review queue
5. Google review takes 1–7 days for first submission. Subsequent updates often clear in hours.
6. Live.

---

## Key identifiers (carry forward from prior phases)

- **Domain:** grailsweep.com (also .app and .co.uk redirect-only)
- **Backend:** Modal serverless, workspace `c-a-buckley`, app `matchit-api`
- **Privacy policy:** https://grailsweep.com/privacy (already published)
- **Terms of service:** https://grailsweep.com/terms (already published)
- **LIA / Legitimate Interest Assessment:** https://grailsweep.com/lia
- **Support email:** support@grailsweep.com (Resend-routed, post-Part-4 migration)
- **Stripe price IDs (live):**
  - monthly: `price_1TSXwYFl8aS11HhgulxrZUhR`
  - annual: `price_1TSXyrFl8aS11Hhg2qrU45sS`
  - founder_yearly: `price_1TSXf7Fl8aS11HhgE3lOk3Ma`
  - topup_75: `price_1TSfPyFl8aS11HhgBgUuomas`
- **Search Console:** already verified for grailsweep.com (from sitemap submission work)
- **PWA assets location:** to be confirmed during Phase 1 recon — likely `static/manifest.json` and `static/sw.js` or similar; needs eyeball before audit

---

## Things to flag / open questions for the new session

1. **Personal vs Organization account?** Decide first — affects the whole timeline.
2. **Stripe billing approach — option 2 (redirect to web) vs option 3 (add Google Play Billing)?** Affects what gets built in Phase 1.
3. **Twelve testers lined up?** If personal account, start recruiting now. Realistic candidates: son, business partner, family, friends, GrailSweep beta-list users if any exist.
4. **Package ID choice.** `com.grailsweep.app` is the obvious candidate, but worth confirming nothing else is using it on the Play Store first. **Permanent decision — cannot be changed.**
5. **Keystore storage.** Where does the signing key get backed up? Lose it and the app is unupdateable. Recommend: 1Password / Bitwarden, plus a printed copy in a safe. Treat like a master password.
6. **Funko vertical timing.** Originally earmarked as son's beta test. May or may not factor into the closed-testing tester pool — separate decision.
7. **Apple App Store deferral.** Apple Guideline 4.2 is brutal on thin PWA wrappers. Whole separate project; not in scope for this handover. Recommend tackling Google first, learning the process, then revisiting iOS strategy later.

---

## Working conventions (carry forward from Parts 1–5)

- One phase at a time, complete and verify before starting the next
- Recon before edits; no surprises
- Backup before destructive actions (especially the keystore — back it up to multiple locations before uploading anything)
- "STOP. DO NOT DEPLOY." footer on every Claude Code instruction
- Concise messages, no padding
- For session-pacing: when user says "do phase X", do phase X then stop. Don't auto-extend.

---

## Expected timeline

Assuming personal account, 12 testers organised, no major PWA audit failures:

| Phase | Time |
|---|---|
| 1. PWA audit + fixes | 1–3 hours |
| 2. Assets prepared | 4–8 hours (most of the time is on screenshots + copy) |
| 3. Google account signup + verification | 1 hour active, 24–48 hours waiting |
| 4. PWABuilder packaging | 1 hour |
| 5. Digital Asset Links | 30 min |
| 6. Closed testing | **14 days minimum** (personal) or 1–2 weeks recommended (organization) |
| 7. Production review | 1–7 days |

**Realistic total: 3–4 weeks from start to live on Play Store for a personal account.** Organization account skips Phase 6's hard 14-day requirement and could be live in 1–2 weeks if DUNS is already in hand.

Time to grandkids playing GrailSweep on the school bus: not soon enough.