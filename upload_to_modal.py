"""
upload_to_modal.py — One-shot full upload of a vertical's images.db +
npy_cache + image_db to the Modal volume.

⚠ WARNING — 2026-08-10 ⚠
This script has the identical unconditional-overwrite shape as the
smart_upload.py incident from the same night (see the banner at the top of
that file): it reads the local images.db and calls upload_vertical.remote(),
which writes it straight over the volume's copy with no check on what's
already there. This script was not the one run during the incident, but the
same stale-local-file mistake here would have the same effect. Guarded the
same way: aborts before uploading if the local row count is lower than the
volume's current row count. Do not remove this guard to "make a run
simpler".

NOTE: VOLUME_NAME below is "matchit-data" — as of 2026-08-10 no such volume
exists (`modal volume list` shows only "matchit-data-v2"). Until this is
fixed, running this script will error out (or create/write to an unrelated
empty volume, depending on SDK create-if-missing behaviour) rather than
touch production data.
"""

import modal
import os
import sqlite3

VOLUME_NAME = "matchit-data"
LOCAL_BASE = "C:/Users/c_a_b/AppData/Local/MatchITv2_ProductMatch_Data"

vol = modal.Volume.from_name(VOLUME_NAME)
app = modal.App("matchit-upload")


def _local_row_count(db_path):
    """COUNT(*) FROM images in a local images.db, or None if it can't be
    read (missing/corrupt/mid-write) -- treated as unknown, not zero, so a
    read failure can't masquerade as an empty database in the guard below."""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return None


@app.function(volumes={"/data": vol}, timeout=300)
def remote_get_row_count(vertical_id):
    """Row count of the volume's CURRENT images.db for vertical_id, or None
    if it doesn't exist yet (first-ever upload -- nothing to compare against)."""
    db_path = "/data/MatchITv2_ProductMatch_Data/" + vertical_id + "/images.db"
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    finally:
        conn.close()


@app.function(volumes={"/data": vol}, timeout=86400)
def upload_vertical(vertical_id, db_bytes, cache_files):
    base = "/data/MatchITv2_ProductMatch_Data/" + vertical_id
    os.makedirs(base, exist_ok=True)

    if len(db_bytes) > 0:
        with open(os.path.join(base, "images.db"), "wb") as f:
            f.write(db_bytes)
        print("Saved images.db for " + vertical_id)

    cache_dir = os.path.join(base, "npy_cache")
    os.makedirs(cache_dir, exist_ok=True)
    for name, data in cache_files.items():
        with open(os.path.join(cache_dir, name), "wb") as f:
            f.write(data)
        print("Saved npy_cache/" + name)

    vol.commit()
    print("Upload complete for " + vertical_id + "!")


@app.function(volumes={"/data": vol}, timeout=86400)
def upload_image_batch(vertical_id, batch):
    img_dir = "/data/MatchITv2_ProductMatch_Data/" + vertical_id + "/image_db"
    os.makedirs(img_dir, exist_ok=True)
    count = 0
    for fname, img_bytes in batch:
        with open(os.path.join(img_dir, fname), "wb") as f:
            f.write(img_bytes)
        count += 1
    vol.commit()
    return count


@app.function(volumes={"/data": vol}, timeout=86400)
def check_volume():
    print("Volume contents:")
    for root, dirs, files in os.walk("/data"):
        level = root.replace("/data", "").count(os.sep)
        if level > 2:
            continue
        indent = "  " * level
        print(indent + os.path.basename(root) + "/")
        subindent = "  " * (level + 1)
        for f in files[:10]:
            size = os.path.getsize(os.path.join(root, f))
            if size > 1024 * 1024:
                print(subindent + f + "  (" + str(round(size/1024/1024, 1)) + " MB)")
            else:
                print(subindent + f + "  (" + str(round(size/1024, 1)) + " KB)")
        if len(files) > 10:
            print(subindent + "... and " + str(len(files) - 10) + " more files")


@app.local_entrypoint()
def main(vertical: str = "cards", images: bool = False, check: bool = False):
    if check:
        check_volume.remote()
        return

    vertical_dir = os.path.join(LOCAL_BASE, vertical)
    if not os.path.exists(vertical_dir):
        print("ERROR: " + vertical_dir + " not found!")
        return

    print("Uploading vertical: " + vertical)

    db_path = os.path.join(vertical_dir, "images.db")
    if not os.path.exists(db_path):
        print("ERROR: images.db not found!")
        return

    # GUARD, added 2026-08-10: refuse to overwrite the volume's images.db
    # with a local copy that has fewer rows -- see the warning banner at the
    # top of this file. Runs before the file is even read into memory.
    local_count = _local_row_count(db_path)
    volume_count = remote_get_row_count.remote(vertical)
    if local_count is not None and volume_count is not None and local_count < volume_count:
        print("ABORT: local images.db has " + str(local_count) + " rows, volume currently "
              "has " + str(volume_count) + " rows -- local file looks stale, refusing to "
              "upload. See the warning banner at the top of this file. If this drop is "
              "genuinely intended, investigate and confirm before re-running -- do not "
              "just force past this check.")
        return

    print("Reading and uploading images.db...")
    with open(db_path, "rb") as f:
        db_bytes = f.read()
    upload_vertical.remote(vertical, db_bytes, {})
    del db_bytes
    print("DB uploaded!")

    cache_dir = os.path.join(vertical_dir, "npy_cache")
    if os.path.exists(cache_dir):
        cache_files = {}
        for fname in os.listdir(cache_dir):
            fpath = os.path.join(cache_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    cache_files[fname] = f.read()
                print("Read " + fname)
        if cache_files:
            print("Uploading cache...")
            upload_vertical.remote(vertical, b"", cache_files)
            print("Cache uploaded!")

    if images:
        img_dir = os.path.join(vertical_dir, "image_db")
        if not os.path.exists(img_dir):
            print("ERROR: image_db dir not found: " + img_dir)
        else:
            all_files = [f for f in os.listdir(img_dir) if os.path.isfile(os.path.join(img_dir, f))]
            print("Uploading " + str(len(all_files)) + " images in batches...")
            BATCH = 500
            for i in range(0, len(all_files), BATCH):
                batch = []
                for fname in all_files[i:i + BATCH]:
                    with open(os.path.join(img_dir, fname), "rb") as f:
                        batch.append((fname, f.read()))
                count = upload_image_batch.remote(vertical, batch)
                print("Batch " + str(i // BATCH + 1) + ": uploaded " + str(count) + " images")
            print("Images uploaded!")

    print("All done!")