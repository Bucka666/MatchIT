# equivalence_harness.py
# READ-ONLY against the server: only calls the existing public /api/v1/match
# (camera leg) and /match (upload leg). Does not touch CardsDB, Modal volumes,
# or any prod state beyond what a normal scan already does (quota counters,
# stats.json, price history) — identical side effects to a real user scan.
#
# Two entrypoints in one file:
#   1. Capture mode  — POST all 150 test_queries/*.jpg to BOTH handlers against
#      a given --target-url, dump a per-image golden-record JSON.
#   2. Diff mode      — python equivalence_harness.py --diff A.json B.json
#      compares two such captures field-by-field and reports decision drift.
#
# Auth/secret plumbing (Modal secret fetch, X-CF-Proxy-Secret, X-API-Key) is
# reused near-verbatim from run_server_groundtruth.py. Join pattern (filename
# key, per-bucket breakdown) follows phase4_pixel_match.py.
#
# Usage:
#   python equivalence_harness.py --target-url https://grailsweep.com \
#       --label baseline_gpu --out baseline_gpu.json [--limit 3] [--direct-modal]
#   python equivalence_harness.py --diff baseline_gpu.json cputwin.json
#
import argparse
import ast
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import modal

QUERY_DIR = "test_queries"
GROUNDTRUTH_CSV = "groundtruth.csv"
BUCKETS_CSV = "buckets.csv"
BUCKET_ORDER = ("distinct", "variant", "mixed")

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}

# ── Modal secret plumbing — reused from run_server_groundtruth.py verbatim ──
_SECRET_NAME = "external-api-credentials"
_ENV_IN_SECRET = "GRAILSWEEP_API_KEYS"   # JSON dict {client_name: key}
_SCANNER_KEY_NAME = "grailsweep_scanner"

_CF_SECRET_NAME = "cf-proxy-secret"
_CF_ENV_IN_SECRET = "CF_PROXY_SECRET"

_key_app = modal.App("equivalence-harness-key-fetch")


@_key_app.function(secrets=[modal.Secret.from_name(_SECRET_NAME)])
def _read_scanner_key():
    import os as _os, json as _json
    raw = _os.environ.get(_ENV_IN_SECRET, "")
    d = _json.loads(raw)
    return d[_SCANNER_KEY_NAME]


@_key_app.function(secrets=[modal.Secret.from_name(_CF_SECRET_NAME)])
def _read_cf_proxy_secret():
    import os as _os
    return _os.environ.get(_CF_ENV_IN_SECRET, "")


def fetch_secrets_from_modal(want_key, want_cf):
    key = cf = None
    with _key_app.run():
        if want_key:
            key = _read_scanner_key.remote()
        if want_cf:
            cf = _read_cf_proxy_secret.remote()
    return key, cf


def resolve_credentials(from_modal_secret, direct_modal):
    api_key = None if from_modal_secret else os.environ.get("GRAILSWEEP_API_KEY", "")
    cf_secret = None
    if from_modal_secret or direct_modal:
        what = []
        if from_modal_secret:
            what.append(f"scanner key from '{_SECRET_NAME}'")
        if direct_modal:
            what.append(f"CF proxy secret from '{_CF_SECRET_NAME}'")
        print("Fetching " + " + ".join(what) + " via Modal (not printed)...")
        key, cf = fetch_secrets_from_modal(want_key=from_modal_secret, want_cf=direct_modal)
        if from_modal_secret:
            if not key:
                raise SystemExit("Modal secret returned an empty scanner key.")
            api_key = key
        if direct_modal:
            if not cf:
                raise SystemExit("Modal secret returned an empty CF proxy secret.")
            cf_secret = cf
        print("Secret(s) obtained (held in memory only).\n")
    return api_key, cf_secret


# ── Ground truth / bucket joins ──────────────────────────────────────────────

