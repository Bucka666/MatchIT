
CLAUDE_HANDOVER_2026-04-21-EVENING.md
Session date: 2026-04-21 (evening session)
Last deploy: 2026-04-21 ~20:30 UTC
Scanner status: WORKING
Sound status: FIXED — working on Chrome, Safari, PWA for both scan and photo match

✅ COMPLETED TODAY
Critical Fixes

Sound on photo match — root cause found: _getAudioCtx was scoped inside DOMContentLoaded making it invisible to _playCardSound. Moved to module scope in results.html. Now works on all devices/browsers.
iOS sound guards — IntersectionObserver/setTimeout sound triggers now skip on iOS, relying solely on touchstart gesture path which is the only reliable iOS audio trigger.
hasInstantAnswer scope fix — variable declared inside if (topCard) block but referenced outside. Now declared at outer scope.
classList null safety — detailOverlay, setupOverlay, cfgSound elements now null-checked before .classList access in match.html.
DEBUG FILTERS removed — two debug print statements removed from app.py, were logging on every scan request.

Performance & Cost

Cloudflare cache rules — two rules created: /api/v1/image/ and /img/db/ with 1-year edge + browser TTL. Eliminates image requests reaching Modal after first cache population.
Collection URL migration — one-time migration rewrites old modal.run absolute URLs to grailsweep.com in localStorage on collection page load.
scaledown_window=60 — reduced from 180s, containers shut down faster when idle
max_inputs=3 — reduced from 5, fewer concurrent threads per container
Numpy cache — force=False on warmup, saves ~2s on cold start vs SQLite
Image cache headers — max-age=31536000, immutable on /api/v1/image/ responses
RAS images removed from cards vertical image serving

UX & Features

Set filter dropdown — replaced pills with dropdown on collection page, handles 100+ Pokémon sets cleanly
PWA code prompt banner — Pro users with no matchit_access_code_v1 see purple banner prompting code entry on collection page load. Validates, syncs collection, registers push.
+ quick add button — size reduced, emoji replaced with + text, no more iOS flash
Apple touch icon — /apple-touch-icon.png and /apple-touch-icon-precomposed.png routes added, no more 404s
FX rates proxy — /api/fx_rates proxies frankfurter.app, fixes CORS errors
Beta banner — contact link fixed to /contact, cold start warning added
Sort dropdown — native <select> replaced with custom dark-themed dropdown
sw.js at root — /sw.js registered at root scope, push notifications work in PWA
VAPID key fix — cfg context processor added, key now reaches Jinja templates correctly
Push enable button — manual 🔔 button on collection page, user-initiated permission request
Collection sync — server-side sync confirmed working, 46 cards stored for GRAIL-XAJI-0Y6D
Push subscriptions — confirmed working, 2 devices subscribed for GRAIL-XAJI-0Y6D


❌ STILL OUTSTANDING

Cloudflare cache propagation — rules created today, need 24hrs to fully populate. Check cf-cache-status: HIT on collection images tomorrow.
Multiple containers still spawning — collection page still triggers 2-3 containers on first load. Cloudflare cache should fix this once propagated.
One container loading SQLite instead of numpy — occasional cold start loads from SQLite (7.5s) instead of numpy cache (5.3s). Timing issue with volume sync on new containers.
GitHub/Codespaces setup — deferred, needed for remote deploy from mobile. Steps documented in earlier session.
Weekly scheduler first logged run — Monday 28th April 1am UTC will be first logged run. Check scheduler_log.json on volume after that.
Sound on PWA — confirmed working after today's fix, but worth retesting after PWA cache clears naturally.


🔑 KEY FACTS

Craig's collection code: GRAIL-XAJI-0Y6D — 46 cards, 2 push devices
Modal workspace: c-a-buckley, app: matchit-api
Volume: matchit-data-v2
T4 rate: $0.59/hr actual
Cold start: ~25s (PaddleOCR 7s + CLIP 11s + numpy cache 5s)
Warm match: 2.2-2.5s typical
Cloudflare: Free plan, 2 cache rules active for images
Deploy command: PYTHONIOENCODING=utf-8 modal deploy matchit_modal.py


📋 TOMORROW'S PRIORITIES

Verify Cloudflare cache — check cf-cache-status: HIT on collection images in Chrome DevTools
Monitor Modal costs — should drop significantly with Cloudflare caching images
GitHub repo setup — push code to private repo for remote Codespaces access
Test push notification end-to-end — send a test push to verify full delivery chain
Fix match.html errors — hasInstantAnswer and classList null errors still showing in console (fixed in results.html but check match.html too)