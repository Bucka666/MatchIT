"""
backfill_fix_orig_names.py — Fix original_filename for scraped card rows
=========================================================================
Rows inserted by backfill_scraped_cards.py had original_filename="front.png",
which doesn't match the FRONT classifier in app.py's _infer_view_from_orig().
That classifier requires the filename to contain "_FRONT" (e.g. "mtg-hob-29_FRONT.png").

This script renames those rows to "{sku}_FRONT.png" so the cache loader
correctly classifies them as FRONT images.

Usage:
    modal run backfill_fix_orig_names.py
"""

import os
import sys
sys.path.insert(0, "/app")

import modal
from modal_config import image, vol

app = modal.App("grailsweep-fix-orig-names")


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
    ],
    timeout=300,
)
def fix_orig_names():
    import sqlite3
    from pathlib import Path

    os.environ["LOCALAPPDATA"] = "/modal_data"

    db_path = Path("/modal_data/MatchITv2_ProductMatch_Data/cards/images.db")
    if not db_path.exists():
        print(f"[FIX-ORIG] images.db not found at {db_path}", flush=True)
        return

    conn = sqlite3.connect(str(db_path))

    rows = conn.execute("""
        SELECT image_id, sku, original_filename
        FROM images
        WHERE original_filename = 'front.png'
          AND (sku LIKE 'mtg-%' OR sku LIKE 'pokemon-%' OR sku LIKE 'ygo-%')
    """).fetchall()

    print(f"[FIX-ORIG] Found {len(rows)} rows with bad original_filename", flush=True)

    if not rows:
        print("[FIX-ORIG] Nothing to do.", flush=True)
        conn.close()
        return

    updated = 0
    for image_id, sku, old_name in rows:
        new_name = f"{sku}_FRONT.png"
        conn.execute(
            "UPDATE images SET original_filename = ? WHERE image_id = ?",
            (new_name, image_id),
        )
        print(f"[FIX-ORIG] {sku}: {old_name} → {new_name}", flush=True)
        updated += 1

    conn.commit()
    conn.close()

    print(f"[FIX-ORIG] Done — {updated} rows updated", flush=True)


@app.local_entrypoint()
def main():
    fix_orig_names.remote()
