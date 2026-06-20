"""
verify_me4_profiles.py — Step 1 verification for Strategy A fix.
Confirms me4 profile.json files exist at the scrape path and that
game subdirectory naming matches what the loader expects.
"""
import modal

app = modal.App("verify-me4-profiles")
vol = modal.Volume.from_name("matchit-data-v2")


@app.function(volumes={"/modal_data": vol}, timeout=60)
def verify():
    import os, json
    from pathlib import Path

    SCRAPE_ROOT = Path("/modal_data/MatchITv2_ProductMatch_Data/cards")
    CARDSDB_ROOT = Path("/modal_data/CardsDB")

    print("=" * 60)
    print("SCRAPE ROOT:", SCRAPE_ROOT)
    print("Exists:", SCRAPE_ROOT.exists())
    if SCRAPE_ROOT.exists():
        subdirs = [d.name for d in SCRAPE_ROOT.iterdir() if d.is_dir()]
        print("Subdirectories:", sorted(subdirs))

    print()
    print("CARDSDB ROOT:", CARDSDB_ROOT)
    print("Exists:", CARDSDB_ROOT.exists())
    if CARDSDB_ROOT.exists():
        subdirs = [d.name for d in CARDSDB_ROOT.iterdir() if d.is_dir()]
        print("Subdirectories:", sorted(subdirs))

    print()
    print("=" * 60)
    print("CHECKING me4 profiles in SCRAPE path...")
    pkm_scrape = SCRAPE_ROOT / "pokemon"
    if not pkm_scrape.exists():
        print("ERROR: pokemon dir does not exist at scrape path!")
        return

    me4_dirs = sorted([d for d in pkm_scrape.iterdir() if d.is_dir() and d.name.startswith("me4-")])
    print(f"Found {len(me4_dirs)} me4-* directories in scrape path")

    print()
    print("Sample — first 3 me4 directories:")
    for d in me4_dirs[:3]:
        profile_path = d / "profile.json"
        front_path = d / "front.png"
        print(f"  {d.name}/")
        print(f"    profile.json: {'EXISTS' if profile_path.exists() else 'MISSING'}")
        print(f"    front.png:    {'EXISTS' if front_path.exists() else 'MISSING'}")
        if profile_path.exists():
            try:
                with open(profile_path) as f:
                    p = json.load(f)
                print(f"    profile keys: {sorted(p.keys())}")
                print(f"    name:         {p.get('name')}")
                print(f"    set_name:     {p.get('set_name')}")
                print(f"    card_number:  {p.get('card_number')}")
                prices = p.get('prices', {})
                print(f"    prices keys:  {list(prices.keys())}")
            except Exception as e:
                print(f"    ERROR reading profile: {e}")
        print()

    print("=" * 60)
    print("CHECKING me4 profiles in CARDSDB path...")
    pkm_cardsdb = CARDSDB_ROOT / "pokemon"
    if not pkm_cardsdb.exists():
        print("pokemon dir does not exist in CardsDB — expected for new sets")
    else:
        me4_cardsdb = [d for d in pkm_cardsdb.iterdir() if d.is_dir() and d.name.startswith("me4-")]
        print(f"Found {len(me4_cardsdb)} me4-* directories in CardsDB")

    print()
    print("=" * 60)
    print("GAME DIR NAMING CHECK")
    print("Scrape path game dirs:", sorted([d.name for d in SCRAPE_ROOT.iterdir() if d.is_dir()]) if SCRAPE_ROOT.exists() else "N/A")
    print("CardsDB game dirs:    ", sorted([d.name for d in CARDSDB_ROOT.iterdir() if d.is_dir()]) if CARDSDB_ROOT.exists() else "N/A")
    print("Loader iterates CardsDB subdirs — if game dir names differ between trees, fallback must normalise.")


@app.local_entrypoint()
def main():
    verify.remote()
