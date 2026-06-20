"""
r2_util.py — shared R2 upload helper for the image_db writers.

Non-fatal by design: every failure mode (missing creds, missing boto3,
upload error) logs and returns False. The caller's local/volume write is
always the source of truth — R2 is an additive mirror, never a dependency.
"""

import os

_client = None
_creds_warned = False


def _get_client():
    global _client, _creds_warned
    if _client is not None:
        return _client
    try:
        import boto3
        from botocore.config import Config

        cfg = Config(
            max_pool_connections=8,
            retries={"max_attempts": 5, "mode": "adaptive"},
            region_name="auto",
        )
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=cfg,
        )
        return _client
    except Exception as e:
        if not _creds_warned:
            print(f"[R2-UPLOAD] r2 creds unavailable: {e}", flush=True)
            _creds_warned = True
        return None


def upload_to_r2(image_id, local_path):
    key = image_id + ".jpg"
    client = _get_client()
    if client is None:
        return False
    try:
        client.upload_file(local_path, os.environ["R2_BUCKET"], key, ExtraArgs={
            "ContentType": "image/jpeg",
            "CacheControl": "public, max-age=31536000, immutable",
        })
        return True
    except Exception as e:
        print(f"[R2-UPLOAD] FAILED {key}: {e}", flush=True)
        return False
