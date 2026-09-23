"""
backfill_ygo_prices.py — One-time full-catalog price backfill for Yu-Gi-Oh!
cards via YGOProDeck's bulk cardinfo.php endpoint.

YGOProDeck returns EVERY card (all printings) in one unauthenticated GET
with no filter — no per-card fetching, no rate limit, no pagination:

    GET https://db.ygoprodeck.com/api/v7/cardinfo.php

Confirmed live (2026-09-23): ~21.3MB, fetched in well under 1s. As with
MTG, the real cost of this backfill is opening and rewriting the local
profile.json files over the Modal network volume, not the fetch.

Card identity is more involved than MTG's: YGOProDeck prices are PER CARD
(one card_prices[0] block), not per printing, but a single card can be
printed across many sets and therefore have many CardsDB folders (e.g.
"ygo-MRD-001-46986414" and "ygo-SDY-006-46986414" can be the same
monster). This walks each card's own card_sets[] array — the SAME walk
scrape_yugioh.py's scrape_set() already does per target set — and calls
scrape_yugioh.py's make_card_id() (imported, not re-derived, via
add_local_file below) for every set entry, so the same one price block
gets applied to every existing local folder for every printing of that
card.

Write shape matches scrape_yugioh.py's build_profile() exactly:
    profile["prices"]["tcgplayer"]  = {"normal": {"market": tcgplayer_price}}
    profile["prices"]["cardmarket"] = {"normal": {"trend": cardmarket_price}}
    profile["prices"]["ebay"]       = {"normal": {"market": ebay_price}}
    profile["prices"]["amazon"]     = {"normal": {"market": amazon_price}}
Also stamps prices_updated / cardmarket_updated / tcgplayer_updated (see
backfill_mtg_prices.py's docstring for why — same freshness fields
_best_price_hint()/_extract_gbp_from_profile() already read).

resume=True (default) skips any card that already has a real price on
any of the four sources.

PARALLELISM: same queue-based worker-pool pattern as backfill_mtg_prices.py
(see that file's docstring for the exact 1h43m ThreadPoolExecutor.map()
stall this avoids) — a shared work queue + N independent worker threads,
not map(). Checkpointed every _CHECKPOINT_EVERY cards from the start.

Run:
    modal run backfill_ygo_prices.py --dry-run              # fetch + match only, no writes
    modal run backfill_ygo_prices.py --sample 60 --dry-run  # small-sample scope check
    modal run backfill_ygo_prices.py --sample 60            # small-sample REAL write test
    modal run backfill_ygo_prices.py                        # full ~36K catalog, real writes
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import modal

VOLUME_NAME = "matchit-data-v2"
_N_WORKERS = 8
_CHECKPOINT_EVERY = 300
_RESULT_Q_MAXSIZE = 200

vol = modal.Volume.from_name(VOLUME_NAME)
app = modal.App("matchit-ygo-price-backfill")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("requests")
    # Reuses scrape_yugioh.py's make_card_id()/_safe_float() verbatim via a
    # real import rather than re-deriving the id-cleaning logic in a second
    # place — this mini image otherwise has none of the main app's heavy deps.
    .add_local_file("scrape_yugioh.py", "/root/scrape_yugioh.py")
)


def _fetch_ygoprodeck_bulk_prices() -> dict:
    """One HTTP call for the entire catalog. Returns {sku: {tcgplayer, cardmarket,
    ebay, amazon}} keyed by scrape_yugioh.py's own make_card_id(sku-per-printing) —
    built by walking each card's card_sets[] exactly as scrape_set() does per
    target set, so every existing local folder for every printing of a card
    gets the same price block. A card with no usable price on any of the
    four sources is skipped entirely (matches backfill_onepiece_dotgg_prices.py's
    convention: absent from the dict == no_match downstream, whether that's
    because YGOProDeck has no price data or because it isn't in the catalog
    at all — both correctly unpriceable)."""
    import requests
    from scrape_yugioh import make_card_id, _safe_float

    t0 = time.time()
    resp = requests.get(
        "https://db.ygoprodeck.com/api/v7/cardinfo.php", timeout=60,
        headers={"User-Agent": "GrailSweep-YGO-PriceBackfill/1.0"},
    )
    resp.raise_for_status()
    cards = resp.json().get("data", [])
    print(f"[YGO-BULK-FETCH] {len(cards)} cards fetched in {time.time() - t0:.1f}s", flush=True)

    prices = {}
    skipped_no_price = 0
    skipped_no_sets = 0
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
            skipped_no_price += 1
            continue

        card_sets = card.get("card_sets") or []
        if not card_sets:
            skipped_no_sets += 1
            continue
        for cs in card_sets:
            set_code = cs.get("set_code") or ""
            if not set_code:
                continue
            sku = make_card_id(card, set_code)
            prices[sku] = entry

    print(f"[YGO-BULK-FETCH] {len(prices)} printing-folders priced "
          f"({skipped_no_price} cards with no usable price, "
          f"{skipped_no_sets} cards with no card_sets entries)", flush=True)
    return prices


def _refresh_one_ygo(sku: str, profile_path: Path, all_prices: dict,
                      resume: bool, dry_run: bool) -> dict:
    """Pure per-card I/O — open, match by folder name, conditionally write.
    Touches NO shared state, safe to call from any worker thread. Never
    raises: every failure is caught and reported as an 'error' outcome."""
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)

        if resume:
            existing = profile.get("prices") or {}
            tcg = (existing.get("tcgplayer") or {}).get("normal") or {}
            cm = (existing.get("cardmarket") or {}).get("normal") or {}
            eb = (existing.get("ebay") or {}).get("normal") or {}
            am = (existing.get("amazon") or {}).get("normal") or {}
            if tcg.get("market") or cm.get("trend") or eb.get("market") or am.get("market"):
                return {"outcome": "skipped_existing"}

        entry = all_prices.get(sku)
        if entry is None:
            return {"outcome": "no_match"}

        if dry_run:
            return {"outcome": "priced"}

        existing = dict(profile.get("prices") or {})
        new_prices = dict(existing)
        new_prices["tcgplayer"] = {"normal": {"market": entry["tcgplayer"]}}
        new_prices["cardmarket"] = {"normal": {"trend": entry["cardmarket"]}}
        new_prices["ebay"] = {"normal": {"market": entry["ebay"]}}
        new_prices["amazon"] = {"normal": {"market": entry["amazon"]}}
        if new_prices == existing:
            return {"outcome": "no_change"}

        profile["prices"] = new_prices
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        profile["prices_updated"] = now
        profile["cardmarket_updated"] = now
        profile["tcgplayer_updated"] = now
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)

        return {"outcome": "priced", "before": existing, "after": new_prices}
    except Exception as e:
        return {"outcome": "error", "error": str(e)}


def _worker(work_q: "queue.Queue", result_q: "queue.Queue", all_prices: dict,
            resume: bool, dry_run: bool) -> None:
    while True:
        item = work_q.get()
        if item is None:
            break
        sku, profile_path = item
        result = _refresh_one_ygo(sku, profile_path, all_prices, resume, dry_run)
        result_q.put((sku, result))


def backfill_ygo_prices_impl(db_root: Path, dry_run: bool = False,
                              resume: bool = True, sample: int = 0) -> dict:
    t0 = time.time()
    ygo_dir = Path(db_root) / "yugioh"
    if not ygo_dir.exists():
        return {"error": str(ygo_dir)}

    all_prices = _fetch_ygoprodeck_bulk_prices()

    with os.scandir(ygo_dir) as it:
        folder_names = sorted(e.name for e in it if e.is_dir())
    if sample > 0:
        folder_names = folder_names[:sample]

    targets = []
    for name in folder_names:
        profile_path = ygo_dir / name / "profile.json"
        if profile_path.exists():
            targets.append((name, profile_path))

    print(f"[YGO-BACKFILL] {len(targets)} folders targeted "
          f"(sample={sample or 'full'}, resume={resume}, dry_run={dry_run})", flush=True)

    stats = {"cards_checked": 0, "priced": 0, "no_change": 0,
              "skipped_existing": 0, "no_match": 0, "errors": 0}
    sample_diffs = []

    work_q: "queue.Queue" = queue.Queue()
    for sku, profile_path in targets:
        work_q.put((sku, profile_path))
    for _ in range(_N_WORKERS):
        work_q.put(None)

    result_q: "queue.Queue" = queue.Queue(maxsize=_RESULT_Q_MAXSIZE)
    workers = [threading.Thread(target=_worker, args=(work_q, result_q, all_prices, resume, dry_run), daemon=True)
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
            if not dry_run and len(sample_diffs) < 10:
                sample_diffs.append({"sku": sku, "before": result.get("before"), "after": result.get("after")})
        elif outcome == "no_change":
            stats["no_change"] += 1
        elif outcome == "skipped_existing":
            stats["skipped_existing"] += 1
        elif outcome == "no_match":
            stats["no_match"] += 1
        else:
            stats["errors"] += 1

        processed += 1
        if processed % _CHECKPOINT_EVERY == 0:
            print(f"[YGO-BACKFILL] ...{processed}/{len(targets)} processed "
                  f"(priced={stats['priced']} no_change={stats['no_change']} "
                  f"skipped={stats['skipped_existing']} no_match={stats['no_match']} "
                  f"errors={stats['errors']})", flush=True)
            if not dry_run:
                vol.commit()

    watcher.join()
    if not dry_run:
        vol.commit()

    stats["elapsed_s"] = round(time.time() - t0, 1)
    stats["sample_diffs"] = sample_diffs
    print(f"[YGO-BACKFILL] Done. { {k: v for k, v in stats.items() if k != 'sample_diffs'} }", flush=True)
    return stats


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=7200,  # ceiling, not a target — see backfill_mtg_prices.py's
    # identical note; one-time full-catalog run, no prior art to size
    # against precisely.
)
def backfill_ygo_prices(dry_run: bool = False, resume: bool = True, sample: int = 0) -> dict:
    import sys
    sys.path.insert(0, "/root")
    vol.reload()
    return backfill_ygo_prices_impl(Path("/modal_data/CardsDB"), dry_run=dry_run, resume=resume, sample=sample)


@app.local_entrypoint()
def run(dry_run: bool = False, resume: bool = True, sample: int = 0):
    print(f"Starting YGO price backfill (dry_run={dry_run}, resume={resume}, sample={sample or 'full'})...", flush=True)
    result = backfill_ygo_prices.remote(dry_run=dry_run, resume=resume, sample=sample)
    print("\n=== YGO PRICE BACKFILL RESULT ===", flush=True)
    for k, v in result.items():
        if k == "sample_diffs":
            print("  sample_diffs:", flush=True)
            for d in v:
                print(f"    {d['sku']}: before={d['before']} after={d['after']}", flush=True)
        else:
            print(f"  {k}: {v}", flush=True)
