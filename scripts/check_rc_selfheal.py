"""
scripts/check_rc_selfheal.py — one-off, READ-ONLY verification for the
RevenueCat webhook self-heal fix (app.py _rc_find_entry /
_rc_find_or_create_entry, deployed as CACHE_NAME grailsweep-v130).

Does not write to subscriptions.json and does not deploy anything.

Usage:
    modal run scripts/check_rc_selfheal.py

Reports, in order:
    1. Every subscriptions.json entry with source == "revenuecat_ios".
    2. For each, whether it would PASS the exact check in
       /api/revenuecat/restore — by calling that route in-process via
       Flask's test client, so this reuses the real endpoint logic
       instead of re-deriving the status/expiry comparison by hand.
    3. Recent [REVENUECAT] lines from `modal app logs`, for context.
"""
import subprocess
import sys

import modal

sys.path.insert(0, ".")
from modal_config import vol, image

app = modal.App("gs-check-rc-selfheal")


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
    timeout=120,
)
def check():
    import os
    import sys as _sys
    from datetime import datetime

    os.chdir("/app")
    _sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"
    vol.reload()

    # Same boot sequence as serve_light() in matchit_modal.py — no model
    # warmup, this only needs the Flask app object and subscriptions.json.
    from matchit_modal import _fix_vertical_config, _fix_db_paths
    _fix_vertical_config()
    import app as _app_module  # noqa: F401  (import order matters — see matchit_modal.py)
    _fix_db_paths()

    from app import app as flask_app, _load_subs

    # ── Part 1 ──────────────────────────────────────────────────────────
    subs = _load_subs()
    rc_entries = [(code, e) for code, e in subs.items() if e.get("source") == "revenuecat_ios"]

    print("=" * 72)
    print(f"PART 1 — subscriptions.json entries with source == 'revenuecat_ios': {len(rc_entries)}")
    print("=" * 72)
    if not rc_entries:
        print("None found. Stopping — nothing to check against the restore endpoint.")
        return

    for code, e in rc_entries:
        print(f"  code={code}")
        print(f"    tier={e.get('tier')}  status={e.get('status')}  "
              f"expires_at={e.get('expires_at')}  "
              f"stripe_subscription_id={e.get('stripe_subscription_id')}")

    # ── Part 2 — reuse the REAL /api/revenuecat/restore logic by calling ─
    # it in-process, rather than re-deriving the status/expiry comparison.
    print()
    print("=" * 72)
    print("PART 2 — pass/fail against the live /api/revenuecat/restore endpoint")
    print("=" * 72)
    client = flask_app.test_client()
    for code, e in rc_entries:
        sid = e.get("stripe_subscription_id") or ""
        resp = client.post(
            "/api/revenuecat/restore",
            json={"app_user_id": sid, "original_app_user_id": ""},
            headers={"User-Agent": "Modal/check_rc_selfheal"},
        )
        data = resp.get_json(silent=True) or {}
        if data.get("found"):
            print(f"  code={code}  PASS  "
                  f"(endpoint returned code={data.get('code')} tier={data.get('tier')})")
            continue

        # found=false alone doesn't say why — the endpoint doesn't report a
        # reason. This block only explains the failure for readability; the
        # PASS/FAIL verdict itself came from the real endpoint call above.
        reasons = []
        status = e.get("status")
        if status != "active":
            reasons.append(f"status={status!r} != 'active'")
        expires = e.get("expires_at")
        if expires:
            try:
                if datetime.fromisoformat(expires) < datetime.utcnow():
                    reasons.append(f"expires_at={expires} is in the past")
            except Exception:
                reasons.append(f"expires_at={expires!r} unparsable")
        if not reasons:
            reasons.append("unclear — status/expiry look fine locally; check "
                            "stripe_subscription_id match or the restore "
                            "endpoint's rate limit")
        print(f"  code={code}  FAIL  reason: {'; '.join(reasons)}")


@app.local_entrypoint()
def main():
    check.remote()

    # ── Part 3 — recent [REVENUECAT] log lines, for context alongside the
    # entries above. Runs locally (not on Modal) since `modal app logs` is
    # a CLI-side operation, not something reachable from inside a function.
    print()
    print("=" * 72)
    print("PART 3 — recent [REVENUECAT] log lines (last ~20)")
    print("=" * 72)
    try:
        logs = subprocess.run(
            ["modal", "app", "logs", "matchit-api"],
            capture_output=True, text=True, timeout=60,
        )
        lines = [l for l in logs.stdout.splitlines() if "REVENUECAT" in l.upper()]
        if not lines:
            print("No [REVENUECAT] lines in the current log buffer "
                  "(buffer is small/recent-only — see CLAUDE.md gotcha).")
        else:
            for l in lines[-20:]:
                print(" ", l)
    except Exception as e:
        print(f"Could not fetch logs: {e}")
