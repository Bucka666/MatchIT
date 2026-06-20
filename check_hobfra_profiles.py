# C:\MatchIT\check_hobfra_profiles.py
import modal

app = modal.App("check-hobfra-profiles")
image = modal.Image.debian_slim()
vol = modal.Volume.from_name("matchit-data-v2")

@app.local_entrypoint()
def main():
    check.remote()

@app.function(image=image, volumes={"/data": vol})
def check():
    import os, json

    # Check a working card first
    print("=== mtg-drc-7 (working card) ===")
    drc_dir = "/data/CardsDB/mtg/mtg-drc-7"
    if os.path.exists(drc_dir):
        contents = os.listdir(drc_dir)
        print(f"  Dir contents: {contents}")
        pj = os.path.join(drc_dir, "profile.json")
        if os.path.exists(pj):
            with open(pj) as f:
                data = json.load(f)
            print(f"  profile.json keys: {list(data.keys())}")
            print(f"  Sample values:")
            for k in list(data.keys())[:12]:
                v = data[k]
                preview = str(v)[:100] if v is not None else "null"
                print(f"    {k}: {preview}")
    else:
        print(f"  Dir does not exist: {drc_dir}")

    # Now check hob/fra
    print("\n=== mtg-hob-29 (broken card) ===")
    hob_dir = "/data/CardsDB/mtg/mtg-hob-29"
    if os.path.exists(hob_dir):
        contents = os.listdir(hob_dir)
        print(f"  Dir contents: {contents}")
        pj = os.path.join(hob_dir, "profile.json")
        if os.path.exists(pj):
            with open(pj) as f:
                data = json.load(f)
            print(f"  profile.json keys: {list(data.keys())}")
            print(f"  Sample values:")
            for k in list(data.keys())[:12]:
                v = data[k]
                preview = str(v)[:100] if v is not None else "null"
                print(f"    {k}: {preview}")
        else:
            print(f"  ✗ profile.json MISSING")
    else:
        print(f"  ✗ Dir does not exist: {hob_dir}")

    # Count coverage across all 46 hob/fra SKUs
    print("\n=== Coverage across all hob/fra cards ===")
    hob_present = 0
    hob_missing = []
    fra_present = 0
    fra_missing = []
    mtg_root = "/data/CardsDB/mtg"
    if os.path.exists(mtg_root):
        for sku in os.listdir(mtg_root):
            if sku.startswith("mtg-hob-"):
                pj = os.path.join(mtg_root, sku, "profile.json")
                if os.path.exists(pj):
                    hob_present += 1
                else:
                    hob_missing.append(sku)
            elif sku.startswith("mtg-fra-"):
                pj = os.path.join(mtg_root, sku, "profile.json")
                if os.path.exists(pj):
                    fra_present += 1
                else:
                    fra_missing.append(sku)
    print(f"  hob: {hob_present} profile.json present, {len(hob_missing)} missing")
    if hob_missing[:5]:
        print(f"    sample missing: {hob_missing[:5]}")
    print(f"  fra: {fra_present} profile.json present, {len(fra_missing)} missing")
    if fra_missing[:5]:
        print(f"    sample missing: {fra_missing[:5]}")