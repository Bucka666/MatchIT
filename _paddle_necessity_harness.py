"""
_paddle_necessity_harness.py — ONE-OFF measurement harness, NOT part of the
live app and not deployed. Runs the REAL CLIP/DINOv2 matcher + REAL
ocr_confirm.py against the 150-card validation set on the live Modal volume,
to measure:
  RUN 1: does PaddleOCR ever fire when Vision is healthy (it shouldn't)?
  RUN 2: with Vision forced off IN THIS HARNESS PROCESS ONLY (a runtime
         monkeypatch of ocr_confirm._get_vision_api_key — the file on disk
         and the deployed app are never touched), does the resulting
         Paddle-driven OCR confirmation help, hurt, or no-op the final
         match outcome vs ground truth?

Both runs reuse the SAME pre-OCR CLIP+DINOv2 result per card (computed once)
so the visual baseline is identical across both OCR conditions — only the
OCR layer on top differs.
"""

import copy
import csv
import json
import os
import sys

import modal

sys.path.insert(0, "/app")
from modal_config import vol, image

harness_app = modal.App("grailsweep-paddle-necessity-test")

# test_queries/ is excluded from the main image's /app mount (modal_config.py
# ignore list) — add it back explicitly just for this harness.
_harness_image = (
    image
    .add_local_dir("C:/MatchIT/test_queries", remote_path="/app/test_queries")
    .add_local_file("C:/MatchIT/groundtruth.csv", remote_path="/app/groundtruth.csv")
)


def _game_of(sku: str) -> str:
    if sku.startswith("mtg-"):
        return "MTG"
    if sku.startswith("ygo-"):
        return "YGO"
    return "POKEMON"


