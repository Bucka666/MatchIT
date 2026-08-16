"""
refresh_onepiece_dotgg_prices.py — Daily refresh of TCGPlayer (USD) and
Cardmarket (EUR) prices for One Piece cards via dotgg.gg's public
card-data API.

Replaces refresh_onepiece_prices.py (JustTCG) as the daily cron source —
see backfill_onepiece_dotgg_prices.py's docstring for why. Mirrors that
file's standalone-script convention (helpers copied rather than
cross-imported) and refresh_onepiece_prices.py's calling shape:
refresh_onepiece_dotgg_prices(db_root, dry_run) -> stats dict, lazy-
imported and called from matchit_modal.py's scheduled cron. No api_key
parameter — dotgg needs none, unlike the JustTCG version this replaces.

Refresh (not backfill) semantics: re-fetches cards whose prices_updated
timestamp is missing or >24h old, not just cards with no price at all --
this is a recurring daily cron, prices should stay current. Since dotgg
returns the whole catalog in one unauthenticated GET (no pagination, no
rate limit observed), there's no reason to skip any card by cost/quota --
staleness is checked purely so a full local write pass isn't repeated on
data that hasn't changed since yesterday.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import requests

_DOTGG_URL = "https://api.dotgg.gg/cgfw/getcards?game=onepiece&mode=indexed"
_STALE_AFTER = timedelta(hours=24)


def _fetch_dotgg_prices() -> dict:
    """Single unauthenticated GET for the entire One Piece catalog. Returns
    {"{SET}-{NUM}": {"usd": float|None, "eur": float|None}}, keyed exactly
    as dotgg's own `id` field (matches our profiles' api_id verbatim)."""
    r = requests.get(_DOTGG_URL, headers={"User-Agent": "GrailSweep/1.0 contact@grailsweep.com"}, timeout=30)
    r.raise_for_status()
    data = r.json()

    names = data.get("names", [])
    rows = data.get("data", [])
    id_idx = names.index("id")
    price_idx = names.index("price")
    cmprice_idx = names.index("cmPrice")

    def _to_float(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    prices = {}
    for row in rows:
        card_id = row[id_idx]
        usd = _to_float(row[price_idx])
        eur = _to_float(row[cmprice_idx])
        if usd is not None or eur is not None:
            prices[card_id] = {"usd": usd, "eur": eur}

    print(f"[OP-DOTGG-FETCH] {len(rows)} rows, {len(prices)} with a usable price", flush=True)
    return prices


def _is_stale(profile: dict) -> bool:
    """True if prices_updated is missing, unparseable, or >24h old."""
    ts = profile.get("prices_updated")
    if not ts:
        return True
    try:
        updated = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return True
    return (datetime.utcnow() - updated) > _STALE_AFTER


def refresh_onepiece_dotgg_prices(db_root: Path, dry_run: bool = False) -> dict:
    """
    Refresh TCGPlayer (USD) and Cardmarket (EUR) prices for One Piece
    cards via dotgg.gg. Re-fetches cards whose existing price is missing
    or >24h stale (refresh semantics, not one-time backfill -- see module
    docstring). Flat write, matching the existing pattern:
        profile["prices"]["tcgplayer"] = {"market": price}
        profile["prices"]["cardmarket"] = {"avg_sell": price}
    """
    onepiece_dir = Path(db_root) / "onepiece"
    if not onepiece_dir.exists():
        return {"error": str(onepiece_dir)}

    stats = {
        "cards_checked": 0, "priced": 0,
        "skipped_fresh": 0, "no_match": 0, "errors": 0,
    }

    all_prices = _fetch_dotgg_prices()

    for folder in sorted(onepiece_dir.iterdir()):
        profile_path = folder / "profile.json"
        if not profile_path.exists():
            continue

        stats["cards_checked"] += 1
        try:
            with open(profile_path, encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:
            stats["errors"] += 1
            continue

        if not _is_stale(profile):
            stats["skipped_fresh"] += 1
            continue

        api_id = str(profile.get("api_id") or "").strip()
        entry = all_prices.get(api_id)
        if entry is None:
            stats["no_match"] += 1
            continue

        if not dry_run:
            prices = profile.setdefault("prices", {})
            if entry["usd"] is not None:
                prices["tcgplayer"] = {"market": entry["usd"]}
            if entry["eur"] is not None:
                prices["cardmarket"] = {"avg_sell": entry["eur"]}
            profile["prices_updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)

        stats["priced"] += 1

    return stats
