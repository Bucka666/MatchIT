"""
migrate_volume.py — Migrate MatchIT data from Modal Volume v1 to v2
====================================================================
Run this ONCE to copy all data from matchit-data (v1) to matchit-data-v2 (v2).

Steps:
    1. Run: modal volume create --version=2 matchit-data-v2
    2. Run: modal run migrate_volume.py
    3. Verify data in matchit-data-v2 via Modal dashboard
    4. Update matchit_modal.py to use matchit-data-v2
    5. modal deploy matchit_modal.py
    6. Delete old volume: modal volume delete matchit-data
"""

import modal

# Both volumes mounted simultaneously
vol_v1 = modal.Volume.from_name("matchit-data")
vol_v2 = modal.Volume.from_name("matchit-data-v2", version=2)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .run_commands("apt-get install -y rsync")
)

app = modal.App("matchit-migrate")


@app.function(
    image=image,
    volumes={
        "/v1": vol_v1,
        "/v2": vol_v2,
    },
    timeout=86400,  # 24 hours — 30GB may take a while
    memory=4096,
    retries=3,
)
def migrate():
    import subprocess
    import os

    print("[MIGRATE] Starting migration from v1 → v2...", flush=True)

    vol_v1.reload()
    print("[MIGRATE] v1 volume reloaded", flush=True)

    import subprocess
    # Check what's actually mounted
    result = subprocess.run(['ls', '-la', '/'], capture_output=True, text=True)
    print(f"[MIGRATE] Root contents: {result.stdout}", flush=True)
    result = subprocess.run(['ls', '-la', '/v1'], capture_output=True, text=True)
    print(f"[MIGRATE] /v1 contents: {result.stdout}", flush=True)
    result = subprocess.run(['df', '-h'], capture_output=True, text=True)
    print(f"[MIGRATE] Disk info: {result.stdout}", flush=True)

    # Check source
    result = subprocess.run(
        ["du", "-sh", "/v1"],
        capture_output=True, text=True
    )
    print(f"[MIGRATE] Source size: {result.stdout.strip()}", flush=True)

    # Count files
    result = subprocess.run(
        ["find", "/v1", "-type", "f"],
        capture_output=True, text=True
    )
    file_count = len(result.stdout.strip().splitlines())
    print(f"[MIGRATE] Source files: {file_count:,}", flush=True)

    # rsync v1 → v2
    # -a = archive (preserves permissions, timestamps etc)
    # -v = verbose
    # --progress = show progress
    # --stats = show summary stats at end
    print("[MIGRATE] Starting rsync...", flush=True)

    # Use the resolved path
    import os
    v1_real = os.path.realpath("/v1")
    v2_real = os.path.realpath("/v2")
    print(f"[MIGRATE] v1 real path: {v1_real}", flush=True)
    print(f"[MIGRATE] v2 real path: {v2_real}", flush=True)
    result = subprocess.run(['ls', '-la', v1_real], capture_output=True, text=True)
    print(f"[MIGRATE] v1 real contents: {result.stdout[:500]}", flush=True)

    result = subprocess.run(
        [
            "rsync", "-av",
            "--stats",
            "--human-readable",
            v1_real + "/",
            v2_real + "/",
        ],
        text=True,
    )

    if result.returncode != 0:
        print(f"[MIGRATE] rsync failed with code {result.returncode}", flush=True)
        raise RuntimeError("rsync failed")

    # Verify destination
    result = subprocess.run(
        ["du", "-sh", "/v2"],
        capture_output=True, text=True
    )
    print(f"[MIGRATE] Destination size: {result.stdout.strip()}", flush=True)

    result = subprocess.run(
        ["find", "/v2", "-type", "f"],
        capture_output=True, text=True
    )
    v2_count = len(result.stdout.strip().splitlines())
    print(f"[MIGRATE] Destination files: {v2_count:,}", flush=True)

    # Commit v2
    vol_v2.commit()
    print("[MIGRATE] Volume v2 committed ✅", flush=True)
    print(f"[MIGRATE] Migration complete — {v2_count:,} files copied", flush=True)


@app.local_entrypoint()
def main():
    migrate.remote()