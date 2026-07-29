import json, subprocess, sys

# Download current set_metadata.json from volume
print("Downloading set_metadata.json from volume...")
result = subprocess.run(
    ['modal', 'volume', 'get', 'matchit-data-v2', 'set_metadata.json', '-'],
    capture_output=True, encoding='utf-8'
)
if result.returncode != 0:
    print("ERROR:", result.stderr); sys.exit(1)

# `modal volume get ... -` appends a status line ("Finished downloading files
# to local!") to STDOUT after the payload, so a bare json.loads() dies with
# "Extra data". Trim to the final closing brace before parsing.
raw = result.stdout
end = raw.rfind('}')
if end == -1:
    print("ERROR: no JSON object found in volume output"); sys.exit(1)
data = json.loads(raw[:end + 1])
print(f"Loaded {len(data)} sets")

# Back up exactly what we pulled, before any mutation. Step 3 overwrites the
# live set_metadata.json in place, so this is the only rollback path.
backup = 'C:/MatchIT/set_metadata_backup.json'
with open(backup, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
print(f"Backed up original to {backup}")

# English name map — all 26
name_map = {
    'jpn-s5a':  'Matchless Fighters',
    'jpn-s10a': 'Dark Phantasma',
    'jpn-s10b': 'Pokémon GO',
    'jpn-s10d': 'Time Gazer',
    'jpn-s10p': 'Space Juggler',
    'jpn-s11':  'Lost Abyss',
    'jpn-s11a': 'Incandescent Arcana',
    'jpn-sv6a': 'Night Wanderer',
    'jpn-sk':   'VSTAR Premium Trainer Box',
    'jpn-sld':  'Darkrai VSTAR Starter Set',
    'jpn-sll':  'Lucario VSTAR Starter Set',
    'jpn-sn':   'Pikachu V & Eevee V Starter Set',
    'jpn-spd':  'Deoxys VSTAR & VMAX High-Class Deck',
    'jpn-spz':  'Zeraora VSTAR & VMAX High-Class Deck',
    'jpn-sval': 'Starter Set ex Fuecoco & Ampharos ex',
    'jpn-svam': 'Starter Set ex Sprigatito & Lucario ex',
    'jpn-svaw': 'Starter Set ex Quaxly & Mimikyu ex',
    'jpn-svb':  'Premium Trainer Box ex',
    'jpn-svc':  'Starter Set ex Pikachu ex & Pawmot',
    'jpn-svel': 'Terastal Skeledirge ex Starter Set',
    'jpn-svem': 'Terastal Mewtwo ex Starter Set',
    'jpn-svf':  'Deck Build Box Ruler of the Black Flame',
    'jpn-svhk': 'Ancient Koraidon ex Starter Deck & Build Set',
    'jpn-svhm': 'Future Miraidon ex Starter Deck & Build Set',
    'jpn-svp1': 'Scarlet & Violet ex Special Set',
    'jpn-sp6':  'VSTAR Special Set',
}

# Total map — 8 TCGdex-confirmed sets only
total_map = {
    'jpn-s5a':  70,
    'jpn-s10a': 71,
    'jpn-s10b': 71,
    'jpn-s10d': 67,
    'jpn-s10p': 67,
    'jpn-s11':  100,
    'jpn-s11a': 68,
    'jpn-sv6a': 64,
}

updated = 0
missing = []
for set_id, name in name_map.items():
    if set_id in data:
        data[set_id]['name'] = name
        if set_id in total_map:
            data[set_id]['total'] = total_map[set_id]
            data[set_id]['printed_total'] = total_map[set_id]
        updated += 1
        print(f"  OK  {set_id} -> {name}")
    else:
        missing.append(set_id)
        print(f"  WARN {set_id} not found in metadata — skipped")

print(f"\nUpdated: {updated}  Missing: {len(missing)}")

# Write updated file
out = 'C:/MatchIT/set_metadata_updated.json'
with open(out, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
print(f"Written to {out}")
