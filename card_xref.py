"""
card_xref.py — Cross-reference mapper for trading cards
========================================================
Links the same card across different price/listing sources
(TCGPlayer, Cardmarket, eBay) and maintains a unified xref table.

The scraper (scrape_pokemon_tcg.py) already pulls TCGPlayer and
Cardmarket IDs from pokemontcg.io. This module:

1. Loads existing profile.json files and builds an xref index
2. Provides lookup functions: "given a TCGPlayer ID, what's the MatchIT SKU?"
3. Can enrich profiles with additional source IDs (eBay, PriceCharting, etc.)
4. Exports a flat CSV for manual review or bulk import

Usage:
    from card_xref import XRefIndex

    idx = XRefIndex("C:/Users/c_a_b/My Drive/CardsDB")
    idx.build()

    # Lookup by any external ID
    sku = idx.find_by_tcgplayer("12345")
    sku = idx.find_by_cardmarket("67890")
    sku = idx.find_by_name("Charizard", set_id="base1")

    # Export for review
    idx.export_csv("xref_export.csv")
"""

import csv
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class XRefIndex:
    """
    In-memory cross-reference index for card profiles.

    Maps between:
        - MatchIT card_id (folder name, e.g. "base1-4")
        - TCGPlayer product ID
        - Cardmarket product ID
        - Card name + set (for fuzzy matching)
    """

    def __init__(self, db_root: str):
        self.db_root = db_root
        self.profiles: Dict[str, dict] = {}         # card_id → profile
        self._by_tcgplayer: Dict[str, str] = {}      # tcgplayer_id → card_id
        self._by_cardmarket: Dict[str, str] = {}     # cardmarket_id → card_id
        self._by_name_set: Dict[str, List[str]] = {} # "name|set_id" → [card_ids]
        self._by_api_id: Dict[str, str] = {}          # pokemontcg.io id → card_id

    def build(self, game_folder: str = "pokemon") -> int:
        """
        Scan all profile.json files and build the index.
        Returns number of profiles loaded.
        """
        base = os.path.join(self.db_root, game_folder)
        if not os.path.isdir(base):
            print(f"[XREF] Directory not found: {base}")
            return 0

        count = 0
        for entry in os.scandir(base):
            if not entry.is_dir() or entry.name.startswith("_"):
                continue

            profile_path = os.path.join(entry.path, "profile.json")
            if not os.path.exists(profile_path):
                continue

            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    prof = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[XREF] Skipping {entry.name}: {e}")
                continue

            card_id = entry.name
            self.profiles[card_id] = prof
            self._index_profile(card_id, prof)
            count += 1

        print(f"[XREF] Indexed {count} cards from {game_folder}/")
        print(f"  TCGPlayer IDs: {len(self._by_tcgplayer)}")
        print(f"  Cardmarket IDs: {len(self._by_cardmarket)}")
        return count

    def _index_profile(self, card_id: str, prof: dict):
        """Add a single profile to all lookup indices."""
        # TCGPlayer
        tcgp_id = str(prof.get("tcgplayer_id", "")).strip()
        if tcgp_id:
            self._by_tcgplayer[tcgp_id] = card_id

        # Cardmarket
        cm_id = str(prof.get("cardmarket_id", "")).strip()
        if cm_id:
            self._by_cardmarket[cm_id] = card_id

        # API ID
        api_id = str(prof.get("api_id", "")).strip()
        if api_id:
            self._by_api_id[api_id] = card_id

        # Name + Set compound key
        name = (prof.get("name") or "").strip().lower()
        set_id = (prof.get("set_id") or "").strip().lower()
        if name:
            key = f"{name}|{set_id}"
            self._by_name_set.setdefault(key, []).append(card_id)

    # ─────────────────────────────────────────
    # Lookups
    # ─────────────────────────────────────────

    def find_by_tcgplayer(self, tcgplayer_id: str) -> Optional[str]:
        """Find MatchIT card_id by TCGPlayer product ID."""
        return self._by_tcgplayer.get(str(tcgplayer_id).strip())

    def find_by_cardmarket(self, cardmarket_id: str) -> Optional[str]:
        """Find MatchIT card_id by Cardmarket product ID."""
        return self._by_cardmarket.get(str(cardmarket_id).strip())

    def find_by_api_id(self, api_id: str) -> Optional[str]:
        """Find MatchIT card_id by pokemontcg.io API ID."""
        return self._by_api_id.get(str(api_id).strip())

    def find_by_name(self, name: str, set_id: str = "") -> List[str]:
        """
        Find card_ids by name (optionally filtered by set).
        Returns list since the same name can appear in multiple sets.
        """
        key = f"{name.strip().lower()}|{set_id.strip().lower()}"
        if set_id:
            return self._by_name_set.get(key, [])

        # No set specified — find across all sets
        results = []
        prefix = f"{name.strip().lower()}|"
        for k, ids in self._by_name_set.items():
            if k.startswith(prefix):
                results.extend(ids)
        return results

    def get_profile(self, card_id: str) -> Optional[dict]:
        """Get the full profile for a card."""
        return self.profiles.get(card_id)

    def get_prices(self, card_id: str) -> dict:
        """Get price data for a card across all sources."""
        prof = self.profiles.get(card_id, {})
        return prof.get("prices", {})

    def get_xref_urls(self, card_id: str) -> dict:
        """Get external URLs for a card."""
        prof = self.profiles.get(card_id, {})
        urls = {}
        if prof.get("tcgplayer_url"):
            urls["tcgplayer"] = prof["tcgplayer_url"]
        if prof.get("cardmarket_url"):
            urls["cardmarket"] = prof["cardmarket_url"]
        return urls

    # ─────────────────────────────────────────
    # Enrichment — add IDs from new sources
    # ─────────────────────────────────────────

    def enrich_profile(self, card_id: str, updates: dict, save: bool = True) -> bool:
        """
        Add or update fields in a card's profile.

        Example:
            idx.enrich_profile("base1-4", {
                "ebay_listing_id": "394851234567",
                "pricecharting_id": "charizard-base-set",
            })
        """
        if card_id not in self.profiles:
            return False

        self.profiles[card_id].update(updates)

        if save:
            # Determine the game folder from the card_id prefix or category
            prof = self.profiles[card_id]
            game = prof.get("category", "POKEMON").lower()
            game_folder = {"pokemon": "pokemon", "mtg": "mtg", "yugioh": "yugioh"}.get(game, "pokemon")

            profile_path = os.path.join(self.db_root, game_folder, card_id, "profile.json")
            try:
                with open(profile_path, "w", encoding="utf-8") as f:
                    json.dump(prof, f, indent=2, ensure_ascii=False)
                return True
            except OSError as e:
                print(f"[XREF] Failed to save enriched profile {card_id}: {e}")
                return False

        return True

    # ─────────────────────────────────────────
    # Export
    # ─────────────────────────────────────────

    def export_csv(self, output_path: str) -> int:
        """
        Export the full xref table as CSV for manual review.
        Returns number of rows written.
        """
        fields = [
            "card_id", "name", "set_id", "set_name", "card_number",
            "rarity", "energy_type", "set_era",
            "tcgplayer_id", "tcgplayer_url",
            "cardmarket_id", "cardmarket_url",
            "api_id",
        ]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for card_id, prof in sorted(self.profiles.items()):
                row = {"card_id": card_id}
                row.update(prof)
                writer.writerow(row)

        print(f"[XREF] Exported {len(self.profiles)} cards to {output_path}")
        return len(self.profiles)

    # ─────────────────────────────────────────
    # Stats
    # ─────────────────────────────────────────

    def stats(self) -> dict:
        """Summary statistics for the index."""
        rarities = {}
        eras = {}
        sets = {}
        for prof in self.profiles.values():
            r = prof.get("rarity", "")
            if r:
                rarities[r] = rarities.get(r, 0) + 1
            e = prof.get("set_era", "")
            if e:
                eras[e] = eras.get(e, 0) + 1
            s = prof.get("set_id", "")
            if s:
                sets[s] = sets.get(s, 0) + 1

        return {
            "total_cards": len(self.profiles),
            "with_tcgplayer": len(self._by_tcgplayer),
            "with_cardmarket": len(self._by_cardmarket),
            "unique_sets": len(sets),
            "by_rarity": dict(sorted(rarities.items(), key=lambda x: -x[1])),
            "by_era": dict(sorted(eras.items())),
        }


# ─────────────────────────────────────────────
# CLI helper
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Card cross-reference index")
    parser.add_argument("--db-root", required=True, help="CardsDB root path")
    parser.add_argument("--game", default="pokemon", help="Game folder to index")
    parser.add_argument("--export", default="", help="Export CSV path")
    parser.add_argument("--lookup", default="", help="Look up a card by name")
    parser.add_argument("--stats", action="store_true", help="Show index stats")
    args = parser.parse_args()

    idx = XRefIndex(args.db_root)
    idx.build(args.game)

    if args.stats:
        import pprint
        pprint.pprint(idx.stats())

    if args.lookup:
        results = idx.find_by_name(args.lookup)
        if results:
            for cid in results:
                p = idx.get_profile(cid)
                print(f"  {cid}: {p.get('name')} [{p.get('set_name')}] "
                      f"- {p.get('rarity')} - TCG:{p.get('tcgplayer_id')} CM:{p.get('cardmarket_id')}")
        else:
            print(f"  No results for '{args.lookup}'")

    if args.export:
        idx.export_csv(args.export)