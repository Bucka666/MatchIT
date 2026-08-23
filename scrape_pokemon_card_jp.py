# scrape_pokemon_card_jp.py
# Downloads card images from pokemon-card.com (JP official site) for missing
# SWSH S-series sets and stages them under C:\CardsDB\jpn-{setcode}\.
#
# Mechanism confirmed in scratch/check_pokemon_card_jp_urls.py:
#   GET https://www.pokemon-card.com/card-search/resultAPI.php
#       ?pg={SET_CODE}&se_ta=&page={N}&regulation_sidebar_form=all&sm_and_keyword=true
#   pg accepts the plain alphanumeric set code directly (case-insensitive) —
#   no internal numeric product-ID lookup needed. Paginate page=1..maxPage.
#   Each cardList entry gives cardThumbFile, a relative image path:
#     /assets/images/card_images/large/{SET_CODE}/{cardID:06d}_P_{ROMANIZED_NAME}.jpg
#
# SKU MAPPING NOTE (see Step 1/4 investigation): resultAPI.php's cardList
# ONLY exposes {cardID, cardThumbFile, cardNameAltText, cardNameViewText} —
# there is no collector-number field anywhere in the response. So this
# script cannot know that a given image is "card #129" — it only knows the
# site's internal global cardID. Images are staged by cardID
# (C:\CardsDB\jpn-{setcode}\{cardID:06d}.jpg). Many of these sets (s4
# onward) already have real per-number SKUs in identifier_lookup.json
# (e.g. jpn-s12a-129) with pricing data already synced — this script does
# NOT attempt to match cardID images to those existing number-based SKUs.
# That requires a separate name-matching (or visual-matching) pass once
# images are on disk. images.db / embeddings are intentionally NOT touched
# here — that's a separate CLIP embedding build step after images are
# confirmed correct on disk.
#
# set_metadata.json SAFETY NOTE: several of these sets already carry
# authoritative printed_total/total values scraped from elsewhere (e.g.
# jpn-s12a: printed_total=172, total=254). This script's raw per-cardID
# image count is NOT the same number (e.g. s12a returns 351 raw images,
# because parallel/foil reprints get distinct cardIDs under the same set).
# Overwriting an existing non-null total with that raw count would corrupt
# real data. So: existing non-null printed_total/total fields are left
# untouched; the scraped count is written to a new "jp_image_scrape_count"
# field instead. Only brand-new entries (no prior set_metadata row) get
# "total" populated directly from the scrape count, since there's nothing
# authoritative to protect. New entries follow the EXISTING schema shape
# (exclude/game/name/printed_total/ptcgoCode/total) — no "language" field,
# since none of the other 1254 entries in set_metadata.json have one.

import argparse
import json
import os
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.pokemon-card.com/card-search/"}

RESULT_API = "https://www.pokemon-card.com/card-search/resultAPI.php"
IMAGE_BASE = "https://www.pokemon-card.com"

CARDSDB_ROOT = r"C:\CardsDB"
SET_METADATA_PATH = r"C:\MatchIT\set_metadata.json"

IMAGE_DOWNLOAD_DELAY = 1.0   # seconds, between image downloads (be polite)
PAGE_FETCH_DELAY = 0.3       # seconds, between resultAPI.php page requests

# All missing SWSH S-series sets confirmed live against resultAPI.php
# (batch-verified 2026-08-22; bare "s1" returns 0 hits — SWSH1 was only ever
# sold as the two half-decks s1w/s1h, both of which work).
DEFAULT_SET_CODES = [
    "s1a", "s1w", "s1h",
    "s2", "s2a",
    "s3", "s3a",
    "s4", "s4a",
    "s5r", "s5i", "s5a",
    "s6h", "s6k", "s6a",
    "s7d", "s7r",
    "s8", "s8a", "s8b",
    "s9", "s9a",
    "s10a", "s10b", "s10d", "s10p",
    "s11", "s11a",
    "s12", "s12a",
]


