"""
refresh_onepiece_prices.py — Daily refresh of TCGPlayer (USD) prices for
One Piece cards via JustTCG.

Mirrors refresh_en_prices.py's standalone-script convention (helpers copied
rather than cross-imported — see that file's own docstring for the same
convention) and scrape_pokemon_jpn.py's backfill_justtcg_prices calling
shape: refresh_onepiece_prices(db_root, api_key, dry_run) -> stats dict,
lazy-imported and called from matchit_modal.py's scheduled cron.

Two corrections versus a naive port of the Pokemon JustTCG pattern, both
confirmed live (2026-08-15) while building backfill_onepiece_prices.py:

1. GET /v2/sets?game=one-piece-card-game returns a hard 404 ("Requested
   function was not found") — not transient, never worth retrying. There
   is no per-set enumeration step here, unlike Pokemon's
   _get_justtcg_set_map + _resolve_justtcg_set_id. Instead,
   GET /v2/cards?game=one-piece-card-game paginates the ENTIRE catalog
   directly with no set= filter (confirmed HTTP 200) — this walks that.

2. One Piece cards carry their official set-code+number directly in each
   card's `number` field (e.g. "OP04-014"), so matching is a direct lookup
   against identifier_lookup.json's key shape
   (f"{set_id}-{card_number}".lower()), not fuzzy name/set matching.

Refresh (not backfill) semantics: re-fetches cards whose prices_updated
timestamp is missing or >24h old, not just cards with no price at all —
this is a recurring daily cron, prices should stay current, not just get
filled in once.

Stops cleanly on a non-retryable error (401/403/404, e.g. exhausted quota)
rather than raising — returns whatever was collected before the failure.
Confirmed live: JustTCG's free-tier key returned 401 INVALID_API_KEY after
~1,460 cards checked in a single run, consistent with a daily quota rather
than a broken key. A recurring cron must degrade gracefully on this, not
crash and skip the whole run.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

_JUSTTCG_BASE = "https://api.justtcg.com/v2"
_JUSTTCG_GAME = "one-piece-card-game"
_STALE_AFTER = timedelta(hours=24)


def _justtcg_get_with_backoff(url: str, headers: dict, timeout: int, max_retries: int = 5):
    """GET with Retry-After-aware backoff on 429; fails fast (no retry) on
    400/401/403/404 — those won't resolve by waiting. Blindly retrying them
    (scrape_pokemon_jpn.py's original behavior, which this was ported from)
    cost ~4-4.5 minutes live retrying a confirmed-dead endpoint during
    testing. Returns the parsed JSON dict, or None if retries are exhausted
    or the error is non-retryable."""
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
                        except ValueError:
                            delay = _FALLBACK_DELAY
                    else:
                        delay = _FALLBACK_DELAY
                    print(f"[JUSTTCG-OP] 429 — backing off {delay}s (attempt {attempt + 1}/{max_retries})", flush=True)
                    time.sleep(delay)
                    continue
                print(f"[JUSTTCG-OP] Exhausted {max_retries} retries (429): {url}", flush=True)
                return None
            if r.status_code in _NON_RETRYABLE:
                print(f"[JUSTTCG-OP] {r.status_code} (non-retryable), stopping: {url} — {r.text[:200]}", flush=True)
                return None
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                print(f"[JUSTTCG-OP] Request error, retrying in {_FALLBACK_DELAY}s (attempt {attempt + 1}/{max_retries}): {e}", flush=True)
                time.sleep(_FALLBACK_DELAY)
                continue
            print(f"[JUSTTCG-OP] Exhausted {max_retries} retries (error): {url} — {e}", flush=True)
            return None
    return None


def _fetch_all_justtcg_onepiece_prices(api_key: str) -> dict:
    """
    Walk the entire JustTCG one-piece-card-game catalog in one global
    pagination (no set= filter — see module docstring). Returns
    {"{set_id}-{card_number}": near_mint_price_usd}, e.g. {"op04-014": 56.18}.

    Stops (returns whatever was collected so far) on quota exhaustion or
    any other non-retryable error — never raises.
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
            print(f"[JUSTTCG-OP] Stopping at offset {offset} ({pages} pages, {len(prices)} priced so far)", flush=True)
            break

        cards = data.get("data", [])
        if not cards:
            break

        for card in cards:
            number = (card.get("number") or "").strip()
            if "-" not in number:
                continue  # sealed products, "N/A" etc.
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
            print(f"[JUSTTCG-OP] {pages} pages, offset={offset}, {len(prices)} priced so far", flush=True)
        time.sleep(0.6)  # 10 req/min on free tier; relax on paid

        if offset >= 20000:  # safety ceiling — no known total for this query
            print(f"[JUSTTCG-OP] Hit safety ceiling at offset {offset}", flush=True)
            break

    print(f"[JUSTTCG-OP] Done: {pages} pages, {len(prices)} distinct priced identities", flush=True)
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


def refresh_onepiece_prices(db_root: Path, api_key: str, dry_run: bool = False) -> dict:
    """
    Refresh TCGPlayer (USD) prices for One Piece cards via JustTCG.
    Re-fetches cards whose existing price is missing or >24h stale (refresh
    semantics, not one-time backfill — see module docstring). Flat write,
    matching the existing Pokemon JustTCG pattern:
        profile["prices"]["tcgplayer"] = {"market": price}
    """
    onepiece_dir = Path(db_root) / "onepiece"
    if not onepiece_dir.exists():
        return {"error": str(onepiece_dir)}

    stats = {
        "cards_checked": 0, "priced": 0,
        "skipped_fresh": 0, "no_match": 0, "errors": 0,
    }

    all_prices = _fetch_all_justtcg_onepiece_prices(api_key)

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

    return stats
