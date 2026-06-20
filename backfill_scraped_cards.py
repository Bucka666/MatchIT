"""
backfill_scraped_cards.py — Register scraped card images into images.db
=======================================================================
Walks the scraped cards directory and INSERTs any card folder that has a
front.png but no corresponding images.db row. Safe to re-run: existing
rows are detected and skipped via O(1) set lookup.

Local dry-run (Windows):
    python backfill_scraped_cards.py --tcg mtg --dry-run
    python backfill_scraped_cards.py --tcg mtg --set hob,fra

Via Modal (live volume):
    modal run backfill_scraped_cards.py -- --tcg mtg
    modal run backfill_scraped_cards.py -- --tcg all --dry-run
"""

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime

import modal
sys.path.insert(0, "/app")
from modal_config import VOLUME_NAME, vol
from r2_util import upload_to_r2

# ─────────────────────────────────────────────────────────────
# Modal setup
# ─────────────────────────────────────────────────────────────

app = modal.App("grailsweep-backfill")

_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("Pillow", "boto3")
    .add_local_file(__file__, "/app/backfill_scraped_cards.py")
    .add_local_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "modal_config.py"), "/app/modal_config.py")
    .add_local_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "r2_util.py"), "/app/r2_util.py")
)

# ─────────────────────────────────────────────────────────────
# TCG folder mapping
# ─────────────────────────────────────────────────────────────

TCG_FOLDERS = {
    "mtg":     "mtg",
    "pokemon": "pokemon",
    "yugioh":  "yugioh",
}


# ─────────────────────────────────────────────────────────────
# Path helpers — computed at call time so LOCALAPPDATA is set
# ─────────────────────────────────────────────────────────────

def _get_paths():
    base = os.environ.get("LOCALAPPDATA", "/modal_data")
    data_dir = os.path.join(base, "MatchITv2_ProductMatch_Data", "cards")
    db_path  = os.path.join(data_dir, "images.db")
    img_dir  = os.path.join(data_dir, "image_db")
    os.makedirs(img_dir, exist_ok=True)
    return db_path, img_dir, data_dir  # data_dir == cards_root


# ─────────────────────────────────────────────────────────────
# Core registration logic (importable by set_scheduler.py)
# ─────────────────────────────────────────────────────────────

