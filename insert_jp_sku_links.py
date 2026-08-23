"""
insert_jp_sku_links.py — Register the confirmed JP SWSH SKU->image links
(jp_sku_links.json) into images.db, embedding left NULL.

jp_sku_links.json maps sku -> source jpg path (e.g. "jpn-s12a-001" ->
"C:/CardsDB/jpn-s12a/042262.jpg"). Those source images are raw cardID-named
scrape files, not the standard CardsDB/{game}/{sku}/front.png layout that
_iter_cardsdb_images() / sync_cardsdb_images.py expect, so they can't be
picked up by that walker. This script mirrors its insert step (uuid
image_id, copy into image_db/, normalize, upload_to_r2 non-fatal,
INSERT OR REPLACE INTO images) but sources (sku, path) pairs from
jp_sku_links.json directly.

This DB has no view/category column -- every row is a FRONT entry
(see incremental_embed.py). Only SKUs with no existing images.db row are
inserted; everything else is skipped silently. No existing row is ever
modified.

Embedding is left NULL here -- run incremental_embed.py afterwards to do
the actual CLIP embedding pass and append to the npy cache.

Usage:
    python insert_jp_sku_links.py [--dry-run] [--limit N]
"""
import argparse
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime

from app import init_db, get_images_db_path, get_image_db_dir, normalize_uploaded_image
from r2_util import upload_to_r2

LINKS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jp_sku_links.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="preview only, no writes")
    parser.add_argument("--limit", type=int, default=0, help="max new rows to insert (0 = unlimited)")
    args = parser.parse_args()

    with open(LINKS_PATH, encoding="utf-8") as f:
        links = json.load(f)  # sku -> source image path

    init_db()
    db_path = get_images_db_path()
    img_dir = get_image_db_dir()
    print(f"images.db : {db_path}")
    print(f"image_db  : {img_dir}")
    print(f"links file: {LINKS_PATH} ({len(links)} entries)\n")

    conn = sqlite3.connect(db_path)
    existing_skus = {row[0] for row in conn.execute("SELECT DISTINCT sku FROM images")}

    to_process = []
    skipped_existing = 0
    skipped_missing = 0
    for sku, src_path in sorted(links.items()):
        if sku in existing_skus:
            skipped_existing += 1
            continue
        if not os.path.exists(src_path):
            skipped_missing += 1
            print(f"  [MISSING] {sku}: {src_path}")
            continue
        to_process.append((sku, src_path))

    print(f"[SCAN] {len(links)} total, {skipped_existing} already have a FRONT entry, "
          f"{skipped_missing} missing source file, {len(to_process)} to insert")

    if args.limit > 0:
        to_process = to_process[:args.limit]
        print(f"[LIMIT] processing only {len(to_process)} (--limit {args.limit})")

    if args.dry_run:
        print("\n[DRY-RUN] sample of what would be inserted:")
        for sku, src_path in to_process[:10]:
            print(f"  {sku}  <-  {src_path}")
        print(f"\n[DRY-RUN] {len(to_process)} would be inserted, 0 written (dry run)")
        conn.close()
        return

    inserted = 0
    r2_failed = []
    failed = []

    for sku, src_path in to_process:
        try:
            image_id = str(uuid.uuid4())
            dst_path = os.path.join(img_dir, f"{image_id}.jpg")

            shutil.copy2(src_path, dst_path)
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
                (image_id, sku, "", os.path.basename(src_path), dst_path, datetime.utcnow().isoformat()),
            )
            existing_skus.add(sku)
            inserted += 1
            if inserted % 100 == 0:
                conn.commit()
                print(f"  ... {inserted}/{len(to_process)}")
        except Exception as e:
            failed.append(f"{sku}: {e}")
            print(f"  [FAIL] {sku} ({src_path}): {e}")

    conn.commit()
    conn.close()

    print(f"\nInsert complete. Inserted={inserted}, skipped_existing={skipped_existing}, skipped_missing={skipped_missing}")
    if r2_failed:
        print(f"R2 upload failures ({len(r2_failed)}, rows still inserted, backfillable via r2_image_upload.py): {r2_failed[:10]}")
    if failed:
        print(f"Insert failures ({len(failed)}): {failed[:10]}")


if __name__ == "__main__":
    main()
