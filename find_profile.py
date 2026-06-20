import modal

app = modal.App("find-profile")
image = modal.Image.debian_slim()
vol = modal.Volume.from_name("matchit-data-v2")

@app.local_entrypoint()
def main():
    find.remote()

@app.function(image=image, volumes={"/data": vol})
def find():
    import os, json, sqlite3

    # Look at what's actually in the volume
    print("=== Top-level /data contents ===")
    for entry in sorted(os.listdir("/data"))[:30]:
        full = os.path.join("/data", entry)
        kind = "DIR" if os.path.isdir(full) else "FILE"
        print(f"  [{kind}] {entry}")

    # Find any profile.json files
    print("\n=== Profile-related files (first 20) ===")
    found = []
    for root, dirs, files in os.walk("/data"):
        for f in files:
            if "profile" in f.lower() or f.endswith(".json"):
                found.append(os.path.join(root, f))
                if len(found) >= 20:
                    break
        if len(found) >= 20:
            break
    for f in found:
        size = os.path.getsize(f)
        print(f"  {f} ({size:,} bytes)")

    # Check SQLite for any extra columns beyond what db_check shows
    print("\n=== Full schema of cards table ===")
    for db_candidate in ["/data/cards.db", "/data/MatchITv2_ProductMatch_Data/cards.db"]:
        if os.path.exists(db_candidate):
            print(f"DB: {db_candidate}")
            conn = sqlite3.connect(db_candidate)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            print(f"Tables: {tables}")
            for t in tables:
                cur.execute(f"PRAGMA table_info({t})")
                cols = cur.fetchall()
                print(f"\nTable '{t}' columns:")
                for c in cols:
                    print(f"  {c[1]} ({c[2]})")
            conn.close()
            break
    else:
        print("No DB found at expected paths")