import modal
import os

VOLUME_NAME = "matchit-data"
LOCAL_BASE = "C:/Users/c_a_b/AppData/Local/MatchITv2_ProductMatch_Data"

vol = modal.Volume.from_name(VOLUME_NAME)
app = modal.App("matchit-upload")


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