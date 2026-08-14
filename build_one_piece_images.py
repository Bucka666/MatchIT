"""
Download One Piece card images from Bandai CDN to C:\CardsDB\onepiece\{id}\front.png
Skips cards that already have a front.png (safe to re-run).
Rate-limited to avoid hammering Bandai's CDN.

Run: python build_one_piece_images.py
"""

import json
import time
import urllib.request
from pathlib import Path

CARDSDB_OP = Path(r"C:\CardsDB\onepiece")
DELAY = 0.05  # seconds between requests — polite rate limit

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://en.onepiece-cardgame.com/"
}

total = 0
skipped = 0
failed = []

card_dirs = sorted(CARDSDB_OP.iterdir())
print(f"Processing {len(card_dirs)} cards...")

for card_dir in card_dirs:
    img_path = card_dir / "front.png"
    if img_path.exists():
        skipped += 1
        continue

    profile_path = card_dir / "profile.json"
    if not profile_path.exists():
        continue

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    img_url = profile.get("img_url", "")
    if not img_url:
        failed.append(f"{card_dir.name}: no img_url")
        continue

    try:
        req = urllib.request.Request(img_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            img_path.write_bytes(resp.read())
        total += 1
        if total % 100 == 0:
            print(f"  Downloaded {total}...")
        time.sleep(DELAY)
    except Exception as e:
        failed.append(f"{card_dir.name}: {e}")
        print(f"  FAILED {card_dir.name}: {e}")

print(f"\nDone: {total} downloaded, {skipped} skipped, {len(failed)} failed")
if failed:
    print("Failed cards:")
    for f in failed[:20]:
        print(f"  {f}")
