"""
refresh_mtg_prices.py — Daily refresh of TCGplayer (USD) and Cardmarket
(EUR) prices for MTG cards via Scryfall's bulk-data API.

Mirrors refresh_onepiece_dotgg_prices.py's staleness-index design (see
that file's module docstring for the full rationale) but sourced from
Scryfall's default_cards bulk file instead of a per-game API — see
backfill_mtg_prices.py's docstring for the fetch mechanics and why a
single bulk file replaces ~80,000 individual requests. This is the daily
INCREMENTAL sibling: run backfill_mtg_prices.py once first to fill the
catalog cold, then this keeps it fresh.

STALENESS INDEX: mtg_price_staleness.json, same {sku: last_checked_iso}
shape and same 48h window as onepiece's — a performance cache only,
never the source of truth (a sku missing from it is always treated as
stale). Update policy per outcome, identical to onepiece's:
  - priced / no_change / no_match -> index updated to now
  - error                         -> index NOT updated (unknown state)

PARALLELISM: shared work queue + N independent worker threads, NOT
ThreadPoolExecutor.map() — see backfill_mtg_prices.py's docstring for
the exact 1h43m stall this avoids (map() withholds all results until
the head of the input list resolves; a queue-based pool costs at most
one of N_WORKERS threads to a single hang).

Called from matchit_modal.py's scheduled_onepiece_price_refresh as pass
3 (lazy-imported, same convention as that cron's other two passes).
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
    "/modal_data/mtg_price_staleness.json"
    if os.path.exists("/modal_data")
    else "mtg_price_staleness.json"
)

_SKIP_LAYOUTS = {"token", "emblem", "art_series", "double_faced_token"}


def _fetch_scryfall_bulk_prices() -> dict:
    """See backfill_mtg_prices.py's identical function for full commentary —
    duplicated here rather than cross-imported, matching this repo's
    standalone-script convention for the refresh/backfill sibling pairs
    (e.g. refresh_onepiece_dotgg_prices.py / backfill_onepiece_dotgg_prices.py)."""
    import gzip
    import requests
    from scrape_mtg import make_card_id, _safe_float

    meta = requests.get(
        "https://api.scryfall.com/bulk-data", timeout=30,
        headers={"User-Agent": "GrailSweep-MTG-PriceRefresh/1.0"},
    ).json()
    entry = next((d for d in meta.get("data", []) if d.get("type") == "default_cards"), None)
    if not entry:
        raise RuntimeError("Scryfall bulk-data: no default_cards entry found")

    url = entry["jsonl_download_uri"]
    print(f"[MTG-REFRESH-FETCH] downloading {url} "
          f"({entry.get('compressed_size', 0) / 1e6:.1f}MB compressed)...", flush=True)

    prices = {}
    with requests.get(url, stream=True, timeout=180,
                       headers={"User-Agent": "GrailSweep-MTG-PriceRefresh/1.0"}) as resp:
        resp.raise_for_status()
        with gzip.GzipFile(fileobj=resp.raw) as gz:
            for line in gz:
                line = line.strip()
                if not line:
                    continue
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if card.get("layout") in _SKIP_LAYOUTS:
                    continue
                p = card.get("prices") or {}
                usd, usd_foil = _safe_float(p.get("usd")), _safe_float(p.get("usd_foil"))
                eur, eur_foil = _safe_float(p.get("eur")), _safe_float(p.get("eur_foil"))
                if usd is None and usd_foil is None and eur is None and eur_foil is None:
                    continue
                sku = make_card_id(card)
                prices[sku] = {"usd": usd, "usd_foil": usd_foil, "eur": eur, "eur_foil": eur_foil}

    print(f"[MTG-REFRESH-FETCH] {len(prices)} priced skus", flush=True)
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
        new_prices["tcgplayer"] = {
            "normal": {"market": entry["usd"]},
            "foil": {"market": entry["usd_foil"]},
        }
        new_prices["cardmarket"] = {
            "normal": {"trend": entry["eur"]},
            "foil": {"trend": entry["eur_foil"]},
        }
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


def refresh_mtg_prices(
    db_root: Path,
    dry_run: bool = False,
    commit_cb: Optional[Callable[[], None]] = None,
) -> dict:
    t0 = time.time()
    mtg_dir = Path(db_root) / "mtg"
    if not mtg_dir.exists():
        return {"error": str(mtg_dir)}

    stats = {
        "cards_checked": 0, "priced": 0,
        "skipped_fresh": 0, "skipped_no_change": 0, "no_match": 0, "errors": 0,
    }

    all_prices = _fetch_scryfall_bulk_prices()
    staleness = _load_staleness_index()

    targets = []
    with os.scandir(mtg_dir) as it:
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

    print(f"[MTG-REFRESH] {len(targets)} stale-or-unknown of "
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
            print(f"[MTG-REFRESH] ...{processed}/{len(targets)} processed "
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
    print(f"[MTG-REFRESH] Done. {stats}", flush=True)
    return stats
