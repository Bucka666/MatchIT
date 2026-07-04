import os
import re
import uuid
import json
import stripe
import shutil
import sqlite3
import base64
import threading
import time
from datetime import datetime
from pathlib import Path
from io import BytesIO
from typing import Dict, List, Optional, Tuple
from marketplace import marketplace_search, auto_classify_product, detect_barcode, barcode_to_search_query, build_search_query
from api_routes import register_api_routes
from vertical_loader import get_vertical
from ocr_confirm import ocr_confirm_ranking, ocr_direct_lookup, _PKM_SETCODE_MAP
from profile_utils import _load_card_profile_for_sku
from flask_cors import CORS
from email_sender import gs_send_email
from r2_util import upload_to_r2
from fx_rates import get_fx
import numpy as np
from PIL import Image, ImageOps

from flask import (
    Flask,
    request,
    render_template,
    redirect,
    url_for,
    send_file,
    abort,
    make_response,
    session,
    flash,
    current_app,
    send_from_directory,
    jsonify,
)

# ============================================================
# App
# ============================================================

from log_config import configure_logging
configure_logging()
print("[LOG-INIT] app.py: logging configured -> stdout INFO", flush=True)

app = Flask(__name__)
app.secret_key = os.environ.get("MATCHIT_SECRET", "dev-secret-change-me")
CORS(app)
register_api_routes(app)

# ── Stripe webhook worker durability ─────────────────────────────────────────
# Stripe events are processed off-request in daemon threads. To survive Modal
# container scaledown/shutdown without dropping a paid checkout, we (a) track
# live worker threads and join them on SIGTERM, and (b) only mark an event
# processed in the idempotency Dict AFTER its side effects succeed (see
# _process_stripe_event_safe). _vol_commit_fn is wired by matchit_modal.py to
# vol.commit so subscriptions.json writes are flushed before the container dies;
# it stays None (no-op) in local dev / non-Modal contexts.
import signal as _signal
_vol_commit_fn = None  # set by matchit_modal.py after vol is available
_active_stripe_threads = []
_active_stripe_threads_lock = threading.Lock()

def _handle_sigterm(signum, frame):
    print("[WEBHOOK] SIGTERM — draining Stripe worker threads...", flush=True)
    with _active_stripe_threads_lock:
        threads = list(_active_stripe_threads)
    for _t in threads:
        _t.join(timeout=10)
    print("[WEBHOOK] Stripe worker drain complete.", flush=True)

# signal.signal() only works on the main thread of the main interpreter; guard
# so an off-main-thread import can't crash app load.
try:
    _signal.signal(_signal.SIGTERM, _handle_sigterm)
except ValueError as _sig_e:
    print(f"[WEBHOOK] SIGTERM handler not registered (non-main thread): {_sig_e}", flush=True)

# ── CF Proxy Secret enforcement ──────────────────────────────────────────────
# Blocks direct .modal.run access. The Cloudflare Worker injects
# X-CF-Proxy-Secret on every request; requests without it (or with the wrong
# value) are rejected with 403. If CF_PROXY_SECRET env var isn't set, the
# check is skipped (allows local dev / safe rollout).
import secrets as _secrets_mod
_CF_PROXY_SECRET = os.environ.get("CF_PROXY_SECRET", "").strip()

@app.before_request
def _enforce_cf_proxy():
    if not _CF_PROXY_SECRET:
        return  # Secret not configured — skip check

    # Allow Modal's own internal traffic (e.g. cron jobs, internal callbacks)
    ua = request.headers.get("User-Agent", "") or ""
    if ua.startswith("Modal/") or ua.startswith("modal-client"):
        return

    incoming = (request.headers.get("X-CF-Proxy-Secret") or "").strip()
    if not incoming or not _secrets_mod.compare_digest(incoming, _CF_PROXY_SECRET):
        return ("Forbidden — direct origin access is blocked. "
                "Please use https://grailsweep.com\n"), 403
# ─────────────────────────────────────────────────────────────────────────────

# ============================================================
# Stable paths (AppData)
# ============================================================


def get_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    vertical_id = get_vertical().get("id", "default")
    d = os.path.join(base, "MatchITv2_ProductMatch_Data", vertical_id)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        print(f"[PATH] get_data_dir: makedirs({d}) raised {type(e).__name__}: {e} — continuing", flush=True)
    return d


def get_images_db_path() -> str:
    return os.path.join(get_data_dir(), "images.db")


def get_image_db_dir() -> str:
    d = os.path.join(get_data_dir(), "image_db")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        print(f"[PATH] get_image_db_dir: makedirs({d}) raised {type(e).__name__}: {e} — continuing", flush=True)
    return d


def get_pending_dir(batch_id: str) -> str:
    d = os.path.join(get_data_dir(), "pending", batch_id)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        print(f"[PATH] get_pending_dir: makedirs({d}) raised {type(e).__name__}: {e} — continuing", flush=True)
    return d


# ============================================================
# Config (project root)
# ============================================================

CONFIG_NAME = "config.json"


def get_config_path() -> str:
    return os.path.join(app.root_path, CONFIG_NAME)


def load_or_create_config() -> dict:
    p = get_config_path()
    if os.path.exists(p):
        try:
            cfg = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}

    # Core
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 5000)
    cfg.setdefault("secret_key", "CHANGE_ME_TO_RANDOM")
    cfg.setdefault("admin_password", "admin123")
    cfg.setdefault("require_admin_for_db", True)

    # Matching knobs
    cfg.setdefault("top_k_sku", 20)
    cfg.setdefault("top_m_per_sku", 3)
    cfg.setdefault("cap_per_sku", 30)

    # Confidence display (softmax-based)
    cfg.setdefault("softmax_temp", 0.015)
    cfg.setdefault("low_cert_prob", 0.55)
    cfg.setdefault("low_cert_prob_gap", 0.15)

    # Near-tie rerank thresholds
    cfg.setdefault("rerank_score_eps", 0.010)
    cfg.setdefault("rerank_sim_eps", 0.003)

    # Phase 2: consistency
    cfg.setdefault("consistency_n", 5)
    cfg.setdefault("consistency_sigma", 0.040)

    # AUTO selector exists but we LOCK Mode B in code (no A/B switching)
    cfg.setdefault("auto_enabled", True)
    cfg.setdefault("auto_force_full_small_bytes", 220_000)
    cfg.setdefault("auto_force_full_small_max_side", 900)

    # Embedding presets
    cfg.setdefault("embedder_backend", "clip")  # "clip", "dinov2", or "ensemble"
    cfg.setdefault("vertical", "keys")  # active vertical: "keys", "hardware", etc.
    cfg.setdefault("auto_mode_a_multi_crop", True)
    cfg.setdefault("auto_mode_a_suppress_bg", True)
    cfg.setdefault("auto_mode_a_max_side", 1024)

    # NOTE: multi_crop must be True to match DB embeddings
    cfg.setdefault("auto_mode_b_multi_crop", True)
    cfg.setdefault("auto_mode_b_suppress_bg", True)
    cfg.setdefault("auto_mode_b_max_side", 1024)

    # KeysDB mirror (Google Drive desktop mirror)
    cfg.setdefault("keysdb_root", r"C:\Users\c_a_b\My Drive\KeysDB")
    cfg.setdefault("keysdb_sync_max_new", 0)

    # --------------------------------------------------------
    # PAIRED CUT→BLANK STABILISATION (Option B)
    # --------------------------------------------------------
    cfg.setdefault("paired_front_shortlist_n", 120)
    cfg.setdefault("paired_back_verify_weight", 0.03)

    # --------------------------------------------------------
    # STYLE GATING (silhouette-based)
    # --------------------------------------------------------
    cfg.setdefault("style_gating_enabled", True)
    cfg.setdefault("style_mortice_aspect_thresh", 3.2)
    cfg.setdefault("style_edge_strip_px", 18)
    cfg.setdefault("style_symmetry_thresh", 0.62)

    # --------------------------------------------------------
    # SOFT TYPE PENALTY (non-destructive; no hard filter)
    # --------------------------------------------------------
    cfg.setdefault("style_type_mismatch_penalty", 0.985)
    cfg.setdefault("style_cyl_vs_double_penalty", 0.975)
    cfg.setdefault("style_mortice_mismatch_penalty", 0.965)
    cfg.setdefault("style_unknown_penalty", 0.995)

    # --------------------------------------------------------
    # SWAP-SAFE FRONT/BACK (paired scoring)
    # --------------------------------------------------------
    cfg.setdefault("paired_swap_safe_enabled", True)
    cfg.setdefault("paired_min_term_weight", 0.02)

    # --------------------------------------------------------
    # BACK CLAMP:
    # Only allow BACK to contribute when its best similarity is strong enough
    # --------------------------------------------------------
    cfg.setdefault("paired_back_min_best_sim", 0.88)

    # --------------------------------------------------------
    # FRONT SCORE WEIGHTS (stability on close families)
    # --------------------------------------------------------
    cfg.setdefault("front_score_w1", 0.75)
    cfg.setdefault("front_score_w2", 0.15)
    cfg.setdefault("front_score_w3", 0.10)
    cfg.setdefault("front_score_w1_two", 0.80)
    cfg.setdefault("front_score_w2_two", 0.20)

    # --------------------------------------------------------
    # CRITICAL STABILISER:
    # Paired scoring must never be worse than FRONT-only
    # --------------------------------------------------------
    cfg.setdefault("paired_front_floor_enabled", True)

    # --------------------------------------------------------
    # QUERY-LEVEL ORIENTATION RESOLUTION (deterministic)
    # --------------------------------------------------------
    cfg.setdefault("paired_query_orient_resolve", True)
    cfg.setdefault("paired_query_orient_topn", 40)
    cfg.setdefault("paired_query_orient_margin", 0.002)

    # --------------------------------------------------------
    # ORIENT CONFIDENCE GATE
    # --------------------------------------------------------
    cfg.setdefault("paired_orient_confident_margin", 0.012)

    # --------------------------------------------------------
    # LENGTH CUE (silhouette bbox height ratio)
    # --------------------------------------------------------
    cfg.setdefault("length_cue_enabled", True)
    cfg.setdefault("length_downscale_max_side", 420)
    cfg.setdefault("length_sigma", 0.055)
    cfg.setdefault("length_penalty_max", 0.03)
    cfg.setdefault("length_use_max_of_two", True)

    # --------------------------------------------------------
    # PROFILE SOFT PENALTY (replaces hard knockout filters)
    # Mismatching profile axes get a scoring penalty.
    # Matching or unknown = 1.0 (neutral). Only confirmed
    # mismatches are penalized. CLIP can still override.
    # --------------------------------------------------------
    cfg.setdefault("profile_filter_min_pool", 5)
    cfg.setdefault("profile_penalty_groove", 0.970)
    cfg.setdefault("profile_penalty_pin", 0.975)
    cfg.setdefault("profile_penalty_key_type", 0.960)
    cfg.setdefault("profile_penalty_groove_side", 0.970)
    cfg.setdefault("profile_penalty_flag_profile", 0.965)
    cfg.setdefault("profile_penalty_bow_shape", 0.960)
    cfg.setdefault("profile_penalty_gauge", 0.950)
    cfg.setdefault("profile_penalty_dimple_track", 0.955)

    # ── QUERY IMAGE CLEANING (rembg) ─────────────────
    # PERF: Disabled by default — rembg adds 2-6s per query and
    # the cleaned images are NOT used for CLIP embedding (only for
    # auto groove detection, which is also disabled).
    cfg.setdefault("query_clean_enabled", False)

    # ── AUTO GROOVE COUNTING ─────────────────────────
    # Disabled — unreliable on user photos. Groove counter
    # still used for batch DB profiling only.
    cfg.setdefault("auto_groove_enabled", False)

    # ── DINOV2 TIE-BREAKER ───────────────────────────
    cfg.setdefault("dinov2_tiebreak_enabled", True)
    cfg.setdefault("dinov2_tiebreak_eps", 0.015)
    # PERF: Background-preload DINOv2 at startup so first
    # tie-break doesn't incur 30s cold-start penalty.
    cfg.setdefault("dinov2_tiebreak_preload", True)
    # ACCURACY: DINOv2 must beat CLIP rank1 by at least this margin to swap —
    # a bare d1 > d0 is not enough (#10 fix). Starting value; tune from
    # [DINO-DIAG] instrumentation.
    cfg.setdefault("dinov2_swap_margin", 0.05)

    # ── OCR PROMOTION GATE (15c) ─────────────────────
    # A weak OCR match (bare card number, no denominator corroboration) must
    # not override a CLIP rank1 that is itself confident. Starting values;
    # tune from [OCR-GATE] instrumentation.
    cfg.setdefault("clip_promote_block_floor", 0.55)
    cfg.setdefault("clip_promote_block_gap", 0.05)

    # Save back (best effort)
    #try:
     #   Path(p).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    #except Exception:
       # pass

    return cfg


CFG = load_or_create_config()
_flask_secret = os.environ.get("FLASK_SECRET_KEY") or CFG.get("secret_key")
if _flask_secret:
    app.secret_key = str(_flask_secret)

@app.context_processor
def inject_config():
    return {"cfg": CFG}

@app.context_processor
def inject_fx():
    """Cached FX rates (see fx_rates.py) for any template that converts
    USD/EUR card prices to GBP. Cache-read only — no network call here."""
    return {"fx": get_fx()}

# ============================================================
# Startup safety check: API key configuration
# ============================================================
try:
    import os as _os, json as _json
    _api_keys_env = _os.environ.get("GRAILSWEEP_API_KEYS", "").strip()
    if _api_keys_env:
        _parsed = _json.loads(_api_keys_env)
        if isinstance(_parsed, dict) and _parsed:
            print(f"[STARTUP] API keys loaded from env: {len(_parsed)} key(s) — {list(_parsed.keys())}", flush=True)
        else:
            print(f"[STARTUP] WARNING: GRAILSWEEP_API_KEYS env var is empty or malformed dict", flush=True)
    else:
        _cfg_keys = CFG.get("api_keys", {})
        if _cfg_keys:
            print(f"[STARTUP] API keys loaded from CFG (dev fallback): {len(_cfg_keys)} key(s)", flush=True)
        else:
            print(f"[STARTUP] WARNING: No API keys found in env or CFG. /api/v1/match will reject all requests with 500.", flush=True)
except Exception as _e:
    print(f"[STARTUP] WARNING: API key config check failed: {_e}", flush=True)

# ============================================================
# Vertical config (domain-specific: keys, hardware, etc.)
# ============================================================

from vertical_loader import (
    load_vertical, get_vertical, get_branding, get_categories,
    get_category_list, get_field_defs, get_field_ids,
    parse_all_fields, compute_field_penalty, get_silhouette_type,
    is_style_detection_enabled, get_category_family,get_db_root,
)

VERTICAL = load_vertical(str(CFG.get("vertical", "keys")), app.root_path)


@app.context_processor
def inject_vertical():
    """Make vertical branding and field definitions available in all templates."""
    branding = get_branding()
    raw_ui = branding.get("ui_text", {})
    ui_text = {
        "how_to_title": raw_ui.get("how_to_title", "How to identify your product"),
        "step1_text": raw_ui.get("step1_text", "If you have a <strong>product code</strong>, enter it below — or search by <strong>brand name</strong>"),
        "step1_placeholder": raw_ui.get("step1_placeholder", "e.g. product code or brand…"),
        "step2_text": raw_ui.get("step2_text", "Or <strong>upload / take photos</strong> of the product — fill in the details below for better results"),
        "progress_subtitle": raw_ui.get("progress_subtitle", "Analysing your product against the database…"),
        "guide_tips": raw_ui.get("guide_tips", [
            "Fill most of the frame with the product (avoid lots of background).",
            "Use good lighting — avoid heavy shadows or glare.",
            "Keep the product flat and straight if possible.",
            "Set the details above if known — this significantly narrows the search."
        ]),
        "feedback_correct_prompt": raw_ui.get("feedback_correct_prompt", "Was the correct product in these results?"),
        "feedback_notfound_prompt": raw_ui.get("feedback_notfound_prompt", "Which product was it? (enter SKU or leave blank if unknown)"),
        "feedback_sku_placeholder": raw_ui.get("feedback_sku_placeholder", "e.g. SKU code"),
        "db_sync_title": raw_ui.get("db_sync_title", "DB Sync"),
        "db_sync_desc": raw_ui.get("db_sync_desc", "Imports new images from your synced database folder."),
        "db_sync_button": raw_ui.get("db_sync_button", "Sync from DB"),
        "db_sync_confirm": raw_ui.get("db_sync_confirm", "Sync new images into MatchIT database?"),
    }
    return {
        "vbrand": branding,
        "vcategories": get_category_list(),
        "vfields": get_field_defs(),
        "vcategory_map": get_categories(),
        "ui_text": ui_text,
    }


@app.context_processor
def inject_footer_stats():
    from flask import g as _g
    from datetime import datetime
    if not hasattr(_g, "_cached_stats"):
        try:
            _s = _load_stats()
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            # If the stored today_date doesn't match today's UTC date,
            # the today_scans value is stale — return 0 instead.
            if _s.get("today_date") == today_str:
                today_count = _s.get("today_scans", 0)
            else:
                today_count = 0
            _g._cached_stats = (_s.get("total_scans", 0), today_count)
        except Exception:
            _g._cached_stats = (0, 0)
    _ft, _fd = _g._cached_stats
    print(f"[SSR-FOOTER] total={_ft} today={_fd}", flush=True)
    return {"footer_total_scans": _ft, "footer_today_scans": _fd}


@app.context_processor
def inject_scan_state():
    """
    Inject GS_SCAN_STATE into every SSR page so the frontend knows the user's
    current scan allowance without an extra round-trip. FAIL-OPEN: on any error
    returns an empty dict so the JS fallback path takes over.
    Task 9c Phase E.
    """
    try:
        import db as _db
        import urllib.parse as _up
        from flask import request as _req, g as _g
        if hasattr(_g, "_cached_scan_state"):
            return {"gs_scan_state": _g._cached_scan_state}
        ua   = _req.user_agent.string
        lang = _req.headers.get("Accept-Language", "")
        addr = _req.headers.get("CF-Connecting-IP",
               _req.headers.get("X-Forwarded-For", _req.remote_addr or ""))
        server_fp = _db.compute_server_fingerprint(ua, lang, addr)
        device_id = _req.cookies.get("matchit_device_id_v1") or None
        code_raw  = _req.cookies.get("gs_access_code", "")
        code      = _up.unquote(code_raw).strip().upper() or None
        subs_for_state = _load_subs()
        tier      = _db.resolve_tier_from_code(code, CFG.get("premium_codes", []), subs_for_state)
        if tier in {"legacy", "lifetime"}:
            # Truly exempt — no cap, no counter
            state = {"allowed": True, "reason": "premium_exempt", "tier": tier,
                     "count": None, "remaining": None, "limit": None}
        elif tier in {"monthly", "annual"}:
            # Capped premium — read tier usage state (never increments)
            tier_state = None
            try:
                tier_state = _db.read_tier_state(code, tier, subs_for_state)
            except Exception as _te:
                app.logger.debug(f"[inject_scan_state] tier_state error: {_te}")
            try:
                _topup_r = _db.read_topup_credits(server_fp, device_id)
                _topup_rem = _topup_r.get("credits", 0) if _topup_r.get("ok") else 0
            except Exception:
                _topup_rem = 0
            if tier_state:
                tier_allowed = tier_state["tier_remaining"] > 0
                state = {
                    "allowed":           tier_allowed,
                    "reason":            "tier_within_limit" if tier_allowed else "tier_limit_exceeded",
                    "tier":              tier,
                    "count":             tier_state["tier_used"],
                    "remaining":         tier_state["tier_remaining"],
                    "limit":             tier_state["tier_limit"],
                    "tier_used":         tier_state["tier_used"],
                    "tier_limit":        tier_state["tier_limit"],
                    "tier_period_end":        tier_state["tier_period_end"],
                    "tier_warned_80pct":      tier_state["tier_warned_80pct"],
                    "tier_transition_warned": tier_state["tier_transition_warned"],
                    "tier_remaining":         tier_state["tier_remaining"],
                    "topup_remaining":        _topup_rem,
                }
            else:
                # Fallback if tier_state unavailable — fail open
                state = {"allowed": True, "reason": "premium_exempt", "tier": tier,
                         "count": None, "remaining": None, "limit": None,
                         "topup_remaining": _topup_rem}
        else:
            read_result = _db.read_free_scans(server_fp, device_id)
            count = read_result.get("count", 0) if read_result.get("ok") else 0
            remaining = max(0, _db.FREE_TIER_MONTHLY_LIMIT - count)
            allowed = count < _db.FREE_TIER_MONTHLY_LIMIT
            topup = _db.read_topup_credits(server_fp, device_id)
            topup_rem = topup.get("credits", 0) if topup.get("ok") else 0
            if not allowed:
                allowed = topup_rem > 0
            state = {
                "allowed":        allowed,
                "reason":         "premium_exempt" if tier else ("free_within_limit" if allowed else "free_limit_exceeded"),
                "tier":           tier or "free",
                "count":          count,
                "remaining":      remaining,
                "limit":          _db.FREE_TIER_MONTHLY_LIMIT,
                "topup_remaining": topup_rem,
            }
        _g._cached_scan_state = state
        return {"gs_scan_state": state}
    except Exception as _exc:
        app.logger.debug(f"[inject_scan_state] fail-open: {_exc}")
        return {"gs_scan_state": {}}


    """
MARKETPLACE ROUTE — Add to app.py (updated with barcode + text-only search)
============================================================================
SETUP:
1. Copy marketplace.py into your MatchIT root folder
2. Copy marketplace.html into your templates/ folder
3. Add the import and route below to app.py
4. Add "serpapi_key": "YOUR_KEY" to config.json
5. pip install pyzbar   (optional — enables barcode/QR scanning)
6. Restart app.py

NOTE on pyzbar: On Windows you may also need the ZBar DLL.
  If pyzbar is not installed, barcode detection is simply skipped —
  everything else works fine.
"""

"""
MARKETPLACE ROUTE — Add to app.py (v3: Google Lens powered)
============================================================
SETUP:
1. Copy marketplace.py into your MatchIT root folder (replace old version)
2. Copy marketplace.html into your templates/ folder (replace old version)
3. Update the import line in app.py (see below)
4. Update the route in app.py (see below)
5. "serpapi_key" must be in config.json
6. Restart app.py
"""

"""
MARKETPLACE ROUTE v4 — AI-powered visual product search
========================================================
IMPORT LINE (update in app.py):
  from marketplace import marketplace_search, auto_classify_product, detect_barcode, barcode_to_search_query, build_search_query

ROUTE: Replace the existing marketplace route in app.py with this one.
"""

@app.route("/marketplace", methods=["GET", "POST"])
def marketplace():
    if request.method == "GET":
        return render_template("marketplace.html")

    manual_query = request.form.get("manual_query", "").strip()
    query_category = request.form.get("key_type", "").strip().upper()
    query_profile = parse_all_fields(dict(request.form))

    def _pick_first_file(key):
        try:
            items = request.files.getlist(key)
        except Exception:
            items = []
        for f in items:
            if f and getattr(f, "filename", ""):
                return f
        return None

    up1 = _pick_first_file("query_image")
    query_path = None
    query_filename = None
    barcode_info = None

    if up1 is not None:
        query_id = str(uuid.uuid4())
        import os
        _localappdata = os.environ.get("LOCALAPPDATA", "")
        if _localappdata == "/modal_data":
            query_dir = Path("/modal_data") / "query"
        else:
            query_dir = Path(app.root_path) / "static" / "query"
        query_dir.mkdir(parents=True, exist_ok=True)
        query_filename = f"{query_id}.jpg"
        query_path = query_dir / query_filename
        try:
            up1.save(str(query_path))
            normalize_uploaded_image(str(query_path))
        except Exception as e:
            return render_template("marketplace.html", error=f"Failed to save image: {e}")

        barcode_info = detect_barcode(str(query_path))

    elif not manual_query:
        return render_template("marketplace.html", error="Please upload an image or enter a search query.")

    # Build text search terms for barcode/manual/category input
    if barcode_info and barcode_info.get("found"):
        search_terms = barcode_to_search_query(barcode_info)
    elif manual_query:
        search_terms = manual_query
    elif query_category:
        search_terms = build_search_query(query_category, query_profile, get_categories())
    else:
        search_terms = ""  # Let AI classification fill this in

    api_key = os.environ.get("SERPAPI_API_KEY", "") or CFG.get("serpapi_key", "")
    if not api_key:
        return render_template("marketplace.html",
                               error="SerpAPI key not configured. Add 'serpapi_key' to config.json.")

    try:
        result = marketplace_search(
            query_image_path=str(query_path) if query_path else None,
            search_terms=search_terms,
            api_key=api_key,
            embedder=get_embedder(),
            max_results=10,
            num_fetch=15,
        )
    except Exception as e:
        current_app.logger.exception("Marketplace search failed")
        return render_template("marketplace.html",
                               error=f"Search failed: {e}",
                               query_filename=query_filename)

    return render_template("marketplace.html",
                           results=result.get("results", []),
                           classification=result.get("classification"),
                           search_query=result.get("search_query", ""),
                           search_mode=result.get("search_mode", "text"),
                           timings=result.get("timings", {}),
                           query_filename=query_filename,
                           barcode_info=barcode_info)
# ============================================================
# Admin (simple session flag)
# ============================================================


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def admin_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


def check_admin_password(pw: str) -> bool:
    expected = os.environ.get("ADMIN_PASSWORD") or CFG.get("admin_password", "")
    return (pw or "") == str(expected)


# ============================================================
# DB
# ============================================================


