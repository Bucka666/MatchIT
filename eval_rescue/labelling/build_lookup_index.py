"""
build_lookup_index.py — rebuilds lookup_index.json and recompacts the
REF blob embedded in label.html.

Source (per label.html's own header comment, unchanged):
  - C:\\CardsDB\\{pokemon,mtg,yugioh}\\*\\profile.json  (per-card)
  - C:\\MatchIT\\set_metadata.json                       (set-level `total`)
  - C:\\MatchIT\\set_dates.json                           (set-level `year`)

JOIN KEY FIX (2026-08-04): JP sets were previously looked up in
set_metadata.json using the bare TCGdex code straight from each card's
profile.json `set_id` (e.g. "S11"). set_metadata.json keys JP sets as
"jpn-{code}" in lowercase (e.g. "jpn-s11"), so every JP lookup silently
missed and every JP set's `total` came out null (124/129 sets). EN/MTG/
YGO were unaffected — their profile.json `set_id` already matches the
set_metadata.json key directly, and that path is untouched here.

`total` is sourced from set_metadata's `printed_total` field, not its
`total` field — confirmed by cross-checking existing (working) EN
entries, e.g. bw1 has printed_total=114/total=115 and the existing
index carries 114.

Run: python build_lookup_index.py
Writes lookup_index.json (flat, 22ish MiB) and rewrites label.html's
single `const REF = ...;` line in place — nothing else in label.html
is touched (search function, STORE key, IMAGES array all untouched).
"""
import json
import os

ROOT = r"C:\MatchIT"
CARDSDB = r"C:\CardsDB"
LABEL_DIR = os.path.join(ROOT, "eval_rescue", "labelling")

GAME_DIRS = {
    "pokemon": None,   # resolved per-card: Pokemon-EN or Pokemon-JP
    "mtg": "MTG",
    "yugioh": "YGO",
}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_flat_index():
    set_metadata = load_json(os.path.join(ROOT, "set_metadata.json"))
    set_dates = load_json(os.path.join(ROOT, "set_dates.json"))

    flat = []
    for subdir, fixed_game in GAME_DIRS.items():
        base = os.path.join(CARDSDB, subdir)
        if not os.path.isdir(base):
            continue
        for sku in os.listdir(base):
            card_dir = os.path.join(base, sku)
            profile_path = os.path.join(card_dir, "profile.json")
            if not os.path.isfile(profile_path):
                continue
            try:
                profile = load_json(profile_path)
            except Exception:
                continue

            set_id = profile.get("set_id")
            if not set_id:
                continue

            if fixed_game:
                game = fixed_game
            else:
                game = "Pokemon-JP" if sku.startswith("jpn-") else "Pokemon-EN"

            # THE FIX: JP sets join set_metadata.json / set_dates.json via
            # "jpn-" + lowercased set_id. Every other game joins via the
            # raw set_id unchanged — exactly the pre-existing, working
            # convention for EN/MTG/YGO.
            meta_key = ("jpn-" + set_id.lower()) if game == "Pokemon-JP" else set_id

            meta_entry = set_metadata.get(meta_key) or {}
            total = meta_entry.get("printed_total")

            date_entry = set_dates.get(meta_key) or {}
            date_str = date_entry.get("date") or ""
            year = date_str[:4] if len(date_str) >= 4 else None

            flat.append({
                "sku": sku,
                "name": profile.get("name"),
                "number": profile.get("card_number"),
                "total": total,
                "set_code": set_id,
                "set_name": profile.get("set_name") or "",
                "year": year,
                "game": game,
            })

    return flat


def compact(flat):
    """[set_code, set_name, total, year, game] deduped -> REF.sets,
    [sku, name, number, setIndex] -> REF.cards. Same shape as before."""
    set_index = {}
    sets = []
    cards = []
    for e in flat:
        key = (e["set_code"], e["set_name"], e["total"], e["year"], e["game"])
        idx = set_index.get(key)
        if idx is None:
            idx = len(sets)
            set_index[key] = idx
            sets.append([e["set_code"], e["set_name"], e["total"], e["year"], e["game"]])
        cards.append([e["sku"], e["name"], e["number"], idx])
    return {"sets": sets, "cards": cards}


def write_label_html(ref):
    path = os.path.join(LABEL_DIR, "label.html")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    new_line = "const REF = " + json.dumps(ref, ensure_ascii=False, separators=(",", ":")) + ";\n"

    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("const REF ="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        raise RuntimeError("const REF = ... line not found in label.html — refusing to write")

    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)


def main():
    flat = build_flat_index()
    with open(os.path.join(LABEL_DIR, "lookup_index.json"), "w", encoding="utf-8") as f:
        json.dump(flat, f, ensure_ascii=False)
    print(f"[BUILD] lookup_index.json: {len(flat)} entries")

    ref = compact(flat)
    print(f"[BUILD] REF.sets: {len(ref['sets'])}  REF.cards: {len(ref['cards'])}")

    write_label_html(ref)
    print("[BUILD] label.html REF blob rewritten in place")


if __name__ == "__main__":
    main()
