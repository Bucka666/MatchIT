"""
scrape_pokemon_tcg.py — Pull Pokémon card data from pokemontcg.io
=================================================================
Builds the CardsDB/pokemon/ folder structure compatible with MatchIT.

Each card gets:
    CardsDB/pokemon/{card_id}/
        front.png          ← high-res card image
        profile.json       ← metadata for filtering & cross-referencing

Usage:
    python scrape_pokemon_tcg.py                      # fetch all sets
    python scrape_pokemon_tcg.py --set base1           # single set
    python scrape_pokemon_tcg.py --set base1,base2     # multiple sets
    python scrape_pokemon_tcg.py --resume               # skip existing
    python scrape_pokemon_tcg.py --api-key YOUR_KEY     # higher rate limit

Requires: requests, Pillow (pip install requests Pillow)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

try:
    from PIL import Image
    from io import BytesIO
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("[WARN] Pillow not installed — skipping image validation. pip install Pillow")


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

API_BASE = "https://api.pokemontcg.io/v2"
DEFAULT_DB_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "CardsDB")
RATE_LIMIT_DELAY = 0.35  # ~3 req/sec for free tier
RATE_LIMIT_DELAY_WITH_KEY = 0.05  # ~20 req/sec with API key
IMAGE_DOWNLOAD_TIMEOUT = 30
PAGE_SIZE = 250  # API max


# ─────────────────────────────────────────────
# Rarity normalization
# ─────────────────────────────────────────────

RARITY_MAP = {
    "common":           "COMMON",
    "uncommon":         "UNCOMMON",
    "rare":             "RARE",
    "rare holo":        "RARE_HOLO",
    "rare holo ex":     "RARE_ULTRA",
    "rare holo gx":     "RARE_ULTRA",
    "rare holo v":      "RARE_ULTRA",
    "rare holo vmax":   "RARE_ULTRA",
    "rare holo vstar":  "RARE_ULTRA",
    "rare ultra":       "RARE_ULTRA",
    "rare secret":      "RARE_SECRET",
    "rare rainbow":     "RARE_RAINBOW",
    "rare full art":    "RARE_FULL_ART",
    "rare shiny":       "RARE_ULTRA",
    "rare shining":     "RARE_ULTRA",
    "rare ace":         "RARE_ULTRA",
    "amazing rare":     "RARE_ULTRA",
    "illustration rare":        "RARE_FULL_ART",
    "special illustration rare": "RARE_FULL_ART",
    "hyper rare":       "RARE_RAINBOW",
    "double rare":      "RARE_HOLO",
    "ultra rare":       "RARE_ULTRA",
    "shiny rare":       "RARE_ULTRA",
    "shiny ultra rare": "RARE_SECRET",
    "promo":            "PROMO",
    # Fallback handled in normalize_rarity()
}


def normalize_rarity(raw: Optional[str]) -> str:
    """Map API rarity strings to our vertical's rarity enum."""
    if not raw:
        return ""
    return RARITY_MAP.get(raw.lower().strip(), "RARE")


# ─────────────────────────────────────────────
# Era detection from set release date
# ─────────────────────────────────────────────

# Maps set series names → our era codes
SERIES_ERA_MAP = {
    "base":               "WOTC",
    "gym":                "WOTC",
    "neo":                "WOTC",
    "legendary":          "WOTC",
    "e-card":             "WOTC",
    "ex":                 "EX_ERA",
    "pop":                "EX_ERA",
    "diamond & pearl":    "DP_ERA",
    "platinum":           "DP_ERA",
    "heartgold & soulsilver": "DP_ERA",
    "black & white":      "BW_ERA",
    "xy":                 "XY_ERA",
    "sun & moon":         "SM_ERA",
    "sword & shield":     "SWSH_ERA",
    "scarlet & violet":   "SV_ERA",
}


