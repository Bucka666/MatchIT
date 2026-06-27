"""
build_set_metadata.py — Build set_metadata.json for set-completion tracking.

Scans CardsDB folder structure to discover unique set_ids per game,
then enriches each set with API data (name, total, printed_total).

Output: C:\MatchIT\set_metadata.json
Schema per entry:
  {
    "name": str,
    "game": "POKEMON" | "MTG" | "YUGIOH",
    "printed_total": int | null,   # collector-facing denominator (excludes secret rares)
    "total": int | null,           # full count including secret rares
    "exclude": bool,               # True = skip in set-completion UI
    ... game-specific extras
  }

MTG note: total=null intentionally — MTG completion is tracked by named
card, not collector number, so the backend computes the total dynamically
from CardsDB profiles rather than using Scryfall's inflated card_count.

Promo note: Pokémon promo set_ids are "svp", "swshp", "bwp" etc. — they
do NOT contain the word "promo". The exclude check below catches any that
do; a dedicated exclude list can be added in Phase B if needed.

Usage:
    python build_set_metadata.py
    CARDSDB_ROOT=C:/my/CardsDB python build_set_metadata.py
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Set

# ── Config ─────────────────────────────────────────────────────────────────

_DEFAULT_CARDSDB = Path("C:/CardsDB")
CARDSDB_ROOT = Path(os.environ.get("CARDSDB_ROOT", str(_DEFAULT_CARDSDB)))
OUTPUT_PATH = Path(__file__).parent / "set_metadata.json"

POKEMON_SETS_API = "https://api.pokemontcg.io/v2/sets?select=id,name,total,printedTotal"
MTG_SETS_API    = "https://api.scryfall.com/sets"
YGO_SETS_API    = "https://db.ygoprodeck.com/api/v7/cardsets.php"

# MTG set types that represent non-playable products (no card completion)
MTG_EXCLUDE_TYPES = {"memorabilia", "token", "minigame"}


# ── Step 1: Discover set_ids from CardsDB folder structure ─────────────────

def scan_cardsdb(root: Path) -> Dict[str, Set[str]]:
    """
    Walk CardsDB/pokemon|mtg|yugioh/ directories and extract unique set_ids
    by parsing folder names — much faster than reading 135k profile.json files.

    Folder name conventions:
      pokemon:  {set_id}-{card_number}     e.g. sv1-85, base1-1, swshp-BW001
      mtg:      mtg-{set_code}-{num}       e.g. mtg-hob-110, mtg-10e-1
      yugioh:   ygo-{set_code}-{card_code}-{api_id}  e.g. ygo-LOB-EN005-12345
    """
    sets: Dict[str, Set[str]] = {"POKEMON": set(), "MTG": set(), "YUGIOH": set()}
    game_map = {"pokemon": "POKEMON", "mtg": "MTG", "yugioh": "YUGIOH"}

    for game_dir in root.iterdir():
        if not game_dir.is_dir():
            continue
        game = game_map.get(game_dir.name.lower())
        if not game:
            continue

        for card_dir in game_dir.iterdir():
            if not card_dir.is_dir():
                continue
            n = card_dir.name

            if game == "POKEMON":
                if "-" in n:
                    sets["POKEMON"].add(n.split("-")[0])
            elif game == "MTG":
                parts = n.split("-")
                if len(parts) >= 3 and parts[0] == "mtg":
                    sets["MTG"].add(parts[1])
            elif game == "YUGIOH":
                # ygo-{set_code}-{card_code}-{api_id}
                # set_code is always parts[1] because YGO set codes
                # (LOB, MRD, 25LP, 2017) never contain hyphens
                parts = n.split("-")
                if len(parts) >= 3 and parts[0] == "ygo":
                    sets["YUGIOH"].add(parts[1])

    return sets


# ── API helpers ────────────────────────────────────────────────────────────

def _fetch_json(url: str, label: str):
    """Fetch JSON from a URL. Returns parsed object or None on failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "GrailSweep/1.0 contact@grailsweep.com",
                "Accept":     "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [ERROR] {label}: HTTP {e.code}")
        return None
    except Exception as e:
        print(f"  [ERROR] {label}: {e}")
        return None


# ── Pokémon ────────────────────────────────────────────────────────────────

