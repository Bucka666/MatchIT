"""Build sku_game_map.json — maps ambiguous SKUs (no mtg-/ygo- prefix) to their game."""

import os, json

CARDSDB = "C:/CardsDB"
GAMES = ("pokemon", "mtg", "yugioh")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sku_game_map.json")

CATEGORY_MAP = {"POKEMON": "POKEMON", "MTG": "MTG", "YUGIOH": "YUGIOH"}


def main():
    result = {}
    counts = {"POKEMON": 0, "MTG": 0, "YUGIOH": 0, "unknown": 0}

    for game_folder in GAMES:
        game_dir = os.path.join(CARDSDB, game_folder)
        if not os.path.isdir(game_dir):
            print(f"[WARN] {game_dir} not found, skipping")
            continue

        for sku in os.listdir(game_dir):
            if sku.startswith("mtg-") or sku.startswith("ygo-") or sku.startswith("_"):
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

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"))

    size_kb = round(os.path.getsize(OUT) / 1024, 1)
    print(f"SKUs per game: POKEMON={counts['POKEMON']}, MTG={counts['MTG']}, YUGIOH={counts['YUGIOH']}, unknown={counts['unknown']}")
    print(f"Total: {len(result)} SKUs — {OUT} ({size_kb} KB)")


if __name__ == "__main__":
    main()
