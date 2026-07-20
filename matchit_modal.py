import modal
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "/app")
from modal_config import VOLUME_NAME, vol, image
from log_config import configure_logging

QUERY_IMAGE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

configure_logging()
print("[LOG-INIT] modal entry: logging configured -> stdout INFO", flush=True)

app = modal.App("matchit-api")


def _sweep_query_dir():
    """Delete scan images from /modal_data/query older than QUERY_IMAGE_TTL_SECONDS."""
    query_dir = "/modal_data/query"
    if not os.path.isdir(query_dir):
        print("[QUERY-SWEEP] startup: query dir missing, skipping", flush=True)
        return 0, 0
    cutoff = time.time() - QUERY_IMAGE_TTL_SECONDS
    deleted_n, deleted_b, errors = 0, 0, 0
    for fname in os.listdir(query_dir):
        fpath = os.path.join(query_dir, fname)
        try:
            st = os.stat(fpath)
            if st.st_mtime < cutoff:
                deleted_b += st.st_size
                os.remove(fpath)
                deleted_n += 1
        except Exception:
            errors += 1
    mb = deleted_b / (1024 * 1024)
    suffix = f" ({errors} errors)" if errors else ""
    print(f"[QUERY-SWEEP] startup deleted {deleted_n} files, freed {mb:.1f} MB{suffix}", flush=True)
    return deleted_n, deleted_b


def _fix_vertical_config():
    import json
    vpath = "/app/verticals/cards/vertical.json"
    with open(vpath, "r") as f:
        vcfg = json.load(f)
    vcfg["db_root"] = "/modal_data/CardsDB"
    with open(vpath, "w") as f:
        json.dump(vcfg, f, indent=2)
    os.makedirs("/modal_data/CardsDB", exist_ok=True)


def _fix_db_paths():
    import sqlite3
    db_path = "/modal_data/MatchITv2_ProductMatch_Data/cards/images.db"
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE images SET path = REPLACE(path, ?, ?)",
            (
                "C:\\Users\\c_a_b\\AppData\\Local\\MatchITv2_ProductMatch_Data\\cards\\image_db\\",
                "/modal_data/MatchITv2_ProductMatch_Data/cards/image_db/",
            ),
        )
        conn.commit()
        conn.close()
        print("Fixed image paths for Modal")
    except Exception as e:
        print("Path fix skipped: " + str(e))


def _make_image_wsgi():
    """Lightweight Flask mini-app that serves images straight from the volume.
    No ML models are imported here — this is intentional so that a cold
    container receiving only image requests never pays the ~30s CLIP/DINOv2
    load penalty."""
    from flask import Flask as _Flask, send_file, abort, make_response

    _img = _Flask("matchit_img_server")

    IMAGE_DB_DIR = "/modal_data/MatchITv2_ProductMatch_Data/cards/image_db"
    QUERY_DIR    = "/modal_data/query"
    os.makedirs(QUERY_DIR, exist_ok=True)
    RAS_DIR      = "/app/ras_images"

    @_img.route("/img/db/<image_id>.jpg")
    def _img_db(image_id):
        path = os.path.join(IMAGE_DB_DIR, image_id + ".jpg")
        if not os.path.exists(path):
            abort(404)
        resp = make_response(send_file(path, mimetype="image/jpeg"))
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @_img.route("/img/query/<filename>")
    def _img_query(filename):
        if ".." in filename or "/" in filename:
            abort(404)
        path = os.path.join(QUERY_DIR, filename)
        if not os.path.exists(path):
            abort(404)
        resp = make_response(send_file(path))
        resp.headers["Cache-Control"] = "no-store, no-cache"
        return resp

    @_img.route("/img/ras/<sku>.jpg")
    def _img_ras(sku):
        for ext in [".jpg", ".png", ".jpeg", ".webp"]:
            p = os.path.join(RAS_DIR, sku + "_RAS" + ext)
            if os.path.exists(p):
                return send_file(p, mimetype="image/jpeg")
        abort(404)

    return _img


@app.function(
    image=image,
    gpu="T4",
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
        modal.Secret.from_name("stripe-credentials"),
        modal.Secret.from_name("google-vision-credentials"),
        modal.Secret.from_name("google-play-credentials"),
        modal.Secret.from_name("vapid-credentials"),
        modal.Secret.from_name("external-api-credentials"),
        modal.Secret.from_name("cf-proxy-secret"),
        modal.Secret.from_name("resend-api-key"),
        modal.Secret.from_name("r2-credentials"),
        # REVENUECAT_WEBHOOK_SECRET — value from the RevenueCat dashboard
        # (Project settings > Webhooks > Authorization header value). Create
        # via: modal secret create revenuecat-webhook-secret REVENUECAT_WEBHOOK_SECRET=<value>
        modal.Secret.from_name("revenuecat-webhook-secret"),
    ],
    timeout=300,
    min_containers=0,
    scaledown_window=600,
    max_containers=2,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
