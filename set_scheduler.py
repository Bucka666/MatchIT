"""
set_scheduler.py — Automatic new set detection and ingestion for MatchIT
=========================================================================
Detects new sets for all three TCGs, downloads new card images,
runs incremental embedding, advances the release-calendar state machine
(set_release_calendar.json — pokemon_en only, see _advance_entry), runs the
pokemontcg.io-vs-TCGdex-EN source-latency probe, and sends an email summary.

Can be run:
    1. Manually:   python set_scheduler.py
    2. As a Modal cron job (see matchit_modal.py integration below)
    3. Per-TCG:    python set_scheduler.py --tcg POKEMON

Configuration:
    Edit the CONFIG block below, or set environment variables.

Modal integration (matchit_modal.py — actual decorator lives there; this is
illustrative only):

    @app.function(
        image=image,
        gpu="T4",
        volumes={"/modal_data": vol},
        schedule=modal.Cron("0 1 * * *"),  # Every day at 1am UTC
        timeout=3600,
    )
    def scheduled_set_check():
        import os, sys
        os.chdir("/app")
        sys.path.insert(0, "/app")
        os.environ["LOCALAPPDATA"] = "/modal_data"
        from set_scheduler import run_scheduler
        run_scheduler()
"""

# When Modal imports this module directly as a function entrypoint (e.g.
# `modal run set_scheduler.py::rebuild_mtg_totals`), no caller has set up
# sys.path yet — unlike scheduled_set_check in matchit_modal.py, which
# inserts /app before importing set_scheduler as a submodule. Must run
# before the log_config/email_sender imports below.
import os as _os_bootstrap
import sys as _sys_bootstrap
if _os_bootstrap.path.exists("/app") and "/app" not in _sys_bootstrap.path:
    _sys_bootstrap.path.insert(0, "/app")

import argparse
import importlib
import json
import logging
from log_config import configure_logging
configure_logging()
import os
import re
import ssl
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from email_sender import gs_send_email
from fx_rates import get_fx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONFIG — edit these or set as environment variables
# ─────────────────────────────────────────────────────────────

CONFIG = {
    # Email settings
    "email_to":       os.environ.get("MATCHIT_EMAIL_TO", "craig@grailsweep.com"),

    # DB root — where card images are stored
    "db_root": os.environ.get(
        "MATCHIT_DB_ROOT",
        str(Path(os.environ.get("LOCALAPPDATA", "/modal_data")) /
            "MatchITv2_ProductMatch_Data" / "cards"),
    ),

    # Pokémon TCG API key (optional — free tier works without)
    "pokemon_api_key": os.environ.get("POKEMONTCG_API_KEY", ""),

    # TCGs to check
    "tcgs_enabled": ["POKEMON", "MTG", "YUGIOH"],

    # MTG set types to include (Scryfall types)
    "mtg_set_types": ["core", "expansion", "masters", "draft_innovation"],

    # Pokémon sets known to be unavailable without paid API key
    "pokemon_known_unavailable": ["mcd14", "mcd15", "mcd17", "mcd18"],
}

# Calendar state-machine daily window: pokemon_en entries whose release_date
# is more than this many days from today (either direction) are skipped
# cheaply by _advance_entry, before any network call — narrows the daily
# tick to a check around imminent/recent releases instead of churning every
# tracked entry regardless of date.
CALENDAR_WINDOW_DAYS = 7


# FX conversion rates for alert price calculation.
# Read from the cached fx_rates.json (see fx_rates.py) — a FILE READ, not a
# live network call, so this still keeps scheduler runs independent of
# frankfurter.app's live availability (same intent as the old hardcoded
# constants this replaced). get_fx() fails open to {0.79, 0.86} if the cache
# is missing/stale, so behaviour is unchanged when nothing's been refreshed yet.


# ─────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────

def _get_data_dir() -> Path:
    localappdata = os.environ.get("LOCALAPPDATA", "/modal_data")
    return Path(localappdata) / "MatchITv2_ProductMatch_Data" / "cards"


def _get_images_db_path() -> Path:
    return _get_data_dir() / "images.db"


def _get_scheduler_log_path() -> Path:
    """JSON log of all scheduler runs — used for admin summary page."""
    return _get_data_dir() / "scheduler_log.json"


# ─────────────────────────────────────────────────────────────
# Release calendar — source of truth for *when* a set should be checked;
# pokemontcg.io / TCGdex remain the signal for *ready*. Craig seeds entries
# manually; the scheduler only ever advances `state` and stamps
# `source_latency` on what's already there.
# ─────────────────────────────────────────────────────────────

RELEASE_CALENDAR_FILENAME = "set_release_calendar.json"

RELEASE_CALENDAR_STATES = (
    "pending", "detected", "catalog_ingested",
    "indexed_server", "prelive_hold", "live",
    # "ondevice_staged" kept for backward/manual use only — the automatic
    # scheduler no longer transitions any set into it (see _advance_entry).
    "ondevice_staged",
)


def _get_release_calendar_path() -> Path:
    return _get_data_dir() / RELEASE_CALENDAR_FILENAME


def _default_release_calendar() -> Dict:
    return {"sets": [], "discovered_unlisted": []}


def _load_release_calendar() -> Dict:
    """Read-modify-write whole file, same pattern as
    _save_scheduler_log/load_scheduler_log below."""
    path = _get_release_calendar_path()
    if not path.exists():
        return _default_release_calendar()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            calendar = json.load(f)
    except Exception as e:
        logger.error(f"[SCHED] FATAL: set_release_calendar.json exists but failed to load — aborting to prevent state loss: {e}")
        raise
    calendar.setdefault("sets", [])
    calendar.setdefault("discovered_unlisted", [])
    return calendar


def _save_release_calendar(calendar: Dict) -> None:
    """Atomic write (tmp + os.replace) — same precedent as
    rebuild_mtg_set_totals()/rebuild_set_card_lists() in app.py. This file
    is rewritten on every daily tick; a half-write on a crash mid-save
    would corrupt every set's state for the next run, so it can't use the
    plain overwrite that _save_scheduler_log() uses for its append-only log."""
    path = _get_release_calendar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(calendar, f, indent=2)
    os.replace(tmp_path, path)


def _stamp_source_latency(entry: Dict, source: str, field: str) -> bool:
    """Set source_latency[source][field] to now (UTC ISO) only if it is
    still null. Returns True if this call just stamped it, False if an
    observation was already recorded — callers must never overwrite a
    non-null timestamp."""
    latency = entry.setdefault("source_latency", {})
    bucket = latency.setdefault(source, {"first_listed_at": None, "first_imaged_at": None})
    if bucket.get(field) is not None:
        return False
    bucket[field] = datetime.utcnow().isoformat() + "Z"
    return True


def _find_calendar_entry(calendar: Dict, game: str, source: str, source_id: Optional[str],
                          name: Optional[str] = None) -> Optional[Dict]:
    """Match a calendar entry by (game, source_id) when known; falls back to
    an exact case-insensitive name match within the same game when the
    source id for that source hasn't been observed/backfilled yet."""
    for entry in calendar.get("sets", []):
        if entry.get("game") != game:
            continue
        sid = entry.get("source_ids", {}).get(source)
        if sid and source_id and sid == source_id:
            return entry
    if name:
        name_lower = name.strip().lower()
        for entry in calendar.get("sets", []):
            if entry.get("game") == game and entry.get("name", "").strip().lower() == name_lower:
                return entry
    return None


# ─────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────

