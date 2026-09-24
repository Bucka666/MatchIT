// Repo reference copy — mirrors deployed raredex-proxy (CPU-twin routing live since 2026-06-21)
const MODAL_HOST = 'c-a-buckley--matchit-api-serve.modal.run';
const LIGHT_HOST = 'c-a-buckley--matchit-api-serve-light.modal.run';

// Shared secret for origin protection — must match Modal CF_PROXY_SECRET env var
// Real value lives only in the deployed Worker + Modal CF_PROXY_SECRET secret — never commit the live value here
const CF_PROXY_SECRET = 'REDACTED_SEE_DEPLOYED_WORKER';

// Hosts we apply filtering to
const FILTERED_HOSTS = new Set(['grailsweep.com', 'www.grailsweep.com']);

// Static synth responses — never hit Modal
const STATIC_RESPONSES = {
  '/robots.txt': {
    body: 'User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /api/\nSitemap: https://grailsweep.com/sitemap.xml\n',
    contentType: 'text/plain; charset=utf-8',
    cacheSeconds: 86400,
  },
  '/favicon.ico': {
    body: null,
    status: 204,
    cacheSeconds: 86400,
  },
};

// PWA / Store validation — always proxy to Modal, skip probe-block
// Path allowlist: belt-and-suspenders for known validator fetches
const PWA_VALIDATION_PATHS = new Set([
  '/',
  '/match',
  '/static/manifest.json',
  '/sw.js',
  '/privacy',
  '/terms',
  '/contact',
  '/sitemap.xml',
  '/.well-known/assetlinks.json',
]);

const PWA_ASSET_PREFIXES = [
  '/static/assets/icon-',
  '/static/assets/screenshot-',
  '/static/assets/grailsweep_',
];

// UA allowlist: load-bearing — validators crawl arbitrary paths that may match probe patterns
const VALIDATOR_UA_PATTERNS = [
  /PWABuilder/i,
  /Microsoft.*Store/i,
  /Edge.*Store/i,
  /bingbot/i,           // Microsoft uses bingbot for some Store checks
  /Lighthouse/i,        // PWABuilder uses Lighthouse internally
  /Chrome-Lighthouse/i,
];

// Every real route the Flask app (app.py + api_routes.py) registers, plus
// Flask's own implicit /static/<path:filename> handler. Generated 2026-09-24
// by grepping every @app.route(...) decorator in both files — kept as a
// flat safety net so the "unmatched path -> CPU twin" default below (step 4)
// can never silently swallow a real feature route it doesn't recognise.
// A path landing on the CPU twin without matching anything here or in the
// light-routing block above just gets serve_light's cheap 404 instead of
// waking the GPU container to render the same 404 — that's the entire
// point: nonexistent-page requests (bot/scanner guesses like /dpa,
// /subprocessors, /ai-policy, typos, dead links) no longer cost a GPU cold
// start just to return 404. When a new @app.route is added to either file, add it here too — nothing
// breaks if you forget (worst case a brand-new route falls through to the
// GPU default, exactly today's behaviour), but it stops enjoying the
// no-GPU-wakeup protection for its own 404 siblings.
const KNOWN_APP_ROUTES_EXACT = new Set([
  '/', '/.well-known/assetlinks.json', '/admin', '/admin/cancel_code',
  '/admin/create_referral_coupon', '/admin/delete_code', '/admin/feedback',
  '/admin/feedback/clear', '/admin/reembed_all', '/admin/reembed_missing',
  '/admin/refresh_cache', '/admin/run_scheduler', '/admin/run_scheduler_dry',
  '/admin/scans-diagnostic', '/admin/sync_keysdb', '/admin/tier-usage-diagnostic',
  '/api/card-search', '/api/collection/live_prices', '/api/collection/sync',
  '/api/collection/value_history', '/api/create-checkout-session',
  '/api/customer-portal', '/api/deep_grade', '/api/deep_grade_url',
  '/api/delete-alert', '/api/fx_rates', '/api/google-play/rtdn',
  '/api/google-play/verify-purchase', '/api/heartbeat', '/api/imaged-sets',
  '/api/jp-denom-check', '/api/jp-set-coverage', '/api/ocr-lookup',
  '/api/ondevice/telemetry', '/api/pokemon-search', '/api/price_history',
  '/api/price_history/bulk', '/api/push/send', '/api/push/subscribe',
  '/api/redeem-topup', '/api/referral_code', '/api/revenuecat/restore',
  '/api/revenuecat/webhook', '/api/set-alert', '/api/sets/completion',
  '/api/stats', '/api/tier/dismiss-warning', '/api/topup-status',
  '/api/trial/activate', '/api/validate_premium', '/api/watchlist/sync',
  '/app/', '/apple-touch-icon-precomposed.png', '/apple-touch-icon.png',
  '/capture_submit', '/collection', '/contact', '/csv_template',
  '/db_image_review', '/db_manage', '/db_upload', '/delete-account',
  '/favicon.ico', '/feedback', '/get', '/history', '/login', '/login/',
  '/logout', '/marketplace', '/match', '/ocr-test', '/payment-success',
  '/privacy', '/robots.txt', '/search', '/sets', '/sitemap.xml',
  '/sitemap_index.xml', '/static/scanner.html', '/sw.js', '/terms',
  '/upgrade', '/watchlist', '/webhook/stripe', '/xref-search',
  '/api/v1/health', '/api/v1/switch_vertical', '/api/v1/verticals',
  '/api/v1/vertical', '/api/v1/match', '/api/v1/stats',
]);

