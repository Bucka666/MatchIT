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
timestamp is missing or >48h old, not just cards with no price at all --
this is a recurring daily cron, prices should stay current. Since dotgg
returns the whole catalog in one unauthenticated GET (no pagination, no
rate limit observed), there's no reason to skip any card by cost/quota --
staleness is checked purely so a full local write pass isn't repeated on
data that hasn't changed since yesterday.

STALENESS INDEX (2026-09-07, fixes the 1800s timeout): the loop used to
open all ~4,672 profile.json files unconditionally just to read
prices_updated -- confirmed live that a read-only pass over the same
files couldn't even finish inside Modal's 300s default, independent of
any writing. onepiece_price_staleness.json is a small {sku: last_checked}
cache that lets the loop skip already-fresh cards WITHOUT ever opening
their profile.json. It is a performance cache only, never the source of
truth: profile.json's own prices_updated field remains authoritative, and
a sku missing from the index (new card, or a lost/corrupt index file) is
always treated as stale -- worst case is one extra full-cost run, never
permanently wrong pricing.

Update policy for the index (per outcome of checking a card against
dotgg this run):
  - priced      -> index updated to now (profile.json's own
                   prices_updated agrees).
  - no_change   -> index updated to now. dotgg was checked and confirmed
                   identical to what we already have; there is no reason
                   to re-check again before the staleness window expires.
                   NOTE this deliberately diverges from profile.json's own
                   prices_updated, which is only bumped on an actual
                   write -- without this, a card whose price never moves
                   would stay "stale" by the profile's own timestamp
                   forever and get re-opened every single run. The index
                   is free to treat "confirmed unchanged" as equivalent to
                   "confirmed fresh" since it exists purely to avoid
                   redundant re-checks, not to record price history.
  - no_match    -> index updated to now. dotgg's catalog itself doesn't
                   churn daily; if coverage for this card appears later,
                   it's picked up automatically once the window expires.
  - error       -> index NOT updated. We couldn't even read the profile,
                   so we know nothing about this card's real state --
                   caching that as "checked" would let a transient read
                   failure hide a genuinely stale card for the whole
                   window.

Per-card work runs through a ThreadPoolExecutor(max_workers=8), matching
refresh_en_prices.py / scrape_pokemon_jpn.py::refresh_cardmarket_prices's
existing convention on this same volume. The worker function
(_refresh_one) does ONLY pure per-card I/O -- open profile.json, look up
the dotgg match (already an O(1) dict lookup via all_prices.get(), see
_fetch_dotgg_prices below -- untouched, was never the bottleneck),
conditionally write, and return a small result. It touches no shared
state. The main thread, iterating results via as_completed(), is the ONLY
place that increments stats, updates the staleness index, and decides
when to checkpoint -- avoiding the lost-update/race conditions a naive
"just wrap the existing loop in a thread pool" change would introduce.

Every _CHECKPOINT_EVERY (300) cards processed, and once more at the very
end, the main thread persists the staleness index AND calls the caller's
commit_cb (if given) together, so the two files never drift relative to
each other and a timeout-killed run keeps whatever progress it made
instead of losing the whole run (the previous single vol.commit() sat
after this function returned, so a timeout kill committed nothing at
all).
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import requests

_DOTGG_URL = "https://api.dotgg.gg/cgfw/getcards?game=onepiece&mode=indexed"
_STALE_AFTER = timedelta(hours=48)
_MAX_WORKERS = 8
_CHECKPOINT_EVERY = 300

_STALENESS_PATH = (
    "/modal_data/onepiece_price_staleness.json"
    if os.path.exists("/modal_data")
    else "onepiece_price_staleness.json"
)


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


def _load_staleness_index() -> dict:
    """{sku: last_checked_iso} -- see module docstring. Never raises: a
    missing/corrupt file just means every card starts out stale, which is
    correct (self-healing), not an error."""
    try:
        with open(_STALENESS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_staleness_index(index: dict) -> None:
    """Atomic write (tmp + os.replace) so a kill mid-write can never leave
    a truncated/corrupt index -- worst case the replace never happens and
    the previous good version (or none) survives, both self-healing."""
    tmp = _STALENESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    os.replace(tmp, _STALENESS_PATH)


def _index_says_fresh(last_checked) -> bool:
    """True only for a valid timestamp within _STALE_AFTER. Missing,
    unparseable, or too old -> stale (self-healing default)."""
    if not last_checked:
        return False
    try:
        checked = datetime.strptime(last_checked, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return False
    return (datetime.utcnow() - checked) <= _STALE_AFTER


def _refresh_one(profile_path: Path, all_prices: dict, dry_run: bool) -> dict:
    """Pure per-card I/O for one folder -- open, match, conditionally
    write. Returns {"outcome": ..., "timestamp": ... | None}. Touches NO
    shared state (no stats dict, no staleness dict, no vol.commit) so it
    is safe to run from any worker thread with no locking. Never raises:
    every failure anywhere in this function (read, parse, write) is
    caught and reported as an "error" outcome so one bad profile.json
    can't take down the run."""
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)

        api_id = str(profile.get("api_id") or "").strip()
        entry = all_prices.get(api_id)
        if entry is None:
            return {"outcome": "no_match", "timestamp": None}

        if dry_run:
            # Matches original dry_run semantics: report "would touch",
            # never diffs old vs new (that only happens on a real write).
            return {"outcome": "priced", "timestamp": None}

        existing = dict(profile.get("prices", {}))
        new_prices = dict(existing)
        if entry["usd"] is not None:
            new_prices["tcgplayer"] = {"market": entry["usd"]}
        if entry["eur"] is not None:
            new_prices["cardmarket"] = {"avg_sell": entry["eur"]}

        if new_prices == existing:
            return {"outcome": "no_change", "timestamp": None}

        profile["prices"] = new_prices
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        profile["prices_updated"] = now
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)

        return {"outcome": "priced", "timestamp": now}
    except Exception as e:
        return {"outcome": "error", "timestamp": None, "error": str(e)}


