"""
refresh_en_prices.py — Daily refresh of TCGplayer + Cardmarket prices for English
Pokémon cards via the TCGdex EN API (free, no key) for BOTH price blocks.

Mirrors scrape_pokemon_jpn.py's refresh pattern (bounded ThreadPoolExecutor,
write-only-when-prices-present, atomic writes) but for English (non-jpn-) cards
across all sets.

Set-id mapping: TCGdex set IDs differ from pokemontcg.io's (e.g. our rsv10pt5 ->
TCGdex sv10.5w "White Flare"). Built at runtime by normalized-name match against
TCGdex /v2/en/sets, with MANUAL_OVERRIDES for the known name-mismatch sets.

_fetch_card_detail and _build_price_fields are COPIED VERBATIM from
backfill_en_tcgdex_prices.py (standalone-script convention — no cross-import).

Run:
    modal run refresh_en_prices.py                 # full refresh on the live volume
    modal run refresh_en_prices.py --dry-run       # scope count, no per-card fetches
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import modal

# ── TCGdex EN endpoints ───────────────────────────────────────────────────────
_TCGDEX_EN_SETS_URL = "https://api.tcgdex.net/v2/en/sets"
_TCGDEX_EN_CARD_URL = "https://api.tcgdex.net/v2/en/cards/{id}"

# Cardmarket field mapping — TCGdex key → profile.json key
_CM_FIELD_MAP = [
    ("avg_sell", "avg"),
    ("low",      "low"),
    ("trend",    "trend"),
    ("avg_1d",   "avg1"),
    ("avg_7d",   "avg7"),
    ("avg_30d",  "avg30"),
]

# TCGplayer field mapping — TCGdex key → profile.json key
_TCP_FIELD_MAP = [
    ("market", "marketPrice"),
    ("mid",    "midPrice"),
    ("low",    "lowPrice"),
    ("high",   "highPrice"),
]

# pokemontcg.io set_id → TCGdex set_id for the sets whose names don't
# normalize-match TCGdex (the 11 resolvable misses; 3 absent sets — cel25c,
# sve, svp — are deliberately omitted and skip gracefully).
MANUAL_OVERRIDES = {
    "base1":       "base1",       # our "Base" vs TCGdex "Base Set"
    "hgss2":       "hgss2",       # our "HS—Unleashed" vs TCGdex "Unleashed"
    "hgss3":       "hgss3",       # "HS—Undaunted" vs "Undaunted"
    "hgss4":       "hgss4",       # "HS—Triumphant" vs "Triumphant"
    "fut20":       "fut2020",     # "Pokémon Futsal Collection" vs "Pokémon Futsal 2020"
    "swsh9tg":     "swsh9",       # Trainer Gallery subsets fold into the parent set
    "swsh10tg":    "swsh10",
    "swsh11tg":    "swsh11",
    "swsh12tg":    "swsh12",
    "swsh12pt5gg": "swsh12pt5",   # Galarian Gallery → Crown Zenith parent
    "swsh45sv":    "swsh45",      # Shiny Vault → Shining Fates parent
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ── COPIED VERBATIM from backfill_en_tcgdex_prices.py ─────────────────────────
def _fetch_card_detail(tcgdex_id: str, timeout: int = 8) -> dict:
    """Fetch card detail from TCGdex EN API. Returns {} on 404 or failure."""
    url = _TCGDEX_EN_CARD_URL.format(id=urllib.parse.quote(tcgdex_id, safe=""))
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 404:
                return {}
            if r.status_code == 429:
                wait = 2 ** attempt
                logging.warning(f"[EN-REFRESH] 429 on {tcgdex_id} — sleeping {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == 2:
                logging.warning(f"[EN-REFRESH] Failed {tcgdex_id} after 3 attempts: {exc}")
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
    # Per-source timestamps stored separately so freshness can be compared
    # (TCGdex gives each source its own 'updated'); prices_updated kept as the
    # combined value for the existing UI freshness label.
    cm_updated = cm.get("updated")
    tcp_updated = tcp_raw.get("updated")
    if cm_updated:
        result["cardmarket_updated"] = cm_updated
    if tcp_updated:
        result["tcgplayer_updated"] = tcp_updated
    updated = cm_updated or tcp_updated
    if updated:
        result["prices_updated"] = updated

    return result
# ── end verbatim copy (extended: per-source timestamps) ───────────────────────


def _build_setid_map(our_set_ids: list) -> dict:
    """Return {our_set_id: tcgdex_set_id} for English sets, via normalized-name
    match against TCGdex /v2/en/sets plus MANUAL_OVERRIDES. Unmatched sets are
    omitted (caller logs + skips them)."""
    try:
        r = requests.get(_TCGDEX_EN_SETS_URL, timeout=30,
                         headers={"User-Agent": "GrailSweep/1.0"})
        r.raise_for_status()
        tcgdex_sets = r.json()
    except Exception as exc:
        logging.warning(f"[EN-REFRESH] could not fetch TCGdex sets: {exc}")
        tcgdex_sets = []

    by_norm = {_norm(s.get("name")): s.get("id") for s in tcgdex_sets}
    # we still need set names to match by name — load from set_metadata below via caller
    mapping = {}
    for our_id, our_name in our_set_ids:
        if our_id in MANUAL_OVERRIDES:
            mapping[our_id] = MANUAL_OVERRIDES[our_id]
            continue
        tid = by_norm.get(_norm(our_name))
        if tid:
            mapping[our_id] = tid
    return mapping


def _is_fresh(profile: dict, hours: int = 23) -> bool:
    """True if the profile's stored prices_updated is within `hours` of now —
    used to skip cards already current and avoid re-hammering TCGdex."""
    pu = profile.get("prices_updated")
    if not pu:
        return False
    try:
        s = str(pu).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = datetime.strptime(str(pu)[:10], "%Y-%m-%d")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < timedelta(hours=hours)
    except Exception:
        return False


def _refresh_one(folder: Path, tcgdex_set_id: str, set_prefix: str, force: bool = False) -> dict:
    """Refresh a single English card. Never raises — all failures are caught
    and reported in the returned dict for the orchestrating pool."""
    profile_path = folder / "profile.json"
    if not profile_path.exists():
        return {"folder": folder.name, "status": "no_profile"}
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"folder": folder.name, "status": "error", "error": str(e)}

    if not force and _is_fresh(profile):
        return {"folder": folder.name, "status": "fresh_skip"}

    local_id = folder.name[len(set_prefix):]
    tcgdex_id = f"{tcgdex_set_id}-{local_id}"
    detail = _fetch_card_detail(tcgdex_id)
    if not detail:
        return {"folder": folder.name, "status": "no_data"}

    fields = _build_price_fields(detail)
    if not fields:
        # TCGdex returned no usable price — leave existing data intact.
        return {"folder": folder.name, "status": "no_price"}

    profile["prices"] = {
        "tcgplayer":  fields["tcgplayer"],
        "cardmarket": fields["cardmarket"],
    }
    if "cardmarket_id" in fields:
        profile["cardmarket_id"] = fields["cardmarket_id"]
    if "cardmarket_updated" in fields:
        profile["cardmarket_updated"] = fields["cardmarket_updated"]
    if "tcgplayer_updated" in fields:
        profile["tcgplayer_updated"] = fields["tcgplayer_updated"]
    if "prices_updated" in fields:
        profile["prices_updated"] = fields["prices_updated"]

    tmp = profile_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(profile_path)
    return {"folder": folder.name, "status": "refreshed"}


def refresh_en_prices(db_root: Path, dry_run: bool = False, max_workers: int = 8,
                      force: bool = False) -> dict:
    """Refresh prices for all English (non-jpn-) Pokémon cards from TCGdex EN.

    force=True bypasses the 23h freshness skip — needed to backfill new fields
    (e.g. per-source timestamps) across cards that were just refreshed and would
    otherwise all be skipped. The daily cron leaves force=False for efficiency."""
    pokemon_dir = db_root / "pokemon"

    # English POKEMON sets from set_metadata.json (keys are pokemontcg.io set_ids)
    meta_path = db_root.parent / "set_metadata.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[EN-REFRESH] could not load {meta_path}: {e}", flush=True)
        meta = {}
    en_sets = [
        (sid, e.get("name"))
        for sid, e in meta.items()
        if isinstance(e, dict) and e.get("game") == "POKEMON" and not sid.startswith("jpn-")
    ]
    print(f"[EN-REFRESH] {len(en_sets)} English sets in metadata", flush=True)

    setid_map = _build_setid_map(en_sets)
    unmapped = [sid for sid, _ in en_sets if sid not in setid_map]
    print(f"[EN-REFRESH] mapped {len(setid_map)} sets to TCGdex "
          f"({len(unmapped)} unmapped: {sorted(unmapped)})", flush=True)

    # Single directory listing — group folder names by mapped-set prefix in
    # memory. (Listing /modal_data/CardsDB/pokemon once instead of per-set: the
    # volume is a network FS, so one scandir vs ~170 is the whole performance
    # difference.) os.scandir's DirEntry.is_dir() avoids a per-entry stat.
    try:
        with os.scandir(pokemon_dir) as _it:
            all_names = [e.name for e in _it if e.is_dir()]
    except Exception as e:
        print(f"[EN-REFRESH] could not list {pokemon_dir}: {e}", flush=True)
        all_names = []
    folders_by_set = {}
    for our_id in setid_map:
        prefix = our_id + "-"
        names = [n for n in all_names if n.startswith(prefix)]
        if names:
            folders_by_set[our_id] = names

    # ── DRY RUN: pure local scope count — no per-card or per-set TCGdex calls,
    # no profile reads. Card counts come from counting folders per set. ──
    if dry_run:
        total = sum(len(v) for v in folders_by_set.values())
        scope = {
            "mapped_sets":     len(setid_map),
            "unmapped_sets":   len(unmapped),
            "sets_with_cards": len(folders_by_set),
            "total_cards":     total,
        }
        print(f"[EN-REFRESH] DRY-RUN scope (local counts, no per-card API): {scope}", flush=True)
        return scope

    stats = {"refreshed": 0, "no_data": 0, "no_price": 0, "fresh_skip": 0,
             "no_profile": 0, "errors": 0, "sets_done": 0, "sets_skipped": len(unmapped)}

    sorted_sets = sorted(folders_by_set)
    total_sets = len(sorted_sets)
    cards_seen = 0
    for i, our_id in enumerate(sorted_sets, 1):
        tcgdex_set_id = setid_map[our_id]
        prefix = our_id + "-"
        folders = [pokemon_dir / n for n in sorted(folders_by_set[our_id])]

        # Pre-set heartbeat so a slow set doesn't look frozen (per-card TCGdex
        # fetches mean a big set can take a while before its result line).
        print(f"[EN-REFRESH] Processing {our_id} -> {tcgdex_set_id} "
              f"({i}/{total_sets}, {len(folders)} cards, {cards_seen} done so far)...",
              flush=True)

        set_stats = {"refreshed": 0, "no_data": 0, "no_price": 0, "fresh_skip": 0}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for res in pool.map(lambda f: _refresh_one(f, tcgdex_set_id, prefix, force), folders):
                s = res["status"]
                if s == "refreshed":
                    stats["refreshed"] += 1; set_stats["refreshed"] += 1
                elif s == "no_data":
                    stats["no_data"] += 1; set_stats["no_data"] += 1
                elif s == "no_price":
                    stats["no_price"] += 1; set_stats["no_price"] += 1
                elif s == "fresh_skip":
                    stats["fresh_skip"] += 1; set_stats["fresh_skip"] += 1
                elif s == "no_profile":
                    stats["no_profile"] += 1
                elif s == "error":
                    stats["errors"] += 1

        cards_seen += len(folders)
        stats["sets_done"] += 1
        print(f"[EN-REFRESH] {our_id} done ({i}/{total_sets}, {cards_seen} cards total): "
              f"{set_stats}", flush=True)
        time.sleep(0.1)  # gentle between sets — respect TCGdex throttle

    print(f"[EN-REFRESH] Done. {stats}", flush=True)
    return stats


# ── Modal wrapper ─────────────────────────────────────────────────────────────
# Lightweight image (HTTP + JSON only) — matches backfill_en_tcgdex_prices.py.
# Own modal.App + inline vol so `modal run refresh_en_prices.py` works against the
# live volume without a modal_config import. /modal_data mount (NOT /data).
_image = modal.Image.debian_slim().pip_install("requests")
_app   = modal.App("grailsweep-en-price-refresh")
_vol   = modal.Volume.from_name("matchit-data-v2", version=2)


@_app.function(image=_image, volumes={"/modal_data": _vol}, timeout=5400)
def run_refresh(dry_run: bool = False, force: bool = False) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = refresh_en_prices(Path("/modal_data/CardsDB"), dry_run=dry_run, force=force)
    _vol.commit()
    print(f"[EN-REFRESH] volume committed: {result}", flush=True)
    return result


@_app.local_entrypoint()
def main(dry_run: bool = False, force: bool = False):
    print(run_refresh.remote(dry_run=dry_run, force=force))
