"""
sync_cardsdb_images.py — Onboard new CardsDB images into local images.db.

Standalone equivalent of app.py's /admin/sync_keysdb route (the "cards"
vertical branch), runnable without a live Flask session/admin login. Imports
the same helpers app.py already exposes (_iter_cardsdb_images,
normalize_uploaded_image, load_embedding_cache, upload_to_r2) so there is
exactly one implementation of "how a CardsDB image becomes an images.db row"
— this does not reimplement that logic, just drives it from the CLI.

For each new image found under CardsDB (skipping anything whose
original_filename is already in images.db):
  1. assigns a UUID image_id
  2. copies it to the local image_db/ dir as {image_id}.jpg
  3. normalizes it (app.py's normalize_uploaded_image)
  4. uploads it to R2 (non-fatal — failures are logged, the DB row still
     gets written; r2_image_upload.py's planner can backfill any gaps later)
  5. INSERT OR REPLACE INTO images(...) with embedding=NULL
     (embeddings are generated separately by incremental_embed.py)

Run from the project root (needs app.py importable):
    python sync_cardsdb_images.py --dry-run          # preview only, no writes
    python sync_cardsdb_images.py --limit 3           # sync just 3 new images (validation)
    python sync_cardsdb_images.py                      # sync all new images (no cap)
"""

import argparse
import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from app import (
    init_db, get_images_db_path, get_image_db_dir,
    normalize_uploaded_image, load_embedding_cache, _iter_cardsdb_images,
)
from vertical_loader import get_db_root
from r2_util import upload_to_r2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="max new images to sync (0 = unlimited)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, no writes")
    parser.add_argument("--game", default="", help="only sync this CardsDB top-level folder (e.g. onepiece)")
    args = parser.parse_args()

    init_db()
    db_path = get_images_db_path()
    img_dir = get_image_db_dir()
    db_root = Path(get_db_root())

    print(f"images.db : {db_path}")
    print(f"image_db  : {img_dir}")
    print(f"CardsDB   : {db_root}\n")

    if not db_root.exists():
        print(f"[ERROR] DB root not found: {db_root}")
        return

    existing = set()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT original_filename FROM images WHERE original_filename IS NOT NULL"
        ).fetchall()
        for (orig,) in rows:
            if orig:
                existing.add(str(orig).strip().lower())

    to_process = []
    skipped_existing = 0
    skipped_bad = 0

    game_filter = args.game.strip().lower()
    skipped_other_game = 0

    for sku, src_path, rel_id in _iter_cardsdb_images(db_root):
        if not rel_id:
            skipped_bad += 1
            continue
        if game_filter and not rel_id.lower().startswith(game_filter + "/"):
            skipped_other_game += 1
            continue
        rel_key = rel_id.strip().lower()
        if rel_key in existing:
            skipped_existing += 1
            continue
        to_process.append((sku, src_path, rel_id))

    print(f"[SCAN] {len(to_process)} new images found, {skipped_existing} already present, {skipped_bad} bad"
          + (f", {skipped_other_game} outside --game {args.game!r}" if game_filter else ""))

    if args.limit > 0:
        to_process = to_process[:args.limit]
        print(f"[LIMIT] processing only {len(to_process)} (--limit {args.limit})")

    if args.dry_run:
        print("\n[DRY-RUN] sample of what would be synced:")
        for sku, src_path, rel_id in to_process[:10]:
            print(f"  {sku}  {src_path}  ->  {rel_id}")
        print(f"\n[DRY-RUN] {len(to_process)} would be inserted, 0 written (dry run)")
        return

    inserted = 0
    r2_failed = []
    insert_failed = []

    conn = sqlite3.connect(db_path)
    try:
        for sku, src_path, rel_id in to_process:
            try:
                image_id = str(uuid.uuid4())
                dst_path = os.path.join(img_dir, f"{image_id}.jpg")

                shutil.copy2(str(src_path), dst_path)
                normalize_uploaded_image(dst_path)
                r2_ok = upload_to_r2(image_id, dst_path)
                if not r2_ok:
                    r2_failed.append(sku)

                conn.execute(
                    """
                    INSERT OR REPLACE INTO images
                        (image_id, sku, description, original_filename, path, added_at, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (image_id, sku, "", rel_id, dst_path, datetime.utcnow().isoformat()),
                )
                inserted += 1
                print(f"  [OK] {sku} -> {image_id}.jpg" + ("" if r2_ok else "  (R2 upload failed, row inserted anyway)"))
            except Exception as e:
                insert_failed.append(f"{sku}: {e}")
                print(f"  [FAIL] {sku} ({src_path}): {e}")

        conn.commit()
    finally:
        conn.close()

    load_embedding_cache(force=True)

    print(f"\nSync complete. Inserted={inserted}, skipped_existing={skipped_existing}, skipped_bad={skipped_bad}")
    if r2_failed:
        print(f"R2 upload failures ({len(r2_failed)}, rows still inserted, backfillable via r2_image_upload.py): {r2_failed[:10]}")
    if insert_failed:
        print(f"Insert failures ({len(insert_failed)}): {insert_failed[:10]}")


if __name__ == "__main__":
    main()
