# Stripe Redirect Spec — GrailSweep Android TWA (v1.1, post-recon)

**Purpose:** lock down the in-app "Subscribe" / "Buy Top-Up" behaviour so the
Android submission cleanly passes Google Play's Payments Policy check.
**Status:** spec only. No code changes until explicit go-ahead.
**Updated:** post-recon. File paths and architecture now reflect the actual
GrailSweep codebase.

---

## 1. Goal

When GrailSweep runs as a Trusted Web Activity (TWA) on Android, every tap on
a Subscribe or Buy button hands the user off to the **system Chrome browser**
via an explicit Android intent. Payment never happens inside the TWA shell,
never inside a Chrome Custom Tab floating on top of the TWA — fully external,
visibly external.

The user completes Stripe checkout in their normal Chrome session, receives
their GRAIL-XXXX-XXXX (subscription) or TOPUP-XXXX-XXXX (top-up) code by
email, and returns to the app to redeem it (existing flow).

## 2. Why this matters

Default TWA behaviour would already open `checkout.stripe.com` in a Chrome
Custom Tab — technically out-of-app, but visually it reads like the app is
handling payment. Google reviewers tap subscribe buttons and need to see an
unambiguous external launch. The intent URL approach guarantees that.

This is a defensive upgrade over default behaviour, low-cost, high-confidence
compliance signal.

---

## 3. The redirect technique

### 3a. Architecture (post-recon understanding)

The existing flow:

```
[TWA user taps button]
    → handleUpgrade(tier) or gsTopupBuy(tier)
    → fetch POST /api/create-checkout-session  { tier }
    → server creates Stripe Session, returns { url: "https://checkout.stripe.com/c/pay/cs_live_..." }
    → window.location.href = data.url     ← change happens HERE
```

We only modify the final navigation step. The server, the fetch call, the
button markup — all unchanged in terms of behaviour (button text changes
covered in section 4).

### 3b. The shared helper

Create a single helper used by both `handleUpgrade()` and `gsTopupBuy()`.
Suggested location: inline in `templates/base.html` near the top so it's
available globally, OR a new `static/js/twa_redirect.js` loaded by base.html.
Recon to confirm which fits the existing pattern.

```javascript
// Returns true if the page is being rendered inside the GrailSweep
// Android TWA (Trusted Web Activity).
function gsIsRunningInTWA() {
  if (sessionStorage.getItem('gs_isTWA') === 'true') return true;
  if (document.referrer && document.referrer.startsWith('android-app://')) {
    sessionStorage.setItem('gs_isTWA', 'true');
    return true;
  }
  return false;
}

// Navigates the user to a checkout URL. If in TWA, forces system Chrome
// via Android intent. Otherwise normal navigation.
function gsNavigateToCheckout(checkoutUrl) {
  if (gsIsRunningInTWA()) {
    // Strip the https:// prefix and wrap in intent URL.
    // package=com.android.chrome forces the system Chrome app, not a Custom Tab.
    const stripped = checkoutUrl.replace(/^https:\/\//, '');
    const intentUrl =
      'intent://' + stripped +
      '#Intent;scheme=https;package=com.android.chrome;end';
    window.location.href = intentUrl;
  } else {
    window.location.href = checkoutUrl;
  }
}
```

### 3c. Wiring in the two callers

**`templates/upgrade.html` (handleUpgrade function, ~line 401):**

```diff
- if (data.url) {
-     window.location.href = data.url;
- }
+ if (data.url) {
+     gsNavigateToCheckout(data.url);
+ }
```

**`templates/base.html` (gsTopupBuy function, ~line 1348):**

```diff
- if (d.url) { window.location.href = d.url; }
+ if (d.url) { gsNavigateToCheckout(d.url); }
```

That's the entire JS change. Two one-line edits plus the shared helper.

---

## 4. UI copy changes

### 4a. Button labels

All 7 purchase buttons get updated text to carry the external cue.

**`templates/upgrade.html`:**

| Line | Current | New |
|---|---|---|
| 58 | `Get Pro Monthly` | `Subscribe on grailsweep.com` |
| 79 | `Get Pro Annual` | `Subscribe on grailsweep.com` |
| 101 | `Become a Founder` | `Claim Founder offer on grailsweep.com` |

**`templates/base.html` (top-up modal):**

| Line | Current | New |
|---|---|---|
| 1187 | `75 scans · £1.79` | `75 scans · £1.79 — purchase on grailsweep.com` |
| 1195 | `Upgrade to Pro · £3.99/mo` | `Pro · £3.99/mo — subscribe on grailsweep.com` |
| 1204 | `Upgrade to Annual · £34.99/yr` | `Annual · £34.99/yr — subscribe on grailsweep.com` |
| 1213 | `Founder Yearly · £19.99/yr` | `Founder Yearly · £19.99/yr — claim on grailsweep.com` |

The base.html buttons keep their price-in-label format (shorter, fits the
modal layout) but add the external qualifier.

### 4b. Disclaimer copy

Add a small disclaimer in two places.

