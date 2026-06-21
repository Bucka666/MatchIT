import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("app")

_RELOAD_TTL_SECONDS = 300
_reload_lock = threading.Lock()
_last_reload_at = 0.0


def _maybe_reload_volume():
    """Gate: reload the Modal volume mount at most once per 300s per
    replica, so committed profile.json changes (price refreshes, scheduler
    updates) become visible without forcing a reload on every profile read.
    Profile content is NOT cached here — only this reload call is gated;
    every profile read below stays exactly as fresh-per-call as before.
    Best-effort: a reload failure just means we proceed with the current
    mount view, same behaviour as before this existed."""
    global _last_reload_at
    now = time.monotonic()
    if (now - _last_reload_at) < _RELOAD_TTL_SECONDS:
        return
    with _reload_lock:
        if (time.monotonic() - _last_reload_at) < _RELOAD_TTL_SECONDS:
            return  # another thread just reloaded while we waited for the lock
        try:
            from modal_config import vol
            vol.reload()
        except Exception:
            pass
        _last_reload_at = time.monotonic()


def _load_card_profile_for_sku(sku, db_root, scrape_root):
    """Load profile.json for a card SKU.

    Primary path: {db_root}/{game}/{sku}/profile.json (CardsDB).
    Fallback:     {scrape_root}/{game}/{sku}/profile.json
                  (scheduler scrape dir, not yet synced to CardsDB).

    Returns {} if no profile found. Never raises.
    """
    if not sku:
        return {}

    _maybe_reload_volume()

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
