"""
One-shot purge of old scan images from /modal_data/query on the Modal volume.

Dry run (default — no deletions):
    modal run purge_query_images.py

Actually delete:
    modal run purge_query_images.py -- --commit
"""
import modal
import os
import time

QUERY_IMAGE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

_vol = modal.Volume.from_name("matchit-data-v2", version=2)
_app = modal.App("purge-query-images")


@_app.function(
    volumes={"/modal_data": _vol},
    timeout=300,
)
def purge_query_images(commit: bool = False):
    query_dir = "/modal_data/query"
    if not os.path.isdir(query_dir):
        print("query dir not found — nothing to do")
        return

    now = time.time()
    cutoff = now - QUERY_IMAGE_TTL_SECONDS

    all_files = []
    for fname in sorted(os.listdir(query_dir)):
        fpath = os.path.join(query_dir, fname)
        try:
            st = os.stat(fpath)
            all_files.append((fname, fpath, st.st_mtime, st.st_size))
        except Exception:
            pass

    eligible = [(f, p, m, s) for f, p, m, s in all_files if m < cutoff]
    retained = [(f, p, m, s) for f, p, m, s in all_files if m >= cutoff]

    total_bytes = sum(s for _, _, _, s in all_files)
    eligible_bytes = sum(s for _, _, _, s in eligible)

    print("--- Query Image Purge Report ---")
    print(f"Total files:           {len(all_files):>6}  ({total_bytes / (1024*1024):.1f} MB)")
    print(f"Eligible (>30 days):   {len(eligible):>6}  ({eligible_bytes / (1024*1024):.1f} MB)")
    print(f"Retained (<= 30 days): {len(retained):>6}")
    print(f"Mode:                  {'COMMIT — deleting now' if commit else 'DRY RUN (pass --commit to delete)'}")

    if not commit:
        if eligible:
            print("\nOldest eligible files:")
            for fname, _, mtime, size in eligible[:5]:
                age_days = (now - mtime) / 86400
                print(f"  {fname}  ({age_days:.0f}d old, {size/1024:.0f} KB)")
            if len(eligible) > 5:
                print(f"  ... and {len(eligible) - 5} more")
        print("\nNo changes made.")
        return

    deleted, errors = 0, 0
    for fname, fpath, _, _ in eligible:
        try:
            os.remove(fpath)
            deleted += 1
        except Exception as e:
            print(f"  ERROR deleting {fname}: {e}")
            errors += 1

    try:
        _vol.commit()
        print(f"\nDeleted {deleted} files, freed {eligible_bytes / (1024*1024):.1f} MB. vol.commit() OK.")
    except Exception as e:
        print(f"\nDeleted {deleted} files but vol.commit() FAILED: {e}")
    if errors:
        print(f"{errors} deletion errors (see above).")


@_app.local_entrypoint()
def main(commit: bool = False):
    purge_query_images.remote(commit=commit)
