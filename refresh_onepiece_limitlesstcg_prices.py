"""
refresh_onepiece_limitlesstcg_prices.py — Gap-filler for One Piece cards
DotGG has no price data for at all, sourced from onepiece.limitlesstcg.com.
General across every set (op01..op17, eb01..eb04, st01..st36, p, prb01/02,
...) -- NOT hardcoded to any one set, so the next new set needs no manual
registration here either, mirroring the same class of fix already applied
to rebuild_search_and_lookup_after_ingest() elsewhere this session.

Target-SKU discovery: a card's profile.json "prices" dict being empty (no
"tcgplayer" AND no "cardmarket" key) is the real no-data signal. The
staleness index (onepiece_price_staleness.json, written by
refresh_onepiece_dotgg_prices.py) does NOT distinguish outcome type --
priced/no_change/no_match all write an identical last-checked timestamp
(confirmed by reading that script's own code) -- so it cannot answer
"which SKUs are currently unmatched" on its own. This script still reads
AND writes that same index (to avoid DotGG immediately re-checking a SKU
this script just filled), it just isn't the primary selection signal.

Card-number/variant mapping: Limitless groups every print variant of one
card NUMBER on a single page (e.g. /cards/OP17-005 lists the base print,
"aa" alt-art, and "manga" rows together). Local SKUs use the matching
_p1/_p2/... suffix convention. Verified against a real 3-variant case
(OP17-005: base/_p1/_p2 <-> Limitless v=0/v=1/v=2, in the same order)
before relying on it: fetch each base card number's page ONCE, parse
every prints-table row in document order, map positionally to the local
suffix-sorted SKU list for that base number. A row-count mismatch is
reported as a failure, never guessed at.

Schema written matches refresh_onepiece_dotgg_prices.py's own fields
exactly, so a later real DotGG match cleanly overwrites a Limitless-
sourced value the same way it already overwrites any other prior value:
    profile["prices"]["tcgplayer"] = {"market": usd_price}
    profile["prices"]["cardmarket"] = {"avg_sell": eur_price}

GBP is NOT written to profile.json (no existing field for it there --
_extract_gbp_from_profile in app.py computes GBP on read, from these same
USD/EUR fields, via the same get_fx() this script also uses). GBP is
reported in dry-run output only, for review.

Mirrors refresh_onepiece_dotgg_prices.py's shape: ThreadPoolExecutor for
per-base-number fetches, checkpointed writes + staleness-index save +
commit_cb together, real dry_run param (default True, matching the
"safe by default" convention).
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://onepiece.limitlesstcg.com/cards/{code}"
_HEADERS = {"User-Agent": "GrailSweep/1.0 (+https://grailsweep.com; contact@grailsweep.com)"}
_MAX_WORKERS = 4  # politeness -- this hits a third-party site, not our own volume
_REQUEST_DELAY_S = 0.15
_CHECKPOINT_EVERY = 50  # base-number groups, not SKUs (fewer requests than DotGG's SKU-level checkpoint)
_STALE_AFTER = timedelta(hours=48)

# Shared with refresh_onepiece_dotgg_prices.py -- written to (never read
# for gating) after a real write, so DotGG's own next run doesn't
# immediately re-open a SKU this script just filled.
_DOTGG_STALENESS_PATH = (
    "/modal_data/onepiece_price_staleness.json"
    if os.path.exists("/modal_data")
    else "onepiece_price_staleness.json"
)

# THIS script's own tracking, separate from DotGG's. Both scripts run
# daily and both mark literally every card they check as "fresh" for 48h,
# including a no_match/still-empty outcome -- confirmed live (2026-09-21):
# sharing DotGG's index for gating meant a set DotGG had just finished
# checking (e.g. OP17, touched by that morning's 1:30am UTC cron) looked
# "fresh" to THIS script too, for the same 48h window, even though every
# one of its still-empty SKUs genuinely needed a Limitless fill right
# then. That's the exact scenario this script exists for -- sharing the
# index silently defeated its own purpose. A separate file means DotGG
# checking a card has no bearing on whether Limitless gets a turn at it.
_OWN_STALENESS_PATH = (
    "/modal_data/onepiece_price_staleness_limitless.json"
    if os.path.exists("/modal_data")
    else "onepiece_price_staleness_limitless.json"
)


def _index_says_fresh(last_checked) -> bool:
    """Same semantics as refresh_onepiece_dotgg_prices.py's own helper --
    kept independent (not imported) so this script has no hard dependency
    on that module, matching its own standalone-script convention."""
    if not last_checked:
        return False
    try:
        checked = datetime.strptime(last_checked, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return False
    return (datetime.utcnow() - checked) <= _STALE_AFTER


def _load_index(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(path: str, index: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    os.replace(tmp, path)


def _variant_sort_key(sku_name: str):
    m = re.search(r"_p(\d+)$", sku_name)
    return (0, 0) if not m else (1, int(m.group(1)))


def _discover_targets(onepiece_dir: Path, staleness: dict) -> dict:
    """Scans every onepiece profile.json ONCE. Returns
    {(set_id, base_num): {"all_skus": [sorted variant sku names],
                           "needs_write": {sku_name, ...}}}
    only for base numbers with at least one genuinely-empty-priced local
    SKU whose entry in THIS SCRIPT's OWN staleness index isn't itself
    fresh (so a SKU this script already checked recently isn't re-hit
    every single run). `staleness` must be this script's own index, not
    DotGG's shared one -- see _OWN_STALENESS_PATH docstring for why."""
    groups: dict = {}
    for folder in sorted(onepiece_dir.iterdir()):
        if not folder.is_dir():
            continue
        profile_path = folder / "profile.json"
        if not profile_path.exists():
            continue
        name = folder.name  # e.g. "op-op17-005_p1"
        parts = name.split("-")
        if len(parts) < 3 or parts[0] != "op":
            continue
        set_id = parts[1]
        card_num_full = "-".join(parts[2:])  # e.g. "005_p1"
        base_num = card_num_full.split("_")[0]

        try:
            with open(profile_path, encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:
            continue
        prices = profile.get("prices") or {}
        has_tcg = isinstance(prices.get("tcgplayer"), dict) and bool(prices["tcgplayer"])
        has_cm = isinstance(prices.get("cardmarket"), dict) and bool(prices["cardmarket"])
        empty = not has_tcg and not has_cm

        key = (set_id, base_num)
        groups.setdefault(key, {"all_skus": [], "needs_write": set()})
        groups[key]["all_skus"].append(name)
        if empty and not _index_says_fresh(staleness.get(name)):
            groups[key]["needs_write"].add(name)

    for g in groups.values():
        g["all_skus"].sort(key=_variant_sort_key)

    return {k: v for k, v in groups.items() if v["needs_write"]}


def _fetch_prints_table(set_id: str, base_num: str):
    code = f"{set_id.upper()}-{base_num}"
    url = _BASE_URL.format(code=code)
    r = requests.get(url, headers=_HEADERS, timeout=15)
    if r.status_code == 404:
        return None, "404"
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Verified selector (see module docstring) -- a broader selector chain
    # tried earlier matched an unrelated tr.current element elsewhere on
    # the page and produced a spurious all-None leading row.
    rows = [tr for tr in soup.find_all("tr") if tr.select_one(".prints-table-card-number")]

    variants = []
    for tr in rows:
        label_el = tr.select_one(".prints-table-card-number")
        label = label_el.get_text(strip=True) if label_el else ""
        usd_el = tr.select_one("a.card-price.usd")
        eur_el = tr.select_one("a.card-price.eur")
        usd = eur = None
        if usd_el:
            m = re.search(r"[\d,]+\.\d+", usd_el.get_text())
            if m:
                usd = float(m.group(0).replace(",", ""))
        if eur_el:
            m = re.search(r"[\d,]+\.\d+", eur_el.get_text())
            if m:
                eur = float(m.group(0).replace(",", ""))
        variants.append({"label": label, "usd": usd, "eur": eur})
    return variants, None


def _process_one_base(key, group, onepiece_dir: Path, fx: dict, dry_run: bool):
    """Runs in a worker thread. Returns a list of per-SKU result dicts.
    Does not touch shared stats/staleness state (mirrors
    refresh_onepiece_dotgg_prices.py's _refresh_one isolation)."""
    set_id, base_num = key
    all_skus = group["all_skus"]
    needs_write = group["needs_write"]

    try:
        variants, err = _fetch_prints_table(set_id, base_num)
    except Exception as e:
        return [{"sku": s, "outcome": "error", "reason": f"exception: {e}"} for s in needs_write]

    if err == "404":
        return [{"sku": s, "outcome": "not_found_on_limitless"} for s in needs_write]
    if not variants:
        return [{"sku": s, "outcome": "no_prints_table_rows"} for s in needs_write]
    if len(variants) != len(all_skus):
        return [{
            "sku": s, "outcome": "row_count_mismatch",
            "reason": f"{len(variants)} Limitless rows vs {len(all_skus)} local SKUs",
        } for s in needs_write]

    results = []
    for sku_name, variant in zip(all_skus, variants):
        if sku_name not in needs_write:
            continue  # already has real data (likely from DotGG) -- never overwrite
        usd, eur = variant["usd"], variant["eur"]
        if usd is None and eur is None:
            results.append({"sku": sku_name, "outcome": "no_price_on_limitless"})
            continue

        if eur is not None:
            gbp = round(eur * fx["eur_gbp"], 2)
            gbp_source = "eur->gbp"
        else:
            gbp = round(usd * fx["usd_gbp"], 2)
            gbp_source = "usd->gbp"

        result = {
            "sku": sku_name, "outcome": "priced",
            "usd": usd, "eur": eur, "gbp": gbp, "gbp_source": gbp_source,
            "label": variant["label"] or "base",
        }

        if not dry_run:
            profile_path = onepiece_dir / sku_name / "profile.json"
            try:
                with open(profile_path, encoding="utf-8") as f:
                    profile = json.load(f)
                existing = dict(profile.get("prices", {}))
                new_prices = dict(existing)
                if usd is not None:
                    new_prices["tcgplayer"] = {"market": usd}
                if eur is not None:
                    new_prices["cardmarket"] = {"avg_sell": eur}
                profile["prices"] = new_prices
                profile["prices_updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                profile["prices_source"] = "limitlesstcg"
                tmp_path = str(profile_path) + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(profile, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, profile_path)
            except Exception as e:
                result["outcome"] = "write_error"
                result["reason"] = str(e)

        results.append(result)

    return results


def refresh_onepiece_limitlesstcg_prices(
    db_root: Path,
    dry_run: bool = True,
    commit_cb: Optional[Callable[[], None]] = None,
) -> dict:
    """Scrapes onepiece.limitlesstcg.com to fill price gaps DotGG hasn't
    covered, across every One Piece set. dry_run=True (default): computes
    and reports everything, writes nothing. dry_run=False: writes into
    profile["prices"] using the exact schema refresh_onepiece_dotgg_prices.py
    uses, and updates BOTH staleness indexes: this script's own (every
    SKU it actually attempted, any outcome) and DotGG's shared one (only
    SKUs it successfully priced, so DotGG's own next run doesn't
    immediately re-open something this script just filled). Gating on
    which SKUs to attempt uses ONLY this script's own index -- see
    _OWN_STALENESS_PATH docstring for why sharing DotGG's index for that
    purpose silently defeated this script's entire reason for existing."""
    from fx_rates import get_fx

    t0 = time.time()
    onepiece_dir = Path(db_root) / "onepiece"
    if not onepiece_dir.exists():
        return {"error": str(onepiece_dir)}

    fx = get_fx()
    print(f"[OP-LIMITLESS] fx rates in use: usd_gbp={fx['usd_gbp']} eur_gbp={fx['eur_gbp']}", flush=True)

    own_staleness = _load_index(_OWN_STALENESS_PATH)
    dotgg_staleness = _load_index(_DOTGG_STALENESS_PATH)
    targets = _discover_targets(onepiece_dir, own_staleness)
    total_skus_targeted = sum(len(g["needs_write"]) for g in targets.values())
    print(f"[OP-LIMITLESS] {len(targets)} base-number groups, "
          f"{total_skus_targeted} SKUs targeted for a Limitless fill (dry_run={dry_run})", flush=True)

    stats = {
        "base_groups": len(targets), "skus_targeted": total_skus_targeted,
        "priced": 0, "not_found_on_limitless": 0, "no_prints_table_rows": 0,
        "row_count_mismatch": 0, "no_price_on_limitless": 0, "error": 0, "write_error": 0,
    }
    all_results = []

    processed_groups = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_process_one_base, key, group, onepiece_dir, fx, dry_run): key
            for key, group in targets.items()
        }
        for future in as_completed(futures):
            try:
                results = future.result()
            except Exception as e:
                key = futures[future]
                results = [{"sku": s, "outcome": "error", "reason": str(e)} for s in targets[key]["needs_write"]]

            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            for r in results:
                stats[r["outcome"]] = stats.get(r["outcome"], 0) + 1
                all_results.append(r)
                if not dry_run:
                    # Own index: every SKU actually attempted, any outcome
                    # -- this is what gates re-attempts, so it must record
                    # failures too (a permanent row-count mismatch
                    # shouldn't be re-fetched daily forever either).
                    own_staleness[r["sku"]] = now
                    if r["outcome"] == "priced":
                        # Shared index: only real successes, so DotGG's
                        # own next run doesn't immediately re-open what
                        # this script just filled.
                        dotgg_staleness[r["sku"]] = now

            processed_groups += 1
            time.sleep(_REQUEST_DELAY_S)
            if processed_groups % _CHECKPOINT_EVERY == 0:
                print(f"[OP-LIMITLESS] ...{processed_groups}/{len(targets)} base groups "
                      f"(priced={stats['priced']} errors={stats['error'] + stats['write_error']})", flush=True)
                if not dry_run:
                    _save_index(_OWN_STALENESS_PATH, own_staleness)
                    _save_index(_DOTGG_STALENESS_PATH, dotgg_staleness)
                    if commit_cb:
                        commit_cb()

    if not dry_run:
        _save_index(_OWN_STALENESS_PATH, own_staleness)
        _save_index(_DOTGG_STALENESS_PATH, dotgg_staleness)
        if commit_cb:
            commit_cb()

    stats["elapsed_s"] = round(time.time() - t0, 1)
    print(f"[OP-LIMITLESS] Done. {stats}", flush=True)
    return {"stats": stats, "results": all_results, "fx": fx}


if __name__ == "__main__":
    db_root = Path("/modal_data/CardsDB") if os.path.exists("/modal_data") else Path(r"C:\CardsDB")
    result = refresh_onepiece_limitlesstcg_prices(db_root, dry_run=True)
    print(json.dumps(result["stats"], indent=2))
