
CLAUDE_HANDOVER_2026-04-21.md
Session date: 2026-04-21
Last deploy: 2026-04-21 ~01:40 UTC
Scanner status: WORKING

✅ COMPLETED TODAY

Session panel overlay — fixed z-index collision, controls bar now position:fixed;bottom:0;z-index:300
Quick grade on scanner — _rule_based_grade now called in api_routes.py and result shown in detail modal
Collection duplicate check — fixed set vs set_name key mismatch across all three templates
Collection missing image — fixed results.html to use /api/v1/image/{uuid} URL format
Price consistency — results.html now uses live FX rate and highest-value logic same as scanner
Magic: TG label — fixed to "Magic: The Gathering"
Top N matches hidden — when only 1 result (OCR direct match), heading hidden
MTG extractor false positives — regex tightened to letters-only, expanded blocklist
Stats persistence — vol.commit() added to _save_stats and _save_price_history
Scan counter — only increments on successful match added to session
Failgate overlay — after 5 failed scan attempts, overlay appears with options
5-tier scanner sounds — _playPing/Chime/GoodFind/BigWin/Jackpot ported from results page
iOS audio unlock — silent buffer + resume on first touch/tap gesture
Mobile layout — switched to dvh units, cx-scanner flex:0 0 48dvh, guide box 78%
Photo match collection entry — fixed image URL, set_name key, price calculation
SEO — meta description, keywords, OG tags, Twitter cards, JSON-LD, canonical, geo tags
OG image — grailsweep_og.png created and deployed to /static/assets/
Page title — expanded to include keywords
Sitemap — verified working, indexing requested in Google Search Console
Cloudflare Worker — fixed redirect:'follow' → redirect:'manual' to fix /admin 404
Server-side collection sync — collections.json on Modal volume, /api/collection/sync GET/POST endpoints
Collection lazy loading — loading="lazy" on collection images, fixes container explosion
Push notifications — VAPID keys generated, sw.js updated, gsInitPush in base.html, /api/push/subscribe endpoint, _send_alert_push in set_scheduler.py
Price alerts — code field added to alert schema, synced from frontend
Sort dropdown — replaced native <select> with custom dark-themed dropdown
Set filter pills — second row of set filters appears when TCG selected
PWA safe area — padding-top: env(safe-area-inset-top) added to .mi-shell
Add to collection button — ⭐ button added directly on scanner session rows
Stop button — hides session panel when pressed
Modal memory snapshot — enable_memory_snapshot=True + enable_gpu_snapshot:True + warmup preload
Concurrent inputs — @modal.concurrent(max_inputs=5)


❌ OUTSTANDING

Push notification button not showing — gsEnablePushBtn never appears. Root cause: VAPID public key likely not reaching Jinja template correctly. config.get("vapid_public_key") in base.html needs verifying — check if vapid_public_key is actually in config.json and being passed to the template context.
Sound on photo match results — DOMContentLoaded unlock doesn't work on iOS. Partial fix in place but unverified.
Modal container cycling — collection page still triggering multiple containers despite lazy loading. Image requests hitting separate cold containers. May need to investigate the image serving path.
Collection code null on PWA — PWA has separate localStorage. User must enter code via Pro tab after each PWA reinstall. Consider auto-prompting for code on first PWA load if matchit_access_code_v1 is null but matchit_premium_v1 is set.
Weekly scheduler central merge — onboard_skus.py --profile not running on automated Monday runs.


🔑 KEY FINDINGS TODAY

VAPID public key stored in config.json as vapid_public_key — verify this key name matches what base.html requests via config.get("vapid_public_key", "")
Collection code GRAIL-XAJI-0Y6D — Craig's main Safari collection, 46 cards, synced to server
Modal T4 actual rate — $0.59/hr (includes 1.25x regional multiplier)
Memory snapshot — captures but doesn't meaningfully reduce cold start (PaddleOCR re-initialises regardless)
Container explosion — caused by collection page loading all images simultaneously, fixed with loading="lazy"


📋 TOMORROW'S PRIORITIES

Fix push notification button — check vapid_public_key in config.json and Flask template context
Test collection sync on PWA — enter code, verify 46 cards restore
Sound on photo match — test DOMContentLoaded fix
Modal container cycling — investigate why image requests still spawn new containers