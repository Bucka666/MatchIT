"""
collection_snapshot.py — daily portfolio-value snapshot writer.

Chained onto scheduled_en_price_refresh() (matchit_modal.py), the last of
the 3 daily price-refresh crons (1:30 OnePiece, 3:00 JP, 4:00 EN) — not a
new schedule. Modal's 5-scheduled-function plan cap is already fully used
(same constraint that forced the Limitless One Piece pass onto its own
existing cron rather than a new one — see scheduled_onepiece_price_refresh).

For every saved collection in collections.json, sums a live current price
per item and appends ONE real entry for today's UTC date to
collection_value_history.json, keyed by premium code. This is the real,
recorded time-series collection_value_history() (app.py) was missing —
it previously recomputed all 30 "days" from today's snapshot on every
request, which could never show a genuine day-to-day step and would
retroactively rewrite every visible day whenever any item's price changed
(confirmed live 2026-09-22/23). This file is the fix for that: one real
row per collection per day, never rewritten once written for a past day.

Price extraction is a verbatim copy of app.py's _best_price_hint() /
_extract_variant_price() / _load_profile_direct() — standalone-script
convention used throughout this repo (see scrape_pokemon_jpn.py's own
_extract_gbp_from_profile copy) specifically to avoid importing app.py,
which preloads CLIP/DINOv2/embedding caches at module import — exactly
the cold start scheduled_en_price_refresh/scheduled_jp_price_refresh
already go out of their way to avoid paying.

Idempotent per UTC day: re-running the same day replaces that day's
entry for each code rather than appending a duplicate — safe to run more
than once in a day (manual test run + the real cron both land cleanly).

Run:
    modal run collection_snapshot.py               # writes to the live volume
    modal run collection_snapshot.py --dry-run      # scope count, no writes
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ── Verbatim copies from app.py (standalone-script convention, see module
# docstring above) — keep in sync by hand if the app.py originals change. ──

_GAME_DIR_NAME = {"POKEMON": "pokemon", "MTG": "mtg", "YUGIOH": "yugioh", "ONEPIECE": "onepiece"}


def _load_contaminated_skus(modal_data_root: str) -> frozenset:
    path = os.path.join(modal_data_root, "contaminated_skus.json")
    try:
        with open(path, encoding="utf-8") as f:
            return frozenset(json.load(f))
    except Exception:
        return frozenset({
            "svp-13", "svp-65", "svp-91", "svp-97", "svp-98",
            "svp-123", "svp-129", "svp-141", "svp-159", "svp-173",
        })


def _load_profile_direct(sku: str, db_root: str, game: str) -> dict:
    game_dir = _GAME_DIR_NAME.get(game)
    if not game_dir:
        return {}
    profile_path = os.path.join(db_root, game_dir, sku, "profile.json")
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_sku_game(sku: str, db_root: str, cache: dict) -> str:
    """Prefix is definitive for ygo-/mtg-/op-; bare-prefix Pokémon/MTG
    collisions need a profile read (mirrors app.py's _get_sku_game, minus
    the module-level cache dict which is passed in here instead)."""
    if sku in cache:
        return cache[sku]
    if sku.startswith("ygo-"):
        result = "YUGIOH"
    elif sku.startswith("mtg-"):
        result = "MTG"
    elif sku.startswith("op-"):
        result = "ONEPIECE"
    else:
        result = "POKEMON"
        profile = _load_profile_direct(sku, db_root, "POKEMON")
        if not profile:
            # not a pokemon SKU after all -- bare-prefix collision with MTG
            profile = _load_profile_direct(sku, db_root, "MTG")
            if profile:
                result = "MTG"
        elif (profile.get("category") or "").upper() == "MTG":
            result = "MTG"
    cache[sku] = result
    return result


def _extract_variant_price(d, keys, prefer_variant=None):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    if prefer_variant:
        vd = d.get(prefer_variant)
        if isinstance(vd, dict):
            for k in keys:
                v = vd.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
    for _variant, vd in d.items():
        if isinstance(vd, dict):
            for k in keys:
                v = vd.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
    return None


def _best_price_hint(prices, cm_updated=None, tcp_updated=None, sku=None, contaminated=frozenset()):
    if not isinstance(prices, dict):
        return None, None
    cm = prices.get("cardmarket") or {}
    if sku in contaminated:
        cm = {}
    cm_val = _extract_variant_price(cm, ("trend", "avg_sell", "low"))
    tcg = prices.get("tcgplayer") or {}
    tcg_val = _extract_variant_price(tcg, ("market",), prefer_variant="holofoil")
    if cm_val is not None and tcg_val is not None and cm_updated and tcp_updated:
        if str(tcp_updated) > str(cm_updated):
            return tcg_val, "USD"
        return cm_val, "EUR"
    if cm_updated and not tcp_updated and cm_val is not None:
        return cm_val, "EUR"
    if tcp_updated and not cm_updated and tcg_val is not None:
        return tcg_val, "USD"
    if cm_val is not None:
        return cm_val, "EUR"
    if tcg_val is not None:
        return tcg_val, "USD"
    return None, None


def _get_fx(modal_data_root: str) -> dict:
    """Cache-only read of fx_rates.json (same file fx_rates.py's get_fx()
    reads) — no network call, matches every other price computation in
    this repo's scheduled jobs."""
    path = os.path.join(modal_data_root, "fx_rates.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {"usd_gbp": data.get("usd_gbp") or 0.79, "eur_gbp": data.get("eur_gbp") or 0.86}
    except Exception:
        return {"usd_gbp": 0.79, "eur_gbp": 0.86}


# ── Snapshot logic ───────────────────────────────────────────────────────────

def write_collection_snapshot(modal_data_root: str, dry_run: bool = False,
                               commit_cb: Optional[Callable[[], None]] = None,
                               max_workers: int = 32) -> dict:
    """For every saved collection, sum a live current price per item and
    append/replace today's UTC-date entry in collection_value_history.json.

    Returns a summary dict. dry_run=True computes everything but skips the
    write, for scope-checking before paying the real cost."""
    collections_path = os.path.join(modal_data_root, "collections.json")
    history_path = os.path.join(modal_data_root, "collection_value_history.json")
    db_root = os.path.join(modal_data_root, "CardsDB")

    try:
        with open(collections_path, encoding="utf-8") as f:
            collections = json.load(f)
    except Exception as e:
        print(f"[SNAPSHOT] Could not read {collections_path}: {e}", flush=True)
        return {"error": str(e), "codes_processed": 0}

    if not isinstance(collections, dict) or not collections:
        print("[SNAPSHOT] No saved collections — nothing to do", flush=True)
        return {"codes_processed": 0}

    contaminated = _load_contaminated_skus(modal_data_root)
    fx = _get_fx(modal_data_root)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    game_cache: dict = {}

    def _price_one(sku: str):
        game = _get_sku_game(sku, db_root, game_cache)
        profile = _load_profile_direct(sku, db_root, game)
        prices = profile.get("prices")
        price, currency = _best_price_hint(
            prices,
            profile.get("cardmarket_updated"),
            profile.get("tcgplayer_updated"),
            sku=sku,
            contaminated=contaminated,
        )
        if price is None or not currency:
            return None
        rate = fx["eur_gbp"] if currency == "EUR" else fx["usd_gbp"]
        return round(price * rate, 2)

    try:
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        history = {}

    summary = {"codes_processed": 0, "total_items_priced": 0, "total_items_unpriced": 0, "per_code": {}}

    for code, items in collections.items():
        if not isinstance(items, list) or not items:
            continue
        skus = [it.get("sku") for it in items if isinstance(it, dict) and it.get("sku")]
        priced = 0
        unpriced = 0
        total_gbp = 0.0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for gbp in pool.map(_price_one, skus):
                if gbp is not None:
                    total_gbp += gbp
                    priced += 1
                else:
                    unpriced += 1

        total_gbp = round(total_gbp, 2)
        summary["codes_processed"] += 1
        summary["total_items_priced"] += priced
        summary["total_items_unpriced"] += unpriced
        summary["per_code"][code] = {
            "item_count": len(skus), "priced": priced, "unpriced": unpriced, "total_gbp": total_gbp,
        }
        print(f"[SNAPSHOT] {code}: {len(skus)} items, {priced} priced, "
              f"{unpriced} unpriced, total=£{total_gbp}", flush=True)

        if not dry_run:
            entries = history.get(code, [])
            # Idempotent per UTC day: replace today's entry if this already
            # ran today (manual test + real cron landing same day), else append.
            entries = [e for e in entries if e.get("date") != today_str]
            entries.append({"date": today_str, "total_gbp": total_gbp, "item_count": len(skus)})
            entries.sort(key=lambda e: e["date"])
            history[code] = entries

    if not dry_run:
        tmp_path = history_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, history_path)
        print(f"[SNAPSHOT] Wrote {history_path}", flush=True)
        if commit_cb is not None:
            try:
                commit_cb()
            except Exception as e:
                print(f"[SNAPSHOT] commit_cb() failed: {e}", flush=True)
    else:
        print("[SNAPSHOT] DRY-RUN — no write made", flush=True)

    print(f"[SNAPSHOT] Done. {summary}", flush=True)
    return summary


# ── Modal wrapper (manual/standalone runs against the live volume) ──────────
import modal

_VOLUME_NAME = "matchit-data-v2"
_vol = modal.Volume.from_name(_VOLUME_NAME, version=2)
_image = modal.Image.debian_slim(python_version="3.11")
_modal_app = modal.App("matchit-collection-snapshot")


@_modal_app.function(image=_image, volumes={"/modal_data": _vol}, timeout=900)
def run_remote(dry_run: bool = False):
    _vol.reload()
    result = write_collection_snapshot("/modal_data", dry_run=dry_run, commit_cb=_vol.commit)
    return result


@_modal_app.local_entrypoint()
def main(dry_run: bool = False):
    print(run_remote.remote(dry_run=dry_run))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = os.environ.get("LOCALAPPDATA", ".")
    write_collection_snapshot(root, dry_run=args.dry_run)
