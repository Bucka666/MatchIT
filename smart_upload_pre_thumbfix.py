"""
smart_upload.py — Incremental upload to Modal volume.

Combines upload_to_modal.py + upload_profiles.py + thumbnail images.
Tracks what was already uploaded in .upload_state.json so only new/changed
files are sent on subsequent runs.

Usage examples:
  modal run smart_upload.py                            # DB + profiles (all verticals)
  modal run smart_upload.py --images                  # also upload image_db images
  modal run smart_upload.py --thumbnails              # also upload CardsDB thumbnails
  modal run smart_upload.py --all-data                # everything
  modal run smart_upload.py --vertical cards          # one vertical only
  modal run smart_upload.py --dry-run                 # preview without uploading
  modal run smart_upload.py --reset                   # clear state, re-upload everything
  modal run smart_upload.py --check                   # show volume contents
"""

import modal
import os
import json

# ── Config ────────────────────────────────────────────────────────────────────

VOLUME_NAME = "matchit-data-v2"
LOCAL_BASE = "C:/Users/c_a_b/AppData/Local/MatchITv2_ProductMatch_Data"
LOCAL_CARDSDB = "C:/CardsDB"
LOCAL_SETMETA       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "set_metadata.json")
LOCAL_SKUMAP        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sku_game_map.json")
LOCAL_IDLOOKUP      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identifier_lookup.json")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".upload_state.json")

THUMBNAIL_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_BATCH_SIZE = 200
PROFILE_BATCH_SIZE = 500
THUMBNAIL_BATCH_SIZE = 300

# ── Modal setup ────────────────────────────────────────────────────────────────

vol = modal.Volume.from_name(VOLUME_NAME)
app = modal.App("matchit-smart-upload")

# ── State tracking ─────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def file_fingerprint(local_path):
    stat = os.stat(local_path)
    return {"size": stat.st_size, "mtime": round(stat.st_mtime, 1)}


def needs_upload(state, state_key, local_path):
    """Return True if file is new or changed since last upload."""
    stored = state.get(state_key)
    return stored != file_fingerprint(local_path)


def mark_uploaded(state, state_key, local_path):
    state[state_key] = file_fingerprint(local_path)


# ── Remote functions ───────────────────────────────────────────────────────────

@app.function(volumes={"/data": vol}, timeout=86400)
def remote_upload_db_and_cache(vertical_id, db_bytes, cache_files):
    base = "/data/MatchITv2_ProductMatch_Data/" + vertical_id
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "images.db"), "wb") as f:
        f.write(db_bytes)
    print("  saved images.db for " + vertical_id)
    cache_dir = os.path.join(base, "npy_cache")
    os.makedirs(cache_dir, exist_ok=True)
    for name, data in cache_files.items():
        with open(os.path.join(cache_dir, name), "wb") as f:
            f.write(data)
        print("  saved npy_cache/" + name)
    vol.commit()
    return True


@app.function(volumes={"/data": vol}, timeout=86400)
def remote_upload_image_batch(vertical_id, batch):
    img_dir = "/data/MatchITv2_ProductMatch_Data/" + vertical_id + "/image_db"
    os.makedirs(img_dir, exist_ok=True)
    for fname, data in batch:
        with open(os.path.join(img_dir, fname), "wb") as f:
            f.write(data)
    vol.commit()
    return len(batch)


@app.function(volumes={"/data": vol}, timeout=86400)
def remote_upload_profile_batch(batch):
    count = 0
    for rel_path, data in batch:
        full_path = "/data/CardsDB/" + rel_path
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(data)
        count += 1
    vol.commit()
    return count


@app.function(volumes={"/data": vol}, timeout=86400)
def remote_upload_thumbnail_batch(batch):
    count = 0
    for rel_path, data in batch:
        full_path = "/data/CardsDB/" + rel_path
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(data)
        count += 1
    vol.commit()
    return count


@app.function(volumes={"/data": vol}, timeout=86400)
def remote_upload_root_file(filename, data):
    with open("/data/" + filename, "wb") as f:
        f.write(data)
    print("  saved /" + filename + " (" + str(len(data)) + " bytes)")
    vol.commit()
    return True


@app.function(volumes={"/data": vol}, timeout=86400)
def remote_check_volume():
    print("Volume contents:")
    for root, dirs, files in os.walk("/data"):
        level = root.replace("/data", "").count(os.sep)
        if level > 3:
            continue
        indent = "  " * level
        print(indent + os.path.basename(root) + "/")
        subindent = "  " * (level + 1)
        for f in sorted(files)[:10]:
            size = os.path.getsize(os.path.join(root, f))
            if size >= 1024 * 1024:
                print(subindent + f + "  (" + str(round(size / 1024 / 1024, 1)) + " MB)")
            else:
                print(subindent + f + "  (" + str(round(size / 1024, 1)) + " KB)")
        if len(files) > 10:
            print(subindent + "... and " + str(len(files) - 10) + " more files")


