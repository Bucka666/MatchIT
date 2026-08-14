"""
One-off: rebuild a clean images.db from the corrupted volume copy.

Diagnosis (see conversation): PRAGMA integrity_check found "Rowid 135282 out
of order" on one page of the images table, plus a similar single-row dupe in
match_history (id=241). Cross-checked via three independent read strategies
(natural scan, ORDER BY image_id, ORDER BY sku) -- all three return the exact
same 140,537 distinct image_ids with zero set difference, confirming this is
duplicate-read corruption (a bad page pointer causing re-traversal), not data
loss. A plain natural scan already captures every row correctly; this script
dedupes via Python dicts keyed by primary key rather than relying on SQL
DISTINCT/GROUP BY against the damaged structure.

Also recovers match_feedback (clean, no discrepancy found) and match_history
(1 duplicate row, same class of issue) so the rebuild doesn't silently drop
real user feedback/history data that wasn't part of the One Piece work.

Then merges in the local images.db's ~4,672 One Piece rows (fresh UUIDs, no
collision possible) so the output is the volume's true state + the new
vertical, not the stale local-only merge that got corrupted earlier via a
raw file copy.

Output: C:\\MatchIT\\images_rebuilt.db -- NOT uploaded anywhere by this script.
"""

import sqlite3

CORRUPT_PATH = r"C:\MatchIT\images_volume_corrupt.db"
LOCAL_PATH = r"C:\Users\c_a_b\AppData\Local\MatchITv2_ProductMatch_Data\cards\images.db"
OUTPUT_PATH = r"C:\MatchIT\images_rebuilt.db"

IMAGES_SCHEMA = """
CREATE TABLE images(
                image_id TEXT PRIMARY KEY,
                sku TEXT,
                original_filename TEXT,
                path TEXT,
                added_at TEXT,
                embedding BLOB
            , description TEXT, flagged INTEGER DEFAULT 0)
"""


def main():
    corrupt = sqlite3.connect(CORRUPT_PATH)

    # ── images: natural scan already proven complete (see docstring) ──
    print("Reading images from corrupted volume copy (natural scan)...")
    images_by_id = {}
    cols = ["image_id", "sku", "original_filename", "path", "added_at", "embedding", "description", "flagged"]
    col_list = ", ".join(cols)
    for row in corrupt.execute(f"SELECT {col_list} FROM images"):
        images_by_id[row[0]] = row
    print(f"  recovered {len(images_by_id)} distinct rows from volume")

    # ── match_feedback: no discrepancy found, straightforward read ──
    fb_schema_row = corrupt.execute(
        "SELECT sql FROM sqlite_master WHERE name='match_feedback'"
    ).fetchone()
    fb_cols_info = corrupt.execute("PRAGMA table_info(match_feedback)").fetchall()
    fb_cols = [c[1] for c in fb_cols_info]
    fb_by_id = {}
    for row in corrupt.execute(f"SELECT {', '.join(fb_cols)} FROM match_feedback"):
        fb_by_id[row[0]] = row
    print(f"  recovered {len(fb_by_id)} distinct rows from match_feedback")

    # ── match_history: dedupe by id (Python dict), same corruption class ──
    hist_cols_info = corrupt.execute("PRAGMA table_info(match_history)").fetchall()
    hist_cols = [c[1] for c in hist_cols_info]
    hist_by_id = {}
    for row in corrupt.execute(f"SELECT {', '.join(hist_cols)} FROM match_history"):
        hist_by_id[row[0]] = row
    print(f"  recovered {len(hist_by_id)} distinct rows from match_history")

    corrupt.close()

    # ── merge in local's One Piece rows ──
    print("\nMerging local One Piece rows...")
    local = sqlite3.connect(LOCAL_PATH)
    op_rows = local.execute(
        f"SELECT {col_list} FROM images WHERE sku LIKE 'op-%'"
    ).fetchall()
    local.close()
    print(f"  found {len(op_rows)} One Piece rows locally")

    collisions = 0
    for row in op_rows:
        if row[0] in images_by_id:
            collisions += 1
        images_by_id[row[0]] = row
    print(f"  collisions with existing volume rows: {collisions} (expected 0 -- fresh UUIDs)")

    # ── build fresh output db ──
    print(f"\nBuilding fresh database at {OUTPUT_PATH}...")
    import os
    if os.path.exists(OUTPUT_PATH):
        os.remove(OUTPUT_PATH)
    out = sqlite3.connect(OUTPUT_PATH)
    out.execute(IMAGES_SCHEMA)
    out.execute("CREATE INDEX idx_images_sku ON images(sku)")
    out.execute("CREATE INDEX idx_images_added_at ON images(added_at)")

    out.execute(fb_schema_row[0])
    out.execute("CREATE INDEX idx_feedback_sku ON match_feedback(confirmed_sku)")
    out.execute("CREATE INDEX idx_feedback_verdict ON match_feedback(verdict)")

    hist_schema_row_conn = sqlite3.connect(CORRUPT_PATH)
    hist_schema = hist_schema_row_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='match_history'"
    ).fetchone()[0]
    hist_schema_row_conn.close()
    out.execute(hist_schema)
    out.execute("CREATE INDEX idx_history_at ON match_history(matched_at)")

    print("Inserting images...")
    placeholders = ",".join(["?"] * len(cols))
    out.executemany(f"INSERT INTO images ({col_list}) VALUES ({placeholders})", images_by_id.values())

    print("Inserting match_feedback...")
    fb_placeholders = ",".join(["?"] * len(fb_cols))
    out.executemany(f"INSERT INTO match_feedback ({', '.join(fb_cols)}) VALUES ({fb_placeholders})", fb_by_id.values())

    print("Inserting match_history...")
    hist_placeholders = ",".join(["?"] * len(hist_cols))
    out.executemany(f"INSERT INTO match_history ({', '.join(hist_cols)}) VALUES ({hist_placeholders})", hist_by_id.values())

    out.commit()

    # sqlite_sequence should self-populate correctly from AUTOINCREMENT inserts;
    # verify explicitly rather than assume.
    for tbl in ("match_feedback", "match_history"):
        max_id = out.execute(f"SELECT MAX(id) FROM {tbl}").fetchone()[0]
        seq = out.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (tbl,)).fetchone()
        print(f"  {tbl}: MAX(id)={max_id}, sqlite_sequence.seq={seq[0] if seq else None}")

    out.close()
    print("\nDone. Run verification separately -- this script does not check integrity or upload anything.")


if __name__ == "__main__":
    main()
