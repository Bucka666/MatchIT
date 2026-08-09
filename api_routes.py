"""
api_routes.py — REST API for MatchIT visual identification engine
=================================================================
Drop-in module that adds /api/v1/ endpoints to your Flask app.

Integration (add to app.py):
    from api_routes import register_api_routes
    register_api_routes(app)

Endpoints:
    POST /api/v1/match        — upload image, get top-N matches as JSON
    GET  /api/v1/health       — check API status
    GET  /api/v1/vertical     — current vertical info (categories, fields)

Authentication:
    All endpoints require header:  X-API-Key: <key>
    Keys are stored in config.json under "api_keys": {"client_name": "key_value"}
"""



import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, "/app")

logger = logging.getLogger(__name__)
from functools import wraps
from pathlib import Path
from typing import Optional

import numpy as np
from flask import abort, current_app, jsonify, request, send_file


def register_api_routes(app):
    """Register all API routes on the Flask app."""

    # ─────────────────────────────────────────
    # Auth middleware
    # ─────────────────────────────────────────

    def _get_api_keys():
        """Load API keys. Prefers env var (Modal secret), falls back to CFG for dev."""
        import os, json as _json
        env_val = os.environ.get("GRAILSWEEP_API_KEYS", "").strip()
        if env_val:
            try:
                parsed = _json.loads(env_val)
                if isinstance(parsed, dict) and parsed:
                    return parsed
            except Exception as e:
                print(f"[API-KEYS] Failed to parse GRAILSWEEP_API_KEYS env var: {e}", flush=True)
        # Dev / fallback path
        from app import CFG
        keys = CFG.get("api_keys", {})
        if not keys:
            single = CFG.get("api_key", "")
            if single:
                keys = {"default": single}
        return keys

    def api_key_required(f):
        """Decorator to enforce API key authentication."""
        @wraps(f)
        def decorated(*args, **kwargs):
            api_key = (request.headers.get("X-API-Key", "") or request.args.get("api_key", "") or request.form.get("api_key", "")).strip()
            if not api_key:
                return jsonify({"error": "Missing X-API-Key header"}), 401

            valid_keys = _get_api_keys()
            if not valid_keys:
                return jsonify({"error": "No API keys configured on server"}), 500

            # Find the client name for this key
            client_name = None
            for name, key in valid_keys.items():
                if key == api_key:
                    client_name = name
                    break

            if client_name is None:
                return jsonify({"error": "Invalid API key"}), 403

            # Attach client name to request context
            request._api_client = client_name
            return f(*args, **kwargs)
        return decorated

    # ─────────────────────────────────────────
    # Health check
    # ─────────────────────────────────────────

    @app.route("/api/v1/health", methods=["GET"])
    def api_health():
        from app import get_cached_rows, FRONT_MATRIX
        from vertical_loader import get_vertical

        v = get_vertical()
        rows = get_cached_rows(force=False)
        front_count = FRONT_MATRIX.shape[0] if FRONT_MATRIX is not None else 0

        return jsonify({
            "status": "ok",
            "vertical": v.get("id", "unknown"),
            "vertical_name": v.get("name", "Unknown"),
            "total_images": len(rows) if rows else 0,
            "front_embeddings": front_count,
            "engine": "matchit-api-v1",
        })
    @app.route("/api/v1/switch_vertical", methods=["POST"])
    @api_key_required
    def api_switch_vertical():
        """Switch the active vertical and reload caches."""
        import json as _json
        from vertical_loader import load_vertical
        from app import CFG, load_embedding_cache

        new_vertical = request.form.get("vertical", "").strip()
        if not new_vertical:
            return jsonify({"error": "No vertical specified"}), 400

        # Check vertical exists
        vertical_path = os.path.join(app.root_path, "verticals", new_vertical, "vertical.json")
        if not os.path.exists(vertical_path):
            return jsonify({"error": f"Vertical '{new_vertical}' not found"}), 404

        # Update config
        config_path = os.path.join(app.root_path, "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = _json.load(f)
        config["vertical"] = new_vertical
        with open(config_path, "w", encoding="utf-8") as f:
            _json.dump(config, f, indent=2, ensure_ascii=False)

        # Reload vertical
        CFG["vertical"] = new_vertical
        load_vertical(new_vertical, app.root_path)

        # Reload embedding cache
        load_embedding_cache(force=True)

        # Get new state
        from app import FRONT_MATRIX, get_cached_rows
        from vertical_loader import get_vertical
        v = get_vertical()
        rows = get_cached_rows(force=False)

        return jsonify({
            "status": "switched",
            "vertical": v.get("id", ""),
            "vertical_name": v.get("name", ""),
            "total_images": len(rows) if rows else 0,
            "front_embeddings": FRONT_MATRIX.shape[0] if FRONT_MATRIX is not None else 0,
        })

    @app.route("/api/v1/verticals", methods=["GET"])
    def api_list_verticals():
        """List all available verticals."""
        verticals_dir = os.path.join(app.root_path, "verticals")
        result = []
        if os.path.isdir(verticals_dir):
            for d in sorted(os.listdir(verticals_dir)):
                vpath = os.path.join(verticals_dir, d, "vertical.json")
                if os.path.exists(vpath):
                    try:
                        import json as _json
                        with open(vpath, "r", encoding="utf-8") as f:
                            vc = _json.load(f)
                        result.append({
                            "id": vc.get("id", d),
                            "name": vc.get("name", d),
                            "icon": vc.get("icon", ""),
                        })
                    except Exception:
                        result.append({"id": d, "name": d, "icon": ""})
        return jsonify({"verticals": result})
    # ─────────────────────────────────────────
    # Vertical info
    # ─────────────────────────────────────────

    @app.route("/api/v1/vertical", methods=["GET"])
    @api_key_required
    def api_vertical_info():
        from vertical_loader import (
            get_vertical, get_categories, get_category_list, get_field_defs,
        )

        v = get_vertical()
        return jsonify({
            "id": v.get("id", ""),
            "name": v.get("name", ""),
            "categories": get_category_list(),
            "profile_fields": [
                {
                    "id": f["id"],
                    "label": f.get("label", f["id"]),
                    "type": f.get("type", "select"),
                    "options": f.get("options", []),
                    "default": f.get("default", ""),
                }
                for f in get_field_defs()
            ],
        })

    # ─────────────────────────────────────────
    # Match endpoint
    # ─────────────────────────────────────────

    @app.route("/api/v1/match", methods=["POST"])
    @api_key_required
    def api_match():
        """
        Upload an image, get back top-N matches with scores.

        Request:
            - multipart/form-data with field "image" (required)
            - optional field "image_back" (second image)
            - optional field "category" (e.g. "CYLINDER", "POKEMON")
            - optional fields matching profile field IDs (e.g. "rarity", "energy_type")
            - optional field "top_k" (default 5, max 20)

        Response:
            {
                "matches": [
                    {
                        "rank": 1,
                        "sku": "base1-4",
                        "score": 0.943,
                        "probability": 0.87,
                        "profile": { ... },
                        "images": [ ... ]
                    },
                    ...
                ],
                "low_confidence": false,
                "query_id": "uuid",
                "timing_ms": 1234,
                "diagnostics": { ... }
            }
        """
        t_start = time.time()

        from app import (
            CFG, get_embedder, normalize_uploaded_image,
            _embed_one_query, _run_match_paired_two_stage,
            _dinov2_tiebreak, load_embedding_cache,
            _clean_and_auto_grooves,
            _evaluate_scan_decision,
            _image_id_for_sku, _is_set_imaged,
            get_data_dir, _increment_scan_counter,
        )
        from ocr_confirm import ocr_direct_lookup, _denominator_blocks_promotion
        from vertical_loader import parse_all_fields, get_vertical

        # ── Parse parameters ──
        top_k = min(int(request.form.get("top_k", 5)), 20)
        query_category = request.form.get("category", "").strip().upper()
        # Needed early for the pre-CLIP OCR-first block below (jp_mode is
        # read again later, closer to the match call, for the CLIP-side
        # JP filter — that later read is unaffected by this one).
        _early_jp_mode = request.form.get('jp_mode', 'en')

        # Parse profile fields from form data
        query_profile = parse_all_fields(dict(request.form))

        # ── Handle image upload ──
        image_file = request.files.get("image")
        if image_file is None or not getattr(image_file, "filename", ""):
            return jsonify({"error": "No image provided. Send as multipart field 'image'"}), 400

        image_back_file = request.files.get("image_back")

        query_id = str(uuid.uuid4())
        query_dir = Path(app.root_path) / "static" / "query"
        query_dir.mkdir(parents=True, exist_ok=True)

        # Save front image
        query_path1 = query_dir / f"api_{query_id}.jpg"
        try:
            image_file.save(str(query_path1))
            normalize_uploaded_image(str(query_path1))
        except Exception as e:
            return jsonify({"error": f"Failed to process image: {e}"}), 400

        # Save back image (optional)
        query_path2 = None
        if image_back_file and getattr(image_back_file, "filename", ""):
            query_path2 = query_dir / f"api_{query_id}_back.jpg"
            try:
                image_back_file.save(str(query_path2))
                normalize_uploaded_image(str(query_path2))
            except Exception:
                query_path2 = None

        # ── OCR-first for YGO, MTG, and Pokémon — mirrors app.py's /match ──
        # Runs before CLIP. On a confident read with a DB row, returns a
        # match immediately (skips CLIP entirely). On a confident read of
        # a set with zero images in our index, returns an honest no-match
        # instead of letting CLIP guess at an unrelated card. Anchored to
        # the OCR-extracted identity, never to a CLIP result — CLIP
        # hasn't run yet at this point in the request.
        if query_category in ("YUGIOH", "MTG", "POKEMON"):
            try:
                _ocr_direct_sku, _ocr_extracted_set_id = ocr_direct_lookup(
                    str(query_path1), query_category, jp_mode=(_early_jp_mode == 'jp')
                )

                # Denominator gate — prevent OCR-FIRST from returning a wrong card when
                # the denominator from the card doesn't match the candidate set's total.
                if _ocr_direct_sku and query_category == "POKEMON":
                    import ocr_confirm as _oc_ref
                    from app import _load_set_metadata
                    _ocr_denom = _oc_ref._last_pkm_denominator
                    if _denominator_blocks_promotion(
                        _ocr_direct_sku, _ocr_denom, _load_set_metadata(), query_category
                    ):
                        print(
                            f"[OCR-FIRST] denominator mismatch, falling through to CLIP: "
                            f"{_ocr_direct_sku} denom={_ocr_denom}",
                            flush=True,
                        )
                        _ocr_direct_sku = None

                if _ocr_direct_sku:
                    scan_decision = _evaluate_scan_decision(request, sku=_ocr_direct_sku)
                    if not scan_decision.get("allowed", True):
                        _reason = scan_decision.get("reason", "free_limit_exceeded")
                        _tier   = scan_decision.get("tier", "free") or "free"
                        _messages = {
                            "free_limit_exceeded": "You've used all 150 free scans this month.",
                            "tier_limit_exceeded": "You've reached your fair-use cap for this period.",
                        }
                        return jsonify({
                            "error":           "scan_limit_reached",
                            "reason":          _reason,
                            "tier":            _tier,
                            "limit":           scan_decision.get("limit"),
                            "count":           scan_decision.get("count"),
                            "remaining":       0,
                            "topup_remaining": scan_decision.get("topup_remaining", 0),
                            "message":         _messages.get(_reason, "Scan limit reached."),
                        }), 402

                    # Profile enrichment — same centralized-then-per-folder
                    # pattern app.py's own OCR-first block already uses.
                    _vertical_id_ocr = get_vertical().get("id", "")
                    _profile_ocr = {}
                    _cpp_ocr = os.path.join(app.root_path, f"sku_profiles_{_vertical_id_ocr}.json")
                    if not os.path.exists(_cpp_ocr):
                        _cpp_ocr = os.path.join(app.root_path, "sku_profiles.json")
                    if os.path.exists(_cpp_ocr):
                        try:
                            with open(_cpp_ocr, "r", encoding="utf-8") as _f:
                                _cp_ocr = json.load(_f)
                            if _ocr_direct_sku in _cp_ocr:
                                _profile_ocr.update(_cp_ocr[_ocr_direct_sku])
                        except Exception:
                            pass
                    _cxp_ocr = os.path.join(app.root_path, f"sku_crossrefs_{_vertical_id_ocr}.json")
                    if not os.path.exists(_cxp_ocr):
                        _cxp_ocr = os.path.join(app.root_path, "sku_crossrefs.json")
                    if os.path.exists(_cxp_ocr):
                        try:
                            with open(_cxp_ocr, "r", encoding="utf-8") as _f:
                                _cx_ocr = json.load(_f)
                            if _ocr_direct_sku in _cx_ocr:
                                _xr_ocr = _cx_ocr[_ocr_direct_sku]
                                if _xr_ocr.get("manufacturer"):
                                    _profile_ocr["manufacturer"] = _xr_ocr["manufacturer"]
                                if _xr_ocr.get("crossrefs"):
                                    _profile_ocr["crossrefs"] = _xr_ocr["crossrefs"]
                        except Exception:
                            pass
                    if not _profile_ocr:
                        from profile_utils import _load_card_profile_for_sku as _lcp_ocr
                        from vertical_loader import get_db_root as _gdr_ocr
                        _dbr_ocr = _gdr_ocr()
                        if _dbr_ocr:
                            _profile_ocr = _lcp_ocr(_ocr_direct_sku, _dbr_ocr, get_data_dir()) or {}

                    _match_entry_ocr = {"rank": 1, "sku": _ocr_direct_sku, "score": 1.0, "probability": 1.0}
                    if _profile_ocr:
                        from app import _attach_set_total as _attach_set_total_ocr
                        from app import _sanitise_prices_for_response as _sanitise_ocr
                        _profile_ocr = _sanitise_ocr(_profile_ocr)
                        _match_entry_ocr["profile"] = _attach_set_total_ocr(_profile_ocr)
                    _image_id_ocr = _image_id_for_sku(_ocr_direct_sku)
                    if _image_id_ocr:
                        _match_entry_ocr["images"] = [{"image_id": _image_id_ocr, "original_filename": ""}]
                    from app import _extract_gbp_from_profile as _extract_gbp_ocr
                    _match_entry_ocr["best_gbp"] = _extract_gbp_ocr(
                        _match_entry_ocr.get("profile", {}), sku=_ocr_direct_sku
                    )

                    _increment_scan_counter()
                    print(f"[OCR-FIRST] Skipped CLIP — direct match: {_ocr_direct_sku}", flush=True)
                    return jsonify({
                        "matches": [_match_entry_ocr],
                        "low_confidence": False,
                        "ocr_confirmed": True,
                        "show_transition_toast": bool(scan_decision.get("show_transition_toast", False)),
                        "query_id": query_id,
                        "timing_ms": round((time.time() - t_start) * 1000),
                    })

                # No DB row for what OCR read — honest no-match if the
                # card was confidently identified but that set has zero
                # images in our index at all.
                if _ocr_extracted_set_id and not _is_set_imaged(query_category, _ocr_extracted_set_id):
                    print(f"[OCR-FIRST] set={_ocr_extracted_set_id} confidently read, "
                          f"no images in index — honest no-match", flush=True)
                    return jsonify({
                        "matches": [],
                        "low_confidence": True,
                        "reason": "set_not_in_database",
                        "message": "This set isn't in our database yet — we're regularly adding new sets, so check back soon!",
                        "show_transition_toast": True
                    }), 200
            except Exception as _e:
                print(f"[OCR-FIRST] error, falling through to CLIP: {_e}", flush=True)

        # ── Clean + auto detect (groove detection etc) ──
        clean1, clean2, auto_fg, auto_bg, _clean_diag = _clean_and_auto_grooves(
            str(query_path1),
            str(query_path2) if query_path2 else None,
        )

        # ── Embed ──
        try:
            emb = get_embedder()
        except Exception as e:
            return jsonify({"error": f"Embedder not available: {e}"}), 500

        if emb is None or not hasattr(emb, "embed_path"):
            return jsonify({"error": "Embedder not available"}), 500

        v = get_vertical()
        use_multi_crop = v.get("multi_crop", True)
        use_suppress_bg = v.get("suppress_bg", True)

        params = dict(
            multi_crop=use_multi_crop,
            suppress_bg=use_suppress_bg,
            max_side=int(CFG.get("auto_mode_b_max_side", 1024)),
        )
        params_back = dict(
            multi_crop=False,
            suppress_bg=use_suppress_bg,
            max_side=int(CFG.get("auto_mode_b_max_side", 1024)),
        )

        try:
            qf = _embed_one_query(emb, str(query_path1), **params)
            qb = None
            if query_path2 is not None and query_path2.exists():
                qb = _embed_one_query(emb, str(query_path2), **params_back)
        except Exception as e:
            return jsonify({"error": f"Embedding failed: {e}"}), 500

        # ── Match ──
        TOP_K_SKU = int(CFG.get("top_k_sku", 20))
        TOP_M_PER_SKU = int(CFG.get("top_m_per_sku", 3))
        CAP_PER_SKU = int(CFG.get("cap_per_sku", 30))

        jp_mode = request.form.get('jp_mode', 'en')
        exclude_jpn = (jp_mode != 'jp')
        _allowed_sets_raw = request.form.get('allowed_jpn_sets', '').strip()
        _allowed_jpn_sets = set(_allowed_sets_raw.split(',')) if _allowed_sets_raw else None

        try:
            results, low_cert, diag = _run_match_paired_two_stage(
                qf, qb,
                query_front_path=str(query_path1),
                query_back_path=str(query_path2) if query_path2 else None,
                top_k_sku=TOP_K_SKU,
                top_m_per_sku=TOP_M_PER_SKU,
                cap_per_sku=CAP_PER_SKU,
                softmax_temp=float(CFG.get("softmax_temp", 0.015)),
                low_cert_prob=float(CFG.get("low_cert_prob", 0.55)),
                low_cert_prob_gap=float(CFG.get("low_cert_prob_gap", 0.15)),
                cons_n=int(CFG.get("consistency_n", 5)),
                cons_sigma=float(CFG.get("consistency_sigma", 0.040)),
                query_category=query_category,
                query_profile=query_profile,
                auto_front_grooves=auto_fg,
                auto_back_grooves=auto_bg,
                exclude_jpn=exclude_jpn,
                allowed_jpn_sets=_allowed_jpn_sets,
            )
        except Exception as e:
            return jsonify({"error": f"Matching failed: {e}"}), 500
        
        print(f"[SCAN] Top match: {results[0]['sku']} score={results[0]['score']:.4f}", flush=True)
        if results[0]['score'] < 0.65:
            return jsonify({"matches": [], "low_confidence": True, "reason": "score_too_low", "show_transition_toast": False}), 200

        # DINOv2 tiebreaker
        _dino_diag = {"fired": False, "reason": "not_run"}
        if results:
            _req_mode = request.form.get("mode", "precise")
            if _req_mode != "fast":
                results, _dino_diag = _dinov2_tiebreak(results, str(query_path1))
            else:
                _dino_diag = {"fired": False, "reason": "mode_fast_skip"}
        if diag is not None:
            diag["dino"] = _dino_diag

        # OCR set-code confirmation — promotes correct print to rank 1
        if results:
            from ocr_confirm import ocr_confirm_ranking
            # Auto-detect TCG from top CLIP match if user didn't specify
            _effective_tcg = query_category
            if not _effective_tcg and results:
                _top_sku = results[0].get("sku", "")
                if _top_sku.startswith("ygo-"):
                    _effective_tcg = "YUGIOH"
                elif _top_sku.startswith("mtg-"):
                    _effective_tcg = "MTG"
                else:
                    _effective_tcg = "POKEMON"
                print(f"[OCR] Auto-detected TCG from CLIP top match: {_effective_tcg} (sku={_top_sku})", flush=True)
            from app import _load_set_metadata
            results, _ocr_info = ocr_confirm_ranking(
                results,
                str(query_path1),
                _effective_tcg,
                search_depth=10,
                set_metadata=_load_set_metadata(),
                jpn_mode=(jp_mode == 'jp'),
            )
            print(
                f"[OCR] status={_ocr_info.get('ocr_status')} "
                f"extracted={_ocr_info.get('extracted')} "
                f"matched={_ocr_info.get('matched_sku')} "
                f"promoted={_ocr_info.get('promoted')}",
                flush=True,
            )
            if diag is not None:
                diag["ocr"] = _ocr_info

        # Second score gate — re-check after DINOv2 tiebreak + OCR reordering,
        # since either can promote a lower-scoring SKU to rank 0.
        if results[0]['score'] < 0.65:
            return jsonify({
                "matches": [],
                "low_confidence": True,
                "reason": "score_too_low_post_rerank",
                "show_transition_toast": False
            }), 200

        # Honest no-match: OCR read nothing at all AND DINOv2 (which only
        # ever runs when CLIP itself was already stuck in a near-tie) is
        # also a genuine coin-flip between its own top two. Neither
        # independent signal can vouch for the CLIP guess here, so say so
        # instead of returning a top-5 list. Narrow on purpose — any other
        # ocr_status (OCR read SOMETHING, even if unmatched) or a DINOv2
        # tiebreak that didn't fire at all (including CLIP already having
        # a clear winner) falls through to existing behaviour unchanged.
        if (_ocr_info.get("ocr_status") == "no_text_found"
                and _dino_diag.get("fired") is True
                and _dino_diag.get("dino_gap", 1.0) < CFG.get("dinov2_swap_margin", 0.05)):
            return jsonify({
                "matches": [],
                "low_confidence": True,
                "reason": "low_confidence_no_ocr",
                "message": "We couldn't confidently match this card. Try scanning again with better lighting, or hold the card steadier.",
                "show_transition_toast": True
            }), 200

        # Language separation post-filter — bidirectional.
        # EN mode: strip any jpn- that passed through the pre-filter.
        # JP mode: strip any non-jpn- (EN) cards from results.
        jp_mode = request.form.get('jp_mode', 'en')
        _before_lang_filter = len(results)
        if jp_mode == 'jp':
            results = [r for r in results if r.get('sku', '').startswith('jpn-')]
            if not results:
                return jsonify({
                    "matches": [],
                    "low_confidence": True,
                    "reason": "no_results_after_en_strip",
                    "show_transition_toast": False
                }), 200
            logger.info(f"[LANG-FILTER] JP mode: {_before_lang_filter} → {len(results)} results (EN stripped)")
            _JP_SCORE_FLOOR = 0.72
            if results[0].get('score', 0) < _JP_SCORE_FLOOR:
                logger.info(f"[JP-FLOOR] Top score {results[0].get('score', 0):.3f} < {_JP_SCORE_FLOOR} — returning no match")
                return jsonify({"matches": [], "low_confidence": True, "reason": "jp_below_score_floor", "show_transition_toast": False}), 200
        else:
            results = [r for r in results if not r.get('sku', '').startswith('jpn-')]
            if not results:
                return jsonify({
                    "matches": [],
                    "low_confidence": True,
                    "reason": "no_results_after_jp_filter",
                    "show_transition_toast": False
                }), 200
            logger.info(f"[LANG-FILTER] EN mode: {_before_lang_filter} → {len(results)} results (JP stripped)")

        # Option B (restored): charge quota only on OCR-confirmed match --
        # UNLESS the caller opts in to always_charge (e.g. the authenticity
        # mode, which is a single deliberate identification, not a live
        # loop retrying uncertain frames -- see the counting recon in the
        # commit message for why "free CLIP-only" is wrong for that case).
        # Opt-in and default-off so every existing caller (the live camera
        # scanner's own fallback to this same route) is byte-identical to
        # before this parameter existed.
        _always_charge_api = request.form.get("always_charge", "").strip().lower() in ("1", "true", "yes")
        _ocr_status_api = _ocr_info.get("ocr_status", "not_run")
        _ocr_confirmed_api = _ocr_status_api in ("rank1_confirmed", "direct_lookup", "promoted")
        scan_decision = {"allowed": True, "show_transition_toast": False}
        if _ocr_confirmed_api or _always_charge_api:
            _sku_for_charge_api = _ocr_info.get("matched_sku") or results[0].get("sku", "")
            scan_decision = _evaluate_scan_decision(request, sku=_sku_for_charge_api)
            if not scan_decision.get("allowed", True):
                _reason = scan_decision.get("reason", "free_limit_exceeded")
                _tier   = scan_decision.get("tier", "free") or "free"
                _messages = {
                    "free_limit_exceeded": "You've used all 150 free scans this month.",
                    "tier_limit_exceeded": "You've reached your fair-use cap for this period.",
                }
                return jsonify({
                    "error":           "scan_limit_reached",
                    "reason":          _reason,
                    "tier":            _tier,
                    "limit":           scan_decision.get("limit"),
                    "count":           scan_decision.get("count"),
                    "remaining":       0,
                    "topup_remaining": scan_decision.get("topup_remaining", 0),
                    "message":         _messages.get(_reason, "Scan limit reached."),
                }), 402

        # ── Format response (enriched with profile + image data) ──
        import sqlite3
        from vertical_loader import get_db_root

        # Load image_ids for matched SKUs from SQLite. Uses the same
        # transient-mount-recovery helpers _lookup_sku_by_setcode() relies on
        # (ocr_confirm.py) instead of a second, unguarded connect() — a
        # snapshot-restored container's volume mount can be briefly invisible
        # right after "Restoring Function from memory snapshot" (see
        # _reload_volume_once() docstring).
        from ocr_confirm import _get_images_db_path, _reload_volume_once
        db_path = _get_images_db_path()

        # Always bound, regardless of whether the DB lookup below succeeds —
        # matched_skus feeds the profile-loading loop further down and must
        # never be conditionally assigned inside the try that follows.
        sku_image_map = {}
        matched_skus = set()
        for r in results[:top_k]:
            sku = r.get("sku", "") if isinstance(r, dict) else getattr(r, "sku", "")
            if sku:
                matched_skus.add(sku)

        if not db_path.exists():
            logger.warning(f"[IMAGE-MAP] images.db not visible at {db_path} — reloading volume")
            _reload_fired = _reload_volume_once()
            logger.info(
                f"[IMAGE-MAP] reload retry fired={_reload_fired} "
                f"exists_after={db_path.exists()}"
            )

        try:
            conn = sqlite3.connect(str(db_path))
            for sku in matched_skus:
                rows = conn.execute(
                    "SELECT image_id, original_filename FROM images WHERE sku = ? LIMIT 3",
                    (sku,)
                ).fetchall()
                sku_image_map[sku] = [
                    {"image_id": row[0], "original_filename": row[1] or ""}
                    for row in rows
                ]
            conn.close()
        except Exception as e:
            logger.warning(f"[IMAGE-MAP] sku_image_map build failed: {e}")

        # Load profile data for matched SKUs
        # Supports two patterns:
        #   1. Cards-style: CardsDB/pokemon/{sku}/profile.json (per-folder)
        #   2. Keys-style:  sku_profile.json + sku_crossrefs.json (centralized)

        try:
            from modal_config import vol
            vol.reload()
        except Exception:
            pass
        db_root = get_db_root()
        sku_profile_map = {}
        # Try centralized files first (keys pattern)
        app_root = app.root_path
        vertical_id = v.get("id", "")
        central_profile_path = os.path.join(app_root, f"sku_profiles_{vertical_id}.json")
        if not os.path.exists(central_profile_path):
            central_profile_path = os.path.join(app_root, "sku_profiles_kitchen_tools.json")
        central_xref_path = os.path.join(app_root, f"sku_crossrefs_{vertical_id}.json")
        if not os.path.exists(central_xref_path):
            central_xref_path = os.path.join(app_root, "sku_crossrefs.json")

        central_profiles = {}
        central_xrefs = {}
        if os.path.exists(central_profile_path):
            try:
                with open(central_profile_path, "r", encoding="utf-8") as f:
                    central_profiles = json.load(f)
            except Exception:
                pass
        if os.path.exists(central_xref_path):
            try:
                with open(central_xref_path, "r", encoding="utf-8") as f:
                    central_xrefs = json.load(f)
            except Exception:
                pass

        from profile_utils import _load_card_profile_for_sku
        from app import get_data_dir as _get_data_dir, _attach_set_total
        for sku in matched_skus:
            # Check centralized files
            if sku in central_profiles or sku in central_xrefs:
                combined = {}
                if sku in central_profiles:
                    combined.update(central_profiles[sku])
                if sku in central_xrefs:
                    xref = central_xrefs[sku]
                    if xref.get("manufacturer"):
                        combined["manufacturer"] = xref["manufacturer"]
                    if xref.get("crossrefs"):
                        combined["crossrefs"] = xref["crossrefs"]
                sku_profile_map[sku] = _attach_set_total(combined)
                continue

            # Fallback: per-folder profile.json (cards pattern)
            if db_root:
                _card_profile = _load_card_profile_for_sku(sku, db_root, _get_data_dir())
                if _card_profile:
                    sku_profile_map[sku] = _attach_set_total(_card_profile)
                else:
                    direct_path = Path(db_root) / sku / "profile.json"
                    if direct_path.exists():
                        try:
                            with open(direct_path, "r", encoding="utf-8") as pf:
                                sku_profile_map[sku] = _attach_set_total(json.load(pf))
                        except Exception:
                            pass

        from app import _extract_gbp_from_profile
        from app import _sanitise_prices_for_response

        matches = []
        for i, r in enumerate(results[:top_k]):
            if isinstance(r, dict):
                sku = r.get("sku", "")
                match_entry = {
                    "rank": i + 1,
                    "sku": sku,
                    "score": round(float(r.get("score", 0)), 4),
                    "probability": round(float(r.get("prob", 0)), 4),
                }
            else:
                sku = getattr(r, "sku", str(r))
                match_entry = {
                    "rank": i + 1,
                    "sku": sku,
                    "score": round(float(getattr(r, "score", 0)), 4),
                    "probability": round(float(getattr(r, "prob", 0)), 4),
                }

            # Attach profile data
            if sku in sku_profile_map:
                match_entry["profile"] = _sanitise_prices_for_response(sku_profile_map[sku])

            match_entry['best_gbp'] = _extract_gbp_from_profile(
                match_entry.get('profile', {}), sku=sku
            )

            # Attach image references
            if sku in sku_image_map:
                match_entry["images"] = sku_image_map[sku]

            matches.append(match_entry)

        t_end = time.time()
        timing_ms = round((t_end - t_start) * 1000)

        # Record price history; only increment stats counter when quota was
        # actually consumed above (OCR-confirmed, or always_charge) -- keeps
        # this site-wide stats counter in sync with the real per-user quota
        # consumption rather than a separate condition that could drift.
        try:
            from app import _record_price, _extract_gbp_from_profile, _increment_scan_counter, _increment_sku_scan_freq
            if matches:
                _m0 = matches[0]
                _record_price(_m0.get("sku"), _extract_gbp_from_profile(_m0.get("profile", {}), sku=_m0.get("sku")))
            if matches and (_ocr_confirmed_api or _always_charge_api):
                _increment_scan_counter()
                # Per-sku popularity counter (guarded; never affects the result).
                _increment_sku_scan_freq(_m0.get("sku"))
        except Exception:
            pass

        # Quick CPU grade on the uploaded image (must run BEFORE cleanup — grade reads from disk)
        from app import _safe_grade
        _grade = _safe_grade(str(query_path1))

        # Clean up container-FS query images now that match, OCR, and grade are all done
        try:
            query_path1.unlink(missing_ok=True)
            if query_path2: query_path2.unlink(missing_ok=True)
        except Exception:
            pass

        response = {
            "matches": matches,
            "low_confidence": bool(low_cert) and _ocr_info.get("ocr_status") not in ("promoted", "rank1_confirmed", "direct_lookup"),
            "ocr_confirmed": _ocr_confirmed_api,
            "show_transition_toast": bool(scan_decision.get("show_transition_toast", False)),
            "query_id": query_id,
            "timing_ms": timing_ms,
            "vertical": v.get("id", ""),
            "client": getattr(request, "_api_client", "unknown"),
            "grade": _grade,
        }

        # Include diagnostics only if requested
        if request.form.get("include_diagnostics", "").lower() in ("1", "true", "yes"):
            response["diagnostics"] = {
                k: (round(float(v), 4) if isinstance(v, (int, float, np.floating)) else str(v))
                for k, v in (diag or {}).items()
            }

        return jsonify(response)

    # ─────────────────────────────────────────
    # Batch status (future: async batch jobs)
    # ─────────────────────────────────────────

    @app.route("/api/v1/stats", methods=["GET"])
    @api_key_required
    def api_stats():
        """Basic usage stats for the API client."""
        from app import get_cached_rows, FRONT_MATRIX, BACK_MATRIX

        rows = get_cached_rows(force=False)
        return jsonify({
            "total_images": len(rows) if rows else 0,
            "front_embeddings": FRONT_MATRIX.shape[0] if FRONT_MATRIX is not None else 0,
            "back_embeddings": BACK_MATRIX.shape[0] if BACK_MATRIX is not None else 0,
            "client": getattr(request, "_api_client", "unknown"),
        })

    # ─────────────────────────────────────────
    # Serve matched images
    # ─────────────────────────────────────────

    @app.route("/api/v1/image/<image_id>", methods=["GET"])
    def api_serve_image(image_id):
        """Serve a matched image by its image_id."""
        import sqlite3
        from flask import make_response
        from app import get_images_db_path

        def _cached(path, mimetype="image/jpeg"):
            resp = make_response(send_file(path, mimetype=mimetype))
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp

        # Try clean_images first (UUID-named files)
        clean_dir = os.path.join(app.root_path, "clean_images")
        for ext in (".jpg", ".jpeg", ".png"):
            clean_path = os.path.join(clean_dir, f"{image_id}{ext}")
            if os.path.exists(clean_path):
                return _cached(clean_path)

        # Try path from database
        try:
            db_path = get_images_db_path()
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT path FROM images WHERE image_id = ?", (image_id,)
                ).fetchone()
            finally:
                conn.close()
        except Exception as e:
            # Cold-start race: volume/DB not mounted yet on snapshot restore.
            # The image likely EXISTS but is briefly unreachable — signal transient,
            # not permanent (404 would tell the CDN to give up).
            print(f"[IMG-503] api_serve_image DB not ready for {image_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            resp = make_response(("Image temporarily unavailable", 503))
            resp.headers["Retry-After"] = "5"
            return resp

        if row and row[0] and os.path.exists(row[0]):
            return _cached(row[0])

        abort(404)
    
    @app.route("/api/v1/ras_image/<sku>", methods=["GET"])
    def api_serve_ras_image(sku):
        """Serve a RAS (reference) image by SKU name."""
        ras_dirs = [
            os.path.join(app.root_path, "ras_images", "ras_images"),
            r"C:\Users\c_a_b\OneDrive\Pictures\RASTER",
        ]
        for ras_dir in ras_dirs:
            for ext in (".jpg", ".jpeg", ".png"):
                ras_path = os.path.join(ras_dir, f"{sku}_RAS{ext}")
                if os.path.exists(ras_path):
                    return send_file(ras_path, mimetype="image/jpeg")
        return jsonify({"error": "RAS image not found"}), 404