# ── Upload helpers ─────────────────────────────────────────────────────────────

def upload_db_and_cache(vertical_id, state, dry_run):
    vertical_dir = os.path.join(LOCAL_BASE, vertical_id)
    if not os.path.isdir(vertical_dir):
        return

    db_path = os.path.join(vertical_dir, "images.db")
    cache_dir = os.path.join(vertical_dir, "npy_cache")

    db_key = "db:" + vertical_id + ":images.db"
    db_changed = os.path.exists(db_path) and needs_upload(state, db_key, db_path)

    cache_files_to_upload = {}
    if os.path.isdir(cache_dir):
        for fname in os.listdir(cache_dir):
            fpath = os.path.join(cache_dir, fname)
            if os.path.isfile(fpath):
                key = "cache:" + vertical_id + ":" + fname
                if needs_upload(state, key, fpath):
                    cache_files_to_upload[fname] = fpath

    if not db_changed and not cache_files_to_upload:
        print("[" + vertical_id + "] DB + cache: up to date, skipping")
        return

    if dry_run:
        if db_changed:
            mb = round(os.path.getsize(db_path) / 1024 / 1024, 1)
            print("[DRY RUN] would upload " + vertical_id + "/images.db (" + str(mb) + " MB)")
        for fname in cache_files_to_upload:
            kb = round(os.path.getsize(cache_files_to_upload[fname]) / 1024, 1)
            print("[DRY RUN] would upload " + vertical_id + "/npy_cache/" + fname + " (" + str(kb) + " KB)")
        return

    if not os.path.exists(db_path):
        print("[" + vertical_id + "] No images.db found, skipping")
        return

    print("[" + vertical_id + "] Uploading DB" + (" + " + str(len(cache_files_to_upload)) + " cache files" if cache_files_to_upload else "") + "...")
    with open(db_path, "rb") as f:
        db_bytes = f.read()
    cache_data = {}
    for fname, fpath in cache_files_to_upload.items():
        with open(fpath, "rb") as f:
            cache_data[fname] = f.read()

    remote_upload_db_and_cache.remote(vertical_id, db_bytes, cache_data)

    mark_uploaded(state, db_key, db_path)
    for fname, fpath in cache_files_to_upload.items():
        mark_uploaded(state, "cache:" + vertical_id + ":" + fname, fpath)
    save_state(state)
    print("[" + vertical_id + "] DB + cache done")


def upload_images(vertical_id, state, dry_run):
    image_dir = os.path.join(LOCAL_BASE, vertical_id, "image_db")
    if not os.path.isdir(image_dir):
        print("[" + vertical_id + "] No image_db folder, skipping images")
        return

    all_files = [f for f in os.listdir(image_dir) if os.path.isfile(os.path.join(image_dir, f))]
    to_upload = []
    for fname in all_files:
        fpath = os.path.join(image_dir, fname)
        key = "image:" + vertical_id + ":" + fname
        if needs_upload(state, key, fpath):
            to_upload.append(fname)

    if not to_upload:
        print("[" + vertical_id + "] Images: all " + str(len(all_files)) + " up to date, skipping")
        return

    skipped = len(all_files) - len(to_upload)
    total = len(to_upload)
    print("[" + vertical_id + "] Images: uploading " + str(total) + " new/changed" + (" (skipped " + str(skipped) + " unchanged)" if skipped else "") + "...")

    if dry_run:
        size = sum(os.path.getsize(os.path.join(image_dir, f)) for f in to_upload)
        print("[DRY RUN] would upload " + str(total) + " images (" + str(round(size / 1024 / 1024, 1)) + " MB)")
        return

    uploaded = 0
    for i in range(0, total, IMAGE_BATCH_SIZE):
        batch_names = to_upload[i:i + IMAGE_BATCH_SIZE]
        batch = []
        for fname in batch_names:
            fpath = os.path.join(image_dir, fname)
            with open(fpath, "rb") as f:
                batch.append((fname, f.read()))
        remote_upload_image_batch.remote(vertical_id, batch)
        for fname in batch_names:
            fpath = os.path.join(image_dir, fname)
            mark_uploaded(state, "image:" + vertical_id + ":" + fname, fpath)
        uploaded += len(batch_names)
        save_state(state)
        pct = round(uploaded / total * 100)
        print("  " + str(uploaded) + "/" + str(total) + " (" + str(pct) + "%)")

    print("[" + vertical_id + "] Images done")


