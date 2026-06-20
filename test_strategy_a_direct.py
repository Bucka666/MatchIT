"""
test_strategy_a_direct.py — Direct profile-loader verification.

Exercises the exact CardsDB → scrape-dir fallback logic that was
patched into app.py, inside a Modal container on the live volume.
No HTTP, no CLIP model, no Flask startup needed.
"""
import modal

app = modal.App("test-strategy-a-direct")
vol = modal.Volume.from_name("matchit-data-v2")

_image = modal.Image.debian_slim(python_version="3.11")


@app.function(image=_image, volumes={"/modal_data": vol}, timeout=60)
def test_profile_loading():
    import os, json, logging
    from pathlib import Path

    os.environ["LOCALAPPDATA"] = "/modal_data"

    CARDSDB_ROOT = Path("/modal_data/CardsDB")
    SCRAPE_ROOT  = Path("/modal_data/MatchITv2_ProductMatch_Data/cards")

    def get_data_dir():
        """Mirror of app.py get_data_dir() on Modal."""
        # vertical_id is "cards"; LOCALAPPDATA is /modal_data
        return str(SCRAPE_ROOT)

    def get_db_root():
        """Mirror of vertical_loader.get_db_root() on Modal."""
        return str(CARDSDB_ROOT)

    log_lines = []

    def log_info(msg, *args):
        line = msg % args if args else msg
        log_lines.append(line)
        print("  LOG:", line)

    def load_profile(sku: str) -> dict:
        """Exact mirror of the patched enrichment block in app.py 6657–6690."""
        db_root = get_db_root()
        profile = {}

        # Primary: CardsDB
        if db_root:
            for game_dir in Path(db_root).iterdir():
                if not game_dir.is_dir():
                    continue
                profile_path = game_dir / sku / "profile.json"
                if profile_path.exists():
                    try:
                        with open(profile_path, "r", encoding="utf-8") as pf:
                            profile = json.load(pf)
                    except Exception:
                        pass
                    break

        # Fallback: scrape directory (Strategy A patch)
        if not profile:
            scrape_root = get_data_dir()
            if scrape_root:
                for game_dir in Path(scrape_root).iterdir():
                    if not game_dir.is_dir():
                        continue
                    profile_path = game_dir / sku / "profile.json"
                    if profile_path.exists():
                        try:
                            with open(profile_path, "r", encoding="utf-8") as pf:
                                profile = json.load(pf)
                            log_info(
                                "[PROFILE-FALLBACK] %s: loaded from scrape dir (%s/%s)",
                                sku, game_dir.name, sku,
                            )
                        except Exception:
                            pass
                        break

        return profile

    TESTS = [
        # (label, sku, expect_fallback, expected_name_fragment)
        ("Test 1 — me4-1   (Chaos Rising, expect FALLBACK)", "me4-1",       True,  "Weedle"),
        ("Test 2 — me4-10  (Chaos Rising, expect FALLBACK)", "me4-10",      True,  "Ho-Oh"),
        ("Test 3 — me4-100 (Chaos Rising, expect FALLBACK)", "me4-100",     True,  "Greninja"),
        ("Test 4 — mtg-hob-110 (CardsDB, expect PRIMARY)",  "mtg-hob-110", False, None),
        ("Test 5 — base1-1 (original 135K, expect PRIMARY)", "base1-1",     False, None),
    ]

    overall = True

    for label, sku, expect_fallback, name_fragment in TESTS:
        log_lines.clear()
        print(f"\n{'='*62}")
        print(f"  {label}")
        print(f"  SKU: {sku}")

        profile = load_profile(sku)
        fallback_fired = any("[PROFILE-FALLBACK]" in l for l in log_lines)

        if not profile:
            status = "FAIL — profile is empty {}"
            overall = False
        else:
            name     = profile.get("name", "")
            set_name = profile.get("set_name", "")
            card_num = profile.get("card_number", "")
            prices   = profile.get("prices", {})

            price_sample = ""
            for src, sdata in prices.items():
                if isinstance(sdata, dict):
                    for variant, vdata in sdata.items():
                        val = None
                        if isinstance(vdata, dict):
                            val = vdata.get("market") or vdata.get("trend") or vdata.get("avg_sell")
                        elif isinstance(vdata, (int, float)):
                            val = vdata
                        if val:
                            price_sample = f"{src}/{variant}={val}"
                            break
                if price_sample:
                    break

            name_ok = (name_fragment is None) or (name_fragment.lower() in name.lower())
            fb_ok   = (fallback_fired == expect_fallback)

            status = "PASS" if (name and set_name and fb_ok and name_ok) else "FAIL"
            if status == "FAIL":
                overall = False

            print(f"  name:                {name}")
            print(f"  set_name:            {set_name}")
            print(f"  card_number:         {card_num}")
            print(f"  prices sample:       {price_sample or '(none)'}")
            print(f"  [PROFILE-FALLBACK]:  fired={fallback_fired}  expected={expect_fallback}  {'OK' if fb_ok else 'MISMATCH'}")

        print(f"  RESULT: {status}")

    print(f"\n{'='*62}")
    print(f"  Overall: {'ALL PASS' if overall else 'SOME FAILURES — see above'}")
    print(f"{'='*62}")


@app.local_entrypoint()
def main():
    test_profile_loading.remote()
