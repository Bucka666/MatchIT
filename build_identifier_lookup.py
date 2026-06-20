"""
build_identifier_lookup.py — build identifier->SKU lookup for OCR-first matching.

Scans all CardsDB profile.json files and builds a nested dict keyed by game:
  {
    "pokemon": { "sv6-25": "sv6-25", ... },
    "mtg":     { "me1-1": "mtg-me1-1", "mom-263": "mtg-mom-263", ... },
    "ygo":     { "SDBT-EN011": "ygo-SDBT-EN011-...", "89631139": "...", ... }
  }

Output: C:\\MatchIT\\identifier_lookup.json
"""
import json
import os
import re

CARDSDB = r"C:\CardsDB"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identifier_lookup.json")

lookup = {"pokemon": {}, "mtg": {}, "ygo": {}}
collisions = {"pokemon": 0, "mtg": 0, "ygo": 0}
counts = {"pokemon": 0, "mtg": 0, "ygo_setcode": 0, "ygo_passcode": 0}
skipped = {"pokemon": 0, "mtg": 0, "ygo": 0}


def add_key(game, key, sku, bucket):
    if not key:
        return
    sub = lookup[game]
    if key in sub:
        if sub[key] != sku:
            print(f"[WARN] {game} collision: {key!r} -> already {sub[key]!r}, skipping {sku!r}")
            collisions[game] += 1
        return
    sub[key] = sku
    counts[bucket] += 1


for game_dir in ["pokemon", "mtg", "yugioh"]:
    game_path = os.path.join(CARDSDB, game_dir)
    if not os.path.isdir(game_path):
        print(f"[SKIP] {game_path} not found")
        continue

    total_in_dir = 0
    for sku_dir in os.listdir(game_path):
        profile_path = os.path.join(game_path, sku_dir, "profile.json")
        if not os.path.isfile(profile_path):
            continue
        total_in_dir += 1
        sku = sku_dir

        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                p = json.load(f)
        except Exception as e:
            print(f"[WARN] Could not read {profile_path}: {e}")
            continue

        card_number = str(p.get("card_number") or "").strip()
        set_id = str(p.get("set_id") or "").strip()
        category = str(p.get("category") or "").upper().strip()

        if category == "POKEMON":
            if not card_number or not set_id:
                skipped["pokemon"] += 1
                continue
            if sku_dir.startswith("jpn-"):
                key = f"jpn-{set_id}-{card_number}".lower()
            else:
                key = f"{set_id}-{card_number}".lower()
            add_key("pokemon", key, sku, "pokemon")

        elif category == "MTG":
            if not card_number or not set_id:
                skipped["mtg"] += 1
                continue
            key = f"{set_id}-{card_number}".lower()
            add_key("mtg", key, sku, "mtg")

        elif category == "YUGIOH":
            added_any = False
            if card_number:
                add_key("ygo", card_number.upper(), sku, "ygo_setcode")
                added_any = True
            ygoprodeck_id = str(p.get("ygoprodeck_id") or "").strip()
            if ygoprodeck_id and re.match(r'^\d{5,8}$', ygoprodeck_id):
                add_key("ygo", ygoprodeck_id, sku, "ygo_passcode")
                added_any = True
            if not added_any:
                skipped["ygo"] += 1

    print(f"[{game_dir}] scanned {total_in_dir} card folders")

total_keys = sum(len(v) for v in lookup.values())
total_collisions = sum(collisions.values())

print()
print(f"Total keys written : {total_keys:,}")
print(f"  Pokemon          : {counts['pokemon']:,}  (collisions: {collisions['pokemon']})")
print(f"  MTG              : {counts['mtg']:,}  (collisions: {collisions['mtg']})")
print(f"  YGO set codes    : {counts['ygo_setcode']:,}  (collisions: {collisions['ygo']})")
print(f"  YGO passcodes    : {counts['ygo_passcode']:,}")
print(f"Total collisions   : {total_collisions}")
print(f"Skipped (no key)   : pokemon={skipped['pokemon']} mtg={skipped['mtg']} ygo={skipped['ygo']}")

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(lookup, f, ensure_ascii=False, separators=(",", ":"))

size_kb = round(os.path.getsize(OUTPUT) / 1024, 1)
print(f"\nWritten to : {OUTPUT}")
print(f"File size  : {size_kb} KB  ({os.path.getsize(OUTPUT):,} bytes)")