def register_scraped_cards(
    tcg: str = "all",
    sets=None,       # list of set codes to filter, or None for all
    dry_run: bool = False,
    cards_root: str = None,
) -> dict:
    """
    Walk {cards_root}/{tcg_folder}/ and register any card that has a
    front.png but no images.db row.

    Args:
        tcg:     "mtg", "pokemon", "yugioh", or "all"
        sets:    list of set codes to restrict (e.g. ["hob", "fra"]),
                 or None to process every card in the folder
        dry_run: if True, print what would be inserted without writing

    Returns:
        dict keyed by TCG name, each value: {registered, skipped, errors}
    """
    db_path, img_dir, _default_root = _get_paths()
    if cards_root is None:
        cards_root = _default_root

    if not os.path.exists(db_path):
        print(f"[BACKFILL] images.db not found at {db_path}", flush=True)
        return {}

    conn = sqlite3.connect(db_path)
    existing_skus = {row[0] for row in conn.execute("SELECT sku FROM images")}

    tcg_keys = list(TCG_FOLDERS.keys()) if tcg.lower() == "all" else [tcg.lower()]
    totals = {}

    for tcg_key in tcg_keys:
        folder = TCG_FOLDERS.get(tcg_key)
        if not folder:
            print(f"[BACKFILL] Unknown TCG: {tcg_key}", flush=True)
            continue

        tcg_dir = os.path.join(cards_root, folder)
        if not os.path.isdir(tcg_dir):
            print(f"[BACKFILL] {tcg_key}: directory not found: {tcg_dir}", flush=True)
            totals[tcg_key] = {"registered": 0, "skipped": 0, "errors": 0}
            continue

        registered = skipped = errors = 0

        for entry in sorted(os.scandir(tcg_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue

            card_sku = entry.name   # e.g. "mtg-hob-29"
            card_dir = entry.path

            # Set filter — any requested set code must appear in the sku
            if sets:
                if not any(s.lower() in card_sku.lower() for s in sets):
                    continue

            # Skip if already registered
            if card_sku in existing_skus:
                skipped += 1
                continue

            front_png = os.path.join(card_dir, "front.png")
            if not os.path.exists(front_png):
                skipped += 1
                continue

            # Read name from profile.json
            description = card_sku
            profile_json = os.path.join(card_dir, "profile.json")
            if os.path.exists(profile_json):
                try:
                    with open(profile_json, "r", encoding="utf-8") as f:
                        description = json.load(f).get("name", card_sku)
                except Exception:
                    pass

            if dry_run:
                print(f"[BACKFILL] DRY-RUN: would register {card_sku} ({description})", flush=True)
                registered += 1
                continue

            try:
                from PIL import Image, ImageOps
                image_id  = str(uuid.uuid4())
                dst_path  = os.path.join(img_dir, f"{image_id}.jpg")
                # Convert PNG → JPEG (mirrors normalize_uploaded_image in app.py;
                # called inline to avoid Flask context dependency)
                with Image.open(front_png) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    im.save(dst_path, format="JPEG", quality=95, optimize=True)
                upload_to_r2(image_id, dst_path)
                conn.execute(
                    """INSERT OR REPLACE INTO images
                       (image_id, sku, description, original_filename, path, added_at, embedding)
                       VALUES (?,?,?,?,?,?,NULL)""",
                    (image_id, card_sku, description, f"{card_sku}_FRONT.png",
                     dst_path, datetime.utcnow().isoformat()),
                )
                existing_skus.add(card_sku)
                registered += 1
                print(f"[BACKFILL] + {card_sku}: {description}", flush=True)
            except Exception as e:
                print(f"[BACKFILL] ERROR {card_sku}: {e}", flush=True)
                errors += 1

        if not dry_run:
            conn.commit()

        print(
            f"[BACKFILL] {tcg_key}: {registered} cards registered, "
            f"{skipped} skipped, {errors} errors",
            flush=True,
        )
        totals[tcg_key] = {"registered": registered, "skipped": skipped, "errors": errors}

    conn.close()
    return totals


# ─────────────────────────────────────────────────────────────
# Modal remote wrapper
# ─────────────────────────────────────────────────────────────

@app.function(
    image=_image,
    volumes={"/modal_data": vol},
    secrets=[modal.Secret.from_name("r2-credentials")],
    timeout=3600,
)
def _run_remote(tcg: str, sets, dry_run: bool, cards_root: str = None):
    import os, sys
    os.environ["LOCALAPPDATA"] = "/modal_data"
    sys.path.insert(0, "/app")
    from backfill_scraped_cards import register_scraped_cards as _reg
    return _reg(tcg=tcg, sets=sets, dry_run=dry_run, cards_root=cards_root)


@app.local_entrypoint()
def main(tcg: str = "all", sets: str = "", dry_run: bool = False, cards_root: str = ""):
    """
    Run with:
      modal run backfill_scraped_cards.py --tcg mtg --sets hob,fra --dry-run

    Args:
        tcg: "mtg", "pokemon", "yugioh", or "all" (default: all)
        sets: Comma-separated set codes (default: all sets in chosen TCG)
        dry_run: If True, print what would be inserted without writing
    """
    if tcg not in ("mtg", "pokemon", "yugioh", "all"):
        print(f"ERROR: invalid tcg '{tcg}'. Must be one of mtg, pokemon, yugioh, all.")
        return

    sets_list = [s.strip() for s in sets.split(",") if s.strip()] if sets else None

    _run_remote.remote(tcg=tcg, sets=sets_list, dry_run=dry_run, cards_root=cards_root or None)
