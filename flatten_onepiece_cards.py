"""
flatten_onepiece_cards.py — required pre-step before build_one_piece_data.py
for any new pack pulled from punk-records.

punk-records' real per-pack output ships one JSON file PER CARD, nested
under a per-pack directory: english/cards/{PACK_ID}/{card_id}.json.
build_one_piece_data.py expects the older per-pack shape instead -- one
file per pack, each a JSON list of that pack's cards (matches
build_one_piece_data.py's own PUNK_DATA.glob("*.json") -> isinstance(...,
list) loop). This script bridges that gap: for each pack directory under
SRC, it reads every per-card file and writes ONE combined
english/cards_flat/{PACK_ID}.json list file.

Run this first, then point build_one_piece_data.py's --data-dir at
cards_flat/ (not cards/ directly) for any new set pull.

Run: python flatten_onepiece_cards.py
"""
import json
from pathlib import Path

SRC = Path(r"C:\punk-records\english\cards")
DST = Path(r"C:\punk-records\english\cards_flat")
DST.mkdir(parents=True, exist_ok=True)

total_files = total_cards = 0
for pack_dir in SRC.iterdir():
    if not pack_dir.is_dir():
        continue
    cards = []
    for card_file in pack_dir.glob("*.json"):
        try:
            cards.append(json.loads(card_file.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"SKIP unreadable: {card_file} ({e})")
    if not cards:
        continue
    (DST / f"{pack_dir.name}.json").write_text(
        json.dumps(cards, ensure_ascii=False), encoding="utf-8"
    )
    total_files += 1
    total_cards += len(cards)

print(f"Wrote {total_files} pack files, {total_cards} total cards -> {DST}")
