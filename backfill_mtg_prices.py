"""
backfill_mtg_prices.py — One-time full-catalog price backfill for MTG cards
via Scryfall's bulk-data API.

Scryfall publishes a single downloadable file containing a Scryfall card
object (same shape as its per-card API, including `prices`) for every
printing it knows about — no per-card fetching, no rate limit, no
pagination:

    GET https://api.scryfall.com/bulk-data          -> find type=="default_cards"
    GET <that entry's jsonl_download_uri>            -> one gzipped JSONL file

Confirmed live (2026-09-23): ~79MB compressed / ~632MB decompressed /
118,389 records, downloaded+decompressed in under 3s total. The real cost
of this backfill is NOT the fetch — it's opening and rewriting up to
~80,000 individual profile.json files over the Modal network volume, the
same lesson already learned the hard way on One Piece's much smaller
~4,672-file catalog (see backfill_onepiece_dotgg_prices.py's docstring).

Card identity: keys the price lookup by the EXACT same folder-id logic
scrape_mtg.py already uses (make_card_id(), imported — not re-derived —
via add_local_file below), so a local folder name is looked up directly
against the bulk file's derived keys with no separate id field needed.
Skips the same non-real-card layouts scrape_mtg.py's own scrape_set()
skips (token/emblem/art_series/double_faced_token) so the two stay in
sync.

Write shape matches scrape_mtg.py's build_profile() exactly (always both
normal+foil sub-keys together, since Scryfall always returns both as one
matched pair per print):
    profile["prices"]["tcgplayer"]  = {"normal": {"market": usd},  "foil": {"market": usd_foil}}
    profile["prices"]["cardmarket"] = {"normal": {"trend": eur},   "foil": {"trend": eur_foil}}
Also stamps prices_updated / cardmarket_updated / tcgplayer_updated (the
field names _best_price_hint()/_extract_gbp_from_profile() already read
for freshness-aware source selection — see refresh_en_prices.py), which
the original one-shot scrape never set for MTG.

resume=True (default) skips any card that already has a real (non-null)
price on either source — this is what makes a killed/restarted run safe:
a second run only touches whatever the first run didn't finish.

PARALLELISM (2026-09-23): uses a shared work queue + N independent
worker threads pulling and pushing results directly, NOT
ThreadPoolExecutor.map(). map() preserves input order — if the file at
the head of the list is slow or hangs (one bad file on a network volume
is enough), the other workers keep completing later files but map()'s
generator withholds ALL of them until the head resolves. This exact
failure mode caused a real 1h43m stall with zero checkpoint output
elsewhere this session (build_ondevice_index.py). A queue-based pool
means a hang in one file costs at most 1 of N_WORKERS threads, never the
whole run. Checkpointed (commit_cb-equivalent vol.commit() + resume
markers already on disk) every _CHECKPOINT_EVERY cards from the start,
not bolted on after a stall.

Run:
    modal run backfill_mtg_prices.py --dry-run              # fetch + match only, no writes
    modal run backfill_mtg_prices.py --sample 60 --dry-run  # small-sample scope check
    modal run backfill_mtg_prices.py --sample 60            # small-sample REAL write test
    modal run backfill_mtg_prices.py                        # full ~80K catalog, real writes
"""
from __future__ import annotations

import gzip
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
app = modal.App("matchit-mtg-price-backfill")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("requests")
    # Reuses scrape_mtg.py's make_card_id()/_safe_float() verbatim via a real
    # import rather than re-deriving the id-cleaning logic in a second place —
    # this mini image otherwise has none of the main app's heavy deps, so only
    # the one file it needs is mounted in.
    .add_local_file("scrape_mtg.py", "/root/scrape_mtg.py")
)


def _fetch_scryfall_bulk_prices() -> dict:
    """One HTTP call to resolve the default_cards bulk file, one streamed GET
    to download+decompress it, one pass to build {sku: {usd,usd_foil,eur,eur_foil}}
    keyed by scrape_mtg.py's own make_card_id() so the refresh loop below can
    look a local folder name up directly with no extra id field to reconcile.
    Skips any card with NO usable price anywhere (matches
    backfill_onepiece_dotgg_prices.py's _fetch_dotgg_prices() convention —
    a card absent from the dict is indistinguishable from "not in this bulk
    file at all", both correctly reported as no_match downstream)."""
    import requests
    from scrape_mtg import make_card_id, _safe_float

    # Must match scrape_mtg.py's scrape_set() skip list exactly.
    skip_layouts = {"token", "emblem", "art_series", "double_faced_token"}

    meta = requests.get(
        "https://api.scryfall.com/bulk-data", timeout=30,
        headers={"User-Agent": "GrailSweep-MTG-PriceBackfill/1.0"},
    ).json()
    entry = next((d for d in meta.get("data", []) if d.get("type") == "default_cards"), None)
    if not entry:
        raise RuntimeError("Scryfall bulk-data: no default_cards entry found")

    url = entry["jsonl_download_uri"]
    size_mb = entry.get("compressed_size", 0) / 1e6
    print(f"[MTG-BULK-FETCH] downloading {url} ({size_mb:.1f}MB compressed)...", flush=True)
    t0 = time.time()

    prices = {}
    skipped_layout = 0
    skipped_no_price = 0
    total_seen = 0
    with requests.get(url, stream=True, timeout=180,
                       headers={"User-Agent": "GrailSweep-MTG-PriceBackfill/1.0"}) as resp:
        resp.raise_for_status()
        with gzip.GzipFile(fileobj=resp.raw) as gz:
            for line in gz:
                line = line.strip()
                if not line:
                    continue
                total_seen += 1
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if card.get("layout") in skip_layouts:
                    skipped_layout += 1
                    continue

                p = card.get("prices") or {}
                usd, usd_foil = _safe_float(p.get("usd")), _safe_float(p.get("usd_foil"))
                eur, eur_foil = _safe_float(p.get("eur")), _safe_float(p.get("eur_foil"))
                if usd is None and usd_foil is None and eur is None and eur_foil is None:
                    skipped_no_price += 1
                    continue

                sku = make_card_id(card)
                prices[sku] = {"usd": usd, "usd_foil": usd_foil, "eur": eur, "eur_foil": eur_foil}

    print(f"[MTG-BULK-FETCH] {total_seen} records seen, {len(prices)} priced skus kept "
          f"({skipped_layout} non-card layouts, {skipped_no_price} with no usable price), "
          f"fetch+parse took {time.time() - t0:.1f}s", flush=True)
    return prices


