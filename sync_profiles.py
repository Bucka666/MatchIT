import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("app")


def _default_scrape_root() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, "MatchITv2_ProductMatch_Data", "cards")


def _default_cardsdb_root() -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, "CardsDB")


def sync_profiles_to_cardsdb(
    sets_by_game: dict,
    scrape_root: Optional[str] = None,
    cardsdb_root: Optional[str] = None,
    overwrite: bool = True,
) -> dict:
    """Copy scraped profile.json files into CardsDB.

    sets_by_game: {"pokemon": ["me4"], "mtg": ["abc"], ...}
                  Use {"pokemon": ["*"]} or {"mtg": ["*"]} to sync
                  every SKU under that game (used for manual backfill).
    Returns {"copied": N, "updated": N, "skipped": N, "errors": [str]}.
    Never raises.
    """
    scrape_root = scrape_root or _default_scrape_root()
    cardsdb_root = cardsdb_root or _default_cardsdb_root()
    result = {"copied": 0, "updated": 0, "skipped": 0, "errors": []}

    scrape_root_p = Path(scrape_root)
    cardsdb_root_p = Path(cardsdb_root)

    if not scrape_root_p.exists():
        result["errors"].append(f"scrape_root does not exist: {scrape_root}")
        return result

    for game, set_codes in sets_by_game.items():
        game_dir = scrape_root_p / game
        if not game_dir.is_dir():
            result["errors"].append(f"game dir missing in scrape root: {game}")
            continue

        # Build set of SKU dirs to consider
        if set_codes == ["*"]:
            sku_dirs = [d for d in game_dir.iterdir() if d.is_dir()]
        else:
            sku_dirs = []
            for d in game_dir.iterdir():
                if not d.is_dir():
                    continue
                if any(code.lower() in d.name.lower() for code in set_codes):
                    sku_dirs.append(d)

        for sku_dir in sku_dirs:
            src = sku_dir / "profile.json"
            if not src.exists():
                result["skipped"] += 1
                continue
            try:
                with open(src, "r", encoding="utf-8") as f:
                    json.load(f)  # validate JSON
            except Exception as e:
                result["errors"].append(f"{game}/{sku_dir.name}: corrupt source ({e})")
                continue

            dest_dir = cardsdb_root_p / game / sku_dir.name
            dest = dest_dir / "profile.json"
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                existed = dest.exists()
                shutil.copy2(src, dest)
                if existed and overwrite:
                    result["updated"] += 1
                elif not existed:
                    result["copied"] += 1
                else:
                    result["skipped"] += 1
            except Exception as e:
                result["errors"].append(f"{game}/{sku_dir.name}: copy failed ({e})")

    logger.info(
        "[PROFILE-SYNC] copied=%d updated=%d skipped=%d errors=%d",
        result["copied"], result["updated"], result["skipped"], len(result["errors"]),
    )
    return result


# ── Modal entrypoint (run against live volume) ─────────────────
# Usage:
#   modal run sync_profiles.py -- --game pokemon --sets me4
#   modal run sync_profiles.py -- --game pokemon --all-flag
#
# Note: --all-flag is used instead of --all because Modal CLI
# reserves --all. Internally maps to ["*"].
try:
    import sys as _sys
    _sys.path.insert(0, "/app")
    import modal as _modal
    from modal_config import vol as _vol, image as _modal_image
    _modal_app = _modal.App("grailsweep-sync-profiles")
    _HAS_MODAL = True
except Exception:
    _HAS_MODAL = False

if _HAS_MODAL:
    @_modal_app.function(
        image=_modal_image,
        volumes={"/modal_data": _vol},
        timeout=600,
    )
    def _run_sync_remote(game: str, sets: str = "", all_flag: bool = False) -> dict:
        import os
        os.environ["LOCALAPPDATA"] = "/modal_data"
        if all_flag:
            codes = ["*"]
        else:
            codes = [s.strip() for s in sets.split(",") if s.strip()]
            if not codes:
                return {"error": "must provide --sets or --all-flag"}
        sets_by_game = {game: codes}
        result = sync_profiles_to_cardsdb(sets_by_game)
        try:
            _vol.commit()
            result["vol_commit"] = "OK"
        except Exception as e:
            result["vol_commit"] = f"FAILED: {e}"
        print(f"[SYNC] {result}", flush=True)
        return result

    @_modal_app.local_entrypoint()
    def run_sync(game: str, sets: str = "", all_flag: bool = False):
        """Run profile sync against the live Modal volume."""
        result = _run_sync_remote.remote(game=game, sets=sets, all_flag=all_flag)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    # CLI: python sync_profiles.py --game pokemon --sets me4 me3
    # or:  python sync_profiles.py --game pokemon --all
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, choices=["pokemon", "mtg", "yugioh"])
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--sets", nargs="+", help="Set codes to sync")
    grp.add_argument("--all", action="store_true", help="Sync ALL SKUs for the game")
    args = ap.parse_args()

    sets_by_game = {args.game: ["*"] if args.all else args.sets}
    res = sync_profiles_to_cardsdb(sets_by_game)
    print(json.dumps(res, indent=2))
