# run_server_groundtruth.py
# READ-ONLY: only calls the existing public /api/v1/match endpoint.
# Does not touch CardsDB, Modal volumes, or any prod state.
#
# Produces groundtruth.csv: the trusted server's top match per query image.
# These labels become the "answer key" for the on-device MobileCLIP test.
#
# Usage:
#   # default: read key from env var
#   set GRAILSWEEP_API_KEY=...   (PowerShell: $env:GRAILSWEEP_API_KEY="...")
#   python run_server_groundtruth.py
#
#   # or: pull the scanner key straight from the Modal secret, never touching
#   # your shell, disk, or stdout:
#   python run_server_groundtruth.py --from-modal-secret
#
import os
import csv
import argparse
import modal
# NOTE: `requests` is imported lazily inside main() (where it's used), NOT at
# module top-level. The ephemeral Modal function below imports this whole module
# in the cloud container to locate _read_scanner_key; that container runs the
# default (pure-stdlib) image, so a top-level `import requests` would crash it
# with ModuleNotFoundError. Keeping it local keeps the container on stdlib only.

QUERY_DIR = "test_queries"
# Prod match endpoint (grailsweep.com -> Cloudflare Worker -> Modal).
API_URL   = "https://grailsweep.com/api/v1/match"
# Direct Modal origin (bypasses Cloudflare). app=matchit-api, wsgi fn=serve,
# workspace=c-a-buckley => standard Modal web URL pattern. Requires the
# X-CF-Proxy-Secret header, since app.py blocks direct origin access (403)
# unless that header matches CF_PROXY_SECRET.
DIRECT_MODAL_URL = "https://c-a-buckley--matchit-api-serve.modal.run/api/v1/match"
OUT       = "groundtruth.csv"

# Modal secret + key-name that hold the scanner API key (see api_routes.py).
_SECRET_NAME = "external-api-credentials"
_ENV_IN_SECRET = "GRAILSWEEP_API_KEYS"   # JSON dict {client_name: key}
_SCANNER_KEY_NAME = "grailsweep_scanner"

# Modal secret + env-var that hold the Cloudflare proxy secret (see app.py).
_CF_SECRET_NAME = "cf-proxy-secret"
_CF_ENV_IN_SECRET = "CF_PROXY_SECRET"

# Ephemeral Modal app + function defined at MODULE scope (Modal requires global
# scope for @app.function unless serialized=True). Only used by the
# --from-modal-secret path; harmless to define when the env-var path is used.
_key_app = modal.App("groundtruth-key-fetch")


@_key_app.function(secrets=[modal.Secret.from_name(_SECRET_NAME)])
def _read_scanner_key():
    """Runs in the cloud: read the secret, return ONLY the scanner key string.

    The value is the return payload to the caller — never printed/logged here.
    """
    import os as _os, json as _json
    raw = _os.environ.get(_ENV_IN_SECRET, "")
    d = _json.loads(raw)
    return d[_SCANNER_KEY_NAME]


@_key_app.function(secrets=[modal.Secret.from_name(_CF_SECRET_NAME)])
def _read_cf_proxy_secret():
    """Runs in the cloud: return ONLY the CF proxy secret string.

    Returned as the payload to the caller — never printed/logged here.
    """
    import os as _os
    return _os.environ.get(_CF_ENV_IN_SECRET, "")

# Server already hard-gates matches below this score (returns matches: []).
# We additionally exclude anything under SCORE_FLOOR from the on-device test
# so the comparison only runs where the server is genuinely confident.
SCORE_FLOOR = 0.65

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",  ".webp": "image/webp",
}


def fetch_secrets_from_modal(want_key, want_cf):
    """Return (scanner_key, cf_proxy_secret) read from Modal secrets in-cloud.

    Invokes the module-level reader functions with .remote() inside a single
    `with _key_app.run()`. No deploy and no `modal run` CLI needed. Either
    value is None if not requested. Both are held only in local variables here
    — never printed, written, or logged.
    """
    key = cf = None
    with _key_app.run():
        if want_key:
            key = _read_scanner_key.remote()
        if want_cf:
            cf = _read_cf_proxy_secret.remote()
    return key, cf


def parse_match(d):
    """Pull (sku, score, status) out of the real /api/v1/match response.

    Real shape (api_routes.py):
      success:        {"matches": [{"rank":1,"sku":...,"score":...}, ...],
                       "low_confidence": false, "ocr_confirmed": true, ...}
      low confidence: {"matches": [], "low_confidence": true,
                       "reason": "score_too_low", ...}
      error:          {"error": "..."}  (non-200)
    """
    if not isinstance(d, dict):
        return "", "", "BAD_JSON"
    if "error" in d:
        return "", "", f"ERROR:{d.get('error')}"
    matches = d.get("matches") or []
    if not matches:
        reason = d.get("reason") or ("low_confidence" if d.get("low_confidence") else "no_match")
        return "", "", reason
    top = matches[0]
    sku   = top.get("sku", "")
    score = top.get("score", "")
    # status flags worth keeping for the exclusion step / audit trail
    flags = []
    if d.get("ocr_confirmed"):
        flags.append("ocr_confirmed")
    if d.get("low_confidence"):
        flags.append("low_confidence")
    return sku, score, ",".join(flags) if flags else "ok"


