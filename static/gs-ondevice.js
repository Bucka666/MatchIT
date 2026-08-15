// gs-ondevice.js — on-device card matching + OCR-first lookup
// ============================================================
// Extracted verbatim from templates/match.html on 2026-08-09 (was inline,
// lines 3895-4351 for the window.GSOnDevice IIFE, lines 4951-5068 for
// _detectGame/_tryMlKitLocal/_tryOcrLookup — see git history for the exact
// prior location). Byte-identical move, not a rewrite: loaded from both
// match.html and templates/authenticity.html via <script src>, so the two
// pages share one on-device gate instead of match.html's copy drifting
// from a second, forked one on the authenticity page.
//
// Two external references cross the file boundary, both safe because
// they're only touched at call time (long after every <script> tag on the
// page has parsed), not at definition time — script tags share one global
// window scope regardless of file:
//   - API_BASE       (declared in match.html AND authenticity.html, both
//                      as a plain top-level `var`)
//   - window.gsDeviceWeak()  (declared in base.html's <head>, already
//                      window-qualified at the call site)
//
// gsGameForStaleness is exported as window.GSOnDevice.gameForStaleness
// (2026-08-09 fix) — match.html's scCapture() used to call it as a bare,
// non-namespaced reference, which threw a ReferenceError (a plain function
// declared inside this IIFE is not reachable from outside it), silently
// swallowed by scCapture()'s enclosing try/catch and falling straight
// through to the server /match call every time. The staleness gate itself
// never ran, so on-device accepts never actually completed via this path.