@modal.concurrent(max_inputs=3)
@modal.wsgi_app()
def serve():
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"

    vol.reload()
    # _fix_vertical_config() MUST run before app.py is imported — app.py caches
    # the vertical config (db_root) at import (VERTICAL = load_vertical(...)), so
    # importing first would pin db_root to the unfixed local "C:\\CardsDB".
    _fix_vertical_config()
    import app as _app_module
    _app_module._vol_commit_fn = vol.commit
    _fix_db_paths()

    _swept_n, _ = _sweep_query_dir()
    if _swept_n > 0:
        try:
            vol.commit()
            print("[QUERY-SWEEP] vol.commit() OK", flush=True)
        except Exception as _e:
            print(f"[QUERY-SWEEP] vol.commit() FAILED: {_e}", flush=True)

    # Pre-load models during container warmup so snapshot captures them
    # Skip if already loaded (snapshot restore)
    import sys as _sys
    _already_loaded = 'app' in _sys.modules and hasattr(_sys.modules.get('app'), '_EMBEDDER') and _sys.modules['app']._EMBEDDER is not None

    if not _already_loaded:
        from app import app as _flask_app, get_embedder, load_embedding_cache
        # Trigger CLIP load
        try:
            with _flask_app.app_context():
                get_embedder()
        except Exception as _e:
            print(f"[WARMUP] CLIP load failed: {_e}", flush=True)

        # Trigger embedding cache load — use numpy fast cache if available
        try:
            with _flask_app.app_context():
                load_embedding_cache(force=False)
        except Exception as _e:
            print(f"[WARMUP] Cache load failed: {_e}", flush=True)

        # Trigger DINOv2 tie-break preload in a background daemon thread. The
        # local `python app.py` path did this at app.py's
        # `if __name__ == "__main__":` block, which NEVER runs under Modal
        # (serve() imports app as a module), so on the deployed app DINOv2 used
        # to load lazily on the FIRST near-tie scan and stall it. Backgrounded
        # (non-blocking) to match that original pattern and avoid delaying
        # container startup / the snapshot — DINOv2 is only needed on near-ties,
        # so it just has to be warm before the first real tiebreak scan, not
        # before the container serves its first request. Best-effort snapshot
        # capture: if the thread finishes before Modal snapshots it's baked in,
        # otherwise it simply re-fires on the next start. Idempotent — guarded
        # by _TIEBREAK_LOCK / `if _TIEBREAK_EMBEDDER is None` in app.py.
        try:
            import threading as _threading
            from app import CFG as _CFG, _preload_tiebreak_embedder
            if bool(_CFG.get("dinov2_tiebreak_enabled", True)) and bool(_CFG.get("dinov2_tiebreak_preload", True)):
                print("[WARMUP] Starting DINOv2 tie-break background preload...", flush=True)
                _threading.Thread(target=_preload_tiebreak_embedder, daemon=True).start()
        except Exception as _e:
            print(f"[WARMUP] DINOv2 preload trigger failed: {_e}", flush=True)

        print("[WARMUP] All models loaded — container ready for snapshot", flush=True)
    else:
        print("[WARMUP] Models already loaded from snapshot — skipping warmup", flush=True)
        from app import app as _flask_app

    # Image mini-app — ready immediately, no model loading
    img_wsgi = _make_image_wsgi()

    # Models are pre-loaded above — _get_main_app() just returns the cached app.
    def _get_main_app():
        return _flask_app

    def router(environ, start_response):
        if environ.get("PATH_INFO", "").startswith("/img/"):
            return img_wsgi(environ, start_response)
        return _get_main_app()(environ, start_response)

    return router


# ── CPU twin — light routes only, no GPU, no model load ────────────────────
# Hosts /api/ondevice/telemetry, /api/card-profile/<sku>, /api/v1/image/<id>,
# /api/pokemon-search and /search (text search needs no CLIP/DINOv2)
# on a CPU-only container so they never pay for (or wait behind) the GPU
# container's CLIP/DINOv2 warmup. Same Flask app as serve() (same code, same
# routes registered) but the WSGI router below allowlists only these 3
# prefixes — every other path 404s here instead of falling through to the
# full app, keeping /match and /api/v1/match practically unreachable on this
# function. Routing traffic to this URL for those 3 paths is done at the
# Cloudflare Worker layer (not here) — see matchit_modal.py deploy notes.
_LIGHT_ALLOWED_PREFIXES = (
    "/api/ondevice/telemetry",
    "/api/card-profile/",
    "/api/v1/image/",
    "/api/pokemon-search",
    "/search",
    "/api/price_history/bulk",
)


