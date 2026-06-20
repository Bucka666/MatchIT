"""
scrape_yugioh.py — Pull Yu-Gi-Oh! cards from YGOProDeck API
=============================================================
Builds the CardsDB/yugioh/ folder structure compatible with MatchIT.

Each card gets:
    CardsDB/yugioh/ygo-{id}/
        front.png          ← card image
        profile.json       ← metadata for filtering & cross-referencing

Usage:
    python scrape_yugioh.py                                    # fetch default sets
    python scrape_yugioh.py --set "Metal Raiders"              # single set by name
    python scrape_yugioh.py --set "MRD,LOB,SDY"               # by short codes
    python scrape_yugioh.py --set "Metal Raiders,Legend of Blue Eyes White Dragon"
    python scrape_yugioh.py --all-sets                         # scrape everything
    python scrape_yugioh.py --all-sets --year 2024             # all 2024 sets
    python scrape_yugioh.py --all-sets --after 2020-01-01      # 2020 onwards
    python scrape_yugioh.py --all-sets --before 2015-01-01     # pre-2015
    python scrape_yugioh.py --all-sets --after 2022-01-01 --before 2024-01-01
    python scrape_yugioh.py --resume                           # skip existing
    python scrape_yugioh.py --list-sets                        # show all sets
    python scrape_yugioh.py --list-sets --year 2024            # show 2024 sets only

API: https://ygoprodeck.com/api-guide/
Rate limit: 20 requests/sec (we use 100ms delay to be safe)
Images: Must be downloaded and hosted locally (no hotlinking)

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
    print("[WARN] Pillow not installed — skipping image validation")


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

API_BASE = "https://db.ygoprodeck.com/api/v7"
DEFAULT_DB_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "CardsDB")
RATE_LIMIT_DELAY = 0.1  # 100ms between requests
IMAGE_DOWNLOAD_TIMEOUT = 30
USER_AGENT = "MatchIT-YGO-Scraper/1.0"

# Popular starter sets
DEFAULT_SETS = [
    "Legend of Blue Eyes White Dragon",
    "Metal Raiders",
    "Starter Deck: Yugi",
]


# ─────────────────────────────────────────────
# Rarity normalization
# ─────────────────────────────────────────────

RARITY_MAP = {
    "common":               "COMMON",
    "rare":                 "RARE",
    "super rare":           "SUPER_RARE",
    "ultra rare":           "ULTRA_RARE_YGO",
    "secret rare":          "RARE_SECRET",
    "ultimate rare":        "RARE_SECRET",
    "ghost rare":           "RARE_SECRET",
    "starlight rare":       "RARE_SECRET",
    "collector's rare":     "RARE_SECRET",
    "prismatic secret rare":"RARE_SECRET",
    "short print":          "COMMON",
}


def normalize_rarity(raw: Optional[str]) -> str:
    if not raw:
        return ""
    return RARITY_MAP.get(raw.lower().strip(), "RARE")


# ─────────────────────────────────────────────
# Card type mapping
# ─────────────────────────────────────────────

def extract_card_type(card: dict) -> str:
    """Map YGO card type to our enum."""
    ctype = (card.get("type") or "").lower()
    if "normal monster" in ctype:
        return "MONSTER_NORMAL"
    elif "effect monster" in ctype:
        return "MONSTER_EFFECT"
    elif "fusion" in ctype:
        return "MONSTER_FUSION"
    elif "synchro" in ctype:
        return "MONSTER_SYNCHRO"
    elif "xyz" in ctype:
        return "MONSTER_XYZ"
    elif "link" in ctype:
        return "MONSTER_LINK"
    elif "spell" in ctype:
        return "SPELL"
    elif "trap" in ctype:
        return "TRAP"
    return ""


def extract_attribute(card: dict) -> str:
    """Get the monster attribute."""
    attr = (card.get("attribute") or "").upper()
    valid = {"DARK", "LIGHT", "EARTH", "WATER", "FIRE", "WIND", "DIVINE"}
    return attr if attr in valid else ""


def detect_era(set_data: dict) -> str:
    """Determine era from set release date."""
    date_str = set_data.get("tcg_date") or ""
    if not date_str:
        return ""
    try:
        year = int(date_str[:4])
        if year < 2012:
            return "CLASSIC_YGO"
        else:
            return "MODERN_YGO"
    except (ValueError, IndexError):
        return ""


# ─────────────────────────────────────────────
# Date filtering helpers
# ─────────────────────────────────────────────

def filter_sets_by_date(sets: List[dict], year: int = 0,
                        after: str = "", before: str = "") -> List[dict]:
    """Filter sets by release date criteria."""
    filtered = []
    for s in sets:
        date_str = s.get("tcg_date") or ""
        if not date_str:
            continue

        # Year filter
        if year:
            try:
                set_year = int(date_str[:4])
                if set_year != year:
                    continue
            except (ValueError, IndexError):
                continue

        # After filter
        if after:
            if date_str < after:
                continue

        # Before filter
        if before:
            if date_str >= before:
                continue

        filtered.append(s)

    return filtered


# ─────────────────────────────────────────────
# Profile builder
# ─────────────────────────────────────────────

def build_profile(card: dict, set_info: dict, set_code: str, set_rarity: str) -> dict:
    """Build a MatchIT profile from a YGOProDeck card object."""
    prices = card.get("card_prices", [{}])[0] if card.get("card_prices") else {}

    return {
        # Identity
        "api_id":           str(card.get("id", "")),
        "name":             card.get("name", ""),
        "card_number":      set_code,
        "set_id":           set_info.get("set_code", ""),
        "set_name":         set_info.get("set_name", ""),
        "category":         "YUGIOH",

        # Filterable fields
        "rarity":           normalize_rarity(set_rarity),
        "ygo_card_type":    extract_card_type(card),
        "ygo_attribute":    extract_attribute(card),
        "set_era":          detect_era(set_info),
        "language":         "EN",
        "edition":          "",

        # Cross-reference IDs
        "ygoprodeck_id":    str(card.get("id", "")),
        "tcgplayer_url":    f"https://www.tcgplayer.com/search/yugioh/product?q={card.get('name', '')}",
        "cardmarket_url":   f"https://www.cardmarket.com/en/YuGiOh/Products/Search?searchString={card.get('name', '')}",

        # Price snapshot
        "prices": {
            "tcgplayer": {
                "normal": {
                    "market": _safe_float(prices.get("tcgplayer_price")),
                },
            },
            "cardmarket": {
                "normal": {
                    "trend": _safe_float(prices.get("cardmarket_price")),
                },
            },
            "ebay": {
                "normal": {
                    "market": _safe_float(prices.get("ebay_price")),
                },
            },
            "amazon": {
                "normal": {
                    "market": _safe_float(prices.get("amazon_price")),
                },
            },
        },

        # Extra metadata
        "type_line":        card.get("type", ""),
        "race":             card.get("race", ""),
        "atk":              card.get("atk"),
        "def":              card.get("def"),
        "level":            card.get("level"),
        "desc":             card.get("desc", ""),
        "archetype":        card.get("archetype", ""),
    }


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────
# API client
# ─────────────────────────────────────────────

class YGOProDeckClient:
    """Thin wrapper around YGOProDeck API v7."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _get(self, endpoint: str, params: dict = None) -> dict:
        time.sleep(RATE_LIMIT_DELAY)
        url = f"{API_BASE}/{endpoint}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_sets(self) -> List[dict]:
        """Fetch all card sets."""
        data = self._get("cardsets.php")
        return data if isinstance(data, list) else []

    def get_cards_for_set(self, set_name: str) -> List[dict]:
        """Fetch all cards in a set."""
        try:
            data = self._get("cardinfo.php", {"cardset": set_name})
            return data.get("data", [])
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                print(f"  [WARN] No cards found for set: {set_name}")
                return []
            raise

    def download_image(self, url: str, dest_path: str) -> bool:
        """Download a card image."""
        try:
            time.sleep(RATE_LIMIT_DELAY)
            resp = self.session.get(url, timeout=IMAGE_DOWNLOAD_TIMEOUT)
            resp.raise_for_status()

            if HAS_PIL:
                img = Image.open(BytesIO(resp.content))
                img.verify()

            Path(dest_path).write_bytes(resp.content)
            return True
        except Exception as e:
            print(f"  [ERROR] Image download failed: {e}")
            return False


