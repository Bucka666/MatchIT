"""
refresh_ygo_prices.py — Daily refresh of TCGplayer, Cardmarket, eBay and
Amazon prices for Yu-Gi-Oh! cards via YGOProDeck's bulk cardinfo.php
endpoint.

Mirrors refresh_onepiece_dotgg_prices.py's staleness-index design (see
that file's module docstring) but sourced from YGOProDeck's full-catalog
bulk endpoint — see backfill_ygo_prices.py's docstring for the fetch
mechanics and the card_sets[]-walk needed because one YGOProDeck card can
have many CardsDB folders (one per printing). This is the daily
INCREMENTAL sibling: run backfill_ygo_prices.py once first to fill the
catalog cold, then this keeps it fresh.

STALENESS INDEX: ygo_price_staleness.json, same {sku: last_checked_iso}
shape and 48h window as onepiece's/MTG's. Update policy identical:
  - priced / no_change / no_match -> index updated to now
  - error                         -> index NOT updated

PARALLELISM: shared work queue + N independent worker threads, NOT
ThreadPoolExecutor.map() — see backfill_mtg_prices.py's docstring for
the exact 1h43m stall class this avoids.

Called from matchit_modal.py's scheduled_jp_price_refresh cron
(lazy-imported, same convention as its existing scrape_pokemon_jpn call).
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

_N_WORKERS = 8
_CHECKPOINT_EVERY = 300
_RESULT_Q_MAXSIZE = 200
_STALE_AFTER = timedelta(hours=48)

_STALENESS_PATH = (
    "/modal_data/ygo_price_staleness.json"
    if os.path.exists("/modal_data")
    else "ygo_price_staleness.json"
)


def _fetch_ygoprodeck_bulk_prices() -> dict:
    """See backfill_ygo_prices.py's identical function for full commentary —
    duplicated here rather than cross-imported, matching this repo's
    standalone-script convention for refresh/backfill sibling pairs."""
    import requests
    from scrape_yugioh import make_card_id, _safe_float

    resp = requests.get(
        "https://db.ygoprodeck.com/api/v7/cardinfo.php", timeout=60,
        headers={"User-Agent": "GrailSweep-YGO-PriceRefresh/1.0"},
    )
    resp.raise_for_status()
    cards = resp.json().get("data", [])

    prices = {}
    for card in cards:
        card_prices_list = card.get("card_prices") or [{}]
        p = card_prices_list[0] if card_prices_list else {}
        entry = {
            "tcgplayer": _safe_float(p.get("tcgplayer_price")),
            "cardmarket": _safe_float(p.get("cardmarket_price")),
            "ebay": _safe_float(p.get("ebay_price")),
            "amazon": _safe_float(p.get("amazon_price")),
        }
        if all(v is None for v in entry.values()):
            continue
        for cs in (card.get("card_sets") or []):
            set_code = cs.get("set_code") or ""
            if not set_code:
                continue
            sku = make_card_id(card, set_code)
            prices[sku] = entry

    print(f"[YGO-REFRESH-FETCH] {len(cards)} cards, {len(prices)} printing-folders priced", flush=True)
    return prices


def _load_staleness_index() -> dict:
    try:
        with open(_STALENESS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_staleness_index(index: dict) -> None:
    tmp = _STALENESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    os.replace(tmp, _STALENESS_PATH)


def _index_says_fresh(last_checked) -> bool:
    if not last_checked:
        return False
    try:
        checked = datetime.strptime(last_checked, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return False
    return (datetime.utcnow() - checked) <= _STALE_AFTER


def _refresh_one(profile_path: Path, all_prices: dict, dry_run: bool) -> dict:
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)

        sku = profile_path.parent.name
        entry = all_prices.get(sku)
        if entry is None:
            return {"outcome": "no_match", "timestamp": None}

        if dry_run:
            return {"outcome": "priced", "timestamp": None}

        existing = dict(profile.get("prices") or {})
        new_prices = dict(existing)
        new_prices["tcgplayer"] = {"normal": {"market": entry["tcgplayer"]}}
        new_prices["cardmarket"] = {"normal": {"trend": entry["cardmarket"]}}
        new_prices["ebay"] = {"normal": {"market": entry["ebay"]}}
        new_prices["amazon"] = {"normal": {"market": entry["amazon"]}}
        if new_prices == existing:
            return {"outcome": "no_change", "timestamp": None}

        profile["prices"] = new_prices
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        profile["prices_updated"] = now
        profile["cardmarket_updated"] = now
        profile["tcgplayer_updated"] = now
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)

        return {"outcome": "priced", "timestamp": now}
    except Exception as e:
        return {"outcome": "error", "timestamp": None, "error": str(e)}


def _worker(work_q: "queue.Queue", result_q: "queue.Queue", all_prices: dict, dry_run: bool) -> None:
    while True:
        item = work_q.get()
        if item is None:
            break
        sku, profile_path = item
        result = _refresh_one(profile_path, all_prices, dry_run)
        result_q.put((sku, result))


def refresh_ygo_prices(
    db_root: Path,
    dry_run: bool = False,
    commit_cb: Optional[Callable[[], None]] = None,
) -> dict:
    t0 = time.time()
    ygo_dir = Path(db_root) / "yugioh"
    if not ygo_dir.exists():
        return {"error": str(ygo_dir)}

    stats = {
        "cards_checked": 0, "priced": 0,
        "skipped_fresh": 0, "skipped_no_change": 0, "no_match": 0, "errors": 0,
    }

    all_prices = _fetch_ygoprodeck_bulk_prices()
    staleness = _load_staleness_index()

    targets = []
    with os.scandir(ygo_dir) as it:
        for entry in it:
            if not entry.is_dir():
                continue
            profile_path = Path(entry.path) / "profile.json"
            if not profile_path.exists():
                continue
            sku = entry.name
            if _index_says_fresh(staleness.get(sku)):
                stats["skipped_fresh"] += 1
                continue
            targets.append((sku, profile_path))

    print(f"[YGO-REFRESH] {len(targets)} stale-or-unknown of "
          f"{len(targets) + stats['skipped_fresh']} total folders "
          f"({stats['skipped_fresh']} skipped via staleness index, never opened)", flush=True)

    work_q: "queue.Queue" = queue.Queue()
    for sku, profile_path in targets:
        work_q.put((sku, profile_path))
    for _ in range(_N_WORKERS):
        work_q.put(None)

    result_q: "queue.Queue" = queue.Queue(maxsize=_RESULT_Q_MAXSIZE)
    workers = [threading.Thread(target=_worker, args=(work_q, result_q, all_prices, dry_run), daemon=True)
               for _ in range(_N_WORKERS)]
    for w in workers:
        w.start()

    def _wait_then_signal():
        for w in workers:
            w.join()
        result_q.put(None)

    watcher = threading.Thread(target=_wait_then_signal, daemon=True)
    watcher.start()

    processed = 0
    while True:
        item = result_q.get()
        if item is None:
            break
        sku, result = item
        stats["cards_checked"] += 1
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
        else:
            stats["errors"] += 1

        processed += 1
        if processed % _CHECKPOINT_EVERY == 0:
            print(f"[YGO-REFRESH] ...{processed}/{len(targets)} processed "
                  f"(priced={stats['priced']} no_change={stats['skipped_no_change']} "
                  f"no_match={stats['no_match']} errors={stats['errors']})", flush=True)
            if not dry_run:
                _save_staleness_index(staleness)
                if commit_cb:
                    commit_cb()

    watcher.join()
    if not dry_run:
        _save_staleness_index(staleness)
        if commit_cb:
            commit_cb()

    stats["elapsed_s"] = round(time.time() - t0, 1)
    print(f"[YGO-REFRESH] Done. {stats}", flush=True)
    return stats