def _light_404(environ, start_response):
    start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
    return [b"Not Found"]


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
        modal.Secret.from_name("stripe-credentials"),
        modal.Secret.from_name("google-vision-credentials"),
        modal.Secret.from_name("vapid-credentials"),
        modal.Secret.from_name("external-api-credentials"),
        modal.Secret.from_name("cf-proxy-secret"),
        modal.Secret.from_name("resend-api-key"),
        modal.Secret.from_name("r2-credentials"),
    ],
    timeout=300,
    min_containers=0,
    scaledown_window=600,
    max_containers=2,
    enable_memory_snapshot=True,
)
@modal.concurrent(max_inputs=3)
@modal.wsgi_app()
def serve_light():
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"

    vol.reload()
    # _fix_vertical_config() MUST run before app.py is imported — app.py caches
    # the vertical config (db_root) at import (VERTICAL = load_vertical(...)), so
    # importing first would pin db_root to the unfixed local "C:\\CardsDB".
    _fix_vertical_config()
    import app as _app_module
    _app_module._vol_commit_fn = vol.commit
    _fix_db_paths()

    _swept_n, _ = _sweep_query_dir()
    if _swept_n > 0:
        try:
            vol.commit()
            print("[QUERY-SWEEP] vol.commit() OK", flush=True)
        except Exception as _e:
            print(f"[QUERY-SWEEP] vol.commit() FAILED: {_e}", flush=True)

    # Deliberately NO get_embedder() / load_embedding_cache() call here — that's
    # the entire point of this function. The 3 light routes never touch
    # FRONT_INFO/CLIP/DINOv2 (confirmed in recon: _image_id_for_sku falls back
    # to a direct sqlite lookup when FRONT_INFO is empty).
    from app import app as _flask_app
    print("[LIGHT] serve_light booted — no model load, no embedding cache", flush=True)

    def router(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if any(path.startswith(p) for p in _LIGHT_ALLOWED_PREFIXES):
            return _flask_app(environ, start_response)
        return _light_404(environ, start_response)

    return router


@app.local_entrypoint()
def warm():
    """Post-deploy warm-up. Run IMMEDIATELY after `modal deploy`:

        modal run matchit_modal.py::warm

    Every deploy changes the /app image layer (add_local_dir), which
    invalidates the serve() memory/GPU snapshot, so the first request after
    a deploy pays the full cold start (CLIP + embedding cache load, ~11s+).
    This hits the DEPLOYED serve() web endpoint once so that cost lands on
    us here, not on the next real user.

    Targets the deployed app via Function.from_name (NOT the module-level
    `serve`, which under `modal run` would point at a throwaway ephemeral
    container). Hits serve's own *.modal.run URL directly, bypassing the
    Cloudflare Worker, so it always warms the GPU serve function — never the
    CPU serve_light twin. Runs locally (stdlib urllib only, no container).
    """
    import time, urllib.request
    fn = modal.Function.from_name("matchit-api", "serve")
    url = fn.get_web_url() if hasattr(fn, "get_web_url") else fn.web_url
    print(f"[WARM] hitting deployed serve at {url} ...", flush=True)
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GrailSweep-warmup/1.0"})
        with urllib.request.urlopen(req, timeout=240) as resp:
            code = resp.status
        print(f"[WARM] {url} -> {code} in {time.time() - t0:.1f}s (container now warm)", flush=True)
    except Exception as e:
        print(f"[WARM] request failed after {time.time() - t0:.1f}s: {e}", flush=True)


@app.function(
    image=image,
    gpu="T4",
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
        modal.Secret.from_name("stripe-credentials"),
        modal.Secret.from_name("google-vision-credentials"),
        modal.Secret.from_name("vapid-credentials"),
        modal.Secret.from_name("external-api-credentials"),
        modal.Secret.from_name("resend-api-key"),
        modal.Secret.from_name("r2-credentials"),
    ],
    timeout=3600,
)
def run_embed_gpu(hot_reload=True):
    from incremental_embed import run_incremental_embed
    return run_incremental_embed(hot_reload=hot_reload)


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
        modal.Secret.from_name("stripe-credentials"),
        modal.Secret.from_name("google-vision-credentials"),
        modal.Secret.from_name("vapid-credentials"),
        modal.Secret.from_name("external-api-credentials"),
        modal.Secret.from_name("resend-api-key"),
        modal.Secret.from_name("r2-credentials"),
    ],
    schedule=modal.Cron("0 1 * * *"),  # Every day at 1am UTC (was Monday-only —
    # the release calendar's state machine needs daily ticks to advance sets
    # through their states promptly around release_date; see set_scheduler.py
    # run_scheduler()/_advance_entry).
    timeout=3600,
)
def scheduled_set_check():
    import os, sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"

    # Same fix serve() applies before importing app — without this, app.py's
    # load_vertical() reads the unpatched vertical.json (db_root still the
    # local "C:\\CardsDB" literal), which breaks rebuild_mtg_set_totals()
    # (called via run_scheduler's step 2c) silently returning None for
    # every MTG set.
    vol.reload()
    _fix_vertical_config()

    import json as _cj
    with open("/app/config.json") as _cf:
        _ccfg = _cj.load(_cf)
    os.environ["ANTHROPIC_API_KEY"] = _ccfg.get("anthropic_api_key", "")

    from set_scheduler import run_scheduler
    result = run_scheduler(embed_fn=run_embed_gpu.remote)

    # Auto-refresh SV-era set code map from pokemontcg.io API
    try:
        import requests as _req
        import ocr_confirm as _ocr
        _sets_resp = _req.get('https://api.pokemontcg.io/v2/sets?pageSize=250', timeout=30)
        _sets_data = _sets_resp.json().get('data', [])
        _new_map = {}
        for _s in _sets_data:
            _code = _s.get('ptcgoCode', '').strip().upper()
            _sid = _s.get('id', '').strip()
            # Only include 3-letter codes (SV-era cards that print codes on card face)
            if _code and _sid and len(_code) == 3:
                _new_map[_code] = _sid
        if _new_map:
            _ocr._PKM_SETCODE_MAP.update(_new_map)
            print(f"[SCHEDULER] Updated _PKM_SETCODE_MAP: {len(_new_map)} SV-era codes", flush=True)
    except Exception as _e:
        print(f"[SCHEDULER] setcode map refresh failed: {_e}", flush=True)

    try:
        vol.commit()
        print("[CRON] vol.commit() OK", flush=True)
    except Exception as e:
        print(f"[CRON] vol.commit() FAILED: {e}", flush=True)
    print(f"[CRON] Done — {result}", flush=True)

    # Collect newly scraped SKUs for a fast delta rebuild. Two sources feed
    # this now: the legacy bulk per-TCG path (new_set_ids -> folder scan, as
    # before) and the calendar state machine's own tracked new_skus
    # (set_scheduler.run_scheduler's "new_skus" key — sets ingested via
    # _try_catalog_ingest already know their exact SKU list, no rescan
    # needed for those).
    new_skus = None
    try:
        new_set_ids = set()
        for game_data in result.get("tcgs", {}).values():
            for s in game_data.get("new_sets", []):
                sid = s["id"] if isinstance(s, dict) else s
                if sid:
                    new_set_ids.add(sid)
        if new_set_ids:
            new_skus = []
            db_root = "/modal_data/CardsDB"
            for game_folder in ("pokemon", "mtg", "yugioh"):
                game_dir = os.path.join(db_root, game_folder)
                if not os.path.isdir(game_dir):
                    continue
                for sku in os.listdir(game_dir):
                    if sku.startswith("_"):
                        continue
                    parts = sku.split("-")
                    if game_folder == "pokemon":
                        poke_set_id = ("jpn-" + "-".join(parts[1:-1])) if (parts[0] == "jpn" and len(parts) >= 2) else parts[0]
                        if poke_set_id in new_set_ids:
                            new_skus.append(sku)
                    elif game_folder in ("mtg", "yugioh") and len(parts) >= 3 and parts[1] in new_set_ids:
                        new_skus.append(sku)
    except Exception as _e:
        print(f"[CRON] Failed to collect legacy-path new SKUs: {_e}", flush=True)

    calendar_new_skus = result.get("new_skus") or []
    if calendar_new_skus:
        new_skus = (new_skus or []) + [s for s in calendar_new_skus if s not in (new_skus or [])]

    # Delta-only policy: a no-new day does nothing. No full-rebuild
    # fallback when new_skus is empty, and no Sunday force-full safety net
    # (removed — full reconcile is manual-only now; force_full_reconcile
    # is always False from set_scheduler.py and no longer read here).
    if new_skus:
        try:
            rebuild_lookup_files.remote(new_skus=new_skus)
            print(f"[CRON] rebuild_lookup_files completed (delta ({len(new_skus)} SKUs))", flush=True)
        except Exception as e:
            print(f"[CRON] rebuild_lookup_files FAILED: {e}", flush=True)
    else:
        print("[CRON] no new SKUs — skipping rebuild_lookup_files (delta-only policy)", flush=True)


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    schedule=modal.Cron("0 2 * * *"),  # Every day at 2am UTC
    timeout=300,
)
def scheduled_fx_refresh():
    """Daily refresh of /modal_data/fx_rates.json — the single cached rate
    source all 5 GBP-display consumers read from (see fx_rates.py). Also
    piggybacked into the Monday scheduler (set_scheduler.py run_scheduler
    step 2e) so the weekly run always uses a fresh rate too; this daily tick
    just keeps it fresh on the other 6 days."""
    import os, sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"
    vol.reload()
    from fx_rates import refresh_fx_rates
    try:
        result = refresh_fx_rates()
        vol.commit()
        print(f"[FX-CRON] {result}", flush=True)
    except Exception as e:
        print(f"[FX-CRON] FAILED: {e}", flush=True)


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[modal.Secret.from_name("justtcg-credentials")],
    schedule=modal.Cron("0 3 * * *"),  # Every day at 3am UTC — offset from
    # the 2am FX cron and the Monday weekly scheduler, so the three jobs
    # never overlap.
    timeout=5400,  # ceiling, not a target — this does NOT share the weekly
    # job's 3600s budget; that's the whole point of a separate cron.
)
def scheduled_jp_price_refresh():
    """Daily refresh of Cardmarket pricing for jpn- cards that ALREADY have
    a price (see refresh_cardmarket_prices in scrape_pokemon_jpn.py — the
    ~1,261 from the original backfill; the ~3,033 repo-only/vintage cards
    with no resolvable price are skipped, not re-walked, since
    classify_unpriced already proved those are structurally unpriceable).

    No gpu= here — this is pure HTTP fetch + JSON write, CPU-only, same
    pattern as scheduled_fx_refresh above (same image, just no GPU attached
    so it's billed as CPU-only despite the image containing the ML deps).

    Lazy-imports scrape_pokemon_jpn inside the function body (not at this
    module's top level) — that module constructs its own separate
    modal.App + Image at import time, and deferring the import to call-time
    avoids that becoming part of matchit-api's own deploy/build graph. This
    mirrors how scheduled_set_check() above already lazy-imports
    set_scheduler, which has the identical second-modal.App pattern."""
    print(f"[JP-PRICE-CRON] Starting run at {datetime.utcnow().isoformat()}Z", flush=True)
    import os, sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"
    vol.reload()
    from pathlib import Path
    from scrape_pokemon_jpn import refresh_cardmarket_prices
    try:
        # Cardmarket refresh (existing — only re-fetches already-priced cards)
        result = refresh_cardmarket_prices(Path("/modal_data/CardsDB"), dry_run=False)
        print(f"[JP-REFRESH] Cardmarket: {result}")

        # JustTCG refresh (fills cards with no Cardmarket price)
        justtcg_key = os.environ.get("JUSTTCG_API_KEY", "").strip()
        if justtcg_key:
            from scrape_pokemon_jpn import backfill_justtcg_prices
            jtcg_result = backfill_justtcg_prices(
                Path("/modal_data/CardsDB"),
                api_key=justtcg_key,
                dry_run=False,
                resume=True,  # Skip cards that already have any price
            )
            print(f"[JP-REFRESH] JustTCG: {jtcg_result}")
        else:
            print("[JP-REFRESH] JustTCG: no API key found, skipping")

        vol.commit()
        print(f"[JP-PRICE-CRON] {result}", flush=True)
    except Exception as e:
        print(f"[JP-PRICE-CRON] FAILED: {e}", flush=True)
        raise


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[modal.Secret.from_name("pokewallet-credentials")],
    schedule=modal.Cron("0 4 * * *"),  # 4am UTC — after the 3am JP price refresh
    timeout=5400,
)
def scheduled_en_price_refresh():
    """Daily refresh of TCGplayer + Cardmarket pricing for English (non-jpn-)
    Pokémon cards via TCGdex EN (see refresh_en_prices.py). Same CPU-only,
    lazy-import + direct-call + vol.commit pattern as scheduled_jp_price_refresh
    above; refresh_en_prices.py constructs its own separate modal.App at import,
    so the import is deferred to call time to keep it out of matchit-api's deploy
    graph."""
    import os, sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"
    vol.reload()
    from pathlib import Path
    from refresh_en_prices import refresh_en_prices
    try:
        result = refresh_en_prices(Path("/modal_data/CardsDB"), dry_run=False)
        vol.commit()
        print(f"[EN-PRICE-CRON] {result}", flush=True)
    except Exception as e:
        print(f"[EN-PRICE-CRON] FAILED: {e}", flush=True)


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=3600,
)
def rebuild_lookup_files(new_skus: list = None, new_set_ids: list = None):
    """Rebuild set_metadata.json and sku_game_map.json from CardsDB on the Modal volume.
    If new_set_ids is provided (and new_skus is None), scans only those set folders to
    collect SKUs, then runs a delta update — much faster than a full rebuild.
    If new_skus is provided, only processes those SKUs (delta update).
    If both are None, full rebuild from scratch.
    """
    import json, os, urllib.request, urllib.error

    db_root     = "/modal_data/CardsDB"
    sku_map_path = "/modal_data/sku_game_map.json"
    meta_path   = "/modal_data/set_metadata.json"
    CATEGORY_MAP = {"POKEMON": "POKEMON", "MTG": "MTG", "YUGIOH": "YUGIOH"}

    # Canary ground truth: auto-discover known jpn- set_ids from CardsDB folder
    # names (jpn-{setcode}-{cardnum}, where setcode may itself contain hyphens,
    # e.g. jpn-sv-p-098) so the splits below can be sanity-checked against it.
    _jpn_known_set_ids = set()
    _jpn_pokemon_dir = os.path.join(db_root, "pokemon")
    if os.path.isdir(_jpn_pokemon_dir):
        for _folder in os.listdir(_jpn_pokemon_dir):
            _fparts = _folder.split("-")
            if _fparts[0].lower() == "jpn" and len(_fparts) >= 3:
                _jpn_known_set_ids.add("jpn-" + "-".join(_fparts[1:-1]).lower())

    # Resolve new_set_ids → new_skus by scanning only the relevant set folders
    if new_set_ids is not None and new_skus is None:
        target_ids = set(s.lower() for s in new_set_ids)
        collected = []
        for game_folder in ("pokemon", "mtg", "yugioh"):
            game_dir = os.path.join(db_root, game_folder)
            if not os.path.isdir(game_dir):
                continue
            for name in os.listdir(game_dir):
                if name.startswith("_"):
                    continue
                parts = name.split("-")
                if game_folder == "pokemon" and len(parts) >= 2:
                    poke_set_id = ("jpn-" + "-".join(parts[1:-1]).lower()) if (parts[0].lower() == "jpn" and len(parts) >= 3) else parts[0].lower()
                    if poke_set_id.startswith("jpn-") and poke_set_id not in _jpn_known_set_ids:
                        print(f"[REBUILD] CANARY: {name!r} resolved to unknown jpn set_id {poke_set_id!r} (not in auto-discovered jpn- set list)", flush=True)
                    if poke_set_id in target_ids:
                        collected.append(name)
                elif game_folder in ("mtg", "yugioh") and len(parts) >= 3 and parts[1].lower() in target_ids:
                    collected.append(name)
        new_skus = collected
        print(
            f"[REBUILD] new_set_ids {new_set_ids} resolved to {len(new_skus)} SKUs",
            flush=True,
        )

    mode_label = f"delta ({len(new_skus)} SKUs)" if new_skus is not None else "full"
    print(f"[REBUILD] Starting {mode_label} rebuild...", flush=True)

    # ── 1. sku_game_map.json ─────────────────────────────────────────────────
    if new_skus is not None:
        # Delta: load existing map, add profiles for unambiguous new SKUs only
        try:
            with open(sku_map_path, "r", encoding="utf-8") as f:
                sku_game_map = json.load(f)
        except Exception:
            sku_game_map = {}
        added_skus = 0
        for sku in new_skus:
            if sku.startswith("_") or sku.startswith("mtg-") or sku.startswith("ygo-"):
                continue
            for game_folder in ("pokemon", "mtg", "yugioh"):
                profile_path = os.path.join(db_root, game_folder, sku, "profile.json")
                if os.path.isfile(profile_path):
                    try:
                        with open(profile_path, "r", encoding="utf-8") as f:
                            profile = json.load(f)
                        game = CATEGORY_MAP.get((profile.get("category") or "").upper())
                        if game:
                            sku_game_map[sku] = game
                            added_skus += 1
                    except Exception as e:
                        print(f"[REBUILD] WARN {profile_path}: {e}", flush=True)
                    break
        _sku_map_tmp = sku_map_path + ".tmp"
        with open(_sku_map_tmp, "w", encoding="utf-8") as f:
            json.dump(sku_game_map, f, separators=(",", ":"))
        os.replace(_sku_map_tmp, sku_map_path)
        print(f"[REBUILD] sku_game_map.json: {len(sku_game_map)} entries (+{added_skus} new)", flush=True)
    else:
        # Full: scan every game folder
        sku_game_map = {}
        counts = {"POKEMON": 0, "MTG": 0, "YUGIOH": 0, "unknown": 0}
        for game_folder in ("pokemon", "mtg", "yugioh"):
            game_dir = os.path.join(db_root, game_folder)
            if not os.path.isdir(game_dir):
                continue
            for sku in os.listdir(game_dir):
                if sku.startswith("_") or sku.startswith("mtg-") or sku.startswith("ygo-"):
                    continue
                profile_path = os.path.join(game_dir, sku, "profile.json")
                if not os.path.isfile(profile_path):
                    continue
                try:
                    with open(profile_path, "r", encoding="utf-8") as f:
                        profile = json.load(f)
                    game = CATEGORY_MAP.get((profile.get("category") or "").upper())
                    if game:
                        sku_game_map[sku] = game
                        counts[game] += 1
                    else:
                        counts["unknown"] += 1
                except Exception as e:
                    print(f"[REBUILD] WARN {profile_path}: {e}", flush=True)
        _sku_map_tmp = sku_map_path + ".tmp"
        with open(_sku_map_tmp, "w", encoding="utf-8") as f:
            json.dump(sku_game_map, f, separators=(",", ":"))
        os.replace(_sku_map_tmp, sku_map_path)
        print(
            f"[REBUILD] sku_game_map.json: {len(sku_game_map)} entries "
            f"(P={counts['POKEMON']} M={counts['MTG']} Y={counts['YUGIOH']} ?={counts['unknown']})",
            flush=True,
        )

    # ── 2. set_metadata.json ─────────────────────────────────────────────────
    # Load existing metadata to preserve manually curated fields
    existing = {}
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    new_meta = dict(existing)
    new_sets = 0

    if new_skus is not None:
        # Delta: extract set_ids from the new SKU names, skip already-known ones
        poke_new, mtg_new, ygo_new = set(), set(), set()
        for sku in new_skus:
            parts = sku.split("-")
            if sku.startswith("ygo-") and len(parts) >= 3:
                set_id = parts[1]
                if set_id not in new_meta:
                    ygo_new.add(set_id)
            elif sku.startswith("mtg-") and len(parts) >= 3:
                set_id = parts[1]
                if set_id not in new_meta:
                    mtg_new.add(set_id)
            elif "-" in sku and not sku.startswith("_"):
                set_id = ("jpn-" + "-".join(parts[1:-1])) if (parts[0] == "jpn" and len(parts) >= 2) else parts[0]
                if set_id.startswith("jpn-") and set_id.lower() not in _jpn_known_set_ids:
                    print(f"[REBUILD] CANARY: sku {sku!r} resolved to unknown jpn set_id {set_id!r} (not in auto-discovered jpn- set list)", flush=True)
                if set_id not in new_meta:
                    poke_new.add(set_id)
    else:
        # Full: discover all set_ids from folder names (no profile.json reads)
        sets_by_game = {"POKEMON": set(), "MTG": set(), "YUGIOH": set()}
        game_label = {"pokemon": "POKEMON", "mtg": "MTG", "yugioh": "YUGIOH"}
        for game_folder, game in game_label.items():
            game_dir = os.path.join(db_root, game_folder)
            if not os.path.isdir(game_dir):
                continue
            for name in os.listdir(game_dir):
                if name.startswith("_") or not os.path.isdir(os.path.join(game_dir, name)):
                    continue
                parts = name.split("-")
                if game == "POKEMON" and "-" in name:
                    poke_set_id = ("jpn-" + "-".join(parts[1:-1])) if (parts[0] == "jpn" and len(parts) >= 2) else parts[0]
                    if poke_set_id.startswith("jpn-") and poke_set_id.lower() not in _jpn_known_set_ids:
                        print(f"[REBUILD] CANARY: {name!r} resolved to unknown jpn set_id {poke_set_id!r} (not in auto-discovered jpn- set list)", flush=True)
                    sets_by_game["POKEMON"].add(poke_set_id)
                elif game == "MTG" and len(parts) >= 3 and parts[0] == "mtg":
                    sets_by_game["MTG"].add(parts[1])
                elif game == "YUGIOH" and len(parts) >= 3 and parts[0] == "ygo":
                    sets_by_game["YUGIOH"].add(parts[1])
        poke_new = {s for s in sets_by_game["POKEMON"] if s not in new_meta}
        mtg_new  = {s for s in sets_by_game["MTG"]     if s not in new_meta}
        ygo_new  = {s for s in sets_by_game["YUGIOH"]  if s not in new_meta}

    def _fetch_json(url, label):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "GrailSweep/1.0 contact@grailsweep.com",
                         "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"[REBUILD] {label} fetch failed: {e}", flush=True)
            return None

    # Pokémon — single batch call to pokemontcg.io
    if poke_new:
        print(f"[REBUILD] Pokémon: {len(poke_new)} new sets to fetch...", flush=True)
        data = _fetch_json(
            "https://api.pokemontcg.io/v2/sets?select=id,name,total,printedTotal",
            "pokemontcg.io",
        )
        api_lookup = {}
        if data:
            for s in data.get("data", []):
                sid = s.get("id", "")
                if sid:
                    api_lookup[sid.lower()] = s
        for set_id in sorted(poke_new):
            entry = api_lookup.get(set_id.lower())
            if entry:
                new_meta[set_id] = {
                    "name":          entry.get("name", set_id),
                    "game":          "POKEMON",
                    "printed_total": entry.get("printedTotal", entry.get("total")),
                    "total":         entry.get("total"),
                    "exclude":       "promo" in set_id.lower(),
                }
                print(f"[REBUILD]   + Pokémon {set_id}: {entry.get('name')}", flush=True)
            else:
                new_meta[set_id] = {
                    "name": set_id, "game": "POKEMON",
                    "printed_total": None, "total": None,
                    "exclude": "promo" in set_id.lower(),
                }
                print(f"[REBUILD]   ? Pokémon {set_id}: not in pokemontcg.io", flush=True)
            new_sets += 1

    # MTG — single batch call to Scryfall
    if mtg_new:
        print(f"[REBUILD] MTG: {len(mtg_new)} new sets to fetch...", flush=True)
        data = _fetch_json("https://api.scryfall.com/sets", "Scryfall")
        api_lookup = {}
        if data:
            for s in data.get("data", []):
                code = s.get("code", "")
                if code:
                    api_lookup[code.lower()] = s
        MTG_EXCLUDE_TYPES = {"memorabilia", "token", "minigame"}
        for set_id in sorted(mtg_new):
            entry = api_lookup.get(set_id.lower())
            if entry:
                new_meta[set_id] = {
                    "name":          entry.get("name", set_id),
                    "game":          "MTG",
                    "printed_total": None,
                    "total":         None,
                    "scryfall_id":   entry.get("id", ""),
                    "exclude":       entry.get("set_type", "") in MTG_EXCLUDE_TYPES,
                }
                print(f"[REBUILD]   + MTG {set_id}: {entry.get('name')}", flush=True)
            else:
                new_meta[set_id] = {
                    "name": set_id, "game": "MTG",
                    "printed_total": None, "total": None, "exclude": False,
                }
                print(f"[REBUILD]   ? MTG {set_id}: not in Scryfall", flush=True)
            new_sets += 1

    # YGO — single batch call to YGOProDeck
    if ygo_new:
        print(f"[REBUILD] YGO: {len(ygo_new)} new sets to fetch...", flush=True)
        all_sets = _fetch_json("https://db.ygoprodeck.com/api/v7/cardsets.php", "YGOProDeck")
        api_lookup = {}
        if isinstance(all_sets, list):
            for s in all_sets:
                code = (s.get("set_code") or "").strip().upper()
                if code:
                    api_lookup[code] = s
        for set_id in sorted(ygo_new):
            entry = api_lookup.get(set_id.upper())
            if entry:
                num = entry.get("num_of_cards")
                try:
                    num = int(num) if num is not None else None
                except (ValueError, TypeError):
                    num = None
                new_meta[set_id] = {
                    "name":          entry.get("set_name", set_id),
                    "game":          "YUGIOH",
                    "printed_total": num,
                    "total":         num,
                    "exclude":       False,
                }
                print(f"[REBUILD]   + YGO {set_id}: {entry.get('set_name')}", flush=True)
            else:
                new_meta[set_id] = {
                    "name": set_id, "game": "YUGIOH",
                    "printed_total": None, "total": None, "exclude": False,
                }
                print(f"[REBUILD]   ? YGO {set_id}: not in YGOProDeck", flush=True)
            new_sets += 1

    if new_sets or new_skus is not None:
        _meta_tmp = meta_path + ".tmp"
        with open(_meta_tmp, "w", encoding="utf-8") as f:
            json.dump(new_meta, f)
        os.replace(_meta_tmp, meta_path)

    vol.commit()
    print(
        f"[REBUILD] set_metadata.json: {len(new_meta)} sets total ({new_sets} new added)",
        flush=True,
    )
    print(f"[REBUILD] Done ({mode_label}). Volume committed.", flush=True)


