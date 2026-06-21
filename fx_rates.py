"""
fx_rates.py — single source of truth for USD/EUR -> GBP conversion rates.

get_fx() keeps a 300s in-process TTL cache so the hot path normally never
touches the volume — but on expiry it calls vol.reload() before re-reading,
so a long-warm replica still picks up the latest daily-refresh commit
instead of being stuck on a stale mount view. Confirmed live in production
(2026-06-21): without the reload, identical back-to-back requests flapped
between the live rate and the static fallback because Modal volumes don't
auto-propagate a commit from one container into an already-running
container's mounted view.

Two separate clocks, not to be conflated:
  - 300s TTL  = how often THIS replica re-checks the volume for a newer commit
  - 48h stale = how old the cached rate itself is allowed to be before we
                stop trusting it and fall back to static constants
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_FALLBACK = {"usd_gbp": 0.79, "eur_gbp": 0.86}
_STALE_SECONDS = 48 * 3600
_TTL_SECONDS = 300

_cache_lock = threading.Lock()
_cached = None
_cached_at = 0.0


def _fx_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", "/modal_data")
    return Path(base) / "fx_rates.json"


def _read_fx_file() -> dict:
    """Read+validate the cache file. Returns the static fallback if missing,
    unreadable, or older than 48h — never raises."""
    try:
        with open(_fx_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age > _STALE_SECONDS:
            return dict(_FALLBACK)
        return {"usd_gbp": data["usd_gbp"], "eur_gbp": data["eur_gbp"]}
    except Exception:
        return dict(_FALLBACK)


def get_fx() -> dict:
    """In-process cache, 300s TTL — hot path stays memory-only within the
    window. On expiry: best-effort vol.reload() (never raises — safe outside
    a Modal context too), then re-read. The fallback case is cached for the
    TTL window too, so a missing file doesn't force a reload on every call."""
    global _cached, _cached_at

    with _cache_lock:
        now = time.monotonic()
        if _cached is not None and (now - _cached_at) < _TTL_SECONDS:
            return _cached

        try:
            from modal_config import vol
            vol.reload()
        except Exception:
            pass  # local/non-Modal context, or reload unavailable — fall through to a plain read

        _cached = _read_fx_file()
        _cached_at = now
        return _cached


def refresh_fx_rates() -> dict:
    """Call frankfurter ONCE, derive the EUR->GBP cross-rate, and write the
    cache file atomically. Intended to run once daily — never call this from
    a per-request path."""
    import urllib.request

    req = urllib.request.Request(
        "https://api.frankfurter.app/latest?from=USD&to=GBP,EUR",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    rates = data.get("rates", {})
    usd_gbp = rates["GBP"]
    eur_gbp = rates["GBP"] / rates["EUR"]  # cross-rate derived ONCE, here

    payload = {
        "usd_gbp": usd_gbp,
        "eur_gbp": eur_gbp,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    path = _fx_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp_path, path)

    print(f"[FX] refreshed: usd_gbp={usd_gbp} eur_gbp={eur_gbp}", flush=True)
    return payload
