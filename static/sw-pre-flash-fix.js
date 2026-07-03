// GrailSweep Service Worker — enables PWA install + basic caching + push notifications
const CACHE_NAME = 'grailsweep-v69';
const PRECACHE = [
  '/',
  '/static/style.css',
  '/static/assets/grailsweep_app_icon.png'
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
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
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