@app.local_entrypoint()
def rebuild_lookup_files_local():
    rebuild_lookup_files.remote()


@app.local_entrypoint()
def rebuild_new_sets(set_ids: str = ""):
    """Rebuild lookup files for specific sets.
    Pass --set-ids "jpn-sv1,jpn-svln" or leave empty to auto-discover jpn- sets from local CardsDB.
    """
    if set_ids:
        id_list = [s.strip() for s in set_ids.split(",") if s.strip()]
    else:
        pokemon_dir = os.path.join("C:\\CardsDB", "pokemon")
        seen_sets = set()
        for folder in os.listdir(pokemon_dir):
            parts = folder.split("-")
            if parts[0] == "jpn" and len(parts) >= 3:
                if os.path.exists(os.path.join(pokemon_dir, folder, "front.png")):
                    seen_sets.add("jpn-" + "-".join(parts[1:-1]))
        id_list = sorted(seen_sets)
        # Phantom-set canary: every discovered id must have content past "jpn-"
        for sid in id_list:
            if len(sid) <= len("jpn-"):
                print(f"[REBUILD] CANARY: malformed jpn set_id {sid!r} discovered — check folder naming", flush=True)
        print(f"[REBUILD] Auto-discovered {len(id_list)} jpn- sets: {id_list}", flush=True)
    rebuild_lookup_files.remote(new_set_ids=id_list)


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=600,
)
def _jp_coverage_remote():
    import os
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor

    sets_total = defaultdict(int)
    sets_imaged = defaultdict(int)

    base = "/modal_data/CardsDB/pokemon"
    if not os.path.exists(base):
        return {"error": f"Path not found: {base}"}

    jp_folders = [f for f in os.listdir(base) if f.startswith("jpn-")]

    def _set_id(folder):
        parts = folder.split("-")
        # Reconstruct set_id safely (handles jpn-sv-p-098 etc)
        if len(parts) >= 3:
            return "-".join(parts[:-1])
        return "-".join(parts[:2])

    # The per-folder front.png check is a network stat on the Modal volume;
    # 12k+ of them serially blow the timeout, so fan them out over threads and
    # aggregate on the main thread (defaultdict increments aren't thread-safe).
    def _has_image(folder):
        return os.path.exists(os.path.join(base, folder, "front.png"))

    with ThreadPoolExecutor(max_workers=128) as ex:
        imaged_flags = list(ex.map(_has_image, jp_folders))

    for folder, has_image in zip(jp_folders, imaged_flags):
        set_id = _set_id(folder)
        sets_total[set_id] += 1
        if has_image:
            sets_imaged[set_id] += 1

    return {
        "total_cards": sum(sets_total.values()),
        "total_imaged": sum(sets_imaged.values()),
        "total_sets": len(sets_total),
        "sets_imaged": dict(sorted(sets_imaged.items())),
        "sets_total": dict(sorted(sets_total.items())),
    }