def init_db():
    db_path = get_images_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images(
                image_id TEXT PRIMARY KEY,
                sku TEXT,
                original_filename TEXT,
                path TEXT,
                added_at TEXT,
                embedding BLOB
            );
        """
        )

        cols = [r[1] for r in conn.execute("PRAGMA table_info(images);").fetchall()]
        if "description" not in cols:
            conn.execute("ALTER TABLE images ADD COLUMN description TEXT;")
        if "flagged" not in cols:
            conn.execute("ALTER TABLE images ADD COLUMN flagged INTEGER DEFAULT 0;")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_sku ON images(sku);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_added_at ON images(added_at);")

        # Feedback table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS match_feedback(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submitted_at TEXT NOT NULL,
                query_filename TEXT,
                query_filename_2 TEXT,
                confirmed_sku TEXT,
                confirmed_rank INTEGER,
                result_skus TEXT,
                verdict TEXT NOT NULL,
                is_test INTEGER DEFAULT 0
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_sku ON match_feedback(confirmed_sku);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_verdict ON match_feedback(verdict);")

        # Migration: add is_test column if missing
        fb_cols = [r[1] for r in conn.execute("PRAGMA table_info(match_feedback);").fetchall()]
        if "is_test" not in fb_cols:
            conn.execute("ALTER TABLE match_feedback ADD COLUMN is_test INTEGER DEFAULT 0;")

        # Match history table (stores every match for user-visible history)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS match_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matched_at TEXT NOT NULL,
                query_filename TEXT,
                query_filename_2 TEXT,
                top_sku TEXT,
                top_score REAL,
                top_confidence REAL,
                result_skus TEXT,
                low_cert INTEGER DEFAULT 0
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_at ON match_history(matched_at);")

        conn.commit()
    finally:
        conn.close()


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def normalize_uploaded_image(in_path: str) -> None:
    """
    Normalise uploads so mobile/PC behave consistently:
    - Applies EXIF orientation
    - Converts to RGB
    - Re-saves as a clean JPEG
    """
    try:
        with Image.open(in_path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(in_path, format="JPEG", quality=95, optimize=True)
    except Exception:
        current_app.logger.exception(f"[MATCH] normalize_uploaded_image failed for {in_path}")


# ============================================================
# Embedding cache
# ============================================================

_EMBEDDER = None
_ROWS_CACHED: Optional[List[Tuple[str, str, str, np.ndarray]]] = None
_CACHE_LOADED_AT: Optional[str] = None

DESC_BY_IMAGE_ID: Dict[str, str] = {}
ORIG_BY_IMAGE_ID: Dict[str, str] = {}
VIEW_BY_IMAGE_ID: Dict[str, str] = {}
PATH_BY_IMAGE_ID: Dict[str, str] = {}

# Style cache
SKU_TYPE: Dict[str, str] = {}

# Length cache
SKU_LEN: Dict[str, float] = {}

# SKU mean embedding cache (front and back averaged separately)
SKU_MEAN_FRONT: Dict[str, np.ndarray] = {}
SKU_MEAN_BACK:  Dict[str, np.ndarray] = {}

# ── PERF: Pre-stacked matrices for vectorized similarity ──
# Built during load_embedding_cache. Rows are L2-normalized.
FRONT_MATRIX: Optional[np.ndarray] = None   # (N_front, dim)
FRONT_INFO: List[Tuple[str, str, str, str]] = []   # (image_id, sku, orig_name, desc)
BACK_MATRIX: Optional[np.ndarray] = None    # (N_back, dim)
BACK_INFO: List[Tuple[str, str, str, str]] = []    # (image_id, sku, orig_name, desc)


def _image_id_for_sku(sku: str) -> Optional[str]:
    """Look up the front image UUID for a SKU — tries memory cache first, falls back to DB."""
    for image_id, s, *_ in FRONT_INFO:
        if s == sku:
            return image_id
    # FRONT_INFO not yet loaded (cold start OCR-first) — query DB directly
    _db_candidates = []
    try:
        from vertical_loader import get_db_root as _gdr
        _db_root = _gdr()
        if _db_root:
            _db_candidates.append(os.path.join(_db_root, "images.db"))
    except Exception:
        pass
    # Always try the known Modal volume path as fallback
    _db_candidates.append("/modal_data/MatchITv2_ProductMatch_Data/cards/images.db")
    for _db_path in _db_candidates:
        try:
            if os.path.exists(_db_path):
                import sqlite3
                conn = sqlite3.connect(_db_path)
                row = conn.execute(
                    "SELECT image_id FROM images WHERE sku=? LIMIT 1",
                    (sku,)
                ).fetchone()
                conn.close()
                if row:
                    return row[0]
        except Exception as e:
            print(f"[IMAGE_ID] DB lookup failed for {_db_path}: {e}", flush=True)
    return None


def get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        from feature_extractor import ImageEmbedder
        _EMBEDDER = ImageEmbedder()
    return _EMBEDDER


def _preload_primary_embedder():
    """Background-load the primary CLIP embedder so first query is fast."""
    try:
        with app.app_context():
            emb = get_embedder()
            print(f"[STARTUP] Primary embedder ready: {emb.backend_name}", flush=True)
    except Exception as e:
        print(f"[STARTUP] Primary embedder preload failed: {e}", flush=True)


def _infer_view_from_orig(orig_name: str, sku: str = "") -> str:
    s = (orig_name or "").strip()
    if not s:
        return ""

    s2 = s.replace("\\", "/")
    base = s2.rsplit("/", 1)[-1]
    stem = os.path.splitext(base)[0]
    up = stem.upper()

    if up.endswith("_FRONT"):
        return "FRONT"
    if up.endswith("_BACK"):
        return "BACK"
    if up.endswith("_SIDE_C") or up.endswith("_SIDEC"):
        return "SIDE_C"

    up_full = s2.upper()
    if "/SKU_FRONT/" in up_full or up_full.endswith("/SKU_FRONT"):
        return "FRONT"
    if "/SKU_BACK/" in up_full or up_full.endswith("/SKU_BACK"):
        return "BACK"
    if "/SKU_SIDE_C/" in up_full or up_full.endswith("/SKU_SIDE_C"):
        return "SIDE_C"

    if sku:
        sku_u = sku.strip().upper()
        if f"{sku_u}_FRONT" in up_full:
            return "FRONT"
        if f"{sku_u}_BACK" in up_full:
            return "BACK"
        if f"{sku_u}_SIDE_C" in up_full:
            return "SIDE_C"

    return ""


def _abs_image_path_from_db_path(p: str) -> Optional[str]:
    p = (p or "").strip()
    if not p:
        return None
    db_path = get_images_db_path()
    file_path = p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(db_path), p))
    if not os.path.exists(file_path):
        return None
    return file_path


# ============================================================
# STYLE DETECTION (silhouette-based)
# ============================================================


def _downscale_for_style(im: Image.Image, max_side: int = 420) -> Image.Image:
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    m = max(w, h)
    if m > max_side:
        s = max_side / float(m)
        im = im.resize((max(1, int(round(w * s))), max(1, int(round(h * s)))), Image.BILINEAR)
    return im


def _make_mask_white_bg(im: Image.Image) -> np.ndarray:
    arr = np.asarray(im, dtype=np.uint8)
    gray = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]).astype(np.float32)
    p95 = float(np.percentile(gray, 95))
    thr = min(245.0, max(150.0, p95 - 18.0))
    mask = gray < thr

    m = mask.astype(np.uint8)
    mp = np.pad(m, ((1, 1), (1, 1)), mode="edge")
    neigh = (
        mp[:-2, :-2]
        + mp[:-2, 1:-1]
        + mp[:-2, 2:]
        + mp[1:-1, :-2]
        + mp[1:-1, 1:-1]
        + mp[1:-1, 2:]
        + mp[2:, :-2]
        + mp[2:, 1:-1]
        + mp[2:, 2:]
    )
    mask2 = neigh >= 3
    return mask2


def _bbox_from_mask(mask: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    ys, xs = np.where(mask)
    if xs.size == 0 or ys.size == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return x0, y0, x1, y1


def detect_key_type_from_path(image_path: str) -> str:
    """
    Returns: "MORTICE" | "DOUBLE_SIDED" | "CYLINDER" | "UNKNOWN"
    """
    try:
        with Image.open(image_path) as im0:
            im = _downscale_for_style(im0, max_side=420)
    except Exception:
        return "UNKNOWN"

    mask = _make_mask_white_bg(im)
    bb = _bbox_from_mask(mask)
    if not bb:
        return "UNKNOWN"

    x0, y0, x1, y1 = bb
    w = max(1, x1 - x0 + 1)
    h = max(1, y1 - y0 + 1)

    aspect = float(h) / float(w)
    mort_aspect = float(CFG.get("style_mortice_aspect_thresh", 3.2) or 3.2)

    if aspect >= mort_aspect:
        sub = mask[y0 : y1 + 1, x0 : x1 + 1]
        H, W = sub.shape
        yb0 = int(round(H * 0.78))
        bottom = sub[yb0:, :]
        mid = sub[int(round(H * 0.40)) : int(round(H * 0.60)), :]

        def _row_widths(m2: np.ndarray) -> List[int]:
            widths = []
            for r in m2:
                xs = np.where(r)[0]
                if xs.size == 0:
                    continue
                widths.append(int(xs.max() - xs.min() + 1))
            return widths

        bw = _row_widths(bottom)
        mw = _row_widths(mid)
        if bw and mw:
            bit_ratio = (max(bw) / max(1.0, float(np.median(mw))))
            if bit_ratio >= 1.25:
                return "MORTICE"
        if aspect >= (mort_aspect + 0.4):
            return "MORTICE"

    sub = mask[y0 : y1 + 1, x0 : x1 + 1]
    H, W = sub.shape

    yA = int(round(H * 0.30))
    yB = int(round(H * 0.92))
    if yB <= yA + 10:
        yA = 0
        yB = H

    strip_px = int(CFG.get("style_edge_strip_px", 18) or 18)
    strip_px = max(8, min(strip_px, max(8, W // 4)))

    rows = sub[yA:yB, :]
    if rows.size == 0:
        return "UNKNOWN"

    left_counts = []
    right_counts = []

    for r in rows:
        xs = np.where(r)[0]
        if xs.size == 0:
            continue
        xl = int(xs.min())
        xr = int(xs.max())

        left_strip = r[max(0, xl - 0) : min(W, xl + strip_px)]
        right_strip = r[max(0, xr - strip_px + 1) : min(W, xr + 1)]
        left_counts.append(int(left_strip.sum()))
        right_counts.append(int(right_strip.sum()))

    if not left_counts or not right_counts:
        return "UNKNOWN"

    lc = float(np.mean(left_counts))
    rc = float(np.mean(right_counts))
    sym = min(lc, rc) / max(1e-6, max(lc, rc))

    sym_thresh = float(CFG.get("style_symmetry_thresh", 0.62) or 0.62)
    if sym >= sym_thresh:
        return "DOUBLE_SIDED"

    return "CYLINDER"


def detect_key_length_ratio_from_path(image_path: str) -> Optional[float]:
    try:
        max_side = int(CFG.get("length_downscale_max_side", 420) or 420)
        with Image.open(image_path) as im0:
            im = _downscale_for_style(im0, max_side=max_side)
    except Exception:
        return None

    mask = _make_mask_white_bg(im)
    bb = _bbox_from_mask(mask)
    if not bb:
        return None

    _x0, y0, _x1, y1 = bb
    bbox_h = float(max(1, (y1 - y0 + 1)))
    img_h = float(max(1, im.size[1]))
    r = bbox_h / img_h
    r = float(max(0.05, min(0.99, r)))
    return r


def _build_sku_type_cache():
    global SKU_TYPE
    SKU_TYPE = {}

    rows = get_cached_rows(force=False)
    if not rows:
        return

    front_id_by_sku: Dict[str, str] = {}
    for image_id, sku, orig_name, _v in rows:
        sku = (sku or "").strip()
        if not sku:
            continue
        view = VIEW_BY_IMAGE_ID.get(str(image_id), "") or _infer_view_from_orig(orig_name or "", sku)
        view = (view or "").upper().strip()
        if view == "FRONT" and sku not in front_id_by_sku:
            front_id_by_sku[sku] = str(image_id)

    for sku, image_id in front_id_by_sku.items():
        p = _abs_image_path_from_db_path(PATH_BY_IMAGE_ID.get(image_id, "") or "")
        if not p:
            SKU_TYPE[sku] = "UNKNOWN"
            continue
        SKU_TYPE[sku] = detect_key_type_from_path(p)


def _build_sku_length_cache():
    global SKU_LEN
    SKU_LEN = {}

    rows = get_cached_rows(force=False)
    if not rows:
        return

    front_id_by_sku: Dict[str, str] = {}
    for image_id, sku, orig_name, _v in rows:
        sku = (sku or "").strip()
        if not sku:
            continue
        view = VIEW_BY_IMAGE_ID.get(str(image_id), "") or _infer_view_from_orig(orig_name or "", sku)
        view = (view or "").upper().strip()
        if view == "FRONT" and sku not in front_id_by_sku:
            front_id_by_sku[sku] = str(image_id)

    for sku, image_id in front_id_by_sku.items():
        p = _abs_image_path_from_db_path(PATH_BY_IMAGE_ID.get(image_id, "") or "")
        if not p:
            continue
        r = detect_key_length_ratio_from_path(p)
        if r is None:
            continue
        SKU_LEN[sku] = float(r)


# ============================================================
# Cache load
# ============================================================


# Set of JP set IDs that actually have images in the CLIP index (built from
# FRONT_INFO after each cache load). Used by /api/jp-denom-check to tell the
# client which JP sets are matchable. Module-global so the endpoint can read it.
_IMAGED_JP_SETS = frozenset()


def _rebuild_imaged_jp_sets():
    global _IMAGED_JP_SETS
    _IMAGED_JP_SETS = frozenset(
        "jpn-" + "-".join(sku.split('-')[1:-1])
        for _, sku, _, _ in FRONT_INFO
        if sku and sku.startswith('jpn-') and len(sku.split('-')) >= 3
    )
    print(f"[JP-SETS] Built imaged JP set index: {len(_IMAGED_JP_SETS)} sets", flush=True)


def load_embedding_cache(force: bool = False):
    global _ROWS_CACHED, _CACHE_LOADED_AT
    global DESC_BY_IMAGE_ID, ORIG_BY_IMAGE_ID, VIEW_BY_IMAGE_ID, PATH_BY_IMAGE_ID, SKU_TYPE, SKU_LEN
    global SKU_MEAN_FRONT, SKU_MEAN_BACK
    global FRONT_MATRIX, FRONT_INFO, BACK_MATRIX, BACK_INFO

    if isinstance(_ROWS_CACHED, list) and len(_ROWS_CACHED) > 0 and not force:
        return _ROWS_CACHED

    init_db()

    # ── FAST PATH: Load from numpy cache if available and newer than DB ──
    import time as _cache_time
    _cache_dir = os.path.join(get_data_dir(), "npy_cache")
    _cache_meta_path = os.path.join(_cache_dir, "cache_meta.json")
    _cache_front_path = os.path.join(_cache_dir, "front_matrix.npy")
    _cache_back_path = os.path.join(_cache_dir, "back_matrix.npy")
    _cache_data_path = os.path.join(_cache_dir, "cache_data.json")

    db_path = get_images_db_path()
    db_mtime = os.path.getmtime(db_path) if os.path.exists(db_path) else 0

    if (not force
        and os.path.exists(_cache_meta_path)
        and os.path.exists(_cache_front_path)
        and os.path.exists(_cache_data_path)):
        try:
            cache_mtime = os.path.getmtime(_cache_meta_path)
            if cache_mtime > db_mtime:
                _t0 = _cache_time.time()
                print("[CACHE] Loading from numpy fast cache...", flush=True)

                meta = json.loads(Path(_cache_meta_path).read_text(encoding="utf-8"))
                data = json.loads(Path(_cache_data_path).read_text(encoding="utf-8"))

                FRONT_MATRIX = np.load(_cache_front_path)
                if os.path.exists(_cache_back_path):
                    BACK_MATRIX = np.load(_cache_back_path)
                else:
                    BACK_MATRIX = None

                FRONT_INFO = [tuple(x) for x in data["front_info"]]
                BACK_INFO = [tuple(x) for x in data["back_info"]]
                DESC_BY_IMAGE_ID = data["desc_by_id"]
                ORIG_BY_IMAGE_ID = data["orig_by_id"]
                VIEW_BY_IMAGE_ID = data["view_by_id"]
                PATH_BY_IMAGE_ID = data["path_by_id"]

                # Rebuild rows_out from front+back info (lightweight)
                rows_out = []
                # We need embeddings for rows_out but they're in the matrices
                # Just store minimal info — rows_out is only used for get_cached_rows
                # which feeds _build_sku_type_cache etc.
                all_ids = set()
                for image_id, sku, orig_name, desc in FRONT_INFO:
                    if image_id not in all_ids:
                        rows_out.append((image_id, sku, orig_name, np.zeros(1, dtype=np.float32)))
                        all_ids.add(image_id)
                for image_id, sku, orig_name, desc in BACK_INFO:
                    if image_id not in all_ids:
                        rows_out.append((image_id, sku, orig_name, np.zeros(1, dtype=np.float32)))
                        all_ids.add(image_id)

                # Rebuild SKU mean caches from matrices
                SKU_MEAN_FRONT = {}
                SKU_MEAN_BACK = {}
                _front_vecs_by_sku = {}
                for idx, (image_id, sku, orig_name, desc) in enumerate(FRONT_INFO):
                    if sku:
                        _front_vecs_by_sku.setdefault(sku, []).append(FRONT_MATRIX[idx])
                for sku, vecs in _front_vecs_by_sku.items():
                    m = np.mean(np.stack(vecs), axis=0).astype(np.float32)
                    SKU_MEAN_FRONT[sku] = m / (float(np.linalg.norm(m) + 1e-12))

                if BACK_MATRIX is not None:
                    _back_vecs_by_sku = {}
                    for idx, (image_id, sku, orig_name, desc) in enumerate(BACK_INFO):
                        if sku:
                            _back_vecs_by_sku.setdefault(sku, []).append(BACK_MATRIX[idx])
                    for sku, vecs in _back_vecs_by_sku.items():
                        m = np.mean(np.stack(vecs), axis=0).astype(np.float32)
                        SKU_MEAN_BACK[sku] = m / (float(np.linalg.norm(m) + 1e-12))

                _ROWS_CACHED = rows_out
                _CACHE_LOADED_AT = meta.get("loaded_at", datetime.utcnow().isoformat())
                SKU_TYPE = {}
                SKU_LEN = {}

                _t1 = _cache_time.time()
                print(f"[CACHE] Fast loaded {len(FRONT_INFO)} FRONT + {len(BACK_INFO)} BACK in {_t1-_t0:.2f}s", flush=True)
                _rebuild_imaged_jp_sets()


                try:
                    from vertical_loader import get_vertical as _gv
                    if _gv().get("id") == "keys":
                        _build_sku_length_cache()
                except Exception:
                    current_app.logger.exception("[LENGTH] Failed building SKU length cache")

                return _ROWS_CACHED
        except Exception as e:
            print(f"[CACHE] Fast cache load failed, falling back to SQLite: {e}", flush=True)

    # ── NORMAL PATH: Load from SQLite ──
    _t_sql_start = _cache_time.time()

    rows_out = []
    DESC_BY_IMAGE_ID = {}
    ORIG_BY_IMAGE_ID = {}
    VIEW_BY_IMAGE_ID = {}
    PATH_BY_IMAGE_ID = {}
    SKU_TYPE = {}
    SKU_LEN = {}
    SKU_MEAN_FRONT = {}
    SKU_MEAN_BACK  = {}
    FRONT_MATRIX = None
    FRONT_INFO = []
    BACK_MATRIX = None
    BACK_INFO = []

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT image_id, sku, original_filename, description, path, embedding FROM images"
        ).fetchall()

    _front_vecs = {}
    _back_vecs  = {}
    _front_mat_vecs = []
    _front_mat_info = []
    _back_mat_vecs = []
    _back_mat_info = []

    for image_id, sku, orig_name, desc, path, blob in rows:
        if blob is None:
            continue
        emb = from_blob(blob)
        rows_out.append((str(image_id), str(sku or ""), str(orig_name or ""), emb))

        key = str(image_id)
        DESC_BY_IMAGE_ID[key] = (desc or "")
        ORIG_BY_IMAGE_ID[key] = (orig_name or "")
        view = _infer_view_from_orig(orig_name or "", sku or "")
        VIEW_BY_IMAGE_ID[key] = view
        PATH_BY_IMAGE_ID[key] = str(path or "")

        sku_clean = str(sku or "").strip()
        v = np.asarray(emb, dtype=np.float32).reshape(-1)
        v_norm = v / (float(np.linalg.norm(v) + 1e-12))

        if sku_clean:
            if view == "FRONT":
                _front_vecs.setdefault(sku_clean, []).append(v_norm)
            elif view == "BACK":
                _back_vecs.setdefault(sku_clean, []).append(v_norm)

        if view == "FRONT":
            _front_mat_vecs.append(v_norm)
            _front_mat_info.append((str(image_id), str(sku or "").strip(), str(orig_name or ""), (desc or "")))
        elif view == "BACK":
            _back_mat_vecs.append(v_norm)
            _back_mat_info.append((str(image_id), str(sku or "").strip(), str(orig_name or ""), (desc or "")))

    for sku, vecs in _front_vecs.items():
        m = np.mean(np.stack(vecs, axis=0), axis=0).astype(np.float32)
        SKU_MEAN_FRONT[sku] = m / (float(np.linalg.norm(m) + 1e-12))
    for sku, vecs in _back_vecs.items():
        m = np.mean(np.stack(vecs, axis=0), axis=0).astype(np.float32)
        SKU_MEAN_BACK[sku] = m / (float(np.linalg.norm(m) + 1e-12))

    if _front_mat_vecs:
        FRONT_MATRIX = np.stack(_front_mat_vecs).astype(np.float32)
    else:
        FRONT_MATRIX = None
    FRONT_INFO = _front_mat_info
    _rebuild_imaged_jp_sets()

    if _back_mat_vecs:
        BACK_MATRIX = np.stack(_back_mat_vecs).astype(np.float32)
    else:
        BACK_MATRIX = None
    BACK_INFO = _back_mat_info

    _ROWS_CACHED = rows_out
    _CACHE_LOADED_AT = datetime.utcnow().isoformat()

    _t_sql_end = _cache_time.time()
    print(f"[CACHE] Loaded {len(rows_out)} embeddings from SQLite in {_t_sql_end-_t_sql_start:.2f}s "
          f"(FRONT={len(_front_mat_vecs)}, BACK={len(_back_mat_vecs)})", flush=True)

    # ── Save numpy fast cache for next startup ──
    try:
        os.makedirs(_cache_dir, exist_ok=True)

        if FRONT_MATRIX is not None:
            np.save(_cache_front_path, FRONT_MATRIX)
        if BACK_MATRIX is not None:
            np.save(_cache_back_path, BACK_MATRIX)

        cache_data = {
            "front_info": [list(x) for x in FRONT_INFO],
            "back_info": [list(x) for x in BACK_INFO],
            "desc_by_id": DESC_BY_IMAGE_ID,
            "orig_by_id": ORIG_BY_IMAGE_ID,
            "view_by_id": VIEW_BY_IMAGE_ID,
            "path_by_id": PATH_BY_IMAGE_ID,
        }
        Path(_cache_data_path).write_text(json.dumps(cache_data), encoding="utf-8")

        cache_meta = {"loaded_at": _CACHE_LOADED_AT, "count": len(rows_out)}
        Path(_cache_meta_path).write_text(json.dumps(cache_meta), encoding="utf-8")

        print(f"[CACHE] Numpy fast cache saved to {_cache_dir}", flush=True)
    except Exception as e:
        print(f"[CACHE] Failed to save numpy cache: {e}", flush=True)

    # ── Dimension mismatch check ──
    if rows_out:
        db_dim = rows_out[0][3].shape[0]
        try:
            emb_obj = get_embedder()
            expected_dim = getattr(emb_obj, "embedding_dim", None)
            if expected_dim and db_dim != expected_dim:
                print(
                    f"\n*** WARNING: DB embeddings are {db_dim}-dim but current "
                    f"embedder ({getattr(emb_obj, 'backend_name', '?')}) produces {expected_dim}-dim.\n"
                    f"*** You MUST re-embed all images (Admin → Re-embed All) before matching.\n",
                    flush=True,
                )
        except Exception:
            pass

    try:
        from vertical_loader import get_vertical as _gv
        if _gv().get("id") == "keys":
            _build_sku_type_cache()
    except Exception:
        current_app.logger.exception("[STYLE] Failed building SKU type cache")

    try:
        from vertical_loader import get_vertical as _gv
        if _gv().get("id") == "keys":
            _build_sku_length_cache()
    except Exception:
        current_app.logger.exception("[LENGTH] Failed building SKU length cache")


def get_cached_rows(force: bool = False) -> List[Tuple[str, str, str, np.ndarray]]:
    rows = _ROWS_CACHED
    if rows is None or force:
        load_embedding_cache(force=True)
        rows = _ROWS_CACHED
    return rows or []


# ============================================================
# CSV helpers
# ============================================================


def parse_csv_mapping(csv_bytes: bytes) -> dict:
    mapping = {}
    try:
        lines = csv_bytes.decode("utf-8-sig", errors="ignore").splitlines()
        if not lines:
            return mapping

        header = [h.strip().lower() for h in lines[0].split(",")]
        if "filename" not in header or "sku" not in header:
            return mapping

        idx_fn = header.index("filename")
        idx_sku = header.index("sku")
        idx_desc = header.index("description") if "description" in header else None

        for line in lines[1:]:
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) <= max(idx_fn, idx_sku):
                continue

            fn = parts[idx_fn]
            sku = parts[idx_sku]
            desc = ""
            if idx_desc is not None and len(parts) > idx_desc:
                desc = parts[idx_desc]

            if fn:
                mapping[fn] = {"sku": sku, "description": desc}
    except Exception:
        return mapping

    return mapping


def is_invalid_field(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    return s.lower() in {"unknown", "unk", "na", "n/a", "none", "-"}


# ============================================================
# Matching helpers
# ============================================================


def _softmax(scores_np: np.ndarray, temp: float) -> np.ndarray:
    x = (scores_np - float(np.max(scores_np))) / max(float(temp), 1e-6)
    p = np.exp(x)
    p = p / max(float(np.sum(p)), 1e-12)
    return p.astype(np.float32)


def _consistency_from_sims(sim_list, n=3, sigma=0.05):
    try:
        sims = sorted([float(s) for s in sim_list], reverse=True)
    except Exception:
        sims = []
    if len(sims) < 2:
        return 1.0, 0.0
    n = max(2, int(n))
    topn = sims[: min(n, len(sims))]
    std = float(np.std(topn))
    sigma = max(float(sigma), 1e-6)
    cons = float(np.exp(-((std * std) / (2.0 * sigma * sigma))))
    cons = max(0.0, min(1.0, cons))
    return cons, std


def _score_sku_from_sims(sims: List[float]) -> float:
    sims = [float(x) for x in sims]
    if not sims:
        return 0.0

    sims_sorted = sorted(sims, reverse=True)

    if len(sims_sorted) >= 3:
        s1, s2, s3 = sims_sorted[0], sims_sorted[1], sims_sorted[2]
        w1 = float(CFG.get("front_score_w1", 0.75) or 0.75)
        w2 = float(CFG.get("front_score_w2", 0.15) or 0.15)
        w3 = float(CFG.get("front_score_w3", 0.10) or 0.10)
        ws = w1 + w2 + w3
        if ws <= 1e-12:
            return float((s1 + s2 + s3) / 3.0)
        w1, w2, w3 = w1 / ws, w2 / ws, w3 / ws
        return float(w1 * s1 + w2 * s2 + w3 * s3)

    if len(sims_sorted) == 2:
        s1, s2 = sims_sorted[0], sims_sorted[1]
        w1 = float(CFG.get("front_score_w1_two", 0.80) or 0.80)
        w2 = float(CFG.get("front_score_w2_two", 0.20) or 0.20)
        ws = w1 + w2
        if ws <= 1e-12:
            return float(0.70 * s1 + 0.30 * s2)
        w1, w2 = w1 / ws, w2 / ws
        return float(w1 * s1 + w2 * s2)

    return float(sims_sorted[0])


def _embed_one_query(
    emb,
    query_path: str,
    *,
    multi_crop: bool = True,
    suppress_bg: bool = True,
    max_side: int = 1024,
) -> np.ndarray:
    try:
        try:
            v = emb.embed_path(
                str(query_path),
                multi_crop=multi_crop,
                suppress_bg=suppress_bg,
                max_side=max_side,
            )
        except TypeError:
            v = emb.embed_path(str(query_path), multi_crop=multi_crop, suppress_bg=suppress_bg)
    except Exception as e:
        raise RuntimeError(f"Embedder failed on {query_path}: {e}")

    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v) + 1e-12)
    return v / n


def _type_penalty_factor(query_type: str, sku_type: str) -> float:
    if not is_style_detection_enabled():
        return 1.0

    qt = (query_type or "UNKNOWN").upper().strip()
    st = (sku_type or "UNKNOWN").upper().strip()

    # Normalise via vertical's silhouette map (category → silhouette shape)
    qt = get_silhouette_type(qt) or qt
    st = get_silhouette_type(st) or st

    if qt not in {"CYLINDER", "DOUBLE_SIDED", "MORTICE"}:
        return 1.0

    if st == "UNKNOWN":
        return float(CFG.get("style_unknown_penalty", 0.995) or 0.995)

    if st == qt:
        return 1.0

    base = float(CFG.get("style_type_mismatch_penalty", 0.985) or 0.985)

    if (qt == "CYLINDER" and st == "DOUBLE_SIDED") or (qt == "DOUBLE_SIDED" and st == "CYLINDER"):
        base = min(base, float(CFG.get("style_cyl_vs_double_penalty", 0.975) or 0.975))

    if (qt == "MORTICE" and st != "MORTICE") or (st == "MORTICE" and qt != "MORTICE"):
        base = min(base, float(CFG.get("style_mortice_mismatch_penalty", 0.965) or 0.965))

    return max(0.90, min(1.0, float(base)))


def _apply_soft_type_penalty(*, query_type: str, sku: str, score: float, sim_disp: float) -> Tuple[float, float]:
    if not bool(CFG.get("style_gating_enabled", True)):
        return float(score), float(sim_disp)

    st = SKU_TYPE.get(sku, "UNKNOWN")
    fac = _type_penalty_factor(query_type, st)
    if fac >= 0.999999:
        return float(score), float(sim_disp)

    return float(score) * fac, float(sim_disp) * fac


def _apply_length_penalty(
    *,
    sku: str,
    score: float,
    sim_disp: float,
    query_len: Optional[float],
) -> Tuple[float, float]:
    if not bool(CFG.get("length_cue_enabled", True)):
        return float(score), float(sim_disp)

    if query_len is None:
        return float(score), float(sim_disp)

    sku_len = SKU_LEN.get(sku)
    if sku_len is None:
        return float(score), float(sim_disp)

    sigma = float(CFG.get("length_sigma", 0.055) or 0.055)
    sigma = max(1e-4, min(0.25, sigma))
    pmax = float(CFG.get("length_penalty_max", 0.03) or 0.03)
    pmax = max(0.0, min(0.20, pmax))

    diff = float(abs(float(query_len) - float(sku_len)))
    fac = float(np.exp(-((diff * diff) / (2.0 * sigma * sigma))))
    fac = max(0.0, min(1.0, fac))

    mult = float(1.0 - (pmax * (1.0 - fac)))
    return float(score) * mult, float(sim_disp) * mult


def _best_paired_score_swap_safe(
    *,
    sku: str,
    by_front_qf: Dict[str, List[Tuple[float, str, str, str]]],
    by_back_qb: Dict[str, List[Tuple[float, str, str, str]]],
    by_front_qb: Dict[str, List[Tuple[float, str, str, str]]],
    by_back_qf: Dict[str, List[Tuple[float, str, str, str]]],
    top_m_per_sku: int,
    w_b: float,
    min_term_w: float,
    back_min_best_sim: float,
) -> Tuple[float, float, int]:
    # Normal orientation
    f_items = sorted(by_front_qf.get(sku, []), reverse=True, key=lambda x: x[0])
    b_items = sorted(by_back_qb.get(sku, []), reverse=True, key=lambda x: x[0])

    f_top = f_items[:top_m_per_sku] if f_items else []
    b_top = b_items[:top_m_per_sku] if b_items else []

    f_sims = [float(x[0]) for x in f_top] if f_top else []
    b_sims = [float(x[0]) for x in b_top] if b_top else []

    f_score = _score_sku_from_sims(f_sims) if f_sims else 0.0
    b_score = _score_sku_from_sims(b_sims) if b_sims else 0.0

    best_front = float(f_top[0][0]) if f_top else 0.0
    best_back_raw = float(b_top[0][0]) if b_top else 0.0
    best_back_disp = float(best_back_raw)

    # BACK CLAMP: only affects back verify + display (NOT min-term)
    if best_back_raw < float(back_min_best_sim):
        b_score = 0.0
        best_back_disp = 0.0

    mterm = float(min(best_front, best_back_raw))
    normal = float(f_score) + (w_b * float(b_score)) + (min_term_w * mterm)
    normal_disp = float(best_front) + (w_b * float(best_back_disp)) + (min_term_w * mterm)

    # Swapped orientation
    f2_items = sorted(by_front_qb.get(sku, []), reverse=True, key=lambda x: x[0])
    b2_items = sorted(by_back_qf.get(sku, []), reverse=True, key=lambda x: x[0])

    f2_top = f2_items[:top_m_per_sku] if f2_items else []
    b2_top = b2_items[:top_m_per_sku] if b2_items else []

    f2_sims = [float(x[0]) for x in f2_top] if f2_top else []
    b2_sims = [float(x[0]) for x in b2_top] if b2_top else []

    f2_score = _score_sku_from_sims(f2_sims) if f2_sims else 0.0
    b2_score = _score_sku_from_sims(b2_sims) if b2_sims else 0.0

    best_front2 = float(f2_top[0][0]) if f2_top else 0.0
    best_back2_raw = float(b2_top[0][0]) if b2_top else 0.0
    best_back2_disp = float(best_back2_raw)

    if best_back2_raw < float(back_min_best_sim):
        b2_score = 0.0
        best_back2_disp = 0.0

    mterm2 = float(min(best_front2, best_back2_raw))
    swapped = float(f2_score) + (w_b * float(b2_score)) + (min_term_w * mterm2)
    swapped_disp = float(best_front2) + (w_b * float(best_back2_disp)) + (min_term_w * mterm2)

    if swapped > normal:
        return swapped, swapped_disp, 1
    return normal, normal_disp, 0


# ============================================================
# Profile pre-filter
# ============================================================

_SKU_PROFILES: Optional[dict] = None
_SKU_PROFILES_MTIME: float = 0.0


def _load_sku_profiles() -> dict:
    """Load sku_profiles.json — reloads automatically if file changes on disk."""
    global _SKU_PROFILES, _SKU_PROFILES_MTIME
    p = Path(app.root_path) / "sku_profiles.json"
    if not p.exists():
        _SKU_PROFILES = {}
        return _SKU_PROFILES
    try:
        mtime = p.stat().st_mtime
        if _SKU_PROFILES is not None and mtime == _SKU_PROFILES_MTIME:
            return _SKU_PROFILES  # unchanged — use cache
        # File is new or changed — reload
        _SKU_PROFILES = json.loads(p.read_text(encoding="utf-8"))
        _SKU_PROFILES_MTIME = mtime
        app.logger.info(f"[PROFILE] Loaded {len(_SKU_PROFILES)} SKU profiles from disk")
    except Exception as e:
        app.logger.warning(f"[PROFILE] Failed to load sku_profiles.json: {e}")
        _SKU_PROFILES = _SKU_PROFILES or {}
    return _SKU_PROFILES


_SKU_CROSSREFS: Optional[dict] = None
_SKU_CROSSREFS_MTIME: float = 0.0

def _load_sku_crossrefs() -> dict:
    """Load sku_crossrefs.json — reloads automatically if file changes on disk."""
    global _SKU_CROSSREFS, _SKU_CROSSREFS_MTIME
    p = Path(app.root_path) / "sku_crossrefs.json"
    if not p.exists():
        _SKU_CROSSREFS = {}
        return _SKU_CROSSREFS
    try:
        mtime = p.stat().st_mtime
        if _SKU_CROSSREFS is not None and mtime == _SKU_CROSSREFS_MTIME:
            return _SKU_CROSSREFS
        _SKU_CROSSREFS = json.loads(p.read_text(encoding="utf-8"))
        _SKU_CROSSREFS_MTIME = mtime
        app.logger.info(f"[XREF] Loaded cross-refs for {len(_SKU_CROSSREFS)} SKUs")
    except Exception as e:
        app.logger.warning(f"[XREF] Failed to load sku_crossrefs.json: {e}")
        _SKU_CROSSREFS = _SKU_CROSSREFS or {}
    return _SKU_CROSSREFS


def _apply_profile_filter(
    shortlist_skus: List[str],
    profiles: dict,
    query_front_grooves: int,
    query_back_grooves: int,
    query_pin_count: int,
    min_pool: int = 5,
) -> List[str]:
    """
    Filter shortlist to SKUs whose profile is compatible with the query.

    Rules:
    - query value -1 = Unknown -- that axis is skipped entirely
    - SKUs with NO profile entry are always kept (can't exclude unknowns)
    - SKUs WITH a profile that disagrees are excluded
    - Falls back to full shortlist if fewer than min_pool SKUs survive
    """
    if query_front_grooves < 0 and query_back_grooves < 0 and query_pin_count < 0:
        return shortlist_skus

    if not profiles:
        return shortlist_skus

    filtered = []
    for sku in shortlist_skus:
        prof = profiles.get(sku)
        if prof is None:
            filtered.append(sku)
            continue

        if query_front_grooves >= 0:
            sku_front = prof.get("front_grooves", -1)
            if sku_front >= 0 and sku_front != query_front_grooves:
                continue

        if query_back_grooves >= 0:
            sku_back = prof.get("back_grooves", -1)
            if sku_back >= 0 and sku_back != query_back_grooves:
                continue

        if query_pin_count >= 0:
            sku_pins = prof.get("pin_count", 0)
            if sku_pins > 0 and sku_pins != query_pin_count:
                continue

        filtered.append(sku)

    if len(filtered) < min_pool:
        try:
            app.logger.warning(
                f"[PROFILE] Filter left only {len(filtered)} SKUs "
                f"(front={query_front_grooves}, back={query_back_grooves}, pins={query_pin_count}) "
                f"-- falling back to full shortlist of {len(shortlist_skus)}"
            )
        except Exception:
            pass
        return shortlist_skus

    try:
        app.logger.info(
            f"[PROFILE] Filter: {len(shortlist_skus)} -> {len(filtered)} SKUs "
            f"(front={query_front_grooves}, back={query_back_grooves}, pins={query_pin_count})"
        )
    except Exception:
        pass

    return filtered


def _parse_groove_int(val: str) -> int:
    """Parse groove count from form string. Returns -1 if unknown/missing."""
    try:
        v = int((val or "-1").strip())
        if v < 0 or v > 6:
            return -1
        return v
    except (ValueError, TypeError):
        return -1


def _parse_pin_int(val: str) -> int:
    """Parse pin count from form string. Returns -1 if unknown/missing."""
    try:
        v = int((val or "-1").strip())
        if v < 1 or v > 10:
            return -1
        return v
    except (ValueError, TypeError):
        return -1


def _parse_gauge_float(val: str) -> float:
    """Parse mortice gauge from form string. Returns -1 if unknown/missing. Supports half-gauges (e.g. 2.5)."""
    try:
        v = float((val or "-1").strip())
        if v < 1 or v > 12:
            return -1.0
        return v
    except (ValueError, TypeError):
        return -1.0


# ============================================================
# ============================================================
# ── SOFT PROFILE PENALTY (replaces hard knockout filters)
# ============================================================


def _compute_profile_penalty(
    sku: str,
    profiles: dict,
    query_front_grooves: int,
    query_back_grooves: int,
    query_pin_count: int,
    query_key_type: str,
    query_groove_side: str,
    query_flag_profile: str,
    query_bow_shape: str = "",
    query_gauge: float = -1.0,
    query_dimple_track: str = "",
    query_dimple_cuts: int = -1,
) -> float:
    """
    Compute soft penalty multiplier for profile-mismatching SKUs.

    Returns:
        1.0   = match or unknown (no penalty)
        < 1.0 = confirmed mismatch (penalty stacks multiplicatively)
    """
    prof = profiles.get(sku)
    if prof is None:
        return 1.0  # no profile on file — neutral

    mult = 1.0

    # Front grooves
    if query_front_grooves >= 0:
        sku_val = prof.get("front_grooves", -1)
        if sku_val >= 0 and sku_val != query_front_grooves:
            mult *= float(CFG.get("profile_penalty_groove", 0.970))

    # Back grooves
    if query_back_grooves >= 0:
        sku_val = prof.get("back_grooves", -1)
        if sku_val >= 0 and sku_val != query_back_grooves:
            mult *= float(CFG.get("profile_penalty_groove", 0.970))

    # Pin count
    if query_pin_count >= 0:
        sku_val = prof.get("pin_count", 0)
        if sku_val > 0 and sku_val != query_pin_count:
            mult *= float(CFG.get("profile_penalty_pin", 0.975))

    # Key type (with family compatibility)
    if query_key_type:
        sku_val = prof.get("key_type", "")
        if sku_val and sku_val != query_key_type:
            # Family compatibility: MORTICE_* and DOUBLE_BIT_* subtypes
            q_family = query_key_type.split("_")[0] if "_" in query_key_type else query_key_type
            s_family = sku_val.split("_")[0] if "_" in sku_val else sku_val

            # Same family root (MORTICE or DOUBLE) — only penalize specific vs specific
            if q_family == s_family or (query_key_type.startswith("MORTICE") and sku_val.startswith("MORTICE")) or \
               (query_key_type.startswith("DOUBLE_BIT") and sku_val.startswith("DOUBLE_BIT")):
                # Both are same family — penalize only if both are specific subtypes
                q_is_generic = query_key_type in ("MORTICE", "DOUBLE_BIT")
                s_is_generic = sku_val in ("MORTICE", "DOUBLE_BIT")
                if not q_is_generic and not s_is_generic:
                    mult *= float(CFG.get("profile_penalty_key_type", 0.960))
            else:
                mult *= float(CFG.get("profile_penalty_key_type", 0.960))

    # Groove side
    if query_groove_side:
        sku_val = prof.get("groove_side", "")
        if sku_val and sku_val != query_groove_side:
            mult *= float(CFG.get("profile_penalty_groove_side", 0.970))

    # Flag profile
    if query_flag_profile:
        sku_val = prof.get("flag_profile", "")
        if sku_val and sku_val != query_flag_profile:
            mult *= float(CFG.get("profile_penalty_flag_profile", 0.965))

    # Bow shape
    if query_bow_shape:
        sku_val = prof.get("bow_shape", "")
        if sku_val and sku_val != query_bow_shape:
            mult *= float(CFG.get("profile_penalty_bow_shape", 0.960))

    # Mortice gauge (strong discriminator — different gauge = wrong key)
    if query_gauge >= 1.0:
        sku_val = prof.get("mortice_gauge", -1)
        if isinstance(sku_val, (int, float)) and float(sku_val) >= 1.0 and abs(float(sku_val) - float(query_gauge)) > 0.01:
            mult *= float(CFG.get("profile_penalty_gauge", 0.950))

    # Dimple track type
    if query_dimple_track:
        sku_val = prof.get("dimple_track", "")
        if sku_val and sku_val != query_dimple_track:
            mult *= float(CFG.get("profile_penalty_dimple_track", 0.955))

    # Dimple cut count (reuse pin penalty weight)
    if query_dimple_cuts >= 1:
        sku_val = prof.get("dimple_cuts", -1)
        if isinstance(sku_val, (int, float)) and int(sku_val) >= 1 and int(sku_val) != query_dimple_cuts:
            mult *= float(CFG.get("profile_penalty_pin", 0.975))

    return mult


# ============================================================
# ── Query cleaning + auto groove detection helper
# ============================================================


def _clean_and_auto_grooves(
    query_path1: str,
    query_path2: Optional[str],
) -> Tuple[str, Optional[str], int, int, dict]:
    """
    1) Clean query images with rembg (if enabled + available).
    2) Auto-detect groove counts on cleaned images (if enabled + reliable).
    Auto values are returned SEPARATELY — they are NOT applied as filters yet.
    The matching engine validates them against top CLIP candidates first.

    Returns:
        (clean_path1, clean_path2, auto_front_grooves, auto_back_grooves, diag_dict)
        auto values are -1 if not detected or unreliable.
    """
    diag: dict = {
        "clean_enabled": bool(CFG.get("query_clean_enabled", False)),
        "clean_used_front": False,
        "clean_used_back": False,
        "auto_groove_enabled": bool(CFG.get("auto_groove_enabled", False)),
        "auto_front_grooves": -1,
        "auto_front_reliable": None,
        "auto_back_grooves": -1,
        "auto_back_reliable": None,
    }

    clean1 = query_path1
    clean2 = query_path2
    auto_front = -1
    auto_back = -1

    # ── Step 1: rembg cleaning ──
    if bool(CFG.get("query_clean_enabled", False)):
        try:
            from image_cleaner import clean_query_image

            clean1 = clean_query_image(query_path1)
            diag["clean_used_front"] = (clean1 != query_path1)

            if query_path2 and os.path.exists(query_path2):
                clean2 = clean_query_image(query_path2)
                diag["clean_used_back"] = (clean2 != query_path2)

        except ImportError:
            try:
                app.logger.info("[CLEAN] image_cleaner not available — skipping")
            except Exception:
                pass
        except Exception as e:
            try:
                app.logger.warning(f"[CLEAN] Failed: {e}")
            except Exception:
                pass

    # ── Step 2: auto groove counting (detect only, don't apply) ──
    if bool(CFG.get("auto_groove_enabled", False)):
        try:
            from groove_counter import count_grooves

            # Front
            gc = count_grooves(clean1)
            diag["auto_front_grooves"] = gc.get("groove_count", 0)
            diag["auto_front_reliable"] = gc.get("reliable", False)
            if gc.get("reliable", False):
                auto_front = gc["groove_count"]
            try:
                app.logger.info(
                    f"[GROOVE-AUTO] Front: {gc.get('groove_count', 0)} grooves "
                    f"(reliable={gc.get('reliable')}, blade={gc.get('blade_width_avg', '?')}px)"
                )
            except Exception:
                pass

            # Back
            if clean2 and os.path.exists(clean2):
                gc = count_grooves(clean2)
                diag["auto_back_grooves"] = gc.get("groove_count", 0)
                diag["auto_back_reliable"] = gc.get("reliable", False)
                if gc.get("reliable", False):
                    auto_back = gc["groove_count"]
                try:
                    app.logger.info(
                        f"[GROOVE-AUTO] Back: {gc.get('groove_count', 0)} grooves "
                        f"(reliable={gc.get('reliable')}, blade={gc.get('blade_width_avg', '?')}px)"
                    )
                except Exception:
                    pass

        except ImportError:
            try:
                app.logger.info("[GROOVE-AUTO] groove_counter not available — skipping")
            except Exception:
                pass
        except Exception as e:
            try:
                app.logger.warning(f"[GROOVE-AUTO] Failed: {e}")
            except Exception:
                pass

    return clean1, clean2, auto_front, auto_back, diag


# ============================================================
# Main matching engine — VECTORIZED similarity computation
# ============================================================


def _run_match_paired_two_stage(
    q_front: np.ndarray,
    q_back: Optional[np.ndarray],
    *,
    query_front_path: str,
    query_back_path: Optional[str] = None,
    top_k_sku: int,
    top_m_per_sku: int,
    cap_per_sku: int,
    softmax_temp: float,
    low_cert_prob: float,
    low_cert_prob_gap: float,
    cons_n: int,
    cons_sigma: float,
    query_category: str = "",
    query_profile: Optional[Dict] = None,
    auto_front_grooves: int = -1,
    auto_back_grooves: int = -1,
    exclude_jpn: bool = False,
    allowed_jpn_sets: Optional[set] = None,
) -> Tuple[List[dict], bool, dict]:
    import time

    t0 = time.time()

    if query_profile is None:
        query_profile = {}

    # Backward-compat aliases for auto groove confirmation
    query_front_grooves = int(query_profile.get("front_grooves", -1))
    query_back_grooves = int(query_profile.get("back_grooves", -1))

    load_embedding_cache(force=False)
    rows = get_cached_rows(force=False)

    if not rows:
        return [], False, {"error": "cache_empty"}

    # ── Ensure pre-stacked matrices are available ──
    if FRONT_MATRIX is None or FRONT_MATRIX.shape[0] == 0:
        return [], False, {"error": "no_front_comparables"}

    qf = np.asarray(q_front, dtype=np.float32).reshape(-1)
    qf = qf / (float(np.linalg.norm(qf) + 1e-12))

    qb = None
    if q_back is not None:
        qb = np.asarray(q_back, dtype=np.float32).reshape(-1)
        qb = qb / (float(np.linalg.norm(qb) + 1e-12))

    shortlist_n = int(CFG.get("paired_front_shortlist_n", 120) or 120)
    shortlist_n = max(5, shortlist_n)

    w_b = float(CFG.get("paired_back_verify_weight", 0.03) or 0.0)
    w_b = max(0.0, w_b)

    # --------------------------------------------------------
    # Query-level orientation resolver (VECTORIZED)
    # --------------------------------------------------------
    orient_enabled = bool(CFG.get("paired_query_orient_resolve", True)) and (qb is not None)
    orient_topn = int(CFG.get("paired_query_orient_topn", 40) or 40)
    orient_topn = max(10, min(120, orient_topn))
    orient_margin = float(CFG.get("paired_query_orient_margin", 0.002) or 0.002)
    orient_margin = max(0.0, min(0.05, orient_margin))

    confident_margin = float(CFG.get("paired_orient_confident_margin", 0.012) or 0.012)
    confident_margin = max(0.0, min(0.10, confident_margin))

    orient_swapped = False
    orient_score_a = 0.0
    orient_score_b = 0.0
    orient_delta = 0.0
    orient_confident = False

    if orient_enabled and qb is not None:
        # ── PERF: Single matrix multiply instead of Python loop + heap ──
        def _topn_mean(arr: np.ndarray, n: int) -> float:
            if arr.size == 0:
                return 0.0
            n = min(n, arr.size)
            # np.partition is O(N) — faster than full sort for top-N
            top = np.partition(arr, -n)[-n:]
            return float(np.mean(top))

        sims_qf_front = FRONT_MATRIX @ qf   # (N_front,)
        sims_qb_front = FRONT_MATRIX @ qb   # (N_front,)

        if BACK_MATRIX is not None and BACK_MATRIX.shape[0] > 0:
            sims_qf_back = BACK_MATRIX @ qf   # (N_back,)
            sims_qb_back = BACK_MATRIX @ qb   # (N_back,)
        else:
            sims_qf_back = np.array([], dtype=np.float32)
            sims_qb_back = np.array([], dtype=np.float32)

        qf_front_mean = _topn_mean(sims_qf_front, orient_topn)
        qf_back_mean  = _topn_mean(sims_qf_back, orient_topn)
        qb_front_mean = _topn_mean(sims_qb_front, orient_topn)
        qb_back_mean  = _topn_mean(sims_qb_back, orient_topn)

        orient_score_a = float(qf_front_mean + qb_back_mean)
        orient_score_b = float(qf_back_mean + qb_front_mean)

        orient_delta = float(orient_score_b - orient_score_a)
        orient_confident = abs(float(orient_delta)) >= float(confident_margin)

        if orient_score_b > (orient_score_a + orient_margin):
            qf, qb = qb, qf
            query_front_path, query_back_path = (query_back_path or query_front_path), query_front_path
            orient_swapped = True

    # swap-safe decision
    if qb is not None:
        swap_safe = bool(CFG.get("paired_swap_safe_enabled", True)) and (not orient_enabled or (not orient_confident))
    else:
        swap_safe = False

    min_term_w = float(CFG.get("paired_min_term_weight", 0.02) or 0.0)
    min_term_w = max(0.0, min(0.20, min_term_w))

    back_min_best_sim = float(CFG.get("paired_back_min_best_sim", 0.88) or 0.0)
    back_min_best_sim = max(0.0, min(1.0, back_min_best_sim))

    floor_on = bool(CFG.get("paired_front_floor_enabled", True)) and (qb is not None)

    query_type = "UNKNOWN"
    if is_style_detection_enabled() and bool(CFG.get("style_gating_enabled", True)) and query_front_path and os.path.exists(query_front_path):
        query_type = detect_key_type_from_path(query_front_path)
    # If user specified a category, use it for silhouette mapping
    if query_category and is_style_detection_enabled():
        user_sil = get_silhouette_type(query_category)
        if user_sil and user_sil != "UNKNOWN":
            query_type = user_sil

    # Query length
    query_len = None
    try:
        ql1 = detect_key_length_ratio_from_path(query_front_path) if (query_front_path and os.path.exists(query_front_path)) else None
        ql2 = detect_key_length_ratio_from_path(query_back_path) if (query_back_path and os.path.exists(query_back_path)) else None
        if bool(CFG.get("length_use_max_of_two", True)) and ql1 is not None and ql2 is not None:
            query_len = float(max(float(ql1), float(ql2)))
        else:
            query_len = ql1 if ql1 is not None else ql2
        if query_len is not None:
            query_len = float(query_len)
    except Exception:
        query_len = None

    # ── PERF: Vectorized similarity computation ──
    # Compute ALL similarities in one matrix multiply per query vector,
    # then distribute into per-SKU dicts. Same scoring logic, just faster.

    front_sims_qf = FRONT_MATRIX @ qf   # (N_front,)

    back_sims_qb = None
    if qb is not None and BACK_MATRIX is not None and BACK_MATRIX.shape[0] > 0:
        back_sims_qb = BACK_MATRIX @ qb   # (N_back,)

    front_sims_qb = None
    back_sims_qf = None
    if swap_safe and qb is not None:
        front_sims_qb = FRONT_MATRIX @ qb   # (N_front,)
        if BACK_MATRIX is not None and BACK_MATRIX.shape[0] > 0:
            back_sims_qf = BACK_MATRIX @ qf   # (N_back,)

    # Build per-SKU dicts from pre-computed sims
    by_sku_front_qf: Dict[str, List[Tuple[float, str, str, str]]] = {}
    by_sku_back_qb: Dict[str, List[Tuple[float, str, str, str]]] = {}
    by_sku_front_qb: Dict[str, List[Tuple[float, str, str, str]]] = {}
    by_sku_back_qf: Dict[str, List[Tuple[float, str, str, str]]] = {}

    per_sku_count: Dict[str, int] = {}

    _jp_pre_excluded = 0

    # FRONT images
    for idx, (image_id, sku, orig_name, desc) in enumerate(FRONT_INFO):
        sku = (sku or "").strip()
        if not sku:
            continue
        if exclude_jpn and sku.startswith('jpn-'):
            _jp_pre_excluded += 1
            continue
        if allowed_jpn_sets is not None and sku.startswith('jpn-'):
            parts = sku.split('-')
            set_key = "jpn-" + "-".join(parts[1:-1])
            if set_key not in allowed_jpn_sets:
                continue
        if cap_per_sku > 0:
            per_sku_count.setdefault(sku, 0)
            if per_sku_count[sku] >= cap_per_sku:
                continue

        sim_f = float(front_sims_qf[idx])
        by_sku_front_qf.setdefault(sku, []).append((sim_f, str(image_id), orig_name, desc))

        if swap_safe and front_sims_qb is not None:
            sim_qb = float(front_sims_qb[idx])
            by_sku_front_qb.setdefault(sku, []).append((sim_qb, str(image_id), orig_name, desc))

        if cap_per_sku > 0:
            per_sku_count[sku] = per_sku_count.get(sku, 0) + 1

    # BACK images
    for idx, (image_id, sku, orig_name, desc) in enumerate(BACK_INFO):
        sku = (sku or "").strip()
        if not sku:
            continue
        if exclude_jpn and sku.startswith('jpn-'):
            _jp_pre_excluded += 1
            continue
        if allowed_jpn_sets is not None and sku.startswith('jpn-'):
            parts = sku.split('-')
            set_key = "jpn-" + "-".join(parts[1:-1])
            if set_key not in allowed_jpn_sets:
                continue
        if cap_per_sku > 0:
            per_sku_count.setdefault(sku, 0)
            if per_sku_count[sku] >= cap_per_sku:
                continue

        if back_sims_qb is not None:
            sim_b = float(back_sims_qb[idx])
            by_sku_back_qb.setdefault(sku, []).append((sim_b, str(image_id), orig_name, desc))

        if swap_safe and back_sims_qf is not None:
            sim_qf = float(back_sims_qf[idx])
            by_sku_back_qf.setdefault(sku, []).append((sim_qf, str(image_id), orig_name, desc))

        if cap_per_sku > 0:
            per_sku_count[sku] = per_sku_count.get(sku, 0) + 1

    if exclude_jpn and _jp_pre_excluded > 0:
        app.logger.info(f"[JP-PRE-FILTER] excluded {_jp_pre_excluded} jpn- SKUs from candidate pool before CLIP ranking")

    if not by_sku_front_qf:
        return [], False, {"error": "no_front_comparables", "query_type": query_type}

    # --------------------------------------------------------
    # Stage 1: FRONT-only rank all SKUs
    # --------------------------------------------------------
    front_ranked: List[Tuple[float, str, float, str, str, str]] = []
    mean_blend_w = float(CFG.get("sku_mean_blend_weight", 0.15) or 0.15)
    mean_blend_w = max(0.0, min(0.5, mean_blend_w))

    for sku, items in by_sku_front_qf.items():
        items.sort(reverse=True, key=lambda x: x[0])
        top_items = items[:top_m_per_sku]
        sims = [float(x[0]) for x in top_items]
        score = _score_sku_from_sims(sims)
        best_sim, best_id, best_name, best_desc = top_items[0]

        # Blend in SKU mean embedding score for stability
        if mean_blend_w > 0.0 and sku in SKU_MEAN_FRONT:
            mean_sim = float(np.dot(qf, SKU_MEAN_FRONT[sku]))
            score    = float(score)    * (1.0 - mean_blend_w) + mean_sim * mean_blend_w
            best_sim = float(best_sim) * (1.0 - mean_blend_w) + mean_sim * mean_blend_w

        score_adj, sim_adj = _apply_soft_type_penalty(
            query_type=query_type,
            sku=sku,
            score=float(score),
            sim_disp=float(best_sim),
        )
        front_ranked.append((float(score_adj), sku, float(sim_adj), str(best_id), best_name, best_desc))

    front_ranked.sort(reverse=True, key=lambda x: (x[0], x[2]))
    shortlist = front_ranked[:shortlist_n]
    shortlist_skus = [x[1] for x in shortlist]

    # --------------------------------------------------------
    # AUTO GROOVE CONFIRM-ONLY VALIDATION
    # --------------------------------------------------------
    _prof = _load_sku_profiles()
    _auto_confirm_n = 20

    def _confirm_auto_groove(auto_val: int, manual_val: int, field: str) -> int:
        """Return groove count to use: manual if set, confirmed auto, or -1."""
        if manual_val >= 0:
            return manual_val
        if auto_val < 0:
            return -1
        if not _prof:
            return -1

        top_check = shortlist_skus[:_auto_confirm_n]
        agree = 0
        profiled = 0
        for sku in top_check:
            p = _prof.get(sku)
            if p is None:
                continue
            sku_val = p.get(field, -1)
            if sku_val < 0:
                continue
            profiled += 1
            if sku_val == auto_val:
                agree += 1

        if profiled < 3:
            try:
                app.logger.info(
                    f"[GROOVE-CONFIRM] {field}: auto={auto_val}, only {profiled} profiled in top {_auto_confirm_n} — SKIPPED (too few)")
            except Exception:
                pass
            return -1

        ratio = agree / profiled
        if ratio >= 0.40:
            try:
                app.logger.info(
                    f"[GROOVE-CONFIRM] {field}: auto={auto_val}, {agree}/{profiled} agree ({ratio:.0%}) — CONFIRMED")
            except Exception:
                pass
            return auto_val
        else:
            try:
                app.logger.info(
                    f"[GROOVE-CONFIRM] {field}: auto={auto_val}, {agree}/{profiled} agree ({ratio:.0%}) — REJECTED")
            except Exception:
                pass
            return -1

    query_front_grooves = _confirm_auto_groove(auto_front_grooves, query_front_grooves, "front_grooves")
    query_back_grooves  = _confirm_auto_groove(auto_back_grooves,  query_back_grooves,  "back_grooves")

    # Write confirmed values back into query_profile for penalty computation
    query_profile["front_grooves"] = query_front_grooves
    query_profile["back_grooves"] = query_back_grooves

    # --------------------------------------------------------
    # Stage 2: rerank shortlist with back image + penalties
    # --------------------------------------------------------
    ranked: List[Tuple[float, str, float, str, str, str, int, int]] = []
    for sku in shortlist_skus:
        f_items = by_sku_front_qf.get(sku, [])
        if not f_items:
            continue
        f_items.sort(reverse=True, key=lambda x: x[0])
        f_top = f_items[:top_m_per_sku]
        best_front_sim, best_front_id, best_front_name, best_front_desc = f_top[0]
        f_sims = [float(x[0]) for x in f_top]
        f_score = _score_sku_from_sims(f_sims) if f_sims else 0.0

        # Blend in SKU mean embedding score for stability
        if mean_blend_w > 0.0 and sku in SKU_MEAN_FRONT:
            mean_sim_f = float(np.dot(qf, SKU_MEAN_FRONT[sku]))
            f_score        = float(f_score)        * (1.0 - mean_blend_w) + mean_sim_f * mean_blend_w
            best_front_sim = float(best_front_sim) * (1.0 - mean_blend_w) + mean_sim_f * mean_blend_w

        b_items = by_sku_back_qb.get(sku, [])
        if b_items:
            b_items.sort(reverse=True, key=lambda x: x[0])
        sku_img_count = int(len(f_items) + (len(b_items) if qb is not None else 0))

        front_only_score = float(f_score)
        front_only_disp = float(best_front_sim)

        orient = 0
        if qb is not None:
            if swap_safe:
                paired_score, paired_disp, orient = _best_paired_score_swap_safe(
                    sku=sku,
                    by_front_qf=by_sku_front_qf,
                    by_back_qb=by_sku_back_qb,
                    by_front_qb=by_sku_front_qb,
                    by_back_qf=by_sku_back_qf,
                    top_m_per_sku=top_m_per_sku,
                    w_b=w_b,
                    min_term_w=min_term_w,
                    back_min_best_sim=back_min_best_sim,
                )
            else:
                best_back_raw = 0.0
                best_back_disp = 0.0
                b_score = 0.0

                if b_items:
                    b_top = b_items[:top_m_per_sku]
                    b_sims = [float(x[0]) for x in b_top]
                    b_score = _score_sku_from_sims(b_sims)
                    best_back_raw = float(b_top[0][0])
                    best_back_disp = float(best_back_raw)

                    # Blend in SKU mean back score
                    if mean_blend_w > 0.0 and qb is not None and sku in SKU_MEAN_BACK:
                        mean_sim_b  = float(np.dot(qb, SKU_MEAN_BACK[sku]))
                        b_score        = float(b_score)        * (1.0 - mean_blend_w) + mean_sim_b * mean_blend_w
                        best_back_disp = float(best_back_disp) * (1.0 - mean_blend_w) + mean_sim_b * mean_blend_w

                if float(best_back_raw) < float(back_min_best_sim):
                    b_score = 0.0
                    best_back_disp = 0.0

                mterm = float(min(float(best_front_sim), float(best_back_raw)))
                paired_score = float(f_score) + (w_b * float(b_score)) + (min_term_w * mterm)
                paired_disp = float(best_front_sim) + (w_b * float(best_back_disp)) + (min_term_w * mterm)

            front_only_score_adj, front_only_disp_adj = _apply_soft_type_penalty(
                query_type=query_type,
                sku=sku,
                score=float(front_only_score),
                sim_disp=float(front_only_disp),
            )
            paired_score_adj, paired_disp_adj = _apply_soft_type_penalty(
                query_type=query_type,
                sku=sku,
                score=float(paired_score),
                sim_disp=float(paired_disp),
            )

            if floor_on:
                if front_only_score_adj >= paired_score_adj:
                    final_score = float(front_only_score_adj)
                    display_sim = float(front_only_disp_adj)
                    orient = 0
                else:
                    final_score = float(paired_score_adj)
                    display_sim = float(paired_disp_adj)
            else:
                final_score = float(paired_score_adj)
                display_sim = float(paired_disp_adj)

        else:
            final_score, display_sim = _apply_soft_type_penalty(
                query_type=query_type,
                sku=sku,
                score=float(front_only_score),
                sim_disp=float(front_only_disp),
            )

        # LENGTH CUE (Stage-2 only; small penalty)
        final_score, display_sim = _apply_length_penalty(
            sku=sku,
            score=float(final_score),
            sim_disp=float(display_sim),
            query_len=query_len,
        )

        # PROFILE SOFT PENALTY (Stage-2; penalizes confirmed mismatches)
        # Uses generic vertical_loader.compute_field_penalty
        if _prof:
            ppenalty = compute_field_penalty(
                sku=sku,
                profiles=_prof,
                query_values=query_profile,
                query_category=query_category,
            )
            if ppenalty < 1.0:
                final_score = float(final_score) * ppenalty
                display_sim = float(display_sim) * ppenalty

        ranked.append(
            (
                float(final_score),
                sku,
                float(display_sim),
                str(best_front_id),
                best_front_name,
                best_front_desc,
                int(sku_img_count),
                int(orient),
            )
        )

    if not ranked:
        return [], False, {"error": "no_ranked", "query_type": query_type}

    ranked.sort(reverse=True, key=lambda x: (x[0], x[2]))

    # --------------------------------------------------------
    # Near-tie rerank
    # --------------------------------------------------------
    rerank_eps = float(CFG.get("rerank_score_eps", 0.010) or 0.010)
    if len(ranked) >= 2 and rerank_eps > 0:
        best_score = ranked[0][0]
        tie_end = 0
        for i, r in enumerate(ranked):
            if (best_score - r[0]) <= rerank_eps:
                tie_end = i + 1
            else:
                break
        if tie_end > 1:
            tie_group = ranked[:tie_end]
            rest = ranked[tie_end:]
            tie_group.sort(key=lambda x: (x[2], x[0]), reverse=True)
            ranked = tie_group + rest

    top = ranked[:top_k_sku]

    scores_np = np.array([t[0] for t in top], dtype=np.float32)
    conf = _softmax(scores_np, float(softmax_temp))

    low_cert = False
    if len(conf) >= 2:
        p1 = float(conf[0])
        p2 = float(conf[1])
        low_cert = (p1 < float(low_cert_prob)) or ((p1 - p2) < float(low_cert_prob_gap))

    _xrefs = _load_sku_crossrefs()

    results: List[dict] = []
    for i, (score, sku, sim_disp, best_id, best_name, best_desc, sku_img_count, orient) in enumerate(top):
        sims_all = []
        sims_all += [float(x[0]) for x in by_sku_front_qf.get(sku, [])]
        if qb is not None:
            sims_all += [float(x[0]) for x in by_sku_back_qb.get(sku, [])]
            if swap_safe:
                sims_all += [float(x[0]) for x in by_sku_front_qb.get(sku, [])]
                sims_all += [float(x[0]) for x in by_sku_back_qf.get(sku, [])]

        cons, std = _consistency_from_sims(sims_all, n=int(cons_n), sigma=float(cons_sigma))

        results.append(
            {
                "rank": i + 1,
                "sku": sku,
                "score": float(score),
                "prob": float(conf[i]),
                "similarity": float(sim_disp),
                "image_id": str(best_id),
                "filename": best_name,
                "description": (best_desc or "—"),
                "sku_img_count": int(sku_img_count),
                "consistency": float(cons),
                "std": float(std),
                "sku_type": SKU_TYPE.get(sku, "UNKNOWN"),
                "swap_used": bool(orient == 1),
                "image_id_front": str(by_sku_front_qf[sku][0][1]) if by_sku_front_qf.get(sku) else str(best_id),
                "image_id_back":  str(by_sku_back_qb[sku][0][1]) if by_sku_back_qb.get(sku) else None,
                "crossrefs":      (_xrefs.get(sku, {}).get("crossrefs", []) if isinstance(_xrefs.get(sku), dict) else _xrefs.get(sku, [])),
                "manufacturer":   (_xrefs.get(sku, {}).get("manufacturer", "") if isinstance(_xrefs.get(sku), dict) else ""),
            }
        )

    diag = {
        "paired_two_stage": True,
        "shortlist_n": int(shortlist_n),
        "back_verify_weight": float(w_b),
        "style_soft_penalty": bool(CFG.get("style_gating_enabled", True)),
        "query_type": query_type,
        "swap_safe": bool(swap_safe),
        "min_term_w": float(min_term_w),
        "front_floor": bool(floor_on),
        "orient_resolve_enabled": bool(orient_enabled),
        "orient_query_swapped": bool(orient_swapped),
        "orient_score_a": float(orient_score_a),
        "orient_score_b": float(orient_score_b),
        "orient_delta": float(orient_delta),
        "orient_confident": bool(orient_confident),
        "length_cue_enabled": bool(CFG.get("length_cue_enabled", True)),
        "query_len": (None if query_len is None else float(query_len)),
        "profile_filter_front": int(query_profile.get("front_grooves", -1)),
        "profile_filter_back": int(query_profile.get("back_grooves", -1)),
        "profile_filter_pins": int(query_profile.get("pin_count", -1)),
        "auto_front_grooves_raw": int(auto_front_grooves),
        "auto_back_grooves_raw": int(auto_back_grooves),
        "elapsed_s": float(round(time.time() - t0, 4)),
    }
    return results, low_cert, diag


# ============================================================
# ── DINOV2 TIE-BREAKER (with background preload)
# Re-scores top 2 candidates using DINOv2 when CLIP scores
# are within epsilon. Only fires on near-ties. DINOv2 is
# optionally preloaded in a background thread at startup.
# ============================================================

_TIEBREAK_EMBEDDER = None
_TIEBREAK_LOCK = threading.Lock()


def _get_tiebreak_embedder():
    global _TIEBREAK_EMBEDDER
    with _TIEBREAK_LOCK:
        if _TIEBREAK_EMBEDDER is None:
            from feature_extractor import ImageEmbedder
            print("[DINO-TIEBREAK] Loading DINOv2 for tie-breaking...", flush=True)
            _TIEBREAK_EMBEDDER = ImageEmbedder(backend="dinov2")
            print("[DINO-TIEBREAK] DINOv2 ready.", flush=True)
    return _TIEBREAK_EMBEDDER


def _preload_tiebreak_embedder():
    """Background-load DINOv2 so it's warm when first tie-break fires."""
    try:
        _get_tiebreak_embedder()
    except Exception as e:
        print(f"[DINO-TIEBREAK] Background preload failed: {e}", flush=True)