(function () {
  'use strict';

  // ---- config -------------------------------------------------------------
  const GS_ONDEVICE_BASE     = 'https://models.grailsweep.com/gs-ondevice-v1/';
  const GS_EXPECTED_MODEL_SHA = '43A9CF56DCA2441626D42DC494ECEA7D22667FEDE6166A27EAF0B39E87BA24F5'; // uppercase hex

  const GS_ORT_SRC      = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/ort.webgpu.min.js';
  const GS_ORT_WASM_DIR = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/';

  const GS_IDB_NAME     = 'gs_ondevice_bundle_v1';
  const GS_IDB_STORE    = 'files';
  const GS_IDB_MANIFEST = '__manifest';
  const GS_BUNDLE_FILES = ['mobileclip2s2_image_fp16.onnx', 'vectors_f16.npy', 'skus.json', 'meta.json'];

  // ---- status badge -------------------------------------------------------
  function gsSetBadge(state) {
    const el = document.getElementById('gsOnDeviceStatus');
    if (!el) return;
    const map = {
      loading: { text: 'On-device: loading',     cls: 'gs-od-loading' },
      ready:   { text: 'On-device: ready',        cls: 'gs-od-ready'   },
      error:   { text: 'On-device: server mode',  cls: 'gs-od-error'   }
    };
    const s = map[state] || map.error;
    el.textContent = s.text;
    el.className = 'gs-od-badge ' + s.cls;
    el.style.display = '';
  }

  // ---- ORT loader (load library only; no session in Stage 1) --------------
  let _gsOrtP = null;
  function gsLoadOrt() {
    if (window.ort) return Promise.resolve(window.ort);
    if (_gsOrtP) return _gsOrtP;
    _gsOrtP = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = GS_ORT_SRC;
      s.async = true;
      s.onload = () => {
        if (!window.ort) { reject(new Error('ort global missing after load')); return; }
        try { window.ort.env.wasm.wasmPaths = GS_ORT_WASM_DIR; } catch (e) { /* non-fatal in Stage 1 */ }
        resolve(window.ort);
      };
      s.onerror = () => reject(new Error('failed to load ORT from ' + GS_ORT_SRC));
      document.head.appendChild(s);
    });
    return _gsOrtP;
  }

  // ---- IndexedDB ----------------------------------------------------------
  function gsIdbOpen() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error('indexedDB unavailable')); return; }
      const req = indexedDB.open(GS_IDB_NAME, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(GS_IDB_STORE)) db.createObjectStore(GS_IDB_STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror   = () => reject(req.error || new Error('IDB open failed (storage blocked?)'));
    });
  }
  async function gsIdbGet(key) {
    const db = await gsIdbOpen();
    return new Promise((resolve, reject) => {
      const r = db.transaction(GS_IDB_STORE, 'readonly').objectStore(GS_IDB_STORE).get(key);
      r.onsuccess = () => resolve(r.result === undefined ? null : r.result);
      r.onerror   = () => reject(r.error || new Error('IDB get failed'));
    });
  }
  async function gsIdbPut(key, val) {
    const db = await gsIdbOpen();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(GS_IDB_STORE, 'readwrite');
      tx.objectStore(GS_IDB_STORE).put(val, key);
      tx.oncomplete = () => resolve();
      tx.onerror    = () => reject(tx.error || new Error('IDB put failed (storage blocked?)'));
    });
  }
  async function gsAllFilesPresent() {
    for (const f of GS_BUNDLE_FILES) {
      const v = await gsIdbGet(f).catch(() => null);
      if (v == null) return false;
    }
    return true;
  }

  // ---- integrity ----------------------------------------------------------
  async function gsSha256Hex(arrayBuffer) {
    if (!window.crypto || !window.crypto.subtle) { throw new Error('crypto.subtle unavailable (insecure context or unsupported browser)'); }
    const digest = await crypto.subtle.digest('SHA-256', arrayBuffer);
    return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('').toUpperCase();
  }

  // ---- meta (authoritative version source in Stage 1) ---------------------
  async function gsFetchMetaSafe() {
    try {
      const r = await fetch(GS_ONDEVICE_BASE + 'meta.json', { cache: 'no-cache' });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  }

  // ---- ensure bundle (cache-hit fast path, else download + verify) --------
  async function gsEnsureBundle(meta) {
    let manifest = null;
    try { manifest = await gsIdbGet(GS_IDB_MANIFEST); } catch (e) { manifest = null; }

    const cacheValid =
      manifest &&
      manifest.model_sha === GS_EXPECTED_MODEL_SHA &&
      (!meta || manifest.index_version === meta.index_version) &&
      await gsAllFilesPresent();
    if (cacheValid) return true;

    if (!meta) return false; // can't refresh and no usable cache -> server mode

    // model: need bytes to SHA-verify before trusting/caching
    const mResp = await fetch(GS_ONDEVICE_BASE + 'mobileclip2s2_image_fp16.onnx');
    if (!mResp.ok) throw new Error('model fetch ' + mResp.status);
    const mBuf = await mResp.arrayBuffer();
    const mSha = await gsSha256Hex(mBuf);
    if (mSha !== GS_EXPECTED_MODEL_SHA) throw new Error('model SHA mismatch: ' + mSha);
    await gsIdbPut('mobileclip2s2_image_fp16.onnx', new Blob([mBuf]));

    // vectors: large -> store as Blob (no SHA), keep memory light
    const vResp = await fetch(GS_ONDEVICE_BASE + 'vectors_f16.npy');
    if (!vResp.ok) throw new Error('vectors fetch ' + vResp.status);
    await gsIdbPut('vectors_f16.npy', await vResp.blob());

    const sResp = await fetch(GS_ONDEVICE_BASE + 'skus.json');
    if (!sResp.ok) throw new Error('skus fetch ' + sResp.status);
    await gsIdbPut('skus.json', await sResp.text());

    await gsIdbPut('meta.json', JSON.stringify(meta));

    // manifest written LAST -> if any fetch above failed, cache stays invalid and retries cleanly
    await gsIdbPut(GS_IDB_MANIFEST, {
      index_version: meta.index_version,
      preproc_id: meta.preproc_id || null,
      model_sha: GS_EXPECTED_MODEL_SHA,
      files: GS_BUNDLE_FILES,
      cached_at: Date.now()
    });
    return true;
  }

  // --- On-device capability gate -------------------------------------------
  // Low-RAM devices can OOM-kill the tab when the model+index load. Threshold,
  // override key, and the actual weak/strong decision all live in base.html's
  // <head> as window.gsDeviceWeak() (single source of truth, also drives the
  // scanner-panel visibility gate) — this just inverts the polarity for the
  // on-device init/warmup call sites below.
  function gsDeviceCanRunOnDevice() {
    try {
      return !window.gsDeviceWeak();
    } catch (e) {
      return true; // never let the gate itself break init
    }
  }

  // ---- init (background; runs on window load, after render) ---------------
  async function gsOnDeviceInit() {
    if (!gsDeviceCanRunOnDevice()) {
      window.__gsOnDeviceReady = false;
      try { gsSetBadge('error'); } catch (e) {}
      console.log('[ONDEVICE-GATE] on-device disabled for this device; server scan will be used.');
      return;
    }
    if (window.__gsOnDeviceInitStarted) return;
    window.__gsOnDeviceInitStarted = true;
    try {
      gsSetBadge('loading');
      await gsLoadOrt();
      const meta = await gsFetchMetaSafe();
      const ok = await gsEnsureBundle(meta);
      if (!ok) { gsSetBadge('error'); return; }
      window.__gsOnDeviceReady = true;
      gsSetBadge('ready');
      console.log('[gsOnDevice] ready (Stage 1: ORT loaded + bundle cached)');
      try { window.GSOnDevice.warmup().catch(function(){}); } catch (e) {}
    } catch (e) {
      console.warn('[gsOnDevice] init failed -> server mode:', e);
      var _emsg = (e && e.message) ? e.message : String(e);
      var _ename = (e && e.name) ? e.name : 'none';
      // IDB storage failures (common on iOS private mode / restricted WebViews) are
      // tracked separately from genuine model/vector load errors. Both still fall to
      // server mode — this only splits the telemetry event for observability.
      var _isIdb = /IDB |indexedDB/.test(_emsg) || /(QuotaExceeded|Unknown|InvalidState|Security|Version|Abort|Constraint)Error/.test(_ename);
      if (_isIdb) window._gsIdbUnavailable = true;
      try { fetch('/api/ondevice/telemetry', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ event: _isIdb ? 'init_idb_blocked' : 'init_error', error: 'name=' + _ename + ' msg=' + _emsg + ' stack=' + (e && e.stack ? e.stack.split('\n').slice(0,2).join(' >> ') : 'none') }) }); } catch(_) {}
      gsSetBadge('error');
    }
  }

  // ===================== STAGE 2: on-device matching =====================
  const GS_MODEL_FILE = 'mobileclip2s2_image_fp16.onnx';
  const GS_VEC_FILE   = 'vectors_f16.npy';
  const GS_SKU_FILE   = 'skus.json';

  let _gsSession  = null;
  let _gsIndex    = null;
  let _gsIndexN   = 0;
  let _gsDim      = 512;
  let _gsSkus     = null;
  let _gsWarmupP  = null;
  let _gsWarm     = false;
  let _gsServerSets   = null;  // imaged set list from server
  let _gsIndexSetIds  = null;  // set IDs derived from _gsSkus
  let _gsStaleGames   = null;  // Set of game keys whose index coverage is stale

  // ---- PORT FROM HARNESS (verbatim) ----------------------------------------
  const SIZE = 256;

  function loadImage(blob){
    return new Promise((resolve, reject)=>{
      const url = URL.createObjectURL(blob);
      const im = new Image();
      im.onload = ()=>{ resolve({im, url}); };
      im.onerror = (e)=>{ URL.revokeObjectURL(url); reject(e); };
      im.src = url;
    });
  }

  function preprocess(img){
    const w = img.naturalWidth, h = img.naturalHeight;
    const scale = SIZE / Math.min(w, h);
    const tw = Math.round(w * scale), th = Math.round(h * scale);

    let cur = document.createElement("canvas"); cur.width = w; cur.height = h;
    let cx = cur.getContext("2d"); cx.drawImage(img, 0, 0);
    let curW = w, curH = h;
    while (curW > tw*2 && curH > th*2){
      const nw = Math.max(tw, Math.floor(curW/2)), nh = Math.max(th, Math.floor(curH/2));
      const nx = document.createElement("canvas"); nx.width = nw; nx.height = nh;
      const nc = nx.getContext("2d"); nc.imageSmoothingEnabled = true; nc.imageSmoothingQuality = "high";
      nc.drawImage(cur, 0, 0, nw, nh);
      cur = nx; cx = nc; curW = nw; curH = nh;
    }
    const rz = document.createElement("canvas"); rz.width = tw; rz.height = th;
    const rc = rz.getContext("2d"); rc.imageSmoothingEnabled = true; rc.imageSmoothingQuality = "high";
    rc.drawImage(cur, 0, 0, tw, th);

    const sx = Math.floor((tw - SIZE)/2), sy = Math.floor((th - SIZE)/2);
    const fin = document.createElement("canvas"); fin.width = SIZE; fin.height = SIZE;
    const fc = fin.getContext("2d");
    fc.drawImage(rz, sx, sy, SIZE, SIZE, 0, 0, SIZE, SIZE);

    const data = fc.getImageData(0,0,SIZE,SIZE).data;
    const chw = new Float32Array(3*SIZE*SIZE);
    const plane = SIZE*SIZE;
    for (let i=0;i<plane;i++){
      chw[i]           = data[i*4]   / 255;
      chw[plane+i]     = data[i*4+1] / 255;
      chw[2*plane+i]   = data[i*4+2] / 255;
    }
    return new ort.Tensor("float32", chw, [1,3,SIZE,SIZE]);
  }

  function l2norm(a){
    let s=0; for (let i=0;i<a.length;i++) s+=a[i]*a[i];
    s=Math.sqrt(s); const o=new Float32Array(a.length);
    for (let i=0;i<a.length;i++) o[i]=a[i]/s; return o;
  }

  // ---- npy float16 parser + top-2 search -----------------------------------
  function gsF16toF32(h) {
    const s = (h & 0x8000) >> 15, e = (h & 0x7C00) >> 10, f = h & 0x03FF;
    if (e === 0)    return (s ? -1 : 1) * Math.pow(2, -14) * (f / 1024);
    if (e === 0x1F) return f ? NaN : (s ? -1 : 1) * Infinity;
    return (s ? -1 : 1) * Math.pow(2, e - 15) * (1 + f / 1024);
  }
  function gsParseNpyF16(buf) {
    const dv = new DataView(buf);
    if (dv.getUint8(0) !== 0x93) throw new Error('not a .npy file');
    const major = dv.getUint8(6);
    let headerLen, headerStart;
    if (major === 1) { headerLen = dv.getUint16(8, true);  headerStart = 10; }
    else             { headerLen = dv.getUint32(8, true);  headerStart = 12; }
    const header = new TextDecoder('latin1').decode(new Uint8Array(buf, headerStart, headerLen));
    const descr = (header.match(/'descr':\s*'([^']+)'/) || [])[1] || '';
    if (!/f2$/.test(descr)) throw new Error('expected float16 npy, got ' + descr);
    const dims = (header.match(/'shape':\s*\(([^)]*)\)/)[1])
                   .split(',').map(s => s.trim()).filter(Boolean).map(Number);
    const dataStart = headerStart + headerLen;
    const n = dims.reduce((a, b) => a * b, 1);
    const u16 = new Uint16Array(buf, dataStart, n);
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) out[i] = gsF16toF32(u16[i]);
    return { data: out, shape: dims };
  }
  function gsSearchTop2(q) {
    const dim = _gsDim, N = _gsIndexN, idx = _gsIndex;
    let i1 = -1, s1 = -Infinity, i2 = -1, s2 = -Infinity;
    for (let r = 0; r < N; r++) {
      const base = r * dim; let dot = 0;
      for (let d = 0; d < dim; d++) dot += q[d] * idx[base + d];
      if (dot > s1)      { s2 = s1; i2 = i1; s1 = dot; i1 = r; }
      else if (dot > s2) { s2 = dot; i2 = r; }
    }
    return { i1, s1, i2, s2 };
  }

  // ---- lazy loaders (sourced from Stage 1 IDB cache) -----------------------
  async function gsBufFromIdb(name) {
    const v = await gsIdbGet(name);
    if (v == null) throw new Error('not cached: ' + name);
    if (v instanceof Blob) return await v.arrayBuffer();
    if (typeof v === 'string') return v;
    return v;
  }
  async function gsLoadIndex() {
    if (_gsIndex) return;
    const parsed = gsParseNpyF16(await gsBufFromIdb(GS_VEC_FILE));
    _gsIndex = parsed.data; _gsIndexN = parsed.shape[0]; _gsDim = parsed.shape[1];
    const sjson = await gsBufFromIdb(GS_SKU_FILE);
    _gsSkus = JSON.parse(typeof sjson === 'string' ? sjson : new TextDecoder().decode(new Uint8Array(sjson)));
    _gsIndexSetIds = gsBuildIndexSetIds(_gsSkus);
    try {
      const r = await fetch(API_BASE.replace('/api/v1','') + '/api/imaged-sets');
      if (r.ok) {
        _gsServerSets = await r.json();
        _gsStaleGames = gsComputeStaleGames(_gsServerSets, _gsIndexSetIds);
      }
    } catch(e) {
      // fetch failed — _gsStaleGames stays null, staleness check skipped safely
    }
  }
  async function gsLoadSession() {
    if (_gsSession) return;
    await gsLoadOrt();
    const bytes = new Uint8Array(await gsBufFromIdb(GS_MODEL_FILE));
    if (navigator.gpu) {
      try {
        _gsSession = await window.ort.InferenceSession.create(bytes, { executionProviders: ['webgpu'] });
        return;
      } catch (e) { console.warn('[gsOnDevice] webgpu failed, falling back to wasm:', e); }
    }
    _gsSession = await window.ort.InferenceSession.create(bytes, { executionProviders: ['wasm'] });
  }
  function gsWarmup() {
    if (!gsDeviceCanRunOnDevice()) return;
    if (!_gsWarmupP) _gsWarmupP = (async () => { await gsLoadSession(); await gsLoadIndex(); _gsWarm = true; })();
    return _gsWarmupP;
  }

  // ---- game from sku -------------------------------------------------------
  function gsGameFromSku(sku) {
    if (!sku) return 'POKEMON';
    if (sku.startsWith('mtg-')) return 'MTG';
    if (sku.startsWith('ygo-')) return 'YGO';
    return 'POKEMON';
  }

  // ---- staleness check helpers ---------------------------------------------
  function gsExtractSetId(sku) {
    // Mirrors server-side _get_set_id_from_sku logic exactly
    const base = sku.split('-').slice(0, -1).join('-');
    if (base.startsWith('mtg-')) return base.slice(4);
    return base; // jpn-sv7a or sv8pt5 — correct format for each
  }

  function gsGameForStaleness(sku) {
    if (sku.startsWith('jpn-')) return 'POKEMON_JP';
    if (sku.startsWith('mtg-')) return 'MTG';
    return 'POKEMON_EN';
  }

  function gsBuildIndexSetIds(skus) {
    const ids = new Set();
    for (const sku of skus) ids.add(gsExtractSetId(sku));
    return ids;
  }

  function gsComputeStaleGames(serverSets, indexSetIds) {
    const stale = new Set();
    const checks = [
      ['pokemon_en', 'POKEMON_EN'],
      ['pokemon_jp', 'POKEMON_JP'],
      ['mtg', 'MTG']
    ];
    for (const [key, label] of checks) {
      for (const s of (serverSets[key] || [])) {
        if (!indexSetIds.has(s)) { stale.add(label); break; }
      }
    }
    return stale;
  }

  // ---- gate predicate (calibrated Jul 2026 via gate_diag telemetry;
  // reverted gap to 0.02 after equivalence_harness/gate_validate showed
  // 0.005 let through confidently-wrong high-sim confusable pairs — see
  // web_spike/gate_validate.py runs) --------------------------------------
  function gsGateAccept(r) {
    const _accept = r.game !== 'YGO' && r.gap >= 0.02 && r.top1_sim >= 0.80;
    return _accept;
  }

  // ---- public: match a Blob/File ------------------------------------------
  async function gsOnDeviceMatch(blob) {
    await gsWarmup();
    const {im, url} = await loadImage(blob);
    const tensor = preprocess(im);
    URL.revokeObjectURL(url);
    const feeds = {}; feeds['image'] = tensor;
    const out = await _gsSession.run(feeds);
    let emb = out['embedding'].data;
    emb = l2norm(emb);
    const { i1, s1, i2, s2 } = gsSearchTop2(emb);
    const sku  = _gsSkus[i1];
    const game = gsGameFromSku(sku);
    const r = { sku, top1_sim: s1, gap: s1 - s2, game };
    r.accept = gsGateAccept(r);
    return r;
  }

  // ---- telemetry beacon (fire-and-forget; identity resolved server-side) ----
  function gsBeacon(result) {
    try {
      fetch('/api/ondevice/telemetry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        keepalive: true,
        body: JSON.stringify({
          event: 'scan',
          gate_decision: 'accept',
          sku: result.sku,
          game: result.game,
          top1_sim: result.top1_sim,
          gap: result.gap,
          scan_cycle_id: result.scan_cycle_id
        })
      }).catch(function(){});
    } catch (e) { /* never block the scan */ }
  }

  // expose a namespace for Stage 2/3
  window.GSOnDevice = {
    init: gsOnDeviceInit,
    loadOrt: gsLoadOrt,
    idbGet: gsIdbGet,
    isReady: () => !!window.__gsOnDeviceReady,
    BASE: GS_ONDEVICE_BASE,
    FILES: GS_BUNDLE_FILES,
    EXPECTED_MODEL_SHA: GS_EXPECTED_MODEL_SHA,
    match:       gsOnDeviceMatch,
    warmup:      gsWarmup,
    gate:        gsGateAccept,
    gameFromSku: gsGameFromSku,
    gameForStaleness: gsGameForStaleness,
    isWarm:      () => _gsWarm,
    staleGames:  () => _gsStaleGames,
    beacon:      gsBeacon,
  };

  window.addEventListener('load', gsOnDeviceInit);
})();