# ─────────────────────────────────────────────
# Image URL & card ID
# ─────────────────────────────────────────────

def get_image_url(card: dict) -> Optional[str]:
    """Get the best image URL."""
    images = card.get("card_images", [])
    if images:
        return images[0].get("image_url") or images[0].get("image_url_small")
    return None


def make_card_id(card: dict, set_code: str) -> str:
    card_id = str(card.get("id", "0"))
    clean_code = set_code.replace("/", "-").replace("\\", "-").replace(" ", "_").replace("?", "X").replace("*", "X").replace("<", "").replace(">", "").replace("|", "").replace('"', "").replace(":", "-")
    return f"ygo-{clean_code}-{card_id}"


# ─────────────────────────────────────────────
# Main scraper
# ─────────────────────────────────────────────

def scrape_set(client: YGOProDeckClient, set_info: dict, db_root: str,
               resume: bool = False) -> dict:
    """Scrape all cards from one set."""
    set_name = set_info.get("set_name", "Unknown")
    set_code_prefix = set_info.get("set_code", "UNK")
    stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    print(f"\n{'='*60}")
    print(f"  Set: {set_name}")
    print(f"  Code: {set_code_prefix}")
    print(f"  Cards: {set_info.get('num_of_cards', '?')}")
    print(f"  Released: {set_info.get('tcg_date', '?')}")
    print(f"{'='*60}")

    cards = client.get_cards_for_set(set_name)
    stats["total"] = len(cards)

    # Track unique cards (API may return duplicates with different artworks)
    seen_ids = set()

    for i, card in enumerate(cards, 1):
        card_id_num = str(card.get("id", "0"))
        if card_id_num in seen_ids:
            stats["skipped"] += 1
            continue
        seen_ids.add(card_id_num)

        card_name = card.get("name", "?")

        # Get set-specific info (code, rarity) from card_sets
        card_sets = card.get("card_sets", [])
        this_set = None
        for cs in card_sets:
            if cs.get("set_name", "").lower() == set_name.lower():
                this_set = cs
                break
        if this_set is None and card_sets:
            this_set = card_sets[0]

        set_code = (this_set or {}).get("set_code", f"{set_code_prefix}-{i:03d}")
        set_rarity = (this_set or {}).get("set_rarity", "")

        folder_id = make_card_id(card, set_code)
        card_dir = os.path.join(db_root, "yugioh", folder_id)

        # Resume check
        front_path = os.path.join(card_dir, "front.png")
        if resume and os.path.exists(front_path):
            stats["skipped"] += 1
            continue

        print(f"  [{i}/{len(cards)}] {folder_id}: {card_name}", end="", flush=True)

        os.makedirs(card_dir, exist_ok=True)

        # Save profile
        profile = build_profile(card, set_info, set_code, set_rarity)
        profile_path = os.path.join(card_dir, "profile.json")
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)

        # Download image
        img_url = get_image_url(card)
        if img_url:
            if client.download_image(img_url, front_path):
                stats["downloaded"] += 1
                print(" ✓")
            else:
                stats["failed"] += 1
                print(" ✗")
        else:
            print(" [no image]")
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Yu-Gi-Oh! cards from YGOProDeck into MatchIT DB"
    )
    parser.add_argument("--db-root", default=DEFAULT_DB_ROOT,
                        help="Database root path")
    parser.add_argument("--set", default="",
                        help="Comma-separated set names or short codes (e.g. MRD,LOB or 'Metal Raiders')")
    parser.add_argument("--all-sets", action="store_true",
                        help="Scrape ALL available sets (combine with --year/--after/--before to filter)")
    parser.add_argument("--year", type=int, default=0,
                        help="Filter sets by release year (e.g. --year 2024)")
    parser.add_argument("--after", default="",
                        help="Only sets released on or after this date (YYYY-MM-DD)")
    parser.add_argument("--before", default="",
                        help="Only sets released before this date (YYYY-MM-DD)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip cards that already have images")
    parser.add_argument("--list-sets", action="store_true",
                        help="List all available sets and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched")
    parser.add_argument("--max-cards", type=int, default=0,
                        help="Maximum number of cards to download per set (0 = all)")
    args = parser.parse_args()

    client = YGOProDeckClient()

    # Fetch all sets (needed for all modes)
    all_sets = client.get_sets()

    # Build lookup maps: by name (lowercase) and by code (uppercase)
    set_by_name = {s["set_name"].lower(): s for s in all_sets}
    set_by_code = {}
    for s in all_sets:
        code = (s.get("set_code") or "").split("-")[0].upper()
        if code and code not in set_by_code:
            set_by_code[code] = s

    # Apply date filters if provided (for --list-sets and --all-sets)
    filtered_sets = all_sets
    if args.year or args.after or args.before:
        filtered_sets = filter_sets_by_date(all_sets, year=args.year,
                                             after=args.after, before=args.before)

    # ── List sets mode ──
    if args.list_sets:
        display = sorted(filtered_sets, key=lambda s: s.get("tcg_date") or "0000", reverse=True)
        print(f"\n{'Name':<55} {'Code':<15} {'Cards':<6} {'Released'}")
        print("-" * 95)
        for s in display:
            print(f"{s.get('set_name','?'):<55} {s.get('set_code','?'):<15} "
                  f"{s.get('num_of_cards','?'):<6} {s.get('tcg_date','?')}")
        total_cards = sum(int(s.get("num_of_cards", 0) or 0) for s in display)
        print(f"\n{len(display)} sets, ~{total_cards} total cards")
        if args.year:
            print(f"(Filtered to year: {args.year})")
        if args.after:
            print(f"(Filtered: released on or after {args.after})")
        if args.before:
            print(f"(Filtered: released before {args.before})")
        return

    # ── Resolve target sets ──
    target_sets = []

    if args.all_sets:
        # Use filtered sets (already date-filtered above if flags provided)
        target_sets = sorted(filtered_sets, key=lambda s: s.get("tcg_date") or "0000")
    elif args.set:
        target_names = [s.strip() for s in args.set.split(",")]
        for name in target_names:
            # Try exact name match first
            key = name.lower()
            if key in set_by_name:
                target_sets.append(set_by_name[key])
                continue

            # Try short code match (e.g. MRD, LOB, BPRO)
            code_upper = name.upper()
            if code_upper in set_by_code:
                matched = set_by_code[code_upper]
                print(f"[INFO] Code '{name}' -> '{matched['set_name']}'")
                target_sets.append(matched)
                continue

            # Fuzzy match on name
            matches = [s for s in all_sets if key in s["set_name"].lower()]
            if matches:
                target_sets.append(matches[0])
                print(f"[INFO] Fuzzy matched '{name}' -> '{matches[0]['set_name']}'")
            else:
                print(f"[WARN] Set '{name}' not found -- skipping")

        # Apply date filters to explicit --set selections too
        if args.year or args.after or args.before:
            target_sets = filter_sets_by_date(target_sets, year=args.year,
                                               after=args.after, before=args.before)
    else:
        # Default sets
        for name in DEFAULT_SETS:
            key = name.lower()
            if key in set_by_name:
                target_sets.append(set_by_name[key])
            else:
                print(f"[WARN] Default set '{name}' not found")

    if not target_sets:
        print("No valid sets to scrape.")
        return

    print(f"\n[SCRAPER] Target: {len(target_sets)} sets")
    print(f"[SCRAPER] DB root: {os.path.abspath(args.db_root)}")
    print(f"[SCRAPER] Resume: {args.resume}")
    if args.year:
        print(f"[SCRAPER] Year filter: {args.year}")
    if args.after:
        print(f"[SCRAPER] After: {args.after}")
    if args.before:
        print(f"[SCRAPER] Before: {args.before}")

    if args.dry_run:
        total_cards = sum(int(s.get("num_of_cards", 0) or 0) for s in target_sets)
        print(f"\n[DRY RUN] Would fetch ~{total_cards} cards from {len(target_sets)} sets")
        for s in target_sets:
            print(f"  {s.get('set_code','?'):<15} {s.get('set_name','?'):<55} ~{s.get('num_of_cards','?')} cards  ({s.get('tcg_date','?')})")
        est_minutes = round(total_cards * 1.5 / 60)
        print(f"\n  Estimated embed time: ~{est_minutes} minutes ({total_cards} cards x ~1.5s)")
        return

    # Create DB root
    os.makedirs(os.path.join(args.db_root, "yugioh"), exist_ok=True)

    # Scrape
    grand_total = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    for i, set_data in enumerate(target_sets, 1):
        print(f"\n[{i}/{len(target_sets)}] Processing: {set_data.get('set_name', '?')}")
        stats = scrape_set(client, set_data, args.db_root, resume=args.resume)
        for k in grand_total:
            grand_total[k] += stats[k]

    # Summary
    print(f"\n{'='*60}")
    print(f"  YU-GI-OH SCRAPE COMPLETE")
    print(f"  Total cards:     {grand_total['total']}")
    print(f"  Downloaded:      {grand_total['downloaded']}")
    print(f"  Skipped (exist): {grand_total['skipped']}")
    print(f"  Failed:          {grand_total['failed']}")
    print(f"{'='*60}")

    # Save manifest
    manifest_path = os.path.join(args.db_root, "yugioh", "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "scrape_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sets_scraped": [s["set_name"] for s in target_sets],
            "stats": grand_total,
        }, f, indent=2)
    print(f"\nManifest saved: {manifest_path}")


if __name__ == "__main__":
    main()