def _dinov2_tiebreak(results: List[dict], query_front_path: str) -> Tuple[List[dict], dict]:
    """
    If top 2 CLIP scores are within eps, re-score with DINOv2.
    Swaps rank 1 and 2 only if DINOv2 clears its own swap margin.
    Returns (results, dino_diag) — dino_diag is {"fired": False, ...} when
    the tiebreak didn't run, or the full instrumentation dict when it did.
    """
    if not bool(CFG.get("dinov2_tiebreak_enabled", True)):
        return results, {"fired": False, "reason": "disabled"}
    if len(results) < 2:
        return results, {"fired": False, "reason": "lt_2_results"}

    eps = float(CFG.get("dinov2_tiebreak_eps", 0.008))
    gap = float(results[0]["score"]) - float(results[1]["score"])
    if gap > eps:
        return results, {"fired": False, "reason": "clear_clip_winner", "clip_gap": float(gap)}

    if not query_front_path or not os.path.exists(query_front_path):
        return results, {"fired": False, "reason": "no_query_image"}

    try:
        dino = _get_tiebreak_embedder()

        # Embed query front
        q_emb = dino.embed_path(query_front_path, multi_crop=False, suppress_bg=True)
        q_emb = np.asarray(q_emb, dtype=np.float32).reshape(-1)
        q_emb = q_emb / (float(np.linalg.norm(q_emb) + 1e-12))

        dino_sims = []
        for i in range(2):
            sku = results[i].get("sku", "")
            db_path = None

            # Try image_id if available
            img_id = results[i].get("image_id_front") or results[i].get("image_id")
            if img_id:
                db_path = _abs_image_path_from_db_path(PATH_BY_IMAGE_ID.get(str(img_id), ""))

            # Fallback: find FRONT image by SKU from clean_images
            if not db_path:
                clean_dir = os.path.join(os.path.dirname(__file__), "clean_images")
                if os.path.isdir(clean_dir):
                    import sqlite3 as _sq
                    try:
                        _conn = _sq.connect(get_images_db_path())
                        _row = _conn.execute(
                            "SELECT image_id FROM images WHERE sku = ? AND original_filename LIKE '%FRONT%' LIMIT 1",
                            (sku,)
                        ).fetchone()
                        _conn.close()
                        if _row:
                            for _ext in (".jpg", ".jpeg", ".png"):
                                _cp = os.path.join(clean_dir, f"{_row[0]}{_ext}")
                                if os.path.exists(_cp):
                                    db_path = _cp
                                    break
                    except Exception:
                        pass

            # Fallback: any path from SQLite
            if not db_path:
                try:
                    _conn = sqlite3.connect(get_images_db_path())
                    _row = _conn.execute(
                        "SELECT path FROM images WHERE sku = ? AND original_filename LIKE '%FRONT%' LIMIT 1",
                        (sku,)
                    ).fetchone()
                    _conn.close()
                    if _row and _row[0] and os.path.exists(_row[0]):
                        db_path = _row[0]
                except Exception:
                    pass

            if not db_path:
                print(f"[DINO-TIEBREAK] Cannot resolve path for {sku} — aborting", flush=True)
                return results, {"fired": False, "reason": "no_db_path", "clip_gap": float(gap)}

            c_emb = dino.embed_path(db_path, multi_crop=False, suppress_bg=True)
            c_emb = np.asarray(c_emb, dtype=np.float32).reshape(-1)
            c_emb = c_emb / (float(np.linalg.norm(c_emb) + 1e-12))
            dino_sims.append(float(np.dot(q_emb, c_emb)))

        sku0, sku1 = results[0]["sku"], results[1]["sku"]
        d0, d1 = dino_sims[0], dino_sims[1]
        dino_gap = float(d1) - float(d0)
        swap_margin = float(CFG.get("dinov2_swap_margin", 0.05))
        swapped = dino_gap >= swap_margin

        if swapped:
            # DINOv2 prefers #2 by at least the swap margin — swap
            results[0], results[1] = results[1], results[0]
            results[0]["rank"] = 1
            results[1]["rank"] = 2
            print(
                f"[DINO-TIEBREAK] SWAPPED: {sku1} (dino={d1:.4f}) beat {sku0} (dino={d0:.4f}) "
                f"| CLIP gap was {gap:.4f} | dino_gap={dino_gap:.4f} >= margin={swap_margin:.4f}",
                flush=True,
            )
        else:
            print(
                f"[DINO-TIEBREAK] Confirmed: {sku0} (dino={d0:.4f}) vs {sku1} (dino={d1:.4f}) "
                f"| CLIP gap was {gap:.4f} | dino_gap={dino_gap:.4f} < margin={swap_margin:.4f}",
                flush=True,
            )

        dino_diag = {
            "fired": True,
            "clip_gap": float(gap),
            "d0": float(d0),
            "d1": float(d1),
            "dino_gap": float(dino_gap),
            "swapped": bool(swapped),
            "sku_rank1_before": sku0,
            "sku_rank2_before": sku1,
            "sku_rank1_after": (sku1 if swapped else sku0),
            "margin_used": float(swap_margin),
        }
        print(
            "[DINO-DIAG] " + " ".join(f"{k}={v}" for k, v in dino_diag.items()),
            flush=True,
        )
        return results, dino_diag

    except Exception as e:
        print(f"[DINO-TIEBREAK] Failed: {e}", flush=True)
        return results, {"fired": False, "reason": f"exception:{e}"}


def _save_dataurl_to_query_jpg(data_url: str) -> str:
    if not data_url:
        raise ValueError("Missing image data.")
    m = re.match(r"^data:image\/(jpeg|jpg|png);base64,(.+)$", data_url.strip(), re.IGNORECASE)
    if not m:
        raise ValueError("Bad image data (expected data URL).")

    b64 = m.group(2)
    raw = base64.b64decode(b64)

    query_id = str(uuid.uuid4())
    query_filename = f"{query_id}.jpg"
    import os
    _localappdata = os.environ.get("LOCALAPPDATA", "")
    if _localappdata == "/modal_data":
        query_dir = Path("/modal_data") / "query"
    else:
        query_dir = Path(app.root_path) / "static" / "query"
    query_dir.mkdir(parents=True, exist_ok=True)
    query_path = query_dir / query_filename

    with open(query_path, "wb") as f:
        f.write(raw)

    normalize_uploaded_image(str(query_path))
    return query_filename


# ============================================================
# Routes
# ============================================================


@app.route("/")
def index():
    try:
        _stats = _load_stats()
        _total = _stats.get("total_scans", 0)
        _today = _stats.get("today_scans", 0)
        print(f"[SSR-STATS] total={_total} today={_today}", flush=True)
    except Exception:
        _total = 0
        _today = 0
    return render_template("landing.html",
        total_scans=_total,
        today_scans=_today)


@app.route("/login/")
def login_slash():
    return redirect(url_for("login"), code=308)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if check_admin_password(pw):
            session["is_admin"] = True
            return redirect(url_for("admin"))
        return render_template("login.html", error="Incorrect password.")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("match"))


@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    init_db()
    conn = sqlite3.connect(get_images_db_path())
    try:
        db_count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        sku_count = conn.execute("SELECT COUNT(DISTINCT sku) FROM images").fetchone()[0]
    finally:
        conn.close()

    load_embedding_cache()
    cache_count = len(_ROWS_CACHED or [])
    cache_loaded_at = _CACHE_LOADED_AT

    types = {"CYLINDER": 0, "DOUBLE_SIDED": 0, "MORTICE": 0, "UNKNOWN": 0}
    for t in SKU_TYPE.values():
        types[t] = types.get(t, 0) + 1

    try:
        from set_scheduler import load_scheduler_log
        scheduler_runs = load_scheduler_log()
    except Exception:
        scheduler_runs = []

    return render_template(
        "admin.html",
        db_count=db_count,
        sku_count=sku_count,
        cache_count=cache_count,
        cache_loaded_at=cache_loaded_at,
        style_types=types,
        scheduler_runs=scheduler_runs,
    )


@app.route("/admin/refresh_cache", methods=["POST"])
@admin_required
def admin_refresh_cache():
    try:
        load_embedding_cache(force=True)
        flash("Cache refreshed from database.", "ok")
    except Exception as e:
        flash(f"Cache refresh failed: {e}", "err")
    return redirect(url_for("admin"))


@app.route("/admin/reembed_missing", methods=["POST"])
@admin_required
def admin_reembed_missing():
    init_db()
    db_path = get_images_db_path()
    embedder = get_embedder()

    updated = 0
    skipped = 0

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT image_id, path FROM images WHERE embedding IS NULL").fetchall()

        for image_id, p in rows:
            if not p:
                skipped += 1
                continue

            file_path = p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(db_path), p))
            if not os.path.exists(file_path):
                skipped += 1
                continue

            normalize_uploaded_image(file_path)

            try:
                v = get_vertical()
                use_multi_crop = v.get("multi_crop", True)
                use_suppress_bg = v.get("suppress_bg", True)
                emb = embedder.embed_path(file_path, multi_crop=use_multi_crop, suppress_bg=use_suppress_bg)
            except TypeError:
                emb = embedder.embed_path(file_path)

            if emb is None:
                skipped += 1
                continue

            emb = np.asarray(emb, dtype=np.float32).reshape(-1)
            conn.execute("UPDATE images SET embedding = ? WHERE image_id = ?", (to_blob(emb), str(image_id)))
            updated += 1

        conn.commit()
    finally:
        conn.close()

    load_embedding_cache(force=True)
    flash(f"Re-embed missing complete. Updated={updated}, skipped={skipped}", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/reembed_all", methods=["POST"])
@admin_required
def admin_reembed_all():
    init_db()
    db_path = get_images_db_path()
    embedder = get_embedder()

    updated = 0
    skipped = 0

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT image_id, path FROM images").fetchall()

        for image_id, p in rows:
            if not p:
                skipped += 1
                continue

            file_path = p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(db_path), p))
            if not os.path.exists(file_path):
                skipped += 1
                continue

            normalize_uploaded_image(file_path)

            try:
                v = get_vertical()
                use_multi_crop = v.get("multi_crop", True)
                use_suppress_bg = v.get("suppress_bg", True)
                emb = embedder.embed_path(file_path, multi_crop=use_multi_crop, suppress_bg=use_suppress_bg)
            except TypeError:
                emb = embedder.embed_path(file_path)

            if emb is None:
                skipped += 1
                continue

            emb = np.asarray(emb, dtype=np.float32).reshape(-1)
            conn.execute("UPDATE images SET embedding = ? WHERE image_id = ?", (to_blob(emb), str(image_id)))
            updated += 1

        conn.commit()
    finally:
        conn.close()

    load_embedding_cache(force=True)
    flash(f"Re-embed ALL complete. Updated={updated}, skipped={skipped}", "ok")
    return redirect(url_for("admin"))


def _is_image_file(p: Path) -> bool:
    return p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


def _iter_keysdb_images(keysdb_root: Path):
    if not keysdb_root.exists():
        return

    # Support both layouts:
    #   KeysDB/key blank/SKU/...  (old layout)
    #   KeysDB/SKU/...            (current layout — SKU folders directly under root)
    key_blank_root = keysdb_root / "key blank"
    if key_blank_root.exists() and key_blank_root.is_dir():
        base_root = key_blank_root
        rel_prefix = "key blank"
    else:
        base_root = keysdb_root
        rel_prefix = ""

    canon_map = {"SKU_FRONT": "FRONT", "SKU_BACK": "BACK", "SKU_SIDE_C": "SIDE_C"}
    view_dirs = tuple(canon_map.keys())

    for sku_dir in sorted([d for d in base_root.iterdir() if d.is_dir()]):
        sku = sku_dir.name.strip()
        if not sku:
            continue

        has_view_dirs = any((sku_dir / vd).is_dir() for vd in view_dirs)

        if has_view_dirs:
            for vd in view_dirs:
                vdir = sku_dir / vd
                if not vdir.is_dir():
                    continue
                imgs = [p for p in vdir.iterdir() if p.is_file() and _is_image_file(p)]
                if not imgs:
                    continue
                imgs.sort(key=lambda p: (p.suffix.lower() not in {".jpg", ".jpeg"}, p.name.lower()))
                img = imgs[0]
                if rel_prefix:
                    rel_id = f"{rel_prefix}/{sku}/{sku}_{canon_map[vd]}"
                else:
                    rel_id = f"{sku}/{sku}_{canon_map[vd]}"
                yield sku, img, rel_id
            continue

        allowed_stems = {f"{sku}_FRONT", f"{sku}_BACK", f"{sku}_SIDE_C"}
        imgs = []
        for p in sku_dir.iterdir():
            if p.is_file() and _is_image_file(p) and p.stem.strip() in allowed_stems:
                imgs.append(p)

        for img in sorted(imgs):
            if rel_prefix:
                rel_id = f"{rel_prefix}/{sku}/{img.stem.strip()}"
            else:
                rel_id = f"{sku}/{img.stem.strip()}"
            yield sku, img, rel_id

def _iter_cardsdb_images(cardsdb_root: Path):
    """
    Walk CardsDB/{game_folder}/{card_id}/ for card images.
    
    Expected structure (from scrape_pokemon_tcg.py):
        CardsDB/pokemon/base1-4/front.png
        CardsDB/pokemon/base1-4/profile.json
    
    Yields: (sku, image_path, rel_id)
    """
    if not cardsdb_root.exists():
        return
 
    for game_dir in sorted(d for d in cardsdb_root.iterdir() if d.is_dir()):
        game_folder = game_dir.name
        if game_folder.startswith("_"):
            continue
 
        for sku_dir in sorted(d for d in game_dir.iterdir() if d.is_dir()):
            sku = sku_dir.name.strip()
            if not sku or sku.startswith("_"):
                continue
 
            # Look for front.png (scraper output)
            front = sku_dir / "front.png"
            if not front.exists():
                # Fallback: first image file in folder
                candidates = sorted(
                    (p for p in sku_dir.iterdir() if p.is_file() and _is_image_file(p)),
                    key=lambda p: p.name.lower()
                )
                if not candidates:
                    continue
                front = candidates[0]
 
            rel_id = f"{game_folder}/{sku}/{sku}_FRONT"
            yield sku, front, rel_id


