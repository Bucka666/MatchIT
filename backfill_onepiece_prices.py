"""
backfill_onepiece_prices.py — Fill in TCGPlayer (USD) prices for One Piece
cards via JustTCG.

Mirrors scrape_pokemon_jpn.py's backfill_justtcg_prices pattern, but simpler
in two ways discovered while building this:

1. One Piece cards carry their official set-code+number directly in
   JustTCG's `number` field (e.g. "OP04-014"), so cards are matched globally
   by their own parsed identity rather than by which JustTCG "set" bucket
   happens to list them (which can be a promotional/pre-release variant
   listing, not our own set boundaries -- confirmed live: a Monkey.D.Luffy
   OP04-014 card was listed under JustTCG set "Kingdoms of Intrigue
   Pre-Release Cards", not an "OP-04" set).

2. There's no per-set enumeration step at all (unlike Pokemon's
   _get_justtcg_set_map + _resolve_justtcg_set_id): GET /v2/sets?game=
   one-piece-card-game returns a hard 404 ("Requested function was not
   found") -- confirmed live, not transient, not worth retrying. But
   GET /v2/cards?game=one-piece-card-game paginates the ENTIRE catalog
   directly with no set= filter needed (also confirmed live, HTTP 200).
   So this just walks that one global paginated endpoint.

Verified live (2026-08-15): JustTCG's One Piece game slug is
"one-piece-card-game" -- every plausible-sounding alternative (onepiece,
one-piece, one_piece, onepiece-card-game) returns HTTP 400. The /cards
endpoint has no `total` field for this query, so pagination stops on the
first empty page, not a known count. Some entries have number="N/A"
(sealed products etc.) and are skipped.

Flat write, matching the existing Pokemon pattern (not the full
per-condition/price-history detail the API actually returns):
    profile["prices"]["tcgplayer"] = {"market": price}

Only fills cards with no existing tcgplayer.market price (resume=True
default), so a partial/interrupted run or a re-run is safe.

Run:
    modal run backfill_onepiece_prices.py                 # writes to the volume
    modal run backfill_onepiece_prices.py --dry-run       # fetch + match only, no writes
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import modal

VOLUME_NAME = "matchit-data-v2"
_JUSTTCG_BASE = "https://api.justtcg.com/v2"
_JUSTTCG_GAME = "one-piece-card-game"

vol = modal.Volume.from_name(VOLUME_NAME)
app = modal.App("matchit-onepiece-price-backfill")
image = modal.Image.debian_slim(python_version="3.11").pip_install("requests")


def _justtcg_get_with_backoff(url: str, headers: dict, timeout: int, max_retries: int = 5):
    """GET with Retry-After-aware backoff on 429. Inlined verbatim from
    scrape_pokemon_jpn.py (with one fix, see below) rather than imported --
    this script only mounts itself (not the full repo), so a cross-file
    import would need its own add_local_file wiring for one small, stable,
    side-effect-free helper. Returns the parsed JSON dict, or None if
    retries are exhausted / the error is non-retryable.

    Fix vs. the original: 400/401/403/404 fail immediately instead of
    retrying. The original's except clause catches HTTPError (raised by
    raise_for_status()) the same as a transient network error, which cost
    a real ~4-4.5 minutes here retrying a confirmed-dead endpoint
    (GET /v2/sets?game=one-piece-card-game -- a hard 404, "Requested
    function was not found", not a rate-limit or blip). A 4xx client error
    other than 429 will not resolve itself by waiting."""
    import requests

    _FALLBACK_DELAY = 60  # seconds — covers a full per-minute rate-limit window
    _NON_RETRYABLE = {400, 401, 403, 404}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 429:
                if attempt < max_retries - 1:
                    retry_after = r.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = float(retry_after)
                            print(f"[JUSTTCG] 429 — honoring Retry-After: {delay}s (attempt {attempt + 1}/{max_retries})", flush=True)
                        except ValueError:
                            delay = _FALLBACK_DELAY
                            print(f"[JUSTTCG] 429 — unparseable Retry-After '{retry_after}', using fallback {delay}s (attempt {attempt + 1}/{max_retries})", flush=True)
                    else:
                        delay = _FALLBACK_DELAY
                        print(f"[JUSTTCG] 429 — no Retry-After header, using fallback {delay}s (attempt {attempt + 1}/{max_retries})", flush=True)
                    time.sleep(delay)
                    continue
                print(f"[JUSTTCG] Exhausted {max_retries} retries (429): {url}", flush=True)
                return None
            if r.status_code in _NON_RETRYABLE:
                print(f"[JUSTTCG] {r.status_code} (non-retryable), giving up immediately: {url} — {r.text[:200]}", flush=True)
                return None
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                print(f"[JUSTTCG] Request error, retrying in {_FALLBACK_DELAY}s (attempt {attempt + 1}/{max_retries}): {e}", flush=True)
                time.sleep(_FALLBACK_DELAY)
                continue
            print(f"[JUSTTCG] Exhausted {max_retries} retries (error): {url} — {e}", flush=True)
            return None
    return None


def _fetch_all_justtcg_onepiece_prices(api_key: str) -> dict:
    """
    Walk the entire JustTCG one-piece-card-game catalog in one global
    pagination (no set= filter -- see module docstring for why). Returns
    {"{set_id}-{card_number}": near_mint_price_usd}, e.g. {"op04-014": 56.18}.

    Keyed off each card's own `number` field (the official set-code+number,
    e.g. "OP04-014"), not off which JustTCG set bucket it came from. Matches
    identifier_lookup.json's onepiece key format exactly
    (build_identifier_lookup.py: f"{set_id}-{card_number}".lower()).
    """
    prices = {}
    offset = 0
    limit = 20
    headers = {"x-api-key": api_key}
    pages = 0

    while True:
        url = f"{_JUSTTCG_BASE}/cards?game={_JUSTTCG_GAME}&limit={limit}&offset={offset}"
        data = _justtcg_get_with_backoff(url, headers, timeout=12)
        if data is None:
            print(f"[OP-PRICE-FETCH] Giving up at offset {offset} after retries", flush=True)
            break

        cards = data.get("data", [])
        if not cards:
            break

        for card in cards:
            number = (card.get("number") or "").strip()
            if "-" not in number:
                continue  # not a "SETCODE-NUM" identity (sealed products, "N/A" etc.)

            key = number.lower()

            for variant in card.get("variants", []):
                if variant.get("condition") != "Near Mint":
                    continue
                markets = variant.get("markets") or []
                if not markets:
                    continue
                raw = markets[0].get("price")
                if raw:
                    prices[key] = round(float(raw), 2)  # already USD, confirmed live
                break

        pages += 1
        offset += limit
        if pages % 25 == 0:
            print(f"[OP-PRICE-FETCH] {pages} pages, offset={offset}, {len(prices)} priced so far", flush=True)
        time.sleep(0.6)  # 10 req/min on free tier; relax on paid

        if offset >= 20000:  # safety ceiling -- no known total for this query
            print(f"[OP-PRICE-FETCH] Hit safety ceiling at offset {offset}", flush=True)
            break

    print(f"[OP-PRICE-FETCH] Done: {pages} pages, {len(prices)} distinct priced identities", flush=True)
    return prices


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[modal.Secret.from_name("justtcg-credentials")],
    timeout=7200,
)
def backfill_onepiece_prices(dry_run: bool = False, resume: bool = True) -> dict:
    api_key = os.environ.get("JUSTTCG_API_KEY", "").strip()
    if not api_key:
        return {"error": "No JUSTTCG_API_KEY in environment"}

    vol.reload()
    onepiece_dir = Path("/modal_data/CardsDB/onepiece")
    if not onepiece_dir.exists():
        return {"error": str(onepiece_dir)}

    stats = {
        "cards_checked": 0, "priced": 0,
        "skipped_existing": 0, "no_match": 0, "errors": 0,
    }

    # ── Fetch all JustTCG one-piece pricing, keyed by "{set_id}-{num}" ──
    all_prices = _fetch_all_justtcg_onepiece_prices(api_key)
    print(f"[OP-PRICE-BACKFILL] Total distinct priced identities: {len(all_prices)}", flush=True)

    # ── Match against local CardsDB/onepiece profiles, write flat prices ──
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
            existing = (profile.get("prices") or {}).get("tcgplayer") or {}
            if existing.get("market"):
                stats["skipped_existing"] += 1
                continue

        set_id = str(profile.get("set_id") or "").strip().lower()
        card_number = str(profile.get("card_number") or "").strip().lower()
        if not set_id or not card_number:
            stats["no_match"] += 1
            continue

        price = all_prices.get(f"{set_id}-{card_number}")
        if price is None:
            stats["no_match"] += 1
            continue

        if not dry_run:
            prices = profile.setdefault("prices", {})
            prices["tcgplayer"] = {"market": price}
            profile["prices_updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)

        stats["priced"] += 1

    if not dry_run and stats["priced"] > 0:
        vol.commit()

    return stats


@app.local_entrypoint()
def run(dry_run: bool = False, resume: bool = True):
    print(f"Starting One Piece JustTCG price backfill (dry_run={dry_run}, resume={resume})...", flush=True)
    result = backfill_onepiece_prices.remote(dry_run=dry_run, resume=resume)
    print("\n=== ONE PIECE JUSTTCG BACKFILL RESULT ===", flush=True)
    for k, v in result.items():
        print(f"  {k}: {v}", flush=True)