function _detectGame() {
  var el = document.querySelector('[data-game].active, .game-tab.active, .mi-game-tab.active');
  if (el) {
    var g = (el.dataset.game || el.textContent || '').toLowerCase();
    if (g.includes('pok')) return 'pokemon';
    if (g.includes('mtg') || g.includes('magic')) return 'mtg';
    if (g.includes('ygo') || g.includes('yugi')) return 'ygo';
  }
  return 'pokemon'; // safe default
}

async function _tryMlKitLocal(imageBlob) {
  var _t0 = Date.now();

  // ── iOS Capacitor path ───────────────────────────────────────
  // http://localhost:47291 is blocked by ATS from a WKWebView
  // loading https://grailsweep.com, so on iOS we call the native
  // Vision plugin via the Capacitor bridge instead.
  if (window.Capacitor &&
      typeof window.Capacitor.isNativePlatform === 'function' &&
      window.Capacitor.isNativePlatform() &&
      window.Capacitor.getPlatform() === 'ios') {
    try {
      var plugin = window.Capacitor.Plugins && window.Capacitor.Plugins.GSOcrPlugin;
      if (!plugin) throw new Error('GSOcrPlugin not registered');
      var base64 = await new Promise(function(resolve, reject) {
        var r = new FileReader();
        r.onload  = function() { resolve(r.result.split(',')[1]); };
        r.onerror = reject;
        r.readAsDataURL(imageBlob);
      });
      var result = await plugin.scan({ imageBase64: base64 });
      console.log('[MLKIT-CLIENT] ios after ' + (Date.now()-_t0) + 'ms, text=' + (result.text || '(empty)'));
      return result.text || null;
    } catch(e) {
      console.log('[MLKIT-CLIENT] ios failed after ' + (Date.now()-_t0) + 'ms: ' + (e && e.message ? e.message : e));
      return null;
    }
  }

  // ── Android / desktop path (unchanged) ──────────────────────
  try {
    const resp = await Promise.race([
      fetch('http://localhost:47291/scan', { method:'POST', body: imageBlob, headers:{} }),
      new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 12000))
    ]);
    if (!resp.ok) {
      console.log('[MLKIT-CLIENT] http ' + resp.status + ' after ' + (Date.now()-_t0) + 'ms');
      return null;
    }
    const data = await resp.json();
    console.log('[MLKIT-CLIENT] ok after ' + (Date.now()-_t0) + 'ms, text=' + (data.text || '(empty)'));
    return data.text || null;
  } catch(e) {
    console.log('[MLKIT-CLIENT] failed after ' + (Date.now()-_t0) + 'ms: ' + (e && e.message ? e.message : e));
    return null;
  }
}

async function _tryOcrLookup(imageFile) {
  try {
    var game = _detectGame();
    var ocrSource = 'unknown';
    var rawText = await _tryMlKitLocal(imageFile);
    if (rawText) {
      ocrSource = 'mlkit';
    }
    if (!rawText || !rawText.trim()) return null;
    var text   = rawText;

    var resp = await fetch('/api/ocr-lookup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        raw_text: text,
        game: game,
        lang: _ocrLang,
        ocr_source: ocrSource
      })
    });
    var data = await resp.json();
    if (data.status === 'ok') {
      console.log('[OCR] Lookup hit:', data.sku);
      return data;
    }
    console.log('[OCR] Lookup miss');
    // Miss can still carry a parsed denominator (e.g. out-of-index card whose
    // NNN/TTT was read but didn't resolve to a known SKU) — surface it as a
    // distinct shape (`miss: true`) so callers never mistake it for a hit.
    return (data && data.ocr_denom) ? { miss: true, ocr_denom: data.ocr_denom } : null;
  } catch(e) {
    console.warn('[OCR] Error:', e);
    return null;
  }
}
