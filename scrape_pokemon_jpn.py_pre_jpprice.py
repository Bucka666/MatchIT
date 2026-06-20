"""
scrape_pokemon_jpn.py — Scrape Japanese Pokémon sets from TCGdex
================================================================
Writes output into CardsDB/pokemon/ using jpn- SKU prefix.

Usage:
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --list-sets
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --sets sv6 --dry-run
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --sets sv6,sv7
    python scrape_pokemon_jpn.py --db-root C:\CardsDB --resume
"""

import os
import sys
import json
import time
import argparse
import requests
import logging
import urllib.parse
from pathlib import Path
from tcgdexsdk import TCGdex, Language

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
    args = parser.parse_args()

    sdk = TCGdex(Language.JA)
    db_root = Path(args.db_root)

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