**`templates/upgrade.html`:** insert above the three tier buttons (or below
each, designer's call) — appears once at the top of the pricing layout:

> Payments are processed securely by Stripe on our website. After purchase,
> return to the app and enter the code emailed to you.

**`templates/base.html` top-up modal:** insert above the 4-button group:

> Purchases complete on grailsweep.com. After payment you'll receive a
> redemption code by email — enter it in the box above.

---

## 5. The return path

Existing email-with-code flow handles this:

1. User in TWA taps any Subscribe / Buy button
2. System Chrome opens Stripe checkout (via intent)
3. User completes payment in Chrome
4. Existing Stripe webhook generates code:
   - GRAIL-XXXX-XXXX for monthly / annual / founder_yearly
   - TOPUP-XXXX-XXXX for topup_75
5. Email sent with code (existing)
6. User opens GrailSweep app
7. User pastes code in the redeem field:
   - Subscription codes → `templates/upgrade.html` redeem box (`upgradeValidateCode()`)
   - Top-up codes → `templates/base.html` modal redeem box (`gsRedeemTopupCode()`)
8. Device fingerprint registers, premium / top-up activates

Zero new infrastructure. Two redeem flows already exist with distinct
placeholders (`GRAIL-XXXX-XXXX` and `TOPUP-XXXX-XXXX`), no risk of confusion.

**v1.1 polish (NOT v1.0):** Stripe success page could carry a "Return to
GrailSweep app" deep link plus a "Copy code" button. Defer.

---

## 6. Anti-patterns (each one = rejection risk)

1. **Stripe Elements rendered inside the TWA.** Not how the current
   architecture works — confirmed in recon. Safe.
2. **Stripe Checkout opened in a Chrome Custom Tab.** This is the default
   behaviour we're upgrading away from with the intent approach.
3. **Stripe success page rendered inside the TWA.** Current `success_url` is
   `https://grailsweep.com/payment-success?...` — same domain as TWA scope.
   This means after Stripe checkout, the success page would route back into
   the TWA. **Acceptable for v1** because the user is returning to our own
   confirmation page, not Stripe's. Confirmation copy on that page should
   tell them to check their email for the code.
4. **Button labels without the external qualifier.** Reviewer needs the
   visual cue.
5. **Restore Purchase button.** Doesn't exist, don't add one.

---

## 7. Files to change

Confirmed from recon:

| File | Change |
|---|---|
| `templates/base.html` | Add `gsIsRunningInTWA()` and `gsNavigateToCheckout()` helpers near top of script block. Modify `gsTopupBuy()` to call `gsNavigateToCheckout()`. Update 4 button labels (lines 1187, 1195, 1204, 1213). Add disclaimer above modal buttons. |
| `templates/upgrade.html` | Modify `handleUpgrade()` to call `gsNavigateToCheckout()`. Update 3 button labels (lines 58, 79, 101). Add disclaimer above tier buttons. |
| `static/manifest.json` | No change — manifest is TWA-clean per recon. |
| `app.py` | No change to `/api/create-checkout-session`. |
| `config.json` | No change. |

Single PR / single deploy.

---

## 8. Test plan

1. **Desktop browser:** Pricing buttons hit `/api/create-checkout-session`,
   navigate to `checkout.stripe.com` in same tab. No intent URL. Verify in
   DevTools Network tab.
2. **Mobile Chrome (not TWA):** Same as desktop. Verify `gsIsRunningInTWA()`
   returns `false`.
3. **Sideloaded TWA via PWABuilder APK:** Install via `adb install`. Tap
   each of the 7 purchase buttons. **Expected:** Chrome launches as a
   separate app (system Chrome icon visible in recent apps), GrailSweep
   app is no longer in the foreground. Complete checkout. Receive email
   with code. Return to GrailSweep app. Redeem code. Premium / top-up
   activates.
4. **Reviewer simulation:** ask Craig's son (Funko beta tester) or partner
   to play Google reviewer — install via `adb install`, tap every Subscribe
   button, confirm each launches Chrome externally. No Stripe UI should
   appear inside the TWA window.
5. **Regression — web users:** confirm the existing web purchase flow on
   grailsweep.com is unaffected. All 7 buttons + 4 tiers + redeem boxes
   work as before.

All 5 must pass before packaging the production `.aab` for upload.

---

## 9. Out of scope

- iOS / Apple App Store strategy (deferred)
- Google Play Billing integration as a fallback
- Deep-link return path from Stripe success → app
- Analytics on Android-vs-web conversion
- Refactoring `app.py:4880-4881` success/cancel URLs to include `src=android`
  parameter (could be added later for analytics; not needed for compliance)

---

## STOP. DO NOT DEPLOY.

This spec describes intended changes. Do not deploy any code until:

1. Backup of `templates/base.html` and `templates/upgrade.html` is taken
2. Each change is reviewed against this spec
3. Local TWA test (section 8, step 3) passes
4. Craig gives the explicit go-ahead

The closed-test track on Play Console must run against the version that has
all of this in place. Pushing changes mid-test risks resetting tester
engagement progress with Google.