def build_pokemon_metadata(set_ids: Set[str]) -> Dict[str, dict]:
    print(f"[POKEMON] Fetching set list from pokemontcg.io ({len(set_ids)} sets to match)...")
    data = _fetch_json(POKEMON_SETS_API, "pokemontcg.io/v2/sets")

    api_lookup: Dict[str, dict] = {}
    if data:
        for s in data.get("data", []):
            sid = s.get("id", "")
            if sid:
                api_lookup[sid.lower()] = s

    result: Dict[str, dict] = {}
    missing = 0
    for set_id in sorted(set_ids):
        entry = api_lookup.get(set_id.lower())
        if entry:
            name    = entry.get("name", set_id)
            total   = entry.get("total")
            printed = entry.get("printedTotal", total)
        else:
            print(f"  [WARN] '{set_id}' not found in pokemontcg.io")
            name    = set_id
            total   = None
            printed = None
            missing += 1

        result[set_id] = {
            "name":          name,
            "game":          "POKEMON",
            "printed_total": printed,
            "total":         total,
            "exclude":       "promo" in set_id.lower(),
        }

    print(f"  Matched {len(result) - missing}/{len(result)}, warnings: {missing}")
    return result


# ── MTG ────────────────────────────────────────────────────────────────────

def build_mtg_metadata(set_ids: Set[str]) -> Dict[str, dict]:
    print(f"[MTG] Fetching set list from Scryfall ({len(set_ids)} sets to match)...")
    data = _fetch_json(MTG_SETS_API, "scryfall.com/sets")

    api_lookup: Dict[str, dict] = {}
    if data:
        for s in data.get("data", []):
            code = s.get("code", "")
            if code:
                api_lookup[code.lower()] = s

    result: Dict[str, dict] = {}
    missing = 0
    for set_id in sorted(set_ids):
        entry = api_lookup.get(set_id.lower())
        if entry:
            name        = entry.get("name", set_id)
            set_type    = entry.get("set_type", "")
            scryfall_id = entry.get("id", "")
        else:
            print(f"  [WARN] '{set_id}' not found in Scryfall")
            name        = set_id
            set_type    = ""
            scryfall_id = ""
            missing += 1

        result[set_id] = {
            "name":          name,
            "game":          "MTG",
            "printed_total": None,
            "total":         None,  # computed at runtime from CardsDB unique named cards
            "scryfall_id":   scryfall_id,
            "exclude":       set_type in MTG_EXCLUDE_TYPES,
        }

    print(f"  Matched {len(result) - missing}/{len(result)}, warnings: {missing}")
    return result


# ── YGO ────────────────────────────────────────────────────────────────────

