"""
scrape_pokemon_jpn.py — Scrape Japanese Pokémon sets from TCGdex
================================================================
Writes output into CardsDB/pokemon/ using jpn- SKU prefix.

Usage:
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --list-sets
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --sets sv6 --dry-run
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --sets sv6,sv7
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --resume

    # Cardmarket price backfill (existing cards only, no image fetch):
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --prices-only --dry-run
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --prices-only

    # Against the live Modal volume:
    modal run scrape_pokemon_jpn.py::run_price_backfill
"""

# Keeps tcgdexsdk (only needed by the image-scrape path, lazily imported
# inside main()) from being evaluated at module-load time via the
# scrape_set() type hint below — the price-backfill Modal image installs
# only `requests`, and a top-level SDK import there would crash the
# container before backfill_cardmarket_prices() ever runs.
from __future__ import annotations

import os
import sys
import json
import time
import argparse
import requests
import logging
import urllib.parse
from pathlib import Path

# Japanese set names contain CJK characters — force UTF-8 on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def download_image(url: str, dest: Path, timeout: int = 5) -> bool:
    """Download image to dest. Retries once on failure. Returns True on success."""
    for attempt in (1, 2):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return True
        except Exception as e:
            if attempt == 1:
                log.warning("Image download failed (attempt 1), retrying: %s — %s", url, e)
            else:
                log.warning("Image download failed (attempt 2), skipping: %s — %s", url, e)
    return False


