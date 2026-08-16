"""
backfill_onepiece_dotgg_prices.py — Fill in TCGPlayer (USD) and Cardmarket
(EUR) prices for One Piece cards via dotgg.gg's public card-data API.

Replaces JustTCG as the One Piece price source. JustTCG's free-tier key
returned 401 INVALID_API_KEY account-wide starting 2026-08-15/16 (affecting
even its already-working JP Pokemon usage, confirmed live), and even before
that only ever priced 7 of 4,672 One Piece cards (0.15% coverage) in a full
run. dotgg.gg needs no API key, no set enumeration, and no pagination --
one unauthenticated GET returns the entire catalog -- and covers both
currencies:

    GET https://api.dotgg.gg/cgfw/getcards?game=onepiece&mode=indexed

Confirmed live (2026-08-16): 5,336 total rows, 100% of our 4,672 CardsDB
SKUs matched by the response's own `id` field (format "EB01-050" — the
same uppercase {SET}-{NUM} shape as our profiles' own api_id field, so
the join is exact, not fuzzy). Coverage on the matched set: 2,188 priced
in USD, 3,498 priced in EUR (Cardmarket), 3,894 priced in either.

Flat write, matching the pattern every other price writer in this repo
already uses for One Piece (build_onepiece_search_index.py's price
reader was updated alongside this to prefer Cardmarket, same as Pokemon):
    profile["prices"]["tcgplayer"] = {"market": price}
    profile["prices"]["cardmarket"] = {"avg_sell": price}
Either key is written only if dotgg actually returned a non-zero value for
it — a card priced in one currency but not the other keeps only the one
key, never a zero placeholder.

Only fills cards with no existing price (resume=True default), so a
partial/interrupted run or a re-run is safe.

Run:
    modal run backfill_onepiece_dotgg_prices.py                # writes to the volume
    modal run backfill_onepiece_dotgg_prices.py --dry-run       # fetch + match only, no writes
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import modal

VOLUME_NAME = "matchit-data-v2"
_DOTGG_URL = "https://api.dotgg.gg/cgfw/getcards?game=onepiece&mode=indexed"

vol = modal.Volume.from_name(VOLUME_NAME)
app = modal.App("matchit-onepiece-dotgg-price-backfill")
image = modal.Image.debian_slim(python_version="3.11").pip_install("requests")


def _fetch_dotgg_prices() -> dict:
    """Single unauthenticated GET for the entire One Piece catalog. Returns
    {"{SET}-{NUM}": {"usd": float|None, "eur": float|None}}, keyed exactly
    as dotgg's own `id` field (matches our profiles' api_id verbatim)."""
    import requests

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


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=1800,  # ceiling, not a target — dotgg's own fetch is a single
    # fast request, but reading+conditionally writing 4,672 profile.json
    # files over the Modal network volume is what actually takes time.
    # 300s wasn't enough (confirmed live 2026-08-16, FunctionTimeoutError
    # partway through); the old JustTCG version used 7200s for the same
    # per-profile I/O pattern, sized for its own rate-limit backoffs which
    # don't apply here, so 1800s is a generous middle ground.
)
def backfill_onepiece_dotgg_prices(dry_run: bool = False, resume: bool = True) -> dict:
    vol.reload()
    onepiece_dir = Path("/modal_data/CardsDB/onepiece")
    if not onepiece_dir.exists():
        return {"error": str(onepiece_dir)}

    stats = {
        "cards_checked": 0, "priced": 0,
        "skipped_existing": 0, "no_match": 0, "errors": 0,
    }

    all_prices = _fetch_dotgg_prices()
    print(f"[OP-DOTGG-BACKFILL] Total distinct priced identities: {len(all_prices)}", flush=True)

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

        if resume:
            existing = profile.get("prices") or {}
            if (existing.get("tcgplayer") or {}).get("market") or (existing.get("cardmarket") or {}).get("avg_sell"):
                stats["skipped_existing"] += 1
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

    if not dry_run and stats["priced"] > 0:
        vol.commit()

    return stats


@app.local_entrypoint()
def run(dry_run: bool = False, resume: bool = True):
    print(f"Starting One Piece dotgg.gg price backfill (dry_run={dry_run}, resume={resume})...", flush=True)
    result = backfill_onepiece_dotgg_prices.remote(dry_run=dry_run, resume=resume)
    print("\n=== ONE PIECE DOTGG BACKFILL RESULT ===", flush=True)
    for k, v in result.items():
        print(f"  {k}: {v}", flush=True)