@harness_app.function(
    image=_harness_image,
    gpu="T4",
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
    timeout=3600,
)
def run_paddle_necessity_test(limit: int = 0):
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"

    vol.reload()
    from matchit_modal import _fix_vertical_config, _fix_db_paths
    _fix_vertical_config()
    _fix_db_paths()

    from app import (
        app as flask_app, CFG, get_embedder, load_embedding_cache,
        _embed_one_query, _run_match_paired_two_stage, _dinov2_tiebreak,
        _clean_and_auto_grooves, _load_set_metadata,
    )
    from vertical_loader import get_vertical
    import ocr_confirm
    from ocr_confirm import ocr_confirm_ranking

    with flask_app.app_context():
        emb = get_embedder()
        load_embedding_cache(force=False)
        v = get_vertical()
        set_metadata = _load_set_metadata()

        rows = []
        with open("/app/groundtruth.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("server_sku"):
                    rows.append(r)
        if limit:
            rows = rows[:limit]
        print(f"[HARNESS] {len(rows)} cards loaded", flush=True)

        # ---- instrumentation: count Paddle invocations without changing behavior ----
        _paddle_calls = {"count": 0}
        _orig_paddle_read = ocr_confirm._paddle_ocr_read

        def _wrapped_paddle_read(crop_image):
            _paddle_calls["count"] += 1
            return _orig_paddle_read(crop_image)

        _orig_get_key = ocr_confirm._get_vision_api_key

        records = []
        run1 = {"total": 0, "paddle_fired": 0, "correct": 0}
        run2 = {"PADDLE_HELPED": 0, "PADDLE_HURT": 0, "NO_OP": 0, "CHANGED_STILL_WRONG": 0, "total": 0}
        per_game = {}

        for i, row in enumerate(rows, 1):
            fn = row["filename"]
            true_sku = row["server_sku"]
            game = _game_of(true_sku)
            img_path = f"/app/test_queries/{fn}"
            if not os.path.exists(img_path):
                print(f"  [SKIP] missing file: {fn}", flush=True)
                continue

            try:
                clean1, clean2, auto_fg, auto_bg, _diag = _clean_and_auto_grooves(img_path, None)
                qf = _embed_one_query(
                    emb, img_path,
                    multi_crop=v.get("multi_crop", True),
                    suppress_bg=v.get("suppress_bg", True),
                    max_side=int(CFG.get("auto_mode_b_max_side", 1024)),
                )
                results, low_cert, diag = _run_match_paired_two_stage(
                    qf, None,
                    query_front_path=img_path,
                    query_back_path=None,
                    top_k_sku=int(CFG.get("top_k_sku", 20)),
                    top_m_per_sku=int(CFG.get("top_m_per_sku", 3)),
                    cap_per_sku=int(CFG.get("cap_per_sku", 30)),
                    softmax_temp=float(CFG.get("softmax_temp", 0.015)),
                    low_cert_prob=float(CFG.get("low_cert_prob", 0.55)),
                    low_cert_prob_gap=float(CFG.get("low_cert_prob_gap", 0.15)),
                    cons_n=int(CFG.get("consistency_n", 5)),
                    cons_sigma=float(CFG.get("consistency_sigma", 0.040)),
                    query_category="",
                    query_profile={},
                    auto_front_grooves=auto_fg,
                    auto_back_grooves=auto_bg,
                )
                if not results:
                    print(f"  [NO-RESULT] {fn}", flush=True)
                    continue

                results = _dinov2_tiebreak(results, img_path)
                visual_pred = results[0]["sku"] if isinstance(results[0], dict) else results[0].sku
                visual_correct = (visual_pred == true_sku)
                tcg_for_ocr = "YUGIOH" if game == "YGO" else game

                # ---- RUN 1: Vision normal (real key from the real secret) ----
                ocr_confirm._paddle_ocr_read = _wrapped_paddle_read
                _paddle_calls["count"] = 0
                results_v = copy.deepcopy(results)
                results_v, ocr_info_v = ocr_confirm_ranking(
                    results_v, img_path, tcg_for_ocr, search_depth=10, set_metadata=set_metadata,
                )
                vision_final = results_v[0]["sku"] if isinstance(results_v[0], dict) else results_v[0].sku
                paddle_fired_run1 = _paddle_calls["count"] > 0
                vision_correct = (vision_final == true_sku)

                # ---- RUN 2: Vision forced off, HARNESS-ONLY monkeypatch ----
                ocr_confirm._get_vision_api_key = lambda: ""
                _paddle_calls["count"] = 0
                results_p = copy.deepcopy(results)
                results_p, ocr_info_p = ocr_confirm_ranking(
                    results_p, img_path, tcg_for_ocr, search_depth=10, set_metadata=set_metadata,
                )
                paddle_final = results_p[0]["sku"] if isinstance(results_p[0], dict) else results_p[0].sku
                paddle_fired_run2 = _paddle_calls["count"] > 0
                ocr_confirm._get_vision_api_key = _orig_get_key
                ocr_confirm._paddle_ocr_read = _orig_paddle_read

                paddle_correct = (paddle_final == true_sku)

                if not visual_correct and paddle_correct:
                    classification = "PADDLE_HELPED"
                elif visual_correct and not paddle_correct:
                    classification = "PADDLE_HURT"
                elif paddle_final == visual_pred:
                    classification = "NO_OP"
                else:
                    classification = "CHANGED_STILL_WRONG"

                rec = {
                    "fn": fn, "game": game, "true": true_sku,
                    "visual_pred": visual_pred, "visual_correct": visual_correct,
                    "vision_final": vision_final, "vision_correct": vision_correct,
                    "paddle_fired_run1": paddle_fired_run1,
                    "paddle_final": paddle_final, "paddle_correct": paddle_correct,
                    "paddle_fired_run2": paddle_fired_run2,
                    "paddle_extracted": ocr_info_p.get("extracted"),
                    "classification": classification,
                }
                records.append(rec)

                run1["total"] += 1
                if paddle_fired_run1:
                    run1["paddle_fired"] += 1
                if vision_correct:
                    run1["correct"] += 1

                run2["total"] += 1
                run2[classification] += 1

                pg = per_game.setdefault(game, {
                    "PADDLE_HELPED": 0, "PADDLE_HURT": 0, "NO_OP": 0, "CHANGED_STILL_WRONG": 0,
                    "total": 0, "vision_correct": 0, "paddle_fired_run1": 0,
                })
                pg["total"] += 1
                pg[classification] += 1
                if vision_correct:
                    pg["vision_correct"] += 1
                if paddle_fired_run1:
                    pg["paddle_fired_run1"] += 1

                if classification in ("PADDLE_HELPED", "PADDLE_HURT"):
                    print(f"  [{classification}] {fn} true={true_sku} visual={visual_pred} "
                          f"paddle={paddle_final} extracted={rec['paddle_extracted']!r}", flush=True)
                if i % 25 == 0:
                    print(f"  ... {i}/{len(rows)} done", flush=True)

            except Exception as e:
                print(f"  [ERROR] {fn}: {e}", flush=True)
                continue

        print("\n=== RUN 1 (Vision working) ===", flush=True)
        print(json.dumps(run1, indent=2), flush=True)
        print("\n=== RUN 2 (Vision forced off -> Paddle) tallies ===", flush=True)
        print(json.dumps(run2, indent=2), flush=True)
        print("\n=== per-game ===", flush=True)
        print(json.dumps(per_game, indent=2), flush=True)

        try:
            with open("/tmp/paddle_necessity_results.csv", "w", newline="", encoding="utf-8") as f:
                if records:
                    w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
                    w.writeheader()
                    w.writerows(records)
        except Exception as e:
            print(f"[HARNESS] could not write csv: {e}", flush=True)

        return {"run1": run1, "run2": run2, "per_game": per_game, "n_records": len(records)}


@harness_app.local_entrypoint()
def main(limit: int = 0):
    result = run_paddle_necessity_test.remote(limit=limit)
    print(json.dumps(result, indent=2))
