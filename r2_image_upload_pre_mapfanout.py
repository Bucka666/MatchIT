"""
r2_image_upload.py — Bulk-upload card images from the Modal volume to R2
=========================================================================
Standalone, CPU-only Modal function. Does NOT touch the live matchit-api
app — separate modal.App, run manually via `modal run`, never `modal deploy`.

Usage:
    modal run r2_image_upload.py::bulk_upload --no-dry-run
    modal run r2_image_upload.py::bulk_upload --no-dry-run --workers 128
"""

import re

import modal

VOLUME_NAME = "matchit-data-v2"
IMAGE_DB_DIR = "/modal_data/MatchITv2_ProductMatch_Data/cards/image_db"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.jpg$"
)
PROGRESS_EVERY = 2000

app = modal.App("grailsweep-r2-upload")

vol = modal.Volume.from_name(VOLUME_NAME, version=2)

image = modal.Image.debian_slim(python_version="3.11").pip_install("boto3")


def _r2_client(workers):
    import os

    import boto3
    from botocore.config import Config

    cfg = Config(
        max_pool_connections=workers,
        retries={"max_attempts": 5, "mode": "adaptive"},
        region_name="auto",
    )
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=cfg,
    )


def _list_existing_keys(client, bucket):
    keys = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[modal.Secret.from_name("r2-credentials")],
    timeout=10800,
)
def bulk_upload(dry_run: bool = True, workers: int = 128):
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed

    bucket = os.environ["R2_BUCKET"]
    client = _r2_client(workers)

    all_files = os.listdir(IMAGE_DB_DIR)
    valid_keys = set()
    bad_files = []
    for fname in all_files:
        if UUID_RE.match(fname):
            valid_keys.add(fname)
        else:
            bad_files.append(fname)

    print(f"[SCAN] local files: {len(all_files)} total, {len(valid_keys)} valid, {len(bad_files)} skipped-bad", flush=True)
    if bad_files:
        print(f"[SCAN] skipped-bad examples: {bad_files[:10]}", flush=True)

    print("[LIST] listing existing objects in R2 bucket...", flush=True)
    existing_keys = _list_existing_keys(client, bucket)
    print(f"[LIST] existing in R2: {len(existing_keys)}", flush=True)

    to_upload = sorted(valid_keys - existing_keys)
    already_present = len(valid_keys) - len(to_upload)

    print(
        f"[PLAN] valid={len(valid_keys)} already_present={already_present} "
        f"to_upload={len(to_upload)} skipped_bad={len(bad_files)}",
        flush=True,
    )

    if dry_run:
        print("[DRY-RUN] no bytes uploaded, exiting.", flush=True)
        return

    extra_args = {
        "ContentType": "image/jpeg",
        "CacheControl": "public, max-age=31536000, immutable",
    }

    failed_keys = []
    uploaded = 0

    def _upload_one(key):
        path = os.path.join(IMAGE_DB_DIR, key)
        client.upload_file(path, bucket, key, ExtraArgs=extra_args)
        return key

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_upload_one, key): key for key in to_upload}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                fut.result()
                uploaded += 1
            except Exception as e:
                failed_keys.append(key)
                print(f"[UPLOAD-FAIL] {key}: {e}", flush=True)
            if uploaded % PROGRESS_EVERY == 0 and uploaded > 0:
                print(f"[PROGRESS] uploaded {uploaded} / {len(to_upload)}", flush=True)

    print("[LIST] re-listing R2 bucket for final count...", flush=True)
    final_keys = _list_existing_keys(client, bucket)
    final_count = len(final_keys)

    pass_fail = "PASS" if final_count == len(valid_keys) else "FAIL"

    print("=" * 60, flush=True)
    print("[SUMMARY]", flush=True)
    print(f"  valid local files:      {len(valid_keys)}", flush=True)
    print(f"  already present (skip): {already_present}", flush=True)
    print(f"  uploaded this run:      {uploaded}", flush=True)
    print(f"  bad files skipped:      {len(bad_files)} {bad_files[:10]}", flush=True)
    print(f"  failed uploads:         {len(failed_keys)} {failed_keys[:10]}", flush=True)
    print(f"  final R2 object count:  {final_count}", flush=True)
    print(f"  RESULT: {pass_fail} (final_count == len(valid_keys): {final_count} == {len(valid_keys)})", flush=True)
    print("=" * 60, flush=True)