@app.local_entrypoint()
def inspect_jp_coverage():
    result = _jp_coverage_remote.remote()
    if "error" in result:
        print(f"ERROR: {result['error']}")
        return

    print(f"\n=== JP COVERAGE REPORT ===")
    print(f"Total JP cards in CardsDB: {result['total_cards']}")
    print(f"Total with front.png:      {result['total_imaged']}")
    print(f"Total JP sets:             {result['total_sets']}")
    print(f"\nImaged cards per set (sets with at least 1 image):")
    for set_id, imaged in result["sets_imaged"].items():
        total = result["sets_total"].get(set_id, 0)
        pct = (imaged / total * 100) if total else 0
        print(f"  {set_id}: {imaged}/{total} ({pct:.0f}%)")
    print(f"\nSets with ZERO images:")
    for set_id, total in result["sets_total"].items():
        if result["sets_imaged"].get(set_id, 0) == 0:
            print(f"  {set_id}: {total} cards, no images")
    print("==========================\n")


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=60,
)
def _jp_sample_profile_remote(prefix: str = "jpn-"):
    import json, os

    # Lazily scan for the FIRST folder matching `prefix` with a profile.json and
    # stop. (glob.glob over this ~135k-folder volume dir eagerly stats every entry
    # and blows the timeout — scandir + early break reads only until the first hit.)
    pref = prefix.lower()
    pokemon_dir = "/modal_data/CardsDB/pokemon"
    sample_path = None
    with os.scandir(pokemon_dir) as it:
        for d in it:
            if not d.name.lower().startswith(pref) or not d.is_dir():
                continue
            candidate = os.path.join(d.path, "profile.json")
            if os.path.isfile(candidate):
                sample_path = candidate
                break

    if sample_path is None:
        return {"error": f"No {prefix}* profile.json files found"}

    with open(sample_path) as f:
        profile = json.load(f)

    return {
        "path": sample_path,
        "keys": list(profile.keys()),
        "sample": profile,
    }

