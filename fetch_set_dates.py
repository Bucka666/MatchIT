# fetch_set_dates.py — one-time, READ-ONLY, no production impact.
# Sources set release dates: pokemontcg.io (Pokemon) + Scryfall (MTG).
# Caches to set_dates.json = { set_id: {date, set_type?, source} }.
import json, time, urllib.request

OUT = "set_dates.json"
UA = "GrailSweep-index-recon/1.0 (set release-date fetch; contact support@grailsweep.com)"

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

out = {}

# ---- Pokemon: pokemontcg.io ----
print("[pkm] fetching pokemontcg.io sets ...", flush=True)
pkm = get_json("https://api.pokemontcg.io/v2/sets?pageSize=250")
for s in pkm.get("data", []):
    d = s.get("releaseDate", "")  # 'YYYY/MM/DD'
    iso = d.replace("/", "-") if d else ""
    out[s["id"]] = {"date": iso, "set_type": None, "source": "pokemontcg.io",
                    "name": s.get("name", "")}
print(f"[pkm] {len(pkm.get('data', []))} sets", flush=True)

# ---- MTG: Scryfall (one call, set User-Agent per their etiquette) ----
print("[mtg] fetching scryfall sets ...", flush=True)
time.sleep(0.1)
scry = get_json("https://api.scryfall.com/sets")
mtg_n = 0
for s in scry.get("data", []):
    out[s["code"]] = {"date": s.get("released_at", "") or "", "set_type": s.get("set_type"),
                      "source": "scryfall", "name": s.get("name", "")}
    mtg_n += 1
print(f"[mtg] {mtg_n} sets", flush=True)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=0)
print(f"[saved] {OUT} ({len(out)} sets total)", flush=True)
