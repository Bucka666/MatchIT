import json
import logging
from pathlib import Path

logger = logging.getLogger("app")


def _load_card_profile_for_sku(sku, db_root, scrape_root):
    """Load profile.json for a card SKU.

    Primary path: {db_root}/{game}/{sku}/profile.json (CardsDB).
    Fallback:     {scrape_root}/{game}/{sku}/profile.json
                  (scheduler scrape dir, not yet synced to CardsDB).

    Returns {} if no profile found. Never raises.
    """
    if not sku:
        return {}

    # Primary: CardsDB per-folder
    if db_root:
        try:
            for game_dir in Path(db_root).iterdir():
                if not game_dir.is_dir():
                    continue
                profile_path = game_dir / sku / "profile.json"
                if profile_path.exists():
                    try:
                        with open(profile_path, "r", encoding="utf-8") as pf:
                            return json.load(pf)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    # Fallback: scrape dir (scheduler-added sets)
    if scrape_root:
        try:
            for game_dir in Path(scrape_root).iterdir():
                if not game_dir.is_dir():
                    continue
                profile_path = game_dir / sku / "profile.json"
                if profile_path.exists():
                    try:
                        with open(profile_path, "r", encoding="utf-8") as pf:
                            profile = json.load(pf)
                        logger.info(
                            "[PROFILE-FALLBACK] %s: loaded from scrape dir (%s/%s)",
                            sku, game_dir.name, sku,
                        )
                        return profile
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    return {}