const KNOWN_APP_ROUTES_PREFIXES = [
  '/api/card-profile/',   // <string:sku>
  '/api/search-index/',   // <game>
  '/api/set-total/',      // <path:sku>
  '/api/sets-list/',      // <game>
  '/api/sets/',           // <set_id>/cards (also covers /api/sets/completion above)
  '/cards/pokemon/',      // <set_slug>/<card_slug>
  '/db_delete/',          // <image_id>
  '/db_flag_image/',      // <image_id>
  '/db_replace_image/',   // <image_id>
  '/db_review/',          // <batch_id>
  '/img/query/',          // <filename>
  '/img/ras/',            // <sku>.jpg
  '/sitemap-',            // <chunk_name>.xml
  '/api/v1/image/',       // <image_id>
  '/api/v1/ras_image/',   // <sku>
  '/static/',             // Flask's implicit static file handler
];

function isKnownAppRoute(path) {
  return KNOWN_APP_ROUTES_EXACT.has(path) || KNOWN_APP_ROUTES_PREFIXES.some(p => path.startsWith(p));
}

// Probe paths — synth 404, never hit Modal
const PROBE_PATTERNS = [
  /\.(php|phtml|asp|aspx|jsp|cgi|pl|sh)($|\?|\/)/i,    // any server-side script — grailsweep.com is pure Python
  /^\/\.env/i,
  /^\/\.git/i,
  /^\/\.aws/i,
  /^\/\.ssh/i,
  /^\/\.docker/i,
  /^\/\.vscode/i,
  /^\/\.idea/i,
  /^\/\.htaccess/i,
  /^\/\.htpasswd/i,
  /^\/wp-/i,
  /^\/wordpress/i,
  /^\/xmlrpc/i,
  /^\/wlwmanifest/i,
  /^\/phpmyadmin/i,
  /^\/pma/i,
  /^\/adminer/i,
  /^\/administrator/i,
  /^\/index\.php/i,
  /^\/config\.php/i,
  /^\/configuration\.php/i,
  /^\/setup\.php/i,
  /^\/install\.php/i,
  /^\/cgi-bin/i,
  /^\/owa\//i,
  /^\/autodiscover/i,
  /^\/exchange\//i,
  /^\/server-status/i,
  /^\/server-info/i,
  /^\/actuator\//i,
  /^\/manager\/html/i,
  /^\/composer\.(json|lock)/i,
  /^\/\.well-known\/apple-app-site-association/i,
  /^\/apple-app-site-association/i,
  /^\/feed\//i,
  /^\/sites\//i,
  /^\/update\//i,
  /^\/admin\//i,
];

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const host = url.hostname;
    const path = url.pathname;

    // Route lightweight, no-GPU routes to the CPU twin (serve_light).
    // Placed first so it covers POST telemetry + GET card-profile/image in one
    // spot, before the method-based early-exit splits them apart.
    //
    // 2026-09-24 cost recon: added /, /privacy, /terms, /contact, /upgrade
    // and the sitemap routes — static/legal pages and the homepage with zero
    // model/GPU dependency (verified against app.py), which recon measured
    // as ~65% of the GPU function's daily request volume, some taking
    // 30-55s wall time for a cold GPU reload to serve a static page.
    if (
      path === '/' ||
      path === '/privacy' ||
      path === '/terms' ||
      path === '/contact' ||
      path === '/upgrade' ||
      path === '/sitemap.xml' ||
      path === '/sitemap_index.xml' ||
      path === '/api/ondevice/telemetry' ||
      path === '/api/pokemon-search' ||
      path === '/search' ||
      path === '/api/price_history/bulk' ||
      path === '/api/heartbeat' ||
      path === '/api/stats' ||
      path.startsWith('/sitemap-') ||
      path.startsWith('/api/card-profile/') ||
      path.startsWith('/api/v1/image/')
    ) {
      return proxyToModal(request, url, LIGHT_HOST);
    }

    // Legacy domain — permanent redirect to grailsweep.com (no Modal hit)
    if (host === 'raredex.co' || host === 'www.raredex.co') {
      const target = 'https://grailsweep.com' + url.pathname + url.search;
      return new Response(null, {
        status: 301,
        headers: {
          'Location': target,
          'Cache-Control': 'public, max-age=31536000',
        },
      });
    }

    // Non-grailsweep hosts (anything else bound to this Worker) — passthrough
    if (!FILTERED_HOSTS.has(host)) {
      return proxyToModal(request, url);
    }

    // Only filter GET / HEAD — POST and others always pass through
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return proxyToModal(request, url);
    }

    // 1. Static synth responses (robots.txt, favicon) — handled before any filtering
    if (STATIC_RESPONSES[path] !== undefined) {
      const r = STATIC_RESPONSES[path];
      const headers = {
        'Cache-Control': `public, max-age=${r.cacheSeconds}`,
        'X-GS-Synth': 'static',
      };
      if (r.contentType) headers['Content-Type'] = r.contentType;
      return new Response(r.body, { status: r.status || 200, headers });
    }

    // 2. PWA / Store validator bypass — skip probe-block, proxy with secret intact
    // NOTE: must use proxyToModal (not fetch) so X-CF-Proxy-Secret is injected
    const ua = request.headers.get('user-agent') || '';
    const isPwaPath =
      PWA_VALIDATION_PATHS.has(path) ||
      PWA_ASSET_PREFIXES.some(prefix => path.startsWith(prefix));
    const isValidatorUA = VALIDATOR_UA_PATTERNS.some(p => p.test(ua));
    if (isPwaPath || isValidatorUA) {
      return proxyToModal(request, url);
    }

    // 3. Probe path block — synth 404
    for (const pattern of PROBE_PATTERNS) {
      if (pattern.test(path)) {
        return new Response('Not Found', {
          status: 404,
          headers: {
            'Content-Type': 'text/plain; charset=utf-8',
            'Cache-Control': 'public, max-age=3600',
            'X-GS-Synth': 'probe-block',
          },
        });
      }
    }

    // 4. Known real app route (not already sent to the CPU twin above) —
    // pass through to the GPU function, unchanged behaviour.
    if (isKnownAppRoute(path)) {
      return proxyToModal(request, url);
    }

    // 5. Doesn't match any route this app actually serves — bot probes,
    // typos, deprecated links. Send to the CPU twin instead of the GPU
    // function: serve_light's own router 404s it just as correctly, without
    // spending a GPU cold start on a response that was always going to be
    // a 404. See KNOWN_APP_ROUTES_* above for what routes to.
    return proxyToModal(request, url, LIGHT_HOST);
  },
};

function proxyToModal(originalRequest, url, targetHost = MODAL_HOST) {
  const modalUrl = new URL(url.toString());
  modalUrl.hostname = targetHost;
  modalUrl.protocol = 'https:';

  const headers = new Headers(originalRequest.headers);
  headers.set('X-Forwarded-Host', originalRequest.headers.get('host') || url.hostname);
  headers.set('X-CF-Proxy-Secret', CF_PROXY_SECRET);

  return fetch(modalUrl.toString(), {
    method: originalRequest.method,
    headers,
    body:
      originalRequest.method !== 'GET' && originalRequest.method !== 'HEAD'
        ? originalRequest.body
        : null,
    redirect: 'manual',
  });
}
