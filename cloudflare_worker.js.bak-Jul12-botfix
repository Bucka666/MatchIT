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

// Probe paths — synth 404, never hit Modal
const PROBE_PATTERNS = [
  /\.php($|\?|\/)/i,    // any .php file — grailsweep.com is pure Python, no .php exists
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
];

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const host = url.hostname;
    const path = url.pathname;

    // Route 3 lightweight, no-GPU routes to the CPU twin (serve_light).
    // Placed first so it covers POST telemetry + GET card-profile/image in one
    // spot, before the method-based early-exit splits them apart.
    if (
      path === '/api/ondevice/telemetry' ||
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

    // 4. Default — pass through to Modal (with proxy secret)
    return proxyToModal(request, url);
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