def resolve_credentials(from_modal_secret, direct_modal):
    """Return (api_key, cf_secret), both in memory only.

    api_key: from Modal secret if --from-modal-secret, else GRAILSWEEP_API_KEY.
    cf_secret: the CF proxy secret, only when --direct-modal (else None).
    A single Modal session fetches whatever is needed from the cloud.
    """
    api_key = None if from_modal_secret else os.environ.get("GRAILSWEEP_API_KEY", "")
    cf_secret = None

    if from_modal_secret or direct_modal:
        what = []
        if from_modal_secret:
            what.append(f"scanner key from '{_SECRET_NAME}'")
        if direct_modal:
            what.append(f"CF proxy secret from '{_CF_SECRET_NAME}'")
        print("Fetching " + " + ".join(what) + " via Modal (not printed)...")
        key, cf = fetch_secrets_from_modal(want_key=from_modal_secret,
                                           want_cf=direct_modal)
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


def main():
    import requests  # local import: keeps the Modal container on pure stdlib

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--from-modal-secret", action="store_true",
        help=f"Pull the '{_SCANNER_KEY_NAME}' key from Modal secret "
             f"'{_SECRET_NAME}' via an ephemeral function instead of the "
             f"GRAILSWEEP_API_KEY env var. Key is never printed/saved/logged.",
    )
    ap.add_argument(
        "--access-code", metavar="CODE", default="",
        help="Send gs_access_code=<CODE> as a Cookie on every request. A legacy "
             "code (config.json premium_codes) resolves to an exempt tier so the "
             "scan cap never blocks the run. Counter logic is unchanged.",
    )
    ap.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Process only the first N images (canary test). 0 = all.",
    )
    ap.add_argument(
        "--direct-modal", action="store_true",
        help="Hit the Modal origin directly (bypass Cloudflare). Swaps the base "
             "URL to the .modal.run endpoint and adds the X-CF-Proxy-Secret "
             "header (read from Modal secret 'cf-proxy-secret', never printed).",
    )
    args = ap.parse_args()

    if not os.path.isdir(QUERY_DIR):
        raise SystemExit(f"Query dir not found: {QUERY_DIR}/  (drop your capture images there)")

    api_key, cf_secret = resolve_credentials(args.from_modal_secret, args.direct_modal)
    if not api_key:
        print("WARNING: no API key (set GRAILSWEEP_API_KEY or use "
              "--from-modal-secret) — requests will likely 401.\n")

    # Assemble target URL + constant headers once (key/cf-secret in memory only).
    target_url = DIRECT_MODAL_URL if args.direct_modal else API_URL
    base_headers = {}
    if api_key:
        base_headers["X-API-Key"] = api_key
    if args.access_code:
        base_headers["Cookie"] = f"gs_access_code={args.access_code}"
    if cf_secret:
        base_headers["X-CF-Proxy-Secret"] = cf_secret
    print(f"Target: {target_url}"
          f"{'  [+gs_access_code cookie]' if args.access_code else ''}"
          f"{'  [+X-CF-Proxy-Secret]' if cf_secret else ''}")

    # Collect images, optionally capped for a canary run.
    images = [fn for fn in sorted(os.listdir(QUERY_DIR))
              if os.path.splitext(fn)[1].lower() in _MIME]
    if args.limit and args.limit > 0:
        images = images[:args.limit]
        print(f"Canary mode: processing first {len(images)} image(s).")
    print()

    rows = []
    for fn in images:
        ext = os.path.splitext(fn)[1].lower()
        path = os.path.join(QUERY_DIR, fn)
        with open(path, "rb") as f:
            files   = {"image": (fn, f, _MIME[ext])}
            headers = dict(base_headers)
            try:
                r = requests.post(target_url, files=files, headers=headers, timeout=60)
                if r.status_code != 200:
                    sku, score, status = "", "", f"HTTP_{r.status_code}"
                    # try to surface server error message
                    try:
                        status += ":" + str(r.json().get("error", ""))[:80]
                    except Exception:
                        pass
                else:
                    sku, score, status = parse_match(r.json())
            except Exception as e:
                sku, score, status = "", "", f"EXC:{e}"
        print(f"{fn}: {sku!r} (score={score}) [{status}]")
        rows.append({"filename": fn, "server_sku": sku, "score": score, "status": status})

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "server_sku", "score", "status"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {OUT} ({len(rows)} rows)")

    # Flag weak/failed server matches to EXCLUDE from the on-device test.
    def _is_weak(r):
        if not r["server_sku"]:
            return True
        try:
            return float(r["score"]) < SCORE_FLOOR
        except (TypeError, ValueError):
            return True

    weak = [r for r in rows if _is_weak(r)]
    print(f"\n{len(rows)} queries; {len(weak)} weak/failed server matches to exclude "
          f"(no sku or score < {SCORE_FLOOR}):")
    for r in weak:
        print(f"  EXCLUDE: {r['filename']}  [{r['status']}]")
    print(f"\n=> {len(rows) - len(weak)} queries will be used as ground truth.")


if __name__ == "__main__":
    main()