def _refresh_one_mtg(sku: str, profile_path: Path, all_prices: dict,
                      resume: bool, dry_run: bool) -> dict:
    """Pure per-card I/O — open, match by folder name, conditionally write.
    Touches NO shared state, safe to call from any worker thread. Never
    raises: every failure is caught and reported as an 'error' outcome."""
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)

        if resume:
            existing = profile.get("prices") or {}
            has_usd = (existing.get("tcgplayer") or {}).get("normal", {}).get("market") or \
                      (existing.get("tcgplayer") or {}).get("foil", {}).get("market")
            has_eur = (existing.get("cardmarket") or {}).get("normal", {}).get("trend") or \
                      (existing.get("cardmarket") or {}).get("foil", {}).get("trend")
            if has_usd or has_eur:
                return {"outcome": "skipped_existing"}

        entry = all_prices.get(sku)
        if entry is None:
            return {"outcome": "no_match"}

        if dry_run:
            return {"outcome": "priced"}

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
        result = _refresh_one_mtg(sku, profile_path, all_prices, resume, dry_run)
        result_q.put((sku, result))


def backfill_mtg_prices_impl(db_root: Path, dry_run: bool = False,
                              resume: bool = True, sample: int = 0) -> dict:
    t0 = time.time()
    mtg_dir = Path(db_root) / "mtg"
    if not mtg_dir.exists():
        return {"error": str(mtg_dir)}

    all_prices = _fetch_scryfall_bulk_prices()

    # Single directory listing (network FS — one scandir beats one stat per
    # folder), matching refresh_en_prices.py's own optimization at this scale.
    with os.scandir(mtg_dir) as it:
        folder_names = sorted(e.name for e in it if e.is_dir())
    if sample > 0:
        folder_names = folder_names[:sample]

    targets = []
    for name in folder_names:
        profile_path = mtg_dir / name / "profile.json"
        if profile_path.exists():
            targets.append((name, profile_path))

    print(f"[MTG-BACKFILL] {len(targets)} folders targeted "
          f"(sample={sample or 'full'}, resume={resume}, dry_run={dry_run})", flush=True)

    stats = {"cards_checked": 0, "priced": 0, "no_change": 0,
              "skipped_existing": 0, "no_match": 0, "errors": 0}
    sample_diffs = []  # a handful of real before/after prices, for reporting

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
            print(f"[MTG-BACKFILL] ...{processed}/{len(targets)} processed "
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
    print(f"[MTG-BACKFILL] Done. { {k: v for k, v in stats.items() if k != 'sample_diffs'} }", flush=True)
    return stats


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=7200,  # ceiling, not a target — sized like OnePiece's combined
    # pass1+pass2 slot for the same reason: the fetch is fast, reading+
    # writing up to ~80,000 profile.json files over the network volume is
    # what takes real time, and this is a one-time full-catalog run with no
    # prior art to size against precisely.
)
def backfill_mtg_prices(dry_run: bool = False, resume: bool = True, sample: int = 0) -> dict:
    import sys
    sys.path.insert(0, "/root")
    vol.reload()
    return backfill_mtg_prices_impl(Path("/modal_data/CardsDB"), dry_run=dry_run, resume=resume, sample=sample)


@app.local_entrypoint()
def run(dry_run: bool = False, resume: bool = True, sample: int = 0):
    print(f"Starting MTG price backfill (dry_run={dry_run}, resume={resume}, sample={sample or 'full'})...", flush=True)
    result = backfill_mtg_prices.remote(dry_run=dry_run, resume=resume, sample=sample)
    print("\n=== MTG PRICE BACKFILL RESULT ===", flush=True)
    for k, v in result.items():
        if k == "sample_diffs":
            print("  sample_diffs:", flush=True)
            for d in v:
                print(f"    {d['sku']}: before={d['before']} after={d['after']}", flush=True)
        else:
            print(f"  {k}: {v}", flush=True)
