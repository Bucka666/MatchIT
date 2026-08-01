"""
Read-only price-coverage census over CardsDB on matchit-data-v2.
Standalone throwaway Modal app - does NOT touch matchit-api.

Walks every card profile.json and classifies price presence:
  HAS_PRICE   - at least one usable price field > 0
  EMPTY_BLOCK - "prices" key exists but every field is 0/missing
  NO_BLOCK    - no "prices" key at all

Reads only. Never writes to the volume, never calls vol.commit().

Run:  $env:PYTHONIOENCODING="utf-8"; modal run price_coverage.py
"""
import modal

app = modal.App("grailsweep-price-coverage")
vol = modal.Volume.from_name("matchit-data-v2")

CARDS = "/modal_data/CardsDB"
META = "/modal_data/set_metadata.json"
CAL = "/modal_data/MatchITv2_ProductMatch_Data/cards/set_release_calendar.json"

# A full sequential walk of ~135k profiles over the network volume does NOT
# finish in 3600s (observed twice with rebuild_identifier_lookup). These reads
# are I/O-bound, so a thread pool is the fix - same approach app.py already
# uses for its per-row profile.json reads.
WORKERS = 64


@app.function(volumes={"/modal_data": vol}, timeout=3600)
def census():
    import os, json
    from collections import Counter, defaultdict
    from concurrent.futures import ThreadPoolExecutor

    # ---- price-presence test (per spec: any of these > 0 counts) ----
    CM_FIELDS = ("avg_sell", "low", "avg_7d", "avg_30d", "trend", "avg_1d", "mid")
    TCG_FIELDS = ("market", "mid")

    def classify(prof):
        pr = prof.get("prices")
        if not isinstance(pr, dict) or not pr:
            return "NO_BLOCK"
        cm = pr.get("cardmarket")
        if isinstance(cm, dict):
            for f in CM_FIELDS:
                v = cm.get(f)
                if isinstance(v, (int, float)) and v > 0:
                    return "HAS_PRICE"
        tcg = pr.get("tcgplayer")
        if isinstance(tcg, dict):
            for _variant, vd in tcg.items():
                if isinstance(vd, dict):
                    for f in TCG_FIELDS:
                        v = vd.get(f)
                        if isinstance(v, (int, float)) and v > 0:
                            return "HAS_PRICE"
                elif isinstance(vd, (int, float)) and vd > 0:
                    return "HAS_PRICE"
        # any other source key with a numeric > 0
        for src, sd in pr.items():
            if src in ("cardmarket", "tcgplayer"):
                continue
            if isinstance(sd, (int, float)) and sd > 0:
                return "HAS_PRICE"
            if isinstance(sd, dict):
                for _k, v in sd.items():
                    if isinstance(v, (int, float)) and v > 0:
                        return "HAS_PRICE"
        return "EMPTY_BLOCK"

    try:
        meta = json.load(open(META, encoding="utf-8"))
    except Exception:
        meta = {}
    try:
        cal = json.load(open(CAL, encoding="utf-8-sig"))
        cal_dates = {}
        for e in cal.get("sets", []):
            for k in ("pokemontcg_io", "tcgdex_en"):
                sid = (e.get("source_ids") or {}).get(k)
                if sid:
                    cal_dates[str(sid).lower()] = e.get("release_date")
    except Exception:
        cal_dates = {}

    games = [g for g in ("pokemon", "mtg", "yugioh")
             if os.path.isdir(os.path.join(CARDS, g))]

    def read_one(args):
        game, folder = args
        p = os.path.join(CARDS, game, folder, "profile.json")
        try:
            prof = json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
        return (game, folder, classify(prof),
                str(prof.get("set_id") or ""), str(prof.get("set_name") or ""),
                str(prof.get("set_era") or ""), str(prof.get("language") or ""))

    rows = []
    for game in games:
        gp = os.path.join(CARDS, game)
        folders = os.listdir(gp)
        print(f"[SCAN] {game}: {len(folders)} folders", flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for r in ex.map(read_one, ((game, f) for f in folders), chunksize=64):
                if r:
                    rows.append(r)
        print(f"[SCAN] {game}: {len(rows)} profiles read so far", flush=True)

    print(f"\nTOTAL profiles read: {len(rows)}\n")

    def lang_of(game, folder, prof_lang):
        if game != "pokemon":
            return "EN"
        if folder.lower().startswith("jpn-"):
            return "JP"
        return (prof_lang or "EN").upper()[:2] or "EN"

    def gamekey(game, lang):
        if game == "pokemon":
            return f"pkmn {lang}"
        return {"mtg": "mtg", "yugioh": "ygo"}.get(game, game)

    # ---- a. totals ----
    cls = Counter(r[2] for r in rows)
    t = len(rows)
    print("--- a. TOTALS ---")
    for k in ("HAS_PRICE", "EMPTY_BLOCK", "NO_BLOCK"):
        print(f"  {k:<12} {cls[k]:>7}  {cls[k]/t:>6.1%}")
    priceless = [r for r in rows if r[2] != "HAS_PRICE"]
    print(f"  {'PRICELESS':<12} {len(priceless):>7}  {len(priceless)/t:>6.1%}  (EMPTY_BLOCK + NO_BLOCK)")
    print()

    # ---- b. by game ----
    print("--- b. PRICE-LESS BY GAME ---")
    by_game_all = Counter(gamekey(r[0], lang_of(r[0], r[1], r[6])) for r in rows)
    by_game_pl = Counter(gamekey(r[0], lang_of(r[0], r[1], r[6])) for r in priceless)
    print(f"  {'game':<12}{'priceless':>10}{'total':>9}{'% of that game':>16}{'% of all priceless':>20}")
    for g, n in by_game_all.most_common():
        pl = by_game_pl.get(g, 0)
        print(f"  {g:<12}{pl:>10}{n:>9}{pl/n:>15.1%}{(pl/len(priceless) if priceless else 0):>19.1%}")
    print()

    # ---- f. EMPTY vs NO_BLOCK within price-less ----
    print("--- f. EMPTY_BLOCK vs NO_BLOCK (within price-less) ---")
    split = Counter((gamekey(r[0], lang_of(r[0], r[1], r[6])), r[2]) for r in priceless)
    print(f"  {'game':<12}{'EMPTY_BLOCK':>14}{'NO_BLOCK':>11}")
    for g in by_game_all:
        e = split.get((g, "EMPTY_BLOCK"), 0)
        n = split.get((g, "NO_BLOCK"), 0)
        if e or n:
            print(f"  {g:<12}{e:>14}{n:>11}")
    print(f"  {'ALL':<12}{cls['EMPTY_BLOCK']:>14}{cls['NO_BLOCK']:>11}")
    print()

    # ---- e. by language ----
    print("--- e. PRICE-LESS BY LANGUAGE ---")
    lang_all = Counter(lang_of(r[0], r[1], r[6]) for r in rows)
    lang_pl = Counter(lang_of(r[0], r[1], r[6]) for r in priceless)
    for lg, n in lang_all.most_common():
        pl = lang_pl.get(lg, 0)
        print(f"  {lg:<6} priceless={pl:>7} / total={n:>7}  ({pl/n:.1%} of that language)")
    print()

    # ---- c. by set ----
    print("--- c. TOP 30 SETS BY PRICE-LESS CARD COUNT ---")
    set_tot = Counter()
    set_pl = Counter()
    set_info = {}
    for r in rows:
        game, folder, c, sid, sname, era, plang = r
        key = (game, sid or folder.rsplit("-", 1)[0])
        set_tot[key] += 1
        set_info.setdefault(key, (sname, era, lang_of(game, folder, plang)))
        if c != "HAS_PRICE":
            set_pl[key] += 1
    print(f"  {'set_id':<14}{'game':<9}{'lang':<5}{'priceless':>10}{'total':>7}{'%':>7}  set_name")
    for (game, sid), pl in set_pl.most_common(30):
        tot = set_tot[(game, sid)]
        sname, era, lg = set_info[(game, sid)]
        whole = "WHOLE SET" if pl == tot else ""
        print(f"  {sid[:13]:<14}{game[:8]:<9}{lg:<5}{pl:>10}{tot:>7}{pl/tot:>6.0%}  {sname[:28]:<28}{whole}")
    print()

    # ---- whole-set vs scattered ----
    print("--- c2. WHOLE-SET vs SCATTERED (sets with >=1 price-less card) ---")
    whole = [k for k in set_pl if set_pl[k] == set_tot[k]]
    partial = [k for k in set_pl if 0 < set_pl[k] < set_tot[k]]
    whole_cards = sum(set_pl[k] for k in whole)
    part_cards = sum(set_pl[k] for k in partial)
    print(f"  sets 100% price-less : {len(whole):>5}  covering {whole_cards:>7} cards")
    print(f"  sets partially       : {len(partial):>5}  covering {part_cards:>7} cards")
    print(f"  sets fully priced    : {len(set_tot) - len(set_pl):>5}")
    print()

    # ---- d. by set age / era ----
    print("--- d. PRICE-LESS BY SET ERA (release dates absent from set_metadata) ---")
    era_all = Counter(r[5] or "(none)" for r in rows)
    era_pl = Counter(r[5] or "(none)" for r in priceless)
    for era, n in era_all.most_common(15):
        pl = era_pl.get(era, 0)
        print(f"  {era[:26]:<28} priceless={pl:>7} / {n:>7}  ({pl/n:.1%})")
    print()
    print("  calendar-known release dates (only sets in set_release_calendar):")
    if cal_dates:
        for sid, d in sorted(cal_dates.items()):
            k = ("pokemon", sid)
            if k in set_tot:
                print(f"    {sid:<14} released {d}  priceless={set_pl.get(k,0)}/{set_tot[k]}")
    else:
        print("    (none resolvable)")
    print()

    print("Census complete. NOTHING WAS WRITTEN - no vol.commit(), read-only.")


@app.local_entrypoint()
def main():
    census.remote()