@app.route("/admin/sync_keysdb", methods=["POST"])
@admin_required
def admin_sync_keysdb():
    init_db()
    db_path = get_images_db_path()
    img_dir = get_image_db_dir()
 
    # Detect vertical to choose the right sync strategy
    vertical_id = get_vertical().get("id", "keys")
 
    db_root = Path(get_db_root() or CFG.get("keysdb_root", ""))
    max_new = int(CFG.get("keysdb_sync_max_new", 0) or 0)
 
    if not db_root.exists():
        flash(f"DB root not found: {db_root}", "err")
        return redirect(url_for("admin"))
 
    # Pick the right iterator based on vertical
    if vertical_id == "cards":
        image_iter = _iter_cardsdb_images(db_root)
    else:
        image_iter = _iter_keysdb_images(db_root)
 
    # Load existing entries to skip duplicates
    existing = set()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT original_filename FROM images WHERE original_filename IS NOT NULL"
        ).fetchall()
        for (orig,) in rows:
            if orig:
                existing.add(str(orig).strip().lower())
 
    inserted = 0
    skipped_existing = 0
    skipped_bad = 0
 
    with sqlite3.connect(db_path) as conn:
        for sku, src_path, rel_id in image_iter:
            if not rel_id:
                skipped_bad += 1
                continue
 
            # Keys vertical has the parent_dir check — skip for cards
            if vertical_id != "cards":
                parent_dir = src_path.parent.name.upper()
                if parent_dir not in {"SKU_FRONT", "SKU_BACK", "SKU_SIDE_C", sku.upper()}:
                    skipped_bad += 1
                    continue
 
            rel_key = rel_id.strip().lower()
            if rel_key in existing:
                skipped_existing += 1
                continue
 
            if max_new > 0 and inserted >= max_new:
                break
 
            try:
                image_id = str(uuid.uuid4())
                dst_path = os.path.join(img_dir, f"{image_id}.jpg")
 
                shutil.copy2(str(src_path), dst_path)
                normalize_uploaded_image(dst_path)
                upload_to_r2(image_id, dst_path)

                conn.execute(
                    """
                    INSERT OR REPLACE INTO images
                        (image_id, sku, description, original_filename, path, added_at, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        image_id,
                        sku,
                        "",
                        rel_id,
                        dst_path,
                        datetime.utcnow().isoformat(),
                    ),
                )
 
                existing.add(rel_key)
                inserted += 1
 
            except Exception:
                skipped_bad += 1
                current_app.logger.exception(f"[SYNC] Failed importing {src_path}")
 
        conn.commit()
 
    load_embedding_cache(force=True)
 
    label = "CardsDB" if vertical_id == "cards" else "KeysDB"
    flash(
        f"{label} sync complete. Inserted={inserted}, "
        f"skipped_existing={skipped_existing}, skipped_bad={skipped_bad}",
        "ok",
    )
    return redirect(url_for("admin"))



@app.route("/admin/feedback")
@admin_required
def admin_feedback():
    import json as _json
    db_path = get_images_db_path()

    # Date filter — ?since=2026-03-14 or empty for all time
    since = (request.args.get("since") or "").strip()
    date_clause = ""
    date_params: list = []
    if since:
        date_clause = " AND submitted_at >= ?"
        date_params = [since]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            f"SELECT * FROM match_feedback WHERE 1=1 {date_clause} ORDER BY submitted_at DESC LIMIT 500",
            date_params,
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM match_feedback WHERE is_test=0 {date_clause}",
            date_params,
        ).fetchone()[0]
        correct = conn.execute(
            f"SELECT COUNT(*) FROM match_feedback WHERE verdict='correct' AND is_test=0 {date_clause}",
            date_params,
        ).fetchone()[0]
        wrong = conn.execute(
            f"SELECT COUNT(*) FROM match_feedback WHERE verdict='incorrect' AND is_test=0 {date_clause}",
            date_params,
        ).fetchone()[0]

        confused_rows = conn.execute(f"""
            SELECT confirmed_sku, AVG(COALESCE(confirmed_rank, 99)) as avg_rank, COUNT(*) as cnt
            FROM match_feedback
            WHERE is_test=0 AND confirmed_sku IS NOT NULL AND confirmed_sku != ''
              AND (
                (verdict='correct' AND confirmed_rank > 1)
                OR verdict='incorrect'
              )
              {date_clause}
            GROUP BY confirmed_sku ORDER BY cnt DESC, avg_rank DESC LIMIT 10
        """, date_params).fetchall()

        # Enrich with total test count and rank-1 count per SKU
        confused = []
        for row in confused_rows:
            sku = row[0]
            avg_rank = round(row[1])
            not_rank1_count = row[2]

            total_for_sku = conn.execute(f"""
                SELECT COUNT(*) FROM match_feedback
                WHERE is_test=0 AND confirmed_sku = ? AND verdict='correct' {date_clause}
            """, [sku] + date_params).fetchone()[0]

            rank1_count = conn.execute(f"""
                SELECT COUNT(*) FROM match_feedback
                WHERE is_test=0 AND confirmed_sku = ? AND verdict='correct' AND confirmed_rank = 1 {date_clause}
            """, [sku] + date_params).fetchone()[0]

            incorrect_count = conn.execute(f"""
                SELECT COUNT(*) FROM match_feedback
                WHERE is_test=0 AND confirmed_sku = ? AND verdict='incorrect' {date_clause}
            """, [sku] + date_params).fetchone()[0]

            confused.append({
                "sku": sku,
                "avg_rank": avg_rank,
                "not_rank1_count": not_rank1_count,
                "total_tests": total_for_sku + incorrect_count,
                "rank1_count": rank1_count,
                "incorrect_count": incorrect_count,
            })

        not_found_count = conn.execute(
            f"SELECT COUNT(*) FROM match_feedback WHERE is_test=0 AND verdict='incorrect' AND (confirmed_sku IS NULL OR confirmed_sku='') {date_clause}",
            date_params,
        ).fetchone()[0]

        confirmed_skus = conn.execute(f"""
            SELECT confirmed_sku, COUNT(*) as cnt
            FROM match_feedback WHERE verdict='correct' AND confirmed_sku IS NOT NULL AND is_test=0 {date_clause}
            GROUP BY confirmed_sku ORDER BY cnt DESC LIMIT 20
        """, date_params).fetchall()

    return render_template("admin_feedback.html",
        rows=rows, total=total, correct=correct, wrong=wrong,
        confused=confused, confirmed_skus=confirmed_skus,
        not_found_count=not_found_count, since=since)


@app.route("/admin/feedback/clear", methods=["POST"])
@admin_required
def admin_feedback_clear():
    db_path = get_images_db_path()
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM match_feedback").fetchone()[0]
        conn.execute("DELETE FROM match_feedback")
        conn.commit()
    flash(f"Cleared {count} feedback entries.", "ok")
    return redirect(url_for("admin_feedback"))



@app.route("/admin/run_scheduler", methods=["POST"])
@admin_required
def admin_run_scheduler():
    from set_scheduler import run_scheduler
    try:
        result = run_scheduler(dry_run=False)
        new_total = sum(len(v.get("new_sets", [])) for v in result.get("tcgs", {}).values())
        if new_total > 0:
            flash(f"✅ Scheduler complete — {new_total} new set(s) found and queued for download.", "ok")
        else:
            flash("✅ Scheduler complete — all TCGs up to date.", "ok")
    except Exception as e:
        flash(f"❌ Scheduler error: {e}", "err")
    return redirect(url_for("admin"))


@app.route("/admin/run_scheduler_dry", methods=["POST"])
@admin_required
def admin_run_scheduler_dry():
    from set_scheduler import run_scheduler
    try:
        result = run_scheduler(dry_run=True)
        new_total = sum(len(v.get("new_sets", [])) for v in result.get("tcgs", {}).values())
        if new_total > 0:
            flash(f"🔍 Dry run complete — {new_total} new set(s) detected (not downloaded).", "ok")
        else:
            flash("🔍 Dry run complete — all TCGs up to date.", "ok")
    except Exception as e:
        flash(f"❌ Dry run error: {e}", "err")
    return redirect(url_for("admin"))


@app.route("/admin/delete_code", methods=["POST"])
@admin_required
def admin_delete_code():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip().upper()
    if not code or not (code.startswith("GRAIL-") or code.startswith("TOPUP-")):
        return jsonify({"ok": False, "error": "invalid_code_format"}), 400
    subs = _load_subs()
    if code not in subs:
        return jsonify({"ok": False, "error": "not_found", "code": code}), 404
    deleted_entry = subs[code]
    del subs[code]
    _save_subs(subs)
    print(f"[ADMIN] Deleted code {code}: {deleted_entry}", flush=True)
    return jsonify({"ok": True, "code": code, "deleted_entry": deleted_entry})


@app.route("/admin/cancel_code", methods=["POST"])
@admin_required
def admin_cancel_code():
    raw = (request.form.get("code") or "").strip().upper()
    if not raw:
        flash("Cancel: no code provided.", "err")
        return redirect(url_for("admin"))

    # Format check
    if raw.startswith("TOPUP-"):
        flash(f"Cancel: {raw} is a top-up credit code, not a subscription. "
              f"Top-ups can't be cancelled — use /admin/delete_code if you "
              f"need to remove it.", "err")
        return redirect(url_for("admin"))
    if not raw.startswith("GRAIL-"):
        flash(f"Cancel: '{raw}' is not a valid subscription code format "
              f"(expected GRAIL-XXXX-XXXX).", "err")
        return redirect(url_for("admin"))

    # Legacy code check — these live in config.json, not subscriptions.json
    if raw in CFG.get("premium_codes", []):
        flash(f"Cancel: {raw} is a legacy code defined in config.json, "
              f"not in subscriptions. Remove it from config.json manually "
              f"if you need to revoke it.", "err")
        return redirect(url_for("admin"))

    # Load and locate
    subs = _load_subs()
    entry = subs.get(raw)
    if not entry:
        flash(f"Cancel: code {raw} not found in subscriptions.", "err")
        return redirect(url_for("admin"))

    prev_status = entry.get("status", "<missing>")

    if prev_status == "cancelled":
        flash(f"Cancel: {raw} was already cancelled. No change made.", "ok")
        return redirect(url_for("admin"))

    # Do the cancel — inline, two lines, no helper reuse
    entry["status"] = "cancelled"
    _save_subs(subs)

    # Receipt
    email      = entry.get("email", "<none>")
    tier       = entry.get("tier", "<none>")
    expires_at = entry.get("expires_at", "<none>")
    created_at = entry.get("created_at", "<none>")
    stripe_id  = entry.get("stripe_subscription_id", "<none>")

    print(f"[ADMIN] Cancelled code {raw} (was {prev_status}, "
          f"email={email}, tier={tier}, stripe_id={stripe_id})", flush=True)

    flash(f"Cancelled {raw}. {prev_status} → cancelled. "
          f"email={email} · tier={tier} · "
          f"expires={expires_at} · created={created_at}", "ok")
    return redirect(url_for("admin"))


@admin_required
def reset_db():
    db_path = get_images_db_path()
    img_dir = get_image_db_dir()
    try:
        if os.path.exists(db_path):
            os.remove(db_path)
        if os.path.isdir(img_dir):
            shutil.rmtree(img_dir, ignore_errors=True)
        os.makedirs(img_dir, exist_ok=True)

        init_db()
        load_embedding_cache(force=True)
        flash("Database reset complete.", "ok")
    except Exception as e:
        flash(f"Reset failed: {e}", "err")
    return redirect(url_for("admin"))


@app.route("/csv_template", endpoint="csv_template")
@admin_required
def csv_template_download():
    content = "filename,sku,description\nexample.jpg,ABC123,Example description\n"
    bio = BytesIO(content.encode("utf-8"))
    resp = make_response(
        send_file(
            bio,
            mimetype="text/csv",
            as_attachment=True,
            download_name="matchit_upload_template.csv",
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ============================================================
# Images
# ============================================================


@app.route("/img/query/<filename>")
def img_query(filename):
    import os
    _localappdata = os.environ.get("LOCALAPPDATA", "")
    if _localappdata == "/modal_data":
        query_dir = "/modal_data/query"
    else:
        query_dir = os.path.join(current_app.root_path, "static", "query")
    file_path = os.path.normpath(os.path.join(query_dir, filename))
    if not os.path.abspath(file_path).startswith(os.path.abspath(query_dir)):
        abort(404)
    if not os.path.exists(file_path):
        abort(404)
    resp = make_response(send_file(file_path))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/robots.txt")
def robots():
    return app.response_class(
        "User-agent: *\nDisallow: /api/\nDisallow: /webhook/\nDisallow: /xref-search\nDisallow: /results\nDisallow: /admin\nAllow: /\nSitemap: https://grailsweep.com/sitemap.xml\n",
        mimetype="text/plain"
    )


@app.route("/.well-known/assetlinks.json")
def assetlinks():
    content = '[{"relation":["delegate_permission/common.handle_all_urls"],"target":{"namespace":"android_app","package_name":"com.grailsweep.app","sha256_cert_fingerprints":["71:33:4D:D1:08:13:87:C8:B0:9C:AB:F9:04:94:C2:4C:8B:E5:03:AE:02:B9:43:9A:C0:D3:B7:80:C9:83:8A:FA","50:B2:FA:6D:CE:B7:8F:BC:F9:5C:E3:CC:F2:4F:66:B9:B3:1A:B3:BE:1D:67:E4:A0:88:E9:33:44:50:F7:40:C1"]}}]'
    return app.response_class(content, mimetype="application/json")


@app.route("/api/stats")
def public_stats():
    from datetime import datetime
    stats = _load_stats()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if stats.get("today_date") != today:
        stats["today_scans"] = 0
    try:
        import modal
        _sd = modal.Dict.from_name(
            "scan-source-counters", create_if_missing=True)
        _modal_scans = _sd.get("modal", 0)
        _ondevice_scans = _sd.get("ondevice", 0)
    except Exception:
        _modal_scans = 0
        _ondevice_scans = 0
    resp = jsonify({
        "total_scans": stats.get("total_scans", 0),
        "today_scans": stats.get("today_scans", 0),
        "modal_scans": _modal_scans,
        "ondevice_scans": _ondevice_scans
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/heartbeat")
def heartbeat():
    from ondevice_version import ONDEVICE_INDEX_VERSION
    resp = jsonify({
        "ok": True,
        "index_version": ONDEVICE_INDEX_VERSION,
        "model_sha": "43A9CF56DCA2441626D42DC494ECEA7D22667FEDE6166A27EAF0B39E87BA24F5",
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/ondevice/telemetry", methods=["POST"])
def ondevice_telemetry():
    """
    Beacon for on-device gate decisions (Phase 2).

    Identity is ALWAYS derived from request headers + cookie — never from
    the POST body. On event=scan + gate_decision=accept: charges quota
    (check_and_record_scan), bumps the UX ticker (_increment_scan_counter),
    and increments sku-scan-freq. Fail-open on any error so a counter
    failure never surfaces to the client.
    """
    import urllib.parse as _up
    import db as _db
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    event          = (data.get("event") or "").strip()
    gate_decision  = (data.get("gate_decision") or "").strip()
    sku            = (data.get("sku") or "").strip()
    decline_reason = data.get("decline_reason")
    top1_sim       = data.get("top1_sim")
    gap            = data.get("gap")
    game           = (data.get("game") or "").strip()
    version_tuple  = (data.get("version_tuple") or "").strip()
    error          = data.get("error")
    print(
        f"[ONDEVICE-TELEMETRY] event={event!r} gate={gate_decision!r} "
        f"decline_reason={decline_reason!r} top1_sim={top1_sim} gap={gap} "
        f"game={game!r} version={version_tuple!r} sku={sku!r} error={error!r}",
        flush=True,
    )
    # Derive identity server-side — security boundary.
    server_fp = device_id = None
    tier = code = None
    subs = {}
    try:
        ua        = request.user_agent.string
        lang      = request.headers.get("Accept-Language", "")
        addr      = request.headers.get("CF-Connecting-IP",
                    request.headers.get("X-Forwarded-For", request.remote_addr or ""))
        server_fp = _db.compute_server_fingerprint(ua, lang, addr)
        device_id = request.cookies.get("matchit_device_id_v1") or None
        code_raw  = request.cookies.get("gs_access_code", "")
        code      = _up.unquote(code_raw).strip().upper() or None
        subs      = _load_subs()
        tier      = _db.resolve_tier_from_code(code, CFG.get("premium_codes", []), subs)
        if code and tier in ("monthly", "annual"):
            sub = subs.get(code, {})
            if _ensure_tier_period_backfilled(code, sub):
                subs = _load_subs()
    except Exception as _id_exc:
        print(f"[ONDEVICE-TELEMETRY] identity error (fail-open): {_id_exc}", flush=True)
    # Fire counters on accepted scan only. Each counter is individually guarded.
    if event == "scan" and gate_decision == "accept":
        try:
            _db.check_and_record_scan(server_fp, device_id, tier,
                                      code=code, subscriptions_obj=subs, sku=sku)
        except Exception as _q_exc:
            print(f"[ONDEVICE-TELEMETRY] quota error (fail-open): {_q_exc}", flush=True)
        try:
            _increment_scan_counter(source="ondevice")
        except Exception as _s_exc:
            print(f"[ONDEVICE-TELEMETRY] stats error (fail-open): {_s_exc}", flush=True)
        try:
            if sku:
                _increment_sku_scan_freq(sku)
        except Exception as _f_exc:
            print(f"[ONDEVICE-TELEMETRY] freq error (fail-open): {_f_exc}", flush=True)
    resp = jsonify({"ok": True})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _fuzzy_pokemon_identifier(raw_text, num, setcode_map, lookup):
    """
    Rescue an mlkit read where the NNN/NNN number is clean but the set-code
    letters are garbled. Fuzzy-match the alpha tokens against the printed
    set-code map, build {db_id}-{num} candidates, intersect with the REAL
    lookup keyspace, and accept ONLY if exactly one real SKU survives.
    Ambiguous or no match -> None (caller falls back to Vision/CLIP).
    'num' is the clean collector number string, e.g. "58". Returns the
    resolved identifier key (e.g. "rsv10pt5-58") or None.
    """
    text_up = (raw_text or "").upper()
    tokens = re.findall(r'[A-Z]{2,6}', text_up)
    if not tokens:
        return None

    def _close(a, b):
        # startswith-2 either direction, or simple edit-distance <= 1
        if a.startswith(b[:2]) or b.startswith(a[:2]):
            return True
        if abs(len(a) - len(b)) <= 1:
            diffs = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
            return diffs <= 1
        return False

    # identifier key -> resolved SKU, for every set code whose letters are
    # close to one of the read tokens AND which yields a real lookup key
    matched = {}
    for code, db_id in setcode_map.items():
        code_up = code.upper()
        if any(_close(tok, code_up) for tok in tokens):
            key = f"{db_id}-{num}".lower()
            if key in lookup:
                matched[key] = lookup[key]

    # Accept only if the surviving keys resolve to exactly one SKU
    if len(set(matched.values())) == 1:
        return next(iter(matched))
    return None


@app.route('/api/ocr-lookup', methods=['POST'])
def ocr_lookup():
    try:
        from ocr_confirm import parse_pokemon_text, parse_pokemon_denominator, parse_mtg_text, parse_ygo_text
        data = request.get_json(force=True)
        raw_text = (data.get('raw_text') or '').strip()
        game = (data.get('game') or 'pokemon').lower()
        ocr_source = data.get('ocr_source', 'unknown')
        print(f"[OCR-LOOKUP] raw_text from {ocr_source}: {repr(raw_text)}", flush=True)

        if not raw_text:
            return jsonify({'status': 'error', 'message': 'No text provided'}), 400

        text_lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

        # On-device denominator backstop: surface the printed total on a miss too,
        # so the client can veto a conflicting on-device pick (Option A).
        ocr_denom = None
        try:
            if game == 'pokemon':
                ocr_denom = parse_pokemon_denominator(text_lines)
        except Exception:
            ocr_denom = None

        identifier = None

        if game == 'pokemon':
            # ML Kit sometimes reads "WH NNN/NNN" instead of "WHT EN NNN/NNN"
            # Expand known truncations before parsing
            if ocr_source == 'mlkit':
                raw_text = re.sub(r'\bWH\b', 'WHT', raw_text)
                text_lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
            identifier = parse_pokemon_text(text_lines)

        elif game == 'mtg':
            identifier = parse_mtg_text(text_lines)

        elif game == 'ygo':
            ygo_lookup = _identifier_lookup.get('ygo', {})
            set_code, passcode = parse_ygo_text(text_lines)
            if set_code and ygo_lookup.get(set_code):
                identifier = set_code
            elif passcode and ygo_lookup.get(passcode):
                identifier = passcode
            else:
                identifier = set_code or passcode

        # Bare numbers without a set code are ambiguous —
        # can't resolve without set context. Treat as miss.
        if game in ('pokemon', 'mtg'):
            if identifier and '-' not in str(identifier):
                # Fuzzy rescue: mlkit + pokemon only, clean NNN/NNN present.
                # Garbled set-code letters but clean number — match the letters
                # against the set-code map and accept ONLY if it resolves to a
                # single real SKU. On success, set identifier and fall through
                # to the normal lookup so the response shape is identical to an
                # exact hit (status 'ok' + profile + image_id, which the client
                # requires). Ambiguous -> reject -> Vision/CLIP fallback.
                _fuzzy_id = None
                if ocr_source == 'mlkit' and game == 'pokemon':
                    _m = re.search(r'(\d{1,3})\s*/\s*\d{1,3}', raw_text)
                    _num = str(int(_m.group(1))) if _m else str(identifier)
                    _fuzzy_id = _fuzzy_pokemon_identifier(
                        raw_text, _num, _PKM_SETCODE_MAP,
                        _identifier_lookup.get('pokemon', {})
                    )
                if _fuzzy_id:
                    print(f"[OCR-LOOKUP] fuzzy hit: {repr(raw_text.strip())} -> {_fuzzy_id} (source=mlkit)", flush=True)
                    identifier = _fuzzy_id
                else:
                    print(f'[OCR-LOOKUP] Bare number rejected: {identifier} (source={ocr_source})', flush=True)
                    return jsonify({'status': 'not_found', 'ocr_denom': ocr_denom})

        if not identifier:
            return jsonify({'status': 'not_found', 'ocr_denom': ocr_denom})

        game_lookup = _identifier_lookup.get(game, {})
        sku = game_lookup.get(identifier)
        if not sku:
            print(f'[OCR-LOOKUP] No SKU for: {identifier} (source={ocr_source})', flush=True)
            return jsonify({'status': 'not_found', 'ocr_denom': ocr_denom})

        db_root = get_db_root()
        profile = _load_card_profile_for_sku(sku, db_root, get_data_dir())
        if not profile:
            return jsonify({'status': 'not_found', 'ocr_denom': ocr_denom})

        print(f'[OCR-LOOKUP] Hit: {identifier} -> {sku} (source={ocr_source})', flush=True)
        image_id = _image_id_for_sku(sku)
        return jsonify({
            'status': 'ok',
            'source': 'ocr_lookup',
            'ocr_denom': ocr_denom,
            'sku': sku,
            'profile': profile,
            'image_id': image_id
        })

    except Exception as e:
        print(f'[OCR-LOOKUP] Error: {e}', flush=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/jp-denom-check', methods=['POST'])
def jp_denom_check():
    """
    Given a printed denominator (e.g. 165 from "040/165"),
    returns which JP sets match and whether any of them have images.
    Client uses this to skip the GPU scan entirely when a card
    cannot possibly be in the database.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        denom = data.get('denom')
        if not denom or not isinstance(denom, int) or denom <= 0 or denom > 300:
            return jsonify({'allowed_sets': [], 'has_images': False, 'reason': 'invalid_denom'}), 200

        meta = _load_set_metadata()
        matched_sets = []
        for set_id, info in meta.items():
            if not set_id.startswith('jpn-'):
                continue
            if info.get('exclude'):
                continue
            pt = info.get('printed_total')
            t = info.get('total')
            if pt == denom or t == denom:
                matched_sets.append(set_id)

        imaged = [s for s in matched_sets if s in _IMAGED_JP_SETS]
        has_images = len(imaged) > 0

        app.logger.info(
            f"[JP-DENOM] denom={denom} matched={matched_sets} imaged={imaged}"
        )
        return jsonify({
            'allowed_sets': imaged,
            'has_images': has_images,
            'denom': denom
        }), 200

    except Exception as e:
        app.logger.error(f"[JP-DENOM] error: {e}")
        return jsonify({'allowed_sets': [], 'has_images': False, 'reason': 'error'}), 200


@app.route('/api/card-profile/<string:sku>')
def card_profile(sku):
    """Pure profile lookup by SKU for on-device gate rendering. Does NOT record a scan."""
    try:
        db_root = get_db_root()
        profile = _load_card_profile_for_sku(sku, db_root, get_data_dir())
        if not profile:
            return jsonify({'status': 'not_found', 'sku': sku}), 404
        image_id = _image_id_for_sku(sku)
        return jsonify({
            'status': 'ok',
            'sku': sku,
            'profile': profile,
            'image_id': image_id,
        })
    except Exception as e:
        print(f'[CARD-PROFILE] Error for {sku!r}: {e}', flush=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _best_price_hint(prices, cm_updated=None, tcp_updated=None):
    """Pick a best-estimate price + its source currency from a profile.prices dict.

    Freshness-aware: if both sources have a value AND both have an 'updated'
    timestamp, the more recently updated source wins (TCGdex ISO-8601 'Z'
    timestamps compare correctly lexicographically). If only one source has a
    timestamp, that source is preferred. If neither has a timestamp (old
    profiles pre-refresh), falls back to the original behaviour: prefer
    Cardmarket avg_sell (EUR), else TCGPlayer market (USD).

    Returns (value, currency) or (None, None). The client multiplies by the
    matching live fx rate — server does no conversion."""
    if not isinstance(prices, dict):
        return None, None

    cm = prices.get("cardmarket") or {}
    cm_val = None
    if isinstance(cm, dict):
        v = cm.get("avg_sell") or cm.get("trend") or cm.get("low")
        if isinstance(v, (int, float)) and v > 0:
            cm_val = float(v)

    tcg = prices.get("tcgplayer") or {}
    tcg_val = None
    if isinstance(tcg, dict):
        holo = tcg.get("holofoil") or {}
        if isinstance(holo, dict) and isinstance(holo.get("market"), (int, float)) and holo["market"] > 0:
            tcg_val = float(holo["market"])
        else:
            for _variant, _vd in tcg.items():
                if isinstance(_vd, dict) and isinstance(_vd.get("market"), (int, float)) and _vd["market"] > 0:
                    tcg_val = float(_vd["market"])
                    break

    # Both values + both timestamps → newer source wins.
    if cm_val is not None and tcg_val is not None and cm_updated and tcp_updated:
        if str(tcp_updated) > str(cm_updated):
            return tcg_val, "USD"
        return cm_val, "EUR"
    # Only one timestamp present → prefer that source (if it has a value).
    if cm_updated and not tcp_updated and cm_val is not None:
        return cm_val, "EUR"
    if tcp_updated and not cm_updated and tcg_val is not None:
        return tcg_val, "USD"
    # No timestamps (old profiles) → original behaviour: Cardmarket first.
    if cm_val is not None:
        return cm_val, "EUR"
    if tcg_val is not None:
        return tcg_val, "USD"
    return None, None


@app.route("/api/pokemon-search")
def pokemon_search():
    raw = request.args.get("q", "").strip()

    matches = []
    if "/" in raw:
        # "number/total" → match card_number prefix AND exact set_total.
        # Leading zeros stripped on each numeric part so "032/165" matches "32"/"165".
        num_part, _, total_part = raw.partition("/")
        num_part = num_part.strip()
        total_part = total_part.strip()
        if num_part.isdigit():
            num_part = str(int(num_part))
        if total_part.isdigit():
            total_part = str(int(total_part))
        if not num_part:
            return jsonify({"results": [], "count": 0})
        for entry in _pokemon_search_index:
            num_match = entry["number"].startswith(num_part)
            total_match = (str(entry.get("set_total") or "") == total_part) if total_part else True
            if num_match and total_match:
                matches.append(entry)
                if len(matches) >= 12:
                    break
    else:
        q = raw.lower()
        if len(q) < 2:
            return jsonify({"results": [], "count": 0})
        for entry in _pokemon_search_index:
            if entry["name"].lower().startswith(q) or entry["number"].startswith(q):
                matches.append(entry)
                if len(matches) >= 12:
                    break

    db_root = get_db_root()
    data_dir = get_data_dir()
    enriched = []
    for r in matches:
        sku = r["sku"]
        profile = _load_card_profile_for_sku(sku, db_root, data_dir)
        prices = profile.get("prices") if profile else None
        best_val, best_ccy = _best_price_hint(
            prices,
            profile.get("cardmarket_updated") if profile else None,
            profile.get("tcgplayer_updated") if profile else None,
        )
        image_id = None
        try:
            image_id = _image_id_for_sku(sku)
        except Exception:
            image_id = None
        if image_id:
            image_url = f"https://images.grailsweep.com/{image_id}.jpg"
        elif profile and profile.get("image_url_small"):
            image_url = profile.get("image_url_small")
        else:
            image_url = ""
        enriched.append({
            "sku":            sku,
            "name":           r["name"],
            "number":         r["number"],
            "set_name":       r["set_name"],
            "set_total":      r.get("set_total"),
            "image_url":      image_url,
            "prices":         prices,
            "prices_updated": profile.get("prices_updated") if profile else None,
            "best_price":     best_val,
            "best_currency":  best_ccy,
        })

    return jsonify({"results": enriched, "count": len(enriched)})


@app.route("/search")
def search_page():
    return render_template("search.html")


@app.route("/sitemap.xml")
def sitemap():
    from flask import Response
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://grailsweep.com/</loc>
    <lastmod>{date}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://grailsweep.com/match</loc>
    <lastmod>{date}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://grailsweep.com/upgrade</loc>
    <lastmod>{date}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://grailsweep.com/collection</loc>
    <lastmod>{date}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://grailsweep.com/privacy</loc>
    <lastmod>{date}</lastmod>
    <changefreq>yearly</changefreq>
    <priority>0.3</priority>
  </url>
  <url>
    <loc>https://grailsweep.com/terms</loc>
    <lastmod>{date}</lastmod>
    <changefreq>yearly</changefreq>
    <priority>0.3</priority>
  </url>
  <url>
    <loc>https://grailsweep.com/delete-account</loc>
    <lastmod>{date}</lastmod>
    <changefreq>yearly</changefreq>
    <priority>0.3</priority>
  </url>
  <url>
    <loc>https://grailsweep.com/contact</loc>
    <lastmod>{date}</lastmod>
    <changefreq>yearly</changefreq>
    <priority>0.3</priority>
  </url>
</urlset>'''.format(date=today)
    resp = Response(xml, mimetype="application/xml")
    resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=604800"
    return resp


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static", "assets"),
        "grailsweep_app_icon.png",
        mimetype="image/png",
    )


@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
def apple_touch_icon():
    return send_from_directory(
        os.path.join(app.root_path, 'static', 'assets'),
        'grailsweep_app_icon.png',
        mimetype='image/png'
    )


@app.route('/sw.js')
def service_worker():
    from flask import send_file, make_response
    response = make_response(send_file('static/sw.js'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/static/scanner.html')
def scanner():
    from flask import send_file, make_response
    response = make_response(send_file('static/scanner.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route("/img/ras/<sku>.jpg")
def img_ras(sku):
    """Serve raster/illustration images. Checks OneDrive RASTER folder first, then local ras_images fallback."""
    ras_dirs = [
        r"C:\Users\c_a_b\OneDrive\Pictures\RASTER",
        os.path.join(app.root_path, "ras_images"),
    ]
    for ras_dir in ras_dirs:
        for ext in [".jpg", ".png", ".jpeg", ".webp"]:
            filename  = f"{sku}_RAS{ext}"
            file_path = os.path.normpath(os.path.join(ras_dir, filename))
            if os.path.abspath(file_path).startswith(os.path.abspath(ras_dir)):
                if os.path.exists(file_path):
                    return send_file(file_path)
    abort(404)


# ============================================================
# DB Manage (Admin)
# ============================================================


@app.route("/db_manage", methods=["GET", "POST"])
@admin_required
def db_manage():
    init_db()
    db_path = get_images_db_path()

    if request.method == "POST":
        image_id = (request.form.get("image_id") or "").strip()
        sku = (request.form.get("sku") or "").strip()
        desc = (request.form.get("description") or "").strip()

        if not image_id:
            flash("Missing image_id.", "err")
            return redirect(url_for("db_manage"))

        if is_invalid_field(sku) or is_invalid_field(desc):
            flash("Update blocked: SKU + Description cannot be blank/UNKNOWN.", "err")
            return redirect(url_for("db_manage", q=request.args.get("q", ""), page=request.args.get("page", "1")))

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE images SET sku = ?, description = ? WHERE image_id = ?", (sku, desc, image_id))
            conn.commit()
        finally:
            conn.close()

        load_embedding_cache(force=True)
        flash("Updated.", "ok")
        return redirect(url_for("db_manage", q=request.args.get("q", ""), page=request.args.get("page", "1")))

    q = (request.args.get("q") or "").strip()
    page = int(request.args.get("page") or 1)
    page = max(page, 1)
    per_page = 50
    offset = (page - 1) * per_page

    where = ""
    params: List[str] = []
    if q:
        where = "WHERE sku LIKE ? OR description LIKE ? OR original_filename LIKE ? OR image_id LIKE ?"
        like = f"%{q}%"
        params = [like, like, like, like]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM images {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT image_id, sku, description, original_filename, path, added_at
            FROM images
            {where}
            ORDER BY added_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()
    finally:
        conn.close()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("db_manage.html", rows=rows, q=q, page=page, total=total, total_pages=total_pages)


@app.route("/db_delete/<image_id>", methods=["POST"])
@admin_required
def db_delete(image_id):
    init_db()
    db_path = get_images_db_path()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    img_path = None
    try:
        row = conn.execute("SELECT path FROM images WHERE image_id = ?", (image_id,)).fetchone()
        if not row:
            flash("Not found.", "err")
            return redirect(url_for("db_manage"))

        img_path = row["path"]
        conn.execute("DELETE FROM images WHERE image_id = ?", (image_id,))
        conn.commit()
    finally:
        conn.close()

    try:
        if img_path and os.path.exists(img_path):
            os.remove(img_path)
    except Exception:
        pass

    load_embedding_cache(force=True)
    flash("Deleted.", "ok")
    return redirect(url_for("db_manage", q=request.args.get("q", ""), page=request.args.get("page", "1")))


# ============================================================
# Upload + Review (Admin)
# ============================================================


@app.route("/db_upload", methods=["GET", "POST"])
@admin_required
def db_upload():
    if request.method == "GET":
        return render_template("db_upload.html")

    files = request.files.getlist("photos")
    csv_file = request.files.get("csv")

    if not files:
        flash("No photos uploaded.", "err")
        return redirect(url_for("db_upload"))

    csv_map = {}
    if csv_file and csv_file.filename:
        csv_map = parse_csv_mapping(csv_file.read())

    batch_id = str(uuid.uuid4())
    pending_dir = get_pending_dir(batch_id)

    records = []
    for i, f in enumerate(files):
        if not f or not f.filename:
            continue

        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
            continue

        img_id = str(uuid.uuid4())
        out_name = f"{img_id}.jpg"
        out_path = os.path.join(pending_dir, out_name)
        f.save(out_path)

        normalize_uploaded_image(out_path)

        sku = (request.form.get(f"sku_{i}") or "").strip()
        desc = (request.form.get(f"desc_{i}") or "").strip()

        if (not sku or not desc) and csv_map:
            key_candidates = [f.filename]
            stem, _ = os.path.splitext(f.filename)
            key_candidates += [stem, stem + ".jpg", stem + ".jpeg"]
            for k in key_candidates:
                if k in csv_map:
                    sku = sku or (csv_map[k].get("sku") or "").strip()
                    desc = desc or (csv_map[k].get("description") or "").strip()
                    break

        records.append(
            {
                "image_id": img_id,
                "pending_filename": out_name,
                "original_filename": f.filename,
                "sku": sku,
                "description": desc,
            }
        )

    bad = [
        r["original_filename"]
        for r in records
        if is_invalid_field(r.get("sku")) or is_invalid_field(r.get("description"))
    ]
    if bad:
        shutil.rmtree(pending_dir, ignore_errors=True)
        flash(
            "Upload blocked: every image must have SKU + Description (not blank / not 'unknown'). "
            f"Fix these: {', '.join(bad[:6])}" + (" ..." if len(bad) > 6 else ""),
            "err",
        )
        return redirect(url_for("db_upload"))

    Path(os.path.join(pending_dir, "pending.json")).write_text(
        json.dumps({"batch_id": batch_id, "records": records}, indent=2),
        encoding="utf-8",
    )
    return redirect(url_for("db_review", batch_id=batch_id))


@app.route("/db_review/<batch_id>", methods=["GET", "POST"])
@admin_required
def db_review(batch_id):
    pending_dir = get_pending_dir(batch_id)
    meta_path = os.path.join(pending_dir, "pending.json")
    if not os.path.exists(meta_path):
        flash("Pending batch not found.", "err")
        return redirect(url_for("db_upload"))

    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    records = meta.get("records", [])

    if request.method == "GET":
        return render_template("db_review.html", batch_id=batch_id, records=records)

    for rec in records:
        rid = rec["image_id"]
        rec["sku"] = (request.form.get(f"sku_{rid}") or "").strip()
        rec["description"] = (request.form.get(f"desc_{rid}") or "").strip()

    bad = [
        rec.get("original_filename") or rec.get("pending_filename")
        for rec in records
        if is_invalid_field(rec.get("sku")) or is_invalid_field(rec.get("description"))
    ]
    if bad:
        flash(
            "Commit blocked: every image must have SKU + Description (not blank / not 'unknown'). "
            f"Fix these: {', '.join(bad[:6])}" + (" ..." if len(bad) > 6 else ""),
            "err",
        )
        return render_template("db_review.html", batch_id=batch_id, records=records)

    init_db()
    db_path = get_images_db_path()
    img_dir = get_image_db_dir()
    embedder = get_embedder()

    inserted = 0
    conn = sqlite3.connect(db_path)
    try:
        for rec in records:
            image_id = rec["image_id"]
            sku = rec["sku"]
            desc = rec["description"]
            orig = rec.get("original_filename") or ""
            pending_filename = rec.get("pending_filename")

            src_path = os.path.join(pending_dir, pending_filename)
            if not os.path.exists(src_path):
                continue

            dst_path = os.path.join(img_dir, f"{image_id}.jpg")
            shutil.copy2(src_path, dst_path)

            normalize_uploaded_image(dst_path)
            upload_to_r2(image_id, dst_path)

            try:
                emb = embedder.embed_path(dst_path, multi_crop=True, suppress_bg=True)
            except TypeError:
                emb = embedder.embed_path(dst_path)

            if emb is None:
                continue

            emb = np.asarray(emb, dtype=np.float32).reshape(-1)
            conn.execute(
                """
                INSERT OR REPLACE INTO images(image_id, sku, description, original_filename, path, added_at, embedding)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    image_id,
                    sku,
                    desc,
                    orig,
                    dst_path,
                    datetime.utcnow().isoformat(),
                    to_blob(emb),
                ),
            )
            inserted += 1

        conn.commit()
    finally:
        conn.close()

    load_embedding_cache(force=True)
    shutil.rmtree(pending_dir, ignore_errors=True)

    flash(f"Batch committed. Inserted/updated rows: {inserted}", "ok")
    return redirect(url_for("admin"))


# ============================================================
# Image Quality Review
# ============================================================


@app.route("/db_image_review")
@admin_required
def db_image_review():
    init_db()
    db_path = get_images_db_path()
    q = (request.args.get("q") or "").strip()
    show = request.args.get("show", "all")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total_count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        flagged_count = conn.execute("SELECT COUNT(*) FROM images WHERE flagged = 1").fetchone()[0]

        where_parts: List[str] = []
        params: List = []
        if q:
            where_parts.append("(sku LIKE ? OR original_filename LIKE ?)")
            like = f"%{q}%"
            params += [like, like]
        if show == "flagged":
            where_parts.append("flagged = 1")

        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = conn.execute(
            f"SELECT image_id, sku, original_filename, flagged FROM images {where} ORDER BY sku, added_at",
            params,
        ).fetchall()
    finally:
        conn.close()

    from collections import OrderedDict
    by_sku: dict = OrderedDict()
    for row in rows:
        sku = row["sku"] or "UNKNOWN"
        if sku not in by_sku:
            by_sku[sku] = []
        by_sku[sku].append(dict(row))

    return render_template(
        "db_image_review.html",
        total_count=total_count,
        flagged_count=flagged_count,
        q=q,
        show=show,
        by_sku=by_sku,
    )


@app.route("/db_flag_image/<image_id>", methods=["POST"])
@admin_required
def db_flag_image(image_id):
    init_db()
    flagged = int(request.form.get("flagged", 0))
    conn = sqlite3.connect(get_images_db_path())
    try:
        conn.execute("UPDATE images SET flagged = ? WHERE image_id = ?", (flagged, image_id))
        conn.commit()
    finally:
        conn.close()
    return ("", 204)


@app.route("/db_replace_image/<image_id>", methods=["POST"])
@admin_required
def db_replace_image(image_id):
    init_db()
    f = request.files.get("image")
    if not f or not f.filename:
        flash("No image uploaded.", "err")
        return redirect(url_for("db_image_review"))

    db_path = get_images_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT sku, path FROM images WHERE image_id = ?", (image_id,)).fetchone()
    finally:
        conn.close()

    if not row:
        flash("Image not found.", "err")
        return redirect(url_for("db_image_review"))

    img_dir = get_image_db_dir()
    out_path = os.path.join(img_dir, f"{image_id}.jpg")
    f.save(out_path)
    normalize_uploaded_image(out_path)
    upload_to_r2(image_id, out_path)

    embedder = get_embedder()
    v = get_vertical()
    use_multi_crop = v.get("multi_crop", True)
    use_suppress_bg = v.get("suppress_bg", True)
    try:
        emb = embedder.embed_path(out_path, multi_crop=use_multi_crop, suppress_bg=use_suppress_bg)
    except TypeError:
        emb = embedder.embed_path(out_path)

    emb_blob = to_blob(np.asarray(emb, dtype=np.float32).reshape(-1)) if emb is not None else None

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE images SET path = ?, flagged = 0, embedding = ? WHERE image_id = ?",
            (out_path, emb_blob, image_id),
        )
        conn.commit()
    finally:
        conn.close()

    load_embedding_cache(force=True)
    flash("Image replaced and re-embedded.", "ok")
    return redirect(url_for("db_image_review"))


# ============================================================
# Capture submit (camera -> data URL)
# ============================================================


@app.route("/capture_submit", methods=["POST"])
def capture_submit():
    try:
        data1 = (request.form.get("image_data_1") or "").strip()
        data2 = (request.form.get("image_data_2") or "").strip()
        two_mode = (request.form.get("two_mode") or "0").strip() == "1"

        # Groove counts (optional; -1 = unknown)
        query_category = request.form.get("key_type", "").strip().upper()
        query_profile = parse_all_fields(dict(request.form))

        q1_fn = _save_dataurl_to_query_jpg(data1)
        q2_fn = None
        if two_mode and data2:
            q2_fn = _save_dataurl_to_query_jpg(data2)

        q_dir = Path(app.root_path) / "static" / "query"
        q1_path = q_dir / q1_fn
        q2_path = (q_dir / q2_fn) if q2_fn else None

        # ── Clean images + auto groove detect ──
        clean1, clean2, auto_fg, auto_bg, _clean_diag = (
            _clean_and_auto_grooves(
                str(q1_path),
                str(q2_path) if q2_path else None,
            )
        )
        print(f"[DEBUG CLEAN] front_clean={clean1 != str(q1_path)} back_clean={clean2 != (str(q2_path) if q2_path else None)} auto_fg={auto_fg} auto_bg={auto_bg}", flush=True)

        TOP_K_SKU = int(CFG.get("top_k_sku", 20))
        TOP_M_PER_SKU = int(CFG.get("top_m_per_sku", 3))
        CAP_PER_SKU = int(CFG.get("cap_per_sku", 30))

        softmax_temp = float(CFG.get("softmax_temp", 0.015))
        low_cert_prob = float(CFG.get("low_cert_prob", 0.55))
        low_cert_prob_gap = float(CFG.get("low_cert_prob_gap", 0.15))

        cons_n = int(CFG.get("consistency_n", 5))
        cons_sigma = float(CFG.get("consistency_sigma", 0.040))

        emb = get_embedder()

        params = dict(
            multi_crop=bool(CFG.get("auto_mode_b_multi_crop", True)),
            suppress_bg=bool(CFG.get("auto_mode_b_suppress_bg", True)),
            max_side=int(CFG.get("auto_mode_b_max_side", 1024)),
        )

        # PERF: Back image only contributes 3% weight — single-crop saves ~2s
        params_back = dict(
            multi_crop=False,
            suppress_bg=bool(CFG.get("auto_mode_b_suppress_bg", True)),
            max_side=int(CFG.get("auto_mode_b_max_side", 1024)),
        )

        # Embed from ORIGINALS — rembg cleaning is only for groove detection.
        # CLIP's own _fast_bg_crop is tuned for keys and matches the DB embeddings.
        qf = _embed_one_query(emb, str(q1_path), **params)
        qb = None
        if q2_path is not None and q2_path.exists():
            qb = _embed_one_query(emb, str(q2_path), **params_back)

        jp_mode = request.form.get('jp_mode', 'en')
        exclude_jpn = (jp_mode != 'jp')
        # Read allowed JP sets from the request (sent by client after denom check)
        _allowed_sets_raw = request.form.get('allowed_jpn_sets', '').strip()
        _allowed_jpn_sets = set(_allowed_sets_raw.split(',')) if _allowed_sets_raw else None

        results, low_cert, _diag = _run_match_paired_two_stage(
            qf,
            qb,
            query_front_path=str(q1_path),
            query_back_path=str(q2_path) if (q2_path is not None and q2_path.exists()) else None,
            top_k_sku=TOP_K_SKU,
            top_m_per_sku=TOP_M_PER_SKU,
            cap_per_sku=CAP_PER_SKU,
            softmax_temp=softmax_temp,
            low_cert_prob=low_cert_prob,
            low_cert_prob_gap=low_cert_prob_gap,
            cons_n=cons_n,
            cons_sigma=cons_sigma,
            query_category=query_category,
            query_profile=query_profile,
            auto_front_grooves=auto_fg,
            auto_back_grooves=auto_bg,
            exclude_jpn=exclude_jpn,
            allowed_jpn_sets=_allowed_jpn_sets,
        )

        # DINOv2 tie-breaker on top 2 if scores are close
        _req_mode = request.form.get("mode", request.args.get("mode", "precise"))
        print(f"[MODE] Scan mode: {_req_mode}", flush=True)
        if results and _req_mode != "fast":
            results = _dinov2_tiebreak(results, str(q1_path))

        if not results:
            return render_template("match.html", error="No SKU results found (cache may be empty).")

        # Option B: only charge scan on confident match (score >= 0.65)
        _top_score = (results[0].get('score', 0) if isinstance(results[0], dict) else getattr(results[0], 'score', 0))
        if _top_score < 0.65:
            return render_template("match.html", error="No confident match found.")
        _sku_for_charge_cs = results[0].get("sku", "") if isinstance(results[0], dict) else getattr(results[0], "sku", "")
        scan_decision = _evaluate_scan_decision(request, sku=_sku_for_charge_cs)
        if not scan_decision.get("allowed", True):
            return jsonify({
                "error": "free_scan_limit_reached",
                "limit": scan_decision.get("limit", 150),
                "count": scan_decision.get("count", 150),
                "remaining": 0,
                "tier": "free",
                "message": "You've used all 150 free scans this month. Top-up to continue scanning."
            }), 402
        _increment_scan_counter()

        # Save to match history
        _save_match_history(q1_fn, q2_fn, results, low_cert)

        import uuid as _uuid
        feedback_token = str(_uuid.uuid4())
        session["feedback_token"] = feedback_token
        session["feedback_q1"] = q1_fn
        session["feedback_q2"] = q2_fn
        session["feedback_skus"] = [r.sku for r in results]

        grade = _safe_grade(str(q1_path))

        return render_template(
            "results.html",
            results=results,
            query_filename=q1_fn,
            query_filename_2=q2_fn,
            low_cert=low_cert,
            feedback_token=feedback_token,
            selected_mode="CAPTURE_PAIRED_TWO_STAGE_MODE_B_SOFT_TYPE_SWAPSAFE_FLOOR",
            ocr_status="",
            ocr_sku="",
            grade=grade,
        )

    except Exception as e:
        current_app.logger.exception("capture_submit failed")
        return render_template("match.html", error=f"Capture failed: {e}")


# ============================================================
# Match Feedback
# ============================================================

@app.route("/feedback", methods=["POST"])
def submit_feedback():
    import json as _json
    from datetime import datetime as _dt

    token       = request.form.get("token", "")
    verdict     = request.form.get("verdict", "")
    confirmed   = request.form.get("confirmed_sku", "")
    confirmed_rank = request.form.get("confirmed_rank", "")
    is_test     = 1 if request.form.get("is_test") == "1" else 0

    if token != session.get("feedback_token", ""):
        return ("Invalid token", 400)

    q1   = session.get("feedback_q1", "")
    q2   = session.get("feedback_q2", "")
    skus = _json.dumps(session.get("feedback_skus", []))

    db_path = get_images_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO match_feedback
              (submitted_at, query_filename, query_filename_2, confirmed_sku, confirmed_rank, result_skus, verdict, is_test)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            _dt.utcnow().isoformat(),
            q1, q2,
            confirmed or None,
            int(confirmed_rank) if confirmed_rank else None,
            skus,
            verdict,
            is_test,
        ))
        conn.commit()

    session.pop("feedback_token", None)
    return ("OK", 200)


# ============================================================
# Match History
# ============================================================


def _save_match_history(query_filename, query_filename_2, results, low_cert):
    """Save a match result to the history table."""
    if not results:
        return
    try:
        import json as _json
        from datetime import datetime as _dt

        top = results[0]
        top_sku = top["sku"] if isinstance(top, dict) else top.sku
        top_score = float(top["score"] if isinstance(top, dict) else top.score)
        top_conf = float(top["prob"] if isinstance(top, dict) else top.prob) * 100.0
        top_sim = float(top["similarity"] if isinstance(top, dict) else top.similarity)
        # Store match strength (similarity rescaled to intuitive 0-100%)
        top_strength = max(0.0, min(100.0, ((top_sim - 0.80) / 0.18) * 100.0))
        result_skus = _json.dumps([
            (r["sku"] if isinstance(r, dict) else r.sku)
            for r in results[:5]
        ])

        db_path = get_images_db_path()
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                INSERT INTO match_history
                  (matched_at, query_filename, query_filename_2, top_sku, top_score, top_confidence, result_skus, low_cert)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _dt.utcnow().isoformat(),
                query_filename or "",
                query_filename_2 or "",
                top_sku,
                top_score,
                top_strength,
                result_skus,
                1 if low_cert else 0,
            ))
            conn.commit()
    except Exception:
        pass  # history saving must never break the match flow


@app.route("/history")
def match_history():
    init_db()
    db_path = get_images_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM match_history ORDER BY matched_at DESC LIMIT 30"
        ).fetchall()

    import json as _json
    entries = []
    for row in rows:
        try:
            skus_list = _json.loads(row["result_skus"] or "[]")
        except Exception:
            skus_list = []

        entries.append({
            "matched_at": row["matched_at"] or "",
            "query_filename": row["query_filename"] or "",
            "query_filename_2": row["query_filename_2"] or "",
            "top_sku": row["top_sku"] or "—",
            "top_score": float(row["top_score"] or 0),
            "top_confidence": float(row["top_confidence"] or 0),
            "result_skus_list": skus_list,
            "low_cert": bool(row["low_cert"]),
        })

    return render_template("history.html", entries=entries)


# ============================================================
# Collection
# ============================================================

_PREMIUM_TIERS = {"monthly", "annual", "lifetime", "legacy"}


def _ssr_subscription():
    from flask import request as _req
    import urllib.parse as _up
    try:
        _code_raw = _req.cookies.get("gs_access_code", "")
        _code = _up.unquote(_code_raw).strip().upper()
        if not _code:
            print("[SSR-SUBS] no gs_access_code cookie — guest", flush=True)
            return {"is_premium": False, "tier": None, "code": None}
        # Load subs once — used by both legacy and subscription paths
        _subs = _load_subs()
        # Legacy codes live in config.json; honour an explicit cancelled status if one exists in subs.json
        if _code in CFG.get("premium_codes", []):
            _legacy_entry = _subs.get(_code)
            if _legacy_entry and _legacy_entry.get("status") == "cancelled":
                print(f"[SSR-SUBS] code={_code} tier=legacy status=cancelled — not premium", flush=True)
                return {"is_premium": False, "tier": None, "code": _code}
            print(f"[SSR-SUBS] code={_code} tier=legacy (config)", flush=True)
            return {"is_premium": True, "tier": "legacy", "code": _code}
        _entry = _subs.get(_code)
        if not _entry:
            print(f"[SSR-SUBS] code={_code} not found in subs", flush=True)
            return {"is_premium": False, "tier": None, "code": _code}
        _tier = _entry.get("tier", "monthly")
        _status = _entry.get("status")
        if _status == "cancelled":
            print(f"[SSR-SUBS] code={_code} status=cancelled — not premium", flush=True)
            return {"is_premium": False, "tier": None, "code": _code}
        _expires = _entry.get("expires_at")
        if _expires:
            try:
                from datetime import datetime as _dt
                if _dt.fromisoformat(_expires) < _dt.utcnow():
                    print(f"[SSR-SUBS] code={_code} expired at {_expires} — not premium", flush=True)
                    return {"is_premium": False, "tier": None, "code": _code}
            except Exception:
                pass
        _is_prem = _tier in _PREMIUM_TIERS
        print(f"[SSR-SUBS] code={_code} tier={_tier} status={_status}", flush=True)
        return {"is_premium": _is_prem, "tier": _tier, "code": _code}
    except Exception as _se:
        print(f"[SSR-SUBS] error: {_se}", flush=True)
        return {"is_premium": False, "tier": None, "code": None}


@app.route("/collection")
def collection():
    _sub = _ssr_subscription()
    _is_prem = bool(_sub.get("is_premium"))
    print(f"[SSR-PREMIUM] code={_sub.get('code')} tier={_sub.get('tier')} is_premium={_is_prem}", flush=True)
    if _is_prem:
        _ssr_col = []
        try:
            if _sub.get("code"):
                _cols = _load_collections()
                _ssr_col = _cols.get(_sub["code"], [])
                print(f"[SSR-COLLECTION] code={_sub['code']} cards={len(_ssr_col)}", flush=True)
            else:
                print("[SSR-COLLECTION] no code — none rendered", flush=True)
        except Exception as _ce:
            print(f"[SSR-COLLECTION] error: {_ce}", flush=True)
    else:
        _ssr_col = []
        print("[SSR-COLLECTION] gated: non-premium user, prefetch skipped", flush=True)
    return render_template("collection.html", ssr_sub=_sub, ssr_is_premium=_is_prem, ssr_collection=_ssr_col)


@app.route("/upgrade")
def upgrade():
    _sub = _ssr_subscription()
    _is_prem = bool(_sub.get("is_premium"))
    print(f"[SSR-PREMIUM] code={_sub.get('code')} tier={_sub.get('tier')} is_premium={_is_prem}", flush=True)
    return render_template("upgrade.html", ssr_sub=_sub, ssr_is_premium=_is_prem)


@app.route("/api/customer-portal", methods=["POST"])
def customer_portal():
    from flask import jsonify
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "") or CFG.get("stripe_secret_key", "")

    data = request.json
    email = data.get("email", "").strip()

    if not email:
        return jsonify({"error": "Email required"}), 400

    try:
        # Find customer by email
        customers = stripe.Customer.list(email=email, limit=1)
        if not customers.data:
            return jsonify({"error": "No subscription found for this email"}), 404

        customer_id = customers.data[0].id

        # Create portal session
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url="https://grailsweep.com/collection",
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _send_alert_confirmation(email, card_name, target_low, target_high):
    targets = []
    if target_low:
        targets.append(f"Notify when price drops below £{float(target_low):.2f}")
    if target_high:
        targets.append(f"Notify when price rises above £{float(target_high):.2f}")

    bullets_html = "".join(
        f'<li style="margin:6px 0;color:#1a1830;">{t}</li>' for t in targets
    )

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1830;background:#ffffff;">
        <h1 style="color:#1a1830;font-size:1.3rem;font-weight:600;margin:0 0 16px 0;">Price alert confirmed</h1>
        <p style="margin:0 0 16px 0;line-height:1.5;">Your price alert for <strong>{card_name}</strong> has been registered.</p>
        <div style="background:#f6f4fb;border-left:3px solid #b14dff;border-radius:4px;padding:16px 20px;margin:20px 0;">
            <ul style="margin:0;padding-left:18px;">
                {bullets_html}
            </ul>
        </div>
        <p style="color:#5f5e5a;font-size:0.9rem;line-height:1.5;margin:16px 0;">
            We check prices weekly and will email you when your target is reached.
            Price alerts are a GrailSweep Pro feature.
        </p>
        <hr style="border:none;border-top:1px solid #e5e2ed;margin:24px 0 16px 0;">
        <p style="font-size:0.8rem;color:#888780;margin:0;">
            GrailSweep — Trading card scanner and price reference<br>
            <a href="https://grailsweep.com" style="color:#7c3aed;text-decoration:none;">grailsweep.com</a>
        </p>
    </div>
    """

    text_lines = [
        f"Price alert confirmed for {card_name}",
        "",
        "Your price alert has been registered:",
    ]
    for t in targets:
        text_lines.append(f"  - {t}")
    text_lines += [
        "",
        "We check prices weekly and will email you when your target is reached.",
        "Price alerts are a GrailSweep Pro feature.",
        "",
        "GrailSweep",
        "https://grailsweep.com",
    ]
    text_body = "\n".join(text_lines)

    try:
        gs_send_email(
            to=email,
            subject=f"Price alert set for {card_name}",
            html=html,
            text=text_body,
        )
        print(f"[ALERTS] Confirmation sent to {email}")
    except Exception as e:
        print(f"[ALERTS] Confirmation email failed: {e}")


@app.route("/api/set-alert", methods=["POST"])
def set_price_alert():
    from flask import jsonify
    import json, os

    data = request.json
    email       = data.get("email", "").strip()
    code        = data.get("code", "").strip().upper()
    sku         = data.get("sku", "").strip()
    card_name   = data.get("card_name", "").strip()
    target_low  = data.get("target_low")
    target_high = data.get("target_high")
    current_gbp = data.get("current_gbp", 0)

    if not email or not sku:
        return jsonify({"error": "Email and SKU required"}), 400

    alerts_path = "/modal_data/price_alerts.json" if os.path.exists("/modal_data") else "price_alerts.json"
    try:
        with open(alerts_path, "r") as f:
            alerts = json.load(f)
    except:
        alerts = []

    for a in alerts:
        if a.get("email") == email and a.get("sku") == sku:
            return jsonify({"error": "Alert already exists for this card and email"}), 400

    alerts.append({
        "email":       email,
        "code":        code,
        "sku":         sku,
        "card_name":   card_name,
        "target_low":  target_low,
        "target_high": target_high,
        "current_gbp": current_gbp,
        "created":     __import__("datetime").datetime.utcnow().isoformat(),
        "triggered":   False
    })

    with open(alerts_path, "w") as f:
        json.dump(alerts, f, indent=2)

    _send_alert_confirmation(email, card_name, target_low, target_high)

    return jsonify({"status": "ok", "message": "Alert set successfully"})


@app.route("/api/delete-alert", methods=["POST"])
def delete_price_alert():
    from flask import jsonify
    import json, os

    data  = request.json
    email = data.get("email", "").strip()
    sku   = data.get("sku", "").strip()

    alerts_path = "/modal_data/price_alerts.json" if os.path.exists("/modal_data") else "price_alerts.json"
    try:
        with open(alerts_path, "r") as f:
            alerts = json.load(f)
    except:
        alerts = []

    original_count = len(alerts)
    alerts = [a for a in alerts if not (a.get("email") == email and a.get("sku") == sku)]
    deleted = len(alerts) < original_count

    with open(alerts_path, "w") as f:
        json.dump(alerts, f, indent=2)

    if not deleted:
        return jsonify({"status": "not_found"})
    return jsonify({"status": "ok"})


@app.route("/privacy")
def privacy():
    resp = make_response(render_template("privacy.html"))
    resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=604800"
    return resp


@app.route("/terms")
def terms():
    resp = make_response(render_template("terms.html"))
    resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=604800"
    return resp


@app.route("/delete-account")
def delete_account():
    resp = make_response(render_template("delete_account.html"))
    resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=604800"
    return resp


@app.route("/ocr-test")
def ocr_test():
    return render_template("ocr_test.html")


@app.route("/contact")
def contact():
    resp = make_response(render_template("contact.html"))
    resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=604800"
    return resp



@app.route("/api/deep_grade", methods=["POST"])
def deep_grade():
    import anthropic
    import base64
    import json as _json

    data       = request.json
    image_path = data.get("image_path", "")
    card_name  = data.get("card_name", "Unknown card")

    if not image_path:
        return jsonify({"error": "No image path provided"}), 400

    import os
    full_path = None
    _basename = os.path.basename(image_path)
    candidates = [
        image_path,
        os.path.join("/modal_data", image_path.lstrip("/")),
        os.path.join("/app", image_path.lstrip("/")),
        os.path.join(app.root_path, "static", "query", _basename),
    ]
    for p in candidates:
        if os.path.exists(p):
            full_path = p
            break

    if not full_path:
        return jsonify({"error": "Image not found"}), 404

    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(full_path).convert("RGB")
        # Resize if too large — Anthropic limit is 5MB
        max_size = (1568, 1568)
        img.thumbnail(max_size, Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

        prompt = (
            f"You are an expert trading card grader with years of experience grading "
            f"Pokemon TCG, Magic: The Gathering, and Yu-Gi-Oh cards.\n\n"
            f"Analyse this image of {card_name} and provide a condition grade.\n\n"
            f"STEP 0 — AUTHENTICITY CHECK (complete before condition assessment)\n"
            f"Examine this card for signs of being counterfeit. Assess whichever are visible:\n"
            f"- Print quality: sharpness, colour accuracy, no blurring, pixelation or colour bleed\n"
            f"- Font: correct weight, spacing and style for all text (card name, HP, attacks, copyright)\n"
            f"- Borders: even and consistent width on all four sides\n"
            f"- Card-specific markers:\n"
            f"  * Pokémon: holo foil pattern, energy symbol accuracy, HP within plausible range for era, rarity symbol shape, copyright line format, Poké Ball pattern on card back if visible\n"
            f"  * MTG: border colour correct for set era, set symbol matches collector number, mana cost symbols, copyright line\n"
            f"  * YGO: hologram sticker at bottom right, foil on name bar and effect box, ATK/DEF format, Konami copyright line\n"
            f'If image quality prevents a confident assessment, use verdict "uncertain" with confidence "low" and explain why in the note.\n'
            f'Output as JSON field "authenticity": {{"verdict": "likely_genuine"|"uncertain"|"likely_fake", "confidence": "high"|"medium"|"low", "flags": [array of specific concerns, empty array if none], "note": "one sentence summary"}}\n\n'
            f"STEP 1 — Hard defect check. You MUST check for each of these before scoring.\n"
            f"RULE A — Corner or crease damage: If ANY corner has physically absent material, tearing, or jagged edges, OR if the card body has any visible crease, fold, or bend → score MUST be 3.0 or below, label Very Good or below.\n"
            f"RULE C — Surface crease or deep scratch: If there is a heavy surface crease or deep scratch → score MUST be 5.0 or below, label Excellent or below.\n"
            f"If ANY rule above applies, apply that ceiling and skip STEP 2 entirely. Do not average. Do not consider other attributes.\n\n"
            f"STEP 2 — Normal assessment (only if no hard defects found):\n"
            f"1. Centering (are borders even on all sides?)\n"
            f"2. Corners (TCG cards have naturally rounded corners by design — only flag corners with missing material, chips, bends, or fraying beyond the natural card shape)\n"
            f"3. Edges (clean, or chipped/worn?)\n"
            f"4. Surface (scratches, print lines, holo damage, stains?)\n"
            f"5. Overall presentation\n\n"
            f"Be strict — align with PSA/BGS conservatism. When in doubt, grade lower.\n\n"
            f"Respond with ONLY valid JSON in this exact format, no other text:\n"
            f'{{\n'
            f'  "authenticity": {{"verdict": "likely_genuine", "confidence": "high", "flags": [], "note": "Print quality and card markers consistent with a genuine card."}},\n'
            f'  "score": <number 1-10 with one decimal>,\n'
            f'  "label": "<one of: Gem Mint / Mint / Near Mint-Mint / Near Mint / Excellent-Mint / Excellent / Very Good-Excellent / Very Good / Good / Poor>",\n'
            f'  "centering": "<Poor/Fair/Good/Excellent>",\n'
            f'  "corners": "<Poor/Fair/Good/Excellent>",\n'
            f'  "edges": "<Poor/Fair/Good/Excellent>",\n'
            f'  "surface": "<Poor/Fair/Good/Excellent>",\n'
            f'  "summary": "<2-3 sentence plain English explanation, describing any hard defects found but DO NOT specify which corner (left/right/top/bottom) — just say \'a corner\' shows damage>"\n'
            f'}}'
        )

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        text = message.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = _json.loads(text)
        result["method"] = "deep"
        authenticity = result.get("authenticity", {
            "verdict": "uncertain",
            "confidence": "low",
            "flags": [],
            "note": "Authenticity assessment unavailable."
        })
        result["authenticity"] = authenticity
        return jsonify(result)

    except Exception as e:
        print(f"[DEEP_GRADE] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/deep_grade_url", methods=["POST"])
def deep_grade_url():
    import anthropic
    import base64
    import json as _json
    import urllib.request
    import os

    data      = request.json
    image_url = data.get("image_url", "")
    card_name = data.get("card_name", "Unknown card")

    if not image_url:
        return jsonify({"error": "No image URL provided"}), 400

    # Fix relative URLs — prepend Modal base URL if needed
    if image_url.startswith("/"):
        _base = os.environ.get("MODAL_BASE_URL", "https://c-a-buckley--matchit-api-serve.modal.run")
        image_url = _base.rstrip("/") + image_url

    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "GrailSweep/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            img_bytes = resp.read()
        img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")

        if image_url.lower().endswith(".png"):
            media_type = "image/png"
        elif image_url.lower().endswith(".webp"):
            media_type = "image/webp"
        else:
            media_type = "image/jpeg"

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

        prompt = f"""You are an expert trading card grader with years of experience grading Pokemon TCG, Magic: The Gathering, and Yu-Gi-Oh cards.

Analyse this image of {card_name} and provide a condition grade.

STEP 0 — AUTHENTICITY CHECK (complete before condition assessment)
Examine this card for signs of being counterfeit. Assess whichever are visible:
- Print quality: sharpness, colour accuracy, no blurring, pixelation or colour bleed
- Font: correct weight, spacing and style for all text (card name, HP, attacks, copyright)
- Borders: even and consistent width on all four sides
- Card-specific markers:
  * Pokémon: holo foil pattern, energy symbol accuracy, HP within plausible range for era, rarity symbol shape, copyright line format, Poké Ball pattern on card back if visible
  * MTG: border colour correct for set era, set symbol matches collector number, mana cost symbols, copyright line
  * YGO: hologram sticker at bottom right, foil on name bar and effect box, ATK/DEF format, Konami copyright line
If image quality prevents a confident assessment, use verdict "uncertain" with confidence "low" and explain why in the note.
Output as JSON field "authenticity": {{"verdict": "likely_genuine"|"uncertain"|"likely_fake", "confidence": "high"|"medium"|"low", "flags": [array of specific concerns, empty array if none], "note": "one sentence summary"}}

STEP 1 — Hard defect check. You MUST check for each of these before scoring.
RULE A — Corner or crease damage: If ANY corner has physically absent material, tearing, or jagged edges, OR if the card body has any visible crease, fold, or bend → score MUST be 3.0 or below, label Very Good or below.
RULE C — Surface crease or deep scratch: If there is a heavy surface crease or deep scratch → score MUST be 5.0 or below, label Excellent or below.
If ANY rule above applies, apply that ceiling and skip STEP 2 entirely. Do not average. Do not consider other attributes.

STEP 2 — Normal assessment (only if no hard defects found):
1. Centering (are borders even on all sides?)
2. Corners (TCG cards have naturally rounded corners by design — only flag corners with missing material, chips, bends, or fraying beyond the natural card shape)
3. Edges (clean, or chipped/worn?)
4. Surface (scratches, print lines, holo damage, stains?)
5. Overall presentation

Be strict — align with PSA/BGS conservatism. When in doubt, grade lower.

Respond with ONLY valid JSON in this exact format, no other text:
{{
  "authenticity": {{"verdict": "likely_genuine", "confidence": "high", "flags": [], "note": "Print quality and card markers consistent with a genuine card."}},
  "score": <number 1-10 with one decimal>,
  "label": "<one of: Gem Mint / Mint / Near Mint-Mint / Near Mint / Excellent-Mint / Excellent / Very Good-Excellent / Very Good / Good / Poor>",
  "centering": "<Poor/Fair/Good/Excellent>",
  "corners": "<Poor/Fair/Good/Excellent>",
  "edges": "<Poor/Fair/Good/Excellent>",
  "surface": "<Poor/Fair/Good/Excellent>",
  "summary": "<2-3 sentence plain English explanation, describing any hard defects found but DO NOT specify which corner (left/right/top/bottom) — just say 'a corner' shows damage>"
}}"""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        text = message.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = _json.loads(text)
        result["method"] = "deep"
        authenticity = result.get("authenticity", {
            "verdict": "uncertain",
            "confidence": "low",
            "flags": [],
            "note": "Authenticity assessment unavailable."
        })
        result["authenticity"] = authenticity
        return jsonify(result)

    except Exception as e:
        print(f"[DEEP_GRADE_URL] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/price_history")
def price_history_api():
    sku = request.args.get('sku', '')
    history = _load_price_history()
    entries = history.get(sku, [])
    return jsonify({"sku": sku, "history": entries})


@app.route("/api/referral_code", methods=["POST"])
def get_referral_code():
    data = request.json
    access_code = data.get("access_code", "").strip().upper()
    if not access_code:
        return jsonify({"error": "No access code provided"}), 400
    ref_code = _get_or_create_ref_code(access_code)
    if not ref_code:
        # Check if it's a legacy code from config.json
        # Legacy codes are authoritative in config — do NOT write to subscriptions.json
        # (that write fails on cold Modal containers and is unnecessary since _ssr_subscription
        # now checks config.json directly as a fast path)
        legacy_codes = CFG.get("premium_codes", [])
        if access_code in legacy_codes:
            ref_code = _get_or_create_ref_code(access_code)
        if not ref_code:
            return jsonify({"error": "Access code not found"}), 404
    subs = _load_subs()
    entry = subs.get(access_code, {})
    return jsonify({
        "ref_code": ref_code,
        "ref_count": entry.get("ref_count", 0),
        "ref_max": entry.get("ref_max", 10)
    })


@app.route("/admin/create_referral_coupon")
def create_referral_coupon():
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "") or CFG.get("stripe_secret_key", "")
    try:
        coupon = stripe.Coupon.create(
            id="GRAILSWEEP_REFERRAL",
            name="Referral Discount - First Month",
            amount_off=100,
            currency="gbp",
            duration="once",
        )
        return jsonify({"created": True, "id": coupon.id})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/admin/scans-diagnostic", methods=["GET"])
def scans_diagnostic():
    if request.args.get("token") != "scans-diag-2026":
        return jsonify({"error": "forbidden"}), 403
    import db as _db
    fingerprint = request.args.get("fingerprint") or None
    device_id   = request.args.get("device_id") or None
    action = request.args.get("action", "read")
    if action == "read":
        return jsonify(_db.diagnostic_dump(fingerprint, device_id))
    elif action == "increment":
        return jsonify(_db.increment_free_scans(fingerprint, device_id))
    elif action == "check_and_record":
        import urllib.parse as _up
        ua        = request.user_agent.string
        lang      = request.headers.get("Accept-Language", "")
        addr      = request.headers.get("CF-Connecting-IP",
                    request.headers.get("X-Forwarded-For", request.remote_addr or ""))
        server_fp = _db.compute_server_fingerprint(ua, lang, addr)
        did       = request.args.get("device_id") or request.cookies.get("matchit_device_id_v1") or None
        tier      = request.args.get("tier") or None
        return jsonify(_db.check_and_record_scan(server_fp, did, tier))
    else:
        return jsonify({"error": "unknown action, use read, increment, or check_and_record"}), 400


@app.route("/admin/tier-usage-diagnostic", methods=["GET"])
def tier_usage_diagnostic():
    if request.args.get("token") != "tier-diag-2026":
        return jsonify({"error": "forbidden"}), 403
    import db as _db
    code   = request.args.get("code", "").strip().upper()
    action = request.args.get("action", "read")
    if not code:
        return jsonify({"error": "code parameter required"}), 400
    subs = _load_subs()
    sub  = subs.get(code)
    if not sub:
        return jsonify({"error": "code not found in subscriptions"}), 404
    tier = sub.get("tier")
    if action == "read":
        tier_state = None
        if tier in ("monthly", "annual"):
            try:
                tier_state = _db.read_tier_state(code, tier, subs)
            except Exception as _e:
                tier_state = {"error": str(_e)}
        redacted_sub = {k: v for k, v in sub.items()
                        if k not in ("stripe_subscription_id", "email")}
        return jsonify({
            "code":                code,
            "tier":                tier,
            "tier_state":          tier_state,
            "subscription_record": redacted_sub,
        })
    if action == "reset_80pct":
        sub["tier_warned_80pct"] = False
        subs[code] = sub
        _save_subs(subs)
        return jsonify({"ok": True, "reset": "tier_warned_80pct"})
    if action == "reset_transition":
        sub["tier_transition_warned"] = False
        subs[code] = sub
        _save_subs(subs)
        return jsonify({"ok": True, "reset": "tier_transition_warned"})
    if action == "reset_warning":
        # Backward compat — resets both flags together
        sub["tier_warned_80pct"] = False
        sub["tier_transition_warned"] = False
        subs[code] = sub
        _save_subs(subs)
        return jsonify({"ok": True, "reset": "both_flags"})
    return jsonify({"error": "unknown action, use read, reset_80pct, reset_transition, or reset_warning"}), 400


@app.route("/api/create-checkout-session", methods=["POST"])
def create_checkout_session():
    from flask import jsonify
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "") or CFG.get("stripe_secret_key", "")
    print(f"[STRIPE] API key loaded: {bool(stripe.api_key)}", flush=True)

    req_data = request.json
    tier = req_data.get("tier")
    ref_code = req_data.get("ref_code", "").strip().upper()
    price_map = {
        "monthly":        CFG.get("stripe_price_monthly"),
        "annual":         CFG.get("stripe_price_annual"),
        "lifetime":       CFG.get("stripe_price_lifetime"),
        "topup_75":       CFG.get("stripe_price_topup_125"),
    }

    price_id = price_map.get(tier)
    print(f"[STRIPE] Price ID for {tier}: {price_id}", flush=True)
    if not price_id:
        return jsonify({"error": "Invalid tier"}), 400

    mode = "payment" if tier in ("lifetime", "topup_75") else "subscription"

    try:
        session_params = {
            "payment_method_types": ["card"],
            "line_items": [{"price": price_id, "quantity": 1}],
            "mode": mode,
            "success_url": "https://grailsweep.com/payment-success?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": "https://grailsweep.com/upgrade",
            "metadata": {"tier": tier},
        }

        # Apply referral discount if valid code provided (monthly only)
        if ref_code and tier == "monthly":
            referrals = _load_referrals()
            if ref_code in referrals:
                session_params["discounts"] = [{"coupon": "GRAILSWEEP_REFERRAL"}]
                session_params["metadata"]["ref_code"] = ref_code

        session = stripe.checkout.Session.create(**session_params)
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/redeem-topup", methods=["POST"])
def redeem_topup():
    """
    Redeem a TOPUP-XXXX-XXXX one-time scan credit code.

    Validates code, marks it as redeemed (one-shot), and adds
    credits to the redeeming device's modal.Dict balance.
    """
    from flask import jsonify
    import db as _db
    import urllib.parse as _up

    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip().upper()

    if not code:
        return jsonify({"error": "missing_code", "message": "Please provide a top-up code."}), 400
    if not code.startswith("TOPUP-"):
        return jsonify({"error": "invalid_format",
                        "message": "Top-up codes start with TOPUP-. Did you mean to redeem a Pro access code?"}), 400

    # Compute redeeming user's identifiers (same as _evaluate_scan_decision)
    ua   = request.user_agent.string
    lang = request.headers.get("Accept-Language", "")
    addr = request.headers.get("CF-Connecting-IP",
           request.headers.get("X-Forwarded-For", request.remote_addr or ""))
    server_fp = _db.compute_server_fingerprint(ua, lang, addr)
    device_id = request.cookies.get("matchit_device_id_v1") or None

    if not server_fp and not device_id:
        return jsonify({"error": "no_identifier",
                        "message": "Could not identify your device. Please ensure cookies are enabled."}), 400

    # Load subs ONCE — modify in place, save once. Avoid g-cache stale read.
    subs = _load_subs()

    entry = subs.get(code)
    if not entry:
        return jsonify({"error": "code_not_found",
                        "message": "This top-up code was not found. Check for typos and try again."}), 404

    if entry.get("type") != "topup":
        return jsonify({"error": "wrong_code_type",
                        "message": "This is not a top-up code. Pro access codes (GRAIL-) should be redeemed on the upgrade page."}), 400

    if entry.get("status") == "redeemed":
        redeemed_at = entry.get("redeemed_at", "earlier")
        return jsonify({"error": "already_redeemed",
                        "message": f"This top-up code was already redeemed ({redeemed_at}). Each code can only be used once."}), 409
    elif entry.get("status") == "cancelled":
        return jsonify({"error": "code_cancelled", "message": "This code has been cancelled and cannot be redeemed."}), 409

    # Mark as redeemed and add credits — shared with the Google Play
    # verify-purchase flow (same-device auto-redeem, see _redeem_topup_entry).
    result = _redeem_topup_entry(code, entry, subs, device_id, server_fp)
    if not result.get("ok"):
        return jsonify({
            "error": "balance_write_failed",
            "message": "Your code was accepted but we couldn't add credits. Please contact support@grailsweep.com with code " + code
        }), 500

    return jsonify({
        "ok": True,
        "credits_added": result["credits_added"],
        "new_balance": result.get("new_balance"),
        "message": f"Top-up redeemed! {result['credits_added']} extra scans added to your account."
    })


def _redeem_topup_entry(code, entry, subs, device_id, server_fp):
    """
    Mark a TOPUP- entry as redeemed and add its credits to the device's
    modal.Dict balance. Shared by /api/redeem-topup (manual code entry)
    and the Google Play verify-purchase flow (auto-redeemed on the same
    device immediately after purchase).
    """
    import db as _db
    from datetime import datetime

    credits = entry.get("credits_remaining", entry.get("credits_total", 125))
    entry["status"] = "redeemed"
    entry["redeemed_at"] = datetime.utcnow().isoformat()
    entry["redeemed_by_device"] = device_id
    entry["redeemed_by_fp"] = server_fp
    entry["credits_remaining"] = 0  # tracked in modal.Dict from now on
    subs[code] = entry
    _save_subs(subs)

    result = _db.add_topup_credits(server_fp, device_id, credits)
    if not result.get("ok"):
        # Credits failed to write — code is now marked redeemed but user
        # has no balance. This is a recoverable but bad state. Log loudly.
        print(f"[TOPUP REDEEM ERROR] code={code} balance write failed: {result.get('error')}", flush=True)
        return {"ok": False, "error": result.get("error")}

    print(f"[TOPUP REDEEMED] code={code} credits={credits} new_balance={result.get('new_balance')} device={device_id} fp={server_fp}", flush=True)
    return {"ok": True, "credits_added": credits, "new_balance": result.get("new_balance")}


# ── Google Play Billing (TWA only) ──────────────────────────────────────────
# Stripe remains the purchase path for regular browser/PWA and Microsoft
# Store traffic — this block is only reached via the Play Billing button
# shown when 'getDigitalGoodsService' is available (i.e. inside the TWA).

_GOOGLE_PLAY_PACKAGE = "com.grailsweep.app"
_GOOGLE_PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"

_GOOGLE_PLAY_SKU_TIER_MAP = {
    "grailsweep_monthly":    "monthly",
    "grailsweep_ultimate":   "annual",
    "grailsweep_topup_125":  "topup_75",
}


def _google_play_access_token():
    """Mint an OAuth2 bearer token for the Play Developer API from the
    service account JSON in the google-play-credentials Modal secret."""
    import json as _json
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as _GRequest

    sa_json = os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        raise RuntimeError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON not set")
    info = _json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    creds.refresh(_GRequest())
    return creds.token


def _google_play_get_subscription(token, purchase_token):
    import requests as _requests
    url = (f"{_GOOGLE_PLAY_API}/applications/{_GOOGLE_PLAY_PACKAGE}"
           f"/purchases/subscriptionsv2/tokens/{purchase_token}")
    resp = _requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _google_play_acknowledge_subscription(token, sku_id, purchase_token):
    import requests as _requests
    url = (f"{_GOOGLE_PLAY_API}/applications/{_GOOGLE_PLAY_PACKAGE}"
           f"/purchases/subscriptions/{sku_id}/tokens/{purchase_token}:acknowledge")
    resp = _requests.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    resp.raise_for_status()


def _google_play_get_product(token, sku_id, purchase_token):
    import requests as _requests
    url = (f"{_GOOGLE_PLAY_API}/applications/{_GOOGLE_PLAY_PACKAGE}"
           f"/purchases/products/{sku_id}/tokens/{purchase_token}")
    resp = _requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _google_play_acknowledge_product(token, sku_id, purchase_token):
    import requests as _requests
    url = (f"{_GOOGLE_PLAY_API}/applications/{_GOOGLE_PLAY_PACKAGE}"
           f"/purchases/products/{sku_id}/tokens/{purchase_token}:acknowledge")
    resp = _requests.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    resp.raise_for_status()


def _google_play_consume_product(token, sku_id, purchase_token):
    import requests as _requests
    url = (f"{_GOOGLE_PLAY_API}/applications/{_GOOGLE_PLAY_PACKAGE}"
           f"/purchases/products/{sku_id}/tokens/{purchase_token}:consume")
    resp = _requests.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    resp.raise_for_status()


@app.route("/api/google-play/verify-purchase", methods=["POST"])
def google_play_verify_purchase():
    """
    Verify a Google Play Billing purchase (subscription or one-time
    top-up) made inside the TWA, then grant entitlement using the exact
    same functions the Stripe webhook uses (_issue_new_code /
    _issue_new_topup_code + _redeem_topup_entry) — no parallel
    entitlement path. Purchases are same-device, so subscriptions are
    auto-activated (via /api/validate_premium client-side) and top-ups
    are auto-redeemed inline instead of requiring an emailed code.
    """
    from flask import jsonify
    import db as _db

    data = request.get_json(silent=True) or {}
    purchase_token = (data.get("purchaseToken") or "").strip()
    sku_id = (data.get("skuId") or "").strip()
    product_type = (data.get("productType") or "").strip()

    if not purchase_token or not sku_id or product_type not in ("subscription", "onetime"):
        return jsonify({"error": "invalid_request",
                         "message": "Missing purchaseToken, skuId, or productType."}), 400

    tier = _GOOGLE_PLAY_SKU_TIER_MAP.get(sku_id)
    if not tier:
        return jsonify({"error": "unknown_sku", "message": f"Unrecognised SKU: {sku_id}"}), 400

    # Idempotency — a retried/duplicate purchaseToken must never grant twice.
    _pd = None
    try:
        import modal as _modal
        _pd = _modal.Dict.from_name("google-play-purchase-tokens", create_if_missing=True)
        _cached = _pd.get(purchase_token)
        if _cached is not None:
            print(f"[GPLAY] Duplicate purchaseToken={purchase_token} — returning cached result", flush=True)
            return jsonify(_cached)
    except Exception as _idem_e:
        print(f"[GPLAY] Idempotency check failed: {_idem_e} — proceeding anyway", flush=True)

    try:
        access_token = _google_play_access_token()
    except Exception as e:
        print(f"[GPLAY] Failed to mint access token: {e}", flush=True)
        return jsonify({"error": "server_config_error",
                         "message": "Purchase verification is temporarily unavailable."}), 500

    # Same device identifiers used by /api/redeem-topup and free-scan gating.
    ua   = request.user_agent.string
    lang = request.headers.get("Accept-Language", "")
    addr = request.headers.get("CF-Connecting-IP",
           request.headers.get("X-Forwarded-For", request.remote_addr or ""))
    server_fp = _db.compute_server_fingerprint(ua, lang, addr)
    device_id = request.cookies.get("matchit_device_id_v1") or None
    # Same formula as validate_premium()'s per-code device fingerprint — NOT
    # server_fp above, which uses a different hash and isn't what's stored
    # in a subscription entry's "devices" list.
    import hashlib as _hashlib
    _ip_partial = ".".join(addr.split(".")[:2]) if addr else ""
    device_fingerprint = _hashlib.md5((ua + lang + _ip_partial).encode()).hexdigest()[:16]

    try:
        if product_type == "subscription":
            info = _google_play_get_subscription(access_token, purchase_token)
            state = info.get("subscriptionState", "")
            if state not in ("SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD"):
                return jsonify({"error": "not_active",
                                 "message": f"Subscription is not active (state: {state})."}), 402

            code = _issue_new_code(email="", tier=tier, subscription_id=purchase_token,
                                    ref_code="", source="google_play",
                                    device_fingerprint=device_fingerprint)

            try:
                _google_play_acknowledge_subscription(access_token, sku_id, purchase_token)
            except Exception as _ack_e:
                print(f"[GPLAY] Subscription acknowledge failed (non-fatal): {_ack_e}", flush=True)

            response_payload = {"ok": True, "type": "subscription", "code": code, "tier": tier,
                                 "message": "Subscription activated!"}

        else:  # one-time top-up
            info = _google_play_get_product(access_token, sku_id, purchase_token)
            state = info.get("purchaseState", 1)  # 0=purchased, 1=canceled, 2=pending
            if state != 0:
                return jsonify({"error": "not_purchased",
                                 "message": f"Purchase is not complete (state: {state})."}), 402

            if not server_fp and not device_id:
                return jsonify({"error": "no_identifier",
                                 "message": "Could not identify your device. Please ensure cookies are enabled."}), 400

            code = _issue_new_topup_code(email="", credits=125,
                                          payment_intent_id=purchase_token, source="google_play")
            subs = _load_subs()
            entry = subs[code]
            result = _redeem_topup_entry(code, entry, subs, device_id, server_fp)
            if not result.get("ok"):
                return jsonify({"error": "balance_write_failed",
                                 "message": "Purchase verified but we couldn't add credits. "
                                            "Contact support@grailsweep.com with code " + code}), 500

            try:
                _google_play_acknowledge_product(access_token, sku_id, purchase_token)
                _google_play_consume_product(access_token, sku_id, purchase_token)
            except Exception as _ack_e:
                print(f"[GPLAY] Product acknowledge/consume failed (non-fatal): {_ack_e}", flush=True)

            response_payload = {"ok": True, "type": "topup",
                                 "credits_added": result["credits_added"],
                                 "new_balance": result.get("new_balance"),
                                 "message": f"{result['credits_added']} scans added!"}

    except Exception as e:
        print(f"[GPLAY] Verification failed: {e}", flush=True)
        return jsonify({"error": "verification_failed",
                         "message": "Could not verify purchase with Google Play."}), 502

    # Record for idempotency only after entitlement has actually been granted.
    if _pd is not None:
        try:
            _pd.put(purchase_token, response_payload)
        except Exception as _put_e:
            print(f"[GPLAY] Idempotency write failed: {_put_e}", flush=True)

    return jsonify(response_payload)


# ── Google Play RTDN (Real-time Developer Notifications) ────────────────────
# Ongoing subscription lifecycle events (renewal, cancellation, billing
# grace/hold), delivered by Cloud Pub/Sub push. Fully separate from
# google_play_verify_purchase() above, which only handles the initial
# purchase — do not merge these paths.
#
# IMPORTANT — this endpoint must be reached via https://grailsweep.com/...,
# NOT the raw *.modal.run URL. _enforce_cf_proxy() (top of this file) 403s
# any request without X-CF-Proxy-Secret, which only the Cloudflare Worker
# injects (see cloudflare_worker.js::proxyToModal). Google's Pub/Sub push
# will never carry that header, so the raw Modal URL will always reject it.

def _verify_rtdn_request(req):
    """
    Verify an inbound Pub/Sub push request is genuinely from Google's
    Pub/Sub push service for this project, not arbitrary internet
    traffic. Two independent, env-gated checks (skipped individually if
    their env var isn't set — same safe-rollout idiom as
    _CF_PROXY_SECRET above, so the endpoint can be stood up before
    Craig finishes Pub/Sub + secret configuration):

      1. Shared-secret query token (?token=...) — set on the push
         endpoint URL itself in the Pub/Sub subscription config.
      2. Google-signed OIDC identity token in the Authorization header,
         verified against Google's public certs, checked for audience +
         issuer + the specific service-account email used to configure
         the push subscription.

    Returns (ok: bool, reason: str).
    """
    expected_token = os.environ.get("GOOGLE_PLAY_RTDN_TOKEN", "").strip()
    if expected_token:
        incoming_token = (req.args.get("token") or "").strip()
        if not incoming_token or not _secrets_mod.compare_digest(incoming_token, expected_token):
            return False, "bad_shared_secret"

    expected_sa_email = os.environ.get("GOOGLE_PLAY_RTDN_SA_EMAIL", "").strip()
    if expected_sa_email:
        expected_audience = os.environ.get("GOOGLE_PLAY_RTDN_AUDIENCE", "").strip()
        if not expected_audience:
            # OIDC check is "on" (SA email configured) but audience isn't —
            # this is a config error, not a missing-check. Fail closed
            # rather than guess an audience from request headers (the CF
            # Worker rewrites Host to the Modal hostname, so request.url_root
            # would be wrong here — see proxyToModal in cloudflare_worker.js).
            return False, "rtdn_audience_not_configured"

        auth_header = req.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False, "missing_bearer_token"
        raw_token = auth_header[len("Bearer "):].strip()
        try:
            from google.oauth2 import id_token as _id_token
            from google.auth.transport import requests as _g_auth_requests
            claims = _id_token.verify_oauth2_token(
                raw_token, _g_auth_requests.Request(), audience=expected_audience
            )
            if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
                return False, "bad_issuer"
            if not claims.get("email_verified") or claims.get("email") != expected_sa_email:
                return False, "sa_email_mismatch"
        except Exception as e:
            return False, f"oidc_verify_failed:{e}"

    if not expected_token and not expected_sa_email:
        return True, "unconfigured"
    return True, "ok"


def _handle_rtdn_subscription_notification(sub_notif):
    """
    Given a decoded RTDN subscriptionNotification block, fetch the
    CURRENT real state from Google directly — never trust the
    notification's own claims about what happened, same principle
    google_play_verify_purchase() already uses for the initial purchase
    — and update the matching subscriptions.json entry accordingly.
    """
    from datetime import datetime, timedelta

    purchase_token = sub_notif.get("purchaseToken", "")
    notification_type = sub_notif.get("notificationType")
    if not purchase_token:
        print("[RTDN] subscriptionNotification missing purchaseToken", flush=True)
        return

    print(f"[RTDN] subscriptionNotification type={notification_type} token={purchase_token}", flush=True)

    access_token = _google_play_access_token()
    info = _google_play_get_subscription(access_token, purchase_token)
    state = info.get("subscriptionState", "")

    subs = _load_subs()
    match_code = None
    for code, entry in subs.items():
        if entry.get("source") == "google_play" and entry.get("stripe_subscription_id") == purchase_token:
            match_code = code
            break

    if not match_code:
        print(f"[RTDN] No subscriptions.json entry for purchaseToken={purchase_token} "
              f"(state={state}) — nothing to update", flush=True)
        return

    entry = subs[match_code]
    tier = entry.get("tier", "monthly")
    now_iso = datetime.utcnow().isoformat()

    if state == "SUBSCRIPTION_STATE_ACTIVE":
        # Renewed (or recovered from grace/hold) — extend expiry using the
        # exact same per-tier duration logic as _issue_new_code().
        if tier == "lifetime":
            expires = None
        elif tier == "annual":
            expires = (datetime.utcnow() + timedelta(days=366)).isoformat()
        else:
            expires = (datetime.utcnow() + timedelta(days=32)).isoformat()
        entry["status"] = "active"
        entry["expires_at"] = expires
        entry["rtdn_last_state"] = state
        entry["rtdn_updated_at"] = now_iso
        print(f"[RTDN] {match_code} renewed (tier={tier}), new expires_at={expires}", flush=True)

    elif state == "SUBSCRIPTION_STATE_IN_GRACE_PERIOD":
        # Google is still retrying the card — keep access, just log.
        entry["rtdn_last_state"] = state
        entry["rtdn_updated_at"] = now_iso
        print(f"[RTDN] {match_code} in grace period — access retained, no status change", flush=True)

    else:
        # ON_HOLD (grace period already lapsed with no successful
        # payment), CANCELED, EXPIRED, REVOKED, PAUSED, PENDING, or any
        # other/unrecognised state — treat as not-entitled. If the
        # customer's card recovers, a later ACTIVE notification (handled
        # above) reinstates the same code automatically.
        entry["status"] = "expired"
        entry["expired_at"] = now_iso
        entry["rtdn_last_state"] = state
        entry["rtdn_updated_at"] = now_iso
        print(f"[RTDN] {match_code} set to expired (state={state})", flush=True)

    _save_subs(subs)


@app.route("/api/google-play/rtdn", methods=["POST"])
def google_play_rtdn():
    """
    Google Play RTDN push endpoint. Per Google's Pub/Sub push contract,
    always return 200 quickly — even on internal error — to avoid
    retry-triggered duplicate processing; genuine duplicate deliveries
    are instead caught by the messageId idempotency check below.
    """
    from flask import jsonify
    import base64, json as _json

    ok, reason = _verify_rtdn_request(request)
    if not ok:
        print(f"[RTDN] Rejected: {reason}", flush=True)
        return jsonify({"error": "unauthorized"}), 401

    envelope = request.get_json(silent=True) or {}
    message = envelope.get("message") or {}
    message_id = message.get("messageId") or message.get("message_id") or ""

    # Idempotency — keyed by Pub/Sub messageId, NOT purchaseToken. A
    # subscription receives many notifications over its lifetime for the
    # same purchaseToken (each renewal, cancellation, etc.), so reusing
    # the google-play-purchase-tokens dict (keyed by purchaseToken, used
    # for the one-time initial-purchase dedup above) would wrongly
    # swallow every notification after the first for a given token.
    _pd = None
    if message_id:
        try:
            import modal as _modal
            _pd = _modal.Dict.from_name("google-play-rtdn-messages", create_if_missing=True)
            if _pd.get(message_id) is not None:
                print(f"[RTDN] Duplicate messageId={message_id} — skipping", flush=True)
                return jsonify({"status": "ok"}), 200
        except Exception as _idem_e:
            print(f"[RTDN] Idempotency check failed: {_idem_e} — proceeding anyway", flush=True)

    try:
        data_b64 = message.get("data", "")
        if not data_b64:
            print("[RTDN] Empty message.data — nothing to process", flush=True)
            return jsonify({"status": "ok"}), 200

        notification = _json.loads(base64.b64decode(data_b64).decode("utf-8"))

        if "testNotification" in notification:
            print(f"[RTDN] testNotification received: {notification['testNotification']}", flush=True)

        elif "subscriptionNotification" in notification:
            _handle_rtdn_subscription_notification(notification["subscriptionNotification"])

        else:
            print(f"[RTDN] Unhandled notification shape: {list(notification.keys())}", flush=True)

    except Exception as e:
        # Never fail this response — log loudly and move on. A dropped
        # event here is recoverable: the next renewal/cancellation
        # notification re-verifies live state from Google anyway.
        print(f"[RTDN] Processing error (swallowed, returning 200): {e}", flush=True)
        return jsonify({"status": "ok"}), 200

    if _pd is not None and message_id:
        try:
            _pd.put(message_id, {"ts": time.time()})
        except Exception as _put_e:
            print(f"[RTDN] Idempotency write failed: {_put_e}", flush=True)

    return jsonify({"status": "ok"}), 200
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/api/topup-status", methods=["GET"])
def topup_status():
    """
    Return the current user's free-tier and top-up balance.
    Used by frontend (Task 9c) for badge + modal display.
    """
    from flask import jsonify
    import db as _db

    try:
        ua   = request.user_agent.string
        lang = request.headers.get("Accept-Language", "")
        addr = request.headers.get("CF-Connecting-IP",
               request.headers.get("X-Forwarded-For", request.remote_addr or ""))
        server_fp = _db.compute_server_fingerprint(ua, lang, addr)
        device_id = request.cookies.get("matchit_device_id_v1") or None

        free_state  = _db.read_free_scans(server_fp, device_id)
        topup_state = _db.read_topup_credits(server_fp, device_id)

        return jsonify({
            "ok": True,
            "free_used":       free_state.get("count", 0),
            "free_limit":      free_state.get("limit", 150),
            "free_remaining":  free_state.get("remaining", 0),
            "topup_remaining": topup_state.get("credits", 0),
        })
    except Exception as e:
        print(f"[TOPUP STATUS ERROR] {e}", flush=True)
        return jsonify({"ok": False, "error": "status_unavailable"}), 200


@app.route("/payment-success")
def payment_success():
    return render_template("payment_success.html")


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "") or CFG.get("stripe_secret_key", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "") or CFG.get("stripe_webhook_secret", "")

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    import json as _json
    payload_dict = _json.loads(payload)
    event_id = event["id"]
    etype = event["type"]

    try:
        import modal as _modal
        _d = _modal.Dict.from_name("stripe-webhook-events", create_if_missing=True)
        if _d.get(event_id, None) is not None:
            print(f"[WEBHOOK] Duplicate event {event_id} (type={etype}) — skipping", flush=True)
            return jsonify({"status": "ok"})
    except Exception as _idem_e:
        print(f"[WEBHOOK] Idempotency check failed for {event_id}: {_idem_e} — proceeding anyway", flush=True)

    # Key is written AFTER work succeeds (inside _process_stripe_event_safe), not
    # here — so a container killed mid-processing leaves the event un-marked and
    # Stripe's retry reprocesses it cleanly instead of being silently deduped out.
    _t = threading.Thread(
        target=_process_stripe_event_safe,
        args=(event, payload_dict),
        daemon=True,
    )
    with _active_stripe_threads_lock:
        _active_stripe_threads.append(_t)
    _t.start()

    return jsonify({"status": "ok"})


def _process_stripe_event_safe(event, payload_dict):
    event_id = event["id"]
    etype = event["type"]
    try:
        _process_stripe_event(event, payload_dict)
        # Mark processed AFTER work succeeds — never before.
        try:
            import modal as _modal
            _dd = _modal.Dict.from_name("stripe-webhook-events", create_if_missing=True)
            _dd.put(event_id, {"ts": time.time(), "type": etype})
        except Exception:
            pass
        # Flush volume write so subscriptions.json survives container shutdown.
        try:
            if _vol_commit_fn:
                _vol_commit_fn()
        except Exception:
            pass
        print(f"[WEBHOOK] Successfully processed {event_id} (type={etype})", flush=True)
    except Exception as _bg_e:
        print(f"[WEBHOOK] BACKGROUND PROCESSING FAILED for {event_id}: {_bg_e}", flush=True)
        # Key was never set — Stripe retry will reprocess naturally.
    finally:
        with _active_stripe_threads_lock:
            try:
                _active_stripe_threads.remove(threading.current_thread())
            except ValueError:
                pass


def _process_stripe_event(event, payload_dict):
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "") or CFG.get("stripe_secret_key", "")

    etype = event["type"]
    obj_dict = payload_dict["data"]["object"]

    if etype == "checkout.session.completed":
        email = (obj_dict.get("customer_details") or {}).get("email")
        tier = (obj_dict.get("metadata") or {}).get("tier", "monthly")
        ref_code_used = (obj_dict.get("metadata") or {}).get("ref_code", "")
        subscription_id = obj_dict.get("subscription")
        if email:
            if tier == "topup_75":
                # One-time scan top-up — issue TOPUP- code, not GRAIL-
                payment_intent = obj_dict.get("payment_intent")
                _issue_new_topup_code(email, credits=125, payment_intent_id=payment_intent)
            else:
                # Subscription tier — existing GRAIL- code flow
                new_code = _issue_new_code(email, tier, subscription_id, ref_code=ref_code_used)
                # Persist billing period fields for tier cap tracking
                if new_code and subscription_id and tier in ("monthly", "annual"):
                    try:
                        _stripe_sub = stripe.Subscription.retrieve(subscription_id)
                        _ps = _stripe_sub.get("current_period_start")
                        _pe = _stripe_sub.get("current_period_end")
                        if _ps and _pe:
                            from datetime import datetime as _dt
                            _subs = _load_subs()
                            if new_code in _subs:
                                _subs[new_code]["current_period_start"] = _dt.utcfromtimestamp(_ps).isoformat()
                                _subs[new_code]["current_period_end"]   = _dt.utcfromtimestamp(_pe).isoformat()
                                _save_subs(_subs)
                                print(f"[WEBHOOK] Persisted period fields for code={new_code}", flush=True)
                    except Exception as _we:
                        print(f"[WEBHOOK] Period field persist failed: {_we}", flush=True)

    elif etype == "invoice.paid":
        subscription_id = obj_dict.get("subscription")
        if subscription_id:
            _extend_subscription(subscription_id)
            # Persist new billing period on renewal + reset 80% warning flag
            try:
                _ps = obj_dict.get("period_start") or (
                    (obj_dict.get("lines") or {}).get("data") or [{}])[0].get("period", {}).get("start")
                _pe = obj_dict.get("period_end") or (
                    (obj_dict.get("lines") or {}).get("data") or [{}])[0].get("period", {}).get("end")
                if _ps and _pe:
                    from datetime import datetime as _dt
                    _subs = _load_subs()
                    for _code, _entry in _subs.items():
                        if _entry.get("stripe_subscription_id") == subscription_id:
                            _entry["current_period_start"] = _dt.utcfromtimestamp(_ps).isoformat()
                            _entry["current_period_end"]   = _dt.utcfromtimestamp(_pe).isoformat()
                            _entry["tier_warned_80pct"]       = False
                            _entry["tier_transition_warned"]  = False
                            print(f"[WEBHOOK] Renewed period for code={_code}", flush=True)
                            break
                    _save_subs(_subs)
            except Exception as _re:
                print(f"[WEBHOOK] invoice.paid period update failed: {_re}", flush=True)

    elif etype == "customer.subscription.deleted":
        subscription_id = obj_dict.get("id")
        if subscription_id:
            _cancel_subscription(subscription_id)


import os as _os


def _atomic_write_json(path, data, indent=None, log_prefix="WRITE"):
    import json as _json
    import os as _ow
    _payload = _json.dumps(data, indent=indent)
    _payload_bytes = _payload.encode("utf-8")
    # First-write path: Modal volume rejects O_CREAT on new files via os.open;
    # use plain open() which works fine for initial creation.
    if not _ow.path.exists(path):
        print(f"[{log_prefix}] file does not exist, doing direct first-write", flush=True)
        with open(path, "w", encoding="utf-8") as _f:
            _f.write(_payload)
        try:
            _ow.chmod(path, 0o644)
        except Exception:
            pass
        print(f"[{log_prefix}] saved {len(_payload_bytes)} bytes to {path}", flush=True)
        return
    # Existing-file path: atomic swap via .tmp to avoid partial reads.
    _tmp = path + ".tmp"
    _fd = _ow.open(_tmp, _ow.O_WRONLY | _ow.O_CREAT | _ow.O_TRUNC, 0o644)
    try:
        with _ow.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(_payload)
        _ow.replace(_tmp, path)
        try:
            _ow.chmod(path, 0o644)
        except Exception:
            pass
        print(f"[{log_prefix}] saved {len(_payload_bytes)} bytes to {path}", flush=True)
    except Exception as _e:
        try:
            _ow.unlink(_tmp)
        except OSError:
            pass
        raise _e


SUBS_PATH = "/modal_data/subscriptions.json" if _os.path.exists("/modal_data") else "subscriptions.json"

def _load_subs():
    import json as _json
    try:
        from flask import g as _g
        if hasattr(_g, "_cached_subs"):
            return _g._cached_subs
    except RuntimeError:
        pass
    try:
        with open(SUBS_PATH, "r") as f:
            _data = _json.load(f)
    except Exception:
        _data = {}
    try:
        from flask import g as _g
        _g._cached_subs = _data
    except RuntimeError:
        pass
    return _data

def _save_subs(data):
    _atomic_write_json(SUBS_PATH, data, indent=2, log_prefix="SUBS")

# ── Per-user collection sync ──────────────────────────────────────────────────
COLLECTIONS_PATH = "/modal_data/collections.json" if _os.path.exists("/modal_data") else "collections.json"

def _load_collections():
    import json as _json
    try:
        with open(COLLECTIONS_PATH, "r") as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_collections(data):
    _atomic_write_json(COLLECTIONS_PATH, data, log_prefix="COLLECTIONS")
    try:
        import modal as _modal
        _modal.Volume.from_name("matchit-data-v2").commit()
    except Exception:
        pass

# ── Per-user watchlist sync ───────────────────────────────────────────────────
WATCHLIST_PATH = "/modal_data/watchlist.json" if _os.path.exists("/modal_data") else "watchlist.json"

def _load_watchlist():
    import json as _json
    try:
        with open(WATCHLIST_PATH, "r") as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_watchlist(data):
    _atomic_write_json(WATCHLIST_PATH, data, log_prefix="WATCHLIST")
    try:
        import modal as _modal
        _modal.Volume.from_name("matchit-data-v2").commit()
    except Exception:
        pass

# ── Push subscriptions ───────────────────────────────────────────────────────
PUSH_SUBS_PATH = "/modal_data/push_subscriptions.json" if _os.path.exists("/modal_data") else "push_subscriptions.json"

def _load_push_subs():
    import json as _json
    try:
        with open(PUSH_SUBS_PATH, "r") as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_push_subs(data):
    _atomic_write_json(PUSH_SUBS_PATH, data, log_prefix="PUSH_SUBS")
    try:
        import modal as _modal
        _modal.Volume.from_name("matchit-data-v2").commit()
    except Exception:
        pass

# ── Set-completion helpers ────────────────────────────────────────────────────
SET_METADATA_PATH = "/modal_data/set_metadata.json" if _os.path.exists("/modal_data") else "set_metadata.json"
SKU_GAME_MAP_PATH = "/modal_data/sku_game_map.json" if _os.path.exists("/modal_data") else "sku_game_map.json"
IDENTIFIER_LOOKUP_PATH = "/modal_data/identifier_lookup.json" if _os.path.exists("/modal_data") else "identifier_lookup.json"
MTG_SET_TOTALS_PATH = "/modal_data/mtg_set_totals.json" if _os.path.exists("/modal_data") else "mtg_set_totals.json"

_set_metadata_cache = None
_set_metadata_mtime = None
_mtg_set_total_cache = {}
_mtg_set_totals_sidecar_cache = None
_mtg_set_totals_sidecar_mtime = None
_sku_game_cache = {}
_set_game_cache = {}  # set_id → game, derived from sku_game_map
_identifier_lookup = {}


def _preload_sku_game_cache():
    global _sku_game_cache, _set_game_cache
    try:
        with open(SKU_GAME_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _sku_game_cache.update(data)
        print(f"[SKU-GAME] Preloaded {len(data)} SKU→game mappings", flush=True)
        for _sku, _game in _sku_game_cache.items():
            _parts = _sku.rsplit("-", 1)
            if len(_parts) == 2:
                _sid = _parts[0]
                if _sid not in _set_game_cache:
                    _set_game_cache[_sid] = _game
        print(f"[SKU-GAME] Built set-game cache: {len(_set_game_cache)} set IDs", flush=True)
    except Exception as e:
        print(f"[SKU-GAME] Could not preload sku_game_map: {e}", flush=True)

_preload_sku_game_cache()  # runs on import — fires on Modal and local dev


def _preload_identifier_lookup():
    global _identifier_lookup
    try:
        with open(IDENTIFIER_LOOKUP_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _identifier_lookup.update(data)
        total = sum(len(v) if isinstance(v, dict) else 1
                    for v in data.values())
        print(f'[OCR-LOOKUP] Preloaded {total} identifier->SKU mappings '
              f'({", ".join(f"{k}:{len(v)}" for k,v in data.items() if isinstance(v,dict))})',
              flush=True)
    except Exception as e:
        print(
            f'[OCR-LOOKUP] Could not preload '
            f'identifier_lookup: {e}', flush=True)


_preload_identifier_lookup()


POKEMON_SEARCH_INDEX_PATH = "/modal_data/pokemon_search_index.json" if _os.path.exists("/modal_data") else "pokemon_search_index.json"
_pokemon_search_index = []  # list of {sku,name,number,set_id,set_name}


def _preload_pokemon_search_index():
    global _pokemon_search_index
    try:
        with open(POKEMON_SEARCH_INDEX_PATH, "r", encoding="utf-8") as f:
            _pokemon_search_index = json.load(f)
        print(f"[SEARCH] Loaded {len(_pokemon_search_index)} Pokémon entries", flush=True)
    except Exception as e:
        print(f"[SEARCH] No pokemon_search_index.json loaded — search unavailable: {e}", flush=True)


_preload_pokemon_search_index()

def _load_set_metadata():
    global _set_metadata_cache, _set_metadata_mtime
    try:
        current_mtime = _os.path.getmtime(SET_METADATA_PATH)
        if _set_metadata_cache is not None and current_mtime == _set_metadata_mtime:
            return _set_metadata_cache
        with open(SET_METADATA_PATH, "r", encoding="utf-8") as f:
            _set_metadata_cache = json.load(f)
        _set_metadata_mtime = current_mtime
        return _set_metadata_cache
    except Exception:
        return {}

def _get_set_id_from_sku(sku):
    if not sku:
        return None
    if sku.startswith("ygo-"):
        parts = sku.split("-")
        return parts[1] if len(parts) >= 2 else None
        # e.g. 'ygo-SDBT-EN006-45803070' → 'SDBT'
        # e.g. 'ygo-MP24-EN319-12954226' → 'MP24'
        # e.g. 'ygo-LOB-000-39111158'    → 'LOB'
    if sku.startswith("mtg-"):
        parts = sku.split("-")
        return parts[1] if len(parts) >= 2 else None
    # Pokémon: sv1-85 → sv1
    return sku.rsplit("-", 1)[0] if "-" in sku else None

@app.route('/api/set-total/<path:sku>')
def api_set_total(sku):
    """Read-only set-size lookup for the client's on-device denominator backstop.
    Reuses _get_set_id_from_sku (handles ygo-/mtg-/Pokémon key shapes) — does not
    duplicate the rsplit rule inline, so MTG/YGO SKUs key correctly too."""
    try:
        meta = _load_set_metadata()
        set_id = _get_set_id_from_sku(sku)
        entry = meta.get(set_id) or {}
        return jsonify({
            "set_id": set_id,
            "printed_total": entry.get("printed_total"),
            "total": entry.get("total"),
        })
    except Exception:
        return jsonify({"set_id": None, "printed_total": None, "total": None})

def _is_promo_set(set_id, game):
    if game == "POKEMON":
        return set_id.endswith("p")
    return False

def _walk_mtg_set_total(set_id):
    """Ground-truth count for one MTG set: lists the entire CardsDB/mtg
    directory, filters by prefix, and dedupes by parsed card name.
    Unchanged from the original _get_mtg_set_total body — kept as a
    standalone helper so the weekly precompute can call it directly
    (bypassing the sidecar tier below) and the live fallback can reuse
    the exact same logic for sets not yet baked."""
    from vertical_loader import get_db_root as _gdbr
    cards_dir = os.path.join(_gdbr() or "CardsDB", "mtg")
    std_prefix = "mtg-" + set_id + "-"
    alt_prefix = set_id + "-"
    names = set()
    try:
        if not os.path.isdir(cards_dir):
            return None
        for sku_folder in os.listdir(cards_dir):
            if not (sku_folder.startswith(std_prefix) or sku_folder.startswith(alt_prefix)):
                continue
            profile_path = os.path.join(cards_dir, sku_folder, "profile.json")
            if os.path.exists(profile_path):
                try:
                    with open(profile_path, "r", encoding="utf-8") as f:
                        p = json.load(f)
                    if p.get("name"):
                        names.add(p["name"].lower().strip())
                except Exception:
                    pass
    except Exception:
        pass
    return len(names) if names else None


def _load_mtg_set_totals_sidecar():
    """mtime-cached load of the baked mtg_set_totals.json sidecar.
    Mirrors the _load_set_metadata() caching pattern. Returns {} if the
    file doesn't exist yet (pre-bake) or fails to parse — callers then
    fall back to the live walk for every set, i.e. today's behavior."""
    global _mtg_set_totals_sidecar_cache, _mtg_set_totals_sidecar_mtime
    try:
        current_mtime = _os.path.getmtime(MTG_SET_TOTALS_PATH)
        if (_mtg_set_totals_sidecar_cache is not None
                and current_mtime == _mtg_set_totals_sidecar_mtime):
            return _mtg_set_totals_sidecar_cache
        with open(MTG_SET_TOTALS_PATH, "r", encoding="utf-8") as f:
            _mtg_set_totals_sidecar_cache = json.load(f)
        _mtg_set_totals_sidecar_mtime = current_mtime
        return _mtg_set_totals_sidecar_cache
    except Exception:
        return {}


def _get_mtg_set_total(set_id):
    if set_id in _mtg_set_total_cache:
        return _mtg_set_total_cache[set_id]

    sidecar = _load_mtg_set_totals_sidecar()
    if set_id in sidecar:
        result = sidecar[set_id]
        _mtg_set_total_cache[set_id] = result
        return result

    result = _walk_mtg_set_total(set_id)
    _mtg_set_total_cache[set_id] = result
    return result


def _read_profile_name(args):
    """Open one profile.json and return (sku_folder, lowercased name or None).
    Identical to the per-match body in _walk_mtg_set_total — only split out
    so it can be dispatched to a thread pool."""
    cards_dir, sku_folder = args
    profile_path = os.path.join(cards_dir, sku_folder, "profile.json")
    if not os.path.exists(profile_path):
        return sku_folder, None
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            p = json.load(f)
        name = p.get("name")
        return sku_folder, (name.lower().strip() if name else None)
    except Exception:
        return sku_folder, None


def _walk_all_mtg_set_totals(set_ids):
    """Bulk variant of _walk_mtg_set_total for the offline precompute.

    Two real runs proved the naive per-set approach doesn't finish:
    1. Calling _walk_mtg_set_total once per set re-lists the ~80k-entry
       CardsDB/mtg directory from scratch every time (250+ sets = 250+
       redundant network listdir()s) — fixed by listing the directory
       ONCE here and reusing it for every set_id's prefix filter.
    2. Even with #1 fixed, sequential profile.json opens still timed out
       at 1800s — ~80k individual small-file reads against the Modal
       volume's network-backed mount, one at a time. Fixed by reading
       them concurrently with a thread pool: these are I/O-bound (waiting
       on the network), so threads overlap that latency instead of
       paying it serially. The GIL doesn't block this — each read is a
       blocking syscall during which other threads run.

    Per-folder matching rule (startswith std_prefix/alt_prefix) and
    per-match logic (open profile.json, dedupe by lowercased name) are
    copied verbatim from _walk_mtg_set_total — only WHEN each profile.json
    gets read changed (once, concurrently, deduped across sets), not WHAT
    is read or how a match is decided. Output is identical to calling
    _walk_mtg_set_total(set_id) once per set_id: no two distinct set_ids'
    prefixes can match the same folder (std_prefix always starts with
    "mtg-" and ends with the set_id's own trailing hyphen; confirmed no
    MTG set_id contains a hyphen, so prefixes don't nest), so each folder
    is attributed to at most one set_id either way, and is therefore only
    ever read once regardless.

    The live per-request fallback in _get_mtg_set_total still calls
    _walk_mtg_set_total for a single not-yet-baked set, where none of this
    batching would help (only one set, no redundant work to eliminate).
    """
    from vertical_loader import get_db_root as _gdbr
    from concurrent.futures import ThreadPoolExecutor

    cards_dir = os.path.join(_gdbr() or "CardsDB", "mtg")
    try:
        all_folders = os.listdir(cards_dir) if os.path.isdir(cards_dir) else []
    except Exception:
        all_folders = []

    # Which set_id(s) each folder matches — same startswith check as
    # _walk_mtg_set_total, just run once per folder instead of once per
    # (set_id, folder) pair.
    folder_to_set = {}
    for set_id in set_ids:
        std_prefix = "mtg-" + set_id + "-"
        alt_prefix = set_id + "-"
        for sku_folder in all_folders:
            if sku_folder.startswith(std_prefix) or sku_folder.startswith(alt_prefix):
                folder_to_set[sku_folder] = set_id

    folder_name = {}
    if folder_to_set:
        with ThreadPoolExecutor(max_workers=32) as pool:
            for sku_folder, name in pool.map(
                _read_profile_name,
                ((cards_dir, f) for f in folder_to_set),
            ):
                folder_name[sku_folder] = name

    names_by_set = {set_id: set() for set_id in set_ids}
    for sku_folder, set_id in folder_to_set.items():
        name = folder_name.get(sku_folder)
        if name:
            names_by_set[set_id].add(name)

    return {set_id: (len(names) if names else None) for set_id, names in names_by_set.items()}


def rebuild_mtg_set_totals():
    """Weekly precompute: bakes {set_id: total} for every true MTG set into
    mtg_set_totals.json on the volume, atomically (temp file + os.replace),
    so /api/sets/completion can skip the 80k-folder walk at request time.

    Uses _walk_all_mtg_set_totals (one shared directory listing) rather
    than _walk_mtg_set_total per set — see that function's docstring for
    why this is provably identical output, not a behavior change. Never
    goes through the sidecar-aware _get_mtg_set_total, so a stale or
    missing sidecar can't feed back into itself — every run recomputes
    from the actual CardsDB contents.

    Set list: every set_id flagged game=="MTG" in set_metadata.json,
    EXCLUDING set_ids where sku_game_map disagrees (e.g. me1/me2/me3/me4
    are flagged MTG in metadata but are real Pokemon sets per
    sku_game_map — sku_game_map is authoritative, same rule the live
    /api/sets/completion handler already applies per-SKU).
    """
    set_metadata = _load_set_metadata()
    candidate_ids = []
    skipped = []
    for set_id, meta in set_metadata.items():
        if meta.get("game") != "MTG":
            continue
        override_game = _get_set_game_from_sku_map(set_id)
        if override_game and override_game != "MTG":
            skipped.append(set_id)
            continue
        candidate_ids.append(set_id)

    totals = _walk_all_mtg_set_totals(candidate_ids)

    tmp_path = MTG_SET_TOTALS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(totals, f)
    os.replace(tmp_path, MTG_SET_TOTALS_PATH)

    print(f"[MTG-TOTALS] Baked {len(totals)} MTG set totals -> "
          f"{MTG_SET_TOTALS_PATH} ({len(skipped)} skipped as non-MTG "
          f"overrides: {skipped})", flush=True)

    sample_ids = [sid for sid in ("10e", "2ed", "mkm") if sid in totals]
    samples = {sid: totals[sid] for sid in sample_ids}
    print(f"[MTG-TOTALS] Samples: {samples}", flush=True)

    return {"baked": len(totals), "skipped": skipped, "samples": samples}


def rebuild_identifier_lookup():
    """Rebuild identifier_lookup.json (OCR-first SKU lookup) from CardsDB.

    Logic is copied verbatim from the standalone build_identifier_lookup.py
    (key shape, collision rule, per-game bucketing) — that script stays as
    Craig's manual local tool against C:\\CardsDB; this is the Modal-aware
    twin the per-set scheduler chain calls so new sets stop leaving this
    file stale (it was previously manual-only).

    Always a full rescan, same as rebuild_mtg_set_totals/rebuild_set_card_lists
    above — collision detection needs the whole existing key set in memory
    regardless, so a true delta wouldn't save the expensive part anyway.
    """
    from vertical_loader import get_db_root as _gdbr
    cards_root = _gdbr() or "CardsDB"

    lookup = {"pokemon": {}, "mtg": {}, "ygo": {}}
    collisions = {"pokemon": 0, "mtg": 0, "ygo": 0}
    counts = {"pokemon": 0, "mtg": 0, "ygo_setcode": 0, "ygo_passcode": 0}
    skipped = {"pokemon": 0, "mtg": 0, "ygo": 0}

    def add_key(game, key, sku, bucket):
        if not key:
            return
        sub = lookup[game]
        if key in sub:
            if sub[key] != sku:
                collisions[game] += 1
            return
        sub[key] = sku
        counts[bucket] += 1

    for game_dir in ("pokemon", "mtg", "yugioh"):
        game_path = os.path.join(cards_root, game_dir)
        if not os.path.isdir(game_path):
            continue
        for sku_dir in os.listdir(game_path):
            profile_path = os.path.join(game_path, sku_dir, "profile.json")
            if not os.path.isfile(profile_path):
                continue
            sku = sku_dir
            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    p = json.load(f)
            except Exception as e:
                print(f"[ID-LOOKUP] WARN could not read {profile_path}: {e}", flush=True)
                continue

            card_number = str(p.get("card_number") or "").strip()
            set_id = str(p.get("set_id") or "").strip()
            category = str(p.get("category") or "").upper().strip()

            if category == "POKEMON":
                if not card_number or not set_id:
                    skipped["pokemon"] += 1
                    continue
                key = (f"jpn-{set_id}-{card_number}" if sku_dir.startswith("jpn-")
                       else f"{set_id}-{card_number}").lower()
                add_key("pokemon", key, sku, "pokemon")

            elif category == "MTG":
                if not card_number or not set_id:
                    skipped["mtg"] += 1
                    continue
                key = f"{set_id}-{card_number}".lower()
                add_key("mtg", key, sku, "mtg")

            elif category == "YUGIOH":
                added_any = False
                if card_number:
                    add_key("ygo", card_number.upper(), sku, "ygo_setcode")
                    added_any = True
                ygoprodeck_id = str(p.get("ygoprodeck_id") or "").strip()
                if ygoprodeck_id and re.match(r'^\d{5,8}$', ygoprodeck_id):
                    add_key("ygo", ygoprodeck_id, sku, "ygo_passcode")
                    added_any = True
                if not added_any:
                    skipped["ygo"] += 1

    tmp_path = IDENTIFIER_LOOKUP_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, IDENTIFIER_LOOKUP_PATH)

    total_keys = sum(len(v) for v in lookup.values())
    print(f"[ID-LOOKUP] Rebuilt {total_keys} keys "
          f"(pokemon={counts['pokemon']} mtg={counts['mtg']} "
          f"ygo_setcode={counts['ygo_setcode']} ygo_passcode={counts['ygo_passcode']}) "
          f"collisions={sum(collisions.values())} skipped={skipped} "
          f"-> {IDENTIFIER_LOOKUP_PATH}", flush=True)

    # Refresh the in-memory cache the OCR matcher reads from, so a running
    # server process picks up new keys without a redeploy/restart.
    _identifier_lookup.clear()
    _identifier_lookup.update(lookup)

    return {"total_keys": total_keys, "counts": counts, "collisions": collisions, "skipped": skipped}


# ── Set-detail card-list sidecars (one per game) ─────────────────────────────
POKEMON_SET_CARDS_PATH = "/modal_data/pokemon_set_card_lists.json" if _os.path.exists("/modal_data") else "pokemon_set_card_lists.json"
MTG_SET_CARDS_PATH = "/modal_data/mtg_set_card_lists.json" if _os.path.exists("/modal_data") else "mtg_set_card_lists.json"
YGO_SET_CARDS_PATH = "/modal_data/ygo_set_card_lists.json" if _os.path.exists("/modal_data") else "ygo_set_card_lists.json"

_SET_CARDS_PATH_BY_GAME = {
    "POKEMON": POKEMON_SET_CARDS_PATH,
    "MTG": MTG_SET_CARDS_PATH,
    "YUGIOH": YGO_SET_CARDS_PATH,
}

_json_sidecar_cache = {}  # path -> (mtime, data)


def _load_json_sidecar(path):
    """Generic mtime-cached JSON sidecar loader, mirroring the
    _load_set_metadata()/_load_mtg_set_totals_sidecar() caching pattern.
    Returns {} if the file is missing or fails to parse."""
    global _json_sidecar_cache
    try:
        current_mtime = _os.path.getmtime(path)
        cached = _json_sidecar_cache.get(path)
        if cached is not None and cached[0] == current_mtime:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _json_sidecar_cache[path] = (current_mtime, data)
        return data
    except Exception:
        return {}


def _detect_set_game(set_id, meta):
    """Same game-detection logic sets_cards() has always used: sku_game_map
    overrides metadata (collision sets like me1/me2/me3/me4), then prefix
    inference as a last resort."""
    game = _get_set_game_from_sku_map(set_id) or meta.get("game", "")
    if not game:
        if any(set_id.startswith(p) for p in ("sv", "swsh", "sm", "xy", "bw", "dp", "ex", "base", "neo", "gym", "e-")):
            game = "POKEMON"
        elif set_id.isdigit() or len(set_id) == 4 and set_id.isdigit():
            game = "YUGIOH"
        else:
            game = "MTG"
    return game


def _card_sort_key(c):
    try:
        return (0, int(re.sub(r"\D", "", c["card_number"]) or "0"))
    except Exception:
        return (1, c["card_number"])


def _build_set_card_list(set_id, game=None, meta=None):
    """Builds the per-set card list exactly as sets_cards() did before this
    sidecar existed — same game detection, same SQL queries, same MTG
    name-dedup (lowest card_number wins), same YGO LIMIT 200 +
    truncated/total_in_db semantics, same sort order. Shared by the live
    handler (fallback for any set absent from its game's baked sidecar)
    and the offline weekly bake (looping every set_id).

    Per-row profile.json reads are dispatched to a thread pool (I/O-bound
    network reads against CardsDB — same trick as the MTG totals bake),
    but results are merged back in the original SQL-row order, so the MTG
    dedup tie-break ("lowest card_number wins") is unchanged.

    Pass game/meta if the caller already computed them (the live handler
    needs them anyway to pick which sidecar to check) to avoid redoing
    that work; the bake passes them too, since it already has meta from
    its own set_metadata.json iteration.

    Returns (game, cards, truncated, total_in_db).
    """
    from concurrent.futures import ThreadPoolExecutor

    if meta is None:
        meta = _load_set_metadata().get(set_id, {})
    if game is None:
        game = _detect_set_game(set_id, meta)

    db_path = get_images_db_path()
    cards = []
    truncated = False
    total_in_db = 0

    from vertical_loader import get_db_root as _gdbr
    db_root = _gdbr() or "CardsDB"
    data_dir = get_data_dir()

    if game in ("POKEMON", "MTG"):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                if game == "POKEMON":
                    rows = conn.execute(
                        "SELECT image_id, sku FROM images WHERE sku LIKE ? LIMIT 500",
                        (set_id + "-%",)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT DISTINCT image_id, sku FROM images"
                        " WHERE sku LIKE ? OR sku LIKE ? LIMIT 500",
                        ("mtg-" + set_id + "-%", set_id + "-%")
                    ).fetchall()
            finally:
                conn.close()
        except Exception:
            rows = []

        # Drop SKUs that don't belong to this game (guards bare-prefix collisions, e.g. me1)
        rows = [r for r in rows if _get_sku_game(r["sku"]) == game]

        profiles = {}
        if rows:
            with ThreadPoolExecutor(max_workers=32) as pool:
                for sku, prof in pool.map(
                    lambda r: (r["sku"], _load_card_profile_for_sku(r["sku"], db_root, data_dir)),
                    rows,
                ):
                    profiles[sku] = prof

        seen_names = {}  # MTG dedup: name.lower() → card entry
        for row in rows:
            sku = row["sku"]
            image_id = row["image_id"]
            prof = profiles.get(sku) or {}
            name = prof.get("name") or sku
            card_number = prof.get("card_number") or ""

            if game == "MTG":
                key = name.lower().strip()
                existing = seen_names.get(key)
                if existing is None:
                    seen_names[key] = {
                        "sku": sku,
                        "name": name,
                        "card_number": card_number,
                        "img_url": "https://images.grailsweep.com/" + image_id + ".jpg",
                    }
                else:
                    # Keep lowest card_number variant
                    try:
                        if int(card_number) < int(existing["card_number"]):
                            seen_names[key] = {
                                "sku": sku,
                                "name": name,
                                "card_number": card_number,
                                "img_url": "https://images.grailsweep.com/" + image_id + ".jpg",
                            }
                    except (ValueError, TypeError):
                        pass
            else:
                cards.append({
                    "sku": sku,
                    "name": name,
                    "card_number": card_number,
                    "img_url": "https://images.grailsweep.com/" + image_id + ".jpg",
                })

        if game == "MTG":
            cards = list(seen_names.values())

    else:
        # YUGIOH: query images.db by SKU prefix (fast path)
        YGO_LIMIT = 200
        ygo_pattern = "ygo-" + set_id + "-%"
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                total_in_db = conn.execute(
                    "SELECT COUNT(*) FROM images WHERE sku LIKE ?", (ygo_pattern,)
                ).fetchone()[0]
                ygo_rows = conn.execute(
                    "SELECT image_id, sku FROM images WHERE sku LIKE ? LIMIT ?",
                    (ygo_pattern, YGO_LIMIT)
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            ygo_rows = []
        truncated = total_in_db > YGO_LIMIT

        profiles = {}
        if ygo_rows:
            with ThreadPoolExecutor(max_workers=32) as pool:
                for sku, prof in pool.map(
                    lambda r: (r["sku"], _load_card_profile_for_sku(r["sku"], db_root, data_dir)),
                    ygo_rows,
                ):
                    profiles[sku] = prof

        for row in ygo_rows:
            sku = row["sku"]
            image_id = row["image_id"]
            prof = profiles.get(sku) or {}
            name = prof.get("name") or sku
            card_number = prof.get("card_number") or ""
            cards.append({
                "sku": sku,
                "name": name,
                "card_number": card_number,
                "img_url": "https://images.grailsweep.com/" + image_id + ".jpg",
            })

    cards.sort(key=_card_sort_key)
    return game, cards, truncated, total_in_db


def rebuild_set_card_lists():
    """Weekly precompute: bakes the per-set card list (sku/name/card_number/
    img_url) for every set into THREE per-game sidecars — pokemon/mtg/ygo —
    so /api/sets/<set_id>/cards can skip the live SQL+profile-read path at
    request time.

    Calls _build_set_card_list(set_id) once per set_id in set_metadata.json.
    That function does its own game detection per set (same sku_game_map
    override the live handler already applies), so each baked entry is
    bucketed by whatever game _build_set_card_list itself determines — not
    by metadata's label, which is wrong for collision sets like me1.

    No set is excluded: the live handler never filtered by meta["exclude"]
    either, so this bake doesn't introduce one (parity with current
    behavior, not a new policy).

    Each of the three files is written atomically (.tmp + os.replace).
    """
    set_metadata = _load_set_metadata()
    buckets = {"POKEMON": {}, "MTG": {}, "YUGIOH": {}}

    for set_id, meta in set_metadata.items():
        game = _detect_set_game(set_id, meta)
        game, cards, truncated, total_in_db = _build_set_card_list(set_id, game=game, meta=meta)
        buckets.setdefault(game, {})[set_id] = {
            "cards": cards,
            "truncated": truncated,
            "total_in_db": total_in_db,
        }

    stats = {}
    for game, path in _SET_CARDS_PATH_BY_GAME.items():
        data = buckets.get(game, {})
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_path, path)
        stats[game] = {"sets": len(data), "cards": sum(len(e["cards"]) for e in data.values())}

    total_cards = sum(s["cards"] for s in stats.values())

    print(f"[SET-CARDS] Baked -> {stats}, total_cards={total_cards}", flush=True)
    print(f"[SET-CARDS] Files: {POKEMON_SET_CARDS_PATH} | "
          f"{MTG_SET_CARDS_PATH} | {YGO_SET_CARDS_PATH}", flush=True)

    samples = {}
    for sid in ("base1", "snc", "2017"):
        for game, data in buckets.items():
            if sid in data:
                samples[sid] = {"game": game, "cards": len(data[sid]["cards"])}
                break
    print(f"[SET-CARDS] Samples: {samples}", flush=True)

    return {"stats": stats, "total_cards": total_cards, "samples": samples}


def _get_sku_game(sku):
    """Return game string for a SKU. Prefix is definitive for ygo-/mtg-;
    bare-prefix SKUs (Pokémon vs MTG collision) need a profile read.
    Results cached in _sku_game_cache."""
    if sku in _sku_game_cache:
        return _sku_game_cache[sku]
    if sku.startswith("ygo-"):
        result = "YUGIOH"
    elif sku.startswith("mtg-"):
        result = "MTG"
    else:
        try:
            profile = _load_card_profile_for_sku(sku, get_db_root(), get_data_dir())
            if profile:
                cat = (profile.get("category") or "").upper()
                if cat == "YUGIOH":
                    result = "YUGIOH"
                elif cat == "MTG":
                    result = "MTG"
                else:
                    result = "POKEMON"
            else:
                result = "POKEMON"
        except Exception:
            result = "POKEMON"
    _sku_game_cache[sku] = result
    return result


def _get_set_game_from_sku_map(set_id):
    """Returns game for a set_id derived from sku_game_map entries.
    Ground truth for collision cases (e.g. me1/me2/me3 are MTG in
    set_metadata but POKEMON in sku_game_map).
    Returns None if set_id has no entries in sku_game_map."""
    return _set_game_cache.get(set_id)


REFERRALS_PATH = "/modal_data/referrals.json" if _os.path.exists("/modal_data") else "referrals.json"

def _load_referrals():
    import json as _json
    try:
        with open(REFERRALS_PATH, "r") as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_referrals(data):
    _atomic_write_json(REFERRALS_PATH, data, indent=2, log_prefix="REFERRALS")

def _get_or_create_ref_code(access_code):
    import random
    subs = _load_subs()
    entry = subs.get(access_code)
    if not entry:
        return None
    if entry.get("ref_code"):
        return entry["ref_code"]
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    part1 = ''.join(random.choices(chars, k=4))
    part2 = ''.join(random.choices(chars, k=4))
    ref_code = f"GRAIL-REF-{part1}-{part2}"
    entry["ref_code"] = ref_code
    entry["ref_count"] = entry.get("ref_count", 0)
    entry["ref_max"] = 10
    subs[access_code] = entry
    _save_subs(subs)
    referrals = _load_referrals()
    referrals[ref_code] = access_code
    _save_referrals(referrals)
    return ref_code

def _apply_referral_reward(ref_code):
    from datetime import datetime, timedelta
    referrals = _load_referrals()
    referrer_access_code = referrals.get(ref_code)
    if not referrer_access_code:
        return False
    subs = _load_subs()
    entry = subs.get(referrer_access_code)
    if not entry:
        return False
    ref_count = entry.get("ref_count", 0)
    ref_max = entry.get("ref_max", 10)
    if ref_count >= ref_max:
        return False
    expires = entry.get("expires_at")
    if expires and entry.get("tier") != "lifetime":
        try:
            base = datetime.fromisoformat(expires)
        except Exception:
            base = datetime.utcnow()
        entry["expires_at"] = (base + timedelta(days=30)).isoformat()
    entry["ref_count"] = ref_count + 1
    subs[referrer_access_code] = entry
    _save_subs(subs)
    return True

STATS_PATH = "/modal_data/stats.json" if _os.path.exists("/modal_data") else "stats.json"

_stats_last_good = None   # holds the last successfully-read, VALID stats dict

def _load_stats():
    global _stats_last_good
    import json as _json
    try:
        import modal
        modal.Volume.from_name("matchit-data-v2").reload()
    except Exception:
        pass
    try:
        with open(STATS_PATH, "r") as f:
            parsed = _json.load(f)
        if isinstance(parsed.get("total_scans"), int) and parsed.get("total_scans", 0) > 0:
            _stats_last_good = parsed
            return parsed
        raise ValueError("stats read returned no valid total_scans")
    except Exception:
        if _stats_last_good is not None:
            return _stats_last_good
        return {"total_scans": 0, "today_scans": 0, "today_date": ""}

def _preload_stats_cache():
    global _stats_last_good
    try:
        s = _load_stats()
        if s and s.get("total_scans", 0) > 0:
            print(f"[STATS-PRELOAD] seeded total={s.get('total_scans')} today={s.get('today_scans')}", flush=True)
        else:
            print("[STATS-PRELOAD] cold/empty read, will settle on first valid read", flush=True)
    except Exception as e:
        print(f"[STATS-PRELOAD] skipped: {e}", flush=True)

_preload_stats_cache()

def _save_stats(data):
    _atomic_write_json(STATS_PATH, data, log_prefix="STATS")
    try:
        import modal
        modal.Volume.from_name("matchit-data-v2").commit()
    except Exception:
        pass

def _increment_scan_counter(source="server"):
    # source: "server" for Modal GPU path, "ondevice" for
    # on-device MobileCLIP gate-accept path
    try:
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y-%m-%d")
        stats = _load_stats()
        if stats.get("today_date") != today:
            stats["today_scans"] = 0
            stats["today_date"] = today
        stats["total_scans"] = stats.get("total_scans", 0) + 1
        stats["today_scans"] = stats.get("today_scans", 0) + 1
        _save_stats(stats)
    except Exception as e:
        print(f"[STATS] Failed to increment: {e}")
    # Per-source split counters live in a modal.Dict (NOT stats.json) so the
    # serve_light and GPU containers can't overwrite each other's writes — same
    # from_name(create_if_missing=True) + read-modify-write pattern as the
    # sku-scan-freq Dict in _increment_sku_scan_freq.
    if source == "ondevice":
        try:
            import modal
            _d = modal.Dict.from_name(
                "scan-source-counters", create_if_missing=True)
            _d["ondevice"] = _d.get("ondevice", 0) + 1
        except Exception as _e:
            print(f"[STATS] ondevice counter failed: {_e}")
    else:
        try:
            import modal
            _d = modal.Dict.from_name(
                "scan-source-counters", create_if_missing=True)
            _d["modal"] = _d.get("modal", 0) + 1
        except Exception as _e:
            print(f"[STATS] modal counter failed: {_e}")


PRICE_HISTORY_PATH = "/modal_data/price_history.json" if _os.path.exists("/modal_data") else "price_history.json"

def _load_price_history():
    import json as _json
    try:
        with open(PRICE_HISTORY_PATH, "r") as f:
            return _json.load(f)
    except Exception:
        return {}

_last_vol_commit = 0

def _save_price_history(data):
    global _last_vol_commit
    _atomic_write_json(PRICE_HISTORY_PATH, data, log_prefix="PRICE_HISTORY")
    # Throttle volume commits: at most once every 300s. The file write above is
    # local to the container and fast; the commit is a slow network flush. Prices
    # only change meaningfully once a day (scheduler-driven), so a 5-min commit
    # cadence loses at most a few intraday points if a container scales down.
    now = time.time()
    if now - _last_vol_commit < 300:
        return
    try:
        import modal
        modal.Volume.from_name("matchit-data-v2").commit()
        _last_vol_commit = now
    except Exception:
        pass

def _record_price(sku, gbp_price):
    if not sku or not gbp_price:
        return
    try:
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y-%m-%d")
        history = _load_price_history()
        if sku not in history:
            history[sku] = []
        entries = history[sku]
        if entries and entries[-1].get("date") == today:
            entries[-1]["gbp"] = round(float(gbp_price), 2)
        else:
            entries.append({"date": today, "gbp": round(float(gbp_price), 2)})
        if len(entries) > 30:
            history[sku] = entries[-30:]
        else:
            history[sku] = entries
        _save_price_history(history)
    except Exception as e:
        print(f"[PRICE_HISTORY] Failed to record: {e}")


# Per-SKU scan-frequency counter (card popularity). Deliberately SEPARATE from
# the per-fingerprint free-scan quota Dict ("scan-counters" in db.py): that one
# is keyed by user fingerprint/device and enforces the monthly free limit; this
# one is keyed by sku and just counts how often each card gets scanned.
_SKU_SCAN_FREQ_DICT = "sku-scan-freq"

def _increment_sku_scan_freq(sku):
    """Fire-and-forget per-sku scan-frequency increment.

    Synchronous (NOT threaded) but fully guarded: any failure (modal
    unavailable, network, missing sku) is swallowed so it can never affect
    the scan result returned to the user. Key = sku, value = int count.
    """
    if not sku:
        return
    try:
        import modal
        d = modal.Dict.from_name(_SKU_SCAN_FREQ_DICT, create_if_missing=True)
        # read-modify-write — not concurrency-safe; revisit before relying on counts at real traffic
        d.put(sku, d.get(sku, 0) + 1)
    except Exception as e:
        print(f"[SKU_FREQ] increment failed for {sku}: {e}")

def _rule_based_grade(image_path):
    try:
        from PIL import Image, ImageFilter
        import numpy as np

        # Handle HEIC and other formats robustly
        try:
            img = Image.open(image_path).convert("RGB")
        except Exception:
            with open(image_path, 'rb') as _f:
                from io import BytesIO
                img = Image.open(BytesIO(_f.read())).convert("RGB")
        img = img.resize((300, 420), Image.LANCZOS)
        arr = np.array(img)

        left_strip  = arr[:, :15, :].mean()
        right_strip = arr[:, -15:, :].mean()
        top_strip   = arr[:15, :, :].mean()
        bot_strip   = arr[-15:, :, :].mean()
        h_diff = abs(left_strip - right_strip)
        v_diff = abs(top_strip - bot_strip)
        centering_score = max(0, 10 - (h_diff + v_diff) / 8)

        edge_brightness = (left_strip + right_strip + top_strip + bot_strip) / 4
        edge_score = min(10, edge_brightness / 22)

        face = arr[30:390, 20:280, :]
        variance = float(np.std(face))
        surface_score = min(10, max(4, 10 - (variance - 60) / 12))

        gray = img.convert("L")
        corners = [
            gray.crop((0,   0,   30, 30)),
            gray.crop((270, 0,   300, 30)),
            gray.crop((0,   390, 30, 420)),
            gray.crop((270, 390, 300, 420))
        ]
        corner_scores = []
        for c in corners:
            edges = c.filter(ImageFilter.FIND_EDGES)
            corner_scores.append(np.array(edges).mean())
        corner_sharpness = sum(corner_scores) / len(corner_scores)
        corner_score = min(10, corner_sharpness / 4)

        score = (
            centering_score * 0.30 +
            edge_score      * 0.35 +
            surface_score   * 0.30 +
            corner_score    * 0.05
        )
        score_high = round(min(10, max(1, score)), 1)
        score_low = round(max(1, score_high - 1.0), 1)

        if score_low >= 10.0:  label = "Gem Mint"
        elif score_low >= 9.0: label = "Mint"
        elif score_low >= 8.0: label = "Near Mint-Mint"
        elif score_low >= 7.0: label = "Near Mint"
        elif score_low >= 6.0: label = "Excellent-Mint"
        elif score_low >= 5.0: label = "Excellent"
        elif score_low >= 4.0: label = "Very Good-Excellent"
        elif score_low >= 3.0: label = "Very Good"
        elif score_low >= 2.0: label = "Good"
        else:                  label = "Poor"

        return {"score": score_low, "score_high": score_high, "label": label, "method": "auto"}

    except Exception as e:
        import traceback
        print(f"[GRADE] Rule-based grading failed: {e}")
        print(f"[GRADE] Traceback: {traceback.format_exc()}")
        print(f"[GRADE] Image path was: {image_path}")
        return None


def _safe_grade(image_path):
    """Wrapper around _rule_based_grade. Always returns a grade dict, never None.
    On any exception or unexpected return, returns an honest 'Grade unavailable' fallback
    so the client never receives null and the user sees a distinct UI badge."""
    try:
        result = _rule_based_grade(image_path)
        if isinstance(result, dict) and result.get("label") and result.get("method"):
            return result
        # _rule_based_grade returned None or malformed — honest fallback
        return {"score": None, "label": "Grade unavailable", "method": "auto"}
    except Exception as e:
        try:
            import logging
            logging.getLogger(__name__).warning("[GRADE] _safe_grade caught exception: %s", e, exc_info=True)
        except Exception:
            pass
        return {"score": None, "label": "Grade unavailable", "method": "auto"}


def _extract_gbp_from_profile(profile):
    """Extract first available GBP price from a profile dict, same logic as results.html.
    Rate comes from the cached fx_rates.json (see fx_rates.py) — cache-read
    only, no network call here. Pick-order unified with the other 4
    consumers: market > trend > avg_sell > mid."""
    if not profile:
        return None
    prices = profile.get("prices") if isinstance(profile, dict) else None
    if not prices:
        return None
    fx = get_fx()
    for src, sdata in prices.items():
        if "ebay" in src.lower() or "amazon" in src.lower():
            continue
        if not isinstance(sdata, dict):
            continue
        for _var, vdata in sdata.items():
            if isinstance(vdata, dict):
                price = vdata.get("market") or vdata.get("trend") or vdata.get("avg_sell") or vdata.get("mid")
            else:
                price = vdata
            if price:
                mult = fx["eur_gbp"] if "cardmarket" in src else fx["usd_gbp"]
                return round(float(price) * mult, 2)
    return None

def _issue_new_code(email, tier, subscription_id, ref_code="", source="stripe", device_fingerprint=None):
    import random
    from datetime import datetime, timedelta

    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    part1 = ''.join(random.choices(chars, k=4))
    part2 = ''.join(random.choices(chars, k=4))
    code = f"GRAIL-{part1}-{part2}"

    if tier == "lifetime":
        expires = None
    elif tier == "annual":
        expires = (datetime.utcnow() + timedelta(days=366)).isoformat()
    else:
        expires = (datetime.utcnow() + timedelta(days=32)).isoformat()

    subs = _load_subs()

    # Supersede any other active subscription code already tied to this
    # device, so a device never carries two simultaneously-active
    # subscriptions (e.g. a beta-tester code left active after a Google
    # Play purchase). Top-up codes are untouched — they are independent
    # of subscription status. device_fingerprint uses the same formula as
    # validate_premium()'s per-code device list, since that's the only
    # place this identifier is actually stored.
    if device_fingerprint:
        for _old_code, _entry in subs.items():
            if (_entry.get("type") != "topup"
                    and _entry.get("tier") in ("monthly", "annual", "lifetime")
                    and _entry.get("status") == "active"
                    and device_fingerprint in (_entry.get("devices") or [])):
                _entry["status"] = "superseded"
                _entry["superseded_at"] = datetime.utcnow().isoformat()
                _entry["superseded_by"] = code
                print(f"[SUPERSEDE] {_old_code} -> {code} (device={device_fingerprint})", flush=True)

    subs[code] = {
        "email": email,
        "tier": tier,
        "stripe_subscription_id": subscription_id,
        "created_at": datetime.utcnow().isoformat(),
        "expires_at": expires,
        "status": "active",
        "source": source,
    }
    _save_subs(subs)

    tier_label = {
        "monthly": "Pro Monthly",
        "annual": "Ultimate Plan",
        "lifetime": "Lifetime",
    }.get(tier, "Pro")
    expiry_text = (
        "Your code never expires."
        if tier == "lifetime"
        else "Your subscription renews automatically — you won't need a new code."
    )

    body_text = f"""Hi there,

Thanks for subscribing to GrailSweep {tier_label}.

Your access code is:

    {code}

To activate Pro:
1. Open GrailSweep
2. Go to your Collection
3. Click "Enter Access Code"
4. Paste the code above

{expiry_text}

Keep this email safe — you'll need the code if you clear your browser data.

Questions? Reply to this email.

The GrailSweep team
"""

    body_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1830;background:#ffffff;">
        <h1 style="color:#1a1830;font-size:1.3rem;font-weight:600;margin:0 0 16px 0;">Welcome to GrailSweep {tier_label}</h1>
        <p style="margin:0 0 16px 0;line-height:1.5;">Thanks for subscribing. Your access code is below.</p>
        <div style="background:#f6f4fb;border-left:3px solid #b14dff;border-radius:4px;padding:16px 20px;margin:20px 0;">
            <div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:1.2rem;font-weight:600;color:#1a1830;letter-spacing:1px;">{code}</div>
            <div style="font-size:0.85rem;color:#5f5e5a;margin-top:6px;">Your access code</div>
        </div>
        <p style="margin:16px 0 8px 0;line-height:1.5;font-weight:500;">To activate Pro:</p>
        <ol style="margin:0 0 16px 0;padding-left:20px;line-height:1.7;">
            <li>Open GrailSweep</li>
            <li>Go to your Collection</li>
            <li>Click "Enter Access Code"</li>
            <li>Paste the code above</li>
        </ol>
        <p style="color:#5f5e5a;font-size:0.9rem;line-height:1.5;margin:16px 0;">
            {expiry_text}
        </p>
        <p style="color:#5f5e5a;font-size:0.9rem;line-height:1.5;margin:16px 0;">
            Keep this email safe — you'll need the code if you clear your browser data. Questions? Reply to this email.
        </p>
        <hr style="border:none;border-top:1px solid #e5e2ed;margin:24px 0 16px 0;">
        <p style="font-size:0.8rem;color:#888780;margin:0;">
            GrailSweep — Trading card scanner and price reference<br>
            <a href="https://grailsweep.com" style="color:#7c3aed;text-decoration:none;">grailsweep.com</a>
        </p>
    </div>
    """

    if email:
        try:
            gs_send_email(
                to=email,
                subject=f"Your GrailSweep {tier_label} access code",
                html=body_html,
                text=body_text,
            )
            print(f"[ISSUE] Sent code {code} to {email} tier={tier}", flush=True)
        except Exception as e:
            print(f"[EMAIL ERROR] Failed to send code {code} to {email}: {e}", flush=True)
    else:
        print(f"[ISSUE] Issued code {code} tier={tier} source={source} (no email — in-app activation)", flush=True)

    # Apply referral reward if this purchase used a referral code
    if ref_code:
        _apply_referral_reward(ref_code)
    return code


def _issue_new_topup_code(email, credits=125, payment_intent_id=None, source="stripe"):
    """
    Issue a one-time top-up redemption code (TOPUP-XXXX-XXXX).

    Top-up codes grant N additional scan credits when redeemed.
    Unlike GRAIL- codes (which are tied to subscriptions and
    validated on every scan), TOPUP- codes are one-shot: they
    are redeemed exactly once via /api/redeem-topup, at which
    point credits are written to the redeeming user's modal.Dict
    scan counter and the code is marked spent.

    Args:
        email: purchaser's email from Stripe checkout (blank for Google
            Play purchases, which are auto-redeemed on the same device)
        credits: number of scan credits to grant on redemption (default 125)
        payment_intent_id: Stripe payment_intent ID (or Play purchaseToken)
            for traceability
        source: "stripe" or "google_play"
    """
    import random
    from datetime import datetime

    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    part1 = ''.join(random.choices(chars, k=4))
    part2 = ''.join(random.choices(chars, k=4))
    code = f"TOPUP-{part1}-{part2}"

    subs = _load_subs()
    subs[code] = {
        "type":               "topup",
        "email":              email,
        "credits_total":      credits,
        "credits_remaining":  credits,
        "stripe_payment_intent_id": payment_intent_id,
        "created_at":         datetime.utcnow().isoformat(),
        "status":             "unredeemed",
        "redeemed_at":        None,
        "redeemed_by_device": None,
        "redeemed_by_fp":     None,
        "source":             source,
    }
    _save_subs(subs)

    body_text = f"""Thanks for your GrailSweep top-up purchase.

Your top-up code: {code}

This code grants you {credits} additional card scans on top of your free monthly allowance. Top-up scans never expire — use them whenever you need them.

To redeem your code:
1. Go to https://grailsweep.com/match
2. Enter the code in the "Redeem top-up" field
3. Your {credits} extra scans will be added immediately

If you have any trouble redeeming, contact us at support@grailsweep.com.

The GrailSweep team
"""

    body_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1830;background:#ffffff;">
        <h1 style="color:#1a1830;font-size:1.3rem;font-weight:600;margin:0 0 16px 0;">Your top-up is ready</h1>
        <p style="margin:0 0 16px 0;line-height:1.5;">Thanks for your purchase. Your top-up code is below.</p>
        <div style="background:#f6f4fb;border-left:3px solid #b14dff;border-radius:4px;padding:16px 20px;margin:20px 0;">
            <div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:1.2rem;font-weight:600;color:#1a1830;letter-spacing:1px;">{code}</div>
            <div style="font-size:0.85rem;color:#5f5e5a;margin-top:6px;">{credits} additional scans</div>
        </div>
        <p style="margin:16px 0 8px 0;line-height:1.5;">This code grants you <strong>{credits} additional card scans</strong> on top of your free monthly allowance. Top-up scans never expire.</p>
        <p style="margin:16px 0 8px 0;line-height:1.5;font-weight:500;">To redeem your code:</p>
        <ol style="margin:0 0 16px 0;padding-left:20px;line-height:1.7;">
            <li>Go to <a href="https://grailsweep.com/match" style="color:#7c3aed;text-decoration:none;">grailsweep.com/match</a></li>
            <li>Enter the code in the "Redeem top-up" field</li>
            <li>Your {credits} extra scans will be added immediately</li>
        </ol>
        <p style="color:#5f5e5a;font-size:0.9rem;line-height:1.5;margin:16px 0;">
            If you have any trouble redeeming, contact us at <a href="mailto:support@grailsweep.com" style="color:#7c3aed;text-decoration:none;">support@grailsweep.com</a>.
        </p>
        <hr style="border:none;border-top:1px solid #e5e2ed;margin:24px 0 16px 0;">
        <p style="font-size:0.8rem;color:#888780;margin:0;">
            GrailSweep — Trading card scanner and price reference<br>
            <a href="https://grailsweep.com" style="color:#7c3aed;text-decoration:none;">grailsweep.com</a>
        </p>
    </div>
    """

    if email:
        try:
            gs_send_email(
                to=email,
                subject=f"Your GrailSweep top-up: {credits} scans",
                html=body_html,
                text=body_text,
            )
            print(f"[TOPUP] Issued code {code} to {email} ({credits} credits)", flush=True)
        except Exception as e:
            print(f"[TOPUP EMAIL ERROR] Failed to send code {code} to {email}: {e}", flush=True)
            # Code is already written to subscriptions.json — email failure
            # doesn't lose the code. Manual recovery possible via direct
            # subscriptions.json lookup.
    else:
        print(f"[TOPUP] Issued code {code} ({credits} credits) source={source} (no email — in-app activation)", flush=True)

    return code


def _extend_subscription(subscription_id):
    from datetime import datetime, timedelta
    subs = _load_subs()
    for code, data in subs.items():
        if data.get("stripe_subscription_id") == subscription_id:
            current = data.get("expires_at")
            if current:
                try:
                    base = datetime.fromisoformat(current)
                except Exception:
                    base = datetime.utcnow()
                data["expires_at"] = (base + timedelta(days=32)).isoformat()
            data["status"] = "active"
            break
    _save_subs(subs)


def _cancel_subscription(subscription_id):
    subs = _load_subs()
    for code, data in subs.items():
        if data.get("stripe_subscription_id") == subscription_id:
            data["status"] = "cancelled"
            break
    _save_subs(subs)


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip().upper()
    sub  = data.get("subscription")
    if not code or not sub:
        return jsonify({"error": "missing code or subscription"}), 400
    subs = _load_subs()
    cfg_codes = CFG.get("premium_codes", [])
    if code not in subs and code not in cfg_codes:
        return jsonify({"error": "invalid code"}), 403
    push_subs = _load_push_subs()
    # Store list of subscriptions per code (multiple devices)
    existing = push_subs.get(code, [])
    endpoint = sub.get("endpoint", "")
    if not any(s.get("endpoint") == endpoint for s in existing):
        existing.append(sub)
    push_subs[code] = existing
    _save_push_subs(push_subs)
    return jsonify({"ok": True})


@app.route("/api/tier/dismiss-warning", methods=["POST"])
def tier_dismiss_warning():
    """User-action endpoint: marks an in-period warning as dismissed.
    Auth: code validated against subscriptions.json (mirrors push/subscribe pattern).
    Body: {"code": "GRAIL-XXXX-XXXX", "flag": "80pct" | "transition"}
    """
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    flag = (data.get("flag") or "").strip().lower()
    if not code:
        return jsonify({"ok": False, "error": "code_required"}), 400
    if flag not in ("80pct", "transition"):
        return jsonify({"ok": False, "error": "invalid_flag"}), 400
    try:
        subs = _load_subs()
        sub_record = subs.get(code)
        if not sub_record:
            return jsonify({"ok": False, "error": "code_not_found"}), 404
        tier = sub_record.get("tier")
        if tier not in ("monthly", "annual"):
            return jsonify({"ok": False, "error": "not_capped_tier"}), 400
        if flag == "80pct":
            sub_record["tier_warned_80pct"] = True
        else:
            sub_record["tier_transition_warned"] = True
        subs[code] = sub_record
        _save_subs(subs)
        return jsonify({"ok": True, "flag": flag, "dismissed": True})
    except Exception as e:
        print(f"[TIER-DISMISS] Error for code={code}, flag={flag}: {e}", flush=True)
        return jsonify({"ok": False, "error": "server_error"}), 500


@app.route("/api/push/send", methods=["POST"])
def push_send():
    admin_key = request.headers.get("X-Admin-Key", "")
    if not session.get("is_admin") and admin_key != str(CFG.get("admin_password", "")):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    code    = data.get("code", "").strip().upper()
    title   = data.get("title", "GrailSweep Alert")
    body    = data.get("body", "")
    url     = data.get("url", "/collection")
    push_subs = _load_push_subs()
    targets = push_subs.get(code, []) if code else [s for slist in push_subs.values() for s in slist]
    if not targets:
        return jsonify({"sent": 0})
    try:
        from pywebpush import webpush, WebPushException
        import json as _json
        vapid_private = os.environ.get("VAPID_PRIVATE_KEY", "") or CFG.get("vapid_private_key", "")
        if vapid_private.startswith("-----"):
            import base64 as _b64
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.backends import default_backend as _dbe
            _key = load_pem_private_key(vapid_private.encode(), password=None, backend=_dbe())
            _raw = _key.private_numbers().private_value.to_bytes(32, "big")
            vapid_private = _b64.urlsafe_b64encode(_raw).rstrip(b"=").decode()
        vapid_claims  = {"sub": "mailto:grailsweep@gmail.com"}
        sent = 0
        for sub in targets:
            try:
                webpush(
                    subscription_info=sub,
                    data=_json.dumps({"title": title, "body": body, "url": url}),
                    vapid_private_key=vapid_private,
                    vapid_claims=vapid_claims,
                )
                sent += 1
            except WebPushException:
                pass
        return jsonify({"sent": sent})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/collection/sync", methods=["GET"])
def collection_sync_get():
    code = request.args.get("code", "").strip().upper()
    if not code:
        return jsonify({"error": "missing code"}), 400
    # Validate code exists and is active
    subs = _load_subs()
    cfg_codes = CFG.get("premium_codes", [])
    if code not in subs and code not in cfg_codes:
        return jsonify({"error": "invalid code"}), 403
    collections = _load_collections()
    items = collections.get(code, [])
    return jsonify({"items": items, "count": len(items)})

@app.route("/api/collection/sync", methods=["POST"])
def collection_sync_post():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip().upper()
    items = data.get("items", [])
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not isinstance(items, list):
        return jsonify({"error": "invalid items"}), 400
    # Validate code exists and is active
    subs = _load_subs()
    cfg_codes = CFG.get("premium_codes", [])
    if code not in subs and code not in cfg_codes:
        return jsonify({"error": "invalid code"}), 403
    # Cap at 10000 items to prevent abuse
    items = items[:10000]
    collections = _load_collections()
    collections[code] = items
    _save_collections(collections)
    return jsonify({"saved": len(items)})

@app.route("/api/collection/value_history", methods=["POST"])
def collection_value_history():
    from datetime import datetime as _dt, timedelta as _td
    data = request.get_json(silent=True) or {}
    code  = data.get("code", "").strip().upper()
    items = data.get("items", [])
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not isinstance(items, list):
        return jsonify({"error": "invalid items"}), 400
    subs      = _load_subs()
    cfg_codes = CFG.get("premium_codes", [])
    if code not in subs and code not in cfg_codes:
        return jsonify({"error": "invalid code"}), 401
    history = _load_price_history()
    today   = _dt.utcnow().date()
    days    = [today - _td(days=29 - i) for i in range(30)]
    result  = []
    for day in days:
        day_str   = day.strftime("%Y-%m-%d")
        day_total = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            sku          = item.get("sku") or None
            gbp_fallback = float(item.get("gbp") or 0)
            if sku and sku in history:
                entries = history[sku]
                best = None
                for entry in entries:
                    if entry.get("date", "") <= day_str:
                        best = entry
                day_total += float(best["gbp"]) if best else gbp_fallback
            else:
                day_total += gbp_fallback
        result.append({"date": day_str, "value": round(day_total, 2)})
    return jsonify({"days": result})


@app.route("/api/watchlist/sync", methods=["GET"])
def watchlist_sync_get():
    code = request.args.get("code", "").strip().upper()
    if not code:
        return jsonify({"error": "missing code"}), 400
    subs = _load_subs()
    cfg_codes = CFG.get("premium_codes", [])
    if code not in subs and code not in cfg_codes:
        return jsonify({"error": "invalid code"}), 403
    watchlist = _load_watchlist()
    items = watchlist.get(code, [])
    return jsonify({"items": items, "count": len(items)})

@app.route("/api/watchlist/sync", methods=["POST"])
def watchlist_sync_post():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip().upper()
    items = data.get("items", [])
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not isinstance(items, list):
        return jsonify({"error": "invalid items"}), 400
    subs = _load_subs()
    cfg_codes = CFG.get("premium_codes", [])
    if code not in subs and code not in cfg_codes:
        return jsonify({"error": "invalid code"}), 403
    items = items[:1000]
    watchlist = _load_watchlist()
    watchlist[code] = items
    _save_watchlist(watchlist)
    return jsonify({"ok": True, "count": len(items)})

@app.route("/watchlist")
def watchlist_page():
    return render_template("watchlist.html")

@app.route("/sets")
def sets_page():
    return render_template("sets.html")

@app.route("/api/sets/completion", methods=["POST"])
def sets_completion():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    if not isinstance(items, list):
        return jsonify({"error": "invalid items"}), 400

    from vertical_loader import get_db_root as _gdbr
    db_root = _gdbr() or "CardsDB"
    set_metadata = _load_set_metadata()
    sets_data = {}

    for item in items:
        if not isinstance(item, dict):
            continue
        sku = item.get("sku", "")
        if not sku:
            continue

        set_id = _get_set_id_from_sku(sku)

        if not set_id:
            continue

        # sku_game_map is per-SKU ground truth — use it first.
        # Bare-prefix collisions (e.g. me1 = MTG in set_metadata but
        # POKEMON in sku_game_map) are resolved here, not post-hoc.
        sku_game = _get_sku_game(sku)
        if sku_game:
            game = sku_game
        else:
            # No sku_game_map entry — fall back to prefix then metadata
            if sku.startswith("ygo-"):
                game = "YUGIOH"
            elif sku.startswith("mtg-"):
                game = "MTG"
            else:
                game = "POKEMON"
            meta_game = set_metadata.get(set_id, {}).get("game")
            if meta_game and meta_game != game:
                game = meta_game

        if _is_promo_set(set_id, game):
            continue

        if set_id not in sets_data:
            meta = set_metadata.get(set_id, {})
            # When sku_game_map overrides the metadata game, the metadata
            # name is also wrong (e.g. me1 → "Masters Edition" but cards
            # are Pokémon "Mega Evolution"). Use profile set_name instead.
            meta_game = meta.get("game")
            if sku_game and meta_game and sku_game != meta_game:
                try:
                    _prof = _load_card_profile_for_sku(sku, db_root, get_data_dir())
                    set_display_name = (_prof.get("set_name") or
                                        item.get("set_name") or set_id)
                except Exception:
                    set_display_name = item.get("set_name") or set_id
            else:
                set_display_name = meta.get("name") or item.get("set_name") or set_id
            sets_data[set_id] = {
                "set_id": set_id,
                "name": set_display_name,
                "game": game,
                "owned_skus": set(),
                "owned_names": set(),
                "exclude": meta.get("exclude", False),
            }

        sets_data[set_id]["owned_skus"].add(sku)
        if game == "MTG":
            raw_name = item.get("name") or sku
            sets_data[set_id]["owned_names"].add(raw_name.lower().strip())

    result = []
    for set_id, sd in sets_data.items():
        if sd["exclude"]:
            continue
        meta = set_metadata.get(set_id, {})
        game = sd["game"]
        if game == "POKEMON":
            total = meta.get("printed_total") or meta.get("total")
        elif game == "MTG":
            total = _get_mtg_set_total(set_id)
        else:
            total = meta.get("total")

        if game == "MTG":
            owned = len(sd["owned_names"])
        else:
            owned = len(sd["owned_skus"])

        pct = round(owned / total * 100, 1) if total else None
        result.append({
            "set_id": set_id,
            "name": sd["name"],
            "game": game,
            "owned": owned,
            "total": total,
            "pct": pct,
        })

    result.sort(key=lambda x: (-(x["pct"] or -1), -x["owned"]))
    return jsonify({"sets": result, "count": len(result)})


@app.route("/api/sets/<set_id>/cards")
def sets_cards(set_id):
    set_metadata = _load_set_metadata()
    meta = set_metadata.get(set_id, {})
    # sku_game_map is authoritative for set_id collisions
    # (e.g. me1 is MTG in set_metadata but POKEMON in sku_game_map)
    game = _detect_set_game(set_id, meta)

    # Game is known before any I/O, so only that game's sidecar is ever
    # loaded — a POKEMON request never touches the MTG/YGO files.
    sidecar_path = _SET_CARDS_PATH_BY_GAME.get(game)
    entry = _load_json_sidecar(sidecar_path).get(set_id) if sidecar_path else None

    if entry is not None:
        cards = entry["cards"]
        truncated = entry.get("truncated", False)
        total_in_db = entry.get("total_in_db", len(cards))
    else:
        game, cards, truncated, total_in_db = _build_set_card_list(set_id, game=game, meta=meta)

    set_name = meta.get("name") or set_id
    resp = {
        "set_id": set_id,
        "name": set_name,
        "game": game,
        "total": len(cards),
        "cards": cards,
        "truncated": truncated if game == "YUGIOH" else False,
        "total_in_db": total_in_db if game == "YUGIOH" else len(cards),
        "printed_total": meta.get("printed_total") if game == "POKEMON" else None,
    }
    return jsonify(resp)

@app.route("/api/fx_rates")
def fx_rates():
    """Cache-backed only — see fx_rates.py. No live frankfurter call per
    request; the cache is refreshed once daily by a separate cron job.
    Shape: {"usd_gbp": <rate>, "eur_gbp": <rate>} — both already direct
    multipliers, no client-side division needed."""
    return jsonify(get_fx())


@app.route("/api/validate_premium", methods=["POST"])
def validate_premium():
    from datetime import datetime
    import hashlib

    data = request.json
    code = data.get("code", "").strip().upper()

    # Generate a simple device fingerprint from request headers
    ua = request.headers.get("User-Agent", "")
    lang = request.headers.get("Accept-Language", "")
    ip = request.headers.get("CF-Connecting-IP",
         request.headers.get("X-Forwarded-For", request.remote_addr or ""))
    # Use first part of IP only (not full IP - privacy friendly)
    ip_partial = ".".join(ip.split(".")[:2]) if ip else ""
    fingerprint = hashlib.md5((ua + lang + ip_partial).encode()).hexdigest()[:16]

    # Load subscriptions (needed for both legacy status-override and subscription lookup below)
    try:
        with open(SUBS_PATH, "r") as f:
            import json as _json
            subs = _json.load(f)
    except Exception:
        subs = {}

    # Check legacy codes in config.json — honour an explicit cancelled status if present
    legacy_codes = CFG.get("premium_codes", [])
    if code in legacy_codes:
        legacy_entry = subs.get(code)
        if legacy_entry and legacy_entry.get("status") == "cancelled":
            return jsonify({"valid": False, "reason": "Legacy code revoked"})
        return jsonify({"valid": True, "tier": "legacy"})

    entry = subs.get(code)
    if not entry:
        return jsonify({"valid": False, "reason": "Code not found"})

    if entry.get("status") != "active":
        return jsonify({"valid": False, "reason": f"Subscription not active (status: {entry.get('status', 'unknown')})"})

    expires = entry.get("expires_at")
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.utcnow():
                return jsonify({"valid": False, "reason": "Subscription expired"})
        except Exception:
            pass

    # Device fingerprint check — per-code limit (default 3); reviewer codes carry their own higher limit
    devices = entry.get("devices", [])
    if fingerprint not in devices:
        if len(devices) >= entry.get("device_fingerprint_limit", 3):
            return jsonify({
                "valid": False,
                "reason": "This code is already active on the maximum number of devices. Contact support if you need help."
            })
        devices.append(fingerprint)
        entry["devices"] = devices
        # Save updated devices list back to volume
        try:
            _atomic_write_json(SUBS_PATH, subs, indent=2, log_prefix="SUBS")
        except Exception as e:
            print(f"[SUBS] Failed to save device fingerprint: {e}")

    return jsonify({"valid": True, "tier": entry.get("tier", "monthly")})


# ============================================================
# Cross-reference search
# ============================================================

@app.route("/xref-search")
def xref_search():
    q = request.args.get("q", "").strip()
    if not q:
        return render_template("xref_search.html", query="", results=[])

    xrefs = _load_sku_crossrefs()
    q_low = q.lower()

    load_embedding_cache(force=False)
    rows = _ROWS_CACHED or []
    all_skus = sorted({row[1] for row in rows if len(row) > 1 and row[1]})

    front_by_sku = {}
    any_by_sku   = {}
    for row in rows:
        sku_r = row[1] if len(row) > 1 else ""
        img_r = str(row[0]) if len(row) > 0 else ""
        if not sku_r or not img_r:
            continue
        view = VIEW_BY_IMAGE_ID.get(img_r, "")
        if sku_r not in front_by_sku and "front" in view.lower():
            front_by_sku[sku_r] = img_r
        if sku_r not in any_by_sku:
            any_by_sku[sku_r] = img_r

    scored = []
    # Build SKU -> description map from DB (for searching descriptions too)
    desc_by_sku: Dict[str, str] = {}
    for row in rows:
        sku_r = row[1] if len(row) > 1 else ""
        img_id = str(row[0]) if len(row) > 0 else ""
        if not sku_r:
            continue
        desc = DESC_BY_IMAGE_ID.get(img_id, "")
        if desc and sku_r not in desc_by_sku:
            desc_by_sku[sku_r] = desc

    for sku in all_skus:
        sku_low  = sku.lower()
        xref_entry = xrefs.get(sku, [])

        if isinstance(xref_entry, dict):
            refs = xref_entry.get("crossrefs", [])
            manufacturer = xref_entry.get("manufacturer", "")
        else:
            refs = xref_entry
            manufacturer = ""

        if q_low == sku_low:
            best_score = 98
            match_source = "sku"
        elif sku_low.startswith(q_low):
            best_score = 75
            match_source = "sku"
        elif q_low in sku_low:
            best_score = 50
            match_source = "sku"
        else:
            best_score = 0
            match_source = "none"

        mfr_low = manufacturer.lower()
        if mfr_low:
            if q_low == mfr_low:
                score = 90
            elif mfr_low.startswith(q_low) or q_low.startswith(mfr_low):
                score = 70
            elif q_low in mfr_low:
                score = 55
            else:
                score = 0
            if score > best_score:
                best_score = score
                match_source = "manufacturer"

        # Also search DB description (catches manufacturer names not in crossrefs)
        sku_desc_low = desc_by_sku.get(sku, "").lower()
        if sku_desc_low and q_low in sku_desc_low:
            desc_score = 88 if sku_desc_low.startswith(q_low) else 45
            if desc_score > best_score:
                best_score = desc_score
                match_source = "description"

        for ref in refs:
            if not isinstance(ref, dict):
                continue
            code = ref.get("code", "").lower()

            if q_low == code:
                score = 95
            elif code.startswith(q_low):
                score = 72
            elif code and q_low.startswith(code):
                score = 60
            elif q_low in code:
                score = 40
            else:
                score = 0

            if score > best_score:
                best_score = score
                match_source = "code"

        if best_score > 0:
            front_img_id = front_by_sku.get(sku) or any_by_sku.get(sku)
            scored.append({
                "sku":          sku,
                "score":        best_score,
                "match_type":   "Exact" if best_score >= 95 else "Close" if best_score >= 70 else "Partial",
                "match_source": match_source,
                "crossrefs":    refs,
                "manufacturer": manufacturer,
                "description":  desc_by_sku.get(sku, ""),
                "front_img_id": front_img_id,
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return render_template("xref_search.html", query=q, results=scored[:50])


# ============================================================
# Match
# ============================================================

def _ensure_tier_period_backfilled(code: str, sub: dict) -> bool:
    """Fetch Stripe period fields if missing from subscription record and persist.
    Returns True if a backfill write happened (caller should reload subs)."""
    if "current_period_start" in sub and "current_period_end" in sub:
        return False
    sub_id = sub.get("stripe_subscription_id")
    if not sub_id:
        return False
    try:
        import stripe as _stripe
        _stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "") or CFG.get("stripe_secret_key", "")
        stripe_sub = _stripe.Subscription.retrieve(sub_id)
        ps = stripe_sub.get("current_period_start")
        pe = stripe_sub.get("current_period_end")
        if ps and pe:
            from datetime import datetime as _dt
            sub["current_period_start"] = _dt.utcfromtimestamp(ps).isoformat()
            sub["current_period_end"]   = _dt.utcfromtimestamp(pe).isoformat()
            subs = _load_subs()
            subs[code] = sub
            _save_subs(subs)
            print(f"[TIER-BACKFILL] Persisted period fields from Stripe for code={code}", flush=True)
            return True
        print(f"[TIER-BACKFILL] Stripe response missing period fields for code={code} — using created_at fallback", flush=True)
        return False
    except Exception as _e:
        print(f"[TIER-BACKFILL] Stripe retrieval unavailable for code={code} (using created_at fallback): {_e}", flush=True)
    return False


def _evaluate_scan_decision(req, sku=None):
    """
    Compute the server fingerprint + resolve tier for the current request,
    then call db.check_and_record_scan(). Returns the decision dict.
    FAIL-OPEN: on any error returns allowed=True so a counter failure
    never blocks /match.

    sku: matched card SKU, if known — forwarded to check_and_record_scan
        for the 60s per-SKU quota dedupe.
    """
    try:
        import db as _db
        import urllib.parse as _up
        ua   = req.user_agent.string
        lang = req.headers.get("Accept-Language", "")
        addr = req.headers.get("CF-Connecting-IP",
               req.headers.get("X-Forwarded-For", req.remote_addr or ""))
        server_fp = _db.compute_server_fingerprint(ua, lang, addr)
        device_id = req.cookies.get("matchit_device_id_v1") or None
        code_raw  = req.cookies.get("gs_access_code", "")
        code      = _up.unquote(code_raw).strip().upper() or None
        subs      = _load_subs()
        tier      = _db.resolve_tier_from_code(code, CFG.get("premium_codes", []), subs)
        # Lazy backfill: ensure period fields exist for capped tiers before gate runs
        if code and tier in ("monthly", "annual"):
            sub = subs.get(code, {})
            if _ensure_tier_period_backfilled(code, sub):
                subs = _load_subs()   # reload after write
        result = _db.check_and_record_scan(server_fp, device_id, tier,
                                          code=code, subscriptions_obj=subs, sku=sku)
        # Detect first-time tier → top-up transition and set flag
        if (result.get("reason") == "topup_consumed_premium"
                and code and tier in ("monthly", "annual")):
            try:
                _sub_record = subs.get(code, {})
                if not _sub_record.get("tier_transition_warned", False):
                    _sub_record["tier_transition_warned"] = True
                    subs[code] = _sub_record
                    _save_subs(subs)
                    result["show_transition_toast"] = True
                    print(f"[TIER-TRANSITION] First top-up consumed for code={code} this period", flush=True)
                else:
                    result["show_transition_toast"] = False
            except Exception as _te:
                print(f"[TIER-TRANSITION] Failed to persist flag for code={code}: {_te}", flush=True)
                result["show_transition_toast"] = False
        else:
            result["show_transition_toast"] = False
        result["tier"] = tier
        return result
    except Exception as _exc:
        app.logger.warning(f"[SCAN-DECISION] error (fail-open): {_exc}")
        return {"ok": False, "allowed": True, "counted": False,
                "reason": "error", "error": str(_exc), "show_transition_toast": False}


@app.route("/app/")
def app_slash():
    return redirect(url_for("match"), code=308)


@app.route("/match", methods=["GET", "POST"])
def match():
    if request.method == "GET":
        from flask import make_response
        resp = make_response(render_template("match.html"))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    TOP_K_SKU = int(CFG.get("top_k_sku", 20))
    TOP_M_PER_SKU = int(CFG.get("top_m_per_sku", 3))
    CAP_PER_SKU = int(CFG.get("cap_per_sku", 30))

    softmax_temp = float(CFG.get("softmax_temp", 0.015))
    low_cert_prob = float(CFG.get("low_cert_prob", 0.55))
    low_cert_prob_gap = float(CFG.get("low_cert_prob_gap", 0.15))

    cons_n = int(CFG.get("consistency_n", 5))
    cons_sigma = float(CFG.get("consistency_sigma", 0.040))

    # Read groove counts from UI (default -1 = unknown / not provided)
    query_category = request.form.get("key_type", "").strip().upper()
    query_profile = parse_all_fields(dict(request.form))

    import time as _time
    _t_total_start = _time.time()

    if not request.files:
        return render_template("match.html", error="No file uploaded.")

    def _pick_first_file(key: str):
        try:
            items = request.files.getlist(key)
        except Exception:
            items = []
        for f in items:
            if f and getattr(f, "filename", ""):
                return f
        return None

    up1 = _pick_first_file("query_image") or _pick_first_file("query")
    up2 = _pick_first_file("query_image_2")

    if up1 is None:
        return render_template("match.html", error="No file selected.")

    query_id = str(uuid.uuid4())
    import os
    _localappdata = os.environ.get("LOCALAPPDATA", "")
    if _localappdata == "/modal_data":
        query_dir = Path("/modal_data") / "query"
    else:
        query_dir = Path(app.root_path) / "static" / "query"
    query_dir.mkdir(parents=True, exist_ok=True)

    query_filename = f"{query_id}.jpg"
    query_path1 = query_dir / query_filename

    try:
        up1.save(str(query_path1))
        normalize_uploaded_image(str(query_path1))
    except Exception as e:
        current_app.logger.exception("Failed to save query image")
        return render_template("match.html", error=f"Failed to save query image: {e}")

    import base64
    with open(str(query_path1), "rb") as f:
        query_image_b64 = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()

    query_filename2 = None
    query_path2 = None
    if up2 is not None and getattr(up2, "filename", ""):
        query_filename2 = f"{query_id}_2.jpg"
        query_path2 = query_dir / query_filename2
        try:
            up2.save(str(query_path2))
            normalize_uploaded_image(str(query_path2))
        except Exception:
            query_filename2 = None
            query_path2 = None

    # ── Confirmed-SKU short-circuit — client OCR already identified the card ──
    # Fires on the confirmed_sku form field for ALL games, independent of key_type.
    confirmed_sku = request.form.get("confirmed_sku", "").strip()
    if confirmed_sku:
        try:
            import uuid as _uuid
            feedback_token = str(_uuid.uuid4())
            session["feedback_token"] = feedback_token
            session["feedback_q1"] = query_filename
            session["feedback_q2"] = query_filename2
            session["feedback_skus"] = [confirmed_sku]
            _cs_img = _image_id_for_sku(confirmed_sku)
            _confirmed_results = [{"sku": confirmed_sku, "score": 1.0, "similarity": 1.0, "prob": 1.0, "rank": 1, "ocr_promoted": True, "image_id_front": _cs_img, "image_id": _cs_img}]
            try:
                from vertical_loader import get_vertical as _gv2, get_db_root as _gdr2
                _v2 = _gv2()
                _vid2 = _v2.get("id", "")
                _cp2 = {}
                _cx2 = {}
                _cpp2 = os.path.join(app.root_path, f"sku_profiles_{_vid2}.json")
                if not os.path.exists(_cpp2):
                    _cpp2 = os.path.join(app.root_path, "sku_profiles.json")
                if os.path.exists(_cpp2):
                    with open(_cpp2, "r", encoding="utf-8") as _f2:
                        _cp2 = json.load(_f2)
                _cxp2 = os.path.join(app.root_path, f"sku_crossrefs_{_vid2}.json")
                if not os.path.exists(_cxp2):
                    _cxp2 = os.path.join(app.root_path, "sku_crossrefs.json")
                if os.path.exists(_cxp2):
                    with open(_cxp2, "r", encoding="utf-8") as _f2:
                        _cx2 = json.load(_f2)
                _dbr2 = _gdr2()
                _prof2 = {}
                if confirmed_sku in _cp2:
                    _prof2.update(_cp2[confirmed_sku])
                if confirmed_sku in _cx2:
                    _xr2 = _cx2[confirmed_sku]
                    if _xr2.get("manufacturer"):
                        _prof2["manufacturer"] = _xr2["manufacturer"]
                    if _xr2.get("crossrefs"):
                        _prof2["crossrefs"] = _xr2["crossrefs"]
                if not _prof2 and _dbr2:
                    _prof2 = _load_card_profile_for_sku(confirmed_sku, _dbr2, get_data_dir())
                _confirmed_results[0]["profile"] = _prof2
            except Exception as _pe:
                print(f"[CONFIRMED-SKU] profile enrich error: {_pe}", flush=True)
            grade = _safe_grade(str(query_path1))
            scan_decision = _evaluate_scan_decision(request, sku=confirmed_sku)
            if not scan_decision.get("allowed"):
                return jsonify({
                    "error": "free_scan_limit_reached",
                    "limit": scan_decision.get("limit"),
                    "count": scan_decision.get("count"),
                    "remaining": 0,
                    "tier": scan_decision.get("tier"),
                    "message": "You've used all 150 free scans this month.",
                }), 402
            _save_match_history(query_filename, query_filename2, _confirmed_results, False)
            _increment_scan_counter()
            _increment_sku_scan_freq(confirmed_sku)
            print(f"[CONFIRMED-SKU] Rich render, no CLIP: {confirmed_sku}", flush=True)
            return render_template(
                "results.html",
                results=_confirmed_results,
                query_filename=query_filename,
                query_filename_2=query_filename2,
                low_cert=False,
                feedback_token=feedback_token,
                query_image_b64=query_image_b64,
                ocr_status="ocr_direct_first",
                ocr_sku=confirmed_sku,
                grade=grade,
                paywall_triggered=scan_decision.get("limit_just_crossed", False),
                scans_remaining=scan_decision.get("remaining"),
            )
        except Exception as _e:
            print(f"[CONFIRMED-SKU] error, falling through to CLIP: {_e}", flush=True)

    # ── OCR-first for YGO and MTG ──
    if query_category in ("YUGIOH", "MTG"):
        try:
            _ocr_direct_sku = ocr_direct_lookup(str(query_path1), query_category)
            if _ocr_direct_sku:
                import uuid as _uuid
                feedback_token = str(_uuid.uuid4())
                session["feedback_token"] = feedback_token
                session["feedback_q1"] = query_filename
                session["feedback_q2"] = query_filename2
                session["feedback_skus"] = [_ocr_direct_sku]
                _ocr_direct_img = _image_id_for_sku(_ocr_direct_sku)
                _ocr_direct_results = [{"sku": _ocr_direct_sku, "score": 1.0, "similarity": 1.0, "prob": 1.0, "rank": 1, "ocr_promoted": True, "image_id_front": _ocr_direct_img, "image_id": _ocr_direct_img}]
                try:
                    from vertical_loader import get_vertical as _gv1, get_db_root as _gdr1
                    _v1 = _gv1()
                    _vid1 = _v1.get("id", "")
                    _cp1 = {}
                    _cx1 = {}
                    _cpp1 = os.path.join(app.root_path, f"sku_profiles_{_vid1}.json")
                    if not os.path.exists(_cpp1):
                        _cpp1 = os.path.join(app.root_path, "sku_profiles.json")
                    if os.path.exists(_cpp1):
                        with open(_cpp1, "r", encoding="utf-8") as _f1:
                            _cp1 = json.load(_f1)
                    _cxp1 = os.path.join(app.root_path, f"sku_crossrefs_{_vid1}.json")
                    if not os.path.exists(_cxp1):
                        _cxp1 = os.path.join(app.root_path, "sku_crossrefs.json")
                    if os.path.exists(_cxp1):
                        with open(_cxp1, "r", encoding="utf-8") as _f1:
                            _cx1 = json.load(_f1)
                    _dbr1 = _gdr1()
                    _prof1 = {}
                    if _ocr_direct_sku in _cp1:
                        _prof1.update(_cp1[_ocr_direct_sku])
                    if _ocr_direct_sku in _cx1:
                        _xr1 = _cx1[_ocr_direct_sku]
                        if _xr1.get("manufacturer"):
                            _prof1["manufacturer"] = _xr1["manufacturer"]
                        if _xr1.get("crossrefs"):
                            _prof1["crossrefs"] = _xr1["crossrefs"]
                    if not _prof1 and _dbr1:
                        _prof1 = _load_card_profile_for_sku(_ocr_direct_sku, _dbr1, get_data_dir())
                    _ocr_direct_results[0]["profile"] = _prof1
                except Exception as _pe:
                    print(f"[OCR-FIRST] profile enrich error: {_pe}", flush=True)
                grade = _safe_grade(str(query_path1))
                scan_decision = _evaluate_scan_decision(request, sku=_ocr_direct_sku)
                if not scan_decision.get("allowed"):
                    return jsonify({
                        "error": "free_scan_limit_reached",
                        "limit": scan_decision.get("limit"),
                        "count": scan_decision.get("count"),
                        "remaining": 0,
                        "tier": scan_decision.get("tier"),
                        "message": "You've used all 150 free scans this month.",
                    }), 402
                _save_match_history(query_filename, query_filename2, _ocr_direct_results, False)
                _increment_scan_counter()
                print(f"[OCR-FIRST] Skipped CLIP — direct match: {_ocr_direct_sku}", flush=True)
                return render_template(
                    "results.html",
                    results=_ocr_direct_results,
                    query_filename=query_filename,
                    query_filename_2=query_filename2,
                    low_cert=False,
                    feedback_token=feedback_token,
                    query_image_b64=query_image_b64,
                    ocr_status="ocr_direct_first",
                    ocr_sku=_ocr_direct_sku,
                    grade=grade,
                    paywall_triggered=scan_decision.get("limit_just_crossed", False),
                    scans_remaining=scan_decision.get("remaining"),
                )
        except Exception as _e:
            print(f"[OCR-FIRST] error, falling through to CLIP: {_e}", flush=True)

    _t_save = _time.time()

    # ── Clean images + auto groove detect ──
    clean1, clean2, auto_fg, auto_bg, _clean_diag = (
        _clean_and_auto_grooves(
            str(query_path1),
            str(query_path2) if query_path2 else None,
        )
    )

    _t_clean = _time.time()

    try:
        emb = get_embedder()
    except Exception as e:
        current_app.logger.warning(f"Embedder init failed: {e}")
        return render_template("match.html", error="Embedder not available.")

    if emb is None or not hasattr(emb, "embed_path"):
        return render_template("match.html", error="Embedder not available.")

    from vertical_loader import get_vertical as _gv
    _vert = _gv()
    params = dict(
        multi_crop=bool(_vert.get("multi_crop", CFG.get("auto_mode_b_multi_crop", True))),
        suppress_bg=bool(_vert.get("suppress_bg", CFG.get("auto_mode_b_suppress_bg", True))),
        max_side=int(_vert.get("max_side", CFG.get("auto_mode_b_max_side", 1024))),
    )

    # PERF: Back image only contributes 3% weight — single-crop saves ~2s
    params_back = dict(
        multi_crop=False,
        suppress_bg=bool(CFG.get("auto_mode_b_suppress_bg", True)),
        max_side=int(CFG.get("auto_mode_b_max_side", 1024)),
    )

    try:
        # Embed from ORIGINALS — rembg cleaning is only for groove detection.
        _t_emb_start = _time.time()
        qf = _embed_one_query(emb, str(query_path1), **params)
        _t_emb_front = _time.time()

        qb = None
        if query_path2 is not None and query_path2.exists():
            qb = _embed_one_query(emb, str(query_path2), **params_back)
        _t_emb_back = _time.time()

        jp_mode = request.form.get('jp_mode', 'en')
        exclude_jpn = (jp_mode != 'jp')
        # Read allowed JP sets from the request (sent by client after denom check)
        _allowed_sets_raw = request.form.get('allowed_jpn_sets', '').strip()
        _allowed_jpn_sets = set(_allowed_sets_raw.split(',')) if _allowed_sets_raw else None

        results, low_cert, _diag = _run_match_paired_two_stage(
            qf,
            qb,
            query_front_path=str(query_path1),
            query_back_path=str(query_path2) if (query_path2 is not None and query_path2.exists()) else None,
            top_k_sku=TOP_K_SKU,
            top_m_per_sku=TOP_M_PER_SKU,
            cap_per_sku=CAP_PER_SKU,
            softmax_temp=softmax_temp,
            low_cert_prob=low_cert_prob,
            low_cert_prob_gap=low_cert_prob_gap,
            cons_n=cons_n,
            cons_sigma=cons_sigma,
            query_category=query_category,
            query_profile=query_profile,
            auto_front_grooves=auto_fg,
            auto_back_grooves=auto_bg,
            exclude_jpn=exclude_jpn,
            allowed_jpn_sets=_allowed_jpn_sets,
        )
        _t_match = _time.time()

    except Exception as e:
        current_app.logger.exception("Embedding/matching failed")
        return render_template("match.html", error=f"Embedder failed: {e}")

    # DINOv2 tie-breaker on top 2 if scores are close
    _t_dino_start = _time.time()
    _dino_diag = {"fired": False, "reason": "not_run"}
    if results:
        results, _dino_diag = _dinov2_tiebreak(results, str(query_path1))
    _t_dino_end = _time.time()
    if isinstance(_diag, dict):
        _diag["dino"] = _dino_diag

    # ── TIMING SUMMARY ──
    _t_total = _t_dino_end - _t_total_start
    print(f"\n{'='*60}", flush=True)
    print(f"[TIMING] save+normalize:  {_t_save - _t_total_start:.2f}s", flush=True)
    print(f"[TIMING] clean+groove:    {_t_clean - _t_save:.2f}s", flush=True)
    print(f"[TIMING] embed FRONT:     {_t_emb_front - _t_emb_start:.2f}s", flush=True)
    print(f"[TIMING] embed BACK:      {_t_emb_back - _t_emb_front:.2f}s", flush=True)
    print(f"[TIMING] matching engine: {_t_match - _t_emb_back:.2f}s", flush=True)
    print(f"[TIMING] DINOv2 tiebreak: {_t_dino_end - _t_dino_start:.2f}s", flush=True)
    print(f"[TIMING] ── TOTAL:        {_t_total:.2f}s ──", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Early score gate — bail BEFORE the OCR block so low-confidence junk frames
    # (empty frames, blurry hands, non-cards) never trigger paid Google Vision calls.
    # Mirrors the pre-OCR gate already in api_routes.py. The existing post-OCR gate
    # at ~line 6561 stays as a backstop.
    if not results or (results[0].get('score', 0) if isinstance(results[0], dict) else getattr(results[0], 'score', 0)) < 0.65:
        return render_template("match.html", error="No confident match found.")

    import uuid as _uuid
    feedback_token = str(_uuid.uuid4())
    session["feedback_token"] = feedback_token
    session["feedback_q1"] = query_filename
    session["feedback_q2"] = query_filename2
    session["feedback_skus"] = [r["sku"] if isinstance(r, dict) else r.sku for r in results]

    # OCR set-code confirmation — promotes correct print to rank 1
    if results:
        # Auto-detect TCG from top SKU prefix when category not supplied,
        # so OCR runs only the detected vertical's regions instead of
        # brute-forcing all three (YGO + Pokemon + MTG).
            _effective_tcg = query_category
            if not _effective_tcg:
                _top_sku = results[0].get("sku", "") if isinstance(results[0], dict) else getattr(results[0], "sku", "")
                if _top_sku.startswith("ygo-"):
                    _effective_tcg = "YUGIOH"
                elif _top_sku.startswith("mtg-"):
                    _effective_tcg = "MTG"
                else:
                    _effective_tcg = "POKEMON"
                print(f"[OCR] Auto-detected TCG from CLIP top match: {_effective_tcg} (sku={_top_sku})", flush=True)
            results, _ocr_info = ocr_confirm_ranking(
                results,
                str(query_path1),
                _effective_tcg,
                search_depth=10,
                set_metadata=_load_set_metadata(),
                jpn_mode=(jp_mode == 'jp'),
            )
            # Language separation post-filter — enforce strict JP/EN split.
            # The pre-filter removes jpn- cards from the CLIP pool in EN mode,
            # but this safety net catches any that slip through, and also enforces
            # that JP mode never returns EN results.
            _JP_SCORE_FLOOR = 0.72  # Visual-only floor — no OCR fallback for JP
            _before_lang_filter = len(results)
            if jp_mode == 'jp':
                results = [r for r in results if r.get('sku', '').startswith('jpn-')]
                app.logger.info(f"[LANG-FILTER] JP mode: {_before_lang_filter} → {len(results)} results (EN stripped)")
                if results and results[0].get('score', 0) < _JP_SCORE_FLOOR:
                    app.logger.info(f"[JP-FLOOR] Top score {results[0].get('score', 0):.3f} < {_JP_SCORE_FLOOR} — clearing JP results")
                    results = []
            else:
                results = [r for r in results if not r.get('sku', '').startswith('jpn-')]
                app.logger.info(f"[LANG-FILTER] EN mode: {_before_lang_filter} → {len(results)} results (JP stripped)")
            print(
                f"[OCR] status={_ocr_info.get('ocr_status')} "
                f"extracted={_ocr_info.get('extracted')} "
                f"matched={_ocr_info.get('matched_sku')} "
                f"promoted={_ocr_info.get('promoted')} "
                f"confidence={_ocr_info.get('ocr_confidence', 0.0):.2f} "
                f"high_confidence={_ocr_info.get('high_confidence', False)}",
                flush=True,
            )
            # High confidence OCR match — enrich profile then return immediately
            if _ocr_info.get("high_confidence") and results:
                try:
                    from vertical_loader import get_vertical as _gv2, get_db_root as _gdr2
                    _v2 = _gv2()
                    _vid2 = _v2.get("id", "")
                    _cp2 = {}
                    _cx2 = {}
                    _cpp2 = os.path.join(app.root_path, f"sku_profiles_{_vid2}.json")
                    if not os.path.exists(_cpp2):
                        _cpp2 = os.path.join(app.root_path, "sku_profiles.json")
                    if os.path.exists(_cpp2):
                        with open(_cpp2, "r", encoding="utf-8") as _f2:
                            _cp2 = json.load(_f2)
                    _cxp2 = os.path.join(app.root_path, f"sku_crossrefs_{_vid2}.json")
                    if not os.path.exists(_cxp2):
                        _cxp2 = os.path.join(app.root_path, "sku_crossrefs.json")
                    if os.path.exists(_cxp2):
                        with open(_cxp2, "r", encoding="utf-8") as _f2:
                            _cx2 = json.load(_f2)
                    _dbr2 = _gdr2()
                    for _r2 in results:
                        _sku2 = _r2.get("sku", "") if isinstance(_r2, dict) else getattr(_r2, "sku", "")
                        if not _sku2:
                            continue
                        _prof2 = {}
                        if _sku2 in _cp2:
                            _prof2.update(_cp2[_sku2])
                        if _sku2 in _cx2:
                            _xr2 = _cx2[_sku2]
                            if _xr2.get("manufacturer"):
                                _prof2["manufacturer"] = _xr2["manufacturer"]
                            if _xr2.get("crossrefs"):
                                _prof2["crossrefs"] = _xr2["crossrefs"]
                        if not _prof2 and _dbr2:
                            _prof2 = _load_card_profile_for_sku(_sku2, _dbr2, get_data_dir())
                        if isinstance(_r2, dict):
                            _r2["profile"] = _prof2
                        else:
                            _r2.profile = _prof2
                except Exception as _pe:
                    print(f"[OCR-HI] profile enrich error: {_pe}", flush=True)
                _sku_for_charge = _ocr_info.get("matched_sku") or (
                    results[0].get("sku", "") if isinstance(results[0], dict) else getattr(results[0], "sku", "")
                )
                scan_decision = _evaluate_scan_decision(request, sku=_sku_for_charge)
                if not scan_decision.get("allowed"):
                    return jsonify({
                        "error": "free_scan_limit_reached",
                        "limit": scan_decision.get("limit"),
                        "count": scan_decision.get("count"),
                        "remaining": 0,
                        "tier": scan_decision.get("tier"),
                        "message": "You've used all 150 free scans this month.",
                    }), 402
                _save_match_history(query_filename, query_filename2, results, low_cert)
                _increment_scan_counter()
                grade = _safe_grade(str(query_path1))
                return render_template(
                    "results.html",
                    results=results,
                    query_filename=query_filename,
                    query_filename_2=query_filename2,
                    low_cert=low_cert,
                    feedback_token=feedback_token,
                    query_image_b64=query_image_b64,
                    ocr_status=_ocr_info.get("ocr_status", ""),
                    ocr_sku=_ocr_info.get("matched_sku", ""),
                    grade=grade,
                    paywall_triggered=scan_decision.get("limit_just_crossed", False),
                    scans_remaining=scan_decision.get("remaining"),
                )

    if not results:
        return render_template(
            "match.html",
            error="Embedding cache not loaded or empty. Try Admin -> Refresh cache, or restart the server.",
        )

    # Score gate: only charge on confident match (Option B)
    if results[0].get('score', 0) < 0.65:
        return render_template("match.html", error="No confident match found.")

    # Option B (restored): only charge quota when OCR confirmed the match
    _ocr_status_fb = _ocr_info.get("ocr_status", "not_run") if "_ocr_info" in dir() else "not_run"
    _ocr_confirmed_fb = _ocr_status_fb in ("rank1_confirmed", "direct_lookup", "promoted")

    if not _ocr_confirmed_fb:
        # CLIP matched but OCR did not confirm — show result, do not charge quota or stats
        grade = _safe_grade(str(query_path1))
        return render_template(
            "results.html",
            results=results,
            query_filename=query_filename,
            query_filename_2=query_filename2,
            low_cert=low_cert,
            feedback_token=feedback_token,
            query_image_b64=query_image_b64,
            ocr_status=_ocr_status_fb,
            ocr_sku="",
            grade=grade,
            paywall_triggered=False,
            scans_remaining=None,
        )

    # OCR confirmed — enforce scan limit, save history, charge counter
    _sku_for_charge_fb = _ocr_info.get("matched_sku") or (
        results[0].get("sku", "") if isinstance(results[0], dict) else getattr(results[0], "sku", "")
    )
    scan_decision = _evaluate_scan_decision(request, sku=_sku_for_charge_fb)
    if not scan_decision.get("allowed"):
        return jsonify({
            "error": "free_scan_limit_reached",
            "limit": scan_decision.get("limit"),
            "count": scan_decision.get("count"),
            "remaining": 0,
            "tier": scan_decision.get("tier"),
            "message": "You've used all 150 free scans this month.",
        }), 402

    # Save to match history
    _save_match_history(query_filename, query_filename2, results, low_cert)

    # ── Enrich results with profile data ──
    from vertical_loader import get_vertical, get_db_root
    v = get_vertical()
    vertical_id = v.get("id", "")

    # Load centralized profiles (keys pattern)
    central_profiles = {}
    central_xrefs = {}
    central_profile_path = os.path.join(app.root_path, f"sku_profiles_{vertical_id}.json")
    if not os.path.exists(central_profile_path):
        central_profile_path = os.path.join(app.root_path, "sku_profiles.json")
    if os.path.exists(central_profile_path):
        try:
            with open(central_profile_path, "r", encoding="utf-8") as f:
                central_profiles = json.load(f)
        except Exception:
            pass

    central_xref_path = os.path.join(app.root_path, f"sku_crossrefs_{vertical_id}.json")
    if not os.path.exists(central_xref_path):
        central_xref_path = os.path.join(app.root_path, "sku_crossrefs.json")
    if os.path.exists(central_xref_path):
        try:
            with open(central_xref_path, "r", encoding="utf-8") as f:
                central_xrefs = json.load(f)
        except Exception:
            pass

    db_root = get_db_root()
    for r in results:
        sku = r.get("sku", "") if isinstance(r, dict) else getattr(r, "sku", "")
        if not sku:
            continue

        profile = {}

        # Check centralized files
        if sku in central_profiles:
            profile.update(central_profiles[sku])
        if sku in central_xrefs:
            xref = central_xrefs[sku]
            if xref.get("manufacturer"):
                profile["manufacturer"] = xref["manufacturer"]
            if xref.get("crossrefs"):
                profile["crossrefs"] = xref["crossrefs"]

        if not profile:
            profile = _load_card_profile_for_sku(sku, db_root, get_data_dir())

        if isinstance(r, dict):
            r["profile"] = profile
        else:
            r.profile = profile

    # Record price history for top result
    try:
        _r0 = results[0]
        _r0_sku = _r0.get("sku") if isinstance(_r0, dict) else getattr(_r0, "sku", None)
        _r0_profile = _r0.get("profile", {}) if isinstance(_r0, dict) else getattr(_r0, "profile", {})
        _record_price(_r0_sku, _extract_gbp_from_profile(_r0_profile))
    except Exception:
        pass
    _increment_scan_counter()
    # Per-sku popularity counter — OCR-confirmed here (early return above if not).
    # Guarded inside the helper; never affects the scan result.
    _increment_sku_scan_freq(_r0_sku if "_r0_sku" in dir() else None)

    # Rule-based condition grade from query image
    grade = _safe_grade(str(query_path1))

    return render_template(
        "results.html",
        results=results,
        query_filename=query_filename,
        query_filename_2=query_filename2,      # fix: add underscore to match template
        low_cert=low_cert,
        feedback_token=feedback_token,
        query_image_b64=query_image_b64,
        ocr_status=_ocr_info.get("ocr_status", "") if "_ocr_info" in dir() else "",
        ocr_sku=_ocr_info.get("matched_sku", "") if "_ocr_info" in dir() else "",
        grade=grade,
        paywall_triggered=scan_decision.get("limit_just_crossed", False),
        scans_remaining=scan_decision.get("remaining"),
    )


# ============================================================
# Run local dev server
# ============================================================

if __name__ == "__main__":
    try:
        host = CFG.get("host", "0.0.0.0")
        port = int(CFG.get("port", 5000))
    except Exception:
        host = "0.0.0.0"
        port = 5000

    # ── PERF: Background-preload primary CLIP embedder (~22s on CPU) ──
    print("[STARTUP] Starting CLIP background preload...", flush=True)
    threading.Thread(target=_preload_primary_embedder, daemon=True).start()

    # ── PERF: Background-preload DINOv2 if enabled ──
    if bool(CFG.get("dinov2_tiebreak_enabled", True)) and bool(CFG.get("dinov2_tiebreak_preload", True)):
        print("[STARTUP] Starting DINOv2 background preload...", flush=True)
        threading.Thread(target=_preload_tiebreak_embedder, daemon=True).start()

    # ── PERF: Preload embedding cache so first query doesn't wait for DB read ──
    try:
        with app.app_context():
            load_embedding_cache(force=True)
            n = len(_ROWS_CACHED or [])
            print(f"[STARTUP] Embedding cache preloaded: {n} rows", flush=True)
    except Exception as e:
        print(f"[STARTUP] Cache preload skipped: {e}", flush=True)

    app.run(host=host, port=port, debug=False, use_reloader=False)