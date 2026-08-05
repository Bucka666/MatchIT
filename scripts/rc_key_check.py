"""
scripts/rc_key_check.py — one-off, READ-ONLY. Queries the client's TRUE
original-casing RevenueCat anonymous ID (mixed-case "$RCAnonymousID:"
prefix, lowercase hex — exactly as Purchases.getAppUserID() returned it
before our own .upper() normalization ever touched it), with the
X-Is-Sandbox: true header. Lightweight CPU-only image, no writes.

Usage:
    modal run scripts/rc_key_check.py
"""
import json
import os

import modal

app = modal.App("gs-rc-key-check")
image = modal.Image.debian_slim(python_version="3.11").pip_install("requests")

SUB_ID = "$RCAnonymousID:2b3150bb8f3d404b9374b0221c36121b"


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("revenuecat-api-key")],
    timeout=30,
)
def check():
    import requests

    key = os.environ.get("REVENUECAT_API_KEY")
    print(f"REVENUECAT_API_KEY set: {bool(key)}  length={len(key) if key else 0}")
    if not key:
        return

    print(f"id (true original casing): {SUB_ID}")
    url = f"https://api.revenuecat.com/v1/subscribers/{SUB_ID}"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "X-Is-Sandbox": "true",
        },
        timeout=10,
    )
    print(f"HTTP status: {resp.status_code}")
    data = resp.json()
    subscriber = data.get("subscriber", {})
    print(f"first_seen={subscriber.get('first_seen')}  last_seen={subscriber.get('last_seen')}")
    print(f"original_purchase_date={subscriber.get('original_purchase_date')}")
    print(f"original_application_version={subscriber.get('original_application_version')}")
    print("entitlements:")
    print(json.dumps(subscriber.get("entitlements", {}), indent=2))
    print("subscriptions:")
    print(json.dumps(subscriber.get("subscriptions", {}), indent=2))


@app.local_entrypoint()
def main():
    check.remote()