def detect_era(set_data: dict) -> str:
    """Determine the era code from set metadata."""
    series = (set_data.get("series") or "").lower().strip()

    for key, era in SERIES_ERA_MAP.items():
        if key in series:
            return era

    # Fallback: use release date year
    release = set_data.get("releaseDate", "")
    if release:
        try:
            year = int(release[:4])
            if year <= 2002:
                return "WOTC"
            elif year <= 2006:
                return "EX_ERA"
            elif year <= 2010:
                return "DP_ERA"
            elif year <= 2013:
                return "BW_ERA"
            elif year <= 2016:
                return "XY_ERA"
            elif year <= 2019:
                return "SM_ERA"
            elif year <= 2022:
                return "SWSH_ERA"
            else:
                return "SV_ERA"
        except (ValueError, IndexError):
            pass

    return ""


# ─────────────────────────────────────────────
# Energy type from card types list
# ─────────────────────────────────────────────

def extract_energy_type(card: dict) -> str:
    """Get the primary energy type from a card."""
    types = card.get("types") or []
    if not types:
        return ""

    type_map = {
        "fire": "FIRE", "water": "WATER", "grass": "GRASS",
        "lightning": "LIGHTNING", "psychic": "PSYCHIC",
        "fighting": "FIGHTING", "darkness": "DARKNESS",
        "metal": "METAL", "fairy": "FAIRY", "dragon": "DRAGON",
        "colorless": "COLORLESS",
    }
    primary = types[0].lower().strip()
    return type_map.get(primary, primary.upper())


def extract_hp_range(card: dict) -> str:
    """Bucket the HP into a range."""
    hp_str = card.get("hp", "")
    if not hp_str:
        return ""
    try:
        hp = int(hp_str)
        if hp <= 70:
            return "HP_LOW"
        elif hp <= 120:
            return "HP_MID"
        elif hp <= 200:
            return "HP_HIGH"
        else:
            return "HP_VMAX"
    except (ValueError, TypeError):
        return ""


def extract_supertype(card: dict) -> str:
    """Map supertype to our enum."""
    st = (card.get("supertype") or "").lower().strip()
    return {"pokémon": "POKEMON", "pokemon": "POKEMON",
            "trainer": "TRAINER", "energy": "ENERGY"}.get(st, st.upper())


# ─────────────────────────────────────────────
# Profile builder
# ─────────────────────────────────────────────

def build_profile(card: dict, set_data: dict) -> dict:
    """
    Build a MatchIT profile dict from a pokemontcg.io card object.
    This gets saved as profile.json alongside the card image.
    """
    tcgp = card.get("tcgplayer", {})
    cm = card.get("cardmarket", {})

    return {
        # Identity
        "api_id":           card.get("id", ""),
        "name":             card.get("name", ""),
        "card_number":      card.get("number", ""),
        "set_id":           card.get("set", {}).get("id", ""),
        "set_name":         card.get("set", {}).get("name", ""),
        "category":         "POKEMON",

        # Filterable fields (match vertical.json profile_fields)
        "rarity":           normalize_rarity(card.get("rarity")),
        "energy_type":      extract_energy_type(card),
        "card_supertype":   extract_supertype(card),
        "hp_range":         extract_hp_range(card),
        "set_era":          detect_era(set_data),
        "language":         "EN",
        "edition":          "",

        # Cross-reference IDs
        "tcgplayer_id":     str(tcgp.get("productId", "")) if tcgp else "",
        "tcgplayer_url":    tcgp.get("url", "") if tcgp else "",
        "cardmarket_id":    str(cm.get("productId", "")) if cm else "",
        "cardmarket_url":   cm.get("url", "") if cm else "",

        # Price snapshot (updated at scrape time)
        "prices": {
            "tcgplayer": _extract_tcg_prices(tcgp),
            "cardmarket": _extract_cm_prices(cm),
        },

        # Extra metadata (not used for filtering, but useful)
        "artist":           card.get("artist", ""),
        "subtypes":         card.get("subtypes", []),
        "hp":               card.get("hp", ""),
        "evolves_from":     card.get("evolvesFrom", ""),
        "image_url_small":  card.get("images", {}).get("small", ""),
        "image_url_large":  card.get("images", {}).get("large", ""),
    }


