import sqlite3, os

base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
db_path = os.path.join(base, "MatchITv2_ProductMatch_Data", "kitchen_tools", "images.db")

conn = sqlite3.connect(db_path)
rows = conn.execute("SELECT image_id, original_filename FROM images").fetchall()

for image_id, orig in rows:
    if orig and "_FRONT" not in orig and "_BACK" not in orig:
        new_name = orig + "_FRONT"
        conn.execute("UPDATE images SET original_filename = ? WHERE image_id = ?", (new_name, image_id))
        print(f"  {orig} -> {new_name}")

conn.commit()
conn.close()
print("Done — restart Flask and refresh cache")