def upload_profiles(state, dry_run):
    if not os.path.isdir(LOCAL_CARDSDB):
        print("CardsDB not found at " + LOCAL_CARDSDB)
        return

    all_profiles = []
    for root, dirs, files in os.walk(LOCAL_CARDSDB):
        for fname in files:
            if fname == "profile.json":
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, LOCAL_CARDSDB).replace("\\", "/")
                all_profiles.append((rel, full))

    to_upload = [(rel, full) for rel, full in all_profiles if needs_upload(state, "profile:" + rel, full)]

    skipped = len(all_profiles) - len(to_upload)
    total = len(to_upload)

    if not to_upload:
        print("[profiles] All " + str(len(all_profiles)) + " up to date, skipping")
        return

    print("[profiles] Uploading " + str(total) + " new/changed" + (" (skipped " + str(skipped) + " unchanged)" if skipped else "") + "...")

    if dry_run:
        size = sum(os.path.getsize(full) for _, full in to_upload)
        print("[DRY RUN] would upload " + str(total) + " profiles (" + str(round(size / 1024 / 1024, 1)) + " MB)")
        return

    uploaded = 0
    for i in range(0, total, PROFILE_BATCH_SIZE):
        chunk = to_upload[i:i + PROFILE_BATCH_SIZE]
        batch = []
        for rel, full in chunk:
            with open(full, "rb") as f:
                batch.append((rel, f.read()))
        remote_upload_profile_batch.remote(batch)
        for rel, full in chunk:
            mark_uploaded(state, "profile:" + rel, full)
        uploaded += len(chunk)
        save_state(state)
        pct = round(uploaded / total * 100)
        print("  " + str(uploaded) + "/" + str(total) + " (" + str(pct) + "%)")

    print("[profiles] Done")


def upload_thumbnails(state, dry_run):
    if not os.path.isdir(LOCAL_CARDSDB):
        print("CardsDB not found at " + LOCAL_CARDSDB)
        return

    all_thumbs = []
    for root, dirs, files in os.walk(LOCAL_CARDSDB):
        for fname in files:
            if fname == "profile.json":
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in THUMBNAIL_EXTENSIONS:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, LOCAL_CARDSDB).replace("\\", "/")
                all_thumbs.append((rel, full))

    to_upload = [(rel, full) for rel, full in all_thumbs if needs_upload(state, "thumbnail:" + rel, full)]

    skipped = len(all_thumbs) - len(to_upload)
    total = len(to_upload)

    if not to_upload:
        print("[thumbnails] All " + str(len(all_thumbs)) + " up to date, skipping")
        return

    size_mb = round(sum(os.path.getsize(full) for _, full in to_upload) / 1024 / 1024, 1)
    print("[thumbnails] Uploading " + str(total) + " new/changed (" + str(size_mb) + " MB)" + (" (skipped " + str(skipped) + " unchanged)" if skipped else "") + "...")

    if dry_run:
        print("[DRY RUN] would upload " + str(total) + " thumbnails (" + str(size_mb) + " MB)")
        return

    uploaded = 0
    for i in range(0, total, THUMBNAIL_BATCH_SIZE):
        chunk = to_upload[i:i + THUMBNAIL_BATCH_SIZE]
        batch = []
        for rel, full in chunk:
            with open(full, "rb") as f:
                batch.append((rel, f.read()))
        remote_upload_thumbnail_batch.remote(batch)
        for rel, full in chunk:
            mark_uploaded(state, "thumbnail:" + rel, full)
        uploaded += len(chunk)
        save_state(state)
        pct = round(uploaded / total * 100)
        print("  " + str(uploaded) + "/" + str(total) + " (" + str(pct) + "%)")

    print("[thumbnails] Done")


def upload_set_metadata(state, dry_run):
    if not os.path.exists(LOCAL_SETMETA):
        print("[set_metadata] File not found at " + LOCAL_SETMETA + ", skipping")
        return

    key = "root:set_metadata.json"
    if not needs_upload(state, key, LOCAL_SETMETA):
        print("[set_metadata] Up to date, skipping")
        return

    kb = round(os.path.getsize(LOCAL_SETMETA) / 1024, 1)
    if dry_run:
        print("[DRY RUN] would upload set_metadata.json (" + str(kb) + " KB)")
        return

    print("[set_metadata] Uploading (" + str(kb) + " KB)...")
    with open(LOCAL_SETMETA, "rb") as f:
        data = f.read()
    remote_upload_root_file.remote("set_metadata.json", data)
    mark_uploaded(state, key, LOCAL_SETMETA)
    save_state(state)
    print("[set_metadata] Done")