def refresh_onepiece_dotgg_prices(
    db_root: Path,
    dry_run: bool = False,
    commit_cb: Optional[Callable[[], None]] = None,
) -> dict:
    """
    Refresh TCGPlayer (USD) and Cardmarket (EUR) prices for One Piece
    cards via dotgg.gg. Re-fetches cards the staleness index says are
    missing or >48h checked (refresh semantics, not one-time backfill --
    see module docstring). Flat write, matching the existing pattern:
        profile["prices"]["tcgplayer"] = {"market": price}
        profile["prices"]["cardmarket"] = {"avg_sell": price}

    commit_cb: optional zero-arg callable invoked from the main thread at
    each checkpoint (every _CHECKPOINT_EVERY cards, and once more at the
    end) so the caller (matchit_modal.py) can run vol.commit() in
    lockstep with the staleness-index save below -- kept as a callback
    rather than importing modal.Volume directly here so this function
    stays a plain Path-in/dict-out function, same as before. No-op if
    omitted. Never called with dry_run=True (no writes to commit).
    """
    onepiece_dir = Path(db_root) / "onepiece"
    if not onepiece_dir.exists():
        return {"error": str(onepiece_dir)}

    stats = {
        # cards_checked: opened via the thread pool (stale-or-unknown only).
        # skipped_fresh: short-circuited by the staleness index, never opened.
        # cards_checked + skipped_fresh == total profile.json-bearing folders.
        "cards_checked": 0, "priced": 0,
        "skipped_fresh": 0, "skipped_no_change": 0, "no_match": 0, "errors": 0,
    }

    all_prices = _fetch_dotgg_prices()
    staleness = _load_staleness_index()

    # Staleness short-circuit BEFORE any thread work: only folders the
    # index doesn't already vouch for get opened at all.
    targets = []
    for folder in sorted(onepiece_dir.iterdir()):
        profile_path = folder / "profile.json"
        if not profile_path.exists():
            continue
        sku = folder.name
        if _index_says_fresh(staleness.get(sku)):
            stats["skipped_fresh"] += 1
            continue
        targets.append((sku, profile_path))

    print(f"[OP-DOTGG-REFRESH] {len(targets)} stale-or-unknown of "
          f"{len(targets) + stats['skipped_fresh']} total folders "
          f"({stats['skipped_fresh']} skipped via staleness index, "
          f"never opened)", flush=True)

    processed = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_refresh_one, profile_path, all_prices, dry_run): sku
            for sku, profile_path in targets
        }
        for future in as_completed(futures):
            sku = futures[future]
            stats["cards_checked"] += 1
            try:
                result = future.result()
            except Exception:
                # Safety net: _refresh_one already catches everything
                # internally, but a worker crashing before returning
                # (e.g. a pool-level issue) must not take down aggregation.
                result = {"outcome": "error", "timestamp": None}

            outcome = result["outcome"]
            if outcome == "priced":
                stats["priced"] += 1
                if not dry_run:
                    staleness[sku] = result["timestamp"]
            elif outcome == "no_change":
                stats["skipped_no_change"] += 1
                if not dry_run:
                    staleness[sku] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            elif outcome == "no_match":
                stats["no_match"] += 1
                if not dry_run:
                    staleness[sku] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            else:  # "error" -- staleness deliberately left untouched, see policy above
                stats["errors"] += 1

            processed += 1
            if processed % _CHECKPOINT_EVERY == 0:
                print(f"[OP-DOTGG-REFRESH] ...{processed}/{len(targets)} processed "
                      f"(priced={stats['priced']} no_change={stats['skipped_no_change']} "
                      f"no_match={stats['no_match']} errors={stats['errors']})", flush=True)
                if not dry_run:
                    _save_staleness_index(staleness)
                    if commit_cb:
                        commit_cb()

    if not dry_run:
        _save_staleness_index(staleness)
        if commit_cb:
            commit_cb()

    print(f"[OP-DOTGG-REFRESH] Done. {stats}", flush=True)
    return stats
