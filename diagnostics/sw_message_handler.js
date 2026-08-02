// TEMPORARY DIAGNOSTIC (added v124) — lets a page ask the ACTIVE controller
// which CACHE_NAME it is running, so we can see whether an old SW is still
// controlling the document. If no reply arrives, the controller predates v124.
// Remove together with the #gsDiagPill block in contact.html before resubmit.
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'GS_VERSION' && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ cache: CACHE_NAME });
  }
});
