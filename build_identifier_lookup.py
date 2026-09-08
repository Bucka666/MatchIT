"""
build_identifier_lookup.py — build identifier->SKU lookup for OCR-first matching.

Scans all CardsDB profile.json files and builds a nested dict keyed by game:
  {
    "pokemon":  { "sv6-25": "sv6-25", ... },
    "mtg":      { "me1-1": "mtg-me1-1", "mom-263": "mtg-mom-263", ... },
    "ygo":      { "SDBT-EN011": "ygo-SDBT-EN011-...", "89631139": "...", ... }
    "onepiece": { "op07-091_p1": "op-op07-091_p1", ... }
  }

Output: C:\\MatchIT\\identifier_lookup.json

Usage:
    python build_identifier_lookup.py
    python build_identifier_lookup.py --game onepiece      # only rescan onepiece's CardsDB folder
    python build_identifier_lookup.py --game pokemon,mtg   # comma-separated, or repeat --game

SCOPING (2026-09-07): --game restricts which games' CardsDB folders get
scanned. Previously this was bare module-level code with no CLI at all --
always scanned all 4 games and wrote a wholesale-overwrite dict built only
from that run's own results, so a scoped run without a merge fix would
have silently deleted every other game's keys. The fix loads the existing
identifier_lookup.json first and only replaces the scoped game(s)'
sub-dict (pokemon/mtg/ygo/onepiece), leaving every other game's keys
byte-identical. Omitting --game reproduces the exact prior behavior (every
game rescanned, same collision handling). No external network calls
anywhere in this file, scoped or not -- purely local CardsDB profile.json
reads.
"""
import argparse
import json
import os
import re

CARDSDB = r"C:\CardsDB"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identifier_lookup.json")

# CardsDB folder name -> top-level key used in the output dict. "yugioh" is
# the only mismatch (folder vs. output key), preserved exactly as the
# original script wrote it.
OUTPUT_KEY_BY_FOLDER = {"pokemon": "pokemon", "mtg": "mtg", "yugioh": "ygo", "onepiece": "onepiece"}


def _parse_game_arg(raw_values) -> set:
    """--game accepts both repeatable flags and comma-separated values,
    matching build_one_piece_data.py's --set-id convention. Values are
    CardsDB folder names (pokemon, mtg, yugioh, onepiece), not output keys."""
    only = set()
    for raw in raw_values:
        for piece in raw.split(","):
            piece = piece.strip().lower()
            if piece:
                only.add(piece)
    return only


def main():
    parser = argparse.ArgumentParser(
        description="Build identifier->SKU lookup for OCR-first matching.")
    parser.add_argument("--game", action="append", default=None,
                         help="only rescan this game's CardsDB folder (pokemon, mtg, yugioh, "
                              "onepiece). Repeatable or comma-separated. Omit for all games (default).")
    args = parser.parse_args()
    only_games = _parse_game_arg(args.game) if args.game else None

    def in_scope(folder_name: str) -> bool:
        return only_games is None or folder_name in only_games

    if only_games is not None:
        print(f"Scoped to game(s): {sorted(only_games)}")

    lookup = {"pokemon": {}, "mtg": {}, "ygo": {}, "onepiece": {}}
    collisions = {"pokemon": 0, "mtg": 0, "ygo": 0, "onepiece": 0}
    counts = {"pokemon": 0, "mtg": 0, "ygo_setcode": 0, "ygo_passcode": 0, "onepiece": 0}
    skipped = {"pokemon": 0, "mtg": 0, "ygo": 0, "onepiece": 0}

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

    for game_dir in ["pokemon", "mtg", "yugioh", "onepiece"]:
        if not in_scope(game_dir):
            continue
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

            elif category == "ONEPIECE":
                if not card_number or not set_id:
                    skipped["onepiece"] += 1
                    continue
                key = f"{set_id}-{card_number}".lower()
                add_key("onepiece", key, sku, "onepiece")

        print(f"[{game_dir}] scanned {total_in_dir} card folders")

    total_keys = sum(len(v) for v in lookup.values())
    total_collisions = sum(collisions.values())

    print()
    print(f"Total keys written this run : {total_keys:,}")
    print(f"  Pokemon          : {counts['pokemon']:,}  (collisions: {collisions['pokemon']})")
    print(f"  MTG              : {counts['mtg']:,}  (collisions: {collisions['mtg']})")
    print(f"  YGO set codes    : {counts['ygo_setcode']:,}  (collisions: {collisions['ygo']})")
    print(f"  YGO passcodes    : {counts['ygo_passcode']:,}")
    print(f"  One Piece        : {counts['onepiece']:,}  (collisions: {collisions['onepiece']})")
    print(f"Total collisions   : {total_collisions}")
    print(f"Skipped (no key)   : pokemon={skipped['pokemon']} mtg={skipped['mtg']} ygo={skipped['ygo']} onepiece={skipped['onepiece']}")

    # ── Merge-safe write: load whatever's already on disk, only replace
    #    the scoped game(s)' sub-dict(s). Omitting --game touches every
    #    game, reproducing the original unconditional full-rebuild exactly.
    existing = {"pokemon": {}, "mtg": {}, "ygo": {}, "onepiece": {}}
    if os.path.isfile(OUTPUT):
        try:
            with open(OUTPUT, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing.update({k: v for k, v in loaded.items() if k in existing})
        except Exception as e:
            print(f"[WARN] could not read existing {OUTPUT}, starting fresh: {e}")

    final = dict(existing)
    for game_dir, out_key in OUTPUT_KEY_BY_FOLDER.items():
        if in_scope(game_dir):
            final[out_key] = lookup[out_key]
        # else: leave final[out_key] exactly as loaded from disk, untouched

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, separators=(",", ":"))

    final_total_keys = sum(len(v) for v in final.values())
    size_kb = round(os.path.getsize(OUTPUT) / 1024, 1)
    print(f"\nTotal keys in output file : {final_total_keys:,}")
    print(f"Written to : {OUTPUT}")
    print(f"File size  : {size_kb} KB  ({os.path.getsize(OUTPUT):,} bytes)")


if __name__ == "__main__":
    main()