def upload_sku_game_map(state, dry_run):
    if not os.path.exists(LOCAL_SKUMAP):
        print("[sku_game_map] File not found at " + LOCAL_SKUMAP + ", skipping")
        return

    key = "root:sku_game_map.json"
    if not needs_upload(state, key, LOCAL_SKUMAP):
        print("[sku_game_map] Up to date, skipping")
        return

    kb = round(os.path.getsize(LOCAL_SKUMAP) / 1024, 1)
    if dry_run:
        print("[DRY RUN] would upload sku_game_map.json (" + str(kb) + " KB)")
        return

    print("[sku_game_map] Uploading (" + str(kb) + " KB)...")
    with open(LOCAL_SKUMAP, "rb") as f:
        data = f.read()
    remote_upload_root_file.remote("sku_game_map.json", data)
    mark_uploaded(state, key, LOCAL_SKUMAP)
    save_state(state)
    print("[sku_game_map] Done")


def upload_identifier_lookup(state, dry_run):
    if not os.path.exists(LOCAL_IDLOOKUP):
        print("[identifier_lookup] File not found at " + LOCAL_IDLOOKUP + ", skipping")
        return

    key = "root:identifier_lookup.json"
    if not needs_upload(state, key, LOCAL_IDLOOKUP):
        print("[identifier_lookup] Up to date, skipping")
        return

    kb = round(os.path.getsize(LOCAL_IDLOOKUP) / 1024, 1)
    if dry_run:
        print("[DRY RUN] would upload identifier_lookup.json (" + str(kb) + " KB)")
        return

    print("[identifier_lookup] Uploading (" + str(kb) + " KB)...")
    with open(LOCAL_IDLOOKUP, "rb") as f:
        data = f.read()
    remote_upload_root_file.remote("identifier_lookup.json", data)
    mark_uploaded(state, key, LOCAL_IDLOOKUP)
    save_state(state)
    print("[identifier_lookup] Done")


# ── Entrypoint ─────────────────────────────────────────────────────────────────

@app.local_entrypoint()
def main(
    vertical: str = "all",
    images: bool = False,
    profiles: bool = True,
    thumbnails: bool = False,
    set_metadata: bool = True,
    sku_game_map: bool = True,
    identifier_lookup: bool = True,
    all_data: bool = False,
    dry_run: bool = False,
    reset: bool = False,
    check: bool = False,
):
    """
    Flags:
      --vertical TEXT   vertical to upload (default: all). e.g. cards, keys
      --images          also upload image_db files for the vertical(s)
      --profiles        upload profile.json files from CardsDB (default: on)
      --thumbnails      upload thumbnail images from CardsDB (front.png etc.)
      --set-metadata    upload set_metadata.json to volume root (default: on)
      --all-data        shorthand for --images --thumbnails
      --dry-run         show what would be uploaded without actually uploading
      --reset           clear upload state so everything is re-uploaded from scratch
      --check           inspect volume contents and exit
    """
    if check:
        remote_check_volume.remote()
        return

    if reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print("Upload state cleared — all files will be re-uploaded")
        else:
            print("No state file found (nothing to reset)")
        if not (images or profiles or thumbnails or all_data):
            return

    if all_data:
        images = True
        thumbnails = True

    state = load_state()

    # Determine which verticals to process
    if vertical == "all":
        verticals = [d for d in os.listdir(LOCAL_BASE) if os.path.isdir(os.path.join(LOCAL_BASE, d))]
    else:
        verticals = [vertical]

    print("=== MatchIT Smart Upload" + (" [DRY RUN]" if dry_run else "") + " ===")
    print("Verticals: " + ", ".join(verticals))
    print("DB + cache: yes | Images: " + str(images) + " | Profiles: " + str(profiles) + " | Thumbnails: " + str(thumbnails) + " | set_metadata: " + str(set_metadata))
    print()

    # 1. DB + numpy cache for each vertical
    for v in verticals:
        upload_db_and_cache(v, state, dry_run)

    # 2. image_db for each vertical (optional)
    if images:
        for v in verticals:
            upload_images(v, state, dry_run)

    # 3. Profile JSONs from CardsDB
    if profiles:
        upload_profiles(state, dry_run)

    # 4. Thumbnail images from CardsDB
    if thumbnails:
        upload_thumbnails(state, dry_run)

    # 5. set_metadata.json to volume root
    if set_metadata:
        upload_set_metadata(state, dry_run)

    # 6. sku_game_map.json to volume root
    if sku_game_map:
        upload_sku_game_map(state, dry_run)

    # 7. identifier_lookup.json to volume root
    if identifier_lookup:
        upload_identifier_lookup(state, dry_run)

    print()
    print("=== All done! ===")
