// GrailSweep Service Worker — enables PWA install + basic caching + push notifications
// v126 (2026-08-01) — CLEANUP: diagnostic pills removed from /contact, /terms and
// /privacy, and the v124 'message' handler removed with them. No functional
// change: network-first navigation, the response.ok cache guard and the offline
// fallback are all untouched, as are the v125 disclosure wording, the dropped
// target="_blank" and the max-age=0 headers on the legal/contact routes.
// This is the build intended for App Store resubmission.
// v125 (2026-08-01) — disclosure paragraph on the three purchase surfaces now
// reads '... Terms of Use and Privacy Policy — see links in the footer below.'
// The two links REMAIN as anchors (3.1.2(c) requires functional Terms/Privacy
// links on the purchase screen); target="_blank" was dropped last edit so they
// navigate exactly like the footer links — in a Capacitor WKWebView _blank is
// routed to a separate browser view with its own cache, the likely reason the
// footer links showed fresh pages while these did not. UNTESTED LIVE until this
// deploy. Diagnostic pills (v124) are still present and must be removed in a
// final pass before resubmit.
// v124 (2026-08-01) — TEMPORARY DIAGNOSTIC BUILD. Adds a 'message' handler so a
// page can ask the ACTIVE controller to name its CACHE_NAME, feeding the
// #gsDiagPill banner on /contact. Purpose: observe, on-device, (a) which SW
// actually controls the document, (b) whether the new HTML reached the WebView,
// (c) whether gsIsRunningInIOSApp() is true on that page load. REMOVE this
// handler and the pill before App Store resubmit.
// v123 (2026-08-01) — ROOT-CAUSE FIX: navigation requests are now network-first.
// The previous stale-while-revalidate branch returned the cached page whenever
// one existed, so the installed iOS app rendered pre-v122 HTML indefinitely —
// every compliance fix (Stripe text, access-code copy, disclosures) verified
// clean on the server and was invisible on device. Diagnosed 2026-08-01: the
// live HTML contained gs-web-only/gs-ios-only and gsIsRunningInIOSApp() was
// returning true (StoreKit prices updated fine), but the toggler had nothing to
// hide because the SW handed it a stale document.
// NOTE: the OLD sw.js must activate this one first, so the FIRST launch after
// deploying still shows stale pages. Force-quit and reopen before filming.
// v122 (2026-07-31) — supersedes the undeployed v121. iOS resubmit pass:
//   A) the 3 gs-sub-legal disclosures now price-sync to StoreKit via
//      gsUpdateIOSPrices (spans wrap ONLY the amount, so the required
//      "1 month"/"1 year" length wording survives the override);
//   B) the referral offer (Stripe coupon / Play only) is hidden on iOS —
//      advertising an unredeemable discount is misrepresentation;
//   C) /terms + /privacy no longer steer to Stripe, and cancellation guidance
//      is platform-conditional;
//   D) access-code wording swapped for an Apple-ID-entitlement alternate on iOS;
//   E) Apple + Google added to the GDPR processor list.
// Bump REQUIRED: '/' is in PRECACHE and renders base.html, and /terms + /privacy
// are stale-while-revalidate navigations — installed WebViews would otherwise
// keep serving the pre-fix markup. Cloudflare purge also required (those two
// pages carry s-maxage=604800).
// v121 (2026-07-31) — App Store 3.1.2(c) rejection fix (v1.0(20), reviewed 30 Jul).
// The reviewer hit #gsTopupModal on exhausting free scans — it purchases via real
// StoreKit (gsTopupBuy -> gsIosPurchase) but showed no Terms/Privacy links and only
// implied length. Added the subscription disclosure to that modal AND to
// landing.html's pricing section, moved .gs-sub-legal CSS into base.html's shared
// style block, and switched the cancel-span toggler to querySelectorAll so every
// disclosure on a page flips, not just the first. Bump REQUIRED: '/' is in PRECACHE
// and renders base.html, so installed WebViews would keep serving the modal markup
// that has no disclosures — i.e. exactly the screen that was rejected.
// v120 (2026-07-29) — iOS resubmission: the subscription cancellation sentence on
// /upgrade is now platform-aware (iOS -> Settings > Apple ID > Subscriptions,
// Android -> Play Store > Subscriptions, web -> account settings), toggled by the
// same IIFE in base.html that gates the store badges. Bump REQUIRED because that
// IIFE lives in base.html and '/' is in PRECACHE, so installed users would keep
// running the v119 script that has no cancellation-text branch at all.
// v119 (2026-07-29) — iOS resubmission: store badges are now platform-gated in
// base.html (Play badge hidden in the iOS app, App Store badge hidden in the
// Android TWA) and upgrade.html gained the auto-renewing-subscription
// disclosure. Bump REQUIRED: '/' is in PRECACHE and renders base.html, so
// installed users would keep serving the old footer with both badges visible —
// exactly the cross-platform reference the iOS gating exists to remove.
// v118 (2026-07-28) — JP cards added to the collector search catalogue.
// The bump is REQUIRED, not cosmetic: '/api/search-index/pokemon' is in
// PRECACHE, and activate() only evicts caches whose key !== CACHE_NAME, so
// installed users would otherwise keep the old JP-less index indefinitely
// and never see the EN/JP toggle on a runtime-cached /search.
// This comment ALSO changes the file's byte size on purpose — a bare version
// bump (v117 -> v118) is the same length and gets silently skipped by Modal's
// add_local_dir mount diff, reusing the cached image (see CLAUDE.md gotcha).
// v127 (2026-08-04) — Restore Purchases button (Apple 3.1.1 rejection fix):
// upgrade.html gained #gs-restore-btn + /api/revenuecat/restore reconcile,
// and base.html's gsIosPurchase now reconciles+activates on purchase too.
// v128 (2026-08-04) — Restore Purchases now also on landing.html (Apple
// 3.1.1 applies there too, since it offers purchases). gsRestorePurchases()
// moved from upgrade.html into base.html (now shared, id-parameterized) so
// landing.html's #gs-restore-btn-landing can reuse it without duplication.
// v129 (2026-08-04) — TEMPORARY DIAGNOSTIC BUILD. Adds a #gsDiagPillLanding
// banner to landing.html to determine, on-device, whether the restore
// button is missing from the DOM entirely (stale HTML) or present but
// hidden (reveal not firing). REMOVE before App Store resubmit — see the
// comment wrapping the pill block in templates/landing.html.
// v130 (2026-08-04) — RevenueCat webhook self-heal: _handle_revenuecat_event
// now looks up entries by scanning stripe_subscription_id instead of a
// direct subs.get(app_user_id) dict lookup (which could never match, since
// entries are keyed by the generated code, not app_user_id) — fixes
// RENEWAL/EXPIRATION/CANCELLATION/BILLING_ISSUE silently no-opping, adds
// PRODUCT_CHANGE/UNCANCELLATION handling, and makes product_id matching
// case-insensitive. Also removes the temporary #gsDiagPillLanding banner
// added in v129 (its job is done) and raises the /api/revenuecat/restore
// rate limit from 10 to 30 per 5 minutes.
// v131 (2026-08-04) — match.html's initInstallBanner() now early-returns
// when gsIsRunningInIOSApp() is true, so the "Add to Home Screen" web-PWA
// banner no longer shows inside the native iOS app (nothing to add from —
// there's no browser chrome). Reuses the existing detector, no new checks.
// v132 (2026-08-08) — match.html and results.html both changed (auth-engine
// wiring), so the cached document HTML is stale for both routes.
const CACHE_NAME = 'grailsweep-v132';
const PRECACHE = [
  '/',
  '/static/style.css',
  '/static/assets/grailsweep_app_icon.png',
  '/static/assets/gs_card_placeholder.png',
  '/api/search-index/pokemon',
  '/api/fx_rates'
];

