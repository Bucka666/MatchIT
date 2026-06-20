import sqlite3, os

base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
db_path = os.path.join(base, "MatchITv2_ProductMatch_Data", "kitchen_tools", "images.db")

print(f"DB path: {db_path}")
print(f"Exists: {os.path.exists(db_path)}")

conn = sqlite3.connect(db_path)
rows = conn.execute("SELECT image_id, sku, original_filename, path, embedding IS NULL as needs_embed FROM images LIMIT 5").fetchall()
for r in rows:
    print(r)
print(f"\nTotal: {conn.execute('SELECT COUNT(*) FROM images').fetchone()[0]}")
print(f"Need embedding: {conn.execute('SELECT COUNT(*) FROM images WHERE embedding IS NULL').fetchone()[0]}")
conn.close()