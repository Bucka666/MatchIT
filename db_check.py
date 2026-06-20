import sys
sys.path.insert(0, "/app")

import modal

app = modal.App("db-check")

vol = modal.Volume.from_name("matchit-data-v2", version=2)
image = modal.Image.debian_slim(python_version="3.11")

@app.function(image=image, volumes={"/modal_data": vol}, timeout=120)
def check():
    import sqlite3
    c = sqlite3.connect('/modal_data/MatchITv2_ProductMatch_Data/cards/images.db')

    total = c.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    with_emb = c.execute("SELECT COUNT(*) FROM images WHERE embedding IS NOT NULL").fetchone()[0]
    null_emb = c.execute("SELECT COUNT(*) FROM images WHERE embedding IS NULL").fetchone()[0]
    hob = c.execute("SELECT COUNT(*) FROM images WHERE sku LIKE 'mtg-hob-%'").fetchone()[0]
    hob_emb = c.execute("SELECT COUNT(*) FROM images WHERE sku LIKE 'mtg-hob-%' AND embedding IS NOT NULL").fetchone()[0]
    fra = c.execute("SELECT COUNT(*) FROM images WHERE sku LIKE 'mtg-fra-%'").fetchone()[0]
    fra_emb = c.execute("SELECT COUNT(*) FROM images WHERE sku LIKE 'mtg-fra-%' AND embedding IS NOT NULL").fetchone()[0]
    sample_hob = c.execute("SELECT image_id, sku, path, LENGTH(embedding) FROM images WHERE sku LIKE 'mtg-hob-%' LIMIT 1").fetchone()
    sample_other = c.execute("SELECT image_id, sku, path, LENGTH(embedding) FROM images WHERE sku NOT LIKE 'mtg-hob-%' AND sku NOT LIKE 'mtg-fra-%' AND embedding IS NOT NULL LIMIT 1").fetchone()

    print("=" * 60)
    print("SQLITE DIAGNOSTIC")
    print("=" * 60)
    print("Total rows:                       " + str(total))
    print("Rows with embedding NOT NULL:     " + str(with_emb))
    print("Rows with embedding NULL:         " + str(null_emb))
    print()
    print("hob rows total:                   " + str(hob))
    print("hob rows with embedding:          " + str(hob_emb))
    print("fra rows total:                   " + str(fra))
    print("fra rows with embedding:          " + str(fra_emb))
    print()
    print("--- Sample hob row ---")
    if sample_hob:
        print("  image_id:  " + str(sample_hob[0]))
        print("  sku:       " + str(sample_hob[1]))
        print("  path:      " + str(sample_hob[2]))
        print("  emb bytes: " + str(sample_hob[3]))
    else:
        print("  NONE")
    print()
    print("--- Sample existing (non-hob/fra) row ---")
    if sample_other:
        print("  image_id:  " + str(sample_other[0]))
        print("  sku:       " + str(sample_other[1]))
        print("  path:      " + str(sample_other[2]))
        print("  emb bytes: " + str(sample_other[3]))
    else:
        print("  NONE")
    print("=" * 60)

    c.close()


@app.local_entrypoint()
def main():
    check.remote()