const OFFLINE_API = [
  '/api/search-index/pokemon',
  '/api/fx_rates'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(PRECACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME && k !== 'gs-images-v1').map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

const IMAGE_HOSTS = ['images.grailsweep.com',
                     'images.pokemontcg.io'];

self.addEventListener('fetch', event => {
  // Cache-first for card images — separate cache from CACHE_NAME so images
  // persist across sw.js version bumps and aren't purged every deploy.
  if (IMAGE_HOSTS.some(h => event.request.url.includes(h))) {
    event.respondWith(
      caches.open('gs-images-v1').then(function(cache) {
        return cache.match(event.request).then(
          function(cached) {
            if (cached) return cached;
            return fetch(event.request).then(function(resp) {
              if (resp && resp.status === 200) {
                cache.put(event.request, resp.clone());
              }
              return resp;
            }).catch(function() {
              return new Response('', {status: 404});
            });
          }
        );
      })
    );
    return;
  }
  // Cache-first for the offline-search API endpoints — first load fetches
  // from network and stores it, every load after (online or offline)
  // serves straight from cache so the search page works with no signal.
  if (OFFLINE_API.some(u => event.request.url.includes(u))) {
    event.respondWith(
      caches.match(event.request).then(function(cached) {
        return cached || fetch(event.request).then(
          function(resp) {
            return caches.open(CACHE_NAME).then(function(cache) {
              cache.put(event.request, resp.clone());
              return resp;
            });
          }
        );
      })
    );
    return;
  }
  // NETWORK-FIRST for navigation requests.
  //
  // This was stale-while-revalidate (`return cached || networkFetch`), which
  // served the cached page UNCONDITIONALLY whenever one existed and only
  // refreshed the cache in the background. Effect: the installed iOS app was
  // permanently one deploy behind — it kept rendering pre-v122 HTML (old Stripe
  // / access-code text, no gs-web-only classes), so the platform toggler found
  // nothing to hide even though the server was serving the corrected markup and
  // gsIsRunningInIOSApp() was returning true. That is what caused the repeated
  // "fix verifies clean, ships, still broken on device" cycle, and it is a
  // compliance risk: App Review sees whatever the cache decides to show.
  //
  // Now: always try the network, fall back to cache only when offline. The
  // cache is still populated on every successful fetch, so offline use is
  // unchanged; it is just no longer allowed to win over a live response.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).then(
        function(response) {
          // Only cache good responses. fetch() resolves on 500s and Cloudflare
          // error pages, so an unguarded put would store one and later serve it
          // as the offline fallback. Network-first turns the cache over on every
          // navigation, which makes that far likelier than it was under
          // stale-while-revalidate. Mirrors the image branch's status === 200
          // guard. Bad responses still pass through to the app unchanged.
          if (response.ok) {
            var clone = response.clone();
            caches.open(CACHE_NAME).then(function(cache) { cache.put(event.request, clone); });
          }
          return response;
        }
      ).catch(function() {
        // Offline: exact page if we have it, else the cached landing shell.
        return caches.match(event.request).then(function(cached) {
          return cached || caches.match('/');
        });
      })
    );
    return;
  }
  // Network-first for static assets, fall back to cache
  if (event.request.url.includes('/static/')) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
  }
});

// ── Push Notifications ─────────────────────────────────────────────────────
self.addEventListener('push', event => {
  let data = { title: 'GrailSweep Alert', body: 'A price alert has triggered.', url: '/collection' };
  try { if (event.data) data = { ...data, ...event.data.json() }; } catch(e) {}

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body:  data.body,
      icon:  '/static/assets/grailsweep_app_icon.png',
      badge: '/static/assets/grailsweep_app_icon.png',
      data:  { url: data.url },
      vibrate: [200, 100, 200],
      tag:   'grailsweep-alert',
      renotify: true
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/collection';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (const client of windowClients) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.focus();
          client.navigate(url);
          return;
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});