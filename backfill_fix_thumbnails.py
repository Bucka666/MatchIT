"""
backfill_fix_thumbnails.py — Convert .png entries in images.db to .jpg
=======================================================================
Fixes cards registered by an earlier backfill run that wrote .png files
instead of .jpg. The app serves thumbnails as /img/db/{uuid}.jpg, so
.png paths never resolve.

For each images.db row where path ends in '.png':
  1. Open the PNG with PIL, convert to RGB, save as .jpg
  2. UPDATE the path column to the new .jpg path
  3. Delete the original .png file

Safe to re-run: rows already pointing at .jpg are skipped.

Usage:
    modal run backfill_fix_thumbnails.py
"""

import os
import sys
sys.path.insert(0, "/app")

import modal
from modal_config import VOLUME_NAME, vol, image

app = modal.App("grailsweep-fix-thumbnails")


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
    ],
    timeout=600,
)
def fix_thumbnails():
    import os
    import sqlite3
    from pathlib import Path
    from PIL import Image, ImageOps

    os.environ["LOCALAPPDATA"] = "/modal_data"

    data_dir = Path("/modal_data/MatchITv2_ProductMatch_Data/cards")
    db_path  = data_dir / "images.db"

    if not db_path.exists():
        print(f"[FIX] images.db not found at {db_path}", flush=True)
        return

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT image_id, sku, path FROM images WHERE path LIKE '%.png'"
    ).fetchall()

    if not rows:
        print("[FIX] No .png rows found — nothing to do.", flush=True)
        conn.close()
        return

    print(f"[FIX] Found {len(rows)} .png rows to fix.", flush=True)

    fixed = skipped = errors = 0

    for image_id, sku, png_path in rows:
        if not png_path or not os.path.exists(png_path):
            print(f"[FIX] SKIP {sku}: source file missing ({png_path})", flush=True)
            skipped += 1
            continue

        jpg_path = os.path.splitext(png_path)[0] + ".jpg"

        try:
            # Convert PNG → JPEG (mirrors normalize_uploaded_image in app.py)
            with Image.open(png_path) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode != "RGB":
                    im = im.convert("RGB")
                im.save(jpg_path, format="JPEG", quality=95, optimize=True)

            # Update DB row
            conn.execute(
                "UPDATE images SET path = ? WHERE image_id = ?",
                (jpg_path, image_id),
            )

            # Remove the old .png
            os.remove(png_path)

            print(f"[FIX] + {sku}: {os.path.basename(png_path)} → {os.path.basename(jpg_path)}", flush=True)
            fixed += 1

        except Exception as e:
            print(f"[FIX] ERROR {sku}: {e}", flush=True)
            errors += 1

    conn.commit()
    conn.close()

    print(f"\n[FIX] Done: {fixed} fixed, {skipped} skipped, {errors} errors", flush=True)


@app.local_entrypoint()
def main():
    fix_thumbnails.remote()
