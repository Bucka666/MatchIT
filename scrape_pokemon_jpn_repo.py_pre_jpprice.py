"""
scrape_pokemon_jpn_repo.py — Scrape Japanese Pokémon cards from a local
clone of the TCGdex cards-database repo (data-asia/).
=========================================================================
Complements scrape_pokemon_jpn.py — picks up cards the live TCGdex API
returns empty `cards: []` for (e.g. neo1-4, S9/S9a, many vintage sets),
by reading the per-card .ts source files directly.

Writes output into CardsDB/pokemon/ using jpn- SKU prefix, same
profile.json schema as scrape_pokemon_jpn.py.

Usage:
    python scrape_pokemon_jpn_repo.py --repo-root C:\\Temp\\tcgdex-db --db-root C:\\CardsDB --series SV --dry-run
    python scrape_pokemon_jpn_repo.py --repo-root C:\\Temp\\tcgdex-db --db-root C:\\CardsDB --resume
"""

import os
import sys
import json
import re
import time
import argparse
import requests
import logging
from pathlib import Path

# Japanese card names contain CJK characters — force UTF-8 on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

NAME_JA_RE = re.compile(r"['\"]?ja['\"]?\s*:\s*['\"]([^'\"]+)['\"]")
NAME_FALLBACK_RE = re.compile(r"['\"]?(?:zh-tw|zh-cn|en|th|ko)['\"]?\s*:\s*['\"]([^'\"]+)['\"]")
RARITY_STR_RE = re.compile(r'rarity\s*:\s*["\']([^"\']+)["\']')
RARITY_ENUM_RE = re.compile(r'rarity\s*:\s*Rarity\.(\w+)')


def parse_card_file(text: str):
    """Extract (name, rarity) from a card .ts file's source text."""
    name = None
    m = NAME_JA_RE.search(text)
    if m:
        name = m.group(1)
    else:
        m = NAME_FALLBACK_RE.search(text)
        if m:
            name = m.group(1)

    rarity = None
    m = RARITY_STR_RE.search(text)
    if m:
        rarity = m.group(1)
    else:
        m = RARITY_ENUM_RE.search(text)
        if m:
            rarity = m.group(1)

    return name, rarity


def find_card_files(data_asia: Path, series_filter):
    """Yield (series, set_id, local_id, card_file) for every per-card .ts file."""
    for series_dir in sorted(data_asia.iterdir()):
        if not series_dir.is_dir():
            continue
        series = series_dir.name
        if series_filter and series not in series_filter:
            continue

        for set_dir in sorted(series_dir.iterdir()):
            if not set_dir.is_dir():
                continue
            if set_dir.name == series:
                continue
            set_id = set_dir.name

            for card_file in sorted(set_dir.glob("*.ts")):
                local_id = card_file.stem
                yield series, set_id, local_id, card_file


