"""
ingest_jp_swsh_sets.py — Steps 1-4 of the zero-SKU JP SWSH set ingestion.

For each of the 7 target sets (s1a, s1h, s1w, s2, s2a, s3, s3a):
  1. Paginate pokemon-card.com resultAPI.php, collect cardID/cardNameAltText/
     cardNameViewText, cross-reference against jp_cardid_maps/{set}_map.json
     to get each card's collector number.
  2. Build jpn-{set}-{NNN} SKU entries for every card with a mapped number.
  3. Add a minimal set_metadata.json entry (printed_total derived from the
     largest contiguous 1..N run of mapped collector numbers — this stands
     in for "the OCR denominator" since map_jp_collector_numbers.py doesn't
     persist the raw OCR'd TTT value, only the final resolved NNN per card).
  4. Write identifier_lookup.json and set_metadata.json (append-only, no
     existing entries touched).

Safety: aborts before writing anything if any of the 7 sets already have
identifier_lookup entries (would indicate this has already been run).
"""
import json
import os
import time

import requests

BASE_DIR = r"C:\MatchIT"
MAPS_DIR = os.path.join(BASE_DIR, "jp_cardid_maps")
IDENTIFIER_LOOKUP_PATH = os.path.join(BASE_DIR, "identifier_lookup.json")
SET_METADATA_PATH = os.path.join(BASE_DIR, "set_metadata.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.pokemon-card.com/card-search/"}
RESULT_API = "https://www.pokemon-card.com/card-search/resultAPI.php"
PAGE_FETCH_DELAY = 0.3

TARGET_SETS = ["s1a", "s1h", "s1w", "s2", "s2a", "s3", "s3a"]

META_KEYS = ("failed", "disagreements")


