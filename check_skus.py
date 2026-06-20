import sqlite3
conn = sqlite3.connect(r'C:\Users\c_a_b\AppData\Local\MatchITv2_ProductMatch_Data\cards\images.db')
rows = conn.execute("SELECT DISTINCT sku FROM images WHERE sku LIKE 'dpp%' OR sku LIKE 'bwp%' OR sku LIKE 'swshp%' LIMIT 5").fetchall()
for r in rows: print(r[0])
conn.close()
