import modal

vol = modal.Volume.from_name("matchit-data-v2")
app = modal.App("check-recent")

@app.function(volumes={"/modal_data": vol})
def check():
    import sqlite3, os
    
    conn = sqlite3.connect("/modal_data/MatchITv2_ProductMatch_Data/cards/images.db")
    cur = conn.cursor()
    
    print("=== Exact me4 card count ===")
    # Exact prefix to avoid matching me40, me41 etc
    n = cur.execute("SELECT COUNT(*) FROM images WHERE sku LIKE 'me4-%'").fetchone()[0]
    print(f"  Rows where sku starts 'me4-': {n}")
    
    print("\n=== Sample me4 rows (first 3) ===")
    for r in cur.execute("SELECT image_id, sku, original_filename, path FROM images WHERE sku LIKE 'me4-%' LIMIT 3").fetchall():
        print(f"  {r}")
    
    print("\n=== Do me4 image files exist on disk? ===")
    rows = cur.execute("SELECT path FROM images WHERE sku LIKE 'me4-%' LIMIT 5").fetchall()
    for (path,) in rows:
        # path is likely relative; try a few resolutions
        candidates = [path, f"/modal_data/MatchITv2_ProductMatch_Data/cards/{path}", f"/modal_data/{path}"]
        for c in candidates:
            if os.path.exists(c):
                print(f"  FOUND: {c}")
                break
        else:
            print(f"  MISSING: {path}")
    
    print("\n=== Does CardsDB/pokemon/me4-1 exist? (profile path check) ===")
    print("  ", os.path.exists("/modal_data/CardsDB/pokemon/me4-1"))

@app.local_entrypoint()
def main():
    check.remote()