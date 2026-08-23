"""
ingest_jp_m_sm_sets.py — Steps 1-4 of the zero-SKU JP M-series / SM-era set
ingestion (mirrors ingest_jp_swsh_sets.py's pattern exactly).

For each of the 24 target sets (all confirmed live via resultAPI.php in the
M-series/SM-era probe, and confirmed to have zero pre-existing
identifier_lookup entries as of that probe):
  1. Paginate pokemon-card.com resultAPI.php, collect cardID/cardNameAltText/
     cardNameViewText, cross-reference against jp_cardid_maps/{set}_map.json
     to get each card's collector number.
  2. Build jpn-{set}-{NNN} SKU entries for every card with a mapped number.
  3. Fill in set_metadata.json's printed_total (currently null for all 24 --
     the earlier scrape step already added exclude/game/name/ptcgoCode/total/
     jp_image_scrape_count entries) derived the same way as
     ingest_jp_swsh_sets.py: max() of mapped collector numbers, not longest
     contiguous run (a handful of scattered OCR failures breaks contiguity
     without the true total being smaller).
  4. Write identifier_lookup.json and set_metadata.json (append-only, no
     existing entries touched other than filling null printed_total).

Safety: aborts before writing anything if any of the 24 sets already have
identifier_lookup entries (would indicate this has already been run).

Excludes m1s, m2, m2a, m3, m4, m5 -- those already have real number-based
SKUs in identifier_lookup.json from earlier ingestion; they get linked via
link_jp_skus.py directly rather than needing new SKUs minted here.
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

TARGET_SETS = [
    "m6",
    "sm1m", "sm1s", "sm2k", "sm3n", "sm4a", "sm4s", "sm5m", "sm5s",
    "sm6", "sm6a", "sm7", "sm7a", "sm8", "sm8a", "sm9", "sm9a",
    "sm10", "sm10a", "sm10b", "sm11", "sm11a", "sm12", "sm12a",
]

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
    print(f"  Derived printed_total (max):  {printed_total}")

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
    print(f"Safety check passed: zero existing identifier_lookup entries for all {len(TARGET_SETS)} sets.\n")

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
            new_skus[sku] = sku

    print(f"\n{'=' * 70}\nSTEP 2: New SKU entries\n{'=' * 70}")
    for code in TARGET_SETS:
        print(f"  {code}: {len(per_set_new_skus[code])} SKUs")
    print(f"  TOTAL new SKU entries: {len(new_skus)}")

    # --- Step 3: set_metadata entries ---
    print(f"\n{'=' * 70}\nSTEP 3: set_metadata.json updates\n{'=' * 70}")
    new_meta_entries = {}
    for code in TARGET_SETS:
        key = f"jpn-{code}"
        res = results[code]
        existing = set_meta.get(key)
        if existing is not None:
            entry = dict(existing)
            if entry.get("printed_total") is None and res["printed_total"]:
                entry["printed_total"] = res["printed_total"]
                print(f"  {key}: filling null printed_total with derived {res['printed_total']}")
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
    print(f"set_metadata.json: {len(new_meta_entries)} set entries updated")


if __name__ == "__main__":
    main()
