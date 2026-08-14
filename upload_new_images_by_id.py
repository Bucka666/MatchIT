"""
upload_new_images_by_id.py — Push a specific, known list of local image_db
files to the Modal volume, by image_id.

Exists because smart_upload.py --images relies on .upload_state.json
fingerprints to know what's already on the volume, and that file has never
tracked any "image:cards:*" keys (0 entries, confirmed 2026-08 during the
One Piece vertical rollout) even though the volume's cards/image_db already
had 145,234 files — populated some other way (most likely: production admin
routes writing directly to the volume when running on Modal). A blanket
--images run would therefore try to re-upload all ~140k local files instead
of just the new ones. This script uploads only an explicit id list, reusing
smart_upload.py's existing, already-proven remote_upload_image_batch function
(same write + inline R2 upload) rather than reimplementing it.

After uploading, marks each id in .upload_state.json so smart_upload.py's own
incremental tracking is accurate going forward for these specific files. Does
NOT touch or backfill state for the pre-existing ~135,743 images — that's a
separate decision.

Run:
    python upload_new_images_by_id.py --ids-file path/to/ids.json --dry-run
    python upload_new_images_by_id.py --ids-file path/to/ids.json
"""

import argparse
import json
import os

from smart_upload import (
    LOCAL_BASE, remote_upload_image_batch, load_state, save_state,
    mark_uploaded, app, IMAGE_BATCH_SIZE,
)

VERTICAL = "cards"


@app.local_entrypoint()
def upload_by_id(ids_file: str, dry_run: bool = False):
    ids = json.loads(open(ids_file, encoding="utf-8").read())
    image_dir = os.path.join(LOCAL_BASE, VERTICAL, "image_db")

    to_upload = []
    missing = []
    for image_id in ids:
        fname = image_id + ".jpg"
        fpath = os.path.join(image_dir, fname)
        if os.path.isfile(fpath):
            to_upload.append(fname)
        else:
            missing.append(fname)

    size = sum(os.path.getsize(os.path.join(image_dir, f)) for f in to_upload)
    print(f"[SCAN] {len(to_upload)} local files found for the {len(ids)} requested ids "
          f"({round(size / 1024 / 1024, 1)} MB), {len(missing)} missing locally")
    if missing:
        print(f"  missing sample: {missing[:5]}")

    if dry_run:
        print("[DRY-RUN] no upload performed")
        return

    state = load_state()
    uploaded = 0
    total = len(to_upload)
    for i in range(0, total, IMAGE_BATCH_SIZE):
        batch_names = to_upload[i:i + IMAGE_BATCH_SIZE]
        batch = []
        for fname in batch_names:
            fpath = os.path.join(image_dir, fname)
            with open(fpath, "rb") as f:
                batch.append((fname, f.read()))
        remote_upload_image_batch.remote(VERTICAL, batch)
        for fname in batch_names:
            fpath = os.path.join(image_dir, fname)
            mark_uploaded(state, f"image:{VERTICAL}:{fname}", fpath)
        uploaded += len(batch_names)
        save_state(state)
        print(f"  {uploaded}/{total} ({round(uploaded / total * 100)}%)")

    print(f"\nDone. Uploaded {uploaded} images.")
