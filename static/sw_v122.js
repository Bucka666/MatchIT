// GrailSweep Service Worker — enables PWA install + basic caching + push notifications
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
const CACHE_NAME = 'grailsweep-v121';
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
  // Stale-while-revalidate for navigation requests — serve cached landing instantly,
  // fetch fresh from Modal in background so cold start doesn't show a blank screen
  if (event.request.mode === 'navigate') {
    event.respondWith(
      caches.match(event.request).then(function(cached) {
        var networkFetch = fetch(event.request).then(
          function(response) {
            var clone = response.clone();
            caches.open(CACHE_NAME).then(function(cache) { cache.put(event.request, clone); });
            return response;
          }
        );
        return cached || networkFetch;
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