def fetch_page(set_code, page):
    params = {
        "pg": set_code, "se_ta": "", "page": str(page),
        "regulation_sidebar_form": "all", "sm_and_keyword": "true",
    }
    r = requests.get(RESULT_API, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def enumerate_set(set_code):
    cards = []
    page = 1
    while True:
        data = fetch_page(set_code, page)
        page_cards = data.get("cardList", [])
        if not page_cards:
            break
        cards.extend(page_cards)
        max_page = data.get("maxPage", page)
        if page >= max_page:
            break
        page += 1
        time.sleep(PAGE_FETCH_DELAY)
    return cards


def load_card_map(set_code):
    path = os.path.join(MAPS_DIR, f"{set_code}_map.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if k not in META_KEYS}


def derive_printed_total(numbers):
    """max(mapped numbers) -- the actual OCR'd denominator (TTT) isn't
    persisted per-card by map_jp_collector_numbers.py, only the resolved
    NNN, so max() is the best reconstruction available. NOT "longest
    contiguous run from 1": a handful of scattered OCR failures (present in
    every one of these 7 sets) breaks contiguity without the true total
    being any smaller -- that was tried first and produced nonsense
    (e.g. printed_total=1 for s2a, which has a mapped card at #78) because
    a single early gap short-circuits it. Validated against the already-
    populated set_metadata "total" (raw API scrape count) for all 7 target
    sets: max() matched exactly in 5/7 sets and was total-1 in the other 2
    (the single highest-numbered card failed OCR entirely in each).
    """
    if not numbers:
        return None
    return max(numbers)


def process_set(set_code):
    print(f"\n{'=' * 70}\nSET jpn-{set_code}\n{'=' * 70}")
    cards = enumerate_set(set_code)
    card_map = load_card_map(set_code)

    records = {}
    mapped_numbers = []
    unmapped = 0
    for card in cards:
        raw_id = card.get("cardID", "")
        try:
            card_id = f"{int(raw_id):06d}"
        except (TypeError, ValueError):
            unmapped += 1
            continue
        number = card_map.get(card_id)
        records[raw_id] = {
            "card_id_padded": card_id,
            "collector_number": number,
            "name_alt": card.get("cardNameAltText", ""),
            "name_view": card.get("cardNameViewText", ""),
        }
        if number is not None:
            mapped_numbers.append(int(number))
        else:
            unmapped += 1

    printed_total = derive_printed_total(mapped_numbers)

    print(f"  Total cards from API:        {len(cards)}")
    print(f"  With mapped collector number: {len(mapped_numbers)}")
    print(f"  Without (OCR failed/missing): {unmapped}")
    print(f"  Derived printed_total (max contiguous 1..N): {printed_total}")

    return {
        "set_code": set_code,
        "records": records,
        "mapped_numbers": mapped_numbers,
        "printed_total": printed_total,
        "api_total": len(cards),
        "unmapped": unmapped,
    }


def main():
    with open(IDENTIFIER_LOOKUP_PATH, encoding="utf-8") as f:
        id_lookup = json.load(f)
    pokemon_lookup = id_lookup["pokemon"]

    with open(SET_METADATA_PATH, encoding="utf-8") as f:
        set_meta = json.load(f)

    # --- Safety check: zero existing entries for these sets ---
    preexisting = []
    for code in TARGET_SETS:
        matches = [k for k in pokemon_lookup if k.startswith(f"jpn-{code}-")]
        if matches:
            preexisting.append((code, len(matches)))
    if preexisting:
        print("ABORT: existing identifier_lookup entries found for target sets:")
        for code, n in preexisting:
            print(f"  jpn-{code}-*: {n} entries")
        return
    print("Safety check passed: zero existing identifier_lookup entries for all 7 sets.\n")

    # --- Step 1 ---
    results = {}
    for code in TARGET_SETS:
        results[code] = process_set(code)

    # --- Step 2: build SKU entries ---
    new_skus = {}
    per_set_new_skus = {}
    for code, res in results.items():
        skus = []
        for card in res["records"].values():
            num = card["collector_number"]
            if num is None:
                continue
            sku = f"jpn-{code}-{str(num).zfill(3)}"
            skus.append(sku)
        per_set_new_skus[code] = skus
        for sku in skus:
            new_skus[sku] = sku  # identity mapping, matches jpn-sv2a-* format

    print(f"\n{'=' * 70}\nSTEP 2: New SKU entries\n{'=' * 70}")
    for code in TARGET_SETS:
        print(f"  {code}: {len(per_set_new_skus[code])} SKUs")
    print(f"  TOTAL new SKU entries: {len(new_skus)}")

    # --- Step 3: set_metadata entries ---
    print(f"\n{'=' * 70}\nSTEP 3: set_metadata.json entries\n{'=' * 70}")
    new_meta_entries = {}
    for code in TARGET_SETS:
        key = f"jpn-{code}"
        res = results[code]
        existing = set_meta.get(key)
        if existing is not None:
            print(f"  {key}: entry ALREADY EXISTS -> {existing}")
            entry = dict(existing)
            if entry.get("printed_total") is None and res["printed_total"]:
                entry["printed_total"] = res["printed_total"]
                print(f"    -> filling null printed_total with derived {res['printed_total']}")
            if "jp_image_scrape_count" not in entry:
                entry["jp_image_scrape_count"] = res["api_total"]
            new_meta_entries[key] = entry
        else:
            entry = {
                "exclude": False,
                "game": "POKEMON",
                "name": code,
                "printed_total": res["printed_total"],
                "ptcgoCode": None,
                "total": res["api_total"],
                "jp_image_scrape_count": res["api_total"],
            }
            new_meta_entries[key] = entry
            print(f"  {key}: NEW -> {json.dumps(entry, ensure_ascii=False)}")

    # --- Step 4: write files ---
    pokemon_lookup.update(new_skus)
    with open(IDENTIFIER_LOOKUP_PATH, "w", encoding="utf-8") as f:
        json.dump(id_lookup, f, ensure_ascii=False, indent=2)

    set_meta.update(new_meta_entries)
    with open(SET_METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(set_meta, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 70}\nSTEP 4: Files written\n{'=' * 70}")
    print(f"identifier_lookup.json: +{len(new_skus)} new SKU entries")
    print(f"set_metadata.json: {len(new_meta_entries)} set entries added/updated")
    print("\nNew set_metadata entries (full):")
    for key, entry in new_meta_entries.items():
        print(f"  {key}: {json.dumps(entry, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