@app.function(image=image, volumes={"/modal_data": vol}, timeout=60)
def _jp_set_metadata_remote(focus: str = "jpn-sv2a"):
    import json, os

    meta_path = "/modal_data/set_metadata.json"
    if not os.path.exists(meta_path):
        return {"error": f"set_metadata.json not found at {meta_path}"}

    with open(meta_path) as f:
        meta = json.load(f)

    jp_keys = sorted(k for k in meta if k.startswith("jpn-"))
    has_total = 0
    for k in jp_keys:
        e = meta.get(k) or {}
        if isinstance(e, dict) and (e.get("printed_total") is not None or e.get("total") is not None):
            has_total += 1

    # focus entry + a few samples
    focus_entry = meta.get(focus, "__MISSING__")
    samples = {k: meta[k] for k in jp_keys[:4]}

    return {
        "total_meta_keys": len(meta),
        "total_jp_keys": len(jp_keys),
        "jp_keys_with_total_or_printed_total": has_total,
        "focus_key": focus,
        "focus_entry": focus_entry,
        "samples": samples,
    }


@app.function(image=image, volumes={"/modal_data": vol}, timeout=60)
def _jp_printed_total_map_remote():
    import json, os

    meta_path = "/modal_data/set_metadata.json"
    if not os.path.exists(meta_path):
        return {"error": f"set_metadata.json not found at {meta_path}"}

    with open(meta_path) as f:
        meta = json.load(f)

    # jpn- sets with a non-null printed_total → {set_key: printed_total}
    out = {}
    for k, e in meta.items():
        if not k.startswith("jpn-") or not isinstance(e, dict):
            continue
        pt = e.get("printed_total")
        if pt is not None:
            out[k] = pt
    return {"printed_totals": dict(sorted(out.items()))}


