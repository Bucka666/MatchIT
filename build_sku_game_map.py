"""Build sku_game_map.json — maps ambiguous SKUs (no mtg-/ygo-/op- prefix) to their game.

Usage:
    python build_sku_game_map.py
    python build_sku_game_map.py --game onepiece      # only rescan onepiece's CardsDB folder
    python build_sku_game_map.py --game pokemon,mtg   # comma-separated, or repeat --game

SCOPING (2026-09-07): --game restricts which games' CardsDB folders get
scanned, and the write is now merge-safe (loads the existing sku_game_map.
json first, only replaces entries for scanned games, leaving every other
game's entries byte-identical) -- matches the same fix applied to
build_set_metadata.py / build_identifier_lookup.py tonight, for interface
consistency and to protect other games' entries from a scoped run.

NOTE for One Piece specifically: the per-sku loop below has always
skipped any sku starting with "op-" (see the skip-prefix check), because
One Piece SKUs are globally unambiguous by construction (see
build_one_piece_data.py's docstring) and this file only exists to
disambiguate UNPREFIXED skus (Pokemon's). That means --game onepiece will
always scan CardsDB/onepiece and find exactly 0 entries to add here, by
design -- this is not a bug in the scoping, this file was never something
a One Piece set addition needed to touch. Included for interface
consistency with the rest of the pipeline, not because it does anything
for onepiece.
"""

import argparse
import os, json

CARDSDB = "C:/CardsDB"
GAMES = ("pokemon", "mtg", "yugioh", "onepiece")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sku_game_map.json")

CATEGORY_MAP = {"POKEMON": "POKEMON", "MTG": "MTG", "YUGIOH": "YUGIOH", "ONEPIECE": "ONEPIECE"}


def _parse_game_arg(raw_values) -> set:
    """--game accepts both repeatable flags and comma-separated values,
    matching build_one_piece_data.py's --set-id convention."""
    only = set()
    for raw in raw_values:
        for piece in raw.split(","):
            piece = piece.strip().lower()
            if piece:
                only.add(piece)
    return only


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", action="append", default=None,
                         help="only rescan this game's CardsDB folder (pokemon, mtg, yugioh, "
                              "onepiece). Repeatable or comma-separated. Omit for all games (default). "
                              "NOTE: onepiece SKUs are always op-prefixed and always skipped by "
                              "this script's own filter, so --game onepiece always finds 0 entries.")
    args = parser.parse_args()
    only_games = _parse_game_arg(args.game) if args.game else None

    if only_games is not None:
        print(f"Scoped to game(s): {sorted(only_games)}")

    result = {}
    counts = {"POKEMON": 0, "MTG": 0, "YUGIOH": 0, "ONEPIECE": 0, "unknown": 0}

    for game_folder in GAMES:
        if only_games is not None and game_folder not in only_games:
            continue
        game_dir = os.path.join(CARDSDB, game_folder)
        if not os.path.isdir(game_dir):
            print(f"[WARN] {game_dir} not found, skipping")
            continue

        for sku in os.listdir(game_dir):
            if sku.startswith("mtg-") or sku.startswith("ygo-") or sku.startswith("op-") or sku.startswith("_"):
                continue
            profile_path = os.path.join(game_dir, sku, "profile.json")
            if not os.path.isfile(profile_path):
                continue
            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    profile = json.load(f)
                category = (profile.get("category") or "").upper()
                game = CATEGORY_MAP.get(category)
                if game:
                    result[sku] = game
                    counts[game] += 1
                else:
                    counts["unknown"] += 1
            except Exception as e:
                print(f"[WARN] {profile_path}: {e}")

    # Merge-safe write: an unscoped run (only_games is None) reproduces the
    # original unconditional full-rebuild exactly. A scoped run keeps every
    # existing entry whose game is OUT of scope, and replaces only the
    # scanned games' entries with this run's fresh result.
    if only_games is None:
        final = result
    else:
        existing = {}
        if os.path.isfile(OUT):
            try:
                with open(OUT, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception as e:
                print(f"[WARN] could not read existing {OUT}, starting fresh: {e}")
        final = {sku: game for sku, game in existing.items() if game.lower() not in only_games}
        final.update(result)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(final, f, separators=(",", ":"))

    size_kb = round(os.path.getsize(OUT) / 1024, 1)
    print(f"SKUs per game (this run): POKEMON={counts['POKEMON']}, MTG={counts['MTG']}, YUGIOH={counts['YUGIOH']}, ONEPIECE={counts['ONEPIECE']}, unknown={counts['unknown']}")
    print(f"Total: {len(final)} SKUs — {OUT} ({size_kb} KB)")


if __name__ == "__main__":
    main()