def fetch_page(set_code, page):
    params = {
        "pg": set_code, "se_ta": "", "page": str(page),
        "regulation_sidebar_form": "all", "sm_and_keyword": "true",
    }
    r = requests.get(RESULT_API, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def enumerate_set(set_code):
    """Yield every card dict for a set by paginating resultAPI.php.
    Returns (cards, error) — error is None on success, else a message."""
    cards = []
    page = 1
    while True:
        try:
            data = fetch_page(set_code, page)
        except requests.RequestException as e:
            return cards, f"HTTP error on page {page}: {e}"
        except ValueError as e:
            return cards, f"JSON parse error on page {page}: {e}"

        page_cards = data.get("cardList", [])
        if not page_cards:
            break
        cards.extend(page_cards)

        max_page = data.get("maxPage", page)
        if page >= max_page:
            break
        page += 1
        time.sleep(PAGE_FETCH_DELAY)
    return cards, None


def download_image(url, dest_path):
    resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def process_set(set_code, dry_run):
    set_id = f"jpn-{set_code}"
    dest_dir = os.path.join(CARDSDB_ROOT, set_id)

    print(f"\n{'=' * 70}")
    print(f"SET {set_id}  ({'DRY RUN' if dry_run else 'LIVE'})")
    print("=" * 70)

    cards, err = enumerate_set(set_code)
    if err:
        print(f"  [ERROR] Failed to enumerate {set_id}: {err}")
        return {"set_id": set_id, "downloaded": 0, "skipped": 0, "failed": 0,
                "total_found": len(cards), "error": err}

    print(f"  Found {len(cards)} card image(s) via resultAPI.php")

    if not dry_run:
        os.makedirs(dest_dir, exist_ok=True)

    downloaded = 0
    skipped = 0
    failed = 0

    for card in cards:
        card_id = card.get("cardID", "")
        thumb = card.get("cardThumbFile", "")
        name = card.get("cardNameViewText", "")

        if not card_id or not thumb:
            print(f"  [SKIP] Malformed card entry, missing cardID/cardThumbFile: {card}")
            failed += 1
            continue

        image_url = thumb if thumb.startswith("http") else IMAGE_BASE + thumb
        filename = f"{int(card_id):06d}.jpg"
        dest_path = os.path.join(dest_dir, filename)

        if os.path.exists(dest_path):
            if dry_run:
                print(f"  [WOULD SKIP, exists] {dest_path}")
            skipped += 1
            continue

        if dry_run:
            print(f"  [WOULD DOWNLOAD] {image_url}")
            print(f"                -> {dest_path}  (name: {name})")
            downloaded += 1
            continue

        try:
            download_image(image_url, dest_path)
            downloaded += 1
        except requests.RequestException as e:
            print(f"  [FAILED] {image_url} -> {e}")
            failed += 1
        except OSError as e:
            print(f"  [FAILED] Could not write {dest_path} -> {e}")
            failed += 1

        time.sleep(IMAGE_DOWNLOAD_DELAY)

    print(f"\n  {set_id} summary: downloaded={downloaded} skipped={skipped} "
          f"failed={failed} total_found={len(cards)}")

    return {
        "set_id": set_id, "downloaded": downloaded, "skipped": skipped,
        "failed": failed, "total_found": len(cards), "error": None,
    }


def update_set_metadata(results, dry_run):
    if dry_run:
        print("\n[DRY RUN] Skipping set_metadata.json update.")
        return

    with open(SET_METADATA_PATH, encoding="utf-8") as f:
        set_meta = json.load(f)

    changed = False
    for r in results:
        if r["error"] or r["total_found"] == 0:
            continue
        set_id = r["set_id"]
        entry = set_meta.get(set_id)

        if entry is None:
            set_meta[set_id] = {
                "exclude": False,
                "game": "POKEMON",
                "name": set_id.replace("jpn-", ""),
                "printed_total": None,
                "ptcgoCode": None,
                "total": r["total_found"],
            }
            print(f"  [set_metadata] Added new entry for {set_id} "
                  f"(total={r['total_found']})")
            changed = True
        else:
            entry["jp_image_scrape_count"] = r["total_found"]
            existing_total = entry.get("total")
            if existing_total is None:
                entry["total"] = r["total_found"]
                print(f"  [set_metadata] {set_id}: filled null total -> "
                      f"{r['total_found']}")
            else:
                print(f"  [set_metadata] {set_id}: existing total={existing_total} "
                      f"left untouched (raw scrape includes parallel/reprint "
                      f"cardIDs, not a true collector-number count); "
                      f"jp_image_scrape_count={r['total_found']} recorded instead")
            changed = True

    if changed:
        with open(SET_METADATA_PATH, "w", encoding="utf-8") as f:
            json.dump(set_meta, f, ensure_ascii=False, indent=2)
        print(f"\nset_metadata.json written ({SET_METADATA_PATH})")
    else:
        print("\nNo set_metadata.json changes needed.")


def main():
    parser = argparse.ArgumentParser(description="Scrape JP card images from pokemon-card.com")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would be downloaded, write nothing")
    parser.add_argument("--sets", type=str, default=None,
                         help="Comma-separated set codes to process (default: all missing SWSH S-series sets)")
    args = parser.parse_args()

    set_codes = args.sets.split(",") if args.sets else DEFAULT_SET_CODES
    set_codes = [c.strip() for c in set_codes if c.strip()]

    print(f"Processing {len(set_codes)} set(s): {set_codes}")
    print(f"Mode: {'DRY RUN (no downloads, no writes)' if args.dry_run else 'LIVE'}")

    results = []
    for code in set_codes:
        result = process_set(code, dry_run=args.dry_run)
        results.append(result)

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    total_downloaded = sum(r["downloaded"] for r in results)
    total_skipped = sum(r["skipped"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    total_found = sum(r["total_found"] for r in results)

    for r in results:
        status = f"ERROR: {r['error']}" if r["error"] else "OK"
        print(f"  {r['set_id']:<14} found={r['total_found']:<5} "
              f"downloaded={r['downloaded']:<5} skipped={r['skipped']:<5} "
              f"failed={r['failed']:<5} {status}")

    print(f"\nTOTAL: found={total_found} downloaded={total_downloaded} "
          f"skipped={total_skipped} failed={total_failed}")

    update_set_metadata(results, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