def build_ygo_metadata(set_ids: Set[str]) -> Dict[str, dict]:
    print(f"[YUGIOH] Fetching set list from YGOProDeck ({len(set_ids)} sets to match)...")
    # cardsets.php returns a flat list of all YGO sets
    all_sets = _fetch_json(YGO_SETS_API, "ygoprodeck.com/api/v7/cardsets")

    # YGO set_id in profile.json = set_code from YGOProDeck (e.g. "LOB", "25LP", "2017")
    # Note: the task spec described this as set_name, but the actual profile field is
    # set_code — matching by code is exact and requires no fuzzy name matching.
    api_lookup: Dict[str, dict] = {}
    if isinstance(all_sets, list):
        for s in all_sets:
            code = (s.get("set_code") or "").strip().upper()
            if code:
                api_lookup[code] = s

    result: Dict[str, dict] = {}
    missing = 0
    for set_id in sorted(set_ids):
        entry = api_lookup.get(set_id.upper())
        if entry:
            name = entry.get("set_name", set_id)
            num  = entry.get("num_of_cards")
            try:
                num = int(num) if num is not None else None
            except (ValueError, TypeError):
                num = None
        else:
            print(f"  [WARN] '{set_id}' not found in YGOProDeck")
            name = set_id
            num  = None
            missing += 1

        result[set_id] = {
            "name":          name,
            "game":          "YUGIOH",
            "printed_total": num,
            "total":         num,
            "exclude":       False,
        }

    print(f"  Matched {len(result) - missing}/{len(result)}, warnings: {missing}")
    return result


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()

    if not CARDSDB_ROOT.exists():
        print(f"[ERROR] CardsDB root not found: {CARDSDB_ROOT}")
        raise SystemExit(1)

    print(f"=== build_set_metadata.py ===")
    print(f"CardsDB : {CARDSDB_ROOT}")
    print(f"Output  : {OUTPUT_PATH}\n")

    # ── 1. Discover set_ids ────────────────────────────────────────────────
    print("Scanning CardsDB...")
    sets_by_game = scan_cardsdb(CARDSDB_ROOT)
    for game, ids in sorted(sets_by_game.items()):
        print(f"  {game}: {len(ids)} unique sets")

    # ── 2. Enrich from APIs ───────────────────────────────────────────────
    print()
    pokemon_meta = build_pokemon_metadata(sets_by_game["POKEMON"])
    print()
    mtg_meta     = build_mtg_metadata(sets_by_game["MTG"])
    print()
    ygo_meta     = build_ygo_metadata(sets_by_game["YUGIOH"])

    metadata: Dict[str, dict] = {}
    metadata.update(pokemon_meta)
    # Collision-aware merge: some set_ids (e.g. me1/me2/me3 — Pokémon "Mega
    # Evolution"/"Phantasmal Flames"/"Perfect Order" vs MTG "Masters Edition"
    # I/II/III) exist in both CardsDB/pokemon and CardsDB/mtg. An unconditional
    # update() here let mtg_meta silently clobber the correct Pokémon totals
    # with null MTG ones. Pokémon wins on collision — never overwrite a key
    # pokemon_meta already populated.
    mtg_collisions = sorted(set(mtg_meta) & set(metadata))
    if mtg_collisions:
        print(f"  [COLLISION] {len(mtg_collisions)} set_id(s) in both Pokémon and MTG — "
              f"keeping Pokémon entry: {mtg_collisions}")
    for k, v in mtg_meta.items():
        if k not in metadata:
            metadata[k] = v
    metadata.update(ygo_meta)

    # ── 3. Write output ───────────────────────────────────────────────────
    print(f"\nWriting {len(metadata)} entries to {OUTPUT_PATH}...")
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False, sort_keys=True)

    elapsed = round(time.time() - t0, 1)
    file_kb = OUTPUT_PATH.stat().st_size / 1024

    # ── 4. Summary ────────────────────────────────────────────────────────
    excluded       = sum(1 for v in metadata.values() if v.get("exclude"))
    failed_pkm     = sum(1 for v in pokemon_meta.values() if v.get("total") is None)
    failed_ygo     = sum(1 for v in ygo_meta.values()     if v.get("total") is None)
    failed_total   = failed_pkm + failed_ygo  # MTG null total is expected

    pkm_excl       = sum(1 for v in pokemon_meta.values() if v.get("exclude"))
    mtg_excl       = sum(1 for v in mtg_meta.values()     if v.get("exclude"))

    print(f"\n=== Summary ===")
    print(f"Total sets in metadata : {len(metadata)}")
    print(f"  Pokémon : {len(pokemon_meta):>4}  (excluded as promos : {pkm_excl})")
    print(f"  MTG     : {len(mtg_meta):>4}  (excluded non-playable: {mtg_excl})")
    print(f"  YGO     : {len(ygo_meta):>4}  (excluded: 0)")
    print(f"Total excluded         : {excluded}")
    print(f"API failures (null total, excl. MTG): {failed_total}")
    print(f"  Pokémon failures : {failed_pkm}")
    print(f"  YGO failures     : {failed_ygo}")
    print(f"File size : {file_kb:.1f} KB")
    print(f"Elapsed   : {elapsed}s")

    # ── 5. Sample entries ─────────────────────────────────────────────────
    print("\n=== Sample entries (first non-excluded per game) ===")
    for game_label, meta_dict in [
        ("POKEMON", pokemon_meta),
        ("MTG",     mtg_meta),
        ("YUGIOH",  ygo_meta),
    ]:
        candidates = sorted(
            [(k, v) for k, v in meta_dict.items() if not v.get("exclude")]
        )
        if candidates:
            k, v = candidates[0]
            print(f"\n  {game_label}: {k!r}")
            for fk, fv in v.items():
                print(f"    {fk}: {fv!r}")
        else:
            print(f"\n  {game_label}: (all excluded)")

    print()


if __name__ == "__main__":
    main()
