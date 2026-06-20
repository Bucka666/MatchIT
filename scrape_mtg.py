"""
scrape_mtg.py — Pull Magic: The Gathering cards from Scryfall
=============================================================
Builds the CardsDB/mtg/ folder structure compatible with MatchIT.

Each card gets:
    CardsDB/mtg/{set_code}-{collector_number}/
        front.png          ← card image
        profile.json       ← metadata for filtering & cross-referencing

Usage:
    python scrape_mtg.py                                  # fetch default sets
    python scrape_mtg.py --set fdn                        # single set
    python scrape_mtg.py --set fdn,mkm,woe               # multiple sets
    python scrape_mtg.py --resume                         # skip existing
    python scrape_mtg.py --list-sets                      # show all sets

Requires: requests, Pillow (pip install requests Pillow)
Scryfall API docs: https://scryfall.com/docs/api
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

API_BASE = "https://api.scryfall.com"
DEFAULT_DB_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "CardsDB")
RATE_LIMIT_DELAY = 0.5  # Scryfall asks for 50-100ms between requests
IMAGE_DOWNLOAD_TIMEOUT = 30
USER_AGENT = "MatchIT-MTG-Scraper/1.0"

# Default sets to scrape if none specified
DEFAULT_SETS = ["fdn", "mkm", "woe"]


# ─────────────────────────────────────────────
# Rarity normalization
# ─────────────────────────────────────────────

RARITY_MAP = {
    "common":   "COMMON",
    "uncommon": "UNCOMMON",
    "rare":     "RARE",
    "mythic":   "MYTHIC",
    "special":  "RARE_SECRET",
    "bonus":    "PROMO",
}


def normalize_rarity(raw: Optional[str]) -> str:
    if not raw:
        return ""
    return RARITY_MAP.get(raw.lower().strip(), "RARE")


# ─────────────────────────────────────────────
# Color mapping
# ─────────────────────────────────────────────

def extract_color(card: dict) -> str:
    """Map MTG colors to our enum."""
    colors = card.get("colors") or []
    if not colors:
        # Check if it's a land
        type_line = (card.get("type_line") or "").lower()
        if "land" in type_line:
            return "LAND"
        return "COLORLESS"
    if len(colors) > 1:
        return "MULTI"
    color_map = {
        "W": "WHITE", "U": "BLUE", "B": "BLACK",
        "R": "RED", "G": "GREEN",
    }
    return color_map.get(colors[0], "COLORLESS")


def extract_card_type(card: dict) -> str:
    """Extract the primary card type."""
    type_line = (card.get("type_line") or "").lower()
    if "creature" in type_line:
        return "CREATURE"
    elif "instant" in type_line:
        return "INSTANT"
    elif "sorcery" in type_line:
        return "SORCERY"
    elif "enchantment" in type_line:
        return "ENCHANTMENT"
    elif "artifact" in type_line:
        return "ARTIFACT"
    elif "planeswalker" in type_line:
        return "PLANESWALKER"
    elif "land" in type_line:
        return "LAND"
    return ""


# ─────────────────────────────────────────────
# Era detection
# ─────────────────────────────────────────────

def detect_era(released_at: str) -> str:
    """Determine era from release date."""
    if not released_at:
        return ""
    try:
        year = int(released_at[:4])
        if year <= 2002:
            return "VINTAGE_MTG"
        else:
            return "MODERN_MTG"
    except (ValueError, IndexError):
        return ""


# ─────────────────────────────────────────────
# Profile builder
# ─────────────────────────────────────────────

def build_profile(card: dict, set_data: dict) -> dict:
    """Build a MatchIT profile from a Scryfall card object."""
    prices = card.get("prices") or {}

    # Handle double-faced cards — use front face data
    card_face = card
    if card.get("card_faces") and len(card["card_faces"]) > 0:
        card_face = card["card_faces"][0]

    return {
        # Identity
        "api_id":           card.get("id", ""),
        "name":             card_face.get("name", card.get("name", "")),
        "card_number":      card.get("collector_number", ""),
        "set_id":           card.get("set", ""),
        "set_name":         card.get("set_name", ""),
        "category":         "MTG",

        # Filterable fields (match vertical.json)
        "rarity":           normalize_rarity(card.get("rarity")),
        "mtg_color":        extract_color(card_face if card_face != card else card),
        "mtg_card_type":    extract_card_type(card_face if card_face != card else card),
        "set_era":          detect_era(card.get("released_at", set_data.get("released_at", ""))),
        "language":         "EN",
        "edition":          "",

        # Cross-reference IDs
        "scryfall_id":      card.get("id", ""),
        "scryfall_url":     card.get("scryfall_uri", ""),
        "tcgplayer_id":     str(card.get("tcgplayer_id", "")),
        "cardmarket_id":    str(card.get("cardmarket_id", "")),

        # Price snapshot
        "prices": {
            "tcgplayer": {
                "normal": {
                    "market": _safe_float(prices.get("usd")),
                },
                "foil": {
                    "market": _safe_float(prices.get("usd_foil")),
                },
            },
            "cardmarket": {
                "normal": {
                    "trend": _safe_float(prices.get("eur")),
                },
                "foil": {
                    "trend": _safe_float(prices.get("eur_foil")),
                },
            },
        },

        # Marketplace URLs
        "tcgplayer_url":    card.get("purchase_uris", {}).get("tcgplayer", ""),
        "cardmarket_url":   card.get("purchase_uris", {}).get("cardmarket", ""),

        # Extra metadata
        "artist":           card.get("artist", ""),
        "type_line":        card.get("type_line", ""),
        "mana_cost":        card_face.get("mana_cost", card.get("mana_cost", "")),
        "oracle_text":      card_face.get("oracle_text", card.get("oracle_text", "")),
        "power":            card_face.get("power", card.get("power", "")),
        "toughness":        card_face.get("toughness", card.get("toughness", "")),
        "cmc":              card.get("cmc", 0),
    }


def _safe_float(val) -> Optional[float]:
    """Safely convert a price string to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────