def scrape_set(sdk: TCGdex, set_id: str, db_root: Path,
               resume: bool, dry_run: bool) -> dict:
    """Scrape all cards from one Japanese set. Returns stats dict."""
    stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    try:
        encoded_id = urllib.parse.quote(set_id, safe='')
        card_set = sdk.set.getSync(encoded_id)
    except Exception as e:
        log.warning("Failed to fetch set %s: %s", set_id, e)
        return stats

    if card_set is None:
        log.warning("Set %s not found", set_id)
        return stats

    set_name = card_set.name
    cards = card_set.cards or []
    stats["total"] = len(cards)

    print(f"\n{'='*60}")
    print(f"  Set: {set_name} ({set_id})")
    print(f"  Cards: {len(cards)}")
    print(f"{'='*60}")

    for card in cards:
        sku = f"jpn-{set_id.lower()}-{card.localId}"
        out_dir = db_root / "pokemon" / sku
        image_path = out_dir / "front.png"
        image_url = (card.image + "/high.png") if card.image else None

        if resume and image_path.exists():
            stats["skipped"] += 1
            continue

        profile = {
            "api_id": sku,
            "name": card.name,
            "number": card.localId,
            "card_number": card.localId,
            "set_id": set_id,
            "set_name": set_name,
            "lang": "ja",
            "category": "POKEMON",
            "rarity": getattr(card, "rarity", None) or None,
            "image_url": image_url,
        }

        if dry_run:
            print(f"  DRY-RUN  {sku}  →  {image_url or '(no image)'}")
            print(f"           profile: {json.dumps(profile, ensure_ascii=False)}")
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "profile.json").write_text(
            json.dumps(profile, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if image_url:
            if download_image(image_url, image_path):
                stats["downloaded"] += 1
                print(f"  ok  {sku}")
            else:
                stats["failed"] += 1
                print(f"  FAIL  {sku}  (profile written, no image)")
        else:
            log.warning("No image URL for %s", sku)
            stats["failed"] += 1

        time.sleep(0.1)

    return stats


# ─────────────────────────────────────────────────────────────
# Cardmarket price backfill — separate pass over existing cards.
# TCGdex's per-card DETAIL endpoint carries pricing; the set-list
# endpoint used by scrape_set() above does not, so this does its own
# per-card fetch rather than reusing card_set.cards.
# ─────────────────────────────────────────────────────────────

_TCGDEX_CARD_DETAIL_URL = "https://api.tcgdex.net/v2/ja/cards/{id}"

# Resolver field order matters: dict insertion order is what
# _extract_gbp_from_profile() (app.py) ends up picking from, since
# Cardmarket prices are flat (no variant dict) and each field is read
# via a plain `else: price = vdata` fallback in iteration order.
_CM_FIELD_MAP = [
    ("avg_sell", "avg"),
    ("low",      "low"),
    ("trend",    "trend"),
    ("avg_1d",   "avg1"),
    ("avg_7d",   "avg7"),
    ("avg_30d",  "avg30"),
]


def _fetch_card_detail(tcgdex_id: str, timeout: int = 8) -> dict:
    """GET the TCGdex ja card detail endpoint. Returns {} on 404/error
    (404 is expected/common for vintage and repo-only cards)."""
    url = _TCGDEX_CARD_DETAIL_URL.format(id=urllib.parse.quote(tcgdex_id, safe=''))
    for attempt in (1, 2):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt == 1:
                continue
            log.warning("Card detail fetch failed, skipping: %s — %s", tcgdex_id, e)
    return {}


def _build_cardmarket_fields(detail: dict):
    """Map a TCGdex card-detail response onto the resolver's schema.
    Returns None if there's no resolvable price (avg is null/missing —
    the expected case for ~42% of cards). Holo fields (*-holo) are
    dropped: English Cardmarket data tracks no holo split either."""
    cm = (detail.get("pricing") or {}).get("cardmarket") or {}
    if cm.get("avg") is None:
        return None

    flat = {}
    for out_key, src_key in _CM_FIELD_MAP:
        v = cm.get(src_key)
        if v is not None:
            flat[out_key] = v

    return {
        "prices": flat,
        "cardmarket_id": cm.get("idProduct"),
        "prices_updated": cm.get("updated"),
    }


def _extract_gbp_from_profile(profile):
    """Verbatim mirror of app.py:6367 _extract_gbp_from_profile() — kept
    here so verification entrypoints in this file don't need to import the
    full app.py (CLIP/DINOv2/PaddleOCR cold start)."""
    if not profile:
        return None
    prices = profile.get("prices") if isinstance(profile, dict) else None
    if not prices:
        return None
    for src, sdata in prices.items():
        if "ebay" in src.lower() or "amazon" in src.lower():
            continue
        if not isinstance(sdata, dict):
            continue
        for _var, vdata in sdata.items():
            if isinstance(vdata, dict):
                price = vdata.get("market") or vdata.get("mid") or vdata.get("trend") or vdata.get("avg_sell")
            else:
                price = vdata
            if price:
                mult = 0.86 if "cardmarket" in src else 0.79
                return round(float(price) * mult, 2)
    return None


def backfill_cardmarket_prices(db_root: Path, resume: bool, dry_run: bool) -> dict:
    """Walk existing jpn- card folders that have front.png and add/refresh
    Cardmarket pricing in profile.json. Does not touch images. Cards with
    no resolvable Cardmarket price (404 on TCGdex, or avg is null — repo-
    only/vintage cards typically) are left untouched, no error."""
    pokemon_dir = db_root / "pokemon"
    stats = {"checked": 0, "priced": 0, "unpriced": 0, "skipped_resume": 0, "errors": 0}

    folders = sorted(d for d in os.listdir(pokemon_dir) if d.startswith("jpn-"))
    print(f"[PRICES] {len(folders)} jpn- folders found under {pokemon_dir}", flush=True)

    for i, folder in enumerate(folders, 1):
        out_dir = pokemon_dir / folder
        if not (out_dir / "front.png").exists():
            continue

        profile_path = out_dir / "profile.json"
        if not profile_path.exists():
            continue

        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Could not read %s: %s", profile_path, e)
            stats["errors"] += 1
            continue

        if resume and profile.get("prices"):
            stats["skipped_resume"] += 1
            continue

        stats["checked"] += 1
        tcgdex_id = folder[len("jpn-"):]  # strip prefix only — keeps hyphenated set codes intact
        detail = _fetch_card_detail(tcgdex_id)
        fields = _build_cardmarket_fields(detail)

        if fields is None:
            stats["unpriced"] += 1
        else:
            stats["priced"] += 1
            if dry_run:
                print(f"  DRY-RUN  {folder}  prices={fields['prices']}  cardmarket_id={fields['cardmarket_id']}", flush=True)
            else:
                profile["prices"] = {"tcgplayer": {}, "cardmarket": fields["prices"]}
                if fields["cardmarket_id"] is not None:
                    profile["cardmarket_id"] = str(fields["cardmarket_id"])
                if fields["prices_updated"]:
                    profile["prices_updated"] = fields["prices_updated"]
                profile_path.write_text(
                    json.dumps(profile, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

        if i % 50 == 0:
            print(f"  ... {i}/{len(folders)} folders scanned "
                  f"(checked={stats['checked']} priced={stats['priced']} unpriced={stats['unpriced']})", flush=True)

        time.sleep(0.05)

    print(f"[PRICES] Done. {stats}", flush=True)
    return stats


def classify_unpriced(db_root: Path) -> dict:
    """Diagnostic-only pass: for every jpn- folder that has front.png but no
    'prices' key yet, re-fetch the TCGdex detail endpoint and bucket WHY it's
    unpriced — http_404 (card genuinely absent from the live API, expected
    for repo-only/vintage cards), http_200_no_avg (card exists, no resolvable
    Cardmarket avg), or other (anything that doesn't fit either — should be
    ~0; a non-trivial count here would mean a real logic gap)."""
    pokemon_dir = db_root / "pokemon"
    folders = sorted(d for d in os.listdir(pokemon_dir) if d.startswith("jpn-"))
    buckets = {"checked": 0, "http_404": 0, "http_200_no_avg": 0, "other": 0}
    samples = {"http_404": [], "http_200_no_avg": [], "other": []}

    for i, folder in enumerate(folders, 1):
        out_dir = pokemon_dir / folder
        if not (out_dir / "front.png").exists():
            continue
        profile_path = out_dir / "profile.json"
        if not profile_path.exists():
            continue
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except Exception as e:
            buckets["other"] += 1
            if len(samples["other"]) < 5:
                samples["other"].append(f"{folder}: read error {e}")
            continue

        if profile.get("prices"):
            continue  # already priced — not part of this diagnostic

        buckets["checked"] += 1
        tcgdex_id = folder[len("jpn-"):]
        url = _TCGDEX_CARD_DETAIL_URL.format(id=urllib.parse.quote(tcgdex_id, safe=''))

        try:
            resp = requests.get(url, timeout=8)
        except Exception as e:
            buckets["other"] += 1
            if len(samples["other"]) < 5:
                samples["other"].append(f"{folder}: request error {e}")
            continue

        if resp.status_code == 404:
            buckets["http_404"] += 1
            if len(samples["http_404"]) < 5:
                samples["http_404"].append(folder)
        elif resp.status_code == 200:
            try:
                detail = resp.json()
            except Exception as e:
                buckets["other"] += 1
                if len(samples["other"]) < 5:
                    samples["other"].append(f"{folder}: bad json {e}")
                continue
            cm = (detail.get("pricing") or {}).get("cardmarket") or {}
            if cm.get("avg") is None:
                buckets["http_200_no_avg"] += 1
                if len(samples["http_200_no_avg"]) < 5:
                    samples["http_200_no_avg"].append(folder)
            else:
                buckets["other"] += 1
                if len(samples["other"]) < 5:
                    samples["other"].append(f"{folder}: 200 with avg={cm.get('avg')} but not in 'prices' — should have been priced")
        else:
            buckets["other"] += 1
            if len(samples["other"]) < 5:
                samples["other"].append(f"{folder}: unexpected status {resp.status_code}")

        if i % 200 == 0:
            print(f"  ... {i}/{len(folders)} folders scanned ({buckets})", flush=True)

        time.sleep(0.05)

    buckets["samples"] = samples
    print(f"[CLASSIFY] Done. {buckets}", flush=True)
    return buckets


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Japanese Pokémon TCG cards from TCGdex into CardsDB"
    )
    parser.add_argument("--db-root", required=True,
                        help="Path to CardsDB root, e.g. C:\\CardsDB")
    parser.add_argument("--sets", default="",
                        help="Comma-separated TCGdex set IDs (omit to scrape ALL Japanese sets)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip cards where image already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be scraped without downloading")
    parser.add_argument("--list-sets", action="store_true",
                        help="List all available Japanese set IDs and card counts, then exit")
    parser.add_argument("--prices-only", action="store_true",
                        help="Backfill Cardmarket pricing into existing profile.json files "
                             "(folders with front.png already present) — no image fetch")
    args = parser.parse_args()

    db_root = Path(args.db_root)

    if args.prices_only:
        print(f"[PRICES] DB root: {db_root.resolve()}")
        print(f"[PRICES] Dry-run: {args.dry_run}  Resume: {args.resume}")
        backfill_cardmarket_prices(db_root, resume=args.resume, dry_run=args.dry_run)
        return

    from tcgdexsdk import TCGdex, Language  # lazy: only the image-scrape path needs this
    sdk = TCGdex(Language.JA)

    if args.list_sets:
        print("Fetching Japanese set list from TCGdex...")
        all_sets = sdk.set.listSync()
        print(f"\n{'ID':<25} {'Name':<45} {'Cards'}")
        print("-" * 80)
        for s in all_sets:
            total = s.cardCount.total if s.cardCount else "?"
            print(f"{s.id:<25} {s.name:<45} {total}")
        print(f"\nTotal: {len(all_sets)} sets")
        return

    print("Fetching Japanese set list from TCGdex...")
    all_sets = sdk.set.listSync()
    set_lookup = {s.id: s for s in all_sets}

    if args.sets:
        target_ids = [s.strip() for s in args.sets.split(",") if s.strip()]
        for sid in target_ids:
            if sid not in set_lookup:
                log.warning("Set '%s' not found in TCGdex JA — skipping", sid)
        target_ids = [sid for sid in target_ids if sid in set_lookup]
    else:
        target_ids = [s.id for s in all_sets]

    if not target_ids:
        print("No valid sets to scrape.")
        return

    print(f"\n[SCRAPER] Target: {len(target_ids)} set(s)")
    print(f"[SCRAPER] DB root: {db_root.resolve()}")
    print(f"[SCRAPER] Resume: {args.resume}  Dry-run: {args.dry_run}")

    grand = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    for i, set_id in enumerate(target_ids, 1):
        print(f"\n[{i}/{len(target_ids)}] {set_id}")
        stats = scrape_set(sdk, set_id, db_root,
                           resume=args.resume, dry_run=args.dry_run)
        for k in grand:
            grand[k] += stats[k]

        total = stats["total"]
        dl = stats["downloaded"]
        sk = stats["skipped"]
        fa = stats["failed"]
        print(f"  Summary — total:{total}  downloaded:{dl}  skipped:{sk}  failed:{fa}")

    print(f"\n{'='*60}")
    print(f"  GRAND TOTAL")
    print(f"  Total cards:   {grand['total']}")
    print(f"  Downloaded:    {grand['downloaded']}")
    print(f"  Skipped:       {grand['skipped']}")
    print(f"  Failed:        {grand['failed']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────
# Modal entry point — modal run scrape_pokemon_jpn.py::run_price_backfill
# Runs the Cardmarket price backfill directly against the live volume's
# CardsDB, so prices land where app.py actually serves from (no separate
# sync_profiles.py step needed for this).
# ─────────────────────────────────────────────────────────────

import modal

_price_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("requests")
    .add_local_file(__file__, "/app/scrape_pokemon_jpn.py")
)

price_app = modal.App("grailsweep-jpn-price-backfill")


@price_app.function(
    image=_price_image,
    volumes={"/modal_data": modal.Volume.from_name("matchit-data-v2", version=2)},
    timeout=3600,
)
def _run_price_backfill_remote(resume: bool = True, dry_run: bool = False):
    import sys
    sys.path.insert(0, "/app")
    from scrape_pokemon_jpn import backfill_cardmarket_prices
    return backfill_cardmarket_prices(Path("/modal_data/CardsDB"), resume=resume, dry_run=dry_run)


@price_app.local_entrypoint()
def run_price_backfill(resume: bool = True, dry_run: bool = False):
    result = _run_price_backfill_remote.remote(resume=resume, dry_run=dry_run)
    print(json.dumps(result, indent=2))


@price_app.function(
    image=_price_image,
    volumes={"/modal_data": modal.Volume.from_name("matchit-data-v2", version=2)},
    timeout=60,
)
def _probe_one_remote(folder: str = "jpn-sv4m-001"):
    import sys
    sys.path.insert(0, "/app")
    from scrape_pokemon_jpn import (
        _fetch_card_detail,
        _build_cardmarket_fields,
        _extract_gbp_from_profile,
        _TCGDEX_CARD_DETAIL_URL,
    )
    import requests as _requests

    out_dir = Path("/modal_data/CardsDB/pokemon") / folder
    profile_path = out_dir / "profile.json"

    tcgdex_id = folder[len("jpn-"):]
    url = _TCGDEX_CARD_DETAIL_URL.format(id=tcgdex_id)
    print(f"[PROBE] folder read: {folder}", flush=True)
    print(f"[PROBE] tcgdex_id sent: {tcgdex_id!r}", flush=True)
    print(f"[PROBE] URL: {url}", flush=True)

    raw = _requests.get(url, timeout=8)
    print(f"[PROBE] HTTP status: {raw.status_code}", flush=True)

    # Reuse the EXACT same fetch/extract code path the backfill uses.
    detail = _fetch_card_detail(tcgdex_id)
    print(f"[PROBE] detail has 'pricing' key: {'pricing' in detail}", flush=True)
    fields = _build_cardmarket_fields(detail)
    print(f"[PROBE] extracted fields: {fields}", flush=True)

    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    if fields is not None:
        profile["prices"] = {"tcgplayer": {}, "cardmarket": fields["prices"]}
        if fields["cardmarket_id"] is not None:
            profile["cardmarket_id"] = str(fields["cardmarket_id"])
        if fields["prices_updated"]:
            profile["prices_updated"] = fields["prices_updated"]
        profile_path.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[PROBE] wrote prices to {profile_path}", flush=True)
    else:
        print("[PROBE] no resolvable price — profile.json left unchanged", flush=True)

    print(f"[PROBE] profile['prices'] now: {profile.get('prices')}", flush=True)
    gbp = _extract_gbp_from_profile(profile)
    print(f"[PROBE] _extract_gbp_from_profile() -> {gbp}", flush=True)
    return {"status": raw.status_code, "fields": fields, "gbp": gbp}


@price_app.local_entrypoint()
def probe_one(folder: str = "jpn-sv4m-001"):
    result = _probe_one_remote.remote(folder=folder)
    print(json.dumps(result, indent=2))


@price_app.function(
    image=_price_image,
    volumes={"/modal_data": modal.Volume.from_name("matchit-data-v2", version=2)},
    timeout=3600,
)
def _classify_unpriced_remote():
    import sys
    sys.path.insert(0, "/app")
    from scrape_pokemon_jpn import classify_unpriced
    return classify_unpriced(Path("/modal_data/CardsDB"))


@price_app.local_entrypoint()
def classify_unpriced_cli():
    result = _classify_unpriced_remote.remote()
    print(json.dumps(result, indent=2))


# Known card ids, found via targeted TCGdex API lookups (NOT a volume
# scan) — picked to span the value range: two cheap, two mid, one high.
# folder name = "jpn-" + this id, e.g. "jpn-sv8-001".
_SPOT_CHECK_IDS = {
    "cheapest_1":     "sv8-001",     # avg 0.03 EUR
    "cheapest_2":     "sv4m-001",    # avg 0.04 EUR
    "mid_1":          "sv9a-002",    # avg 1.65 EUR
    "mid_2":          "sv9a-064",    # avg 1.65 EUR
    "most_expensive": "sv9a-022",    # avg 7.03 EUR (highest found by targeted lookup, not a verified global max)
}


@price_app.function(
    image=_price_image,
    volumes={"/modal_data": modal.Volume.from_name("matchit-data-v2", version=2)},
    timeout=300,
)
def _spot_check_remote():
    import sys
    sys.path.insert(0, "/app")
    from scrape_pokemon_jpn import _extract_gbp_from_profile
    import requests as _requests

    pokemon_dir = Path("/modal_data/CardsDB/pokemon")
    picks = {}
    for label, card_id in _SPOT_CHECK_IDS.items():
        folder = "jpn-" + card_id
        profile_path = pokemon_dir / folder / "profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        cm = (profile.get("prices") or {}).get("cardmarket") or {}
        avg_sell = cm.get("avg_sell")
        picks[label] = (folder, avg_sell, profile)

    # Live FX rate, fetched directly (mirrors /api/fx_rates -> frankfurter.app),
    # to compare against the static 0.86 fallback used inside
    # _extract_gbp_from_profile() itself (which never calls live FX — that
    # live/fallback branch lives only in the results.html client-side JS).
    live_eur_rate = None
    try:
        r = _requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=GBP,EUR",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        rates = r.json().get("rates", {})
        if rates.get("GBP") and rates.get("EUR"):
            live_eur_rate = rates["GBP"] / rates["EUR"]
    except Exception as e:
        print(f"[SPOTCHECK] live FX fetch failed: {e}", flush=True)

    print(f"[SPOTCHECK] live EUR->GBP cross rate (frankfurter.app via USD): {live_eur_rate}", flush=True)
    print(f"[SPOTCHECK] static fallback used by _extract_gbp_from_profile(): 0.86", flush=True)

    results = {}
    for label, (folder, avg_sell, profile) in picks.items():
        gbp = _extract_gbp_from_profile(profile)
        cardmarket_id = profile.get("cardmarket_id")
        live_gbp = round(avg_sell * live_eur_rate, 4) if live_eur_rate else None
        entry = {
            "folder": folder,
            "avg_sell_eur": avg_sell,
            "cardmarket_id": cardmarket_id,
            "gbp_static_resolver": gbp,
            "gbp_if_live_fx": live_gbp,
            "sub_penny": gbp is not None and gbp < 0.01,
        }
        results[label] = entry
        print(f"[SPOTCHECK] {label}: {entry}", flush=True)

    return results


@price_app.local_entrypoint()
def spot_check():
    result = _spot_check_remote.remote()
    print(json.dumps(result, indent=2))