@app.local_entrypoint()
def analyze_jp_printed_totals():
    from collections import defaultdict
    r = _jp_printed_total_map_remote.remote()
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    pt_map = r["printed_totals"]
    by_total = defaultdict(list)
    for set_key, pt in pt_map.items():
        by_total[pt].append(set_key)

    print("\n=== JP printed_total ANALYSIS ===")
    print(f"jpn- sets with non-null printed_total: {len(pt_map)}")
    print(f"distinct printed_total values:         {len(by_total)}")

    print("\n1) Count of sets sharing each printed_total (desc by count):")
    for pt in sorted(by_total, key=lambda x: (-len(by_total[x]), x)):
        print(f"  printed_total={pt}: {len(by_total[pt])} set(s) -> {', '.join(sorted(by_total[pt]))}")

    uniques = {pt: v[0] for pt, v in by_total.items() if len(v) == 1}
    shared = {pt: v for pt, v in by_total.items() if len(v) > 1}

    print(f"\n2) printed_totals UNIQUE to a single set ({len(uniques)}):")
    for pt in sorted(uniques):
        print(f"  {pt}: {uniques[pt]}")

    print(f"\n3) printed_totals SHARED across multiple sets ({len(shared)}):")
    for pt in sorted(shared, key=lambda x: (-len(shared[x]), x)):
        print(f"  {pt}: {len(shared[pt])} sets -> {', '.join(sorted(shared[pt]))}")
    print("=================================\n")