# API client
# ─────────────────────────────────────────────

class ScryfallClient:
    """Thin wrapper around Scryfall API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.headers["Accept"] = "application/json"

    def _get(self, url: str, params: dict = None) -> dict:
        for attempt in range(6):
            time.sleep(RATE_LIMIT_DELAY)
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"  [RATE] 429 - waiting {wait}s (attempt {attempt+1}/6)...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return {}

    def get_sets(self) -> List[dict]:
        """Fetch all sets."""
        data = self._get(f"{API_BASE}/sets")
        return data.get("data", [])

    def get_cards_for_set(self, set_code: str) -> List[dict]:
        """Fetch all cards in a set (handles pagination)."""
        cards = []
        url = f"{API_BASE}/cards/search"
        params = {
            "q": f"set:{set_code}",
            "unique": "prints",
            "order": "set",
        }

        while url:
            try:
                data = self._get(url, params=params if not cards else None)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    print(f"  [WARN] No cards found for set {set_code}")
                    break
                raise

            cards.extend(data.get("data", []))

            if data.get("has_more") and data.get("next_page"):
                url = data["next_page"]
                params = None  # next_page includes all params
            else:
                break

        return cards

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
# Image URL extraction
# ─────────────────────────────────────────────

def get_image_url(card: dict) -> Optional[str]:
    """Get the best image URL for a card."""
    # Normal cards
    image_uris = card.get("image_uris")
    if image_uris:
        return image_uris.get("large") or image_uris.get("normal") or image_uris.get("small")

    # Double-faced cards — use front face
    card_faces = card.get("card_faces", [])
    if card_faces and card_faces[0].get("image_uris"):
        face_uris = card_faces[0]["image_uris"]
        return face_uris.get("large") or face_uris.get("normal") or face_uris.get("small")

    return None


# ─────────────────────────────────────────────
# Card ID generation
# ─────────────────────────────────────────────

def make_card_id(card: dict) -> str:
    """Generate a unique folder-safe card ID."""
    set_code = card.get("set", "unk")
    collector_num = card.get("collector_number", "0")
    # Clean up collector number (some have * or other chars)
    collector_num = collector_num.replace("*", "s").replace("/", "-")
    return f"mtg-{set_code}-{collector_num}"


# ─────────────────────────────────────────────
# Main scraper logic
# ─────────────────────────────────────────────

def scrape_set(client: ScryfallClient, set_data: dict, db_root: str,
               resume: bool = False) -> dict:
    """Scrape all cards from one set."""
    set_code = set_data["code"]
    set_name = set_data.get("name", set_code)
    stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    print(f"\n{'='*60}")
    print(f"  Set: {set_name} ({set_code})")
    print(f"  Released: {set_data.get('released_at', '?')}")
    print(f"  Card count: {set_data.get('card_count', '?')}")
    print(f"{'='*60}")

    try:
        cards = client.get_cards_for_set(set_code)
    except Exception as e:
        print(f"  [WARN] Failed to fetch set {set_code}: {e}")
        cards = []
    stats["total"] = len(cards)

    for i, card in enumerate(cards, 1):
        card_id = make_card_id(card)
        card_name = card.get("name", "?")
        card_dir = os.path.join(db_root, "mtg", card_id)

        # Skip tokens, emblems, art series
        layout = card.get("layout", "")
        if layout in ("token", "emblem", "art_series", "double_faced_token"):
            stats["skipped"] += 1
            continue

        # Resume: skip if already exists
        front_path = os.path.join(card_dir, "front.png")
        if resume and os.path.exists(front_path):
            stats["skipped"] += 1
            continue

        print(f"  [{i}/{len(cards)}] {card_id}: {card_name}", end="", flush=True)

        os.makedirs(card_dir, exist_ok=True)

        # Save profile
        profile = build_profile(card, set_data)
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
        description="Scrape MTG cards from Scryfall into MatchIT DB"
    )
    parser.add_argument("--db-root", default=DEFAULT_DB_ROOT,
                        help="Database root path")
    parser.add_argument("--set", default="",
                        help="Comma-separated set codes (default: fdn,mkm,woe)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip cards that already have images")
    parser.add_argument("--list-sets", action="store_true",
                        help="List all available sets and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without downloading")
    parser.add_argument("--type", default="",
                        help="Filter sets by type: core,expansion,masters,draft_innovation,funny,commander")
    parser.add_argument("--all-sets", action="store_true",
                        help="Scrape all available sets")
    args = parser.parse_args()

    client = ScryfallClient()

    # List sets mode
    if args.list_sets:
        sets = client.get_sets()
        # Filter to main set types
        valid_types = {"core", "expansion", "masters", "draft_innovation", "funny", "commander"}
        if args.type:
            valid_types = set(args.type.split(","))

        filtered = [s for s in sets if s.get("set_type") in valid_types]
        filtered.sort(key=lambda s: s.get("released_at", ""), reverse=True)

        print(f"\n{'Code':<10} {'Name':<45} {'Released':<12} {'Cards':<6} {'Type'}")
        print("-" * 90)
        for s in filtered:
            print(f"{s['code']:<10} {s.get('name','?'):<45} "
                  f"{s.get('released_at','?'):<12} {s.get('card_count','?'):<6} {s.get('set_type','?')}")
        print(f"\nShowing {len(filtered)} sets")
        return

    # Resolve target sets
    all_sets = client.get_sets()
    set_lookup = {s["code"]: s for s in all_sets}

    if args.all_sets:
        valid_types = {"core", "expansion", "masters", "draft_innovation", "commander"}
        if args.type:
            valid_types = set(args.type.split(","))
        target_codes = [s["code"] for s in all_sets if s.get("set_type") in valid_types]
    elif args.set:
        target_codes = [s.strip().lower() for s in args.set.split(",")]
    else:
        target_codes = DEFAULT_SETS

    target_sets = []
    for code in target_codes:
        if code in set_lookup:
            target_sets.append(set_lookup[code])
        else:
            print(f"[WARN] Set '{code}' not found — skipping")

    if not target_sets:
        print("No valid sets to scrape.")
        return

    print(f"\n[SCRAPER] Target: {len(target_sets)} sets")
    print(f"[SCRAPER] DB root: {os.path.abspath(args.db_root)}")
    print(f"[SCRAPER] Resume: {args.resume}")

    if args.dry_run:
        total_cards = sum(s.get("card_count", 0) for s in target_sets)
        print(f"\n[DRY RUN] Would fetch ~{total_cards} cards from {len(target_sets)} sets")
        for s in target_sets:
            print(f"  {s['code']:<10} {s.get('name','?'):<45} ~{s.get('card_count','?')} cards")
        return

    # Create DB root
    os.makedirs(os.path.join(args.db_root, "mtg"), exist_ok=True)

    # Scrape
    grand_total = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    for i, set_data in enumerate(target_sets, 1):
        print(f"\n[{i}/{len(target_sets)}] Processing: {set_data.get('name', set_data['code'])}")
        stats = scrape_set(client, set_data, args.db_root, resume=args.resume)
        for k in grand_total:
            grand_total[k] += stats[k]

    # Summary
    print(f"\n{'='*60}")
    print(f"  MTG SCRAPE COMPLETE")
    print(f"  Total cards:     {grand_total['total']}")
    print(f"  Downloaded:      {grand_total['downloaded']}")
    print(f"  Skipped (exist): {grand_total['skipped']}")
    print(f"  Failed:          {grand_total['failed']}")
    print(f"{'='*60}")

    # Save manifest
    manifest_path = os.path.join(args.db_root, "mtg", "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "scrape_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sets_scraped": [s["code"] for s in target_sets],
            "stats": grand_total,
        }, f, indent=2)
    print(f"\nManifest saved: {manifest_path}")


if __name__ == "__main__":
    main()