def _get_existing_skus(prefix: Optional[str] = None) -> Set[str]:
    """Return all SKUs in images.db, optionally filtered by prefix."""
    db_path = _get_images_db_path()
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    if prefix:
        rows = conn.execute(
            "SELECT DISTINCT sku FROM images WHERE sku LIKE ?",
            (f"{prefix}%",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT DISTINCT sku FROM images").fetchall()
    conn.close()
    return {row[0] for row in rows if row[0]}


def _get_existing_set_codes(tcg: str) -> Set[str]:
    skus = _get_existing_skus(
        prefix={"POKEMON": None, "MTG": "mtg-", "YUGIOH": "ygo-"}[tcg]
    )
    codes = set()
    for sku in skus:
        if tcg == "POKEMON":
            # SKU format: {set_id}-{card_number}
            # set_id never contains hyphens, card_number can be digits or alphanumeric
            # e.g. sv3pt5-123, basep-1, bwp-BW004, swsh9tg-TG01
            if "-" in sku:
                codes.add(sku.split("-")[0].lower())
        elif tcg == "MTG":
            parts = sku.split("-")
            if len(parts) >= 3:
                codes.add(parts[1].lower())
        elif tcg == "YUGIOH":
            # ygo-25LP-EN010-46396218 → parts[1] = 25LP
            parts = sku.split("-")
            if len(parts) >= 2:
                codes.add(parts[1].lower())
    return codes


# ─────────────────────────────────────────────────────────────
# New set detection — per TCG
# ─────────────────────────────────────────────────────────────

def _detect_new_pokemon_sets(excluded_ids: Optional[Set[str]] = None) -> List[Dict]:
    """
    Detect new Pokémon sets by comparing pokemontcg.io set list
    against what's already in the DB — without needing an API key.
    Uses the public unauthenticated endpoint with a browser User-Agent.

    excluded_ids: set ids already tracked in the release calendar
    (set_release_calendar.json) — these are governed exclusively by the
    calendar state machine (_advance_entry) so they're never auto-ingested
    here the instant pokemontcg.io lists them. Without this exclusion, a
    future-dated calendar entry would get scraped+embedded immediately by
    this legacy bulk path, defeating the calendar's release-date gate.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.pokemontcg.io/v2/sets?select=id,name,releaseDate,total",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        sets = data.get("data", [])
    except Exception as e:
        logger.error(f"[SCHED-PKM] API error: {e}")
        return []

    existing = _get_existing_set_codes("POKEMON")
    excluded = excluded_ids or set()
    new_sets = []
    for s in sets:
        set_id = s.get("id", "").lower()
        if set_id and set_id not in existing and set_id not in excluded:
            new_sets.append({
                "id":    s["id"],
                "name":  s.get("name", s["id"]),
                "date":  s.get("releaseDate", ""),
                "total": s.get("total", 0),
            })
    # Suppress known unavailable sets
    unavailable = set(CONFIG.get("pokemon_known_unavailable", []))
    new_sets = [s for s in new_sets if s["id"].lower() not in unavailable]
    new_sets.sort(key=lambda x: x["date"])
    logger.info(f"[SCHED-PKM] {len(new_sets)} new sets detected")
    return new_sets


def _detect_new_mtg_sets() -> List[Dict]:
    """Compare Scryfall set list against DB. Return new sets."""
    import urllib.request
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.scryfall.com/sets",
            headers={
                "User-Agent": "MatchIT/1.0 contact@matchit.app",
                "Accept": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        sets = data.get("data", [])
    except Exception as e:
        logger.error(f"[SCHED-MTG] Scryfall API error: {e}")
        return []

    existing  = _get_existing_set_codes("MTG")
    allowed   = set(CONFIG.get("mtg_set_types", []))
    new_sets  = []

    for s in sets:
        set_code  = s.get("code", "").lower()
        set_type  = s.get("set_type", "")
        set_date  = s.get("released_at", "")
        card_count = s.get("card_count", 0)

        if not set_code:
            continue
        if allowed and set_type not in allowed:
            continue
        if card_count == 0:
            continue
        if set_code not in existing:
            new_sets.append({
                "id":    s["code"],
                "name":  s.get("name", s["code"]),
                "date":  set_date,
                "total": card_count,
                "type":  set_type,
            })

    new_sets.sort(key=lambda x: x["date"])
    logger.info(f"[SCHED-MTG] {len(new_sets)} new sets detected")
    return new_sets


def _detect_new_yugioh_sets() -> List[Dict]:
    import subprocess
    try:
        result = subprocess.run(
            [sys.executable, "scrape_yugioh.py", "--list-sets"],
            capture_output=True, text=True, timeout=60,
            cwd=Path(__file__).parent,
        )
        lines = result.stdout.strip().splitlines()
    except Exception as e:
        logger.error(f"[SCHED-YGO] list-sets error: {e}")
        return []

    existing  = _get_existing_set_codes("YUGIOH")
    new_sets  = []

    # YGO set codes are 2-6 uppercase alphanumeric chars
    # e.g. MRD, MGED, 25LP, LOB2, WC4
    # They appear as a standalone token surrounded by spaces in the line
    set_code_re = re.compile(r'\b([A-Z0-9]{2,6})\b')

    for line in lines:
        line = line.strip()
        # Skip header, divider, summary lines
        if not line or line.startswith('-') or line.startswith('Name') \
                or 'sets,' in line.lower() or 'total' in line.lower():
            continue

        # Find date at the end — format YYYY-MM-DD or ?
        date_match = re.search(r'(\d{4}-\d{2}-\d{2}|\?)\s*$', line)
        tcg_date   = date_match.group(1) if date_match else ""

        # Find all uppercase alphanumeric tokens — the set code will be
        # the last one before the date (or end of line)
        line_no_date = line[:date_match.start()].strip() if date_match else line
        tokens = set_code_re.findall(line_no_date)

        if not tokens:
            continue

        # The set code is the last uppercase token before the date
        set_code  = tokens[-1]
        base_code = set_code.lower()

        # Skip if it looks like a card count (pure digits)
        if set_code.isdigit():
            continue

        # Skip if already in DB
        if base_code in existing:
            continue

        # Set name is everything before the set code token
        code_pos  = line_no_date.rfind(set_code)
        set_name  = line_no_date[:code_pos].strip()

        # Try to get card count — digit sequence after set code
        after_code = line_no_date[code_pos + len(set_code):].strip()
        count_match = re.search(r'(\d+)', after_code)
        total = int(count_match.group(1)) if count_match else 0

        new_sets.append({
            "id":    set_code,
            "name":  set_name,
            "date":  tcg_date,
            "total": total,
        })

    new_sets.sort(key=lambda x: x["date"] or "9999")
    logger.info(f"[SCHED-YGO] {len(new_sets)} new sets detected")
    return new_sets


# ─────────────────────────────────────────────────────────────
# Scraper invocation — per TCG
# ─────────────────────────────────────────────────────────────

def _scrape_pokemon(new_sets: List[Dict], db_root: str) -> Dict:
    """Download new Pokémon sets via scrape_pokemon_tcg.py."""
    result = {"downloaded": 0, "failed": 0, "sets": []}
    if not new_sets:
        return result

    set_ids = ",".join(s["id"] for s in new_sets)
    logger.info(f"[SCHED-PKM] Scraping sets: {set_ids}")

    try:
        # Import and call directly
        import scrape_pokemon_tcg
        old_argv = sys.argv[:]
        args = ["scrape_pokemon_tcg.py",
                "--db-root", db_root,
                "--set", set_ids,
                "--resume"]
        api_key = CONFIG.get("pokemon_api_key", "")
        if api_key:
            args += ["--api-key", api_key]
        sys.argv = args
        try:
            scrape_pokemon_tcg.main()
            result["sets"] = [s["id"] for s in new_sets]
            result["downloaded"] = sum(s.get("total", 0) for s in new_sets)
        finally:
            sys.argv = old_argv
    except Exception as e:
        logger.error(f"[SCHED-PKM] Scrape error: {e}")
        result["failed"] = len(new_sets)

    return result


def _scrape_mtg(new_sets: List[Dict], db_root: str) -> Dict:
    """Download new MTG sets via scrape_mtg.py."""
    result = {"downloaded": 0, "failed": 0, "sets": []}
    if not new_sets:
        return result

    set_codes = ",".join(s["id"] for s in new_sets)
    logger.info(f"[SCHED-MTG] Scraping sets: {set_codes}")

    try:
        import scrape_mtg
        old_argv = sys.argv[:]
        sys.argv = ["scrape_mtg.py",
                    "--db-root", db_root,
                    "--set", set_codes,
                    "--resume"]
        try:
            scrape_mtg.main()
            result["sets"] = [s["id"] for s in new_sets]
            result["downloaded"] = sum(s.get("total", 0) for s in new_sets)
        finally:
            sys.argv = old_argv
    except Exception as e:
        logger.error(f"[SCHED-MTG] Scrape error: {e}")
        result["failed"] = len(new_sets)

    return result


def _scrape_yugioh(new_sets: List[Dict], db_root: str) -> Dict:
    """Download new YGO sets via scrape_yugioh.py."""
    result = {"downloaded": 0, "failed": 0, "sets": []}
    if not new_sets:
        return result

    set_codes = ",".join(s["id"] for s in new_sets)
    logger.info(f"[SCHED-YGO] Scraping sets: {set_codes}")

    try:
        import scrape_yugioh
        old_argv = sys.argv[:]
        sys.argv = ["scrape_yugioh.py",
                    "--db-root", db_root,
                    "--set", set_codes,
                    "--resume"]
        try:
            scrape_yugioh.main()
            result["sets"] = [s["id"] for s in new_sets]
            result["downloaded"] = sum(s.get("total", 0) for s in new_sets)
        finally:
            sys.argv = old_argv
    except Exception as e:
        logger.error(f"[SCHED-YGO] Scrape error: {e}")
        result["failed"] = len(new_sets)

    return result


# ─────────────────────────────────────────────────────────────
# Email notification
# ─────────────────────────────────────────────────────────────

def _send_email(subject: str, body_html: str, body_text: str) -> bool:
    """Send summary email via Resend."""
    email_to = CONFIG.get("email_to", "")
    if not email_to:
        logger.warning("[SCHED] No email_to configured — skipping notification")
        return False
    if not os.environ.get("RESEND_API_KEY"):
        logger.warning("[SCHED] RESEND_API_KEY not set — skipping notification")
        return False
    try:
        gs_send_email(
            to=email_to,
            subject=subject,
            html=body_html,
            text=body_text,
        )
        logger.info(f"[SCHED] Email sent to {email_to}")
        return True
    except Exception as e:
        logger.error(f"[SCHED] Email failed: {e}")
        return False


def _build_email(run_summary: Dict, extra_html: str = "", extra_text: str = "") -> Tuple[str, str, str]:
    """Build subject, HTML body and plain text body from run summary.

    extra_html/extra_text (Tier 1 Part B): optional pre-rendered sections —
    ACTION NEEDED / progress / discovery — inserted at the TOP of the email,
    above the existing weekly content. Everything below is UNCHANGED from
    the original weekly summary, including the JP-manual reminder line."""
    now      = datetime.now().strftime("%d %b %Y %H:%M")
    total_new = sum(
        len(v.get("new_sets", []))
        for v in run_summary.get("tcgs", {}).values()
    )
    # "detected", not "added" — the legacy bulk loop is detect-and-flag only
    # for all three games now (see run_scheduler step 1); it never adds
    # anything to CardsDB itself. This base subject is usually overridden
    # below anyway when action_needed/has_flags fire, but must not claim
    # ingestion happened when it didn't.
    subject = (
        f"GrailSweep — {total_new} new set(s) detected [{now}]"
        if total_new > 0
        else f"GrailSweep — No new sets found [{now}]"
    )

    # Plain text
    lines = [f"GrailSweep Set Scheduler — {now}", "=" * 40, ""]
    if extra_text:
        lines.append(extra_text)
    for tcg, data in run_summary.get("tcgs", {}).items():
        lines.append(f"{tcg}:")
        new_sets = data.get("new_sets", [])
        if new_sets:
            for s in new_sets:
                lines.append(f"  + {s['id']} — {s['name']} ({s.get('total',0)} cards)")
        else:
            lines.append("  No new sets.")
        lines.append("")

    embed = run_summary.get("embed", {})
    lines += [
        f"Embeddings added: {embed.get('new_front', 0)} front",
        f"Total in DB:      {embed.get('total_front', 0)}",
        f"Elapsed:          {embed.get('elapsed_s', 0)}s",
    ]

    sync = run_summary.get("sync")
    if sync is not None:
        lines += [
            "",
            f"Profiles synced:  {sync.get('copied', 0)} new, "
            f"{sync.get('updated', 0)} updated, {sync.get('skipped', 0)} skipped",
        ]
        if sync.get("errors"):
            lines.append(f"Sync errors:      {len(sync['errors'])} (see logs)")

    _any_sets = any(data.get("new_sets") for data in run_summary.get("tcgs", {}).values())
    if _any_sets and embed.get("new_front", 0) == 0:
        lines += ["", "⚠️  WARNING: sets detected but no embeddings added — investigate registration step"]

    lines += [
        "",
        f"{total_new} new EN set(s) ingested this week. JP new-set ingest is "
        "MANUAL (TCGdex not polled) — if JP sets released, run the JP "
        "scrape/embed pipeline by hand.",
    ]

    body_text = "\n".join(lines)

    # HTML
    _any_sets_html = any(data.get("new_sets") for data in run_summary.get("tcgs", {}).values())
    warning_html = (
        '<div style="background:rgba(255,160,0,0.08);border:1px solid rgba(255,160,0,0.4);'
        'border-radius:8px;padding:12px 16px;margin-top:16px;font-size:0.85rem;color:#ffb300;">'
        '⚠️ <strong>Sets detected but no embeddings added.</strong> '
        'The registration step may have failed — check Modal logs and re-run '
        '<code>backfill_scraped_cards.py</code> if needed.'
        '</div>'
    ) if (_any_sets_html and embed.get("new_front", 0) == 0) else ""

    _sync = run_summary.get("sync")
    _sync_html = ""
    if _sync is not None:
        _sync_colour = "#ff5252" if _sync.get("errors") else "#00e5ff"
        _sync_html = (
            '<div style="background:rgba(0,229,255,0.05);border:1px solid rgba(0,229,255,0.15);'
            'border-radius:8px;padding:12px 16px;margin-top:16px;">'
            f'<div style="font-size:0.8rem;color:#9b95b8;">Profiles synced: '
            f'<strong style="color:#00ff88;">{_sync.get("copied", 0)} new</strong>, '
            f'<strong style="color:#ffd700;">{_sync.get("updated", 0)} updated</strong>, '
            f'{_sync.get("skipped", 0)} skipped</div>'
            + (
                f'<div style="font-size:0.8rem;color:{_sync_colour};">Sync errors: '
                f'<strong>{len(_sync["errors"])}</strong> (see logs)</div>'
                if _sync.get("errors") else ""
            )
            + '</div>'
        )

    rows = ""
    for tcg, data in run_summary.get("tcgs", {}).items():
        new_sets = data.get("new_sets", [])
        colour = "#00c853" if new_sets else "#9e9e9e"
        badge  = f'<span style="color:{colour};font-weight:700;">{len(new_sets)} new</span>'
        rows  += f"<tr><td style='padding:6px 12px;font-weight:700;'>{tcg}</td><td style='padding:6px 12px;'>{badge}</td><td style='padding:6px 12px;'>"
        if new_sets:
            rows += "<ul style='margin:0;padding-left:16px;'>"
            for s in new_sets:
                rows += f"<li>{s['id']} — {s['name']} ({s.get('total',0)} cards)</li>"
            rows += "</ul>"
        else:
            rows += "No new sets."
        rows += "</td></tr>"

    body_html = f"""
    <html><body style="font-family:'DM Sans',Arial,sans-serif;background:#1a1830;color:#f0ecff;padding:24px;">
    <div style="max-width:600px;margin:0 auto;background:#252248;border-radius:12px;padding:24px;border:1px solid rgba(177,77,255,0.2);">
      <h2 style="font-family:monospace;background:linear-gradient(90deg,#b14dff,#00e5ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-top:0;">
        GrailSweep — Set Scheduler
      </h2>
      <p style="color:#9b95b8;font-size:0.85rem;">{now}</p>
      {extra_html}
      <table style="width:100%;border-collapse:collapse;margin:16px 0;">
        <tr style="border-bottom:1px solid rgba(255,255,255,0.1);">
          <th style="text-align:left;padding:6px 12px;color:#9b95b8;font-size:0.75rem;text-transform:uppercase;">TCG</th>
          <th style="text-align:left;padding:6px 12px;color:#9b95b8;font-size:0.75rem;text-transform:uppercase;">Status</th>
          <th style="text-align:left;padding:6px 12px;color:#9b95b8;font-size:0.75rem;text-transform:uppercase;">Sets</th>
        </tr>
        {rows}
      </table>
      <div style="background:rgba(0,255,136,0.05);border:1px solid rgba(0,255,136,0.15);border-radius:8px;padding:12px 16px;margin-top:16px;">
        <div style="font-size:0.8rem;color:#9b95b8;">Embeddings added: <strong style="color:#00ff88;">{embed.get('new_front',0)}</strong></div>
        <div style="font-size:0.8rem;color:#9b95b8;">Total in DB: <strong style="color:#00e5ff;">{embed.get('total_front',0):,}</strong></div>
        <div style="font-size:0.8rem;color:#9b95b8;">Elapsed: <strong style="color:#ffd700;">{embed.get('elapsed_s',0)}s</strong></div>
      </div>
      {_sync_html}
      {warning_html}
      <div style="background:rgba(155,149,184,0.08);border:1px solid rgba(155,149,184,0.25);border-radius:8px;padding:12px 16px;margin-top:16px;font-size:0.8rem;color:#9b95b8;">
        {total_new} new EN set(s) ingested this week. JP new-set ingest is <strong>MANUAL</strong> (TCGdex not polled) —
        if JP sets released, run the JP scrape/embed pipeline by hand.
      </div>
    </div>
    </body></html>
    """
    return subject, body_html, body_text


# ─────────────────────────────────────────────────────────────
# Tier 1 — discovery notification + event-driven scheduler emails
# ─────────────────────────────────────────────────────────────

def _collect_transitions(run_summary: Dict) -> List[Dict]:
    """Real state changes only (from != to) out of this run's
    calendar.advanced list. No-op ticks (e.g. 'pending'->'pending',
    note='not_yet_listed', or a gate blocking with from==to) are excluded
    by construction — every _advance_entry no-op already returns from==to."""
    transitions = []
    for item in run_summary.get("calendar", {}).get("advanced", []):
        result = item.get("result") or {}
        frm, to = result.get("from"), result.get("to")
        if frm and to and frm != to:
            transitions.append({"name": item.get("name"), "from": frm, "to": to})
    return transitions


def _process_discovery_notifications(calendar: Dict) -> List[Dict]:
    """Tier 1 Part A — noise-controlled discovery alert.

    First run ever (discovery_baseline_done not set): silently marks every
    existing discovered_unlisted entry notified=True and sets the baseline
    flag. Returns [] — no email this run. This permanently silences the
    historical backlog (382 sets as of this deploy) — only entries
    discovered AFTER baseline can ever be notify-worthy.

    Every run after baseline: any entry with notified != True is
    notify-worthy. Returned for the email AND immediately marked
    notified=True here (mutates `calendar` in place — caller is
    responsible for persisting it), so a set can never be emailed twice
    even if a later run is quiet and sends nothing."""
    discovered = calendar.setdefault("discovered_unlisted", [])

    if not calendar.get("discovery_baseline_done"):
        for entry in discovered:
            entry["notified"] = True
        calendar["discovery_baseline_done"] = True
        logger.info(
            f"[DISCOVERY] Baseline established — {len(discovered)} existing "
            f"discovered_unlisted entries silently marked notified, no email"
        )
        return []

    notify_worthy = [e for e in discovered if not e.get("notified")]
    for e in notify_worthy:
        e["notified"] = True
    return notify_worthy


def _build_action_section(action_needed: List[Dict]) -> Tuple[str, str]:
    """ACTION NEEDED — manual go-live. Same 4 manual steps every time,
    mirroring how the existing weekly email already instructs the JP
    manual step verbatim (see the bottom of _build_email)."""
    if not action_needed:
        return "", ""
    text = "ACTION NEEDED — manual go-live\n" + "=" * 40 + "\n"
    for t in action_needed:
        text += f"  * {t['name']}\n"
    text += (
        "\nManual steps:\n"
        "  1. Run equivalence_harness.py yourself (canary regression check).\n"
        "  2. If you want the offline/on-device matcher updated, manually\n"
        "     rebuild + push the on-device index to R2 and bump the version.\n"
        "  3. Set 'ondevice_published' and 'equivalence_confirmed' for the\n"
        "     set in set_release_calendar.json.\n"
        "  4. Flip the set's state to 'live'.\n\n"
    )
    items = "".join(f"<li><strong>{t['name']}</strong></li>" for t in action_needed)
    html = (
        '<div style="background:rgba(255,82,82,0.08);border:2px solid #ff5252;'
        'border-radius:8px;padding:14px 18px;margin-bottom:16px;">'
        '<div style="color:#ff5252;font-weight:700;font-size:0.95rem;margin-bottom:8px;">'
        '⚠️ ACTION NEEDED — manual go-live</div>'
        f'<ul style="margin:0 0 10px 0;padding-left:18px;color:#f0ecff;">{items}</ul>'
        '<ol style="margin:0;padding-left:18px;color:#9b95b8;font-size:0.82rem;">'
        '<li>Run <code>equivalence_harness.py</code> yourself (canary regression check).</li>'
        '<li>If you want the offline/on-device matcher updated, manually rebuild + '
        'push the on-device index to R2 and bump the version.</li>'
        '<li>Set <code>ondevice_published</code> and <code>equivalence_confirmed</code> '
        'for the set in <code>set_release_calendar.json</code>.</li>'
        "<li>Flip the set's state to <code>live</code>.</li>"
        '</ol></div>'
    )
    return text, html


def _build_flagged_manual_section(flagged_manual: Dict) -> Tuple[str, str]:
    """ACTION NEEDED — new sets detected but NOT auto-ingested (legacy bulk
    loop's detect-and-flag policy for all three games). Distinct, additive
    reason from _build_action_section's prelive_hold go-live section above —
    both can appear in the same email. Mirrors the same section-builder
    pattern (empty in/out when there's nothing to report)."""
    if not flagged_manual or not any(flagged_manual.values()):
        return "", ""
    text = (
        "NEW SET(S) DETECTED — MANUAL INGESTION REQUIRED\n" + "=" * 40 + "\n"
        "These sets were detected upstream but NOT auto-ingested (policy).\n"
        "For Pokemon-EN, seed into set_release_calendar.json so the calendar\n"
        "state machine ingests them with the final-print gate. For MTG/YGO,\n"
        "trigger manual ingestion.\n"
    )
    for game, sets in flagged_manual.items():
        for s in sets:
            text += f"  - {game}: {s.get('id')} — {s.get('name')}\n"
    text += "\n"
    items = "".join(
        f"<li><strong>{game}</strong>: {s.get('id')} — {s.get('name')}</li>"
        for game, sets in flagged_manual.items()
        for s in sets
    )
    html = (
        '<div style="background:rgba(255,82,82,0.08);border:2px solid #ff5252;'
        'border-radius:8px;padding:14px 18px;margin-bottom:16px;">'
        '<div style="color:#ff5252;font-weight:700;font-size:0.95rem;margin-bottom:8px;">'
        '⚠️ NEW SET(S) DETECTED — MANUAL INGESTION REQUIRED</div>'
        '<div style="color:#9b95b8;font-size:0.82rem;margin-bottom:8px;">'
        'Detected upstream but NOT auto-ingested (policy). For Pokemon-EN, seed into '
        '<code>set_release_calendar.json</code> so the calendar state machine ingests '
        'them with the final-print gate. For MTG/YGO, trigger manual ingestion.</div>'
        f'<ul style="margin:0;padding-left:18px;color:#f0ecff;">{items}</ul></div>'
    )
    return text, html


def _build_progress_section(other_transitions: List[Dict]) -> Tuple[str, str]:
    """Informational-only progress (detected / catalog_ingested /
    indexed_server) — not action-needed, listed below the action section."""
    if not other_transitions:
        return "", ""
    text = "Progress this run:\n"
    for t in other_transitions:
        text += f"  * {t['name']}: {t['from']} -> {t['to']}\n"
    text += "\n"
    items = "".join(
        f"<li>{t['name']}: {t['from']} → {t['to']}</li>" for t in other_transitions
    )
    html = (
        '<div style="background:rgba(0,229,255,0.05);border:1px solid rgba(0,229,255,0.15);'
        'border-radius:8px;padding:12px 16px;margin-bottom:16px;">'
        '<div style="font-size:0.8rem;color:#9b95b8;margin-bottom:6px;">Progress this run:</div>'
        f'<ul style="margin:0;padding-left:18px;font-size:0.82rem;color:#f0ecff;">{items}</ul></div>'
    )
    return text, html


def _build_discovery_section(notify_worthy: List[Dict]) -> Tuple[str, str]:
    """New, never-before-notified discovered_unlisted entries only — the
    382-set historical backlog never reaches this (see
    _process_discovery_notifications baseline guard)."""
    if not notify_worthy:
        return "", ""
    text = "New sets discovered (not tracked):\n"
    for d in notify_worthy:
        text += (
            f"  * {d.get('name','?')} [{d.get('source')}] id={d.get('observed_id')} "
            f"cards={d.get('cardCount')} release={d.get('releaseDate') or '?'} "
            f"first_seen={d.get('first_seen_at')}\n"
        )
    text += "  Not tracked — add to set_release_calendar.json to start the pipeline.\n\n"
    items = "".join(
        f"<li><strong>{d.get('name','?')}</strong> [{d.get('source')}] "
        f"id={d.get('observed_id')} · cards={d.get('cardCount')} · "
        f"release={d.get('releaseDate') or '?'}</li>"
        for d in notify_worthy
    )
    html = (
        '<div style="background:rgba(255,215,0,0.06);border:1px solid rgba(255,215,0,0.3);'
        'border-radius:8px;padding:12px 16px;margin-bottom:16px;">'
        '<div style="font-size:0.85rem;color:#ffd700;font-weight:700;margin-bottom:6px;">'
        'New sets discovered (not tracked)</div>'
        f'<ul style="margin:0 0 8px 0;padding-left:18px;font-size:0.82rem;color:#f0ecff;">{items}</ul>'
        '<div style="font-size:0.78rem;color:#9b95b8;">Not tracked — add to '
        '<code>set_release_calendar.json</code> to start the pipeline.</div></div>'
    )
    return text, html


def _maybe_send_consolidated_email(
    run_summary: Dict,
    transitions: List[Dict],
    notify_worthy: List[Dict],
    is_monday: bool,
) -> bool:
    """Tier 1 Part B — exactly one consolidated email per run, or none.

    Monday: ALWAYS sends the existing weekly summary (_build_email,
    UNCHANGED content incl. the JP-manual line) — enriched with this run's
    ACTION NEEDED / flagged-manual / progress / discovery sections at the top.

    Non-Monday: sends a lightweight event digest if something happened
    (>=1 transition, >=1 notify-worthy discovery, OR >=1 newly flagged-manual
    set this run). Otherwise sends nothing — a quiet day produces zero emails.

    Subject priority (B4): ACTION NEEDED always wins (even on Monday,
    overriding the weekly subject) > Monday's own subject > non-Monday
    event-update subject. flagged_manual is its own ACTION NEEDED reason,
    additive to (not replacing) the existing prelive_hold go-live reason —
    both can fire in the same email."""
    action_needed      = [t for t in transitions if t["to"] == "prelive_hold"]
    other_transitions  = [t for t in transitions if t["to"] != "prelive_hold"]

    flagged    = run_summary.get("flagged_manual") or {}
    has_flags  = any(flagged.values())

    if not is_monday and not transitions and not notify_worthy and not has_flags:
        logger.info("[SCHED-EMAIL] Quiet non-Monday run — nothing happened, no email sent")
        return False

    action_text, action_html = _build_action_section(action_needed)
    flagged_text, flagged_html = _build_flagged_manual_section(flagged)
    progress_text, progress_html = _build_progress_section(other_transitions)
    discovery_text, discovery_html = _build_discovery_section(notify_worthy)

    extra_text = action_text + flagged_text + progress_text + discovery_text
    extra_html = action_html + flagged_html + progress_html + discovery_html

    if is_monday:
        subject, body_html, body_text = _build_email(run_summary, extra_html=extra_html, extra_text=extra_text)
    else:
        now = datetime.now().strftime("%d %b %Y %H:%M")
        subject = f"GrailSweep — Set update [{now}]"
        body_text = f"GrailSweep Set Scheduler — {now}\n" + "=" * 40 + "\n\n" + extra_text
        body_html = f"""
        <html><body style="font-family:'DM Sans',Arial,sans-serif;background:#1a1830;color:#f0ecff;padding:24px;">
        <div style="max-width:600px;margin:0 auto;background:#252248;border-radius:12px;padding:24px;border:1px solid rgba(177,77,255,0.2);">
          <h2 style="font-family:monospace;background:linear-gradient(90deg,#b14dff,#00e5ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-top:0;">
            GrailSweep — Set Scheduler
          </h2>
          <p style="color:#9b95b8;font-size:0.85rem;">{now}</p>
          {extra_html}
        </div>
        </body></html>
        """

    n_flagged = sum(len(v) for v in flagged.values())
    if action_needed and has_flags:
        subject = (
            f"[GrailSweep] ACTION: {len(action_needed)} ready for go-live + "
            f"{n_flagged} detected — manual ingestion needed"
        )
    elif action_needed:
        subject = (
            f"[GrailSweep] ACTION: {action_needed[0]['name']} ready for go-live"
            if len(action_needed) == 1
            else f"[GrailSweep] ACTION: {len(action_needed)} sets ready for go-live"
        )
    elif has_flags:
        subject = (
            f"[GrailSweep] ACTION: 1 new set detected — manual ingestion needed"
            if n_flagged == 1
            else f"[GrailSweep] ACTION: {n_flagged} new sets detected — manual ingestion needed"
        )
    elif not is_monday:
        if len(transitions) == 1:
            subject = f"[GrailSweep] Set update: {transitions[0]['name']} → {transitions[0]['to']}"
        elif transitions:
            subject = f"[GrailSweep] Set update: {len(transitions)} sets advanced"
        else:
            subject = f"[GrailSweep] Set update: {len(notify_worthy)} new set(s) discovered"

    return _send_email(subject, body_html, body_text)


# ─────────────────────────────────────────────────────────────
# Price alert checking
# ─────────────────────────────────────────────────────────────

def _send_alert_push(alert: Dict, current_gbp: float, reason: str) -> None:
    """Fire a Web Push notification for a price alert."""
    code = alert.get("code", "").strip().upper()
    if not code:
        return
    try:
        import json as _json
        push_subs_path = Path(os.environ.get("LOCALAPPDATA", "/modal_data")) / "push_subscriptions.json"
        if not push_subs_path.exists():
            push_subs_path = Path("/modal_data/push_subscriptions.json")
        if not push_subs_path.exists():
            return
        with open(push_subs_path, "r") as f:
            all_subs = _json.load(f)
        subs = all_subs.get(code, [])
        if not subs:
            return

        import json as _cj
        with open(Path(__file__).parent / "config.json") as _cf:
            cfg = _cj.load(_cf)
        vapid_private = cfg.get("vapid_private_key", "")
        if not vapid_private:
            return
        if vapid_private.startswith("-----"):
            import base64 as _b64
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.backends import default_backend as _dbe
            _key = load_pem_private_key(vapid_private.encode(), password=None, backend=_dbe())
            _raw = _key.private_numbers().private_value.to_bytes(32, "big")
            vapid_private = _b64.urlsafe_b64encode(_raw).rstrip(b"=").decode()

        from pywebpush import webpush, WebPushException
        card_name = alert.get("card_name", alert.get("sku", "Your card"))
        direction = "dropped below" if reason == "low" else "risen above"
        threshold = alert.get("target_low") if reason == "low" else alert.get("target_high")
        payload = _cj.dumps({
            "title": f"GrailSweep Price Alert 💰",
            "body":  f"{card_name} has {direction} £{threshold:.2f} — now £{current_gbp:.2f}",
            "url":   "/collection"
        })
        vapid_claims = {"sub": "mailto:grailsweep@gmail.com"}
        for sub in subs:
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=vapid_private,
                    vapid_claims=vapid_claims,
                )
            except WebPushException:
                pass
        logger.info(f"[ALERTS] Push sent to {len(subs)} device(s) for {code}")
    except Exception as e:
        logger.error(f"[ALERTS] Push failed: {e}")


def _send_alert_email(alert: Dict, current_gbp: float, reason: str) -> None:
    """Send a price alert email to the user via Resend."""
    email_to = alert.get("email", CONFIG.get("email_to", ""))

    if not email_to:
        logger.warning("[ALERTS] No recipient email — skipping alert")
        return
    if not os.environ.get("RESEND_API_KEY"):
        logger.warning("[ALERTS] RESEND_API_KEY not set — skipping alert")
        return

    card_name = alert.get("card_name", alert.get("sku", "Unknown card"))
    sku       = alert.get("sku", "")
    direction = "dropped below" if reason == "low" else "risen above"
    threshold = alert.get("target_low") if reason == "low" else alert.get("target_high")
    subject   = f"Price alert: {card_name} is now £{current_gbp:.2f}"

    body_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1830;background:#ffffff;">
        <h1 style="color:#1a1830;font-size:1.3rem;font-weight:600;margin:0 0 16px 0;">Price alert triggered</h1>
        <p style="margin:0 0 16px 0;line-height:1.5;"><strong>{card_name}</strong> has {direction} your target of £{threshold:.2f}.</p>
        <div style="background:#f6f4fb;border-left:3px solid #b14dff;border-radius:4px;padding:16px 20px;margin:20px 0;">
            <div style="font-size:1.4rem;font-weight:600;color:#1a1830;">£{current_gbp:.2f}</div>
            <div style="font-size:0.85rem;color:#5f5e5a;margin-top:4px;">Current market price</div>
        </div>
        <p style="margin:20px 0;">
            <a href="https://grailsweep.com/match" style="display:inline-block;background:#7c3aed;color:#ffffff;text-decoration:none;padding:10px 20px;border-radius:6px;font-weight:500;font-size:0.9rem;">View on GrailSweep</a>
        </p>
        <p style="color:#5f5e5a;font-size:0.9rem;line-height:1.5;margin:16px 0;">
            To manage your alerts, visit <a href="https://grailsweep.com" style="color:#7c3aed;text-decoration:none;">grailsweep.com</a>.
        </p>
        <hr style="border:none;border-top:1px solid #e5e2ed;margin:24px 0 16px 0;">
        <p style="font-size:0.8rem;color:#888780;margin:0;">
            GrailSweep — Trading card scanner and price reference<br>
            <a href="https://grailsweep.com" style="color:#7c3aed;text-decoration:none;">grailsweep.com</a>
        </p>
    </div>
    """

    body_text = (
        f"Price alert triggered\n\n"
        f"{card_name} has {direction} your target of £{threshold:.2f}.\n"
        f"Current market price: £{current_gbp:.2f}\n\n"
        f"View on GrailSweep: https://grailsweep.com/match\n\n"
        f"To manage your alerts, visit https://grailsweep.com\n"
    )

    try:
        gs_send_email(
            to=email_to,
            subject=subject,
            html=body_html,
            text=body_text,
        )
        logger.info(f"[ALERTS] Alert email sent to {email_to} for {sku}")
    except Exception as e:
        logger.error(f"[ALERTS] Failed to send alert email: {e}")


def check_price_alerts() -> None:
    """
    Read price_alerts.json, compute current GBP price for each alert
    by querying profiles in CardsDB, and fire email alerts when
    target_low or target_high thresholds are crossed.
    """
    alerts_path = Path("/modal_data/price_alerts.json") if Path("/modal_data").exists() else Path(__file__).parent / "price_alerts.json"
    if not alerts_path.exists():
        logger.info("[ALERTS] No price_alerts.json found — skipping")
        return

    try:
        with open(alerts_path, "r", encoding="utf-8") as f:
            alerts: List[Dict] = json.load(f)
    except Exception as e:
        logger.error(f"[ALERTS] Could not read price_alerts.json: {e}")
        return

    if not alerts:
        return

    # CardsDB root — same convention as the rest of the scheduler
    localappdata = os.environ.get("LOCALAPPDATA", "/modal_data")
    cards_db_root = Path(localappdata) / "CardsDB"
    # Modal fallback
    if not cards_db_root.exists():
        cards_db_root = Path("/modal_data/CardsDB")

    triggered: List[int] = []
    fx = get_fx()  # cache file read, not a live call — see fx_rates.py

    for i, alert in enumerate(alerts):
        sku = alert.get("sku", "")
        if not sku:
            continue

        # Resolve profile.json — path convention: CardsDB/{game}/{sku}/profile.json
        # Determine game prefix
        if sku.startswith("mtg-"):
            game = "mtg"
        elif sku.startswith("ygo-"):
            game = "yugioh"
        else:
            game = "pokemon"

        profile_path = cards_db_root / game / sku / "profile.json"
        if not profile_path.exists():
            logger.debug(f"[ALERTS] No profile for {sku} at {profile_path}")
            continue

        try:
            with open(profile_path, "r", encoding="utf-8") as pf:
                profile = json.load(pf)
        except Exception:
            continue

        # Compute best GBP price from the profile.
        # Mirrors the scanner logic in templates/results.html — iterates every source,
        # picks max across all variants/price-points, skips ebay/amazon.
        # Handles two shapes: source.variant.{market,trend,avg_sell,mid} (nested) and
        # source.{price_point} (flat — Pokémon cardmarket).
        prices = profile.get("prices", {})
        best_gbp: float = 0.0

        for src, sdata in prices.items():
            src_lower = src.lower()
            if "ebay" in src_lower or "amazon" in src_lower:
                continue
            if not isinstance(sdata, dict):
                continue
            rate = fx["eur_gbp"] if "cardmarket" in src_lower else fx["usd_gbp"]

            for variant, vdata in sdata.items():
                pr = 0.0
                if isinstance(vdata, dict):
                    pr = (vdata.get("market") or vdata.get("trend")
                          or vdata.get("avg_sell") or vdata.get("mid") or 0)
                elif isinstance(vdata, (int, float)):
                    pr = vdata
                if pr and pr > 0:
                    gbp_candidate = float(pr) * rate
                    if gbp_candidate > best_gbp:
                        best_gbp = gbp_candidate

        gbp: Optional[float] = round(best_gbp, 2) if best_gbp > 0 else None
        logger.info(f"[ALERT-PRICE] sku={sku} gbp={gbp}")

        if gbp is None:
            logger.debug(f"[ALERTS] No price data for {sku}")
            continue

        reason: Optional[str] = None
        target_low  = alert.get("target_low")
        target_high = alert.get("target_high")

        if target_low is not None and gbp <= float(target_low):
            reason = "low"
        elif target_high is not None and gbp >= float(target_high):
            reason = "high"

        if reason:
            logger.info(f"[ALERTS] Triggering {reason} alert for {sku} @ £{gbp}")
            _send_alert_email(alert, gbp, reason)
            _send_alert_push(alert, gbp, reason)
            triggered.append(i)

    # Remove one-shot triggered alerts (keep repeating ones if flagged)
    if triggered:
        remaining = [a for j, a in enumerate(alerts) if j not in triggered]
        try:
            with open(alerts_path, "w", encoding="utf-8") as f:
                json.dump(remaining, f, indent=2)
            logger.info(f"[ALERTS] {len(triggered)} alert(s) fired and removed")
        except Exception as e:
            logger.error(f"[ALERTS] Could not save updated alerts: {e}")


# ─────────────────────────────────────────────────────────────
# Scheduler log (for admin summary page)
# ─────────────────────────────────────────────────────────────

def _save_scheduler_log(run_summary: Dict) -> None:
    """Append this run to the scheduler log JSON."""
    log_path = _get_scheduler_log_path()
    history  = []
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append(run_summary)
    # Keep last 100 runs
    history = history[-100:]

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    logger.info(f"[SCHED] Scheduler log saved to {log_path}")


def load_scheduler_log() -> List[Dict]:
    """Load scheduler run history — used by admin summary page."""
    log_path = _get_scheduler_log_path()
    if not log_path.exists():
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# Main scheduler run
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# Release-calendar state machine (pokemon_en only this block — see
# "Source decision: do not deviate" in the refactor instructions. MTG/YGO
# keep flowing through the legacy bulk detect+ingest path above unchanged).
# ─────────────────────────────────────────────────────────────

def _in_release_window(entry: Dict, today: str) -> bool:
    """release_date-2d through release_date+14d — controls how much probe
    work an entry gets per tick (Phase 2/6 cost control), not whether it can
    advance state (that's the final-print gate in _try_index_server)."""
    rd = entry.get("release_date")
    if not rd:
        return False
    try:
        rd_date = datetime.strptime(rd, "%Y-%m-%d").date()
        today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (rd_date - timedelta(days=2)) <= today_date <= (rd_date + timedelta(days=14))


def _pokemontcg_set_lookup(set_id: str) -> Optional[Dict]:
    """Direct single-set lookup (unauthenticated, same UA pattern as
    _detect_new_pokemon_sets) — used by both the pending->detected check
    and the Phase 6 latency probe."""
    import urllib.request, urllib.parse
    try:
        req = urllib.request.Request(
            f"https://api.pokemontcg.io/v2/sets/{urllib.parse.quote(set_id)}",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("data") or None
    except Exception:
        return None


def _pokemontcg_first_card_imaged(set_id: str) -> Optional[bool]:
    """Cheap existence check: does at least one card in this set have
    image_url_large populated yet? Same field shape build_profile() reads.
    Used by both the final-print gate and the Phase 6 latency probe.

    Three-state, so an upstream outage cannot masquerade as a genuinely
    unimaged set. The old `except Exception: return False` meant any 500
    or network blip stalled a set at catalog_ingested indefinitely while
    logging a misleading "no images yet" (observed 2026-07-28, when
    pokemontcg.io's ?q= endpoints 500'd and held Pitch Black back after
    its cards had already been scraped successfully).

      True  — at least one card has a large image
      False — 200 from the API, but no cards / no image yet (genuine)
      None  — upstream error (non-200, network, parse); state unknown,
              caller should retry next tick rather than treat as blocked

    Note both are falsy: callers that only care about the positive case
    (e.g. the latency probe's first_imaged_at stamp) can keep using a
    plain truth test and correctly decline to stamp on an unknown."""
    import urllib.request, urllib.parse, urllib.error
    try:
        q = urllib.parse.quote(f"set.id:{set_id}")
        req = urllib.request.Request(
            f"https://api.pokemontcg.io/v2/cards?q={q}&pageSize=1",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                logger.warning(f"[INGEST-GATE] set_id={set_id} upstream_error status={resp.status}")
                return None
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"[INGEST-GATE] set_id={set_id} upstream_error status={e.code}")
        return None
    except Exception as e:
        logger.warning(f"[INGEST-GATE] set_id={set_id} upstream_error status={type(e).__name__}")
        return None
    cards = data.get("data") or []
    if not cards:
        logger.info(f"[INGEST-GATE] set_id={set_id} genuine_empty — no cards at source")
        return False
    return bool(cards[0].get("images", {}).get("large"))


def _tcgdex_en_listing() -> List[Dict]:
    """sdk.set.listSync() under Language.EN — ONE call per tick, shared
    across every pending entry (Phase 6 cost control). Returns [] on any
    SDK/network failure — observation-only, never raises into the run."""
    try:
        from tcgdexsdk import TCGdex, Language
        sdk = TCGdex(Language.EN)
        sets = sdk.set.listSync()
        return [{"id": s.id, "name": s.name,
                 "total": (s.cardCount.total if s.cardCount else None)} for s in sets]
    except Exception as e:
        logger.error(f"[PROBE] TCGdex-EN listSync failed: {e}")
        return []


def _tcgdex_en_first_card_imaged(set_id: str) -> bool:
    """getSync(set_id).cards[0].image — NOT _fetch_card_detail (recon
    confirmed that endpoint is pricing-only). False on any failure."""
    try:
        from tcgdexsdk import TCGdex, Language
        sdk = TCGdex(Language.EN)
        card_set = sdk.set.getSync(set_id)
        if not card_set or not card_set.cards:
            return False
        return bool(card_set.cards[0].image)
    except Exception:
        return False


def _collect_skus_for_set(set_id: str, cardsdb_root: str) -> List[str]:
    """Folder-name scan for one Pokemon set's SKUs in the CardsDB tree
    (post sync_profiles_to_cardsdb) — same matching rule as
    _get_existing_set_codes, inverted (one known set_id -> its SKUs).
    Used to report new_skus so matchit_modal.py's rebuild_lookup_files
    delta call still gets an accurate SKU list for calendar-driven sets."""
    game_dir = os.path.join(cardsdb_root, "pokemon")
    if not os.path.isdir(game_dir):
        return []
    target = set_id.lower()
    skus = []
    for name in os.listdir(game_dir):
        if name.startswith("_"):
            continue
        parts = name.split("-")
        if not parts:
            continue
        poke_set_id = ("jpn-" + "-".join(parts[1:-1]).lower()) if (parts[0].lower() == "jpn" and len(parts) >= 3) else parts[0].lower()
        if poke_set_id == target:
            skus.append(name)
    return skus


def _try_detect(entry: Dict) -> Optional[Dict]:
    """pending -> detected: set appears in TCGdex-EN (preferred) or
    pokemontcg.io (fallback) with a non-zero card count."""
    tcgdex_id = entry.get("source_ids", {}).get("tcgdex_en")
    if tcgdex_id:
        try:
            from tcgdexsdk import TCGdex, Language
            sdk = TCGdex(Language.EN)
            card_set = sdk.set.getSync(tcgdex_id)
            if card_set and (card_set.cardCount.total if card_set.cardCount else 0) > 0:
                return {
                    "id": card_set.id,
                    "name": card_set.name or entry.get("name"),
                    "total": card_set.cardCount.total,
                    "date": card_set.releaseDate or "",
                }
        except Exception as e:
            logger.error(f"[SCHED-STATE] TCGdex set lookup failed for {tcgdex_id}: {e}")

    set_id = entry.get("source_ids", {}).get("pokemontcg_io")
    if not set_id:
        return None
    info = _pokemontcg_set_lookup(set_id)
    if info and (info.get("total") or 0) > 0:
        return {
            "id": info.get("id"),
            "name": info.get("name", entry.get("name")),
            "total": info.get("total"),
            "date": info.get("releaseDate", ""),
        }
    return None


def _try_catalog_ingest(entry: Dict, detected: Dict, db_root: str, dry_run: bool) -> Dict:
    """detected -> catalog_ingested (Phase 3): scrape + mirror + lookup
    rebuilds, scoped to exactly this one set — same pieces run_scheduler's
    old steps 1/2b/2c/2d used for "all new sets today", now per-entry. Closes
    the identifier_lookup.json gap (it's now rebuilt here alongside the
    other three sidecars, where previously it was manual-only)."""
    pokemontcg_id = entry.get("source_ids", {}).get("pokemontcg_io")
    set_id = pokemontcg_id or detected["id"]
    result: Dict = {"set_id": set_id, "new_skus": []}

    if dry_run:
        result["dry_run"] = True
        return result

    scrape_entry = dict(detected, id=set_id)
    dl = _scrape_pokemon([scrape_entry], db_root)
    result.update(dl)

    try:
        from backfill_scraped_cards import register_scraped_cards
        reg = register_scraped_cards(tcg="pokemon", sets=[set_id])
        result["registered"] = reg.get("pokemon", {})
    except Exception as e:
        logger.error(f"[SCHED-STATE] Registration error for {set_id}: {e}")
        result["registered_error"] = str(e)
        return result  # leave at 'detected' — retry next tick

    try:
        from sync_profiles import sync_profiles_to_cardsdb, _default_cardsdb_root
        result["sync"] = sync_profiles_to_cardsdb({"pokemon": [set_id]})
        result["new_skus"] = _collect_skus_for_set(set_id, _default_cardsdb_root())
    except Exception as e:
        logger.error(f"[SCHED-STATE] Profile sync error for {set_id}: {e}")
        result["sync_error"] = str(e)
        return result  # leave at 'detected' — retry next tick

    try:
        from matchit_modal import rebuild_lookup_files
        result["lookup_rebuild"] = rebuild_lookup_files.remote(
            new_set_ids=[set_id]
        )
    except Exception as e:
        logger.error(
            f"[SCHED-STATE] lookup rebuild failed for {set_id}: {e}"
        )
        result["lookup_rebuild_error"] = str(e)

    result["ok"] = True
    return result


def _try_index_server(entry: Dict, today: str, dry_run: bool, embed_fn=None) -> Dict:
    """catalog_ingested -> indexed_server: the final-print gate (Phase 4).
    Indexes only once BOTH are true: catalog images are populated, AND
    today >= release_date. Catalog/display freshness (already live as of
    catalog_ingested) and index freshness are deliberately decoupled — this
    gate blocks only the embed step, never the earlier catalog pull."""
    release_date = entry.get("release_date", "")
    if not release_date or today < release_date:
        return {"ok": False, "gate": "pre_release", "release_date": release_date, "today": today}

    set_id = entry.get("source_ids", {}).get("pokemontcg_io")
    if not set_id:
        return {"ok": False, "gate": "no_images_yet"}

    imaged = _pokemontcg_first_card_imaged(set_id)
    if imaged is None:
        # Upstream error — we genuinely don't know whether images exist, so
        # don't report a definitive block. Same no-op effect this tick (state
        # is unchanged either way), but the distinct gate makes a stalled set
        # attributable to the API rather than looking like a real gate miss.
        return {"ok": False, "gate": "upstream_unavailable", "retry_next_tick": True}
    if not imaged:
        return {"ok": False, "gate": "no_images_yet"}

    if dry_run:
        return {"ok": True, "dry_run": True}

    # GUARDRAIL: incremental_embed is APPEND-ONLY (~new cards). NEVER replace
    # with a full whole-index re-embed on any automatic/cron path — full GPU
    # re-embed is manual-only (cost).
    try:
        if embed_fn is not None:
            embed_result = embed_fn(hot_reload=True)
        else:
            from incremental_embed import run_incremental_embed
            embed_result = run_incremental_embed(hot_reload=True)
    except Exception as e:
        logger.error(f"[SCHED-STATE] Embed error for {entry.get('name')}: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "embed": embed_result}


def _notify_ondevice_staged(entry: Dict, stage_result: Dict) -> bool:
    """NOT CALLED FROM ANY AUTOMATIC PATH — kept for manual/future use only.
    The on-device build (build_ondevice_index.py) is a manual, Craig-run
    tool on his own cadence; _advance_entry no longer invokes
    _try_ondevice_stage below. If this is ever called again, the original
    intent stands: reuse the existing _send_email/Resend path to tell Craig
    a set is staged and needs his manual R2 publish + version bump +
    equivalence_harness.py confirmation."""
    name = entry.get("name", "?")
    count = stage_result.get("count", "?")
    out_dir = stage_result.get("out_dir", "?")
    version = stage_result.get("index_version", "?")
    subject = f"GrailSweep — on-device index staged for {name}"
    text = (
        f"On-device MobileCLIP2-S2 index staged for: {name}\n\n"
        f"Vector count: {count}\n"
        f"Staged at: {out_dir}\n"
        f"Index version (single constant, ondevice_version.py): {version}\n\n"
        f"Manual steps required before this set goes live:\n"
        f"  1. Upload {out_dir}/vectors_f16.npy, skus.json, meta.json to\n"
        f"     models.grailsweep.com/gs-ondevice-v1/ (R2).\n"
        f"  2. If bumping the version, edit ONDEVICE_INDEX_VERSION in\n"
        f"     ondevice_version.py and redeploy.\n"
        f"  3. Run equivalence_harness.py against the live deployment and\n"
        f"     confirm no decision regressions on the 150-image canary set.\n"
        f"  4. Set this entry's 'ondevice_published' and\n"
        f"     'equivalence_confirmed' flags to true in\n"
        f"     set_release_calendar.json.\n\n"
        f"The scheduler will not flip this set live until both flags are set.\n"
    )
    html = "<pre>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    return _send_email(subject, html, text)


def _try_ondevice_stage(entry: Dict, dry_run: bool) -> Dict:
    """NOT CALLED FROM ANY AUTOMATIC PATH — kept for manual/future use only
    (see Option B: on-device build removed from the auto-scheduler). The
    full ~102k-card MobileCLIP re-embed in build_ondevice_index.py has no
    incremental path and must not run unattended; Craig rebuilds and
    publishes it himself on his own cadence. _advance_entry's
    indexed_server transition goes straight to 'prelive_hold' now and
    never calls this. Left in place in case a future manual trigger or
    CLI wants it; does NOT upload to R2 or bump the live version either
    way — that stays manual regardless of caller."""
    if not entry.get("ondevice_eligible", False):
        return {"ok": True, "skipped": "not_ondevice_eligible"}

    if dry_run:
        return {"ok": True, "dry_run": True}

    try:
        import build_ondevice_index
        stage_result = build_ondevice_index.main()
    except Exception as e:
        logger.error(f"[SCHED-STATE] On-device stage error for {entry.get('name')}: {e}")
        return {"ok": False, "error": str(e)}

    try:
        _notify_ondevice_staged(entry, stage_result or {})
    except Exception as e:
        logger.error(f"[SCHED-STATE] On-device notify email error: {e}")

    return {"ok": True, "staged": stage_result}


def _try_go_live(entry: Dict, dry_run: bool) -> Dict:
    """prelive_hold -> live (also reachable from the legacy 'ondevice_staged'
    state for backward/manual use). Gated on Craig's manual flags only — no
    auto-trust. Both flags must be explicitly set in the calendar file."""
    if entry.get("ondevice_eligible", False) and not entry.get("ondevice_published", False):
        return {"ok": False, "gate": "awaiting_manual_ondevice_publish"}
    if not entry.get("equivalence_confirmed", False):
        return {"ok": False, "gate": "awaiting_manual_equivalence_confirmation"}
    if dry_run:
        return {"ok": True, "dry_run": True}
    return {"ok": True}


def _advance_entry(entry: Dict, today: str, db_root: str, dry_run: bool, embed_fn=None) -> Dict:
    """Attempt exactly one state transition for this calendar entry.
    Idempotent: any failure leaves `state` unchanged so the next daily tick
    retries — never half-advances. Scoped to game == 'pokemon_en' this
    block (Source decision: do not deviate); MTG/YGO keep flowing through
    the legacy bulk path in run_scheduler unchanged."""
    if entry.get("game") != "pokemon_en":
        return {"ok": False, "skipped": "game_not_supported_by_calendar_state_machine"}

    # ±CALENDAR_WINDOW_DAYS window: skip cheaply (no network call) for any
    # entry whose release_date is far from today in either direction. A
    # missing/unparseable release_date fails open (does NOT skip) — a
    # newly-seeded entry with no date yet should stay visible, same as
    # today's behaviour.
    release_date = entry.get("release_date", "")
    if release_date:
        try:
            rd = datetime.strptime(release_date, "%Y-%m-%d")
            td = datetime.strptime(today, "%Y-%m-%d")
            if abs((td - rd).days) > CALENDAR_WINDOW_DAYS and not entry.get("bypass_window"):
                return {"ok": True, "skipped": "outside_window", "release_date": release_date, "today": today}
        except ValueError:
            pass  # unparseable date — fail open, let it through

    state = entry.get("state", "pending")

    if state == "pending":
        detected = _try_detect(entry)
        if detected:
            if not dry_run:
                entry["state"] = "detected"
            return {"ok": True, "from": "pending", "to": "detected", "detected": detected}
        return {"ok": True, "from": "pending", "to": "pending", "note": "not_yet_listed"}

    if state == "detected":
        detected = _try_detect(entry)
        if not detected:
            # Was detected before, isn't visible right now (transient API
            # hiccup) — stay put, retry tomorrow rather than regress state.
            return {"ok": True, "from": "detected", "to": "detected", "note": "recheck_failed"}
        result = _try_catalog_ingest(entry, detected, db_root, dry_run)
        new_skus = result.pop("new_skus", [])
        if result.get("ok") and not dry_run:
            entry["state"] = "catalog_ingested"
            return {"ok": True, "from": "detected", "to": "catalog_ingested",
                     "new_skus": new_skus, **result}
        return {"ok": result.get("ok", dry_run), "from": "detected", "to": "detected",
                 "new_skus": new_skus, **result}

    if state == "catalog_ingested":
        result = _try_index_server(entry, today, dry_run, embed_fn=embed_fn)
        if result.get("ok") and not dry_run:
            entry["state"] = "indexed_server"
            return {"ok": True, "from": "catalog_ingested", "to": "indexed_server", **result}
        return {"ok": result.get("ok", False), "from": "catalog_ingested", "to": "catalog_ingested", **result}

    if state == "indexed_server":
        # Option B: on-device build removed from automation — this no longer
        # calls _try_ondevice_stage or build_ondevice_index. The server index
        # is already complete and gated by this point (final-print gate in
        # _try_index_server already passed to reach 'indexed_server' at all);
        # the set now just waits at the same manual pre-live hold every set
        # waits at for Craig's go-live decision.
        if dry_run:
            return {"ok": True, "from": "indexed_server", "to": "indexed_server", "dry_run": True}
        entry["state"] = "prelive_hold"
        return {"ok": True, "from": "indexed_server", "to": "prelive_hold"}

    if state == "prelive_hold":
        result = _try_go_live(entry, dry_run)
        if result.get("ok") and not dry_run:
            entry["state"] = "live"
            return {"ok": True, "from": "prelive_hold", "to": "live", **result}
        return {"ok": result.get("ok", False), "from": "prelive_hold", "to": "prelive_hold", **result}

    if state == "ondevice_staged":
        # Legacy/manual state — the automatic scheduler never transitions a
        # set INTO this anymore (see 'indexed_server' above), but if Craig
        # manually sets it for the old manual on-device workflow, advancing
        # out of it still works exactly as before.
        result = _try_go_live(entry, dry_run)
        if result.get("ok") and not dry_run:
            entry["state"] = "live"
            return {"ok": True, "from": "ondevice_staged", "to": "live", **result}
        return {"ok": result.get("ok", False), "from": "ondevice_staged", "to": "ondevice_staged", **result}

    return {"ok": False, "error": f"unknown_state:{state}"}


def _run_source_latency_probe(calendar: Dict, dry_run: bool) -> Dict:
    """Phase 6 — pure observation. Writes only source_latency timestamps and
    discovered_unlisted entries; touches no images.db, no SKU synthesis, no
    embedder, no R2. Runs every tick regardless of state-machine progress,
    for every calendar entry not yet 'live'."""
    summary: Dict = {"stamped": [], "backfilled_tcgdex_ids": [], "discovered_unlisted": []}
    if dry_run:
        return {"dry_run": True}

    pending_entries = [e for e in calendar.get("sets", []) if e.get("state") != "live"]
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # ── pokemontcg.io listing (once per tick) ──
    pokemontcg_listing: Dict[str, Dict] = {}
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.pokemontcg.io/v2/sets?select=id,name,releaseDate,total",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        for s in data.get("data", []):
            sid = s.get("id", "").lower()
            if sid:
                pokemontcg_listing[sid] = s
    except Exception as e:
        logger.error(f"[PROBE] pokemontcg.io listing failed: {e}")

    # ── TCGdex-EN listing (once per tick) ──
    tcgdex_listing = _tcgdex_en_listing()
    tcgdex_by_id = {s["id"].lower(): s for s in tcgdex_listing if s.get("id")}

    for entry in pending_entries:
        if entry.get("game") != "pokemon_en":
            continue
        name_lower = entry.get("name", "").strip().lower()
        sids = entry.setdefault("source_ids", {})

        # pokemontcg.io: match by id, else by name
        pkm_id = (sids.get("pokemontcg_io") or "").lower()
        pkm_match = pokemontcg_listing.get(pkm_id) if pkm_id else None
        if not pkm_match and not pkm_id:
            for s in pokemontcg_listing.values():
                if s.get("name", "").strip().lower() == name_lower:
                    pkm_match = s
                    break
        if pkm_match and (pkm_match.get("total") or 0) > 0:
            if _stamp_source_latency(entry, "pokemontcg_io", "first_listed_at"):
                summary["stamped"].append((entry["name"], "pokemontcg_io", "first_listed_at"))
            if _in_release_window(entry, today) and _pokemontcg_first_card_imaged(pkm_match["id"]):
                if _stamp_source_latency(entry, "pokemontcg_io", "first_imaged_at"):
                    summary["stamped"].append((entry["name"], "pokemontcg_io", "first_imaged_at"))

        # tcgdex_en: match by id, else by name; backfill id on first match
        tcgdex_id = (sids.get("tcgdex_en") or "").lower()
        tcgdex_match = tcgdex_by_id.get(tcgdex_id) if tcgdex_id else None
        if not tcgdex_match and not tcgdex_id:
            for s in tcgdex_listing:
                if s.get("name", "").strip().lower() == name_lower:
                    tcgdex_match = s
                    break
            if tcgdex_match:
                sids["tcgdex_en"] = tcgdex_match["id"]
                summary["backfilled_tcgdex_ids"].append((entry["name"], tcgdex_match["id"]))
        if tcgdex_match and (tcgdex_match.get("total") or 0) > 0:
            if _stamp_source_latency(entry, "tcgdex_en", "first_listed_at"):
                summary["stamped"].append((entry["name"], "tcgdex_en", "first_listed_at"))
            if _in_release_window(entry, today) and _tcgdex_en_first_card_imaged(tcgdex_match["id"]):
                if _stamp_source_latency(entry, "tcgdex_en", "first_imaged_at"):
                    summary["stamped"].append((entry["name"], "tcgdex_en", "first_imaged_at"))

    # ── Discovery augmentation: sets on either source matching NO calendar entry ──
    calendar_names = {e.get("name", "").strip().lower() for e in calendar.get("sets", [])}
    calendar_pkm_ids = {
        (e.get("source_ids", {}).get("pokemontcg_io") or "").lower()
        for e in calendar.get("sets", [])
    } - {""}
    calendar_tcgdex_ids = {
        (e.get("source_ids", {}).get("tcgdex_en") or "").lower()
        for e in calendar.get("sets", [])
    } - {""}
    discovered_keys = {(d.get("source"), d.get("observed_id")) for d in calendar.get("discovered_unlisted", [])}

    for sid, s in pokemontcg_listing.items():
        if sid in calendar_pkm_ids or s.get("name", "").strip().lower() in calendar_names:
            continue
        key = ("pokemontcg_io", sid)
        if key in discovered_keys:
            continue
        calendar.setdefault("discovered_unlisted", []).append({
            "source": "pokemontcg_io", "observed_id": sid, "name": s.get("name", ""),
            "cardCount": s.get("total"), "first_seen_at": datetime.utcnow().isoformat() + "Z",
            # releaseDate comes free in this same listing call (select=
            # id,name,releaseDate,total) — no extra request needed.
            "releaseDate": s.get("releaseDate", ""),
            "notified": False,
        })
        summary["discovered_unlisted"].append(("pokemontcg_io", sid, s.get("name", "")))

    for s in tcgdex_listing:
        sid = (s.get("id") or "").lower()
        if not sid or sid in calendar_tcgdex_ids or s.get("name", "").strip().lower() in calendar_names:
            continue
        key = ("tcgdex_en", sid)
        if key in discovered_keys:
            continue
        calendar.setdefault("discovered_unlisted", []).append({
            "source": "tcgdex_en", "observed_id": sid, "name": s.get("name", ""),
            "cardCount": s.get("total"), "first_seen_at": datetime.utcnow().isoformat() + "Z",
            # TCGdex's lightweight SetResume has no release-date field — left
            # absent rather than adding a getSync() call just to fetch one
            # (A4: no extra detail calls for date-only).
            "releaseDate": "",
            "notified": False,
        })
        summary["discovered_unlisted"].append(("tcgdex_en", sid, s.get("name", "")))

    return summary


def run_scheduler(
    tcgs:      Optional[List[str]] = None,
    dry_run:   bool = False,
    db_root:   Optional[str] = None,
    embed_fn=None,
) -> Dict:
    """
    Daily date-aware run (cron changed from weekly to daily — see
    matchit_modal.py):
    1. Legacy bulk detect+ingest per TCG (UNCHANGED behavior, minus any
       set_id already tracked in the release calendar — those are governed
       exclusively by the calendar state machine below, so this path can't
       race ahead of the release-date gate).
    2. Calendar state machine: advance every non-live entry exactly one
       transition (pokemon_en only this block).
    3. Source-latency probe (observation-only, every tick).
    4. Discovery notification (Tier 1 Part A) — noise-controlled, baseline-
       guarded alert for genuinely new sets seen on a source.
    5. Save log + event-driven email (Tier 1 Part B: Monday always sends
       the weekly summary enriched with this run's activity; other days
       send only if something happened) + price alerts.

    ENGLISH-ONLY BY DESIGN: this scheduler polls pokemontcg.io, Scryfall, and
    YGOProDeck — none of which carry Japanese cards. Japanese new-set
    ingestion (scraping new jpn- sets from TCGdex) is a separate MANUAL
    track, not detected or run automatically here. JP Cardmarket *pricing*
    for already-known jpn- cards IS automated (scheduled_jp_price_refresh,
    its own daily Modal cron in matchit_modal.py) — only new JP *sets*
    require a manual run. The weekly email below flags this each run so it
    doesn't get forgotten.

    Args:
        tcgs:    List of TCGs to check e.g. ['POKEMON', 'MTG'].
                 Defaults to CONFIG['tcgs_enabled'].
        dry_run: If True, detect only — don't download or embed.
        db_root: Override DB root path.

    Returns:
        run_summary dict.
    """
    t_start  = time.time()
    run_at   = datetime.now().isoformat()
    tcgs     = tcgs or CONFIG["tcgs_enabled"]
    db_root  = db_root or CONFIG["db_root"]
    today    = datetime.utcnow().strftime("%Y-%m-%d")

    run_summary: Dict = {
        "run_at":  run_at,
        "dry_run": dry_run,
        "tcgs":    {},
        "embed":   {},
        "calendar": {"advanced": [], "probe": {}},
        "new_skus": [],
        "status":  "ok",
    }

    logger.info(f"[SCHED] Starting daily scheduler run at {run_at} — TCGs: {tcgs}")

    try:
        calendar = _load_release_calendar()
    except Exception as e:
        logger.error(f"[CALENDAR] Load failed: {e} — skipping calendar state machine for this run")
        calendar = None
    _calendar_had_sets = bool(calendar.get("sets") if calendar else False)
    calendar_pokemon_ids = {
        (e.get("source_ids", {}).get("pokemontcg_io") or "").lower()
        for e in (calendar.get("sets", []) if calendar else [])
    } - {""}

    any_new = False

    # ── 1. Legacy bulk detect-and-FLAG per TCG (no download/scrape — see
    # policy comment below; minus calendar-claimed Pokemon set ids, see
    # _detect_new_pokemon_sets) ──
    for tcg in tcgs:
        tcg = tcg.upper()
        logger.info(f"[SCHED] Checking {tcg}...")
        tcg_result: Dict = {"new_sets": [], "downloaded": 0, "failed": 0}

        try:
            if tcg == "POKEMON":
                new_sets = _detect_new_pokemon_sets(excluded_ids=calendar_pokemon_ids)
            elif tcg == "MTG":
                new_sets = _detect_new_mtg_sets()
            elif tcg == "YUGIOH":
                new_sets = _detect_new_yugioh_sets()
            else:
                logger.warning(f"[SCHED] Unknown TCG: {tcg}")
                continue

            tcg_result["new_sets"] = new_sets

            if new_sets and tcg in ("POKEMON", "MTG", "YUGIOH"):
                # Policy: ALL THREE games are detect-and-FLAG only in the
                # legacy bulk loop — no auto-scrape, download, or embed.
                # Craig reviews and triggers ingestion manually (see ACTION
                # NEEDED in the Tier 1 email). Pokémon-EN ingestion now flows
                # exclusively through the calendar state machine
                # (_advance_entry -> _try_catalog_ingest -> _scrape_pokemon),
                # which keeps the final-print + release-date gates; this
                # legacy path no longer scrapes Pokémon at all. This
                # deliberately does NOT set any_new — no images were added
                # to CardsDB, so there is nothing for step 2's incremental
                # embed (or part C's sidecar rebuilds) to pick up yet.
                run_summary.setdefault("flagged_manual", {})[tcg] = [
                    {"id": s.get("id"), "name": s.get("name", s.get("id"))}
                    for s in new_sets
                ]
                logger.info(
                    f"[SCHED] {tcg}: {len(new_sets)} new set(s) flagged for "
                    f"manual action (no auto-scrape)"
                )

        except Exception as e:
            logger.error(f"[SCHED] {tcg} error: {e}")
            tcg_result["error"] = str(e)

        run_summary["tcgs"][tcg] = tcg_result

    # ── 2. Incremental embed (legacy bulk path) ──
    # GUARDRAIL: incremental_embed is APPEND-ONLY (~new cards). NEVER replace
    # with a full whole-index re-embed on any automatic/cron path — full GPU
    # re-embed is manual-only (cost).
    if any_new and not dry_run:
        logger.info("[SCHED] Running incremental embed...")
        try:
            from incremental_embed import run_incremental_embed
            embed_result = run_incremental_embed(hot_reload=True)
            run_summary["embed"] = embed_result
        except Exception as e:
            logger.error(f"[SCHED] Embed error: {e}")
            run_summary["embed"] = {"status": f"error: {e}"}
    else:
        run_summary["embed"] = {
            "new_front": 0, "total_front": 0, "elapsed_s": 0,
            "status": "skipped_no_new_sets" if not any_new else "dry_run",
        }

    # ── 2b. Sync profiles to CardsDB (legacy bulk path) ──
    if any_new and not dry_run:
        sets_by_game = {
            game.lower(): [s["id"] for s in data.get("new_sets", [])]
            for game, data in run_summary["tcgs"].items()
            if data.get("new_sets")
        }
        if sets_by_game:
            try:
                from sync_profiles import sync_profiles_to_cardsdb
                sync_result = sync_profiles_to_cardsdb(sets_by_game)
                run_summary["sync"] = sync_result
            except Exception as e:
                logger.error(f"[SCHED] Profile sync error: {e}")
                run_summary["sync"] = {"copied": 0, "updated": 0, "skipped": 0, "errors": [str(e)]}

    # ── 3. Calendar state machine: advance every non-live entry exactly one
    # transition (pokemon_en only this block). Idempotent — failures leave
    # `state` unchanged for tomorrow's retry. ──
    if calendar is None:
        logger.warning("[CALENDAR] Skipping state machine — calendar failed to load")
    else:
        if not dry_run:
            for entry in calendar.get("sets", []):
                if entry.get("state") == "live":
                    continue
                try:
                    advance_result = _advance_entry(entry, today, db_root, dry_run, embed_fn=embed_fn)
                except Exception as e:
                    logger.error(f"[SCHED-STATE] Unhandled error advancing {entry.get('name')}: {e}")
                    advance_result = {"ok": False, "error": str(e)}
                new_skus = advance_result.pop("new_skus", None)
                if new_skus:
                    run_summary["new_skus"].extend(new_skus)
                run_summary["calendar"]["advanced"].append({
                    "name": entry.get("name"), "state": entry.get("state"), "result": advance_result,
                })
        else:
            run_summary["calendar"]["advanced"] = [
                {"name": e.get("name"), "state": e.get("state"), "result": {"dry_run": True}}
                for e in calendar.get("sets", []) if e.get("state") != "live"
            ]

    # ── 4. Source-latency probe (observation-only, every tick) ──
    try:
        run_summary["calendar"]["probe"] = _run_source_latency_probe(calendar, dry_run)
    except Exception as e:
        logger.error(f"[SCHED-PROBE] Unhandled error: {e}")
        run_summary["calendar"]["probe"] = {"error": str(e)}

    # ── Tier 1 Part A: discovery notification processing (noise-controlled) ──
    # Runs after the probe so this run's own fresh discoveries are covered
    # by the first-run baseline too. dry_run never mutates notified flags.
    notify_worthy_discoveries: List[Dict] = []
    if not dry_run:
        try:
            notify_worthy_discoveries = _process_discovery_notifications(calendar)
        except Exception as e:
            logger.error(f"[DISCOVERY] Unhandled error: {e}")
    run_summary["calendar"]["notify_worthy_discoveries"] = notify_worthy_discoveries

    if not dry_run:
        if calendar is None:
            pass  # already logged above
        elif not calendar.get("sets") and _calendar_had_sets:
            logger.error("[CALENDAR] Refusing to save — sets array is empty but was non-empty at load. Possible load failure.")
        else:
            _save_release_calendar(calendar)

    # Combined "did anything actually change today" signal for 2c/2d below.
    # any_new alone is legacy-path only (Pokémon's _scrape_pokemon branch);
    # calendar-driven Pokémon changes show up in run_summary["new_skus"]
    # instead (populated by step 3 above) — must check both.
    changed = bool(any_new) or bool(run_summary.get("new_skus"))

    # ── 2c. Rebuild MTG set totals sidecar ──
    # Now gated on `changed` — only runs when something was actually
    # scraped/ingested today, not unconditionally every day.
    if not dry_run and changed:
        try:
            from app import rebuild_mtg_set_totals
            run_summary["mtg_totals"] = rebuild_mtg_set_totals()
        except Exception as e:
            logger.error(f"[SCHED] MTG totals rebuild error: {e}")
            run_summary["mtg_totals"] = {"error": str(e)}

    # ── 2d. Rebuild set card-list sidecars (pokemon/mtg/ygo) ──
    # Same `changed` gate as 2c.
    if not dry_run and changed:
        try:
            from app import rebuild_set_card_lists
            run_summary["set_cards"] = rebuild_set_card_lists()
        except Exception as e:
            logger.error(f"[SCHED] Set card-list rebuild error: {e}")
            run_summary["set_cards"] = {"error": str(e)}

        try:
            from app import rebuild_identifier_lookup
            run_summary["identifier_lookup"] = \
                rebuild_identifier_lookup()
        except Exception as e:
            logger.error(
                f"[SCHED] rebuild_identifier_lookup failed: {e}"
            )

        try:
            from build_pokemon_search_index import \
                build_pokemon_search_index
            run_summary["pokemon_search_index"] = \
                build_pokemon_search_index()
        except Exception as e:
            logger.error(
                f"[SCHED] build_pokemon_search_index failed: {e}"
            )

    # ── 2e. Refresh the FX rate cache (single source for all 5 GBP consumers) ──
    # Same "runs every day regardless of any_new" rationale as 2c/2d. Runs
    # before step 5 (price alerts) so this same run's alert check uses the
    # freshly-refreshed rate rather than staying stale until tomorrow.
    if not dry_run:
        try:
            from fx_rates import refresh_fx_rates
            run_summary["fx_refresh"] = refresh_fx_rates()
        except Exception as e:
            logger.error(f"[SCHED] FX rate refresh error: {e}")
            run_summary["fx_refresh"] = {"error": str(e)}

    # Full reconcile is now manual-only (delta-only policy — see
    # matchit_modal.py rebuild_lookup_files invocation, which no longer
    # forces a full rebuild on Sundays or ever automatically). Key kept
    # at False rather than removed, in case anything still inspects it.
    run_summary["force_full_reconcile"] = False

    run_summary["elapsed_s"] = round(time.time() - t_start, 1)

    # ── Save log ──
    _save_scheduler_log(run_summary)

    # ── Tier 1 Part B: consolidated event-driven email (or Monday summary) ──
    # Monday always sends; non-Monday only if something happened this run
    # (a transition, a notify-worthy discovery, or a newly flagged-manual
    # set) — never two emails, never a new send path (still _send_email
    # under the hood).
    is_monday = (datetime.utcnow().weekday() == 0)  # Monday=0
    transitions = _collect_transitions(run_summary)
    run_summary["calendar"]["transitions"] = transitions
    if not dry_run:
        try:
            _maybe_send_consolidated_email(run_summary, transitions, notify_worthy_discoveries, is_monday)
        except Exception as e:
            logger.error(f"[SCHED-EMAIL] Unhandled error: {e}")
    else:
        logger.info("[SCHED-EMAIL] dry_run — no email sent")

    # ── Check price alerts ──
    try:
        check_price_alerts()
    except Exception as e:
        logger.error(f"[SCHED] Price alert check error: {e}")

    logger.info(
        f"[SCHED] Done in {run_summary['elapsed_s']}s — "
        f"any_new={any_new}, dry_run={dry_run}, "
        f"calendar_advanced={len(run_summary['calendar']['advanced'])}"
    )
    return run_summary


# ── Modal entrypoint (manual trigger against the live volume) ──────────
# Usage:
#   modal run set_scheduler.py::rebuild_mtg_totals
#
# Bakes mtg_set_totals.json on the live volume directly, without running
# the full weekly scheduler (no scraping, no embedding, no email). Use
# this for the initial bake; the Monday scheduler calls the same
# app.rebuild_mtg_set_totals() automatically afterwards (see step 2c above).
try:
    import sys as _sys
    _sys.path.insert(0, "/app")
    import modal as _modal
    from modal_config import vol as _vol, image as _modal_image
    _modal_app = _modal.App("grailsweep-mtg-totals")
    _HAS_MODAL = True
except Exception:
    _HAS_MODAL = False

if _HAS_MODAL:
    @_modal_app.function(
        image=_modal_image,
        volumes={"/modal_data": _vol},
        timeout=3600,
    )
    def _rebuild_mtg_totals_remote() -> dict:
        import os as _os_inner
        _os_inner.chdir("/app")
        _os_inner.environ["LOCALAPPDATA"] = "/modal_data"
        _vol.reload()
        # app.py's load_vertical() reads /app/verticals/cards/vertical.json
        # at import time and caches it for the process lifetime. The
        # checked-in file has db_root="C:\\CardsDB" (the local dev path);
        # production's serve() patches it to "/modal_data/CardsDB" before
        # ever importing app — reuse that exact patch here so this
        # standalone bake sees the real CardsDB tree instead of a path
        # that can never resolve on Linux.
        from matchit_modal import _fix_vertical_config
        _fix_vertical_config()
        from app import rebuild_mtg_set_totals
        result = rebuild_mtg_set_totals()
        try:
            _vol.commit()
            result["vol_commit"] = "OK"
        except Exception as e:
            result["vol_commit"] = f"FAILED: {e}"
        print(f"[MTG-TOTALS] {result}", flush=True)
        return result

    @_modal_app.local_entrypoint()
    def rebuild_mtg_totals():
        """Bake mtg_set_totals.json against the live Modal volume (manual, one-off)."""
        result = _rebuild_mtg_totals_remote.remote()
        print(json.dumps(result, indent=2))

    # Same App, same pattern — set-detail card-list sidecars (pokemon/mtg/ygo).
    # Usage:
    #   modal run set_scheduler.py::rebuild_set_cards
    @_modal_app.function(
        image=_modal_image,
        volumes={"/modal_data": _vol},
        timeout=3600,
    )
    def _rebuild_set_cards_remote() -> dict:
        import os as _os_inner
        _os_inner.chdir("/app")
        _os_inner.environ["LOCALAPPDATA"] = "/modal_data"
        _vol.reload()
        from matchit_modal import _fix_vertical_config
        _fix_vertical_config()
        from app import rebuild_set_card_lists
        result = rebuild_set_card_lists()
        try:
            _vol.commit()
            result["vol_commit"] = "OK"
        except Exception as e:
            result["vol_commit"] = f"FAILED: {e}"
        print(f"[SET-CARDS] {result}", flush=True)
        return result

    @_modal_app.local_entrypoint()
    def rebuild_set_cards():
        """Bake the per-game set card-list sidecars against the live Modal volume (manual, one-off)."""
        result = _rebuild_set_cards_remote.remote()
        print(json.dumps(result, indent=2))


# ─────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="MatchIT new set scheduler"
    )
    parser.add_argument(
        "--tcg",
        help="Comma-separated TCGs to check e.g. POKEMON,MTG (default: all)",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect new sets only — don't download or embed",
    )
    parser.add_argument(
        "--db-root",
        help="Override DB root path",
        default=None,
    )
    # Email config overrides
    parser.add_argument("--email-from",     default=None)
    parser.add_argument("--email-to",       default=None)
    parser.add_argument("--email-password", default=None)

    args = parser.parse_args()

    # Apply CLI overrides to CONFIG
    if args.email_from:     CONFIG["email_from"]     = args.email_from
    if args.email_to:       CONFIG["email_to"]       = args.email_to
    if args.email_password: CONFIG["email_password"] = args.email_password

    tcgs = [t.strip().upper() for t in args.tcg.split(",")] if args.tcg else None

    result = run_scheduler(
        tcgs=tcgs,
        dry_run=args.dry_run,
        db_root=args.db_root,
    )

    print("\n=== Scheduler Run Summary ===")
    for tcg, data in result.get("tcgs", {}).items():
        new = data.get("new_sets", [])
        print(f"  {tcg}: {len(new)} new set(s)")
        for s in new:
            print(f"    + {s['id']} — {s['name']}")
    embed = result.get("embed", {})
    print(f"\n  Embeddings added: {embed.get('new_front', 0)}")
    print(f"  Total in DB:      {embed.get('total_front', 0)}")
    print(f"  Elapsed:          {result.get('elapsed_s', 0)}s")