@app.local_entrypoint()
def inspect_jp_set_metadata(focus: str = "jpn-sv2a"):
    r = _jp_set_metadata_remote.remote(focus=focus)
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    print("\n=== JP SET METADATA ===")
    print(f"Total set_metadata keys:        {r['total_meta_keys']}")
    print(f"Total jpn- keys:                {r['total_jp_keys']}")
    print(f"jpn- keys w/ printed_total|total: {r['jp_keys_with_total_or_printed_total']}")
    print(f"\nFocus [{r['focus_key']}]: {r['focus_entry']}")
    print(f"\nSample jpn- entries:")
    for k, v in r["samples"].items():
        print(f"  {k}: {v}")
    print("=======================\n")


@app.local_entrypoint()
def sample_jp_profile(prefix: str = "jpn-"):
    result = _jp_sample_profile_remote.remote(prefix=prefix)
    if "error" in result:
        print(f"ERROR: {result['error']}")
        return
    print(f"\n=== SAMPLE JP PROFILE ===")
    print(f"File: {result['path']}")
    print(f"\nAll keys: {result['keys']}")
    print(f"\nAll fields:")
    for k, v in result["sample"].items():
        print(f"  {k}: {v}")
    print("=========================\n")


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[modal.Secret.from_name("justtcg-credentials")],
    timeout=7200,
)
def _run_justtcg_backfill_remote(dry_run: bool = False):
    import os, sys
    from pathlib import Path
    os.chdir("/app")
    sys.path.insert(0, "/app")
    from scrape_pokemon_jpn import backfill_justtcg_prices
    api_key = os.environ.get("JUSTTCG_API_KEY", "").strip()
    if not api_key:
        return {"error": "No JUSTTCG_API_KEY in environment"}
    vol.reload()
    result = backfill_justtcg_prices(
        Path("/modal_data/CardsDB"),
        api_key=api_key,
        dry_run=dry_run,
        resume=True,
    )
    if not dry_run:
        vol.commit()
    return result


@app.local_entrypoint()
def run_justtcg_backfill(dry_run: bool = False):
    """One-time backfill: fill in TCGPlayer prices for unpriced JP cards via JustTCG."""
    print(f"Starting JustTCG price backfill (resume=True, dry_run={dry_run})...")
    result = _run_justtcg_backfill_remote.remote(dry_run=dry_run)
    print("\n=== JUSTTCG BACKFILL RESULT ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print("================================\n")