def load_groundtruth():
    expected = {}
    if os.path.isfile(GROUNDTRUTH_CSV):
        with open(GROUNDTRUTH_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("server_sku"):
                    expected[r["filename"]] = r["server_sku"]
    return expected


def load_buckets():
    bucket_of = {}
    if os.path.isfile(BUCKETS_CSV):
        with open(BUCKETS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                bucket_of[r["filename"]] = (r.get("bucket") or "untagged").strip() or "untagged"
    return bucket_of


# ── CAMERA leg — POST /api/v1/match, include_diagnostics=true ───────────────
#
# NOTE on dinov2_fired / dinov2 distances: _dinov2_tiebreak() (app.py) never
# attaches a marker to `results` or to `diag` when it fires — it only prints
# to server stdout. The `diag` dict returned by _run_match_paired_two_stage()
# (app.py ~2503) has no dinov2 keys at all. There is therefore NO HTTP-visible
# signal of whether the DINOv2 tiebreak fired or what its distances were —
# this field is structurally unavailable over the wire today. We keep the key
# in the schema (per spec) but it is always None; do not treat a None here as
# "tiebreak did not fire", it means "not observable from this harness."

def _parse_ocr_diag(diag):
    """diagnostics['ocr'] is a Python dict STR (see api_routes.py:612-616's
    dict-comprehension: every non-numeric diag value is str()'d, including the
    nested ocr dict) — literal_eval it back into a real dict."""
    if not isinstance(diag, dict):
        return {}
    raw = diag.get("ocr")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = ast.literal_eval(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def post_camera(session_post, url, fn, path, mime, headers):
    with open(path, "rb") as f:
        files = {"image": (fn, f, mime)}
        data = {"include_diagnostics": "true"}
        try:
            r = session_post(url, files=files, data=data, headers=headers, timeout=60)
        except Exception as e:
            return {"final_sku": "", "score": None, "status": f"EXC:{e}",
                     "ocr_confirmed": False, "ocr_text": None, "dinov2_fired": None,
                     "accept": False, "top5": []}

    if r.status_code != 200 and r.status_code != 402:
        return {"final_sku": "", "score": None, "status": f"HTTP_{r.status_code}",
                 "ocr_confirmed": False, "ocr_text": None, "dinov2_fired": None,
                 "accept": False, "top5": []}

    try:
        d = r.json()
    except Exception:
        return {"final_sku": "", "score": None, "status": "BAD_JSON",
                 "ocr_confirmed": False, "ocr_text": None, "dinov2_fired": None,
                 "accept": False, "top5": []}

    if "error" in d:
        return {"final_sku": "", "score": None, "status": f"ERROR:{d.get('reason', d.get('error'))}",
                 "ocr_confirmed": False, "ocr_text": None, "dinov2_fired": None,
                 "accept": False, "top5": []}

    matches = d.get("matches") or []
    low_conf = bool(d.get("low_confidence"))
    ocr_confirmed = bool(d.get("ocr_confirmed"))
    ocr_info = _parse_ocr_diag(d.get("diagnostics"))

    top5 = [{"rank": m.get("rank"), "sku": m.get("sku"), "score": m.get("score")} for m in matches[:5]]
    final_sku = matches[0].get("sku", "") if matches else ""
    score = matches[0].get("score") if matches else None
    accept = bool(matches) and not low_conf
    status = "ok" if matches and not low_conf else (d.get("reason") or ("low_confidence" if low_conf else "no_match"))

    return {
        "final_sku": final_sku,
        "score": score,
        "status": status,
        "ocr_confirmed": ocr_confirmed,
        "ocr_text": ocr_info.get("extracted"),
        "dinov2_fired": None,   # structurally unavailable over HTTP — see note above
        "accept": accept,
        "top5": top5,
    }


# ── UPLOAD leg — POST /match, scrape rendered results.html (or match.html on
#    failure). Field name is "query_image" (app.py:7313 _pick_first_file),
#    NOT "image" — that's the camera-leg field name only.

_RE_ERROR = re.compile(r'class="mi-flash err"[^>]*>(.*?)</div>', re.S)
_RE_SKU = re.compile(r'class="res-sku">(.*?)</div>', re.S)
_RE_NAME = re.compile(r'class="res-card-name">(.*?)</div>', re.S)
_RE_SET = re.compile(r'class="res-card-set">(.*?)</div>', re.S)
_RE_CONF = re.compile(r'class="res-conf-val[^"]*">\s*(\d+)%', re.S)


def post_upload(session_post, url, fn, path, mime, headers):
    with open(path, "rb") as f:
        files = {"query_image": (fn, f, mime)}
        try:
            r = session_post(url, files=files, headers=headers, timeout=60)
        except Exception as e:
            return {"final_sku": "", "name": None, "set": None, "score": None,
                     "source": "scraped", "status": f"EXC:{e}"}

    if r.status_code != 200:
        return {"final_sku": "", "name": None, "set": None, "score": None,
                 "source": "scraped", "status": f"HTTP_{r.status_code}"}

    body = r.text
    err_m = _RE_ERROR.search(body)
    if err_m:
        return {"final_sku": "", "name": None, "set": None, "score": None,
                 "source": "scraped", "status": f"ERROR:{err_m.group(1).strip()[:120]}"}

    skus = _RE_SKU.findall(body)
    names = _RE_NAME.findall(body)
    sets = _RE_SET.findall(body)
    confs = _RE_CONF.findall(body)

    if not skus:
        return {"final_sku": "", "name": None, "set": None, "score": None,
                 "source": "scraped", "status": "no_results_scraped"}

    return {
        "final_sku": skus[0].strip(),
        "name": names[0].strip() if names else None,
        "set": sets[0].strip() if sets else None,
        # NOTE: this is the rendered "Confidence %" badge (rank-relative display
        # value, app.py results.html ~line 231), NOT the raw cosine similarity
        # the camera leg's "score" is. Not directly comparable across legs.
        "score": float(confs[0]) if confs else None,
        "source": "scraped",
        "status": "ok",
    }


# ── Capture mode ──────────────────────────────────────────────────────────

def run_capture(args):
    import requests

    if not os.path.isdir(QUERY_DIR):
        raise SystemExit(f"Query dir not found: {QUERY_DIR}/")

    expected = load_groundtruth()
    bucket_of = load_buckets()

    api_key, cf_secret = resolve_credentials(args.from_modal_secret, args.direct_modal)
    if not api_key:
        print("WARNING: no API key (set GRAILSWEEP_API_KEY or use --from-modal-secret) "
              "— camera leg requests will likely 401.\n")

    base = args.target_url.rstrip("/")
    camera_url = base + "/api/v1/match"
    upload_url = base + "/match"

    camera_headers = {}
    if api_key:
        camera_headers["X-API-Key"] = api_key
    if args.access_code:
        camera_headers["Cookie"] = f"gs_access_code={args.access_code}"
    if cf_secret:
        camera_headers["X-CF-Proxy-Secret"] = cf_secret

    upload_headers = {}
    if args.access_code:
        upload_headers["Cookie"] = f"gs_access_code={args.access_code}"
    if cf_secret:
        upload_headers["X-CF-Proxy-Secret"] = cf_secret

    print(f"Target: camera={camera_url}  upload={upload_url}"
          f"{'  [+gs_access_code]' if args.access_code else ''}"
          f"{'  [+X-CF-Proxy-Secret]' if cf_secret else ''}")

    images = [fn for fn in sorted(os.listdir(QUERY_DIR))
              if os.path.splitext(fn)[1].lower() in _MIME]
    if args.limit and args.limit > 0:
        images = images[:args.limit]
        print(f"Canary mode: processing first {len(images)} image(s).")
    print()

    rows = {}
    for fn in images:
        ext = os.path.splitext(fn)[1].lower()
        mime = _MIME[ext]
        path = os.path.join(QUERY_DIR, fn)

        camera = post_camera(requests.post, camera_url, fn, path, mime, camera_headers)
        upload = post_upload(requests.post, upload_url, fn, path, mime, upload_headers)

        print(f"{fn}: camera={camera['final_sku']!r}({camera['status']}) "
              f"upload={upload['final_sku']!r}({upload['status']})")

        rows[fn] = {
            "bucket": bucket_of.get(fn, "untagged"),
            "expected": expected.get(fn, ""),
            "camera": camera,
            "upload": upload,
        }

    out = {
        "meta": {
            "label": args.label,
            "target_url": args.target_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "count": len(rows),
        },
        "rows": rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {args.out} ({len(rows)} rows)")

    n_cam_ok = sum(1 for r in rows.values() if r["camera"]["final_sku"])
    n_up_ok = sum(1 for r in rows.values() if r["upload"]["final_sku"])
    print(f"camera leg: {n_cam_ok}/{len(rows)} produced a final_sku")
    print(f"upload leg: {n_up_ok}/{len(rows)} produced a final_sku (scraped)")


# ── Diff mode ──────────────────────────────────────────────────────────────

def run_diff(path_a, path_b):
    with open(path_a, encoding="utf-8") as f:
        a = json.load(f)
    with open(path_b, encoding="utf-8") as f:
        b = json.load(f)

    rows_a, rows_b = a["rows"], b["rows"]
    common = sorted(set(rows_a) & set(rows_b))
    only_a = sorted(set(rows_a) - set(rows_b))
    only_b = sorted(set(rows_b) - set(rows_a))

    print(f"=== EQUIVALENCE DIFF: {a['meta']['label']!r} ({path_a}) vs {b['meta']['label']!r} ({path_b}) ===")
    print(f"common filenames: {len(common)}  only-in-A: {len(only_a)}  only-in-B: {len(only_b)}")
    if only_a:
        print(f"  only in A: {only_a}")
    if only_b:
        print(f"  only in B: {only_b}")

    diffs = []  # (fn, leg, before, after, bucket)
    for fn in common:
        ra, rb = rows_a[fn], rows_b[fn]
        bucket = ra.get("bucket", "untagged")
        cam_a, cam_b = ra["camera"]["final_sku"], rb["camera"]["final_sku"]
        up_a, up_b = ra["upload"]["final_sku"], rb["upload"]["final_sku"]
        if cam_a != cam_b:
            diffs.append((fn, "camera", cam_a, cam_b, bucket))
        if up_a != up_b:
            diffs.append((fn, "upload", up_a, up_b, bucket))

    total = len(common)
    mismatched_fns = {fn for fn, *_ in diffs}
    print(f"\ntotal matched (both legs identical): {total - len(mismatched_fns)}/{total}")
    print(f"mismatched (at least one leg differs): {len(mismatched_fns)}/{total}")

    print("\nper-bucket breakdown (mismatched filenames / total in bucket):")
    for bucket in BUCKET_ORDER:
        bucket_fns = [fn for fn in common if rows_a[fn].get("bucket") == bucket]
        bucket_mismatch = [fn for fn in bucket_fns if fn in mismatched_fns]
        print(f"  {bucket:<10} {len(bucket_mismatch)}/{len(bucket_fns)}")
    untagged_fns = [fn for fn in common if rows_a[fn].get("bucket") not in BUCKET_ORDER]
    if untagged_fns:
        untagged_mismatch = [fn for fn in untagged_fns if fn in mismatched_fns]
        print(f"  {'untagged':<10} {len(untagged_mismatch)}/{len(untagged_fns)}")

    if diffs:
        print(f"\nITEMISED DIFFERENCES ({len(diffs)} leg-level changes):")
        for fn, leg, before, after, bucket in diffs:
            print(f"  [{bucket}] {fn}  {leg}: {before!r} -> {after!r}")
    else:
        print("\nNo decision differences. Match.")

    return 0 if not diffs else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-url", help="Base URL, e.g. https://grailsweep.com")
    ap.add_argument("--direct-modal", action="store_true",
                     help="Add X-CF-Proxy-Secret header (read from Modal secret 'cf-proxy-secret').")
    ap.add_argument("--out", default="equivalence_run.json", help="Output JSON path (capture mode).")
    ap.add_argument("--label", default="run", help="Label stored in the output JSON's meta block.")
    ap.add_argument("--from-modal-secret", action="store_true",
                     help="Pull the scanner API key from Modal secret instead of GRAILSWEEP_API_KEY env var.")
    ap.add_argument("--access-code", metavar="CODE", default="",
                     help="Send gs_access_code=<CODE> as a Cookie on every request.")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                     help="Process only the first N images (canary/smoke test). 0 = all.")
    ap.add_argument("--diff", nargs=2, metavar=("FILE_A", "FILE_B"),
                     help="Diff mode: compare two capture JSON files instead of running a new capture.")
    args = ap.parse_args()

    if args.diff:
        sys.exit(run_diff(args.diff[0], args.diff[1]))

    if not args.target_url:
        ap.error("--target-url is required unless --diff is used")
    run_capture(args)


if __name__ == "__main__":
    main()
