# mtg_cut.py — READ-ONLY. Exact MTG selection cut for the first on-device index.
# Counts cards/set via folder names, joins Scryfall set_type+date, applies the
# generous constructed-only cut. No index built.
import os, json
from collections import Counter, defaultdict

CARDSDB = r"C:\CardsDB"
dates = json.load(open("set_dates.json", encoding="utf-8"))
CONSTRUCTED = {"core", "expansion", "masters", "draft_innovation"}
FP16_PER_CARD = 512*2 + 12   # vector + ~sku bytes

# --- sanity: folder-name set_id parse vs profile.json (sample) ---
sample = []
for x in os.scandir(os.path.join(CARDSDB, "mtg")):
    if x.is_dir() and len(sample) < 5:
        parsed = x.name.split("-")[1]
        try:
            real = json.load(open(os.path.join(x.path, "profile.json"), encoding="utf-8")).get("set_id")
        except Exception:
            real = "?"
        sample.append((x.name, parsed, real, parsed == real))
print("[sanity] folder-parse set_id vs profile set_id:")
for n, p, r, ok in sample:
    print(f"   {n:<22} parsed={p:<6} profile={r:<6} {'OK' if ok else 'MISMATCH'}")

# --- count MTG cards per set_id (fast, folder names only) ---
per_set = Counter()
for x in os.scandir(os.path.join(CARDSDB, "mtg")):
    if x.is_dir():
        per_set[x.name.split("-")[1]] += 1
total_mtg = sum(per_set.values())
print(f"\n[mtg] {total_mtg} cards across {len(per_set)} sets")

# --- classify each set by scryfall set_type ---
by_type_cards = Counter()      # set_type -> card count
by_type_sets = Counter()
unmapped_sets, unmapped_cards = [], 0
for sid, n in per_set.items():
    meta = dates.get(sid)
    if not meta or meta.get("source") != "scryfall":
        unmapped_sets.append(sid); unmapped_cards += n
        by_type_cards["<unmapped>"] += n; by_type_sets["<unmapped>"] += 1
        continue
    st = meta.get("set_type") or "<none>"
    by_type_cards[st] += n; by_type_sets[st] += 1

# --- in/out by set_type table ---
print(f"\n{'set_type':<20}{'sets':>6}{'cards':>8}  {'decision'}")
kept = dropped = 0
for st, c in by_type_cards.most_common():
    keep = st in CONSTRUCTED
    if keep: kept += c
    else: dropped += c
    print(f"{st:<20}{by_type_sets[st]:>6}{c:>8}  {'KEEP' if keep else 'drop'}")
print(f"\n  KEEP (constructed types): {kept} cards")
print(f"  drop (non-constructed):   {dropped} cards")
print(f"  unmapped sets (no scryfall match): {len(unmapped_sets)} sets / {unmapped_cards} cards "
      f"-> {unmapped_sets[:10]}")

# --- recency breakdown of the KEPT pool (so 'ancient bulk' is visible) ---
kept_by_decade = Counter()
ancient_constructed = 0   # constructed but pre-2003 (modern-frame era) = bulk candidate
for sid, n in per_set.items():
    meta = dates.get(sid)
    if not meta or meta.get("source") != "scryfall": continue
    if (meta.get("set_type") or "") not in CONSTRUCTED: continue
    d = meta.get("date") or ""
    yr = int(d[:4]) if d[:4].isdigit() else 0
    dec = f"{(yr//10)*10}s" if yr else "unknown"
    kept_by_decade[dec] += n
    if yr and yr < 2003: ancient_constructed += n
print(f"\n  KEPT pool by release decade:")
for dec in sorted(kept_by_decade):
    print(f"     {dec:<9} {kept_by_decade[dec]:>7}")
print(f"  (of KEPT, pre-2003 'ancient' constructed bulk: {ancient_constructed} cards "
      f"-> optional value-trim candidate)")

# --- final index size (Pokemon all + MTG kept) ---
pkm = sum(1 for x in os.scandir(os.path.join(CARDSDB, "pokemon")) if x.is_dir())
print(f"\n========== FIRST INDEX (generous: all Pokemon + constructed MTG) ==========")
print(f"  Pokemon (all):          {pkm:>7}  = {pkm*FP16_PER_CARD/1e6:>6.1f} MB f16")
print(f"  MTG (constructed kept):  {kept:>7}  = {kept*FP16_PER_CARD/1e6:>6.1f} MB f16")
tot = pkm + kept
print(f"  TOTAL:                  {tot:>7}  = {tot*FP16_PER_CARD/1e6:>6.1f} MB f16  "
      f"({tot*(512*4+12)/1e6:.0f} MB f32)")
