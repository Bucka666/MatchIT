"""
backfill_npy_to_sqlite.py — Copy .npy embeddings back into images.db
=====================================================================
Fixes rows where the embedding column is NULL but the card is already
in front_matrix.npy. This happens when cards were embedded in a previous
run before incremental_embed.py started writing to SQLite.

Reads front_matrix.npy and cache_data.json (read-only), writes only to
images.db.

Usage:
    modal run backfill_npy_to_sqlite.py
"""

import os
import sys
sys.path.insert(0, "/app")

import modal
from modal_config import image, vol

app = modal.App("grailsweep-backfill-npy-to-sqlite")


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
    ],
    timeout=600,
)
def backfill_npy_to_sqlite():
    import json
    import sqlite3
    from pathlib import Path
    import numpy as np

    os.environ["LOCALAPPDATA"] = "/modal_data"

    data_dir  = Path("/modal_data/MatchITv2_ProductMatch_Data/cards")
    cache_dir = data_dir / "npy_cache"
    db_path   = data_dir / "images.db"

    # ── Load .npy cache ──────────────────────────────────────────
    front_npy  = cache_dir / "front_matrix.npy"
    cache_json = cache_dir / "cache_data.json"

    if not front_npy.exists():
        print(f"[BACKFILL-SQLITE] front_matrix.npy not found at {front_npy}", flush=True)
        return
    if not cache_json.exists():
        print(f"[BACKFILL-SQLITE] cache_data.json not found at {cache_json}", flush=True)
        return
    if not db_path.exists():
        print(f"[BACKFILL-SQLITE] images.db not found at {db_path}", flush=True)
        return

    front_matrix = np.load(str(front_npy))  # shape (N, dim)

    with open(cache_json, "r", encoding="utf-8") as f:
        cache_data = json.load(f)

    # front_info rows: [image_id, sku, original_filename, description]
    front_info = cache_data.get("front_info", [])

    if len(front_info) != front_matrix.shape[0]:
        print(
            f"[BACKFILL-SQLITE] Mismatch: {len(front_info)} front_info entries "
            f"vs {front_matrix.shape[0]} .npy rows — aborting",
            flush=True,
        )
        return

    # Build image_id → row index map
    id_to_idx = {row[0]: i for i, row in enumerate(front_info)}
    id_to_sku = {row[0]: row[1] for row in front_info}

    # ── Find NULL rows in SQLite ──────────────────────────────────
    conn = sqlite3.connect(str(db_path))
    null_rows = conn.execute(
        "SELECT image_id FROM images WHERE embedding IS NULL"
    ).fetchall()

    null_ids = [r[0] for r in null_rows]
    print(f"[BACKFILL-SQLITE] Found {len(null_ids)} SQLite rows with NULL embedding", flush=True)

    if not null_ids:
        print("[BACKFILL-SQLITE] Nothing to do.", flush=True)
        conn.close()
        return

    # ── Write embeddings ──────────────────────────────────────────
    written = skipped = 0

    for image_id in null_ids:
        idx = id_to_idx.get(image_id)
        if idx is None:
            print(
                f"[BACKFILL-SQLITE] SKIP {image_id}: not found in .npy cache",
                flush=True,
            )
            skipped += 1
            continue

        vec  = front_matrix[idx].astype(np.float32)
        blob = vec.tobytes()
        sku  = id_to_sku.get(image_id, image_id)

        conn.execute(
            "UPDATE images SET embedding = ? WHERE image_id = ?",
            (blob, image_id),
        )
        written += 1
        print(f"[BACKFILL-SQLITE] Wrote embedding for: {sku}", flush=True)

    conn.commit()
    conn.close()

    print(
        f"[BACKFILL-SQLITE] Done — {written} SQLite rows updated, "
        f"{skipped} skipped (not in .npy)",
        flush=True,
    )


@app.local_entrypoint()
def main():
    backfill_npy_to_sqlite.remote()
