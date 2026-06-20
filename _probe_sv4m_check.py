"""
_probe_sv4m_check.py — read-only Step 3 proof: did jpn-sv4m-001's profile.json
on the live volume actually get the price written, and does the resolver
return the right GBP value? Run with: modal run _probe_sv4m_check.py
"""

import json
import os
import sys

import modal

sys.path.insert(0, "/app")
from modal_config import vol

app = modal.App("grailsweep-probe-sv4m")


def _extract_gbp_from_profile(profile):
    """Verbatim copy of app.py:6367 — kept here so this probe doesn't need
    the full app.py import (CLIP/DINOv2/PaddleOCR cold start)."""
    if not profile:
        return None
    prices = profile.get("prices") if isinstance(profile, dict) else None
    if not prices:
        return None
    for src, sdata in prices.items():
        if "ebay" in src.lower() or "amazon" in src.lower():
            continue
        if not isinstance(sdata, dict):
            continue
        for _var, vdata in sdata.items():
            if isinstance(vdata, dict):
                price = vdata.get("market") or vdata.get("mid") or vdata.get("trend") or vdata.get("avg_sell")
            else:
                price = vdata
            if price:
                mult = 0.86 if "cardmarket" in src else 0.79
                return round(float(price) * mult, 2)
    return None


@app.function(volumes={"/modal_data": vol}, timeout=60)
def probe():
    path = "/modal_data/CardsDB/pokemon/jpn-sv4m-001/profile.json"
    with open(path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    print("profile.json prices block:", flush=True)
    print(json.dumps(profile.get("prices"), indent=2), flush=True)
    print("cardmarket_id:", profile.get("cardmarket_id"), flush=True)
    print("prices_updated:", profile.get("prices_updated"), flush=True)

    gbp = _extract_gbp_from_profile(profile)
    print(f"_extract_gbp_from_profile() -> {gbp}", flush=True)


@app.local_entrypoint()
def main():
    probe.remote()