def download_image(url: str, dest: Path, timeout: int = 5) -> bool:
    """Download image to dest. Retries once on 404/error. Returns True on success."""
    for attempt in (1, 2):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 404:
                if attempt == 1:
                    continue
                return False
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return True
        except Exception as e:
            if attempt == 1:
                log.warning("Image download failed (attempt 1), retrying: %s — %s", url, e)
            else:
                log.warning("Image download failed (attempt 2), skipping: %s — %s", url, e)
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Japanese Pokémon cards from a local TCGdex cards-database clone"
    )
    parser.add_argument("--repo-root", default=r"C:\Temp\tcgdex-db",
                        help="Path to cloned tcgdex/cards-database repo")
    parser.add_argument("--db-root", required=True,
                        help="Path to CardsDB root, e.g. C:\\CardsDB")
    parser.add_argument("--resume", action="store_true",
                        help="Skip cards where front.png already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be scraped without writing anything")
    parser.add_argument("--series", default="",
                        help="Comma-separated series to scrape (e.g. SM,SV) — omit for all")
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    db_root = Path(args.db_root)
    data_asia = repo_root / "data-asia"

    if not data_asia.is_dir():
        log.error("data-asia not found under %s", repo_root)
        return

    series_filter = None
    if args.series:
        series_filter = {s.strip() for s in args.series.split(",") if s.strip()}

    print(f"[SCRAPER] repo-root: {repo_root.resolve()}")
    print(f"[SCRAPER] db-root:   {db_root.resolve()}")
    print(f"[SCRAPER] series:    {', '.join(series_filter) if series_filter else 'ALL'}")
    print(f"[SCRAPER] resume: {args.resume}  Dry-run: {args.dry_run}")

    grand = {
        "processed": 0,
        "already_exist": 0,
        "new": 0,
        "updated": 0,
        "downloaded": 0,
        "profile_only": 0,
        "skipped": 0,
    }

    series_stats = {}
    shown = 0

    for series, set_id, local_id, card_file in find_card_files(data_asia, series_filter):
        s_stats = series_stats.setdefault(series, {
            "processed": 0, "already_exist": 0, "new": 0,
            "updated": 0, "downloaded": 0, "profile_only": 0, "skipped": 0,
        })

        sku = f"jpn-{set_id.lower()}-{local_id}"
        out_dir = db_root / "pokemon" / sku
        image_path = out_dir / "front.png"
        profile_path = out_dir / "profile.json"

        front_exists = image_path.exists()
        profile_exists = profile_path.exists()

        if front_exists:
            grand["already_exist"] += 1
            s_stats["already_exist"] += 1

        if args.resume and front_exists:
            grand["skipped"] += 1
            s_stats["skipped"] += 1
            continue

        try:
            text = card_file.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Failed to read %s: %s", card_file, e)
            continue

        name, rarity = parse_card_file(text)
        image_url = f"https://assets.tcgdex.net/ja/{series}/{set_id}/{local_id}/high.png"

        profile = {
            "api_id": sku,
            "name": name,
            "number": local_id,
            "card_number": local_id,
            "set_id": set_id,
            "set_name": None,
            "lang": "ja",
            "category": "POKEMON",
            "rarity": rarity,
            "image_url": image_url,
        }

        grand["processed"] += 1
        s_stats["processed"] += 1
        if profile_exists:
            grand["updated"] += 1
            s_stats["updated"] += 1
        else:
            grand["new"] += 1
            s_stats["new"] += 1

        if args.dry_run:
            if shown < 5:
                print(f"  DRY-RUN  {sku}  name={name!r}  rarity={rarity!r}  image={image_url}")
                shown += 1
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if args.resume and profile_exists and not front_exists:
            # profile already existed but image is missing — try to fetch it
            pass

        if download_image(image_url, image_path):
            grand["downloaded"] += 1
            s_stats["downloaded"] += 1
            print(f"  ok  {sku}")
        else:
            grand["profile_only"] += 1
            s_stats["profile_only"] += 1
            print(f"  profile-only  {sku}  (no image)")

        time.sleep(0.05)

    print(f"\n{'='*60}")
    print("  PER-SERIES SUMMARY")
    print(f"{'='*60}")
    for series in sorted(series_stats):
        st = series_stats[series]
        print(f"  {series:<8} processed:{st['processed']:<5} new:{st['new']:<5} "
              f"updated:{st['updated']:<5} downloaded:{st['downloaded']:<5} "
              f"profile_only:{st['profile_only']:<5} skipped:{st['skipped']:<5} "
              f"already_had_image:{st['already_exist']}")

    print(f"\n{'='*60}")
    print("  GRAND TOTAL")
    print(f"  Processed:       {grand['processed']}")
    print(f"  Already existed (front.png present): {grand['already_exist']}")
    print(f"  New profiles:    {grand['new']}")
    print(f"  Updated profiles:{grand['updated']}")
    print(f"  Images downloaded: {grand['downloaded']}")
    print(f"  Profile-only:    {grand['profile_only']}")
    print(f"  Skipped (resume):{grand['skipped']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
