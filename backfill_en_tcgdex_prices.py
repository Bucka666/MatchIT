"""
backfill_en_tcgdex_prices.py — Backfill TCGplayer + Cardmarket prices for EN Pokémon
sets that pokemontcg.io has not yet populated, using the TCGdex EN API (free, no key).

Currently targets: me2pt5 (Ascended Heroes) → TCGdex ID prefix: me02.5
Designed to be re-runnable and set-scoped via the _SETID_MAP below.

Run (PowerShell, Craig's machine):
    $env:PYTHONIOENCODING="utf-8"; modal run backfill_en_tcgdex_prices.py::run_price_backfill

Dry-run (logs only, no writes):
    modal run backfill_en_tcgdex_prices.py::run_price_backfill --dry-run
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from pathlib import Path

import modal
import requests

# ── Set ID mapping: pokemontcg.io set_id → TCGdex set_id ──────────────────────
# Add new entries here when other EN sets need backfilling via this route.
_SETID_MAP = {
    "me2pt5": "me02.5",
    "me1":    "me01",
    "me2":    "me02",
    "me3":    "me03",
}

_TCGDEX_EN_URL = "https://api.tcgdex.net/v2/en/cards/{id}"

# Cardmarket field mapping — TCGdex key → profile.json key
# Matches the existing JP Cardmarket format written by scrape_pokemon_jpn.py
_CM_FIELD_MAP = [
    ("avg_sell", "avg"),
    ("low",      "low"),
    ("trend",    "trend"),
    ("avg_1d",   "avg1"),
    ("avg_7d",   "avg7"),
    ("avg_30d",  "avg30"),
]

# TCGplayer field mapping — TCGdex key → profile.json key
# Matches the existing EN TCGplayer format from scrape_pokemon_tcg.py (e.g. base1-4)
_TCP_FIELD_MAP = [
    ("market", "marketPrice"),
    ("mid",    "midPrice"),
    ("low",    "lowPrice"),
    ("high",   "highPrice"),
]


def _fetch_card_detail(tcgdex_id: str, timeout: int = 8) -> dict:
    """Fetch card detail from TCGdex EN API. Returns {} on 404 or failure."""
    url = _TCGDEX_EN_URL.format(id=urllib.parse.quote(tcgdex_id, safe=""))
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 404:
                return {}
            if r.status_code == 429:
                wait = 2 ** attempt
                logging.warning(f"[EN-BACKFILL] 429 on {tcgdex_id} — sleeping {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == 2:
                logging.warning(f"[EN-BACKFILL] Failed {tcgdex_id} after 3 attempts: {exc}")
                return {}
            time.sleep(1)
    return {}


def _build_price_fields(detail: dict) -> dict | None:
    """
    Extract TCGplayer + Cardmarket prices from a TCGdex EN card detail response.
    Uses the top-level 'pricing' field (primary variant).
    Returns None if no usable prices found.
    """
    pricing = detail.get("pricing") or {}

    # ── Cardmarket ────────────────────────────────────────────────────────────
    cm = pricing.get("cardmarket") or {}
    cm_prices = {}
    for out_key, src_key in _CM_FIELD_MAP:
        v = cm.get(src_key)
        if v is not None:
            cm_prices[out_key] = v

    # ── TCGplayer ─────────────────────────────────────────────────────────────
    # top-level pricing.tcgplayer has metadata keys (unit, updated) + variant dicts
    tcp_raw = pricing.get("tcgplayer") or {}
    tcp_prices = {}
    for variant_key, variant_data in tcp_raw.items():
        if variant_key in ("unit", "updated") or not isinstance(variant_data, dict):
            continue
        variant_prices = {}
        for out_key, src_key in _TCP_FIELD_MAP:
            v = variant_data.get(src_key)
            if v is not None:
                variant_prices[out_key] = v
        if variant_prices:
            tcp_prices[variant_key] = variant_prices

    if not cm_prices and not tcp_prices:
        return None

    result: dict = {
        "cardmarket": cm_prices,
        "tcgplayer":  tcp_prices,
    }
    if cm.get("idProduct"):
        result["cardmarket_id"] = str(cm["idProduct"])
    updated = cm.get("updated") or tcp_raw.get("updated")
    if updated:
        result["prices_updated"] = updated

    return result


def backfill_prices(db_root: Path, set_id: str = "me2pt5", dry_run: bool = False) -> dict:
    """
    Iterate all profile.json files for set_id, fetch prices from TCGdex EN,
    and write back. Atomic writes protect against corruption.
    """
    tcgdex_set_id = _SETID_MAP.get(set_id)
    if not tcgdex_set_id:
        raise ValueError(
            f"No TCGdex mapping for set_id={set_id!r}. Add to _SETID_MAP first."
        )

    prefix = f"{set_id}-"
    folders = sorted(
        d for d in (db_root / "pokemon").iterdir()
        if d.is_dir() and d.name.startswith(prefix)
    )
    logging.info(
        f"[EN-BACKFILL] Starting — set={set_id} tcgdex={tcgdex_set_id} "
        f"folders={len(folders)} dry_run={dry_run}"
    )

    updated = skipped = failed = 0

    for folder in folders:
        local_id = folder.name[len(prefix):]
        tcgdex_id = f"{tcgdex_set_id}-{local_id}"
        profile_path = folder / "profile.json"

        if not profile_path.exists():
            logging.warning(f"[EN-BACKFILL] Missing profile.json: {folder.name}")
            failed += 1
            continue

        detail = _fetch_card_detail(tcgdex_id)
        if not detail:
            logging.info(f"[EN-BACKFILL] No TCGdex data for {tcgdex_id} — skipping")
            failed += 1
            time.sleep(0.05)
            continue

        fields = _build_price_fields(detail)
        if not fields:
            logging.info(f"[EN-BACKFILL] No prices in TCGdex response for {tcgdex_id}")
            skipped += 1
            time.sleep(0.05)
            continue

        if not dry_run:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["prices"] = {
                "tcgplayer":  fields["tcgplayer"],
                "cardmarket": fields["cardmarket"],
            }
            if "cardmarket_id" in fields:
                profile["cardmarket_id"] = fields["cardmarket_id"]
            if "prices_updated" in fields:
                profile["prices_updated"] = fields["prices_updated"]

            # Atomic write — tmp then replace
            tmp = profile_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(profile_path)

        logging.info(f"[EN-BACKFILL] {'(dry) ' if dry_run else ''}Updated {folder.name}")
        updated += 1
        time.sleep(0.05)  # ~50ms between requests — matches JP backfill rate

    summary = {"updated": updated, "skipped": skipped, "failed": failed}
    logging.info(f"[EN-BACKFILL] Done — {summary}")
    return summary


# ── Modal entry point ──────────────────────────────────────────────────────────
_image = modal.Image.debian_slim().pip_install("requests")
_app   = modal.App("grailsweep-en-tcgdex-backfill")
_vol   = modal.Volume.from_name("matchit-data-v2", version=2)


@_app.function(image=_image, volumes={"/modal_data": _vol}, timeout=600)
def run_price_backfill(set_id: str = "me2pt5", dry_run: bool = False) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = backfill_prices(Path("/modal_data/CardsDB"), set_id=set_id, dry_run=dry_run)
    print(f"[EN-BACKFILL] Final: {result}")
    return result