def _extract_tcg_prices(tcgp: dict) -> dict:
    """Flatten TCGPlayer price data."""
    if not tcgp or "prices" not in tcgp:
        return {}
    prices = {}
    for variant, data in tcgp.get("prices", {}).items():
        prices[variant] = {
            "low": data.get("low"),
            "mid": data.get("mid"),
            "high": data.get("high"),
            "market": data.get("market"),
        }
    return prices


def _extract_cm_prices(cm: dict) -> dict:
    """Flatten Cardmarket price data."""
    if not cm or "prices" not in cm:
        return {}
    p = cm.get("prices", {})
    return {
        "avg_sell": p.get("averageSellPrice"),
        "low": p.get("lowPrice"),
        "trend": p.get("trendPrice"),
        "avg_1d": p.get("avg1"),
        "avg_7d": p.get("avg7"),
        "avg_30d": p.get("avg30"),
    }


# ─────────────────────────────────────────────
# API client
# ─────────────────────────────────────────────

class PokemonTCGClient:
    """Thin wrapper around pokemontcg.io v2 API."""

    def __init__(self, api_key: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if api_key:
            self.session.headers["X-Api-Key"] = api_key
        self.delay = RATE_LIMIT_DELAY_WITH_KEY if api_key else RATE_LIMIT_DELAY

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make a rate-limited GET request."""
        url = f"{API_BASE}/{endpoint}"
        time.sleep(self.delay)
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_sets(self) -> List[dict]:
        """Fetch all sets."""
        data = self._get("sets", {"pageSize": 500, "orderBy": "releaseDate"})
        return data.get("data", [])

    def get_cards_for_set(self, set_id: str) -> List[dict]:
        """Fetch all cards in a set (handles pagination)."""
        cards = []
        page = 1
        while True:
            data = self._get("cards", {
                "q": f"set.id:{set_id}",
                "pageSize": PAGE_SIZE,
                "page": page,
            })
            batch = data.get("data", [])
            cards.extend(batch)
            total = data.get("totalCount", 0)
            if len(cards) >= total or not batch:
                break
            page += 1
        return cards

    def download_image(self, url: str, dest_path: str) -> bool:
        """Download a card image. Returns True on success."""
        try:
            time.sleep(self.delay)
            resp = self.session.get(url, timeout=IMAGE_DOWNLOAD_TIMEOUT)
            resp.raise_for_status()

            # Validate image if Pillow available
            if HAS_PIL:
                img = Image.open(BytesIO(resp.content))
                img.verify()

            Path(dest_path).write_bytes(resp.content)
            return True
        except Exception as e:
            print(f"  [ERROR] Image download failed: {e}")
            return False


# ─────────────────────────────────────────────
# Main scraper logic
# ─────────────────────────────────────────────

def scrape_set(client: PokemonTCGClient, set_data: dict, db_root: str,
               resume: bool = False) -> dict:
    """
    Scrape all cards from one set.

    Returns stats dict: {total, downloaded, skipped, failed}
    """
    set_id = set_data["id"]
    set_name = set_data.get("name", set_id)
    stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    print(f"\n{'='*60}")
    print(f"  Set: {set_name} ({set_id})")
    print(f"  Released: {set_data.get('releaseDate', '?')}")
    print(f"  Total cards: {set_data.get('total', '?')}")
    print(f"{'='*60}")

    try:
        cards = client.get_cards_for_set(set_id)
    except Exception as e:
        print(f"  [WARN] Failed to fetch set {set_id}: {e}")
        cards = []

    for i, card in enumerate(cards, 1):
        card_id = card.get("id", "unknown")
        card_name = card.get("name", "?")
        card_id = card_id.replace("?", "Q").replace("*", "S").replace("/", "-").replace("\\", "-").replace(":", "-").replace('"', '').replace("<", "").replace(">", "").replace("|", "")
        card_dir = os.path.join(db_root, "pokemon", card_id)
        # Resume: skip if folder + image already exist
        front_path = os.path.join(card_dir, "front.png")
        if resume and os.path.exists(front_path):
            stats["skipped"] += 1
            continue

        print(f"  [{i}/{len(cards)}] {card_id}: {card_name}", end="", flush=True)

        # Create directory
        os.makedirs(card_dir, exist_ok=True)

        # Save profile
        profile = build_profile(card, set_data)
        profile_path = os.path.join(card_dir, "profile.json")
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)

        # Download image (prefer large, fallback to small)
        img_url = card.get("images", {}).get("large") or card.get("images", {}).get("small")
        if img_url:
            if client.download_image(img_url, front_path):
                stats["downloaded"] += 1
                print(" ok")
            else:
                stats["failed"] += 1
                print(" ✗")
        else:
            print(" [no image URL]")
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Pokémon TCG cards from pokemontcg.io into MatchIT DB"
    )
    parser.add_argument("--db-root", default=DEFAULT_DB_ROOT,
                        help="Database root path (default: ../../CardsDB)")
    parser.add_argument("--set", default="",
                        help="Comma-separated set IDs to fetch (default: all)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip cards that already have images")
    parser.add_argument("--api-key", default=os.environ.get("POKEMONTCG_API_KEY", ""),
                        help="pokemontcg.io API key (or set POKEMONTCG_API_KEY env var)")
    parser.add_argument("--list-sets", action="store_true",
                        help="List all available sets and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without downloading")
    args = parser.parse_args()

    client = PokemonTCGClient(api_key=args.api_key or None)

    # List sets mode
    if args.list_sets:
        sets = client.get_sets()
        print(f"\n{'ID':<20} {'Name':<40} {'Released':<12} {'Cards'}")
        print("-" * 85)
        for s in sets:
            print(f"{s['id']:<20} {s.get('name','?'):<40} "
                  f"{s.get('releaseDate','?'):<12} {s.get('total','?')}")
        print(f"\nTotal: {len(sets)} sets")
        return

    # Resolve target sets
    all_sets = client.get_sets()
    set_lookup = {s["id"]: s for s in all_sets}

    if args.set:
        target_ids = [s.strip() for s in args.set.split(",")]
        target_sets = []
        for sid in target_ids:
            if sid in set_lookup:
                target_sets.append(set_lookup[sid])
            else:
                print(f"[WARN] Set '{sid}' not found in API — skipping")
    else:
        target_sets = all_sets

    print(f"\n[SCRAPER] Target: {len(target_sets)} sets")
    print(f"[SCRAPER] DB root: {os.path.abspath(args.db_root)}")
    print(f"[SCRAPER] Resume mode: {args.resume}")
    print(f"[SCRAPER] API key: {'yes' if args.api_key else 'no (free tier, ~3 req/s)'}")

    if args.dry_run:
        total_cards = sum(s.get("total", 0) for s in target_sets)
        print(f"\n[DRY RUN] Would fetch ~{total_cards} cards from {len(target_sets)} sets")
        for s in target_sets:
            print(f"  {s['id']:<20} {s.get('name','?'):<40} ~{s.get('total','?')} cards")
        return

    # Create DB root
    os.makedirs(os.path.join(args.db_root, "pokemon"), exist_ok=True)

    # Scrape
    grand_total = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    for i, set_data in enumerate(target_sets, 1):
        print(f"\n[{i}/{len(target_sets)}] Processing set: {set_data.get('name', set_data['id'])}")
        stats = scrape_set(client, set_data, args.db_root, resume=args.resume)
        for k in grand_total:
            grand_total[k] += stats[k]

    # Summary
    print(f"\n{'='*60}")
    print(f"  SCRAPE COMPLETE")
    print(f"  Total cards:    {grand_total['total']}")
    print(f"  Downloaded:     {grand_total['downloaded']}")
    print(f"  Skipped (exist):{grand_total['skipped']}")
    print(f"  Failed:         {grand_total['failed']}")
    print(f"{'='*60}")

    # Save a manifest for the embedding pipeline
    manifest_path = os.path.join(args.db_root, "pokemon", "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "scrape_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sets_scraped": [s["id"] for s in target_sets],
            "stats": grand_total,
        }, f, indent=2)
    print(f"\nManifest saved: {manifest_path}")


if __name__ == "__main__":
    main()