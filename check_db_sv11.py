import modal

app = modal.App("check-db")
vol = modal.Volume.from_name("matchit-data-v2")

@app.function(volumes={"/modal_data": vol})
def check():
    import sqlite3
    db = "/modal_data/MatchITv2_ProductMatch_Data/cards/images.db"
    con = sqlite3.connect(db)
    cur = con.cursor()

    # Show the columns of the images table so we know what to query
    cur.execute("PRAGMA table_info(images);")
    cols = cur.fetchall()
    print("[IMAGES COLUMNS]", [(c[1], c[2]) for c in cols])

    # Find the column most likely holding the SKU
    colnames = [c[1] for c in cols]
    sku_col = None
    for cand in ("sku", "card_id", "id", "name", "filename", "path"):
        if cand in colnames:
            sku_col = cand
            break
    print("[USING COLUMN]", sku_col)

    if sku_col:
        for pat in ("rsv10pt5%", "rsv10%", "%black%", "%bolt%", "me1%", "me2%", "me3%"):
            cur.execute(f"SELECT {sku_col} FROM images WHERE {sku_col} LIKE ? LIMIT 10;", (pat,))
            rows = [r[0] for r in cur.fetchall()]
            print(f"[MATCH {pat}] -> {rows}")
        cur.execute(f"SELECT substr({sku_col},1,8) AS p, COUNT(*) FROM images WHERE {sku_col} LIKE 'rsv%' OR {sku_col} LIKE 'me%' GROUP BY p ORDER BY p;")
        print("[PREFIX COUNTS]", cur.fetchall())

    con.close()

@app.local_entrypoint()
def main():
    check.remote()